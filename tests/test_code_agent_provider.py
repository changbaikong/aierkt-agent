from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.legacy_code_agent.code_agent_contracts import CodeAgentCheckpoint, CodeAgentOutcome
from agent_core.legacy_code_agent.code_agent_provider import CodeAgentProvider
from agent_core.code_agent_verification import VerificationRecord
from agent_core.contracts import Scope
from agent_core.work_provider import WorkRequest
from agent_core.workspace_snapshot import WorkspaceSnapshotService


class Sandbox:
    def __init__(self) -> None:
        self.prepared = []
        self.cancelled = []
        self.cleaned = []

    def prepare_attempt(self, attempt_id, root):
        self.prepared.append((attempt_id, Path(root)))

    async def cancel(self, attempt_id):
        self.cancelled.append(attempt_id)
        return True

    def cleanup_attempt(self, attempt_id):
        self.cleaned.append(attempt_id)


class Loop:
    def __init__(self, outcome):
        self.outcome = outcome
        self.cancelled = False
        self.resumed = []

    async def run(self, request, snapshot):
        (snapshot.workspace_root / "app.py").write_text("value = 2\n", encoding="utf-8")
        return self.outcome

    async def resume(self, checkpoint, decision):
        self.resumed.append((checkpoint, decision))
        return self.outcome

    def cancel(self):
        self.cancelled = True


class Verifier:
    def __init__(self, status="verified"):
        self.status = status
        self.calls = []

    async def verify(self, attempt_id, snapshot):
        self.calls.append((attempt_id, snapshot.snapshot_id))
        fingerprint = "a" * 64
        return VerificationRecord(
            status=self.status,
            reason=self.status,
            project_root=str(snapshot.project_root),
            before_fingerprint=fingerprint,
            after_fingerprint=fingerprint,
            commands=({"argv": ["python", "-m", "pytest"], "exit_code": 0},),
        )


class AutoApply:
    def __init__(self):
        self.calls = []

    async def process(self, attempt_id, snapshot, verification, **kwargs):
        self.calls.append((attempt_id, snapshot.snapshot_id, verification.status, kwargs))
        return {"status": "applied_verified", "recovery_package": "recovery-1"}


def make_request(project: Path, attempt_id="attempt-1"):
    return WorkRequest(
        work_item_id="work-1",
        attempt_id=attempt_id,
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat"),
        objective="change value",
        task_type="code.agent",
        input={"project_root": str(project)},
    )


@pytest.mark.asyncio
async def test_provider_creates_snapshot_patch_artifacts_without_touching_project(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    sandbox = Sandbox()
    loop = Loop(
        CodeAgentOutcome(
            "completed",
            summary="changed",
            verification_status="verified",
            output={"steps_used": 2},
            evidence=({"name": "code__run", "status": "completed"},),
        )
    )
    verifier = Verifier()
    auto_apply = AutoApply()
    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=lambda request, snapshot: loop,
        sandbox_runner=sandbox,
        artifact_dir=tmp_path / "artifacts",
        verification_service=verifier,
        auto_apply_coordinator=auto_apply,
    )

    events = []
    result = await provider.execute(make_request(project), lambda event: events.append(event))

    assert result.status == "completed"
    assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "value = 2" in result.output["patch"]
    assert result.output["verification_status"] == "verified"
    assert result.output["verification"]["before_fingerprint"] == "a" * 64
    assert verifier.calls == [("attempt-1", result.output["verification"]["snapshot_id"])]
    assert result.output["apply_status"] == "applied_verified"
    assert len(auto_apply.calls) == 1
    call = auto_apply.calls[0]
    assert call[:3] == ("attempt-1", result.output["verification"]["snapshot_id"], "verified")
    assert callable(call[3]["cancelled"])
    assert call[3]["cancelled"]() is False
    assert {artifact.media_type for artifact in result.artifacts} == {
        "text/x-diff",
        "application/json",
    }
    summary = next(item for item in result.artifacts if item.media_type == "application/json")
    payload = json.loads(Path(summary.path).read_text(encoding="utf-8"))
    assert payload["summary"] == "changed"
    assert sandbox.prepared[0][0] == "attempt-1"
    assert sandbox.cleaned == ["attempt-1"]
    assert any(event.event_type == "progress" for event in events)


@pytest.mark.asyncio
async def test_provider_never_trusts_loop_verification_without_host_verifier(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    loop = Loop(CodeAgentOutcome("completed", verification_status="verified"))
    provider = CodeAgentProvider(
        snapshot_service=WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]),
        loop_factory=lambda request, snapshot: loop,
        sandbox_runner=Sandbox(),
        artifact_dir=tmp_path / "artifacts",
    )

    result = await provider.execute(make_request(project), lambda event: None)

    # 没配验证器 = 检查根本没跑过：必须报 not_run，而不是 unverified。
    assert result.output["verification_status"] == "not_run"
    assert result.output["verification"]["reason"] == "host_verifier_unavailable"


@pytest.mark.asyncio
async def test_provider_cancel_stops_loop_and_sandbox(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("x", encoding="utf-8")
    loop = Loop(CodeAgentOutcome("cancelled"))
    sandbox = Sandbox()
    provider = CodeAgentProvider(
        snapshot_service=WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]),
        loop_factory=lambda request, snapshot: loop,
        sandbox_runner=sandbox,
        artifact_dir=tmp_path / "artifacts",
    )
    provider._active_loops["attempt-cancel"] = loop

    assert await provider.cancel("attempt-cancel") is True
    assert loop.cancelled is True
    assert sandbox.cancelled == ["attempt-cancel"]
