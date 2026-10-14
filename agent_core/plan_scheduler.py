"""Host-owned plan scheduler.

The scheduler is the only component allowed to move a ``ControlPlan`` forward.
It turns persisted ``PlanStep`` rows into ordinary Host ``WorkItem`` rows, so the
Provider keeps executing one ``WorkRequest`` at a time and never learns that a
plan exists.

    ControlPlan -> PlanStep 状态判断 -> WorkItem -> 现有 WorkHost 执行
        -> WorkItem 完成事件 -> 更新 PlanStep -> 解锁后续步骤

Every decision is written to the ledger before it is broadcast, so an SSE
subscriber only ever receives a hint to re-read the ledger.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Mapping

from .contracts import Scope
from .control_plan import (
    MAX_STEP_ATTEMPTS,
    PLAN_TERMINAL_STATUSES,
    ControlPlan,
    PlanStep,
    resolve_references,
    step_outputs,
)
from .work_ledger import WorkLedger

PlanEventSink = Callable[[str, str, Mapping[str, Any], str | None, str | None, str | None], Awaitable[None]]
AttemptSubmitter = Callable[[ControlPlan, PlanStep, Scope, dict[str, Any]], Awaitable[dict[str, Any]]]
WorkItemCanceller = Callable[[str, Scope], Awaitable[bool]]
AttemptRequeuer = Callable[[str], Awaitable[None]]

_PLAN_EVENT_PREFIX = {
    "completed": "plan.completed",
    "failed": "plan.failed",
    "cancelled": "plan.cancelled",
    "interrupted": "plan.interrupted",
}


class PlanScheduler:
    def __init__(
        self,
        ledger: WorkLedger,
        *,
        provider_id: str | None = None,
        provider_id_for: Callable[[str], str] | None = None,
        submit_attempt: AttemptSubmitter,
        cancel_work_item: WorkItemCanceller,
        requeue_attempt: AttemptRequeuer,
        publish: PlanEventSink | None = None,
        max_step_attempts: int = MAX_STEP_ATTEMPTS,
    ) -> None:
        self.ledger = ledger
        # 重试 attempt 的执行者必须按 task_type 解析出**具体** provider，不能把
        # 组合层的名字（composite-worker）写回账本——否则重启恢复和取消都找不
        # 到真正的执行者。``provider_id_for`` 是正路；``provider_id``（静态串）
        # 只留给还传字符串的旧调用方。
        if provider_id_for is None:
            static = str(provider_id or "")
            provider_id_for = lambda task_type: static  # noqa: E731 - 一行兜底，不值得一整个函数
        self.provider_id_for = provider_id_for
        self.max_step_attempts = max(1, int(max_step_attempts))
        self._submit_attempt = submit_attempt
        self._cancel_work_item = cancel_work_item
        self._requeue = requeue_attempt
        self._publish = publish
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Event helpers
    # ------------------------------------------------------------------ #

    async def _emit(
        self,
        plan_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        step_id: str | None = None,
        work_item_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        if self._publish is None:
            return
        await self._publish(plan_id, event_type, dict(payload or {}), step_id, work_item_id, attempt_id)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def submit_plan(self, plan: ControlPlan, steps: list[PlanStep]) -> dict[str, Any]:
        """Entry point used right after the Host persisted a validated plan."""
        await self._emit(plan.plan_id, "plan.created", {"objective": plan.objective, "step_count": len(steps)})
        self.ledger.update_plan_status(plan.plan_id, "running")
        await self._emit(plan.plan_id, "plan.started", {"objective": plan.objective})
        launched = await self.advance(plan.plan_id)
        return {"ready_steps": launched}

    async def advance(self, plan_id: str) -> list[str]:
        """Launch every step whose dependencies are satisfied, then re-derive status."""
        async with self._lock:
            return await self._advance_locked(plan_id)

    async def _advance_locked(self, plan_id: str) -> list[str]:
        plan = self.ledger.get_plan_unscoped(plan_id)
        if plan is None or plan.status in PLAN_TERMINAL_STATUSES:
            return []
        launched: list[str] = []
        await self._unblock_satisfied(plan)
        for step in self.ledger.ready_plan_steps(plan_id):
            launched.append(await self._launch(plan, step))
        await self._recompute_status(plan_id)
        return [step_id for step_id in launched if step_id]

    async def _unblock_satisfied(self, plan: ControlPlan) -> None:
        """A blocked step whose dependencies all completed becomes runnable again."""
        steps = self.ledger.list_plan_steps(plan.plan_id)
        statuses = {step.step_id: step.status for step in steps}
        for step in steps:
            if step.status != "blocked":
                continue
            if all(statuses.get(parent) == "completed" for parent in step.depends_on):
                self.ledger.update_step_status(plan.plan_id, step.step_id, "pending")
                await self._emit(plan.plan_id, "step.ready", {"recovered": True}, step_id=step.step_id)

    async def _launch(self, plan: ControlPlan, step: PlanStep) -> str:
        steps = self.ledger.list_plan_steps(plan.plan_id)
        resolved_input = resolve_references(step.input, step_outputs(steps))
        submitted = await self._submit_attempt(plan, step, plan.scope, resolved_input)
        self.ledger.update_step_status(
            plan.plan_id,
            step.step_id,
            "ready",
            work_item_id=submitted["work_item_id"],
        )
        await self._emit(
            plan.plan_id,
            "step.ready",
            {"task_type": step.task_type, "objective": step.objective, "input": resolved_input},
            step_id=step.step_id,
            work_item_id=submitted["work_item_id"],
            attempt_id=submitted["attempt_id"],
        )
        return step.step_id

    # ------------------------------------------------------------------ #
    # Attempt observation
    # ------------------------------------------------------------------ #

    async def on_step_started(self, work_item_id: str, attempt_id: str) -> None:
        step = self.ledger.get_step_by_work_item(work_item_id)
        if step is None:
            return
        plan = self.ledger.get_plan_unscoped(step.plan_id)
        if plan is None or plan.status in PLAN_TERMINAL_STATUSES:
            return
        self.ledger.update_step_status(step.plan_id, step.step_id, "running")
        await self._emit(
            step.plan_id, "step.started", {}, step_id=step.step_id, work_item_id=work_item_id, attempt_id=attempt_id
        )
        await self._recompute_status(step.plan_id)

    def mark_step_interrupted(self, work_item_id: str, error: str) -> None:
        """Sync bookkeeping for a worker that is being torn down.

        Cancellation unwinds through ``finally`` blocks, so this path must not
        await anything; the ledger is the source of truth and the UI re-reads it.
        """
        step = self.ledger.get_step_by_work_item(work_item_id)
        if step is None or step.status not in {"ready", "running", "waiting_approval"}:
            return
        self.ledger.update_step_status(step.plan_id, step.step_id, "interrupted", error=error)
        plan = self.ledger.get_plan_unscoped(step.plan_id)
        if plan is None or plan.status in PLAN_TERMINAL_STATUSES:
            return
        self.ledger.update_plan_status(step.plan_id, "interrupted")
        self.ledger.append_plan_event(
            step.plan_id,
            "plan.interrupted",
            {"reason": error},
            step_id=step.step_id,
            work_item_id=work_item_id,
        )

    async def on_attempt_finished(
        self,
        work_item_id: str,
        attempt_id: str,
        status: str,
        output: Mapping[str, Any] | None,
        error: str | None,
    ) -> None:
        step = self.ledger.get_step_by_work_item(work_item_id)
        if step is None:
            return
        plan = self.ledger.get_plan_unscoped(step.plan_id)
        if plan is None or plan.status in PLAN_TERMINAL_STATUSES:
            return

        if status == "completed":
            self.ledger.update_step_status(
                step.plan_id, step.step_id, "completed", output=dict(output or {}), error=None
            )
            await self._emit(
                step.plan_id,
                "step.completed",
                {"output": dict(output or {})},
                step_id=step.step_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
            await self.advance(step.plan_id)
            return

        if status == "waiting_approval":
            self.ledger.update_step_status(step.plan_id, step.step_id, "waiting_approval")
            await self._emit(
                step.plan_id,
                "step.waiting_approval",
                {"output": dict(output or {})},
                step_id=step.step_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
            self.ledger.update_plan_status(step.plan_id, "waiting_approval")
            return

        if status == "cancelled":
            self.ledger.update_step_status(
                step.plan_id,
                step.step_id,
                "cancelled",
                error=error or "approval rejected",
            )
            await self._emit(
                step.plan_id,
                "step.cancelled",
                {"reason": error or "approval rejected"},
                step_id=step.step_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
            await self._block_dependents(plan, step, transitive=True)
            await self.advance(step.plan_id)
            return

        if status == "interrupted":
            self.ledger.update_step_status(
                step.plan_id, step.step_id, "interrupted", error=error or "host restarted"
            )
            await self._emit(
                step.plan_id,
                "step.interrupted",
                {"reason": error or "host restarted"},
                step_id=step.step_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
            await self._recompute_status(step.plan_id)
            return

        if status == "unknown_result":
            # 「结果未知」的步骤**绝不自动重跑**，即使它的 failure_policy 是 retry：
            # 上一个 attempt 的副作用可能已经发生了一半，重跑必须是用户的决定。
            # 这条规则与 WorkHost.retry 的门禁是同一个：未知结果只能显式重新执行。
            self.ledger.update_step_status(
                step.plan_id, step.step_id, "failed", output=dict(output or {}), error=error
            )
            await self._emit(
                step.plan_id,
                "step.unknown_result",
                {"error": error, "failure_policy": step.failure_policy, "auto_retry": False},
                step_id=step.step_id,
                work_item_id=work_item_id,
                attempt_id=attempt_id,
            )
            await self._block_dependents(plan, step, transitive=True)
            await self.advance(step.plan_id)
            return

        # status == "failed"
        attempts = len(self.ledger.list_attempts(work_item_id)) if work_item_id else 0
        if step.failure_policy == "retry" and attempts < self.max_step_attempts:
            successor = self.ledger.create_attempt(
                work_item_id, provider_id=self.provider_id_for(step.task_type)
            )
            self.ledger.update_step_status(step.plan_id, step.step_id, "ready", error=error)
            await self._emit(
                step.plan_id,
                "step.retrying",
                {"error": error, "attempts": attempts, "max_attempts": self.max_step_attempts},
                step_id=step.step_id,
                work_item_id=work_item_id,
                attempt_id=successor.attempt_id,
            )
            await self._requeue_attempt(successor.attempt_id)
            return

        self.ledger.update_step_status(
            step.plan_id, step.step_id, "failed", output=dict(output or {}), error=error
        )
        await self._emit(
            step.plan_id,
            "step.failed",
            {"error": error, "failure_policy": step.failure_policy},
            step_id=step.step_id,
            work_item_id=work_item_id,
            attempt_id=attempt_id,
        )
        if step.failure_policy == "skip":
            await self._block_dependents(plan, step, transitive=False, only_referencing=True)
        else:
            await self._block_dependents(plan, step, transitive=True)
        await self.advance(step.plan_id)

    async def _requeue_attempt(self, attempt_id: str) -> None:
        await self._requeue(attempt_id)

    async def _block_dependents(
        self,
        plan: ControlPlan,
        failed: PlanStep,
        *,
        transitive: bool,
        only_referencing: bool = False,
    ) -> None:
        steps = self.ledger.list_plan_steps(plan.plan_id)
        dependencies = {step.step_id: step.depends_on for step in steps}
        if transitive:
            targets = _transitive_dependents(dependencies, failed.step_id)
        else:
            targets = {step.step_id for step in steps if failed.step_id in step.depends_on}
        for step in steps:
            if step.step_id not in targets or step.status not in {"pending", "blocked", "ready"}:
                continue
            if only_referencing and not _references_step(step, failed.step_id):
                continue
            self.ledger.update_step_status(plan.plan_id, step.step_id, "blocked")
            await self._emit(
                plan.plan_id,
                "step.blocked",
                {"blocked_by": failed.step_id, "reason": failed.error or failed.status},
                step_id=step.step_id,
                work_item_id=step.work_item_id,
            )

    # ------------------------------------------------------------------ #
    # Status derivation
    # ------------------------------------------------------------------ #

    async def _recompute_status(self, plan_id: str) -> str | None:
        plan = self.ledger.get_plan_unscoped(plan_id)
        if plan is None or plan.status in PLAN_TERMINAL_STATUSES:
            return None
        steps = self.ledger.list_plan_steps(plan_id)
        statuses = [step.status for step in steps]
        if any(status == "waiting_approval" for status in statuses):
            target = "waiting_approval"
        elif all(status == "completed" for status in statuses):
            target = "completed"
        elif any(status in {"pending", "ready", "running"} for status in statuses):
            target = "running"
        elif any(status == "cancelled" for status in statuses):
            target = "cancelled"
        elif any(status == "failed" for status in statuses):
            target = "failed"
        elif any(status == "interrupted" for status in statuses):
            target = "interrupted"
        else:
            target = "failed"
        if target == plan.status:
            return target
        self.ledger.update_plan_status(plan_id, target)
        event_type = _PLAN_EVENT_PREFIX.get(target)
        if event_type:
            await self._emit(plan_id, event_type, {"status": target})
        return target

    # ------------------------------------------------------------------ #
    # User control
    # ------------------------------------------------------------------ #

    async def cancel_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        async with self._lock:
            plan = self.ledger.get_plan(plan_id, scope)
            if plan is None:
                raise KeyError(plan_id)
            if plan.status in PLAN_TERMINAL_STATUSES:
                return {"status": plan.status, "plan_id": plan_id, "cancelled": False}
            self.ledger.update_plan_status(plan_id, "cancelled")
            for step in self.ledger.list_plan_steps(plan_id):
                if step.status == "completed":
                    # Never undo a finished side effect such as an approved file move.
                    continue
                if step.work_item_id and step.status in {"ready", "running", "waiting_approval"}:
                    await self._cancel_work_item(step.work_item_id, scope)
                if step.status != "cancelled":
                    self.ledger.update_step_status(plan_id, step.step_id, "cancelled", error="plan cancelled")
                    await self._emit(
                        plan_id,
                        "step.cancelled",
                        {"reason": "plan cancelled"},
                        step_id=step.step_id,
                        work_item_id=step.work_item_id,
                    )
            await self._emit(plan_id, "plan.cancelled", {"status": "cancelled"})
            return {"status": "cancelled", "plan_id": plan_id, "cancelled": True}

    async def retry_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        async with self._lock:
            plan = self.ledger.get_plan(plan_id, scope)
            if plan is None:
                raise KeyError(plan_id)
            if plan.status not in PLAN_TERMINAL_STATUSES:
                raise ValueError("only terminal plans can be retried")
            requeued: list[str] = []
            for step in self.ledger.list_plan_steps(plan_id):
                if step.status not in {"failed", "interrupted", "blocked", "cancelled"}:
                    continue
                if step.work_item_id and step.status in {"failed", "interrupted"}:
                    successor = self.ledger.create_attempt(
                        step.work_item_id, provider_id=self.provider_id_for(step.task_type)
                    )
                    self.ledger.update_step_status(plan_id, step.step_id, "ready", error=None)
                    await self._emit(
                        plan_id,
                        "step.retrying",
                        {"reason": "plan retry", "previous_error": step.error},
                        step_id=step.step_id,
                        work_item_id=step.work_item_id,
                        attempt_id=successor.attempt_id,
                    )
                    await self._requeue_attempt(successor.attempt_id)
                else:
                    self.ledger.update_step_status(plan_id, step.step_id, "pending", error=None)
                requeued.append(step.step_id)
            self.ledger.update_plan_status(plan_id, "running")
            await self._emit(plan_id, "plan.started", {"retry": True})
            launched = await self._advance_locked(plan_id)
            return {"status": "running", "plan_id": plan_id, "retried_steps": requeued, "ready_steps": launched}


def _transitive_dependents(dependencies: Mapping[str, tuple[str, ...]], failed: str) -> set[str]:
    targets: set[str] = set()
    frontier = [failed]
    while frontier:
        current = frontier.pop()
        for node, parents in dependencies.items():
            if current in parents and node not in targets:
                targets.add(node)
                frontier.append(node)
    return targets


def _references_step(step: PlanStep, step_id: str) -> bool:
    from .control_plan import iter_references

    return any(referenced == step_id for referenced, _path in iter_references(step.input))
