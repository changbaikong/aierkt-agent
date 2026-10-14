from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.code_agent_verification import VerificationRecord, snapshot_fingerprint
from agent_core.workspace_apply import (
    AutoApplyPolicy,
    CodeAgentAutoApplyCoordinator,
    WorkspaceApplyService,
    WorkspaceLockUnavailable,
    prepare_change_set,
)
from agent_core.workspace_snapshot import WorkspaceSnapshotService


class Sandbox:
    def __init__(self):
        self.prepared = []
        self.cleaned = []

    def prepare_attempt(self, attempt_id, root):
        self.prepared.append((attempt_id, Path(root)))

    def cleanup_attempt(self, attempt_id):
        self.cleaned.append(attempt_id)


class Verifier:
    def __init__(self, status="verified", mutate_project: Path | None = None, commands=()):
        self.status = status
        self.mutate_project = mutate_project
        self.commands = commands

    async def verify(self, attempt_id, snapshot):
        fingerprint = snapshot_fingerprint(snapshot)
        if self.mutate_project is not None:
            self.mutate_project.write_bytes(b"user continued\n")
        return VerificationRecord(
            self.status,
            self.status,
            str(snapshot.project_root),
            fingerprint,
            fingerprint,
            commands=self.commands,
        )


def setup_change(tmp_path: Path):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_bytes(b"value = 1\n")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = snapshots.create("attempt-1", project)
    (snapshot.workspace_root / "app.py").write_bytes(b"value = 2\n")
    fingerprint = snapshot_fingerprint(snapshot)
    initial = VerificationRecord(
        "verified", "verified", str(project), fingerprint, fingerprint
    )
    return project, snapshots, snapshot, initial


@pytest.mark.asyncio
async def test_auto_apply_rechecks_applied_project_in_fresh_sandbox_snapshot(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    sandbox = Sandbox()
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=sandbox,
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process("attempt-1", snapshot, initial)

    assert result["status"] == "applied_verified"
    assert (project / "app.py").read_bytes() == b"value = 2\n"
    assert len(sandbox.prepared) == 1
    assert sandbox.cleaned == [sandbox.prepared[0][0]]
    post_attempt = sandbox.prepared[0][0]
    with pytest.raises(Exception):
        snapshots.load(post_attempt)


@pytest.mark.asyncio
async def test_failed_post_apply_verification_restores_original_project(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier("unverified"),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process("attempt-1", snapshot, initial)

    assert result["status"] == "recovered"
    assert result["reason"] == "post_apply_verification_failed"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_post_apply_user_edit_stops_automatic_recovery(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier("verified", project / "app.py"),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process("attempt-1", snapshot, initial)

    assert result["status"] == "manual_recovery_required"
    assert (project / "app.py").read_bytes() == b"user continued\n"


@pytest.mark.asyncio
async def test_auto_apply_disabled_keeps_patch_only_behavior(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=False,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process("attempt-1", snapshot, initial)

    assert result["status"] == "patch_only"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_approved_protected_change_uses_same_fixed_fingerprint(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "requirements.txt").write_bytes(b"old==1\n")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = snapshots.create("attempt-1", project)
    (snapshot.workspace_root / "requirements.txt").write_bytes(b"new==2\n")
    fingerprint = snapshot_fingerprint(snapshot)
    initial = VerificationRecord("verified", "verified", str(project), fingerprint, fingerprint)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    waiting = await coordinator.process("attempt-1", snapshot, initial)
    applied = await coordinator.process(
        "attempt-1",
        snapshot,
        initial,
        approval_granted=True,
        expected_fingerprint=fingerprint,
    )
    replayed = await coordinator.process(
        "attempt-1",
        snapshot,
        initial,
        approval_granted=True,
        expected_fingerprint=fingerprint,
    )

    assert waiting["status"] == "approval_required"
    assert applied["status"] == "applied_verified"
    assert replayed["status"] == "conflict"
    assert (project / "requirements.txt").read_bytes() == b"new==2\n"


@pytest.mark.asyncio
async def test_approval_must_be_reissued_if_snapshot_fingerprint_changes(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )
    old_fingerprint = initial.after_fingerprint
    (snapshot.workspace_root / "app.py").write_bytes(b"value = 3\n")

    result = await coordinator.process(
        "attempt-1",
        snapshot,
        initial,
        approval_granted=True,
        expected_fingerprint=old_fingerprint,
    )

    assert result["status"] == "approval_required"
    assert result["reason"] == "content_changed_since_approval"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["unverified", "binary_change"])
async def test_approval_cannot_apply_unsafe_changes(tmp_path: Path, unsafe: str) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    if unsafe == "unverified":
        initial = VerificationRecord("unverified", "command_failed", str(project), initial.before_fingerprint, initial.after_fingerprint)
    elif unsafe == "binary_change":
        (snapshot.workspace_root / "app.py").write_bytes(b"\x00binary")
    fingerprint = snapshot_fingerprint(snapshot)
    if unsafe != "unverified":
        initial = VerificationRecord("verified", "verified", str(project), fingerprint, fingerprint)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process(
        "attempt-1", snapshot, initial,
        approval_granted=True, expected_fingerprint=fingerprint,
    )

    assert result["status"] == "unsafe_to_apply"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_post_apply_command_change_restores_original_project(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    original_command = {"runtime": "python", "argv": ["python", "-m", "pytest"], "cwd": ".", "timeout_seconds": 300}
    changed_command = {"runtime": "python", "argv": ["python", "-c", "pass"], "cwd": ".", "timeout_seconds": 300}
    initial = VerificationRecord(
        "verified", "verified", str(project), initial.before_fingerprint,
        initial.after_fingerprint, commands=(original_command,),
    )
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=Sandbox(),
        verification_service=Verifier(commands=(changed_command,)),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process("attempt-1", snapshot, initial)

    assert result["status"] == "recovered"
    assert result["reason"] == "post_apply_commands_changed"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_project_lock_stays_held_during_post_apply_verification(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    service = WorkspaceApplyService(tmp_path / "apply-state")
    second = WorkspaceApplyService(tmp_path / "apply-state")
    change_set = prepare_change_set(snapshot, initial, AutoApplyPolicy())

    class CheckingVerifier(Verifier):
        async def verify(self, attempt_id, post_snapshot):
            with pytest.raises(WorkspaceLockUnavailable):
                second.apply(change_set)
            return await super().verify(attempt_id, post_snapshot)

    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True, snapshot_service=snapshots, sandbox_runner=Sandbox(),
        verification_service=CheckingVerifier(), apply_service=service,
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process("attempt-1", snapshot, initial)

    assert result["status"] == "applied_verified"
    assert (project / "app.py").read_bytes() == b"value = 2\n"


@pytest.mark.asyncio
async def test_cancelled_auto_apply_does_not_change_project(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True, snapshot_service=snapshots, sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process(
        "attempt-1", snapshot, initial, cancelled=lambda: True
    )

    assert result["status"] == "cancelled"
    assert result["reason"] == "apply_cancelled"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_cancel_after_last_file_restores_before_post_verification(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True, snapshot_service=snapshots, sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process(
        "attempt-1", snapshot, initial, cancelled=cancelled
    )

    assert result["status"] == "recovered"
    assert result["reason"] == "apply_cancelled"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_cancel_during_post_verification_restores_change(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    state = {"cancelled": False}

    class CancellingVerifier(Verifier):
        async def verify(self, attempt_id, post_snapshot):
            result = await super().verify(attempt_id, post_snapshot)
            state["cancelled"] = True
            return result

    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True, snapshot_service=snapshots, sandbox_runner=Sandbox(),
        verification_service=CancellingVerifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process(
        "attempt-1", snapshot, initial,
        cancelled=lambda: state["cancelled"],
    )

    assert result["status"] == "recovered"
    assert result["reason"] == "apply_cancelled"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.asyncio
async def test_cancel_before_success_result_restores_change(tmp_path: Path) -> None:
    project, snapshots, snapshot, initial = setup_change(tmp_path)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 5

    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True, snapshot_service=snapshots, sandbox_runner=Sandbox(),
        verification_service=Verifier(),
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    result = await coordinator.process(
        "attempt-1", snapshot, initial, cancelled=cancelled
    )

    assert result["status"] == "recovered"
    assert result["reason"] == "apply_cancelled"
    assert (project / "app.py").read_bytes() == b"value = 1\n"
