from __future__ import annotations

import os
import errno
import subprocess
from pathlib import Path

import pytest

from agent_core.code_agent_verification import VerificationRecord, snapshot_fingerprint
from agent_core.workspace_apply import (
    AutoApplyPolicy,
    WorkspaceApplyService,
    WorkspaceLockUnavailable,
    prepare_change_set,
)
from agent_core.workspace_snapshot import WorkspaceSnapshotService


def prepared(tmp_path: Path, changes: dict[str, str]):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_bytes(b"value = 1\n")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snap = snapshots.create("attempt-1", project)
    for name, content in changes.items():
        target = snap.workspace_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode())
    fingerprint = snapshot_fingerprint(snap)
    verification = VerificationRecord(
        "verified",
        "verified",
        str(project),
        fingerprint,
        fingerprint,
    )
    return project, prepare_change_set(snap, verification, AutoApplyPolicy())


def test_apply_uses_fixed_contents_and_creates_durable_recovery_package(tmp_path: Path) -> None:
    project, change_set = prepared(
        tmp_path, {"app.py": "value = 2\n", "nested/new.py": "new = True\n"}
    )
    original_mode = (project / "app.py").stat().st_mode
    service = WorkspaceApplyService(tmp_path / "apply-state")

    result = service.apply(change_set)

    assert result.status == "applied"
    assert (project / "app.py").read_bytes() == b"value = 2\n"
    assert (project / "nested/new.py").read_bytes() == b"new = True\n"
    assert (project / "app.py").stat().st_mode == original_mode
    assert result.transaction_dir.is_dir()
    assert (result.transaction_dir / "transaction.json").is_file()
    assert (result.transaction_dir / "originals" / "app.py").read_bytes() == b"value = 1\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_apply_preserves_explicit_windows_file_permissions(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32api
    import win32con
    import win32security

    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    target = project / "app.py"
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION_DS,
        0,
        ntsecuritycon.FILE_ALL_ACCESS,
        sid,
    )
    win32security.SetNamedSecurityInfo(
        str(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    before_descriptor = win32security.GetFileSecurity(
        str(target), win32security.DACL_SECURITY_INFORMATION
    )
    before_dacl = before_descriptor.GetSecurityDescriptorDacl()
    before_aces = [before_dacl.GetAce(index) for index in range(before_dacl.GetAceCount())]
    before_protected = bool(before_descriptor.GetSecurityDescriptorControl()[0] & 0x1000)

    result = WorkspaceApplyService(tmp_path / "apply-state").apply(change_set)

    after_descriptor = win32security.GetFileSecurity(
        str(target), win32security.DACL_SECURITY_INFORMATION
    )
    after_dacl = after_descriptor.GetSecurityDescriptorDacl()
    after_aces = [after_dacl.GetAce(index) for index in range(after_dacl.GetAceCount())]
    after_protected = bool(after_descriptor.GetSecurityDescriptorControl()[0] & 0x1000)
    assert result.status == "applied"
    assert after_aces == before_aces
    assert after_protected == before_protected


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_apply_holds_target_against_concurrent_write_until_replace(
    tmp_path: Path, monkeypatch
) -> None:
    import win32file

    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    concurrent_write_blocked = False
    original_replace = win32file.ReplaceFile

    def replace(source, target, backup, flags, exclude, reserved) -> None:
        nonlocal concurrent_write_blocked
        try:
            Path(target).write_bytes(b"racing user edit\n")
        except PermissionError:
            concurrent_write_blocked = True
        original_replace(source, target, backup, flags, exclude, reserved)

    monkeypatch.setattr(win32file, "ReplaceFile", replace)
    result = WorkspaceApplyService(tmp_path / "apply-state").apply(change_set)

    assert result.status == "applied"
    assert concurrent_write_blocked is True
    assert (project / "app.py").read_bytes() == b"value = 2\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows replacement semantics")
def test_apply_preserves_concurrent_path_replacement(tmp_path: Path, monkeypatch) -> None:
    import ntsecuritycon
    import win32api
    import win32con
    import win32file
    import win32security

    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    original_replace = win32file.ReplaceFile
    raced = False
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]

    def replace(source, target, backup, flags, exclude, reserved) -> None:
        nonlocal raced
        if not raced:
            raced = True
            racing = Path(target).with_suffix(".racing")
            racing.write_bytes(b"user replacement\n")
            original_replace(str(racing), target, None, 0, None, None)
            dacl = win32security.ACL()
            dacl.AddAccessAllowedAceEx(
                win32security.ACL_REVISION_DS, 0, ntsecuritycon.FILE_ALL_ACCESS, sid
            )
            win32security.SetNamedSecurityInfo(
                target, win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION
                | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
                None, None, dacl, None,
            )
        original_replace(source, target, backup, flags, exclude, reserved)

    monkeypatch.setattr(win32file, "ReplaceFile", replace)
    result = WorkspaceApplyService(tmp_path / "apply-state").apply(change_set)

    assert result.status == "conflict"
    assert result.reason == "concurrent_target_change"
    assert (project / "app.py").read_bytes() == b"user replacement\n"
    descriptor = win32security.GetFileSecurity(
        str(project / "app.py"), win32security.DACL_SECURITY_INFORMATION
    )
    assert descriptor.GetSecurityDescriptorControl()[0] & 0x1000


@pytest.mark.skipif(os.name != "nt", reason="Windows replacement semantics")
def test_restart_restores_concurrent_replacement_saved_at_crash(
    tmp_path: Path, monkeypatch
) -> None:
    import win32file

    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    original_replace = win32file.ReplaceFile
    crashed = False

    def replace(source, target, backup, flags, exclude, reserved) -> None:
        nonlocal crashed
        if not crashed:
            racing = Path(target).with_suffix(".racing")
            racing.write_bytes(b"user replacement\n")
            original_replace(str(racing), target, None, 0, None, None)
            original_replace(source, target, backup, flags, exclude, reserved)
            crashed = True
            raise KeyboardInterrupt("crash after atomic replacement")
        original_replace(source, target, backup, flags, exclude, reserved)

    state = tmp_path / "apply-state"
    monkeypatch.setattr(win32file, "ReplaceFile", replace)
    with pytest.raises(KeyboardInterrupt):
        WorkspaceApplyService(state).apply(change_set)
    monkeypatch.setattr(win32file, "ReplaceFile", original_replace)

    result = WorkspaceApplyService(state).recover_pending()

    assert [item.status for item in result] == ["recovered"]
    assert (project / "app.py").read_bytes() == b"user replacement\n"


def test_recovery_does_not_overwrite_edit_in_restore_window(
    tmp_path: Path, monkeypatch
) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    service = WorkspaceApplyService(tmp_path / "apply-state")
    applied = service.apply(change_set)
    original_restore = service._atomic_restore

    def racing_restore(*args, **kwargs):
        (project / "app.py").write_bytes(b"user in restore window\n")
        return original_restore(*args, **kwargs)

    monkeypatch.setattr(service, "_atomic_restore", racing_restore)
    result = service.recover_transaction(applied.transaction_dir)

    assert result.status == "manual_recovery_required"
    assert (project / "app.py").read_bytes() == b"user in restore window\n"


def test_apply_refuses_conflict_without_writing_any_file(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n", "new.py": "new\n"})
    (project / "app.py").write_bytes(b"user edit\n")
    service = WorkspaceApplyService(tmp_path / "apply-state")

    result = service.apply(change_set)

    assert result.status == "conflict"
    assert result.reason == "baseline_mismatch"
    assert (project / "app.py").read_bytes() == b"user edit\n"
    assert not (project / "new.py").exists()


def test_apply_rolls_back_all_prior_files_when_later_replace_fails(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n", "new.py": "new\n"})
    calls = 0

    def replace(source: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if target.name == "new.py":
            raise PermissionError("denied")
        os.replace(source, target)

    service = WorkspaceApplyService(tmp_path / "apply-state", replace_file=replace)

    result = service.apply(change_set)

    assert calls >= 2
    assert result.status == "recovered"
    assert (project / "app.py").read_bytes() == b"value = 1\n"
    assert not (project / "new.py").exists()


def test_apply_cancellation_between_files_restores_prior_changes(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n", "new.py": "new\n"})
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    result = WorkspaceApplyService(tmp_path / "apply-state").apply(
        change_set, cancelled=cancelled
    )

    assert result.status == "recovered"
    assert result.reason == "apply_cancelled"
    assert (project / "app.py").read_bytes() == b"value = 1\n"
    assert not (project / "new.py").exists()


def test_apply_recovers_when_disk_fills_during_replace(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})

    def disk_full(source: Path, target: Path) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    result = WorkspaceApplyService(
        tmp_path / "apply-state", replace_file=disk_full
    ).apply(change_set)

    assert result.status == "recovered"
    assert (project / "app.py").read_bytes() == b"value = 1\n"


def test_restart_recovers_interrupted_transaction_idempotently(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    calls = 0

    def crash_after_replace(source: Path, target: Path) -> None:
        nonlocal calls
        os.replace(source, target)
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt("simulated process death")

    state = tmp_path / "apply-state"
    crashing = WorkspaceApplyService(state, replace_file=crash_after_replace)
    with pytest.raises(KeyboardInterrupt):
        crashing.apply(change_set)
    assert (project / "app.py").read_bytes() == b"value = 2\n"

    restarted = WorkspaceApplyService(state)
    first = restarted.recover_pending()
    second = restarted.recover_pending()

    assert [item.status for item in first] == ["recovered"]
    assert second == []
    assert (project / "app.py").read_bytes() == b"value = 1\n"


def test_recovery_never_overwrites_user_edit_after_interruption(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})

    def crash(source: Path, target: Path) -> None:
        os.replace(source, target)
        raise KeyboardInterrupt("simulated process death")

    state = tmp_path / "apply-state"
    with pytest.raises(KeyboardInterrupt):
        WorkspaceApplyService(state, replace_file=crash).apply(change_set)
    (project / "app.py").write_bytes(b"user continued\n")

    result = WorkspaceApplyService(state).recover_pending()

    assert [item.status for item in result] == ["manual_recovery_required"]
    assert (project / "app.py").read_bytes() == b"user continued\n"


def test_apply_rejects_hard_link_targets(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    os.link(project / "app.py", project / "alias.py")

    result = WorkspaceApplyService(tmp_path / "apply-state").apply(change_set)

    assert result.status == "conflict"
    assert result.reason == "hard_link_target"


def test_apply_detects_parent_directory_replacement_before_writing(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    original = allowed / "demo"
    (original / "nested").mkdir(parents=True)
    (original / "nested/keep.py").write_bytes(b"keep = True\n")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snap = snapshots.create("attempt-1", original)
    (snap.workspace_root / "nested/new.py").write_bytes(b"new = True\n")
    fingerprint = snapshot_fingerprint(snap)
    fixed = prepare_change_set(
        snap,
        VerificationRecord("verified", "verified", str(original), fingerprint, fingerprint),
        AutoApplyPolicy(),
    )

    class ReplacingParentService(WorkspaceApplyService):
        def _create_transaction(self, change_set):
            transaction = super()._create_transaction(change_set)
            old = original / "nested-old"
            (original / "nested").rename(old)
            (original / "nested").mkdir()
            (original / "nested/keep.py").write_bytes(b"user replacement\n")
            return transaction

    result = ReplacingParentService(tmp_path / "parent-state").apply(fixed)

    assert result.status == "manual_recovery_required"
    assert not (original / "nested/new.py").exists()
    assert (original / "nested/keep.py").read_bytes() == b"user replacement\n"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_apply_rejects_real_windows_junction_inserted_after_preflight(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (nested / "keep.py").write_bytes(b"keep = True\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snap = snapshots.create("attempt-junction", project)
    (snap.workspace_root / "nested/new.py").write_bytes(b"new = True\n")
    fingerprint = snapshot_fingerprint(snap)
    change_set = prepare_change_set(
        snap,
        VerificationRecord("verified", "verified", str(project), fingerprint, fingerprint),
        AutoApplyPolicy(),
    )

    class JunctionService(WorkspaceApplyService):
        def _create_transaction(self, prepared):
            transaction = super()._create_transaction(prepared)
            (project / "nested").rename(project / "nested-old")
            completed = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(project / "nested"), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            return transaction

    result = JunctionService(tmp_path / "junction-state").apply(change_set)

    assert result.status == "manual_recovery_required"
    assert not (outside / "new.py").exists()


def test_project_lock_is_cross_service_and_non_reentrant(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    first = WorkspaceApplyService(tmp_path / "apply-state")
    second = WorkspaceApplyService(tmp_path / "apply-state")

    with first.project_lock(project):
        with pytest.raises(WorkspaceLockUnavailable):
            second.apply(change_set)


def test_approved_deletion_is_recoverable(tmp_path: Path) -> None:
    allowed = tmp_path / "delete-projects"
    delete_project = allowed / "demo"
    delete_project.mkdir(parents=True)
    (delete_project / "old.py").write_bytes(b"old = True\n")
    snapshots = WorkspaceSnapshotService(tmp_path / "delete-snapshots", [allowed])
    snap = snapshots.create("attempt-delete", delete_project)
    (snap.workspace_root / "old.py").unlink()
    fingerprint = snapshot_fingerprint(snap)
    delete_set = prepare_change_set(
        snap,
        VerificationRecord("verified", "verified", str(delete_project), fingerprint, fingerprint),
        AutoApplyPolicy(),
    )
    service = WorkspaceApplyService(tmp_path / "delete-state")

    applied = service.apply(delete_set, approval_granted=True)
    recovered = service.recover_transaction(applied.transaction_dir)

    assert applied.status == "applied"
    assert recovered.status == "recovered"
    assert (delete_project / "old.py").read_bytes() == b"old = True\n"


def test_recovery_refuses_replaced_parent_directory(tmp_path: Path) -> None:
    project, change_set = prepared(tmp_path, {"nested/new.py": "new = True\n"})
    (project / "nested").mkdir()
    service = WorkspaceApplyService(tmp_path / "apply-state")
    applied = service.apply(change_set)
    assert applied.status == "applied"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "new.py").write_bytes(b"new = True\n")
    (project / "nested").rename(project / "nested-old")
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/d", "/c", "mklink", "/J", str(project / "nested"), str(outside)],
            capture_output=True, text=True, check=False,
        )
        assert created.returncode == 0, created.stderr
    else:
        os.symlink(outside, project / "nested", target_is_directory=True)

    result = service.recover_transaction(applied.transaction_dir)

    assert result.status == "manual_recovery_required"
    assert (outside / "new.py").read_bytes() == b"new = True\n"


def test_recovery_write_failure_remains_pending_for_restart(tmp_path: Path, monkeypatch) -> None:
    project, change_set = prepared(tmp_path, {"app.py": "value = 2\n"})
    service = WorkspaceApplyService(tmp_path / "apply-state")
    applied = service.apply(change_set)
    original_restore = service._atomic_restore

    def denied(*args, **kwargs):
        raise PermissionError("restore denied")

    monkeypatch.setattr(service, "_atomic_restore", denied)
    with pytest.raises(PermissionError):
        service.recover_transaction(applied.transaction_dir)
    assert (project / "app.py").read_bytes() == b"value = 2\n"
    monkeypatch.setattr(service, "_atomic_restore", original_restore)

    restarted = WorkspaceApplyService(tmp_path / "apply-state")
    assert [item.status for item in restarted.recover_pending()] == ["recovered"]
    assert (project / "app.py").read_bytes() == b"value = 1\n"


def test_unresolved_recovery_blocks_new_changes_to_same_project(tmp_path: Path) -> None:
    project, first = prepared(tmp_path, {"app.py": "value = 2\n"})
    service = WorkspaceApplyService(tmp_path / "apply-state")
    applied = service.apply(first)
    assert applied.status == "applied"
    (project / "app.py").write_bytes(b"user continued\n")
    assert service.recover_transaction(applied.transaction_dir).status == "manual_recovery_required"

    snapshots = WorkspaceSnapshotService(tmp_path / "next-snapshots", [project.parent])
    snapshot = snapshots.create("next-attempt", project)
    (snapshot.workspace_root / "new.py").write_bytes(b"new = True\n")
    fingerprint = snapshot_fingerprint(snapshot)
    next_change = prepare_change_set(
        snapshot,
        VerificationRecord("verified", "verified", str(project), fingerprint, fingerprint),
        AutoApplyPolicy(),
    )
    result = WorkspaceApplyService(tmp_path / "apply-state").apply(next_change)

    assert result.status == "recovery_required"
    assert not (project / "new.py").exists()


def test_apply_state_must_be_outside_allowed_projects(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    allowed.mkdir()

    with pytest.raises(ValueError, match="outside allowed project roots"):
        WorkspaceApplyService(allowed / "demo" / ".recovery", forbidden_roots=[allowed])

    assert not (allowed / "demo").exists()
