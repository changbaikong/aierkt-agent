"""Shared code-task lifecycle: snapshot → outcome → verify → patch → approval.

Extracted from ``CodeAgentProvider`` when the DeepSeek Harness provider arrived.
The two providers differ in **who runs the agent loop** (in-process tool loop vs.
an external ACP session); everything after "the agent stopped" is identical and
must stay identical, because that is where the Host's guarantees live:

* the original project is never written to until the Host decides so;
* verification is performed by the Host, never trusted from the provider;
* a protected/high-risk change becomes a checkpoint plus an approval record, and
  approving it *re-resumes* the same snapshot rather than re-running the agent.

Keeping one implementation means a fix to the approval path cannot land in one
provider and be missing in the other. The provider-specific parts are passed in
(none for the Harness provider, which has no host-side sandbox runner).
"""

from __future__ import annotations

import inspect
import json
import os
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .code_task_contracts import CodeAgentCheckpoint, CodeAgentOutcome
from .code_agent_verification import VerificationRecord, snapshot_fingerprint
from .work_provider import WorkArtifact, WorkEvent, WorkRequest, WorkResult
from .workspace_snapshot import SnapshotValidationError, WorkspaceSnapshot


class CodeTaskLifecycle:
    """Snapshot/verification/apply/artifact handling shared by code providers."""

    def __init__(
        self,
        *,
        snapshot_service: Any,
        artifact_dir: str | Path,
        artifact_ttl_hours: int = 24,
        verification_service: Any = None,
        auto_apply_coordinator: Any = None,
        sandbox_runner: Any = None,
    ) -> None:
        self.snapshot_service = snapshot_service
        self.artifact_dir = Path(artifact_dir)
        self.artifact_ttl_hours = max(1, int(artifact_ttl_hours))
        self.verification_service = verification_service
        self.auto_apply_coordinator = auto_apply_coordinator
        self.sandbox_runner = sandbox_runner

    # ------------------------------------------------------------------ #
    # Emit helper
    # ------------------------------------------------------------------ #

    @staticmethod
    async def emit(emit: Any, attempt_id: str, event_type: str, payload: Mapping[str, Any]) -> None:
        value = emit(WorkEvent(attempt_id, event_type, dict(payload)))
        if inspect.isawaitable(value):
            await value

    # ------------------------------------------------------------------ #
    # Workspace
    # ------------------------------------------------------------------ #

    def create_snapshot(self, attempt_id: str, project_root: str | Path) -> WorkspaceSnapshot:
        return self.snapshot_service.create(attempt_id, Path(project_root))

    def load_snapshot(self, attempt_id: str) -> WorkspaceSnapshot:
        return self.snapshot_service.load(attempt_id)

    def prepare(self, attempt_id: str, snapshot: WorkspaceSnapshot) -> None:
        """Prepare host-side execution resources, when the provider has any."""
        if self.sandbox_runner is None:
            return
        self.sandbox_runner.prepare_attempt(attempt_id, snapshot.workspace_root)

    def cleanup(self, attempt_id: str) -> None:
        if self.sandbox_runner is None:
            return
        try:
            self.sandbox_runner.cleanup_attempt(attempt_id)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Outcome → WorkResult
    # ------------------------------------------------------------------ #

    def inspect_project_scope(self, snapshot: WorkspaceSnapshot) -> dict[str, Any]:
        """原项目有没有被快照之外的人动过。

        三种结果，处置不同：

        * ``modified`` —— 有内容改动。任务判失败，并记一条
          ``provider_scope_violation`` 事件（这是**已知**的越界，不是"结果未知"）。
        * ``unverifiable`` —— 扫不动（例如项目里有符号链接或特殊文件）。这时
          仍然让任务走完，但把这件事写进输出：宁可少说一句"没被改过"，
          也不能在没验证的情况下宣称它没被改过。
        * ``intact`` —— 与基线一致。
        """
        try:
            changed = snapshot.project_changes()
        except Exception as exc:
            return {
                "status": "unverifiable",
                "files": [],
                "detail": f"{type(exc).__name__}: {exc}",
            }
        if not changed:
            return {"status": "intact", "files": []}
        return {"status": "modified", "files": list(changed)}

    async def result_from_outcome(
        self,
        request: WorkRequest,
        snapshot: WorkspaceSnapshot,
        outcome: CodeAgentOutcome,
        emit: Any,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> WorkResult:
        if outcome.status == "waiting_approval":
            checkpoint = asdict(outcome.checkpoint) if outcome.checkpoint is not None else {}
            action = dict((outcome.checkpoint.original_action if outcome.checkpoint else {}) or {})
            approval = {
                "approval_id": str(outcome.output.get("approval_id") or action.get("approval_id") or ""),
                "tool": str(action.get("name") or ""),
                "summary": "代码代理请求执行一项需要确认的动作",
            }
            await self.emit(emit, request.attempt_id, "waiting_approval", {"approval": approval})
            return WorkResult(
                "waiting_approval",
                output=approval,
                evidence=list(outcome.evidence),
                checkpoint=checkpoint,
                approval=approval,
            )

        if outcome.status == "cancelled":
            return WorkResult(
                "cancelled",
                output=dict(outcome.output),
                error=outcome.error,
                evidence=list(outcome.evidence),
            )
        if outcome.status in {"failed", "budget_exhausted"}:
            return WorkResult(
                "failed",
                output={**dict(outcome.output), "summary": outcome.summary},
                error=outcome.error or outcome.status,
                evidence=list(outcome.evidence),
            )

        scope = self.inspect_project_scope(snapshot)
        if scope["status"] == "modified":
            # 原项目在任务期间被改动了 —— 那不是 Host 的审批链干的。任务不能再
            # 报成功，也不能在这个基础上继续验证/回写。实测 DeepSeek Harness 会
            # 用任务文本里的绝对路径直接编辑原项目，且不会向我们请求权限。
            await self.emit(emit, request.attempt_id, "provider_scope_violation", scope)
            return WorkResult(
                "failed",
                output={
                    **dict(outcome.output),
                    "summary": outcome.summary,
                    "scope_violation": scope,
                },
                error="原项目在任务快照之外被修改：" + "、".join(scope["files"][:5]),
                evidence=list(outcome.evidence),
            )

        verification, verification_record = await self.verify_snapshot(
            request.attempt_id, snapshot, emit
        )
        apply_result = {"status": "patch_only"}
        if self.auto_apply_coordinator is not None and verification_record is not None:
            apply_result = await self.auto_apply_coordinator.process(
                request.attempt_id,
                snapshot,
                verification_record,
                scope=request.scope,
                cancelled=cancelled or (lambda: False),
            )
        if apply_result.get("status") == "approval_required":
            return await self.apply_approval_result(
                request,
                snapshot,
                outcome,
                verification,
                verification_record,
                apply_result,
                emit,
            )
        return await self.completed_result(
            request, snapshot, outcome, verification, apply_result, emit, scope=scope
        )

    async def completed_result(
        self,
        request: WorkRequest,
        snapshot: WorkspaceSnapshot,
        outcome: CodeAgentOutcome,
        verification: Mapping[str, Any],
        apply_result: Mapping[str, Any],
        emit: Any,
        *,
        scope: Mapping[str, Any] | None = None,
    ) -> WorkResult:
        verification_status = str(verification["status"])
        diff = snapshot.diff()
        output = {
            **dict(outcome.output),
            "summary": outcome.summary,
            "verification_status": verification_status,
            "verification": verification,
            "apply_status": str(apply_result.get("status") or "patch_only"),
            "apply": apply_result,
            # 「原项目没被改过」这句话必须有依据：扫不动就说扫不动。
            "project_scope": dict(scope) if scope is not None else self.inspect_project_scope(snapshot),
            "patch": diff.patch,
            "added": list(diff.added),
            "modified": list(diff.modified),
            "deleted": list(diff.deleted),
            "binary_changes": list(diff.binary_changes),
            "protected_changes": list(diff.protected_changes),
            "total_bytes": diff.total_bytes,
        }
        artifacts = self.write_artifacts(request.attempt_id, output, diff.patch)
        await self.emit(
            emit,
            request.attempt_id,
            "progress",
            {"phase": "patch_ready", "changed": len(diff.added) + len(diff.modified) + len(diff.deleted)},
        )
        return WorkResult(
            "completed",
            output=output,
            evidence=[*outcome.evidence, {"name": "host__verify", **verification}],
            artifacts=artifacts,
        )

    async def apply_approval_result(
        self,
        request: WorkRequest,
        snapshot: WorkspaceSnapshot,
        outcome: CodeAgentOutcome,
        verification: Mapping[str, Any],
        verification_record: VerificationRecord | None,
        apply_result: Mapping[str, Any],
        emit: Any,
    ) -> WorkResult:
        risk = dict(apply_result.get("risk") or {})
        fingerprint = str(risk.get("content_fingerprint") or snapshot_fingerprint(snapshot))
        checkpoint = {
            "kind": "code_apply",
            "snapshot_id": snapshot.snapshot_id,
            "original_attempt": request.attempt_id,
            "content_fingerprint": fingerprint,
            "verification": verification_record.as_dict() if verification_record else dict(verification),
            "outcome": {
                "summary": outcome.summary,
                "output": dict(outcome.output),
                "evidence": list(outcome.evidence),
            },
        }
        approval = {
            "approval_id": uuid.uuid4().hex,
            "tool": "code.apply",
            "summary": "代码代理请求将受保护或高风险修改写回原项目",
            "reasons": list(risk.get("reasons") or []),
            "content_fingerprint": fingerprint,
        }
        if apply_result.get("reason"):
            approval["reason"] = str(apply_result["reason"])
        await self.emit(emit, request.attempt_id, "waiting_approval", {"approval": approval})
        return WorkResult(
            "waiting_approval",
            output=approval,
            evidence=list(outcome.evidence),
            checkpoint=checkpoint,
            approval=approval,
        )

    async def resume_apply_approval(
        self,
        request: WorkRequest,
        checkpoint: Mapping[str, Any],
        decision: str,
        emit: Any,
        *,
        cancelled: Callable[[], bool] | None = None,
        prepare: bool = True,
    ) -> WorkResult:
        """Continue an attempt that was paused for patch approval.

        The snapshot is re-loaded from the ledger, never from the caller, and the
        approval only applies to the exact content fingerprint the user saw. If
        the snapshot moved in the meantime, the change is re-verified and a fresh
        approval is requested instead of applying something unreviewed.
        """
        try:
            original_attempt = str(checkpoint["original_attempt"])
            snapshot = self.load_snapshot(original_attempt)
            if snapshot.snapshot_id != str(checkpoint["snapshot_id"]):
                raise SnapshotValidationError("apply checkpoint snapshot mismatch")
            raw_verification = dict(checkpoint["verification"])
            verification_record = VerificationRecord(
                status=str(raw_verification["status"]),
                reason=str(raw_verification["reason"]),
                project_root=str(raw_verification["project_root"]),
                before_fingerprint=str(raw_verification["before_fingerprint"]),
                after_fingerprint=str(raw_verification["after_fingerprint"]),
                commands=tuple(raw_verification.get("commands") or ()),
            )
            raw_outcome = dict(checkpoint.get("outcome") or {})
            outcome = CodeAgentOutcome(
                "completed",
                summary=str(raw_outcome.get("summary") or ""),
                output=dict(raw_outcome.get("output") or {}),
                evidence=tuple(raw_outcome.get("evidence") or ()),
            )
        except Exception as exc:
            return WorkResult("failed", error=f"invalid apply checkpoint: {type(exc).__name__}: {exc}")

        verification_payload = verification_record.as_dict()
        verification_payload["snapshot_id"] = snapshot.snapshot_id
        if decision != "confirmed":
            return await self.completed_result(
                request,
                snapshot,
                outcome,
                verification_payload,
                {"status": "patch_only", "reason": "approval_rejected"},
                emit,
            )
        expected = str(checkpoint.get("content_fingerprint") or "")
        if snapshot_fingerprint(snapshot) != expected:
            from .workspace_apply import AutoApplyPolicy, prepare_change_set

            if prepare:
                self.prepare(request.attempt_id, snapshot)
            refreshed_payload, refreshed_record = await self.verify_snapshot(
                request.attempt_id, snapshot, emit
            )
            if refreshed_record is None or self.auto_apply_coordinator is None:
                return WorkResult("failed", error="snapshot changed and could not be reverified")
            change_set = prepare_change_set(
                snapshot, refreshed_record,
                getattr(self.auto_apply_coordinator, "policy", AutoApplyPolicy()),
            )
            refreshed_apply = {
                "status": "approval_required",
                "reason": "content_changed_since_approval",
                "risk": {
                    "content_fingerprint": change_set.content_fingerprint,
                    "reasons": list(change_set.reasons),
                    "files": len(change_set.changes),
                    "changed_lines": change_set.total_changed_lines,
                    "content_bytes": change_set.total_content_bytes,
                },
            }
            return await self.apply_approval_result(
                request,
                snapshot,
                outcome,
                refreshed_payload,
                refreshed_record,
                refreshed_apply,
                emit,
            )
        if self.auto_apply_coordinator is None:
            return WorkResult("failed", error="auto apply coordinator is unavailable")
        apply_result = await self.auto_apply_coordinator.process(
            request.attempt_id,
            snapshot,
            verification_record,
            approval_granted=True,
            expected_fingerprint=expected,
            scope=request.scope,
            cancelled=cancelled or (lambda: False),
        )
        if apply_result.get("status") == "approval_required":
            return await self.apply_approval_result(
                request,
                snapshot,
                outcome,
                verification_payload,
                verification_record,
                apply_result,
                emit,
            )
        return await self.completed_result(
            request, snapshot, outcome, verification_payload, apply_result, emit
        )

    # ------------------------------------------------------------------ #
    # Verification and artifacts
    # ------------------------------------------------------------------ #

    async def verify_snapshot(
        self, attempt_id: str, snapshot: WorkspaceSnapshot, emit: Any = None
    ) -> tuple[dict[str, Any], Any]:
        """验证快照结果，三态分开报告：verified / unverified / not_run。

        ``not_run`` 和 ``unverified`` 必须分开：前者是"Host 根本没有验证器"，
        后者是"验证器跑了、没能确认"。混成一个词会让"没配验证链"和
        "验证失败"在页面上看起来一样，排障方向完全不同。

        开始验证之前先发一条明确的阶段事件：聊天侧的"验证改动 / 我在跑测试"
        只允许由**宿主真的启动验证**触发。没有验证配置时一条都不发 ——
        任意执行命令、模型自己声称"我测过了"都不算验证。
        """
        if self.verification_service is None:
            return (
                {
                    "status": "not_run",
                    "reason": "host_verifier_unavailable",
                    "snapshot_id": snapshot.snapshot_id,
                },
                None,
            )
        if emit is not None:
            await self.emit(emit, attempt_id, "progress", {"phase": "verification_started"})
        try:
            record = await self.verification_service.verify(attempt_id, snapshot)
            payload = record.as_dict()
        except Exception as exc:
            return (
                {
                    "status": "unverified",
                    "reason": "host_verification_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "snapshot_id": snapshot.snapshot_id,
                },
                None,
            )
        payload["snapshot_id"] = snapshot.snapshot_id
        # 结束事件带上真实结论：排障与页面能区分"跑过了"和"跑挂了"。
        # "验证改动"那条过程句只认开始事件 —— 跑之前不许说测试通过。
        if emit is not None:
            await self.emit(
                emit,
                attempt_id,
                "progress",
                {
                    "phase": "verification_finished",
                    "verification_status": str(payload.get("status") or ""),
                },
            )
        return payload, record

    def write_artifacts(
        self,
        attempt_id: str,
        output: Mapping[str, Any],
        patch: str,
    ) -> list[WorkArtifact]:
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        expires_at = (
            datetime.now(timezone.utc) + timedelta(hours=self.artifact_ttl_hours)
        ).isoformat(timespec="seconds")
        patch_artifact = self._write_artifact(
            attempt_id,
            ".diff",
            "text/x-diff",
            patch.encode("utf-8"),
            expires_at,
        )
        summary_artifact = self._write_artifact(
            attempt_id,
            ".json",
            "application/json",
            json.dumps(dict(output), ensure_ascii=False, sort_keys=True).encode("utf-8"),
            expires_at,
        )
        return [patch_artifact, summary_artifact]

    def _write_artifact(
        self,
        attempt_id: str,
        suffix: str,
        media_type: str,
        data: bytes,
        expires_at: str,
    ) -> WorkArtifact:
        artifact_id = f"{uuid.uuid4().hex}{suffix}"
        target = self.artifact_dir / artifact_id
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{artifact_id}.", suffix=".tmp", dir=self.artifact_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return WorkArtifact(
            artifact_id=artifact_id,
            attempt_id=attempt_id,
            media_type=media_type,
            size=len(data),
            path=str(target.resolve()),
            expires_at=expires_at,
        )

    @staticmethod
    def restore_checkpoint(checkpoint: Mapping[str, Any]) -> CodeAgentCheckpoint:
        return CodeAgentCheckpoint(
            snapshot_id=str(checkpoint.get("snapshot_id") or ""),
            messages=tuple(dict(item) for item in checkpoint.get("messages") or ()),
            steps_used=int(checkpoint.get("steps_used") or 0),
            tokens_used=int(checkpoint.get("tokens_used") or 0),
            original_action=dict(checkpoint.get("original_action") or {}),
        )
