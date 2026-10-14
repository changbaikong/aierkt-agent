"""Persistent scheduler and restricted task worker."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .contracts import AccountTaskScope, AgentRequest, Scope
from .events import Event, EventBus, EventType
from .loop import AgentLoop
from .runner import AgentRunner
from .task_persistence import TaskRecord, TaskStore
from .tools import AllowAllRiskPolicy, ToolRegistry, classify_tool_error

#: 后台定时任务与主聊天共用同一份整轮预算（取自 AgentLoop 的类默认值）。
#: 后台任务同样会走联网取证与核查，按老口径 12000 会在长任务上收口得太早。
TURN_TOKEN_BUDGET = AgentLoop.MAX_TOTAL_TOKENS

logger = logging.getLogger(__name__)

try:
    from croniter import croniter
except ImportError:  # pragma: no cover - dependency is declared in requirements
    croniter = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str, timezone_name: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def normalize_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be an object")
    # Accept the short form emitted by older task prompts so persisted
    # confirmations can still be resumed after the schedule contract changed.
    if not any(schedule.get(key) is not None for key in ("run_at", "interval_seconds", "cron")):
        if schedule.get("hour") is not None or schedule.get("minute") is not None:
            try:
                hour = int(schedule.get("hour"))
                minute = int(schedule.get("minute", 0))
            except (TypeError, ValueError) as exc:
                raise ValueError("hour and minute must be integers") from exc
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError("hour must be 0-23 and minute must be 0-59")
            timezone_name = str(schedule.get("timezone") or "Asia/Shanghai")
            try:
                local_zone = ZoneInfo(timezone_name)
            except Exception as exc:
                raise ValueError(f"invalid timezone: {timezone_name}") from exc
            date_text = schedule.get("date")
            if date_text:
                try:
                    local_date = datetime.fromisoformat(str(date_text)).date()
                except ValueError as exc:
                    raise ValueError("date must be an ISO-8601 date") from exc
            else:
                local_date = datetime.now(local_zone).date()
            candidate = datetime.combine(local_date, datetime.min.time()).replace(
                hour=hour, minute=minute, tzinfo=local_zone
            )
            if candidate <= datetime.now(local_zone):
                candidate += timedelta(days=1)
            return {
                "kind": "run_at",
                "run_at": candidate.astimezone(timezone.utc).isoformat(timespec="seconds"),
                "timezone": timezone_name,
            }
    keys = [key for key in ("run_at", "interval_seconds", "cron") if schedule.get(key) is not None]
    if len(keys) != 1:
        raise ValueError("schedule must contain exactly one of run_at, interval_seconds, cron")
    timezone_name = str(schedule.get("timezone") or "UTC")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError(f"invalid timezone: {timezone_name}") from exc
    if keys[0] == "run_at":
        instant = _parse_dt(str(schedule["run_at"]), timezone_name)
        if instant <= _utc_now():
            raise ValueError("run_at must be in the future")
        return {"kind": "run_at", "run_at": instant.isoformat(timespec="seconds"), "timezone": timezone_name}
    if keys[0] == "interval_seconds":
        try:
            seconds = int(schedule["interval_seconds"])
        except (TypeError, ValueError) as exc:
            raise ValueError("interval_seconds must be an integer") from exc
        if seconds < 60:
            raise ValueError("interval_seconds must be at least 60")
        return {"kind": "interval", "interval_seconds": seconds, "timezone": timezone_name}
    expression = str(schedule["cron"]).strip()
    if len(expression.split()) != 5:
        raise ValueError("cron must contain exactly five fields")
    if croniter is None:
        raise ValueError("cron scheduling requires croniter")
    try:
        croniter(expression, datetime.now(ZoneInfo(timezone_name)))
    except Exception as exc:
        raise ValueError(f"invalid cron: {expression}") from exc
    return {"kind": "cron", "cron": expression, "timezone": timezone_name}


def next_run_at(schedule: dict[str, Any], *, now: datetime | None = None) -> str | None:
    now = now or _utc_now()
    kind = schedule["kind"]
    if kind == "run_at":
        return schedule["run_at"]
    if kind == "interval":
        return (now + timedelta(seconds=int(schedule["interval_seconds"]))).astimezone(timezone.utc).isoformat(timespec="seconds")
    if croniter is None:
        raise ValueError("cron scheduling requires croniter")
    local_now = now.astimezone(ZoneInfo(schedule["timezone"]))
    value = croniter(schedule["cron"], local_now).get_next(datetime)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class TaskService:
    def __init__(
        self,
        *,
        provider: Any,
        registry_provider: Callable[[], ToolRegistry],
        store: TaskStore,
        event_bus: EventBus | None = None,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int = 300,
        poll_seconds: float = 1.0,
        runner: AgentRunner | None = None,
    ) -> None:
        self.provider = provider
        self.registry_provider = registry_provider
        self.store = store
        self.event_bus = event_bus
        self.now = now or _utc_now
        self.lease_seconds = lease_seconds
        self.poll_seconds = poll_seconds
        self._worker: asyncio.Task[None] | None = None
        self._closed = False
        self.runner = runner or AgentRunner(None, None)

    async def _emit(self, event_type: EventType, task: TaskRecord, **data: Any) -> None:
        if self.event_bus:
            try:
                await self.event_bus.emit(Event(event_type, self.now(), {
                    "owner_id": task.scope.owner_id, "channel": task.scope.channel,
                    "conversation_id": task.scope.conversation_id, "module_id": task.scope.module_id,
                }, {"task_id": task.task_id, "title": task.title, **data}))
            except Exception:
                pass

    def _validate_tools(self, allowed_tools: list[str], delivery_mode: str) -> list[str]:
        if not isinstance(allowed_tools, list):
            raise ValueError("allowed_tools must be a list")
        registry = self.registry_provider()
        result: list[str] = []
        for name in allowed_tools:
            tool_name = str(name)
            spec = registry.resolve(tool_name)
            if spec is None:
                raise ValueError(f"unknown tool: {tool_name}")
            if delivery_mode == "execute" and (spec.risk == "high" or tool_name.startswith("tasks__")):
                raise ValueError(f"tool is not allowed for background execution: {tool_name}")
            if tool_name not in result:
                result.append(tool_name)
        return result

    async def create_task(self, *, scope: Scope, title: str, request_text: str, delivery_mode: str, schedule: dict[str, Any], allowed_tools: list[str]) -> TaskRecord:
        if delivery_mode not in {"notify", "execute"}:
            raise ValueError("delivery_mode must be notify or execute")
        if not str(title).strip() or not str(request_text).strip():
            raise ValueError("title and request_text are required")
        normalized = normalize_schedule(schedule)
        tools = self._validate_tools(allowed_tools, delivery_mode)
        initial = next_run_at(normalized, now=self.now())
        return self.store.create_task(scope=scope, title=title.strip(), request_text=request_text.strip(), delivery_mode=delivery_mode, schedule=normalized, allowed_tools=tools, next_run_at=initial)

    async def cancel_task(self, task_id: str, scope: Scope | AccountTaskScope) -> bool:
        # 存储是同步 SQLite：调度器与 HTTP 接口都跑在同一个 event loop 上，
        # 直接在 async 里调它 = 让扫表阻塞 SSE 与聊天流。统一走工作线程。
        task = await asyncio.to_thread(self.store.get_task, task_id, scope)
        changed = await asyncio.to_thread(self.store.cancel_task, task_id, scope)
        if changed and task:
            await self._emit(EventType.TASK_CANCELLED, task)
        return changed

    async def run_now(self, task_id: str, scope: Scope | AccountTaskScope) -> bool:
        task = await asyncio.to_thread(self.store.get_task, task_id, scope)
        if not task or task.status != "scheduled":
            return False
        due_at = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        if not await asyncio.to_thread(self.store.set_due_now, task_id, scope, due_at):
            return False
        await self.tick()
        return True

    async def tick(self) -> int:
        # 这一整轮里的存储调用都挪进工作线程，但**顺序不变**：
        # 到期查询 → 逐条抢占 → 执行 → 结算，任一步都依赖前一步的结果。
        # 它们是"依次等待"，不是"并发发起"——并发会让抢占与结算错位。
        due_at = self.now().astimezone(timezone.utc).isoformat(timespec="seconds")
        due = await asyncio.to_thread(self.store.due_tasks, due_at)
        processed = 0
        for task in due:
            try:
                claimed = await asyncio.to_thread(
                    self.store.claim_due_task, task.task_id, task.scope, self.lease_seconds, now=self.now()
                )
            except Exception:
                continue
            if claimed is None:
                continue
            processed += 1
            try:
                await self._emit(EventType.TASK_STARTED, task, run_id=claimed.run_id)
            except Exception:
                pass
            try:
                if task.delivery_mode == "notify":
                    await self._emit(EventType.TASK_DUE, task, run_id=claimed.run_id, title=task.title, request_text=task.request_text)
                    result = "task due"
                else:
                    result = await asyncio.wait_for(
                        self._execute(task), timeout=max(1, self.lease_seconds - 1)
                    )
                schedule = task.schedule
                next_at = None if schedule["kind"] == "run_at" else next_run_at(schedule, now=self.now())
                await asyncio.to_thread(
                    self.store.finish_run, task.task_id, claimed.run_id, task.scope,
                    status="completed", result=result, next_run_at=next_at, lease_id=claimed.lease_id,
                )
                await self._emit(EventType.TASK_COMPLETED, task, run_id=claimed.run_id, result=result)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                # 稳定错误码：异常原文只进日志与 TaskStore，事件里额外带一个
                # 可枚举的 code，让下游（回执桥 / 前端 / 运维）不必去解析原文。
                error_code = classify_tool_error(exc)
                await asyncio.to_thread(
                    self.store.finish_run, task.task_id, claimed.run_id, task.scope,
                    status="failed", error=error, lease_id=claimed.lease_id,
                )
                await self._emit(EventType.TASK_FAILED, task, run_id=claimed.run_id, error=error, error_code=error_code)
        return processed

    async def _execute(self, task: TaskRecord) -> str:
        if self.provider is None:
            raise RuntimeError("task execution provider is not configured")
        source = self.registry_provider()
        restricted = ToolRegistry()
        for name in task.allowed_tools:
            spec = source.resolve(name)
            if spec is not None:
                restricted.register(spec)
        # 定时任务与主聊天用同一份整轮预算口径（见 TURN_TOKEN_BUDGET=35000）。
        loop = AgentLoop(
            self.provider, restricted, task.scope,
            risk_policy=AllowAllRiskPolicy(), max_steps=5,
            max_total_tokens=TURN_TOKEN_BUDGET, event_bus=self.event_bus,
        )
        outcome = await self.runner.run_internal(
            loop,
            AgentRequest(task.request_text, scope=task.scope, system_prompt="This is a background task. Use only the provided tools and do not create or modify tasks."),
        )
        if outcome.status != "completed":
            raise RuntimeError(outcome.reply or outcome.status)
        return outcome.reply

    async def start(self) -> None:
        if self._worker is None:
            self._closed = False
            self._worker = asyncio.create_task(self._run(), name="task-scheduler")

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.tick()
            except Exception:
                logger.exception("task scheduler tick failed")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
