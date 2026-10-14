from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.workspace_snapshot import SnapshotValidationError, WorkspaceSnapshotService


def make_snapshot(tmp_path: Path):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "keep.txt").write_text("old\n", encoding="utf-8")
    (project / "delete.txt").write_text("gone\n", encoding="utf-8")
    (project / "binary.bin").write_bytes(b"\x00\x01")
    (project / "requirements.txt").write_text("one==1\n", encoding="utf-8")
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    return service, service.create("attempt-diff", project)


def test_diff_reports_text_add_modify_delete_and_patch(tmp_path: Path) -> None:
    _, snapshot = make_snapshot(tmp_path)
    (snapshot.workspace_root / "keep.txt").write_text("new\n", encoding="utf-8")
    (snapshot.workspace_root / "delete.txt").unlink()
    (snapshot.workspace_root / "added.py").write_text("print('new')\n", encoding="utf-8")

    diff = snapshot.diff()

    assert diff.added == ("added.py",)
    assert diff.modified == ("keep.txt",)
    assert diff.deleted == ("delete.txt",)
    assert "--- a/keep.txt" in diff.patch
    assert "+++ b/keep.txt" in diff.patch
    assert "-old" in diff.patch
    assert "+new" in diff.patch
    assert "+++ b/added.py" in diff.patch
    assert "+++ /dev/null" in diff.patch


def test_diff_lists_binary_changes_without_embedding_bytes(tmp_path: Path) -> None:
    _, snapshot = make_snapshot(tmp_path)
    (snapshot.workspace_root / "binary.bin").write_bytes(b"\x00\x02\xff")
    (snapshot.workspace_root / "new.bin").write_bytes(b"\x00secret\xff")

    diff = snapshot.diff()

    assert set(diff.binary_changes) == {"binary.bin", "new.bin"}
    assert "secret" not in diff.patch
    assert "binary.bin" not in diff.patch


@pytest.mark.parametrize("name", ["requirements.txt", "CLAUDE.md", ".gitlab-ci.yml"])
def test_diff_marks_protected_files(tmp_path: Path, name: str) -> None:
    _, snapshot = make_snapshot(tmp_path)
    target = snapshot.workspace_root / name
    target.write_text("two==2\n", encoding="utf-8")

    diff = snapshot.diff()

    assert name in diff.protected_changes


def test_diff_rejects_reparse_point_added_after_snapshot(tmp_path: Path) -> None:
    _, snapshot = make_snapshot(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = snapshot.workspace_root / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating a symlink requires unavailable privileges")

    with pytest.raises(SnapshotValidationError, match="reparse"):
        snapshot.diff()
