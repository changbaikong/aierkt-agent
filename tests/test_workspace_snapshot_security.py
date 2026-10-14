from __future__ import annotations

import os
import json
from pathlib import Path

import pytest

from agent_core.workspace_snapshot import SnapshotValidationError, WorkspaceSnapshotService


def test_snapshot_rejects_project_outside_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])

    with pytest.raises(SnapshotValidationError, match="allowed"):
        service.create("attempt-1", outside)


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_snapshot_rejects_unc_and_device_paths(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])

    for unsafe in [Path(r"\\server\share\repo"), Path(r"\\?\C:\repo")]:
        with pytest.raises(SnapshotValidationError, match="UNC|device"):
            service.create("attempt-unsafe", unsafe)


def test_snapshot_rejects_symlink_inside_project(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    project = allowed / "repo"
    target = tmp_path / "outside"
    project.mkdir(parents=True)
    target.mkdir()
    (target / "secret.txt").write_text("secret", encoding="utf-8")
    link = project / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("creating a directory symlink requires unavailable privileges")

    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    with pytest.raises(SnapshotValidationError, match="reparse"):
        service.create("attempt-link", project)
    assert not any((tmp_path / "snapshots").glob("*.partial"))


def test_snapshot_rejects_existing_attempt_and_leaves_existing_data(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    project = allowed / "repo"
    project.mkdir(parents=True)
    (project / "a.txt").write_text("a", encoding="utf-8")
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = service.create("same-attempt", project)

    with pytest.raises(SnapshotValidationError, match="already exists"):
        service.create("same-attempt", project)

    assert (snapshot.workspace_root / "a.txt").read_text(encoding="utf-8") == "a"


def test_snapshot_excludes_project_runtime_data_directory(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    project = allowed / "repo"
    runtime_data = project / "data" / "memory_vectors"
    runtime_data.mkdir(parents=True)
    (project / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (runtime_data / ".lock").write_text("runtime-only", encoding="utf-8")
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])

    snapshot = service.create("attempt-runtime-data", project)

    assert (snapshot.workspace_root / "app.py").is_file()
    assert not (snapshot.workspace_root / "data" / "memory_vectors").exists()


def test_snapshot_rejects_tampered_host_manifest(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    project = allowed / "repo"
    project.mkdir(parents=True)
    (project / "a.txt").write_text("a", encoding="utf-8")
    service = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = service.create("attempt-manifest", project)
    payload = json.loads(snapshot.manifest_path.read_text(encoding="utf-8"))
    payload["snapshot_id"] = "attacker-controlled"
    snapshot.manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SnapshotValidationError, match="manifest"):
        service.load("attempt-manifest")
