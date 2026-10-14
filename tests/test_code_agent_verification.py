from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_core.code_agent_verification import (
    CodeAgentVerificationService,
    VerificationConfig,
)
from agent_core.sandbox_contracts import SandboxResult
from agent_core.workspace_snapshot import WorkspaceSnapshotService


class Runner:
    def __init__(self, results: list[SandboxResult], *, mutate: Path | None = None) -> None:
        self.results = list(results)
        self.mutate = mutate
        self.commands = []

    async def run(self, attempt_id, command):
        self.commands.append((attempt_id, command))
        result = self.results.pop(0)
        if self.mutate is not None:
            self.mutate.write_text("changed after verification\n", encoding="utf-8")
            self.mutate = None
        return result


def result(*, exit_code=0, timed_out=False, cancelled=False, truncated=False):
    now = datetime.now(timezone.utc)
    return SandboxResult(
        exit_code=exit_code,
        stdout="ok",
        stderr="",
        started_at=now,
        finished_at=now,
        timed_out=timed_out,
        cancelled=cancelled,
        killed=timed_out or cancelled,
        output_truncated=truncated,
        termination_reason=None if exit_code == 0 and not timed_out and not cancelled else "process_failed",
    )


def configured(tmp_path: Path):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = snapshots.create("attempt-1", project)
    config_path = tmp_path / "verification.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {
                        "project_root": str(project),
                        "commands": [
                            {
                                "runtime": "python",
                                "args": ["-I", "-m", "pytest", "-q"],
                                "cwd": ".",
                                "timeout_seconds": 120,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return project, snapshot, VerificationConfig.load(config_path)


@pytest.mark.asyncio
async def test_verification_uses_only_host_configured_commands_and_binds_fingerprint(tmp_path: Path) -> None:
    project, snapshot, config = configured(tmp_path)
    runner = Runner([result()])
    service = CodeAgentVerificationService(
        config=config,
        sandbox_runner=runner,
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
    )

    record = await service.verify("attempt-1", snapshot)

    assert record.status == "verified"
    assert record.project_root == str(project.resolve())
    assert record.before_fingerprint == record.after_fingerprint
    assert len(record.before_fingerprint) == 64
    assert record.commands[0]["argv"] == [
        "C:\\managed\\python.exe",
        "-I",
        "-m",
        "pytest",
        "-q",
    ]
    assert runner.commands[0][1].cwd == snapshot.workspace_root


@pytest.mark.asyncio
async def test_verification_is_invalid_when_command_changes_snapshot(tmp_path: Path) -> None:
    _, snapshot, config = configured(tmp_path)
    runner = Runner([result()], mutate=snapshot.workspace_root / "app.py")
    service = CodeAgentVerificationService(
        config=config,
        sandbox_runner=runner,
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
    )

    record = await service.verify("attempt-1", snapshot)

    assert record.status == "unverified"
    assert record.reason == "snapshot_changed_during_verification"
    assert record.before_fingerprint != record.after_fingerprint


@pytest.mark.asyncio
async def test_verification_fingerprint_includes_generated_and_cache_directories(tmp_path: Path) -> None:
    _, snapshot, config = configured(tmp_path)
    generated = snapshot.workspace_root / "node_modules" / "generated.js"
    generated.parent.mkdir()
    runner = Runner([result()], mutate=generated)
    service = CodeAgentVerificationService(
        config=config,
        sandbox_runner=runner,
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
    )

    record = await service.verify("attempt-1", snapshot)

    assert record.status == "unverified"
    assert record.reason == "snapshot_changed_during_verification"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command_result", "reason"),
    [
        (result(exit_code=7), "command_failed"),
        (result(timed_out=True), "command_timed_out"),
        (result(cancelled=True), "command_cancelled"),
        (result(truncated=True), "evidence_incomplete"),
    ],
)
async def test_verification_fails_closed_for_incomplete_or_failed_evidence(
    tmp_path: Path, command_result: SandboxResult, reason: str
) -> None:
    _, snapshot, config = configured(tmp_path)
    service = CodeAgentVerificationService(
        config=config,
        sandbox_runner=Runner([command_result]),
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
    )

    record = await service.verify("attempt-1", snapshot)

    assert record.status == "unverified"
    assert record.reason == reason


@pytest.mark.asyncio
async def test_verification_without_saved_project_commands_is_not_run(tmp_path: Path) -> None:
    project, snapshot, _ = configured(tmp_path)
    empty = VerificationConfig(version=1, projects={})
    service = CodeAgentVerificationService(
        config=empty,
        sandbox_runner=Runner([]),
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
    )

    record = await service.verify("attempt-1", snapshot)

    assert record.status == "not_run"
    assert record.reason == "no_trusted_test_configuration"
    assert record.project_root == str(project.resolve())


def test_verification_config_rejects_unsafe_cwd_and_unknown_fields(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    path = tmp_path / "verification.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {
                        "project_root": str(project),
                        "commands": [
                            {
                                "runtime": "python",
                                "args": ["-m", "pytest"],
                                "cwd": "../outside",
                                "model_can_override": True,
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown|cwd"):
        VerificationConfig.load(path)


def test_verification_config_must_be_host_owned_outside_project_roots(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    path = project / "verification.json"
    path.write_text(json.dumps({"version": 1, "projects": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="host-owned"):
        VerificationConfig.load(path, forbidden_roots=[tmp_path / "projects"])
