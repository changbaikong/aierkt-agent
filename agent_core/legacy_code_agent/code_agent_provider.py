"""Work provider that owns code-agent snapshot and loop lifecycles.

The Host-facing behaviour of this provider is unchanged; what changed when the
DeepSeek Harness provider arrived is *where* the post-run logic lives. Snapshot
handling, verification, patch artifact writing and the approval checkpoint now
live in :class:`agent_core.code_task_lifecycle.CodeTaskLifecycle`, shared with
the Harness provider, so the two cannot drift apart in the part that carries the
Host's guarantees.

What stays here is what is specific to the in-process agent loop: creating the
loop through ``loop_factory``, tracking the active loop per attempt for
cancellation, and translating a loop outcome into a Work result through the
shared lifecycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .code_agent_contracts import CodeAgentCheckpoint, CodeAgentOutcome
from ..code_task_lifecycle import CodeTaskLifecycle
from ..work_provider import (
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
)
from ..workspace_snapshot import SnapshotValidationError, WorkspaceSnapshot


class CodeAgentProvider:
    capabilities = WorkProviderCapabilities(
        provider_id="code-agent",
        task_types=("code.agent",),
        supports_cancel=True,
        supports_resume=True,
    )

    def __init__(
        self,
        *,
        snapshot_service: Any,
        loop_factory: Any,
        sandbox_runner: Any,
        artifact_dir: str | Path,
        artifact_ttl_hours: int = 24,
        verification_service: Any = None,
        auto_apply_coordinator: Any = None,
    ) -> None:
        self.snapshot_service = snapshot_service
        self.loop_factory = loop_factory
        self.sandbox_runner = sandbox_runner
        self.artifact_dir = Path(artifact_dir)
        self.artifact_ttl_hours = max(1, int(artifact_ttl_hours))
        self.verification_service = verification_service
        self.auto_apply_coordinator = auto_apply_coordinator
        self.lifecycle = CodeTaskLifecycle(
            snapshot_service=snapshot_service,
            artifact_dir=self.artifact_dir,
            artifact_ttl_hours=self.artifact_ttl_hours,
            verification_service=verification_service,
            auto_apply_coordinator=auto_apply_coordinator,
            sandbox_runner=sandbox_runner,
        )
        self._active_loops: dict[str, Any] = {}
        self._cancelled_attempts: set[str] = set()

    async def _emit(
        self,
        emit: Any,
        attempt_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        await self.lifecycle.emit(emit, attempt_id, event_type, payload)

    async def execute(self, request: WorkRequest, emit: Any) -> WorkResult:
        self._cancelled_attempts.discard(request.attempt_id)
        if request.task_type != "code.agent":
            return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")
        project_root = request.task_input.get("project_root")
        if not isinstance(project_root, str) or not project_root.strip():
            return WorkResult("failed", error="code.agent requires input.project_root")
        try:
            snapshot = self.lifecycle.create_snapshot(request.attempt_id, project_root)
        except Exception as exc:
            return WorkResult("failed", error=f"snapshot creation failed: {type(exc).__name__}: {exc}")

        await self._emit(
            emit,
            request.attempt_id,
            "progress",
            {"phase": "snapshot_ready", "snapshot_id": snapshot.snapshot_id},
        )
        try:
            self.lifecycle.prepare(request.attempt_id, snapshot)
            loop = self.loop_factory(request, snapshot)
            self._active_loops[request.attempt_id] = loop
            outcome = await loop.run(request, snapshot)
            return await self.lifecycle.result_from_outcome(
                request,
                snapshot,
                outcome,
                emit,
                cancelled=lambda: request.attempt_id in self._cancelled_attempts,
            )
        except Exception as exc:
            return WorkResult("failed", error=f"code agent failed: {type(exc).__name__}: {exc}")
        finally:
            self._active_loops.pop(request.attempt_id, None)
            self._cancelled_attempts.discard(request.attempt_id)
            self.lifecycle.cleanup(request.attempt_id)

    async def resume(
        self,
        request: WorkRequest,
        checkpoint: Mapping[str, Any],
        decision: str,
        emit: Any,
    ) -> WorkResult:
        self._cancelled_attempts.discard(request.attempt_id)
        if str(checkpoint.get("kind") or "") == "code_apply":
            try:
                return await self.lifecycle.resume_apply_approval(
                    request,
                    checkpoint,
                    decision,
                    emit,
                    cancelled=lambda: request.attempt_id in self._cancelled_attempts,
                )
            finally:
                self._cancelled_attempts.discard(request.attempt_id)
                self.lifecycle.cleanup(request.attempt_id)
        try:
            restored = self._restore_checkpoint(checkpoint)
            original_attempt = str(restored.original_action.get("turn_id") or "")
            if not original_attempt:
                raise SnapshotValidationError("checkpoint has no original attempt")
            snapshot = self.lifecycle.load_snapshot(original_attempt)
            if snapshot.snapshot_id != restored.snapshot_id:
                raise SnapshotValidationError("checkpoint snapshot id does not match stored snapshot")
        except Exception as exc:
            return WorkResult("failed", error=f"invalid code agent checkpoint: {type(exc).__name__}: {exc}")

        await self._emit(
            emit,
            request.attempt_id,
            "progress",
            {"phase": "approval_resume", "snapshot_id": snapshot.snapshot_id},
        )
        try:
            self.lifecycle.prepare(request.attempt_id, snapshot)
            loop = self.loop_factory(request, snapshot)
            self._active_loops[request.attempt_id] = loop
            outcome = await loop.resume(restored, decision)
            return await self.lifecycle.result_from_outcome(
                request,
                snapshot,
                outcome,
                emit,
                cancelled=lambda: request.attempt_id in self._cancelled_attempts,
            )
        except Exception as exc:
            return WorkResult("failed", error=f"code agent resume failed: {type(exc).__name__}: {exc}")
        finally:
            self._active_loops.pop(request.attempt_id, None)
            self._cancelled_attempts.discard(request.attempt_id)
            self.lifecycle.cleanup(request.attempt_id)

    @staticmethod
    def _restore_checkpoint(checkpoint: Mapping[str, Any]) -> CodeAgentCheckpoint:
        return CodeTaskLifecycle.restore_checkpoint(checkpoint)

    async def cancel(self, attempt_id: str) -> bool:
        self._cancelled_attempts.add(attempt_id)
        loop = self._active_loops.get(attempt_id)
        if loop is not None:
            loop.cancel()
        try:
            sandbox_cancelled = bool(await self.sandbox_runner.cancel(attempt_id))
        except Exception:
            sandbox_cancelled = False
        return loop is not None or sandbox_cancelled

    # ------------------------------------------------------------------ #
    # Delegates kept for callers that reached into the provider
    # ------------------------------------------------------------------ #

    async def _result_from_outcome(
        self,
        request: WorkRequest,
        snapshot: WorkspaceSnapshot,
        outcome: CodeAgentOutcome,
        emit: Any,
    ) -> WorkResult:
        return await self.lifecycle.result_from_outcome(
            request,
            snapshot,
            outcome,
            emit,
            cancelled=lambda: request.attempt_id in self._cancelled_attempts,
        )
