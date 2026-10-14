from __future__ import annotations

import json
from pathlib import Path

from agent_core.workspace_snapshot import WorkspaceSnapshotService


def test_snapshot_copies_working_tree_and_writes_host_manifest(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("print('working tree')\n", encoding="utf-8")
    (project / "notes.txt").write_text("uncommitted\n", encoding="utf-8")

    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = service.create("attempt-1", project)

    assert (snapshot.workspace_root / "src" / "app.py").read_text(encoding="utf-8") == "print('working tree')\n"
    assert (snapshot.workspace_root / "notes.txt").read_text(encoding="utf-8") == "uncommitted\n"
    assert snapshot.manifest_path.parent != snapshot.workspace_root
    manifest = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    by_path = {item["path"]: item for item in manifest["files"]}
    assert by_path["src/app.py"]["type"] == "file"
    assert by_path["src/app.py"]["size"] == len(
        (snapshot.workspace_root / "src" / "app.py").read_bytes()
    )
    assert len(by_path["src/app.py"]["sha256"]) == 64


def test_snapshot_excludes_repository_secrets_and_generated_data(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    excluded_dirs = [".git", "__pycache__", ".pytest_cache", "node_modules", "dist", "build", ".venv"]
    for name in excluded_dirs:
        folder = project / name
        folder.mkdir()
        (folder / "hidden.txt").write_text("hidden", encoding="utf-8")
    for name in [".env", ".env.local", "server.pem", "private.key", "work.db"]:
        (project / name).write_text("secret", encoding="utf-8")
    (project / "README.md").write_text("safe", encoding="utf-8")

    snapshot = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]).create(
        "attempt-2", project
    )

    assert (snapshot.workspace_root / "README.md").is_file()
    for name in excluded_dirs:
        assert not (snapshot.workspace_root / name).exists()
    for name in [".env", ".env.local", "server.pem", "private.key", "work.db"]:
        assert not (snapshot.workspace_root / name).exists()


def test_cleanup_removes_only_named_attempt(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "a.txt").write_text("a", encoding="utf-8")
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    first = service.create("attempt-a", project)
    second = service.create("attempt-b", project)

    service.cleanup("attempt-a")

    assert not first.attempt_root.exists()
    assert not first.manifest_path.exists()
    assert second.attempt_root.exists()
