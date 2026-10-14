"""Optional, persistent condition-based reminder service.

This module deliberately owns its storage and scheduler so it can be removed
without changing the core fixed-time task scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from agent_core.actions import (
    ERROR_ORIGIN_NOT_ALLOWED,
    ERROR_UNKNOWN_ACTION,
    ActionGateway,
    LateRegistry,
)
from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, AccountTaskScope, ActionRequest, Scope, ToolSpec
from agent_core.events import Event, EventBus, EventType
from agent_core.tools import ToolRegistry, classify_tool_error


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_INTERVAL_SECONDS = 1800
DEFAULT_WINDOW = {"start": "08:00", "end": "18:00"}
WEATHER_SEARCH_TOOL = "search__web"
#: 重试也救不回来的错误码。见 `_is_unrecoverable`。
_UNRECOVERABLE_ACTION_CODES = frozenset({ERROR_UNKNOWN_ACTION, ERROR_ORIGIN_NOT_ALLOWED})
logger = logging.getLogger(__name__)
_CLEAR_TERMS = ("晴朗", "晴天", "少云", "转晴", "天气晴")
_BAD_TERMS = ("雨", "雪", "雷", "雾", "霾", "沙尘", "冰雹")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _scope_args(scope: Scope) -> tuple[str, str, str, str]:
    return scope.owner_id, scope.channel, scope.conversation_id, scope.module_id


def _account_args(scope: Scope | AccountTaskScope) -> tuple[str, str]:
    if isinstance(scope, AccountTaskScope):
        return scope.account_id, scope.module_id
    return scope.owner_id, scope.module_id


def _parse_clock(value: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (AttributeError, ValueError) as exc:
        raise ValueError("daily_window must use HH:MM values") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("daily_window must use valid HH:MM values")
    return hour, minute


def _normalise_window(value: dict[str, Any] | None) -> dict[str, str]:
    window = dict(DEFAULT_WINDOW)
    if value:
        window.update({key: str(item) for key, item in value.items() if key in window})
    start = _parse_clock(window["start"])
    end = _parse_clock(window["end"])
    if start >= end:
        raise ValueError("daily_window start must be before end")
    return window


def _within_window(now: datetime, timezone_name: str, window: dict[str, str]) -> bool:
    local = now.astimezone(ZoneInfo(timezone_name))
    start_hour, start_minute = _parse_clock(window["start"])
    end_hour, end_minute = _parse_clock(window["end"])
    current = (local.hour, local.minute)
    return (start_hour, start_minute) <= current < (end_hour, end_minute)


def _next_window_start(now: datetime, timezone_name: str, window: dict[str, str]) -> datetime:
    local = now.astimezone(ZoneInfo(timezone_name))
    hour, minute = _parse_clock(window["start"])
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _next_check(now: datetime, timezone_name: str, window: dict[str, str], interval_seconds: int) -> str:
    candidate = now + timedelta(seconds=interval_seconds)
    if not _within_window(candidate, timezone_name, window):
        candidate = _next_window_start(now, timezone_name, window)
    return _iso(candidate)


def _is_unrecoverable(observation: Any) -> bool:
    """这条失败是不是"重试也救不回来"的那类。

    判据只看网关回的**稳定错误码**（`error` 里冒号前那一段）：
    工具不存在、这个来源不被允许 —— 都是配置级的事实，不会因为再试一次而改变。
    其余（超时、transient、handler 抛错）都按"改期再试"处理。

    **为什么按码不按措辞**：措辞会随网关的版本被调整，按 `in` 匹配句子
    的判断会在那时静默失效 —— 而它失效的症状恰恰是"任务无限重排"，很难发现。
    """
    code = str(getattr(observation, "error", "") or "").split(":", 1)[0].strip()
    return code in _UNRECOVERABLE_ACTION_CODES


def _weather_verdict(output: dict[str, Any]) -> tuple[bool, str]:
    results = output.get("results")
    if not isinstance(results, list):
        return False, "天气条件不明确"
    texts: list[str] = []
    for result in results[:5]:
        if isinstance(result, dict):
            texts.extend(str(result.get(key) or "") for key in ("title", "snippet", "summary"))
    text = " ".join(texts)
    if any(term in text for term in _BAD_TERMS):
        return False, "天气条件不明确"
    if any(term in text for term in _CLEAR_TERMS):
        return True, "天气明确晴朗"
    return False, "天气条件不明确"


@dataclass(frozen=True)
class ConditionTask:
    task_id: str
    scope: Scope
    title: str
    request_text: str
    city: str
    condition: str
    timezone: str
    check_interval_seconds: int
    daily_window: dict[str, str]
    allowed_tools: list[str]
    status: str
    next_check_at: str | None
    last_summary: str | None
    last_error: str | None
    triggered_at: str | None
    created_at: str
    updated_at: str


class ConditionTaskStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS condition_tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, module_id TEXT NOT NULL,
                    title TEXT NOT NULL, request_text TEXT NOT NULL, city TEXT NOT NULL,
                    condition TEXT NOT NULL, timezone TEXT NOT NULL,
                    check_interval_seconds INTEGER NOT NULL, daily_window_json TEXT NOT NULL,
                    allowed_tools_json TEXT NOT NULL, status TEXT NOT NULL,
                    lease_id TEXT, lease_expires_at TEXT,
                    next_check_at TEXT, last_summary TEXT, last_error TEXT, triggered_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_condition_tasks_due
                    ON condition_tasks(status, next_check_at);
                CREATE INDEX IF NOT EXISTS idx_condition_tasks_scope
                    ON condition_tasks(owner_id, channel, conversation_id, module_id);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(condition_tasks)")}
            if "lease_id" not in columns:
                db.execute("ALTER TABLE condition_tasks ADD COLUMN lease_id TEXT")
            if "lease_expires_at" not in columns:
                db.execute("ALTER TABLE condition_tasks ADD COLUMN lease_expires_at TEXT")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @staticmethod
    def _row(row: sqlite3.Row) -> ConditionTask:
        scope = Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
        return ConditionTask(
            row["task_id"], scope, row["title"], row["request_text"], row["city"], row["condition"],
            row["timezone"], int(row["check_interval_seconds"]), json.loads(row["daily_window_json"]),
            json.loads(row["allowed_tools_json"]), row["status"], row["next_check_at"], row["last_summary"],
            row["last_error"], row["triggered_at"], row["created_at"], row["updated_at"],
        )

    def create(self, task: ConditionTask) -> ConditionTask:
        with self._connect() as db:
            db.execute(
                """INSERT INTO condition_tasks
                (task_id, owner_id, channel, conversation_id, module_id, title, request_text, city,
                 condition, timezone, check_interval_seconds, daily_window_json, allowed_tools_json,
                 status, lease_id, lease_expires_at, next_check_at, last_summary, last_error, triggered_at,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id, *_scope_args(task.scope), task.title, task.request_text, task.city, task.condition,
                    task.timezone, task.check_interval_seconds, json.dumps(task.daily_window),
                    json.dumps(task.allowed_tools), task.status, task.next_check_at, task.last_summary, task.last_error,
                    task.triggered_at, task.created_at, task.updated_at,
                ),
            )
        return task

    def get(self, task_id: str, scope: Scope | AccountTaskScope) -> ConditionTask | None:
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                row = db.execute("SELECT * FROM condition_tasks WHERE task_id=? AND owner_id=? AND module_id=?", (task_id, *_account_args(scope))).fetchone()
            else:
                row = db.execute("SELECT * FROM condition_tasks WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?", (task_id, *_scope_args(scope))).fetchone()
        return self._row(row) if row else None

    def list(self, scope: Scope | AccountTaskScope) -> list[ConditionTask]:
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                rows = db.execute("SELECT * FROM condition_tasks WHERE owner_id=? AND module_id=? ORDER BY created_at DESC", _account_args(scope)).fetchall()
            else:
                rows = db.execute("SELECT * FROM condition_tasks WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? ORDER BY created_at DESC", _scope_args(scope)).fetchall()
        return [self._row(row) for row in rows]

    def due(self, now: str) -> list[ConditionTask]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM condition_tasks WHERE status='scheduled' AND next_check_at IS NOT NULL AND next_check_at<=? ORDER BY next_check_at LIMIT 100",
                (now,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def claim(self, task: ConditionTask, now: str, lease_seconds: int = 300) -> bool:
        lease_id = uuid.uuid4().hex
        expires = _iso(datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(seconds=max(1, lease_seconds)))
        with self._connect() as db:
            changed = db.execute(
                """UPDATE condition_tasks SET status='checking', lease_id=?, lease_expires_at=?, updated_at=? WHERE task_id=?
                AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='scheduled'""",
                (lease_id, expires, now, task.task_id, *_scope_args(task.scope)),
            )
        return changed.rowcount == 1

    def reschedule(self, task: ConditionTask, *, next_check_at: str, summary: str | None = None, error: str | None = None) -> bool:
        now = _iso(_utc_now())
        with self._connect() as db:
            changed = db.execute(
                """UPDATE condition_tasks SET status='scheduled', lease_id=NULL, lease_expires_at=NULL, next_check_at=?, last_summary=?, last_error=?, updated_at=?
                WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='checking'""",
                (next_check_at, summary, error, now, task.task_id, *_scope_args(task.scope)),
            )
        return changed.rowcount == 1

    def complete(self, task: ConditionTask, summary: str, triggered_at: str) -> bool:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE condition_tasks SET status='completed', lease_id=NULL, lease_expires_at=NULL, next_check_at=NULL, last_summary=?, last_error=NULL,
                triggered_at=?, updated_at=? WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=?
                AND module_id=? AND status='checking'""",
                (summary, triggered_at, triggered_at, task.task_id, *_scope_args(task.scope)),
            )
        return changed.rowcount == 1

    def block(self, task: ConditionTask, error: str) -> bool:
        now = _iso(_utc_now())
        with self._connect() as db:
            changed = db.execute(
                """UPDATE condition_tasks SET status='blocked', lease_id=NULL, lease_expires_at=NULL, last_error=?, updated_at=? WHERE task_id=?
                AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='checking'""",
                (error, now, task.task_id, *_scope_args(task.scope)),
            )
        return changed.rowcount == 1

    def cancel(self, task_id: str, scope: Scope | AccountTaskScope) -> bool:
        now = _iso(_utc_now())
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                changed = db.execute("UPDATE condition_tasks SET status='cancelled', next_check_at=NULL, updated_at=? WHERE task_id=? AND owner_id=? AND module_id=? AND status IN ('scheduled', 'checking', 'blocked')", (now, task_id, *_account_args(scope)))
            else:
                changed = db.execute("UPDATE condition_tasks SET status='cancelled', next_check_at=NULL, updated_at=? WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status IN ('scheduled', 'checking', 'blocked')", (now, task_id, *_scope_args(scope)))
        return changed.rowcount == 1

    def set_due_now(self, task_id: str, scope: Scope | AccountTaskScope, now: str) -> bool:
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                changed = db.execute("UPDATE condition_tasks SET status='scheduled', next_check_at=?, updated_at=? WHERE task_id=? AND owner_id=? AND module_id=? AND status='scheduled'", (now, now, task_id, *_account_args(scope)))
            else:
                changed = db.execute("UPDATE condition_tasks SET status='scheduled', next_check_at=?, updated_at=? WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='scheduled'", (now, now, task_id, *_scope_args(scope)))
        return changed.rowcount == 1

    def recover_interrupted_checks(self, now: str) -> int:
        with self._connect() as db:
            changed = db.execute(
                """UPDATE condition_tasks SET status='scheduled', lease_id=NULL, lease_expires_at=NULL, next_check_at=?, updated_at=?
                WHERE status='checking' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)""",
                (now, now, now),
            )
        return changed.rowcount


class ConditionTaskService:
    """后台条件任务服务。

    Attributes:
        gateway: 动作网关。**执行工具的唯一入口**（2026-09-21 起）。
            在此之前这里自己 `registry_provider().resolve(...)` 再
            `ToolExecutor().execute(...)` —— 那条路径上一次权限判定都没有。
            放在定时触发的场景里尤其危险：任务自己就跑了，没人点过确认。
    """

    def __init__(
        self, *, store: ConditionTaskStore, registry_provider: Callable[[], ToolRegistry],
        event_bus: EventBus | None = None, now: Callable[[], datetime] | None = None,
        poll_seconds: float = 1.0, max_concurrent_checks: int = 2,
        gateway: Any = None,
    ) -> None:
        self.store = store
        self.registry_provider = registry_provider
        self.gateway = gateway
        self.event_bus = event_bus
        self.now = now or _utc_now
        self.store.recover_interrupted_checks(_iso(self.now()))
        self.poll_seconds = poll_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_checks)
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    def gateway_for_actions(self) -> Any:
        """取真正用来执行动作的网关；没注入时就地建一个真网关。

        **为什么不干脆拒绝执行**：没网关时 `self.gateway.invoke(...)` 抛的是
        `AttributeError`，被 `_check_guarded` 吞掉、转成一次重排 ——
        用户看到的症状是"这条提醒从此再也不响"，而系统不报任何错。
        让它在 should-be-impossible 的情况下静默失灵，比自己建一个网关糟得多。

        **为什么不把这当作正常路径**：自建的网关没有审计账本也没有审批回调
        （见 `ActionGateway.standalone`）。生产入口必须注入真网关 ——
        由 `tests/test_execution_paths.py` 里的装配守门锁住。
        所以这里记一条 warning：它出现意味着某条装配路忘了注入，值得查。
        """
        if self.gateway is None:
            logger.warning(
                "condition task service is running without an injected action gateway; "
                "building a standalone one (no journal, no approval sink)"
            )
            self.gateway = ActionGateway.standalone(LateRegistry(self.registry_provider))
        return self.gateway

    async def create_weather_task(
        self, *, scope: Scope, title: str, request_text: str, city: str, condition: str = "weather_clear",
        timezone_name: str = DEFAULT_TIMEZONE, check_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        daily_window: dict[str, Any] | None = None, allowed_tools: list[str] | None = None,
    ) -> ConditionTask:
        if not str(title).strip() or not str(request_text).strip():
            raise ValueError("title and request_text are required")
        if not str(city).strip():
            raise ValueError("city is required")
        if condition != "weather_clear":
            raise ValueError("unsupported condition")
        try:
            ZoneInfo(timezone_name)
        except Exception as exc:
            raise ValueError("invalid timezone") from exc
        if int(check_interval_seconds) < 60:
            raise ValueError("check_interval_seconds must be at least 60")
        window = _normalise_window(daily_window)
        tools = list(allowed_tools or [WEATHER_SEARCH_TOOL])
        if tools != [WEATHER_SEARCH_TOOL]:
            raise ValueError("weather conditions require only the explicitly authorized search__web tool")
        if self.registry_provider().resolve(WEATHER_SEARCH_TOOL) is None:
            raise ValueError("authorized search__web tool is unavailable")
        now = self.now()
        next_at = _iso(now) if _within_window(now, timezone_name, window) else _iso(_next_window_start(now, timezone_name, window))
        stamp = _iso(now)
        return self.store.create(ConditionTask(
            uuid.uuid4().hex, scope, title.strip(), request_text.strip(), city.strip(), condition, timezone_name,
            int(check_interval_seconds), window, tools, "scheduled", next_at, None, None, None, stamp, stamp,
        ))

    async def tick(self) -> int:
        now = self.now()
        due = self.store.due(_iso(now))
        claimed = [task for task in due if self.store.claim(task, _iso(now))]
        await asyncio.gather(*(self._check_guarded(task) for task in claimed), return_exceptions=True)
        return len(claimed)

    async def _check_guarded(self, task: ConditionTask) -> None:
        try:
            await self._check(task)
        except Exception as exc:
            current = self.store.get(task.task_id, task.scope)
            if current is not None and current.status == "checking":
                self.store.reschedule(
                    task,
                    next_check_at=_next_check(self.now(), task.timezone, task.daily_window, task.check_interval_seconds),
                    error=f"{type(exc).__name__}: {exc}",
                )

    async def run_now(self, task_id: str, scope: Scope | AccountTaskScope) -> bool:
        if not self.store.set_due_now(task_id, scope, _iso(self.now())):
            return False
        await self.tick()
        return True

    async def _check(self, task: ConditionTask) -> None:
        async with self._semaphore:
            now = self.now()
            if not _within_window(now, task.timezone, task.daily_window):
                self.store.reschedule(task, next_check_at=_iso(_next_window_start(now, task.timezone, task.daily_window)))
                return
            # 这里只查"这个工具在不在候选表里"（白名单校验），
            # **不再自己 resolve 出 spec 然后自己执行** —— 那是一条没有权限判定的旁路。
            # 执行统一交给网关：它会按顺序做登记、来源限制、参数校验、权限三态、超时与归一化。
            # `allowed_tools` 白名单（"只许 search__web"）保留在这里做纵深防御：
            # 它是"这条任务被创建时授权了哪些工具"的业务约束，网关不知道这层语义。
            if WEATHER_SEARCH_TOOL not in task.allowed_tools:
                self.store.block(task, "authorized search__web tool is not allowed for this task")
                return
            request = ActionRequest(
                action_id=f"ct-{task.task_id}-{uuid.uuid4().hex}",
                name=WEATHER_SEARCH_TOOL,
                arguments={
                    "query": f"{task.city} 今日天气", "max_results": 5, "freshness": "oneDay",
                },
                scope=task.scope,
                origin="scheduler",
                run_id="",
                turn_id="",
                tool_call_id="",
            )
            try:
                observation = await self.gateway_for_actions().invoke(request)
            except Exception as exc:
                # 网关本身炸了（不该发生，但真炸了也不能让这条提醒从此再也不响）：
                # 记成一次失败并改期，由 `reschedule` 保持任务活着。
                logger.exception("condition task action failed (%s)", task.task_id)
                self.store.reschedule(
                    task,
                    next_check_at=_next_check(
                        now, task.timezone, task.daily_window, task.check_interval_seconds,
                    ),
                    error=f"{type(exc).__name__}: {exc}"[:240],
                )
                return
            # 工具被摘掉 / 这条路压根没资格用它 —— 重试多少次都不会长回来。
            # 这种必须停在 `blocked` 上让用户看见；继续 reschedule 的症状是
            # "提醒再也没来，后台每半小时失败一次"，没人会发现。
            if _is_unrecoverable(observation):
                self.store.block(task, "authorized search__web tool is unavailable")
                return
            next_at = _next_check(now, task.timezone, task.daily_window, task.check_interval_seconds)
            if observation.status != "completed":
                # 失败（含 rejected / outcome_unknown）都要落到 `error` 里，
                # 否则这条定时任务会"看起来正常但实际再也不检查"。
                self.store.reschedule(
                    task, next_check_at=next_at,
                    error=(observation.error or observation.status)[:240],
                )
                return
            matched, summary = _weather_verdict(observation.output)
            if not matched:
                self.store.reschedule(task, next_check_at=next_at, summary=summary)
                return
            triggered = _iso(now)
            if self.store.complete(task, summary, triggered) and self.event_bus:
                try:
                    await self.event_bus.emit(Event(
                        EventType.TASK_DUE, now,
                        {"owner_id": task.scope.owner_id, "channel": task.scope.channel,
                         "conversation_id": task.scope.conversation_id, "module_id": task.scope.module_id},
                        {"task_id": task.task_id, "title": task.title, "request_text": task.request_text,
                         "city": task.city, "summary": summary},
                    ))
                except Exception:
                    pass

    async def start(self) -> None:
        if self._worker is None:
            self._closed = False
            self._worker = asyncio.create_task(self._run(), name="condition-task-scheduler")

    async def _run(self) -> None:
        while not self._closed:
            try:
                await self.tick()
            except Exception:
                logger.exception("condition task scheduler tick failed")
            await asyncio.sleep(self.poll_seconds)

    async def stop(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None


def _task_dict(task: ConditionTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "title": task.title,
        "request_text": task.request_text,
        "city": task.city,
        "condition": task.condition,
        "timezone": task.timezone,
        "check_interval_seconds": task.check_interval_seconds,
        "daily_window": task.daily_window,
        "allowed_tools": task.allowed_tools,
        "status": task.status,
        "next_check_at": task.next_check_at,
        "last_summary": task.last_summary,
        "last_error": task.last_error,
        "triggered_at": task.triggered_at,
    }


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def make_condition_task_tools(service: ConditionTaskService) -> list[ToolSpec]:
    def account_scope(scope: Scope) -> AccountTaskScope:
        return AccountTaskScope(scope.owner_id, scope.module_id)

    async def create_weather(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        try:
            task = await service.create_weather_task(
                scope=scope, title=str(arguments.get("title") or ""),
                request_text=str(arguments.get("request_text") or ""), city=str(arguments.get("city") or ""),
                condition=str(arguments.get("condition") or "weather_clear"),
                timezone_name=str(arguments.get("timezone") or DEFAULT_TIMEZONE),
                check_interval_seconds=int(arguments.get("check_interval_seconds") or DEFAULT_INTERVAL_SECONDS),
                daily_window=dict(arguments.get("daily_window") or {}),
                allowed_tools=list(arguments.get("allowed_tools") or [WEATHER_SEARCH_TOOL]),
            )
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "error_kind": classify_tool_error(exc)}
        return {"status": "ok", "output": _task_dict(task)}

    async def list_tasks(_arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        return {"status": "ok", "output": {"tasks": [_task_dict(task) for task in service.store.list(account_scope(scope))]}}

    async def cancel(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        task_id = str(arguments.get("task_id") or "")
        return {"status": "ok", "output": {"cancelled": service.store.cancel(task_id, account_scope(scope)), "task_id": task_id}}

    async def run_now(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        task_id = str(arguments.get("task_id") or "")
        changed = await service.run_now(task_id, account_scope(scope))
        return {
            "status": "ok" if changed else "failed",
            "output": {"queued": changed, "task_id": task_id},
            "error": "task not found or not scheduled" if not changed else "",
        }

    return [
        ToolSpec(
            "create_weather",
            _schema(
                "create_weather",
                "Create a weather-based reminder. Use when user says '晴天提醒我' or '天气好时提醒我'. The task checks weather periodically and triggers when condition is met.",
                {
                    "title": {"type": "string", "description": "Short title, for example '晴天提醒：跑步'."},
                    "request_text": {"type": "string", "description": "What to remind, for example '天气好时提醒我去跑步'."},
                    "city": {"type": "string", "description": "City name in Chinese, for example '北京' or '上海'."},
                    "condition": {"type": "string", "enum": ["weather_clear"], "description": "Weather condition to trigger on. Currently only 'weather_clear' (sunny/clear day) is supported."},
                    "timezone": {"type": "string", "default": DEFAULT_TIMEZONE, "description": "IANA timezone, default 'Asia/Shanghai' for China."},
                    "check_interval_seconds": {"type": "integer", "minimum": 60, "default": DEFAULT_INTERVAL_SECONDS, "description": "How often to check weather in seconds, default 1800 (30 minutes)."},
                    "daily_window": {"type": "object", "properties": {"start": {"type": "string", "description": "Start time like '08:00'"}, "end": {"type": "string", "description": "End time like '18:00'"}}, "description": "Optional time window to check weather, for example {'start': '08:00', 'end': '18:00'} for daytime only."},
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "default": [WEATHER_SEARCH_TOOL], "description": "Tools for checking weather, default includes web search."},
                }, ["title", "request_text", "city"],
            ), create_weather, risk="high", effect=EFFECT_PERSISTENT, confirmation_title="Create weather condition reminder",
        ),
        # 与 tasks.list 同口径的只读快路径：「有哪些条件提醒」不该调模型。
        ToolSpec(
            "list",
            _schema("list", "List condition reminders in the current scope", {}, []),
            list_tasks,
            risk="low",
            effect=EFFECT_READ,
            deterministic_route_allowed=True,
        ),
        ToolSpec("cancel", _schema("cancel", "Cancel a condition reminder", {"task_id": {"type": "string"}}, ["task_id"]), cancel, risk="high", effect=EFFECT_PERSISTENT, confirmation_title="Cancel condition reminder"),
        ToolSpec("run_now", _schema("run_now", "Check a condition reminder immediately", {"task_id": {"type": "string"}}, ["task_id"]), run_now, risk="high", effect=EFFECT_PERSISTENT, confirmation_title="Check condition reminder now"),
    ]
