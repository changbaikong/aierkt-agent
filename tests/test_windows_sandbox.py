from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.sandbox_contracts import (
    SandboxCommand,
    SandboxLimits,
    SandboxRuntimeConfig,
    SandboxValidationError,
)
from agent_core.windows_sandbox import WindowsSandboxRunner


def _fake_version_probe(argv: list[str], **_: object) -> SimpleNamespace:
    executable = Path(argv[0]).name.casefold()
    version = "PowerShell 7.6.5" if executable == "pwsh.exe" else "Python 3.12.0"
    return SimpleNamespace(returncode=0, stdout=version, stderr="")


def _runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    limits: SandboxLimits | None = None,
    executable_source: Path | None = None,
) -> tuple[WindowsSandboxRunner, Path, Path]:
    monkeypatch.setattr("agent_core.sandbox_contracts.subprocess.run", _fake_version_probe)
    runtime_root = tmp_path / "runtime"
    powershell = runtime_root / "powershell" / "pwsh.exe"
    python = runtime_root / "python" / "python.exe"
    powershell.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    cmd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    shutil.copy2(cmd, powershell)
    shutil.copy2(executable_source or cmd, python)
    runner = WindowsSandboxRunner(
        SandboxRuntimeConfig(runtime_root, powershell, python),
        state_root=tmp_path / "state",
        limits=limits,
    )
    task_root = tmp_path / "task"
    task_root.mkdir()
    runner.prepare_attempt("attempt-1", task_root)
    return runner, task_root, python


@pytest.mark.asyncio
async def test_run_returns_stdout_and_nonzero_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task_root, executable = _runner(tmp_path, monkeypatch)
    try:
        success = await runner.run(
            "attempt-1",
            SandboxCommand((str(executable), "/d", "/c", "echo hello"), task_root),
        )
        failure = await runner.run(
            "attempt-1",
            SandboxCommand((str(executable), "/d", "/c", "echo failure 1>&2 & exit /b 7"), task_root),
        )
    finally:
        runner.cleanup_attempt("attempt-1")

    assert success.exit_code == 0
    assert success.stdout.strip() == "hello"
    assert success.stderr == ""
    assert success.termination_reason is None
    assert failure.exit_code == 7
    assert "failure" in failure.stderr
    assert failure.termination_reason == "process_failed"


@pytest.mark.asyncio
async def test_run_kills_command_at_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task_root, executable = _runner(tmp_path, monkeypatch)
    try:
        result = await runner.run(
            "attempt-1",
            SandboxCommand(
                (str(executable), "/d", "/c", "for /L %i in (1,1,1000000000) do @rem"),
                task_root,
                timeout_seconds=1,
            ),
        )
    finally:
        runner.cleanup_attempt("attempt-1")

    assert result.timed_out
    assert result.killed
    assert result.termination_reason == "timeout"


@pytest.mark.asyncio
async def test_command_cannot_raise_the_configured_timeout_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task_root, executable = _runner(
        tmp_path,
        monkeypatch,
        limits=SandboxLimits(command_timeout_seconds=1),
    )
    try:
        with pytest.raises(SandboxValidationError, match="timeout"):
            await runner.run(
                "attempt-1",
                SandboxCommand(
                    (str(executable), "/d", "/c", "echo should-not-run"),
                    task_root,
                    timeout_seconds=2,
                ),
            )
    finally:
        runner.cleanup_attempt("attempt-1")


@pytest.mark.asyncio
async def test_cancel_stops_a_running_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task_root, executable = _runner(tmp_path, monkeypatch)
    task = asyncio.create_task(
        runner.run(
            "attempt-1",
            SandboxCommand(
                (str(executable), "/d", "/c", "for /L %i in (1,1,1000000000) do @rem"),
                task_root,
            ),
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert await runner.cancel("attempt-1")
        result = await task
    finally:
        runner.cleanup_attempt("attempt-1")

    assert result.cancelled
    assert result.killed
    assert result.termination_reason == "cancelled"
    assert not await runner.cancel("attempt-1")


@pytest.mark.asyncio
async def test_output_is_truncated_but_fully_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = SandboxLimits(output_bytes=128)
    runner, task_root, executable = _runner(tmp_path, monkeypatch, limits=limits)
    try:
        result = await runner.run(
            "attempt-1",
            SandboxCommand(
                (str(executable), "/d", "/c", "for /L %i in (1,1,100) do @echo 1234567890"),
                task_root,
            ),
        )
    finally:
        runner.cleanup_attempt("attempt-1")

    assert result.exit_code == 0
    assert result.output_truncated
    assert len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")) <= 128


@pytest.mark.asyncio
async def test_disk_soft_limit_terminates_or_rejects_oversized_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limits = SandboxLimits(soft_disk_bytes=1024, disk_poll_seconds=0.05)
    runner, task_root, executable = _runner(tmp_path, monkeypatch, limits=limits)
    try:
        result = await runner.run(
            "attempt-1",
            SandboxCommand(
                (
                    str(executable),
                    "/d",
                    "/c",
                    "for /L %i in (1,1,100000) do @echo 12345678901234567890>>large.txt",
                ),
                task_root,
            ),
        )
    finally:
        runner.cleanup_attempt("attempt-1")

    assert result.killed
    assert result.termination_reason == "disk_limit"


@pytest.mark.asyncio
async def test_job_memory_limit_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    windows_powershell = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    limits = SandboxLimits(job_memory_bytes=64 * 1024**2)
    runner, task_root, executable = _runner(
        tmp_path,
        monkeypatch,
        limits=limits,
        executable_source=windows_powershell,
    )
    try:
        result = await runner.run(
            "attempt-1",
            SandboxCommand(
                (
                    str(executable),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$x=[Collections.Generic.List[byte[]]]::new(); while($true){$x.Add([byte[]]::new(1048576))}",
                ),
                task_root,
            ),
        )
    finally:
        runner.cleanup_attempt("attempt-1")

    assert result.killed
    assert result.termination_reason == "memory_limit"


@pytest.mark.asyncio
async def test_same_attempt_cannot_run_twice_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, task_root, executable = _runner(tmp_path, monkeypatch)
    first = asyncio.create_task(
        runner.run(
            "attempt-1",
            SandboxCommand(
                (str(executable), "/d", "/c", "for /L %i in (1,1,1000000000) do @rem"),
                task_root,
            ),
        )
    )
    try:
        await asyncio.sleep(0.1)
        with pytest.raises(SandboxValidationError, match="already running"):
            await runner.run(
                "attempt-1",
                SandboxCommand((str(executable), "/d", "/c", "echo second"), task_root),
            )
        await runner.cancel("attempt-1")
        await first
    finally:
        runner.cleanup_attempt("attempt-1")
