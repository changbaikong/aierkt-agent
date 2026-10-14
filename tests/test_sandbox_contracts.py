from __future__ import annotations

import dataclasses
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.sandbox_contracts import (
    SandboxCommand,
    SandboxLimits,
    SandboxResult,
    SandboxRuntimeConfig,
    SandboxValidationError,
    trusted_windows_directory,
    validate_runtime_config,
)
from agent_core.windows_sandbox import build_sandbox_environment, validate_command


def _runtime_config(root: Path) -> SandboxRuntimeConfig:
    return SandboxRuntimeConfig(
        runtime_root=root,
        powershell_executable=root / "powershell" / "pwsh.exe",
        python_executable=root / "python" / "python.exe",
        search_executable=None,
    )


def _create_runtime_files(root: Path) -> SandboxRuntimeConfig:
    config = _runtime_config(root)
    config.powershell_executable.parent.mkdir(parents=True)
    config.python_executable.parent.mkdir(parents=True)
    config.powershell_executable.touch()
    config.python_executable.touch()
    return config


def _successful_version_probe(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> SimpleNamespace:
        calls.append(argv)
        executable = Path(argv[0]).name.casefold()
        version = "PowerShell 7.6.5" if executable == "pwsh.exe" else "Python 3.12.0"
        return SimpleNamespace(returncode=0, stdout=version, stderr="")

    monkeypatch.setattr("agent_core.sandbox_contracts.subprocess.run", fake_run)
    return calls


def test_limits_use_the_phase_one_security_defaults() -> None:
    limits = SandboxLimits()

    assert limits == SandboxLimits(
        command_timeout_seconds=300,
        job_memory_bytes=4 * 1024**3,
        output_bytes=1024**2,
        soft_disk_bytes=2 * 1024**3,
        disk_poll_seconds=0.5,
        active_process_limit=1,
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SandboxRuntimeConfig(Path("runtime"), Path("pwsh"), Path("python")),
        SandboxLimits,
        lambda: SandboxCommand(("python",), Path(".")),
    ],
)
def test_public_input_contracts_are_frozen(factory: object) -> None:
    value = factory()  # type: ignore[operator]

    with pytest.raises(dataclasses.FrozenInstanceError):
        value.runtime_root = Path("changed")  # type: ignore[attr-defined,misc]


def test_sandbox_result_is_frozen() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    result = SandboxResult(None, "", "", now, now, False, False, False, False, None)

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.exit_code = 0  # type: ignore[misc]


@pytest.mark.parametrize("argv", [(), "python.exe"])
def test_command_rejects_empty_or_string_argv(argv: object, tmp_path: Path) -> None:
    with pytest.raises(SandboxValidationError, match="argv"):
        SandboxCommand(argv=argv, cwd=tmp_path)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0, -1])
def test_command_rejects_non_positive_timeout(timeout: int, tmp_path: Path) -> None:
    with pytest.raises(SandboxValidationError, match="timeout"):
        SandboxCommand(("python.exe",), tmp_path, timeout_seconds=timeout)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command_timeout_seconds", 0),
        ("job_memory_bytes", 0),
        ("output_bytes", 0),
        ("soft_disk_bytes", 0),
        ("disk_poll_seconds", 0),
        ("active_process_limit", 0),
    ],
)
def test_limits_reject_non_positive_values(field: str, value: int) -> None:
    values = dataclasses.asdict(SandboxLimits())
    values[field] = value

    with pytest.raises(SandboxValidationError, match=field):
        SandboxLimits(**values)


def test_runtime_validation_rejects_missing_executable(tmp_path: Path) -> None:
    config = _runtime_config(tmp_path / "runtime")
    config.runtime_root.mkdir()

    with pytest.raises(SandboxValidationError, match="powershell_executable"):
        validate_runtime_config(config)


def test_runtime_validation_rejects_executable_outside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    config = _create_runtime_files(runtime_root)
    outside = tmp_path / "outside.exe"
    outside.touch()
    config = dataclasses.replace(config, search_executable=outside)
    _successful_version_probe(monkeypatch)

    with pytest.raises(SandboxValidationError, match="search_executable"):
        validate_runtime_config(config)


@pytest.mark.parametrize(
    ("executable_name", "reported_version", "expected_field"),
    [
        ("pwsh.exe", "PowerShell 5.1.0", "PowerShell 7"),
        ("python.exe", "Python 3.11.9", "Python 3.12"),
    ],
)
def test_runtime_validation_rejects_wrong_runtime_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executable_name: str,
    reported_version: str,
    expected_field: str,
) -> None:
    config = _create_runtime_files(tmp_path / "runtime")

    def fake_run(argv: list[str], **_: object) -> SimpleNamespace:
        output = reported_version if Path(argv[0]).name.casefold() == executable_name else (
            "PowerShell 7.6.5" if executable_name == "python.exe" else "Python 3.12.0"
        )
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr("agent_core.sandbox_contracts.subprocess.run", fake_run)

    with pytest.raises(SandboxValidationError, match=expected_field):
        validate_runtime_config(config)


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse point behavior")
def test_runtime_validation_rejects_reparse_point_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_root = tmp_path / "real-runtime"
    config = _create_runtime_files(real_root)
    linked_root = tmp_path / "linked-runtime"
    try:
        linked_root.symlink_to(real_root, target_is_directory=True)
    except OSError:
        completed = subprocess.run(
            [
                str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(linked_root),
                str(real_root),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    linked_config = _runtime_config(linked_root)
    _successful_version_probe(monkeypatch)

    with pytest.raises(SandboxValidationError, match="reparse"):
        validate_runtime_config(linked_config)


def test_runtime_validation_probes_only_validated_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _create_runtime_files(tmp_path / "runtime")
    calls = _successful_version_probe(monkeypatch)

    validated = validate_runtime_config(config)

    assert validated.runtime_root == config.runtime_root.resolve(strict=True)
    assert validated.powershell_executable == config.powershell_executable.resolve(strict=True)
    assert validated.python_executable == config.python_executable.resolve(strict=True)
    assert calls == [
        [str(validated.powershell_executable), "--version"],
        [str(validated.python_executable), "--version"],
    ]


def _command_attempt(tmp_path: Path) -> SimpleNamespace:
    task_root = tmp_path / "task"
    task_root.mkdir()
    runtime_root = tmp_path / "runtime"
    config = _create_runtime_files(runtime_root)
    return SimpleNamespace(
        attempt_id="attempt-1",
        task_root=task_root.resolve(),
        runtime_config=config,
    )


def test_command_validation_rejects_unregistered_entry_program(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    unregistered = tmp_path / "other.exe"
    unregistered.touch()
    command = SandboxCommand((str(unregistered),), attempt.task_root)

    with pytest.raises(SandboxValidationError, match="registered"):
        validate_command(attempt, command)


def test_command_validation_rejects_cwd_outside_task_root(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        tmp_path,
    )

    with pytest.raises(SandboxValidationError, match="cwd"):
        validate_command(attempt, command)


def test_command_validation_rejects_lexical_parent_segments(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    child = attempt.task_root / "child"
    child.mkdir()
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        child / "..",
    )

    with pytest.raises(SandboxValidationError, match="parent"):
        validate_command(attempt, command)


@pytest.mark.parametrize("cwd", [r"\\server\share", r"\\?\C:\Windows"])
def test_command_validation_rejects_unc_and_device_cwd(tmp_path: Path, cwd: str) -> None:
    attempt = _command_attempt(tmp_path)
    command = SandboxCommand((str(attempt.runtime_config.python_executable),), Path(cwd))

    with pytest.raises(SandboxValidationError, match="UNC|device"):
        validate_command(attempt, command)


def test_command_validation_rejects_reparse_point_cwd(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    real_directory = attempt.task_root / "real"
    real_directory.mkdir()
    linked_directory = attempt.task_root / "linked"
    completed = subprocess.run(
        [
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(linked_directory),
            str(real_directory),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        linked_directory,
    )

    with pytest.raises(SandboxValidationError, match="reparse"):
        validate_command(attempt, command)


@pytest.mark.parametrize(
    "name",
    ["PATH", "path", "TEMP", "LOCALAPPDATA", "PYTHONPATH", "PYTHONHOME", "USERPROFILE"],
)
def test_environment_rejects_reserved_overrides(tmp_path: Path, name: str) -> None:
    attempt = _command_attempt(tmp_path)
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        attempt.task_root,
        env={name: "caller-value"},
    )

    with pytest.raises(SandboxValidationError, match="environment"):
        build_sandbox_environment(attempt, command)


def test_environment_rejects_variables_outside_allowlist(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        attempt.task_root,
        env={"SECRET_TOKEN": "do-not-copy"},
    )

    with pytest.raises(SandboxValidationError, match="SECRET_TOKEN"):
        build_sandbox_environment(attempt, command)


def test_environment_contains_only_fixed_and_allowed_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _command_attempt(tmp_path)
    monkeypatch.setenv("HOST_SECRET", "hidden")
    monkeypatch.setenv("HTTP_PROXY", "http://host-proxy")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\host")
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        attempt.task_root,
        env={"LANG": "zh_CN.UTF-8"},
    )

    environment = build_sandbox_environment(attempt, command)

    sandbox_root = attempt.task_root / ".sandbox"
    assert environment == {
        "TEMP": str(sandbox_root / "temp"),
        "TMP": str(sandbox_root / "temp"),
        "HOME": str(sandbox_root / "home"),
        "USERPROFILE": str(sandbox_root / "home"),
        "LOCALAPPDATA": str(sandbox_root / "local-app-data"),
        "SystemRoot": str(trusted_windows_directory()),
        "PATH": "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
        "POWERSHELL_UPDATECHECK": "Off",
        "LANG": "zh_CN.UTF-8",
    }
    assert Path(environment["LOCALAPPDATA"]).is_relative_to(attempt.task_root)


def test_environment_does_not_trust_a_spoofed_systemroot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempt = _command_attempt(tmp_path)
    spoofed = tmp_path / "fake-windows"
    spoofed.mkdir()
    monkeypatch.setenv("SystemRoot", str(spoofed))
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable),),
        attempt.task_root,
    )

    environment = build_sandbox_environment(attempt, command)

    assert Path(environment["SystemRoot"]) != spoofed
    assert Path(environment["SystemRoot"]).is_dir()


def test_command_validation_adds_noninteractive_powershell_flags(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    command = SandboxCommand(
        (str(attempt.runtime_config.powershell_executable), "-Command", "Get-Location"),
        attempt.task_root,
    )

    validated = validate_command(attempt, command)

    assert validated.argv == (
        str(attempt.runtime_config.powershell_executable.resolve()),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-Location",
    )


def test_command_validation_preserves_registered_python_arguments(tmp_path: Path) -> None:
    attempt = _command_attempt(tmp_path)
    command = SandboxCommand(
        (str(attempt.runtime_config.python_executable), "-c", "print('ok')"),
        attempt.task_root,
    )

    validated = validate_command(attempt, command)

    assert validated.argv == (
        str(attempt.runtime_config.python_executable.resolve()),
        "-c",
        "print('ok')",
    )
    assert validated.cwd == attempt.task_root
