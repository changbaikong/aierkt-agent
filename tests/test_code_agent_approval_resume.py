from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from agent_core.legacy_code_agent.code_agent_contracts import CodeAgentCheckpoint, CodeAgentOutcome
from agent_core.legacy_code_agent.code_agent_provider import CodeAgentProvider
from agent_core.code_agent_verification import VerificationRecord
from agent_core.contracts import Scope
from agent_core.work_provider import WorkRequest
from agent_core.workspace_snapshot import WorkspaceSnapshotService


class Sandbox:
    def prepare_attempt(self, attempt_id, root):
        pass

    async def cancel(self, attempt_id):
        return True

    def cleanup_attempt(self, attempt_id):
        pass


class PausingLoop:
    def __init__(self, outcome):
        self.outcome = outcome
        self.resume_calls = []

    async def run(self, request, snapshot):
        return self.outcome

    async def resume(self, checkpoint, decision):
        self.resume_calls.append((checkpoint, decision))
        return CodeAgentOutcome("completed", summary="continued")

    def cancel(self):
        pass


class ApplyVerifier:
    async def verify(self, attempt_id, snapshot):
        from agent_core.code_agent_verification import snapshot_fingerprint

        fingerprint = snapshot_fingerprint(snapshot)
        return VerificationRecord(
            "verified", "verified", str(snapshot.project_root), fingerprint, fingerprint
        )


class ApprovalApplyCoordinator:
    def __init__(self):
        self.calls = []

    async def process(self, attempt_id, snapshot, verification, **kwargs):
        self.calls.append((attempt_id, kwargs))
        if not kwargs.get("approval_granted"):
            return {
                "status": "approval_required",
                "risk": {
                    "reasons": ["protected_file"],
                    "content_fingerprint": verification.after_fingerprint,
                },
            }
        return {"status": "applied_verified", "risk": {"reasons": []}}


def request(project: Path, attempt_id: str):
    return WorkRequest(
        work_item_id="work-1",
        attempt_id=attempt_id,
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat"),
        objective="run task",
        task_type="code.agent",
        input={"project_root": str(project)},
    )


@pytest.mark.asyncio
async def test_provider_returns_durable_checkpoint_and_resumes_original_snapshot(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "a.py").write_text("x = 1", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    checkpoint = CodeAgentCheckpoint(
        snapshot_id="placeholder",
        messages=({"role": "user", "content": "run"},),
        steps_used=1,
        tokens_used=5,
        original_action={
            "approval_id": "approval-1",
            "action_id": "action-1",
            "call_id": "call-1",
            "name": "code__run",
            "arguments": {"runtime": "python", "args": ["a.py"]},
            "scope": {
                "owner_id": "alice",
                "channel": "web",
                "conversation_id": "chat",
                "module_id": "companion",
            },
            "turn_id": "attempt-original",
        },
    )
    pausing = PausingLoop(
        CodeAgentOutcome(
            "waiting_approval",
            checkpoint=checkpoint,
            output={"approval_id": "approval-1"},
        )
    )

    def factory(work_request, snapshot):
        if pausing.outcome.checkpoint and pausing.outcome.checkpoint.snapshot_id == "placeholder":
            object.__setattr__(pausing.outcome.checkpoint, "snapshot_id", snapshot.snapshot_id)
        return pausing

    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=factory,
        sandbox_runner=Sandbox(),
        artifact_dir=tmp_path / "artifacts",
    )
    paused = await provider.execute(request(project, "attempt-original"), lambda event: None)
    assert paused.status == "waiting_approval"
    assert paused.checkpoint["original_action"]["arguments"] == {
        "runtime": "python",
        "args": ["a.py"],
    }

    result = await provider.resume(
        request(project, "attempt-resume"),
        paused.checkpoint,
        "confirmed",
        lambda event: None,
    )
    assert result.status == "completed"
    assert pausing.resume_calls[0][1] == "confirmed"
    assert pausing.resume_calls[0][0].original_action["arguments"]["args"] == ["a.py"]


@pytest.mark.asyncio
async def test_provider_rejects_checkpoint_with_wrong_snapshot_id(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "a.py").write_text("x = 1", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = snapshots.create("attempt-original", project)
    loop = PausingLoop(CodeAgentOutcome("completed"))
    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=lambda request, snapshot: loop,
        sandbox_runner=Sandbox(),
        artifact_dir=tmp_path / "artifacts",
    )
    checkpoint = asdict(
        CodeAgentCheckpoint(
            snapshot_id="wrong",
            original_action={
                "approval_id": "approval-1",
                "action_id": "action-1",
                "call_id": "call-1",
                "name": "code__run",
                "arguments": {},
                "scope": {
                    "owner_id": "alice",
                    "channel": "web",
                    "conversation_id": "chat",
                    "module_id": "companion",
                },
                "turn_id": "attempt-original",
            },
        )
    )

    result = await provider.resume(
        request(project, "attempt-resume"), checkpoint, "confirmed", lambda event: None
    )
    assert result.status == "failed"
    assert "snapshot" in (result.error or "").lower()
    assert loop.resume_calls == []


@pytest.mark.asyncio
async def test_apply_approval_binds_fingerprint_and_resumes_without_model_tool_call(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "requirements.txt").write_text("old==1", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    loop = PausingLoop(CodeAgentOutcome("completed", summary="update dependency"))
    coordinator = ApprovalApplyCoordinator()
    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=lambda request, snapshot: loop,
        sandbox_runner=Sandbox(),
        artifact_dir=tmp_path / "artifacts",
        verification_service=ApplyVerifier(),
        auto_apply_coordinator=coordinator,
    )

    original_run = loop.run

    async def modifying_run(request, snapshot):
        (snapshot.workspace_root / "requirements.txt").write_text("new==2", encoding="utf-8")
        return await original_run(request, snapshot)

    loop.run = modifying_run
    paused = await provider.execute(request(project, "apply-original"), lambda event: None)
    resumed = await provider.resume(
        request(project, "apply-resume"), paused.checkpoint, "confirmed", lambda event: None
    )

    assert paused.status == "waiting_approval"
    assert paused.checkpoint["kind"] == "code_apply"
    assert paused.approval["reasons"] == ["protected_file"]
    assert resumed.status == "completed"
    assert resumed.output["apply_status"] == "applied_verified"
    assert coordinator.calls[-1][1]["approval_granted"] is True


@pytest.mark.asyncio
async def test_apply_approval_requires_new_decision_when_snapshot_changes(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "requirements.txt").write_text("old==1", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    loop = PausingLoop(CodeAgentOutcome("completed"))
    coordinator = ApprovalApplyCoordinator()
    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=lambda request, snapshot: loop,
        sandbox_runner=Sandbox(),
        artifact_dir=tmp_path / "artifacts",
        verification_service=ApplyVerifier(),
        auto_apply_coordinator=coordinator,
    )

    async def modifying_run(request, snapshot):
        (snapshot.workspace_root / "requirements.txt").write_text("new==2", encoding="utf-8")
        return CodeAgentOutcome("completed")

    loop.run = modifying_run
    paused = await provider.execute(request(project, "changed-original"), lambda event: None)
    stored = snapshots.load("changed-original")
    (stored.workspace_root / "requirements.txt").write_text("changed again", encoding="utf-8")

    resumed = await provider.resume(
        request(project, "changed-resume"), paused.checkpoint, "confirmed", lambda event: None
    )

    assert resumed.status == "waiting_approval"
    assert resumed.output["reason"] == "content_changed_since_approval"


@pytest.mark.asyncio
async def test_changed_approval_never_auto_applies_new_low_risk_content(tmp_path: Path) -> None:
    from agent_core.workspace_apply import AutoApplyPolicy, CodeAgentAutoApplyCoordinator, WorkspaceApplyService

    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True, snapshot_service=snapshots, sandbox_runner=Sandbox(),
        verification_service=ApplyVerifier(), apply_service=WorkspaceApplyService(tmp_path / "apply"),
        policy=AutoApplyPolicy(),
    )
    snapshot = snapshots.create("changed-original", project)
    (snapshot.workspace_root / "app.py").write_text("value = 2\n", encoding="utf-8")
    original = await ApplyVerifier().verify("changed-original", snapshot)
    checkpoint = {
        "kind": "code_apply", "original_attempt": "changed-original",
        "snapshot_id": snapshot.snapshot_id, "content_fingerprint": original.after_fingerprint,
        "verification": original.as_dict(), "outcome": {},
    }
    (snapshot.workspace_root / "app.py").write_text("value = 3\n", encoding="utf-8")
    provider = CodeAgentProvider(
        snapshot_service=snapshots, loop_factory=lambda request, snapshot: None,
        sandbox_runner=Sandbox(), artifact_dir=tmp_path / "artifacts",
        verification_service=ApplyVerifier(), auto_apply_coordinator=coordinator,
    )

    result = await provider.resume(request(project, "changed-resume"), checkpoint, "confirmed", lambda event: None)

    assert result.status == "waiting_approval"
    assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"
