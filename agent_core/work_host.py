"""Host-owned task submission and background Provider execution."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import Scope
from .control_plan import (
    ControlPlan,
    PlanStep,
    validate_plan_definition,
)
from .control_plan import MANDATORY_APPROVAL_TASK_TYPES as _APPROVAL_REQUIRED_TASK_TYPES
from .plan_scheduler import PlanScheduler
from .permission_mode import LOCAL_OWNER_ID, MODE_FULL_ACCESS
from .provider_activity import (
    PROVIDER_ACTIVITY_EVENT_TYPE,
    TERMINAL_PROVIDER_ACTIVITY_STATUSES,
    ProviderActivity,
)
from .work_ledger import WorkLedger
from .provider_matching import evaluate_provider_match
from .work_provider import WorkEvent, WorkRequest, WorkResult

logger = logging.getLogger(__name__)

#: 这些 task_type 会真的改本机状态（截屏存盘、改整机音量），
#: 只有 Web 端逐项确认过的提交才允许跳过审批。清单定义在 control_plan 里
#: （单一真相），这里只是换个更贴合本文件语境的名字。
_APPROVAL_REQUIRED_TASK_TYPES = frozenset({"screen.capture", "system.volume.adjust"})

#: Host 自有的 context/input 字段。调用方（含模型）给一个都算越界：
#:   - _requires_approval：审批标记，Host 按 task_type 决定，不由调用方开关；
#:   - approval_granted：授权结果，只有 decide_approval 那条路径写得出来；
#:   - approved_moves / authorization / authorized：具体授权凭据；
#:   - approved / web_confirmed：同一件事的各种别名叫法。
#: 必须连**别名**一起挡：漏一个拼写，就留下一条"猜字段名"的绕过通道。
_HOST_OWNED_SUBMIT_KEYS = frozenset({
    "_requires_approval",
    "approval_granted",
    "approved_moves",
    "approved",
    "authorization",
    "authorized",
    "web_confirmed",
})


#: 终态投影最后一步的中文说明（页面只用它当兜底文案）。
_PROVIDER_TERMINAL_STEPS = {
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
    "disconnected": "已断开",
    "unknown_result": "结果未知",
}

#: 启动验收日志的宽限期（秒）。验收要做 live 健康探测，得起子进程；刚启动就
#: 关停的路径（端口冲突、配置崩溃）上，半路的探测只会留下关不掉的子进程和
#: 一串刷屏的收尾噪音。先让服务活过这段时间，再花这个钱。
ACCEPTANCE_LOG_DELAY_SECONDS = 5.0


def _file_move_preview(task: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Latest persisted file-move manifest for a step's work item, if any."""
    if not task:
        return None
    for event in reversed(task.get("events") or []):
        payload = event.get("payload") or {}
        preview = payload.get("preview") or payload.get("output") or {}
        if isinstance(preview, Mapping) and preview.get("moves"):
            return {
                "directory": preview.get("directory"),
                "moves": list(preview.get("moves") or []),
                "total": preview.get("total", len(preview.get("moves") or [])),
                "approval_id": ((payload.get("preview") or {}).get("approval_id") if payload.get("preview") else None),
            }
    return None


def _consume_task_exception(task: "asyncio.Task[None]") -> None:
    """Mark a cancelled background task's exception as observed."""
    if task.cancelled():
        return
    task.exception()


def _health_layer_summary(health: Mapping[str, Any]) -> str:
    """验收日志里的分层状态：安装/探测/握手/可执行/Host 验证链。

    没有分层字段的旧格式健康报告只报 available；本地 worker 没有健康探测，
    返回空串，日志行里就不出现这一段。
    """
    if not health:
        return ""
    if "probe" not in health and "handshake" not in health:
        return f"available={str(bool(health.get('available'))).lower()}"
    install = health.get("failure_stage") != "config" and bool(
        health.get("probe", {}).get("ok") or health.get("available") or health.get("executable")
    )
    probe = bool((health.get("probe") or {}).get("ok"))
    handshake = bool((health.get("handshake") or {}).get("ok"))
    executable = bool(health.get("executable"))
    chain = health.get("host_chain") or {}
    chain_flags = "".join(
        [
            "v" if chain.get("verifier") else "-",
            "a" if chain.get("auto_apply") else "-",
        ]
    )
    return (
        f"install={'ok' if install else 'failed'}"
        f" probe={'ok' if probe else 'failed'}"
        f" handshake={'ok' if handshake else 'failed'}"
        f" executable={'yes' if executable else 'no'}"
        f" chain={chain_flags}"
    )


class WorkHost:
    def __init__(
        self,
        db_path: str | Path,
        *,
        provider: Any,
        max_workers: int = 2,
        code_workers: int = 1,
        permission_lookup: Any = None,
    ) -> None:
        self.ledger = WorkLedger(db_path)
        self.provider = provider
        self.max_workers = max(1, min(2, int(max_workers)))
        self.code_workers = max(1, min(4, int(code_workers)))
        # 闸门二的开关来源。None = 永远标准模式（默认，也是所有老测试的行为）。
        # 不做进程内缓存：每次提交都重新问一次，切换后同账号所有会话立即生效。
        self.permission_lookup = permission_lookup
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._code_queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._code_worker_tasks: list[asyncio.Task[None]] = []
        self._closed = True
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._acceptance_task: asyncio.Task[None] | None = None
        self.plans = PlanScheduler(
            self.ledger,
            provider_id_for=self.resolve_attempt_provider_id,
            submit_attempt=self._submit_plan_step,
            cancel_work_item=self.cancel,
            requeue_attempt=self._enqueue_attempt,
            publish=self._publish_plan_event,
        )

    def _owner_has_full_access(self, scope: Scope) -> bool:
        """这个 owner 当前是不是完全访问。**每次调用都重新查**。

        两条硬条件都必须满足，且都不依赖调用方自报：

        1. owner 是本机账号（``local-user``）。匿名 Scope 无论如何都不放行 ——
           它没有可追责的主体，放行等于把开关交给任何访客。
        2. effective mode 是 full_access。非回环监听时 effective 会被
           Controller 降到 standard，所以这里不需要再判一次监听地址
           （判两次就会有两份真相）。

        任何异常都返回 False：Host 是执行侧，查不出权限就该保守，
        不能因为存储抖动把一次审批变成一次静默直通。
        """
        if self.permission_lookup is None:
            return False
        owner = str(getattr(scope, "owner_id", "") or "").strip()
        if not owner or owner != LOCAL_OWNER_ID or owner.startswith("anonymous:"):
            return False
        try:
            return self.permission_lookup.effective_mode(owner) == MODE_FULL_ACCESS
        except Exception:  # 存储不可用 → 保守回落到标准模式
            return False

    async def start(self) -> None:
        if not self._closed:
            return
        self._closed = False
        pending = self.ledger.recover_runtime()
        # A plan never replays an unknown side effect: it is marked interrupted and
        # waits for an explicit retry from the user.
        for plan_id in self.ledger.recover_plans():
            self._record_plan_event(plan_id, "plan.interrupted", {"reason": "host restarted"})
        self._workers = [asyncio.create_task(self._run_worker(self._queue), name=f"work-provider-{idx}") for idx in range(self.max_workers)]
        self._code_worker_tasks = [
            asyncio.create_task(self._run_worker(self._code_queue), name=f"code-provider-{idx}")
            for idx in range(self.code_workers)
        ]
        for attempt_id in pending:
            await self._enqueue_attempt(attempt_id)
        await self._recover_provider_attempts()
        # 启动验收日志在后台跑：它要做 live 健康探测（起子进程），不能挡住任务恢复，
        # 也不该在启动瞬间就跑 —— 端口冲突这类"刚起来就关停"的路径上，探测会被
        # 取消在半路，子进程的收尾噪音会刷掉启动日志的尾部。先等一段宽限期，
        # 让"服务是否真的活着"这个事实先落定，再做验收。
        if self._acceptance_task is None or self._acceptance_task.done():
            self._acceptance_task = asyncio.create_task(self._log_capability_acceptance())

    async def _recover_provider_attempts(self) -> None:
        """Re-attach to external agent sessions that survived a Host restart.

        The ledger hands back only *interrupted* attempts that still own a live
        session, are not in an unknown-result state, and have no newer sibling —
        see ``WorkLedger.interrupted_provider_attempts`` for why each condition
        matters. The provider is asked to build its own resume checkpoint, so the
        Host never learns what an ACP session is.

        A resume that fails ends the successor attempt as ``unknown_result`` and
        is therefore never retried automatically: the whole point of the
        unknown-result state is that "run it again" must be a human decision.
        """
        try:
            candidates = self.ledger.interrupted_provider_attempts()
        except Exception:  # pragma: no cover - recovery must not block startup
            logger.exception("provider recovery lookup failed")
            return
        for candidate in candidates:
            provider = self.provider_for(candidate["provider_id"])
            if provider is None or not provider.capabilities.supports_resume:
                continue
            builder = getattr(provider, "recovery_checkpoint", None)
            if not callable(builder):
                continue
            try:
                checkpoint = builder(candidate["activity"])
            except Exception:
                logger.exception("provider recovery checkpoint failed attempt=%s", candidate["attempt_id"])
                continue
            if not checkpoint:
                continue
            attempt = self.ledger.create_attempt(
                candidate["work_item_id"], provider_id=candidate["provider_id"]
            )
            self.ledger.save_checkpoint(
                attempt.attempt_id,
                {"provider_checkpoint": dict(checkpoint), "resume_decision": "confirmed"},
            )
            await self._publish(
                WorkEvent(
                    attempt.attempt_id,
                    "recovered",
                    {
                        "reason": "host restarted",
                        "previous_attempt_id": candidate["attempt_id"],
                        "session_id": candidate["session_id"],
                    },
                )
            )
            await self._enqueue_attempt(attempt.attempt_id)
            logger.info(
                "recovering provider session attempt=%s session=%s",
                attempt.attempt_id,
                candidate["session_id"],
            )

    def provider_registry(self) -> list[dict[str, Any]]:
        """Registered providers, for the capabilities endpoint."""
        describe = getattr(self.provider, "describe", None)
        if callable(describe):
            try:
                return list(describe())
            except Exception:  # pragma: no cover - defensive
                pass
        capabilities = getattr(self.provider, "capabilities", None)
        from .work_provider import capabilities_for_task_type

        task_types = tuple(getattr(capabilities, "task_types", ()) or ())
        return [
            {
                "provider_id": str(getattr(capabilities, "provider_id", "")),
                "task_types": list(task_types),
                "supports_cancel": bool(getattr(capabilities, "supports_cancel", False)),
                "supports_resume": bool(getattr(capabilities, "supports_resume", False)),
                "selectable": bool(getattr(capabilities, "selectable", False)),
                "default": True,
                "capabilities_by_task_type": {
                    task_type: capabilities_for_task_type(self.provider, task_type).as_dict()
                    for task_type in task_types
                },
            }
        ]

    async def provider_health(self, provider_id: str) -> dict[str, Any] | None:
        """Health report of one concrete provider (None when it has no probe)."""
        provider = self.provider_for(provider_id)
        if provider is None:
            return None
        health = getattr(provider, "health", None)
        if not callable(health):
            return None
        try:
            result = health()
            if hasattr(result, "__await__"):
                result = await result
            return dict(result or {})
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "provider_id": provider_id,
                "available": False,
                "reason": f"{type(exc).__name__}: {exc}",
            }

    # ------------------------------------------------------------------ #
    # Provider capability matching
    # ------------------------------------------------------------------ #

    def _concrete_providers(self) -> list[Any]:
        """Every registered executor, composite flattened."""
        members = getattr(self.provider, "providers", None)
        if isinstance(members, (list, tuple)):
            return list(members)
        return [self.provider]

    async def match_provider(self, provider_id: str = "", task_type: str = "") -> dict[str, Any]:
        """Why can (or cannot) one provider safely run one task type.

        ``provider_id`` 为空（或组合层自己的 id）时按 task_type 解析默认执行者；
        结论里的 provider_id 始终是**具体执行者**的名字，不是组合层的。
        """
        selector = str(provider_id or "").strip()
        provider = self.provider_for(selector) if selector else None
        if provider is None:
            provider = (
                getattr(self.provider, "provider_for", lambda name: None)(task_type)
                or self.provider
            )
        capabilities = getattr(provider, "capabilities", None)
        concrete_id = str(getattr(capabilities, "provider_id", "") or selector)
        health = await self.provider_health(concrete_id)
        return evaluate_provider_match(
            provider, task_type, provider_id=concrete_id, health=health
        ).as_dict()

    async def capability_report(self) -> list[dict[str, Any]]:
        """Match verdict for every provider × every task type it owns."""
        reports: list[dict[str, Any]] = []
        for provider in self._concrete_providers():
            capabilities = getattr(provider, "capabilities", None)
            provider_id = str(getattr(capabilities, "provider_id", ""))
            health = await self.provider_health(provider_id)
            for task_type in tuple(getattr(capabilities, "task_types", ()) or ()):
                reports.append(
                    evaluate_provider_match(
                        provider, str(task_type), provider_id=provider_id, health=health
                    ).as_dict()
                )
        return reports

    async def _log_capability_acceptance(self) -> None:
        """启动验收：一条日志说明每个 provider 为什么匹配、降级或被拒绝。"""
        await asyncio.sleep(ACCEPTANCE_LOG_DELAY_SECONDS)
        try:
            reports = await self.capability_report()
        except asyncio.CancelledError:
            raise
        except Exception:  # 验收日志坏了不能影响任务执行
            logger.exception("capability acceptance report failed")
            return
        for entry in reports:
            reasons = "; ".join(entry["reasons"]) or "-"
            layers = _health_layer_summary(entry.get("health") or {})
            logger.info(
                "capability acceptance: provider=%s task_type=%s verdict=%s %s reasons=%s",
                entry["provider_id"],
                entry["task_type"],
                entry["verdict"],
                layers,
                reasons,
            )

    async def stop(self) -> None:
        self._closed = True
        if self._acceptance_task is not None and not self._acceptance_task.done():
            # 只取消、不等待：验收日志是旁路，收尾不该被它拖住，更不能在这里
            # 吞掉外层的取消。
            self._acceptance_task.add_done_callback(_consume_task_exception)
            self._acceptance_task.cancel()
        self._acceptance_task = None
        for task in [*self._workers, *self._code_worker_tasks]:
            task.cancel()
        all_workers = [*self._workers, *self._code_worker_tasks]
        if all_workers:
            await asyncio.gather(*all_workers, return_exceptions=True)
        self._workers.clear()
        self._code_worker_tasks.clear()

    async def _enqueue_attempt(self, attempt_id: str) -> None:
        attempt = self.ledger.get_attempt(attempt_id)
        task_type = ""
        if attempt is not None:
            with self.ledger._connect() as db:
                row = db.execute(
                    "SELECT task_type FROM work_items WHERE work_item_id=?",
                    (attempt.work_item_id,),
                ).fetchone()
            task_type = str(row["task_type"] or "") if row is not None else ""
        queue = self._code_queue if task_type == "code.agent" else self._queue
        await queue.put(attempt_id)

    def provider_for(self, provider_id: str) -> Any | None:
        """Concrete provider registered under this id, if any."""
        lookup = getattr(self.provider, "provider_by_id", None)
        if callable(lookup):
            return lookup(provider_id)
        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and str(getattr(capabilities, "provider_id", "")) == str(provider_id):
            return self.provider
        return None

    def _input_provider_selector(self, *sources: Mapping[str, Any]) -> str:
        for source in sources:
            if not isinstance(source, Mapping):
                continue
            value = source.get("provider")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def resolve_attempt_provider_id(
        self,
        task_type: str,
        *,
        selector: str = "",
    ) -> str:
        """Provider id to store on a new attempt.

        没给标识时记录具体默认执行者，使重启、查询和取消仍能找到原执行者。
        给了标识但注册表里没有 → 直接拒绝：静默回落正是"选了 A 却跑在 B"的来源。
        """
        value = str(selector or "").strip()
        if not value:
            resolver = getattr(self.provider, "provider_for", None)
            default = resolver(task_type) if callable(resolver) else self.provider
            capabilities = getattr(default, "capabilities", None)
            return str(getattr(capabilities, "provider_id", ""))
        if self.provider_for(value) is None:
            raise ValueError(f"unknown work provider: {value}")
        return value

    def _request_provider_id(
        self,
        *,
        task_type: str,
        stored_provider_id: str,
        work_input: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> str:
        """Which provider should run this attempt, as a selector for the composite.

        Precedence: the attempt row (that is what actually ran/was queued), then
        the task's own input. Historical composite code tasks without an explicit
        executor are ambiguous, so preserve that id and fail rather than migrate.
        """
        stored = str(stored_provider_id or "").strip()
        # A retired executor must fail explicitly, never reinterpret its old
        # queued task or approval checkpoint as a new Harness session.
        if stored and stored != "composite-worker":
            return stored
        selector = self._input_provider_selector(work_input, context)
        if stored == "composite-worker" and task_type == "code.agent":
            return selector or stored
        return selector

    async def submit(self, scope: Scope, objective: str, *, task_type: str = "file.list", input: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(context or {})
        work_input = dict(input or {})
        # 这些字段是 **Host 自己的**：审批标记、授权凭据、已批准的移动清单。
        # 通用 context/input 里出现它们，就是"调用方想替 Host 决定要不要审批"。
        # 一律拒绝，而不是默默忽略 —— 静默覆盖会让攻击面变成"猜哪个字段有效"。
        for owned in _HOST_OWNED_SUBMIT_KEYS:
            if owned in context:
                raise ValueError(f"{owned} is Host-owned")
            if owned in work_input:
                raise ValueError(f"{owned} is Host-owned")
        # 这里**没有**参数完整性检查，是查过之后才这么定的，别顺手加回来：
        #
        # 1. 进到 provider 的 `task_input` 取的是 `input or context`
        #    （见 tests/test_work_host.py 里那条绕过通道用例的说明）。只判 `input`
        #    会把"目录从 context 带进来"这种**正在被支持**的调用判成缺参数。
        # 2. 审批的判据只有一个：这个 task_type 要不要改本机状态 —— **与参数齐不齐无关**。
        #    `test_chat_volume_submit_without_input_also_waits` 钉的就是这条：
        #    参数没给全的提交照样要走到审批那一步，让用户看见再决定。
        #    在提交口上按参数完整性拒绝，等于让"能不能问用户"取决于参数齐不齐。
        #
        # 真正的参数拒绝在**工具那一层**（`work__submit` 的 `argument_validator`）：
        # 拦在模型动手之前，那时候还没有任何审批被挂起。
        supported_types = getattr(self.provider.capabilities, "task_types", None)
        if supported_types and task_type not in supported_types and getattr(self.provider.capabilities, "provider_id", "") == "local-worker":
            raise ValueError(f"unsupported task_type: {task_type}")
        # 审批的判据只有一个：**这个 task_type 是不是要改本机状态**，
        # 外加"完全访问"这条 Host 自己判出来的豁免。
        #
        # 以前还有第二条：`web_confirmed` —— Web 那一跳自己 confirm 过就带它进来，
        # 于是这条路径能跳过审批。它在 2026-09-22 被删掉了：审批必须落在一条
        # **服务端能再查到的记录**上（`work_approvals`），而不是调用方一句
        # "我已经问过用户了"。调用方自报的授权不是授权。
        #
        # `source` 参数也一并删了：它只在那个判断里用过，而"拿调用方自报的字符串
        # 当闸门"正是当时那条绕过通道的根源（`source!="chat"` 就直接放行）。
        #
        # 标记必须同时写进 input 和 context：WorkRequest.task_input 是
        # `input or context`，只写 context 时，只要调用方给了哪怕一个 input 字段，
        # 标记就被整个盖掉，provider 侧的闸门随之失效（这是一条真实的绕过通道）。
        #
        # 完全访问走同一条 checkpoint 通道（而不是在这里直接跳过审批），
        # 这样 provider 侧 `_requires_approval and not approval_granted` 那道闸门仍然成立。
        full_access = self._owner_has_full_access(scope)
        if task_type in _APPROVAL_REQUIRED_TASK_TYPES and not full_access:
            context["_requires_approval"] = True
            work_input["_requires_approval"] = True
        # Explicit provider selection: validated *before* anything is persisted,
        # so a typo cannot leave a queued task that fails later.
        selected_provider = self.resolve_attempt_provider_id(
            task_type, selector=self._input_provider_selector(work_input, context)
        )
        work = self.ledger.create_work_item(scope=scope, objective=objective, task_type=task_type, input=work_input or None, context=context)
        attempt = self.ledger.create_attempt(work.work_item_id, provider_id=selected_provider or self.provider.capabilities.provider_id)
        if full_access and task_type in _APPROVAL_REQUIRED_TASK_TYPES:
            # 走 decide_approval 用的那条通道：checkpoint 里放 approval_granted，
            # provider 读到就直跑。日志只记工具/任务类型，不记参数。
            self.ledger.save_checkpoint(attempt.attempt_id, {"approval_granted": True})
            logger.info("full access auto-approved work task type=%s", task_type)
        await self._enqueue_attempt(attempt.attempt_id)
        return {"status": "queued", "work_item_id": work.work_item_id, "attempt_id": attempt.attempt_id}

    def get(self, work_item_id: str, scope: Scope) -> dict[str, Any]:
        work = self.ledger.get_work_item(work_item_id, scope)
        if work is None:
            raise KeyError(work_item_id)
        attempts = self.ledger.list_attempts(work_item_id)
        latest = attempts[0] if attempts else None
        all_events = []
        for attempt in attempts:
            all_events.extend(self.ledger.list_events(attempt.attempt_id))
        all_events.sort(key=lambda event: (event.created_at, event.attempt_id, event.sequence))
        artifacts = []
        for attempt in attempts:
            artifacts.extend(self.ledger.list_artifacts(attempt.attempt_id, scope))
        activities = self.ledger.list_provider_activities([attempt.attempt_id for attempt in attempts])
        attempt_payloads = []
        for attempt in attempts:
            payload = attempt.__dict__ if hasattr(attempt, "__dict__") else {field: getattr(attempt, field) for field in attempt.__dataclass_fields__}
            payload["provider_activity"] = activities.get(attempt.attempt_id)
            attempt_payloads.append(payload)
        # Latest *reported* projection, not necessarily the latest attempt: an
        # approval-resume attempt only applies a patch and never talks to the
        # provider, so falling back keeps the page showing the real last state.
        latest_activity = next(
            (activities[attempt.attempt_id] for attempt in attempts if attempt.attempt_id in activities),
            None,
        )
        return {
            "work_item_id": work.work_item_id,
            "objective": work.objective,
            "task_type": work.task_type,
            "input": work.input,
            "context": work.context,
            "status": work.status,
            "created_at": work.created_at,
            "updated_at": work.updated_at,
            "attempts": attempt_payloads,
            "provider_activity": latest_activity,
            "events": [event.__dict__ if hasattr(event, "__dict__") else {field: getattr(event, field) for field in event.__dataclass_fields__} for event in all_events],
            "approvals": [approval.__dict__ if hasattr(approval, "__dict__") else {field: getattr(approval, field) for field in approval.__dataclass_fields__} for approval in self.ledger.list_approvals(work_item_id, scope)],
            "artifacts": [{"artifact_id": item.artifact_id, "attempt_id": item.attempt_id, "media_type": item.media_type, "size": item.size_bytes, "created_at": item.created_at, "expires_at": item.expires_at} for item in artifacts],
        }

    def list(self, scope: Scope, *, limit: int = 50) -> list[dict[str, Any]]:
        return [{"work_item_id": item.work_item_id, "objective": item.objective, "status": item.status, "created_at": item.created_at, "updated_at": item.updated_at} for item in self.ledger.list_work_items(scope, limit=limit)]

    async def cancel(self, work_item_id: str, scope: Scope) -> bool:
        detail = self.get(work_item_id, scope)
        attempt = detail["attempts"][0] if detail["attempts"] else None
        if not attempt or attempt["status"] in {"completed", "failed", "cancelled", "interrupted", "unknown_result"}:
            return False
        cancelled = await self.provider.cancel(attempt["attempt_id"])
        if cancelled:
            self.ledger.finish_attempt(attempt["attempt_id"], status="cancelled", error="cancelled by user")
            await self._publish(WorkEvent(attempt["attempt_id"], "cancelled", {"work_item_id": work_item_id}))
            self._finalize_provider_activity(attempt["attempt_id"], "cancelled", "cancelled by user")
            return True
        # A provider that could not confirm the stop reports it: the attempt may
        # have side effects, so it ends as "unknown result" instead of staying
        # "running" forever (which the UI would show as a live task).
        if self._cancel_was_unconfirmed(attempt["attempt_id"]):
            self.ledger.finish_attempt(
                attempt["attempt_id"],
                status="unknown_result",
                error="cancel not confirmed by provider",
            )
            await self._publish(
                WorkEvent(
                    attempt["attempt_id"],
                    "unknown_result",
                    {"work_item_id": work_item_id, "error": "cancel not confirmed by provider"},
                )
            )
            self._finalize_provider_activity(
                attempt["attempt_id"], "unknown_result", "cancel not confirmed by provider"
            )
            return False
        return False

    def _cancel_was_unconfirmed(self, attempt_id: str) -> bool:
        reader = getattr(self.provider, "cancel_status", None)
        if not callable(reader):
            return False
        try:
            return str(reader(attempt_id) or "") == "unknown"
        except Exception:
            return False

    def get_approval(self, approval_id: str, scope: Scope) -> dict[str, Any] | None:
        """按编号取一条 Work 审批；**不属于这个作用域的一律返回 None**。

        与 `decide_approval` 的区别：那个拿不到就直接抛 KeyError，
        这里是查询口，调用方（审批协调器）要靠"回 None 还是不回 None"
        区分"没有这条"与"不是你的" —— 而这两者对调用方必须是同一个答案。
        """
        approval = self.ledger.get_approval(approval_id)
        if approval is None:
            return None
        attempt = self.ledger.get_attempt(approval.attempt_id)
        if attempt is None:
            return None
        work = self.ledger.get_work_item(attempt.work_item_id, scope)
        if work is None:
            return None
        return {
            "approval_id": approval.approval_id,
            "status": approval.status,
            "payload": dict(approval.payload or {}),
            "created_at": approval.created_at,
            "work_item_id": work.work_item_id,
            "objective": work.objective,
            "task_type": work.task_type,
        }

    def pending_approvals(self, scope: Scope, *, limit: int = 50) -> list[dict[str, Any]]:
        """该作用域下所有还 pending 的 Work 审批。

        实现走"先列该作用域的 work item，再逐个取它的审批" ——
        账本没有一条"按 scope 直接查待审"的 SQL，而在这里现写一条
        等于把账本的表结构复制到 Host 里（那张表的名字只该出现在账本模块）。
        代价是 N 次查询；`limit` 是给这个代价设的上限。
        """
        items: list[dict[str, Any]] = []
        for work in self.ledger.list_work_items(scope, limit=limit):
            for approval in self.ledger.list_approvals(work.work_item_id, scope):
                if approval.status != "pending":
                    continue
                items.append(
                    {
                        "approval_id": approval.approval_id,
                        "status": approval.status,
                        "payload": dict(approval.payload or {}),
                        "created_at": approval.created_at,
                        "work_item_id": work.work_item_id,
                        "objective": work.objective,
                        "task_type": work.task_type,
                    }
                )
        return items

    async def decide_approval(self, approval_id: str, scope: Scope, decision: str) -> dict[str, Any]:
        approval = self.ledger.get_approval(approval_id)
        if approval is None:
            raise KeyError(approval_id)
        attempt = self.ledger.get_attempt(approval.attempt_id)
        if attempt is None:
            raise KeyError(approval.attempt_id)
        work = self.ledger.get_work_item(attempt.work_item_id, scope)
        if work is None:
            raise KeyError(attempt.work_item_id)
        plan_step = self.ledger.get_step_by_work_item(attempt.work_item_id)
        if plan_step is not None:
            plan = self.ledger.get_plan_unscoped(plan_step.plan_id)
            if plan is None or plan.status in {"completed", "failed", "cancelled", "interrupted"}:
                raise ValueError("plan is no longer waiting for approval")
        if not self.ledger.decide_approval(approval_id, decision):
            return {"status": "unchanged", "approval_id": approval_id}
        if decision != "approved" and work.task_type != "code.agent":
            self.ledger.finish_attempt(attempt.attempt_id, status="cancelled", error=f"approval {decision}")
            await self.plans.on_attempt_finished(
                attempt.work_item_id, attempt.attempt_id, "cancelled", {}, f"approval {decision}"
            )
            return {"status": decision, "approval_id": approval_id}
        self.ledger.finish_attempt(attempt.attempt_id, status="cancelled", error="approval accepted; successor attempt created")
        # Approval resumes the same executor; a retired executor must not hand
        # its old checkpoint to the new default provider.
        next_attempt = self.ledger.create_attempt(work.work_item_id, provider_id=self._attempt_provider_id_for(work, predecessor=attempt))
        if work.task_type == "code.agent":
            provider_checkpoint = self.ledger.get_checkpoint(attempt.attempt_id)
            if not provider_checkpoint:
                raise ValueError("code agent approval checkpoint is missing")
            checkpoint = {
                "provider_checkpoint": provider_checkpoint,
                "resume_decision": "confirmed" if decision == "approved" else "rejected",
            }
        else:
            checkpoint = {"approval_manifest": list(approval.payload.get("moves") or [])}
            if not approval.payload.get("moves"):
                checkpoint["approval_granted"] = True
        self.ledger.save_checkpoint(next_attempt.attempt_id, checkpoint)
        await self._enqueue_attempt(next_attempt.attempt_id)
        return {"status": "queued", "approval_id": approval_id, "attempt_id": next_attempt.attempt_id}

    async def retry(self, work_item_id: str, scope: Scope, *, confirm_unknown: bool = False) -> dict[str, Any]:
        """Queue one more attempt for a terminal task.

        ``unknown_result`` needs ``confirm_unknown=True``: the previous attempt
        may have half-applied changes, so re-running it is a user decision, not
        a click-again. Automatic recovery never takes this path either — see
        :meth:`_recover_provider_attempts`.
        """
        work = self.ledger.get_work_item(work_item_id, scope)
        if work is None:
            raise KeyError(work_item_id)
        if work.status not in {"failed", "cancelled", "interrupted", "completed", "unknown_result"}:
            raise ValueError("only terminal work can be retried")
        if work.status == "unknown_result" and not confirm_unknown:
            raise ValueError(
                "result is unknown; retry requires explicit confirmation (confirm_unknown)"
            )
        attempt = self.ledger.create_attempt(work_item_id, provider_id=self._attempt_provider_id_for(work))
        await self._enqueue_attempt(attempt.attempt_id)
        return {"status": "queued", "work_item_id": work_item_id, "attempt_id": attempt.attempt_id}

    def _attempt_provider_id_for(self, work: Any, *, predecessor: Any = None) -> str:
        """Keep the predecessor's executor across retries and approvals."""
        if predecessor is None:
            attempts = self.ledger.list_attempts(work.work_item_id)
            predecessor = attempts[0] if attempts else None
        stored = str(getattr(predecessor, "provider_id", "") or "").strip()
        selected = self._request_provider_id(
            task_type=getattr(work, "task_type", ""), stored_provider_id=stored,
            work_input=dict(getattr(work, "input", {}) or {}),
            context=dict(getattr(work, "context", {}) or {}),
        )
        if selected:
            return selected
        return self.resolve_attempt_provider_id(getattr(work, "task_type", ""))

    async def reject_pending_for_owner(self, owner_id: str) -> dict[str, Any]:
        """把该 owner **全部会话**里挂起的审批一次性拒掉，并终止它们所属的等待中步骤。

        账号级开启完全访问时必须做这件事：旧审批是用户在标准模式下"待处理"的意图，
        换了权限档位就不该继续悬着——不执行、也不保留，只留痕。

        返回 ``{"rejected_approvals": n, "cancelled_plan_steps": m, "failed": [...]}``，
        单个条目失败只记日志、不中断其余清理（否则一个坏审批会挡住整个账号解锁）。
        """
        owner = str(owner_id or "").strip()
        result: dict[str, Any] = {"rejected_approvals": 0, "cancelled_plan_steps": 0, "failed": []}
        if not owner:
            return result

        # ① 先终止等待中的计划步骤，再拒审批 —— 顺序反过来的话，
        #    decide_approval 的处理会把 attempt 收成 cancelled 并把步骤推到终态，
        #    步骤数就统计不出来了。
        try:
            waiting_steps = self.ledger.waiting_plan_steps_for_owner(owner)
            touched_plans: set[str] = set()
            for plan_id, step_id in waiting_steps:
                if self.ledger.terminate_plan_step(plan_id, step_id):
                    result["cancelled_plan_steps"] += 1
                    touched_plans.add(plan_id)
            for plan_id in touched_plans:
                self.ledger.terminal_plan_if_settled(plan_id)
        except Exception:  # pragma: no cover - 清理必须尽力而为
            logger.exception("full access cleanup: terminating waiting plan steps failed owner=%s", owner)
            result["failed"].append("plan_steps")

        # ② 拒掉所有 pending approval，并把 attempt/work_item 收成 cancelled。
        for approval_id in self.ledger.pending_approval_ids_for_owner(owner):
            approval = self.ledger.get_approval(approval_id)
            if approval is None:
                continue
            try:
                if not self.ledger.decide_approval(approval_id, "rejected"):
                    continue
                self.ledger.finish_attempt(
                    approval.attempt_id,
                    status="cancelled",
                    error="rejected: permission mode changed to full access",
                )
                await self.plans.on_attempt_finished(
                    approval.attempt_id, approval.attempt_id, "cancelled", {}, "permission mode changed"
                )
                result["rejected_approvals"] += 1
            except Exception:  # pragma: no cover - 单条失败不阻断
                logger.exception("full access cleanup: rejecting approval failed id=%s", approval_id)
                result["failed"].append(f"approval:{approval_id}")
        return result

    # ------------------------------------------------------------------ #
    # Control plans
    # ------------------------------------------------------------------ #

    async def submit_plan(
        self,
        scope: Scope,
        objective: str,
        steps: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Validate a candidate plan, persist it whole, then start its ready steps."""
        capabilities = getattr(self.provider, "capabilities", None)
        definitions = validate_plan_definition(
            objective,
            steps,
            provider_task_types=getattr(capabilities, "task_types", ()) or (),
            provider_id=getattr(capabilities, "provider_id", ""),
        )
        plan = self.ledger.create_plan(scope=scope, objective=objective, steps=definitions)
        persisted = self.ledger.list_plan_steps(plan.plan_id)
        result = await self.plans.submit_plan(plan, persisted)
        return {
            "status": "queued",
            "plan_id": plan.plan_id,
            "step_count": len(persisted),
            "ready_steps": list(result["ready_steps"]),
            "plan_status": self.ledger.get_plan_unscoped(plan.plan_id).status,
        }

    def _step_payload(self, step: PlanStep) -> dict[str, Any]:
        return {
            "step_id": step.step_id,
            "plan_id": step.plan_id,
            "task_type": step.task_type,
            "objective": step.objective,
            "input": step.input,
            "depends_on": list(step.depends_on),
            "requires_approval": step.requires_approval,
            "failure_policy": step.failure_policy,
            "status": step.status,
            "work_item_id": step.work_item_id,
            "output": step.output,
            "error": step.error,
            "created_at": step.created_at,
            "updated_at": step.updated_at,
        }

    def list_plans(self, scope: Scope, *, limit: int = 50) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for plan in self.ledger.list_plans(scope, limit=limit):
            steps = self.ledger.list_plan_steps(plan.plan_id)
            summaries.append(
                {
                    "plan_id": plan.plan_id,
                    "objective": plan.objective,
                    "status": plan.status,
                    "step_count": len(steps),
                    "completed_steps": sum(1 for step in steps if step.status == "completed"),
                    "waiting_step": next((step.step_id for step in steps if step.status == "waiting_approval"), None),
                    "created_at": plan.created_at,
                    "updated_at": plan.updated_at,
                }
            )
        return summaries

    def get_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        plan = self.ledger.get_plan(plan_id, scope)
        if plan is None:
            raise KeyError(plan_id)
        steps = self.ledger.list_plan_steps(plan_id)
        approvals: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        step_payloads: list[dict[str, Any]] = []
        for step in steps:
            payload = self._step_payload(step)
            task: dict[str, Any] | None = None
            if step.work_item_id:
                try:
                    task = self.get(step.work_item_id, scope)
                except KeyError:
                    task = None
            if task is not None:
                approvals.extend(task.get("approvals") or [])
                artifacts.extend(task.get("artifacts") or [])
            payload["task"] = task
            payload["preview"] = (task or {}).get("events") and _file_move_preview(task)
            step_payloads.append(payload)
        return {
            "plan_id": plan.plan_id,
            "objective": plan.objective,
            "status": plan.status,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
            "step_count": len(steps),
            "steps": step_payloads,
            "approvals": approvals,
            "pending_approvals": [item for item in approvals if item.get("status") == "pending"],
            "artifacts": artifacts,
            "artifact_summaries": [
                {
                    "artifact_id": item["artifact_id"],
                    "attempt_id": item["attempt_id"],
                    "media_type": item.get("media_type"),
                    "size": item.get("size"),
                    "expires_at": item.get("expires_at"),
                }
                for item in artifacts
            ],
            "events": [self._plan_event_payload(event) for event in self.ledger.list_plan_events(plan_id)],
        }

    def plan_events(self, plan_id: str, scope: Scope, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        if self.ledger.get_plan(plan_id, scope) is None:
            raise KeyError(plan_id)
        return [self._plan_event_payload(event) for event in self.ledger.list_plan_events(plan_id, after_sequence=after_sequence)]

    async def cancel_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        return await self.plans.cancel_plan(plan_id, scope)

    async def retry_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        return await self.plans.retry_plan(plan_id, scope)

    async def _submit_plan_step(
        self,
        plan: ControlPlan,
        step: PlanStep,
        scope: Scope,
        resolved_input: dict[str, Any],
    ) -> dict[str, Any]:
        step_input = dict(resolved_input)
        # 不看 step.requires_approval 的取值：它来自候选计划（可能是模型生成的），
        # 计划说"我不需要确认"不算数。Host 自己按 task_type 再判一次。
        full_access = self._owner_has_full_access(scope)
        if (step.requires_approval or step.task_type in _APPROVAL_REQUIRED_TASK_TYPES) and not full_access:
            step_input["_requires_approval"] = True
        work = self.ledger.create_work_item(
            scope=scope,
            objective=step.objective,
            task_type=step.task_type,
            input=step_input,
            context={"plan_id": plan.plan_id, "step_id": step.step_id},
        )
        # 账本上必须留**具体执行者**的名字：恢复、取消和查询都按它找 provider。
        # 写组合层的名字（composite-worker）等于把"谁来跑"这个事实丢掉。
        attempt = self.ledger.create_attempt(
            work.work_item_id, provider_id=self.resolve_attempt_provider_id(step.task_type)
        )
        if full_access and (step.requires_approval or step.task_type in _APPROVAL_REQUIRED_TASK_TYPES):
            self.ledger.save_checkpoint(attempt.attempt_id, {"approval_granted": True})
            logger.info("full access auto-approved plan step task_type=%s", step.task_type)
        await self._enqueue_attempt(attempt.attempt_id)
        return {"work_item_id": work.work_item_id, "attempt_id": attempt.attempt_id}

    @staticmethod
    def _plan_event_payload(event: Any) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "plan_id": event.plan_id,
            "step_id": event.step_id,
            "work_item_id": event.work_item_id,
            "attempt_id": event.attempt_id,
            "sequence": event.sequence,
            "type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
        }

    def _record_plan_event(
        self,
        plan_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        step_id: str | None = None,
        work_item_id: str | None = None,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist first, broadcast second: SSE only ever hints a refresh."""
        record = self.ledger.append_plan_event(
            plan_id, event_type, dict(payload), step_id=step_id, work_item_id=work_item_id, attempt_id=attempt_id
        )
        envelope = self._plan_event_payload(record)
        self._broadcast(envelope)
        return envelope

    async def _publish_plan_event(
        self,
        plan_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        step_id: str | None = None,
        work_item_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        self._record_plan_event(plan_id, event_type, payload, step_id, work_item_id, attempt_id)

    def _broadcast(self, payload: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    async def _publish(self, event: WorkEvent) -> None:
        record = self.ledger.append_event(event.attempt_id, event.event_type, dict(event.payload))
        if event.event_type == "file_moved":
            self.ledger.record_file_operation(event.attempt_id, dict(event.payload))
        if event.event_type == PROVIDER_ACTIVITY_EVENT_TYPE:
            # Persist the projection before broadcasting: the SSE message is only
            # a hint, and a page refreshed between the two must not be able to
            # see a status the ledger never stored.
            self._save_provider_activity(event.attempt_id, event.payload)
        attempt = self.ledger.get_attempt(record.attempt_id)
        payload = {"event_id": record.event_id, "work_item_id": attempt.work_item_id if attempt else event.payload.get("work_item_id"), "attempt_id": record.attempt_id, "sequence": record.sequence, "type": record.event_type, "payload": record.payload, "created_at": record.created_at}
        payload.update(self._attempt_meta(record.attempt_id))
        self._broadcast(payload)

    def _save_provider_activity(self, attempt_id: str, payload: Mapping[str, Any]) -> None:
        try:
            activity = ProviderActivity.from_dict(payload)
            if activity is None:
                return
            self.ledger.save_provider_activity(activity)
        except Exception:  # 观测投影坏了不能挡住任务本身
            logger.exception("provider activity projection failed attempt=%s", attempt_id)

    def _finalize_provider_activity(self, attempt_id: str, status: str, error: str | None) -> None:
        """Close the projection when an attempt reaches a terminal state.

        Providers are supposed to publish a terminal snapshot themselves; this is
        the safety net for the paths where they cannot (host stop, unexpected
        exception, plan cancellation). Without it the page would keep showing
        "执行工具" for a task that is already finished.
        """
        stored = self.ledger.get_provider_activity(attempt_id)
        if stored is None:
            return
        activity = ProviderActivity.from_dict(stored)
        if activity is None or activity.status in TERMINAL_PROVIDER_ACTIVITY_STATUSES:
            return
        mapped = {
            "completed": "completed",
            "cancelled": "cancelled",
            "failed": "failed",
            "unknown_result": "unknown_result",
            "interrupted": "disconnected",
        }.get(status, "unknown_result")
        confidence = "confirmed" if mapped == "completed" else "unknown"
        updated = activity.with_changes(
            status=mapped,
            error=error or activity.error,
            confidence=confidence,
            finished_at=activity.finished_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            step=activity.step or _PROVIDER_TERMINAL_STEPS.get(mapped, ""),
        )
        self._save_provider_activity(attempt_id, updated.as_dict())

    def _attempt_meta(self, attempt_id: str) -> dict[str, Any]:
        """一次 join 查到 attempt 所属 work item 的 scope / objective / task_type。

        广播给 SSE 与 WorkResultBridge 用：桥要把结果写回**发起对话**，
        只靠 work scope（module_id="work"）找不到对话，必须带完整的四元组。
        """
        try:
            with self.ledger._connect() as db:
                row = db.execute(
                    """SELECT w.owner_id, w.channel, w.conversation_id, w.module_id,
                              w.objective, w.task_type
                       FROM work_items w JOIN work_attempts t ON t.work_item_id = w.work_item_id
                       WHERE t.attempt_id=?""",
                    (attempt_id,),
                ).fetchone()
        except Exception:  # 观测元数据是旁路：查不到就少带几个字段，不挡广播
            return {}
        if row is None:
            return {}
        return {key: row[key] for key in row.keys()}

    async def _run_worker(self, queue: asyncio.Queue[str]) -> None:
        while not self._closed:
            attempt_id = await queue.get()
            claimed = self.ledger.claim_attempt(attempt_id)
            if claimed is None:
                continue
            with self.ledger._connect() as db:
                row = db.execute("SELECT * FROM work_items WHERE work_item_id=?", (claimed.work_item_id,)).fetchone()
            if row is None:
                self.ledger.finish_attempt(attempt_id, status="failed", error="work item missing")
                continue
            scope = Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
            checkpoint = self.ledger.get_checkpoint(attempt_id) or {}
            work_context = json.loads(row["context_json"])
            work_input = json.loads(row["input_json"] or "{}")
            request = WorkRequest(claimed.work_item_id, attempt_id, scope, row["objective"], context=work_context, task_type=row["task_type"] or "file.list", input=work_input, approval_manifest=tuple(checkpoint.get("approval_manifest") or ()), approval_granted=bool(checkpoint.get("approval_granted")), provider_id=self._request_provider_id(task_type=row["task_type"] or "file.list", stored_provider_id=claimed.provider_id, work_input=work_input, context=work_context))
            await self.plans.on_step_started(claimed.work_item_id, attempt_id)
            await self._publish(WorkEvent(attempt_id, "started", {"work_item_id": claimed.work_item_id, "provider": request.objective}))
            try:
                if checkpoint.get("provider_checkpoint"):
                    result = await self.provider.resume(
                        request,
                        checkpoint["provider_checkpoint"],
                        str(checkpoint.get("resume_decision") or "rejected"),
                        self._publish,
                    )
                else:
                    result = await self.provider.execute(request, self._publish)
                output = dict(result.output)
                if result.status == "waiting_approval":
                    if result.checkpoint:
                        self.ledger.pause_for_approval(
                            attempt_id,
                            result.checkpoint,
                            result.approval or output,
                        )
                    else:
                        self.ledger.create_approval(attempt_id, output)
                else:
                    for artifact in result.artifacts:
                        path = Path(artifact.path)
                        if path.is_file():
                            record = self.ledger.record_artifact(attempt_id, media_type=artifact.media_type, path=str(path.resolve()), size_bytes=artifact.size, expires_at=artifact.expires_at or (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds"))
                            output.setdefault("artifacts", []).append({"artifact_id": record.artifact_id, "media_type": record.media_type, "size": record.size_bytes, "expires_at": record.expires_at})
                    self.ledger.finish_attempt(attempt_id, status=result.status, output=output, error=result.error)
                # Close the provider projection for every terminal status. A
                # provider normally does this itself; the ones that cannot (host
                # stop, crash before publishing, plan cancellation) must still
                # leave the page showing a finished state.
                if result.status != "waiting_approval":
                    self._finalize_provider_activity(attempt_id, result.status, result.error)
                await self._publish(WorkEvent(attempt_id, result.status, {"output": output, "error": result.error}))
                await self.plans.on_attempt_finished(
                    claimed.work_item_id, attempt_id, result.status, output, result.error
                )
            except asyncio.CancelledError:
                self.ledger.finish_attempt(attempt_id, status="interrupted", error="host stopped")
                self._finalize_provider_activity(attempt_id, "interrupted", "host stopped")
                self.plans.mark_step_interrupted(claimed.work_item_id, "host stopped")
                raise
            except Exception as exc:
                self.ledger.finish_attempt(attempt_id, status="failed", error=f"{type(exc).__name__}: {exc}")
                self._finalize_provider_activity(attempt_id, "failed", f"{type(exc).__name__}: {exc}")
                await self._publish(WorkEvent(attempt_id, "failed", {"error": str(exc)}))
                await self.plans.on_attempt_finished(
                    claimed.work_item_id, attempt_id, "failed", {}, f"{type(exc).__name__}: {exc}"
                )
