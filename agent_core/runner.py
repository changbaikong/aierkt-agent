"""Single ingress for durable high-risk confirmation recovery."""

from __future__ import annotations

from dataclasses import dataclass
import asyncio
import inspect
from typing import Any, Awaitable, Callable

from .contracts import AccountTaskScope, AgentOutcome, AgentRequest, ConfirmationRequest, Scope


@dataclass(frozen=True)
class RecoveryNotice:
    """A safe, channel-neutral description of one action requiring a decision."""

    action_id: str
    title: str
    tool: str
    message: str


RecoveryFormatter = Callable[[ConfirmationRequest], str]
AsyncRecoveryFormatter = Callable[[ConfirmationRequest], str | Awaitable[str]]

RECOVERY_TEMPLATES: tuple[str, ...] = (
    "检测到未完成的高风险操作：{title}（工具：{tool}）。该操作尚未执行，请回复“确认”继续，或回复“取消”放弃。",
    "发现一项待处理的高风险操作：{title}（工具：{tool}）。目前尚未执行，请回复“确定”继续，或回复“重置待确认”清理。",
    "上次留下的高风险操作是：{title}（工具：{tool}）。它尚未执行，请回复“同意”执行，或回复“取消”放弃。",
    "恢复到一项未完成的高风险操作：{title}（工具：{tool}）。尚未执行任何副作用，请回复“确认”继续，或回复“取消”放弃。",
    "系统发现待确认动作：{title}（工具：{tool}）。该动作尚未执行，请回复“确定”继续，或回复“重置待确认”清理。",
    "有一项高风险操作等待你的决定：{title}（工具：{tool}）。尚未执行，请回复“同意”执行，或回复“取消”放弃。",
    "检测到之前暂停的操作：{title}（工具：{tool}）。操作尚未执行，请回复“确认”继续，或回复“重置待确认”清理。",
    "发现未完成的高风险请求：{title}（工具：{tool}）。尚未执行，请回复“确定”继续，或回复“取消”放弃。",
    "待恢复的高风险动作：{title}（工具：{tool}）。它尚未执行，请回复“同意”执行，或回复“重置待确认”清理。",
    "系统保留了一项高风险确认：{title}（工具：{tool}）。尚未执行，请回复“确认”继续，或回复“取消”放弃。",
    "你有一项暂停中的高风险操作：{title}（工具：{tool}）。该操作尚未执行，请回复“确定”继续，或回复“重置待确认”清理。",
    "恢复提示：高风险操作{title}（工具：{tool}）仍待确认，尚未执行，请回复“同意”执行，或回复“取消”放弃。",
    "之前的高风险动作尚未完成：{title}（工具：{tool}）。它尚未执行，请回复“确认”继续，或回复“重置待确认”清理。",
    "检测到需要明确决定的操作：{title}（工具：{tool}）。尚未执行，请回复“确定”继续，或回复“取消”放弃。",
    "发现一条遗留高风险确认：{title}（工具：{tool}）。该操作尚未执行，请回复“同意”执行，或回复“重置待确认”清理。",
    "高风险操作{title}（工具：{tool}）正在等待确认，尚未执行，请回复“确认”继续，或回复“取消”放弃。",
    "恢复时发现待处理动作：{title}（工具：{tool}）。目前尚未执行，请回复“确定”继续，或回复“重置待确认”清理。",
    "这项高风险操作还没有完成：{title}（工具：{tool}）。尚未执行，请回复“同意”执行，或回复“取消”放弃。",
    "系统发现暂停的高风险请求：{title}（工具：{tool}）。该请求尚未执行，请回复“确认”继续，或回复“重置待确认”清理。",
    "待你确认的高风险操作为：{title}（工具：{tool}）。尚未执行，请回复“确定”继续，或回复“取消”放弃。",
    "上次中断的高风险动作已恢复记录：{title}（工具：{tool}）。尚未执行，请回复“同意”执行，或回复“重置待确认”清理。",
    "有一项高风险动作需要恢复处理：{title}（工具：{tool}）。它尚未执行，请回复“确认”继续，或回复“取消”放弃。",
    "未完成的高风险操作已找到：{title}（工具：{tool}）。目前尚未执行，请回复“确定”继续，或回复“重置待确认”清理。",
    "检测到一项等待授权的动作：{title}（工具：{tool}）。尚未执行，请回复“同意”执行，或回复“取消”放弃。",
    "高风险操作{title}（工具：{tool}）尚未获得最终确认，也尚未执行，请回复“确认”继续，或回复“重置待确认”清理。",
    "恢复队列中有一项高风险操作：{title}（工具：{tool}）。尚未执行，请回复“确定”继续，或回复“取消”放弃。",
    "系统保留了未完成的高风险动作{title}（工具：{tool}）。该动作尚未执行，请回复“同意”执行，或回复“重置待确认”清理。",
    "请处理一项暂停的高风险操作：{title}（工具：{tool}）。尚未执行，请回复“确认”继续，或回复“取消”放弃。",
    "检测到需要恢复的动作{title}（工具：{tool}）。它尚未执行，请回复“确定”继续，或回复“重置待确认”清理。",
    "高风险确认仍在等待：{title}（工具：{tool}）。目前尚未执行，请回复“同意”执行，或回复“取消”放弃。",
)


class AgentRunner:
    """Coordinate ingress and recovery without owning model or tool execution.

    ConversationManager remains the only turn scheduler and AgentLoop remains
    the only component that executes an approved tool call. This wrapper gives
    every channel one recovery and confirmation boundary.
    """

    def __init__(
        self,
        manager: Any,
        confirmation_store: Any,
        *,
        outbox: Any = None,
        delivery_target: str = "default",
        recovery_formatter: RecoveryFormatter | None = None,
    ) -> None:
        self.manager = manager
        self.confirmation_store = confirmation_store
        self.outbox = outbox
        self.delivery_target = delivery_target
        self.recovery_formatter = recovery_formatter

    async def submit(self, request: AgentRequest) -> Any:
        if self.manager is None:
            raise RuntimeError("AgentRunner was not initialized with a conversation manager")
        return await self.manager.submit(request)

    async def resume(self, action_id: str, scope: Scope, decision: str) -> AgentOutcome:
        if self.manager is None:
            raise RuntimeError("AgentRunner was not initialized with a conversation manager")
        return await self.manager.resume(action_id, scope, decision)

    async def run_internal(self, session: Any, request: AgentRequest) -> AgentOutcome:
        """Use the common invocation surface for non-channel hosts.

        Internal workers keep their own permission policy; this method merely
        prevents them from inventing a second execution protocol.
        """
        return await session.run(request)

    async def resume_internal(self, session: Any, action_id: str, decision: str) -> AgentOutcome:
        resumable = getattr(session, "resume", None)
        if not callable(resumable):
            raise RuntimeError("internal session does not support confirmation recovery")
        return await resumable(action_id, decision)

    def pending(self, scope: Scope) -> list[ConfirmationRequest]:
        account_scope = AccountTaskScope(scope.owner_id, scope.module_id)
        pending_for_account = getattr(self.confirmation_store, "pending_for_account", None)
        if callable(pending_for_account):
            return list(pending_for_account(account_scope))
        return list(self.confirmation_store.pending_for_scope(scope))

    @staticmethod
    def _render_template(request: ConfirmationRequest, index: int) -> str:
        return RECOVERY_TEMPLATES[index % len(RECOVERY_TEMPLATES)].format(title=request.title, tool=request.tool)

    def _next_template(self, request: ConfirmationRequest) -> tuple[int, str]:
        index = self.confirmation_store.next_recovery_template_index(
            AccountTaskScope(request.owner_id, request.module_id), len(RECOVERY_TEMPLATES)
        )
        return index, self._render_template(request, index)

    @staticmethod
    def _safe_message(message: str) -> bool:
        return (
            "尚未执行" in message
            and any(word in message for word in ("确认", "确定", "同意"))
            and any(word in message for word in ("取消", "重置待确认"))
        )

    def recovery_message(self, request: ConfirmationRequest) -> str:
        if self.recovery_formatter is not None:
            try:
                rendered = str(self.recovery_formatter(request)).strip()
                if rendered:
                    return rendered
            except Exception:
                pass
        return self._render_template(request, 0)

    def recover(self, scope: Scope, *, delivery_target: str | None = None) -> list[RecoveryNotice]:
        """Restore durable locks and emit recovery notices without executing actions."""
        recover_locks = getattr(self.confirmation_store, "recover_locks", None)
        if callable(recover_locks):
            recover_locks()
        notices: list[RecoveryNotice] = []
        target = delivery_target or self.delivery_target
        for request in self.pending(scope):
            _, base_template = self._next_template(request)
            notice = RecoveryNotice(
                request.action_id,
                request.title,
                request.tool,
                base_template,
            )
            notices.append(notice)
            if self.outbox is not None:
                self.outbox.enqueue(
                    account_id=request.owner_id,
                    delivery_target=target,
                    event_type="confirmation.recovery_needed",
                    payload={"action_id": request.action_id, "title": request.title, "tool": request.tool},
                    dedupe_key=f"confirmation-recovery:{request.action_id}",
                )
        return notices

    async def recover_with_language(
        self,
        scope: Scope,
        formatter: AsyncRecoveryFormatter,
        *,
        delivery_target: str | None = None,
        formatter_timeout_seconds: float = 8.0,
    ) -> list[RecoveryNotice]:
        """Recover locks and use language generation only for safe phrasing.

        The callback has no tool access and receives only a confirmation
        record. A formatting failure deliberately falls back to deterministic
        wording; authorization remains entirely in ``resume``.
        """
        recover_locks = getattr(self.confirmation_store, "recover_locks", None)
        if callable(recover_locks):
            recover_locks()
        notices: list[RecoveryNotice] = []
        target = delivery_target or self.delivery_target
        for request in self.pending(scope):
            _, base_template = self._next_template(request)
            try:
                try:
                    message = formatter(request, base_template)
                except TypeError:
                    message = formatter(request)
                if inspect.isawaitable(message):
                    message = await asyncio.wait_for(message, timeout=max(0.001, float(formatter_timeout_seconds)))
                rendered = str(message).strip()
            except Exception:
                rendered = ""
            notice = RecoveryNotice(
                request.action_id,
                request.title,
                request.tool,
                rendered if self._safe_message(rendered) else base_template,
            )
            notices.append(notice)
            if self.outbox is not None:
                self.outbox.enqueue(
                    account_id=request.owner_id,
                    delivery_target=target,
                    event_type="confirmation.recovery_needed",
                    payload={"action_id": request.action_id, "title": request.title, "tool": request.tool},
                    dedupe_key=f"confirmation-recovery:{request.action_id}",
                )
        return notices
