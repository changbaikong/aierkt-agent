"""Public contracts and fail-closed configuration checks for the Windows sandbox."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import ctypes
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Mapping


class SandboxValidationError(ValueError):
    """The caller supplied a sandbox configuration or command that is unsafe."""


class SandboxUnavailableError(RuntimeError):
    """The requested Windows isolation mechanism is unavailable."""


_TERMINATION_REASONS = frozenset(
    {
        "timeout",
        "cancelled",
        "output_limit",
        "memory_limit",
        "disk_limit",
        "process_failed",
    }
)


def _require_positive(name: str, value: int | float) -> None:
    if isinstance(value, bool) or value <= 0:
        raise SandboxValidationError(f"{name} must be positive")


@dataclass(frozen=True)
class SandboxRuntimeConfig:
    runtime_root: Path
    powershell_executable: Path
    python_executable: Path
    search_executable: Path | None = None


@dataclass(frozen=True)
class SandboxLimits:
    command_timeout_seconds: int = 300
    job_memory_bytes: int = 4 * 1024**3
    output_bytes: int = 1024**2
    soft_disk_bytes: int = 2 * 1024**3
    disk_poll_seconds: float = 0.5
    active_process_limit: int = 1

    def __post_init__(self) -> None:
        for name in (
            "command_timeout_seconds",
            "job_memory_bytes",
            "output_bytes",
            "soft_disk_bytes",
            "disk_poll_seconds",
            "active_process_limit",
        ):
            _require_positive(name, getattr(self, name))
        if self.active_process_limit != 1:
            raise SandboxValidationError("active_process_limit must be 1 during phase 1")


@dataclass(frozen=True)
class SandboxCommand:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.argv, str) or not isinstance(self.argv, tuple) or not self.argv:
            raise SandboxValidationError("argv must be a non-empty tuple")
        if any(not isinstance(argument, str) or not argument for argument in self.argv):
            raise SandboxValidationError("argv entries must be non-empty strings")
        if self.timeout_seconds is not None:
            _require_positive("timeout_seconds", self.timeout_seconds)


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: datetime
    finished_at: datetime
    timed_out: bool
    cancelled: bool
    killed: bool
    output_truncated: bool
    termination_reason: str | None

    def __post_init__(self) -> None:
        if self.termination_reason not in _TERMINATION_REASONS | {None}:
            raise SandboxValidationError("termination_reason is not a supported value")


def _reject_special_path(path: Path, field_name: str) -> None:
    raw = os.fspath(path)
    normalized = raw.replace("/", "\\")
    if normalized.startswith("\\\\"):
        raise SandboxValidationError(f"{field_name} must not use UNC or device paths")
    if not path.is_absolute():
        raise SandboxValidationError(f"{field_name} must be an absolute path")


def _existing_components(path: Path) -> list[Path]:
    current = path
    components: list[Path] = []
    while True:
        if current.exists() or current.is_symlink():
            components.append(current)
        if current.parent == current:
            break
        current = current.parent
    return components


def _reject_reparse_points(path: Path, field_name: str) -> None:
    for component in _existing_components(path):
        try:
            info = component.lstat()
        except OSError as exc:
            raise SandboxValidationError(f"cannot inspect {field_name}: {exc}") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
        if component.is_symlink() or attributes & reparse_attribute:
            raise SandboxValidationError(f"{field_name} contains a reparse point")


def _resolve_path(path: Path, field_name: str, *, directory: bool) -> Path:
    path = Path(path)
    _reject_special_path(path, field_name)
    _reject_reparse_points(path, field_name)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SandboxValidationError(f"{field_name} does not exist") from exc
    if directory and not resolved.is_dir():
        raise SandboxValidationError(f"{field_name} must be a directory")
    if not directory and not resolved.is_file():
        raise SandboxValidationError(f"{field_name} must be a file")
    return resolved


def _require_inside(root: Path, path: Path, field_name: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SandboxValidationError(f"{field_name} must be inside runtime_root") from exc


def _probe_version(executable: Path, expected: re.Pattern[str], label: str) -> None:
    environment = {"SystemRoot": str(trusted_windows_directory())}
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=environment,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SandboxValidationError(f"unable to probe {label}") from exc
    output = "\n".join((completed.stdout or "", completed.stderr or "")).strip()
    if completed.returncode != 0 or expected.search(output) is None:
        raise SandboxValidationError(f"{label} has an unsupported version")


def trusted_windows_directory() -> Path:
    """Resolve the real Windows directory without trusting caller-controlled environment data."""

    if os.name != "nt":
        raise SandboxUnavailableError("Windows system directory is unavailable on this platform")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_windows_directory = kernel32.GetWindowsDirectoryW
    get_windows_directory.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    get_windows_directory.restype = ctypes.c_uint32
    size = 260
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        written = get_windows_directory(buffer, size)
        if written == 0:
            raise SandboxUnavailableError(
                f"cannot resolve Windows directory: WinError {ctypes.get_last_error()}"
            )
        if written < size:
            return _resolve_path(Path(buffer.value), "Windows directory", directory=True)
        size = written + 1


def validate_runtime_config(config: SandboxRuntimeConfig) -> SandboxRuntimeConfig:
    """Return a resolved runtime configuration after all fail-closed checks pass."""

    root = _resolve_path(config.runtime_root, "runtime_root", directory=True)
    powershell = _resolve_path(
        config.powershell_executable, "powershell_executable", directory=False
    )
    python = _resolve_path(config.python_executable, "python_executable", directory=False)
    search = (
        _resolve_path(config.search_executable, "search_executable", directory=False)
        if config.search_executable is not None
        else None
    )

    for field_name, executable in (
        ("powershell_executable", powershell),
        ("python_executable", python),
        ("search_executable", search),
    ):
        if executable is not None:
            _require_inside(root, executable, field_name)

    _probe_version(powershell, re.compile(r"\bPowerShell\s+7(?:\.|\b)"), "PowerShell 7")
    _probe_version(python, re.compile(r"\bPython\s+3\.12(?:\.|\b)"), "Python 3.12")
    return replace(
        config,
        runtime_root=root,
        powershell_executable=powershell,
        python_executable=python,
        search_executable=search,
    )
