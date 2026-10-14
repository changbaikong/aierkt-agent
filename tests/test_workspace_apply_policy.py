from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.code_agent_verification import VerificationRecord, snapshot_fingerprint
from agent_core.workspace_apply import AutoApplyPolicy, prepare_change_set
from agent_core.workspace_snapshot import WorkspaceSnapshot, WorkspaceSnapshotService


def snapshot(tmp_path: Path, files: dict[str, str | bytes]):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    for name, content in files.items():
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    return service.create("attempt-1", project)


def verified(snap):
    fingerprint = snapshot_fingerprint(snap)
    return VerificationRecord(
        status="verified",
        reason="verified",
        project_root=str(snap.project_root),
        before_fingerprint=fingerprint,
        after_fingerprint=fingerprint,
    )


def test_policy_prepares_fixed_text_content_without_applying_it(tmp_path: Path) -> None:
    snap = snapshot(tmp_path, {"app.py": "value = 1\n", "README.md": "old\n"})
    (snap.workspace_root / "app.py").write_bytes(b"value = 2\n")
    (snap.workspace_root / "notes.txt").write_bytes(b"new\n")
    record = verified(snap)

    prepared = prepare_change_set(snap, record, AutoApplyPolicy())

    assert prepared.eligible is True
    assert prepared.reasons == ()
    assert [change.path for change in prepared.changes] == ["app.py", "notes.txt"]
    assert prepared.changes[0].operation == "modify"
    assert prepared.changes[0].content == b"value = 2\n"
    assert prepared.changes[1].operation == "add"
    assert prepared.content_fingerprint == record.after_fingerprint
    assert (snap.project_root / "app.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("AGENTS.md", "protected_file"),
        ("CLAUDE.md", "protected_file"),
        (".cursor/rules/project.mdc", "protected_file"),
        ("requirements.txt", "protected_file"),
        ("package.json", "protected_file"),
        ("Cargo.lock", "protected_file"),
        ("go.mod", "protected_file"),
        ("pyproject.toml", "protected_file"),
        ("package-lock.json", "protected_file"),
        (".github/workflows/ci.yml", "protected_file"),
        (".gitlab-ci.yml", "protected_file"),
        ("azure-pipelines.yml", "protected_file"),
        ("Jenkinsfile", "protected_file"),
        ("scripts/start_agent.py", "protected_file"),
        ("settings.yaml", "unsupported_file_type"),
    ],
)
def test_policy_sends_protected_and_configuration_files_to_approval(
    tmp_path: Path, name: str, reason: str
) -> None:
    snap = snapshot(tmp_path, {name: "old\n"})
    (snap.workspace_root / name).write_text("new\n", encoding="utf-8")

    prepared = prepare_change_set(snap, verified(snap), AutoApplyPolicy())

    assert prepared.eligible is False
    assert reason in prepared.reasons


def test_policy_sends_deletions_and_binary_changes_to_approval(tmp_path: Path) -> None:
    snap = snapshot(tmp_path, {"old.py": "old\n", "asset.png": b"\x00old"})
    (snap.workspace_root / "old.py").unlink()
    (snap.workspace_root / "asset.png").write_bytes(b"\x00new")

    prepared = prepare_change_set(snap, verified(snap), AutoApplyPolicy())

    assert prepared.eligible is False
    assert "deletion" in prepared.reasons
    assert "binary_change" in prepared.reasons


@pytest.mark.parametrize(
    ("policy", "files", "reason"),
    [
        (AutoApplyPolicy(max_files=1), {"a.py": "2\n", "b.py": "2\n"}, "file_limit"),
        (AutoApplyPolicy(max_changed_lines=1), {"a.py": "2\n3\n"}, "line_limit"),
        (AutoApplyPolicy(max_content_bytes=1), {"a.py": "22\n"}, "content_limit"),
    ],
)
def test_policy_enforces_all_auto_apply_limits(
    tmp_path: Path, policy: AutoApplyPolicy, files: dict[str, str], reason: str
) -> None:
    snap = snapshot(tmp_path, {name: "1\n" for name in files})
    for name, content in files.items():
        (snap.workspace_root / name).write_text(content, encoding="utf-8")

    prepared = prepare_change_set(snap, verified(snap), policy)

    assert prepared.eligible is False
    assert reason in prepared.reasons


def test_policy_rejects_stale_or_missing_verification(tmp_path: Path) -> None:
    snap = snapshot(tmp_path, {"app.py": "1\n"})
    record = verified(snap)
    (snap.workspace_root / "app.py").write_text("2\n", encoding="utf-8")

    stale = prepare_change_set(snap, record, AutoApplyPolicy())
    absent = prepare_change_set(snap, None, AutoApplyPolicy())

    assert stale.eligible is False
    assert "verification_mismatch" in stale.reasons
    assert absent.eligible is False
    assert "unverified" in absent.reasons


def test_policy_accepts_values_exactly_at_all_limits(tmp_path: Path) -> None:
    snap = snapshot(tmp_path, {"a.py": "1\n"})
    (snap.workspace_root / "a.py").write_bytes(b"2\n")
    policy = AutoApplyPolicy(max_files=1, max_changed_lines=2, max_content_bytes=2)

    prepared = prepare_change_set(snap, verified(snap), policy)

    assert prepared.eligible is True
    assert prepared.total_changed_lines == 2
    assert prepared.total_content_bytes == 2


def test_policy_rejects_snapshot_change_while_preparing_fixed_contents(
    tmp_path: Path, monkeypatch
) -> None:
    snap = snapshot(tmp_path, {"app.py": "value = 1\n"})
    (snap.workspace_root / "app.py").write_bytes(b"value = 2\n")
    record = verified(snap)
    original_diff = WorkspaceSnapshot.diff

    def changing_diff(current):
        result = original_diff(current)
        (current.workspace_root / "app.py").write_bytes(b"value = 3\n")
        return result

    monkeypatch.setattr(WorkspaceSnapshot, "diff", changing_diff)
    prepared = prepare_change_set(snap, record, AutoApplyPolicy())

    assert prepared.eligible is False
    assert "snapshot_changed_during_preparation" in prepared.reasons
