"""Windows sandbox command gates and, later, the Attempt lifecycle runner."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from agent_core.sandbox_contracts import (
    SandboxCommand,
    SandboxLimits,
    SandboxResult,
    SandboxRuntimeConfig,
    SandboxUnavailableError,
    SandboxValidationError,
    _reject_reparse_points,
    _reject_special_path,
    _resolve_path,
    trusted_windows_directory,
    validate_runtime_config,
)
from agent_core.windows_sandbox_native import (
    NativeJob,
    NativeProcess,
    create_profile,
    create_job,
    create_suspended_process,
    delete_profile,
    derive_profile_sid,
    grant_runtime_access,
    grant_task_access,
    revoke_access,
)


_ALLOWED_TASK_ENVIRONMENT = frozenset({"CI", "LANG", "LC_ALL", "TZ"})
_RESERVED_ENVIRONMENT = frozenset(
    {
        "HOME",
        "LOCALAPPDATA",
        "PATH",
        "POWERSHELL_TELEMETRY_OPTOUT",
        "POWERSHELL_UPDATECHECK",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHOME",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
)


class _AttemptView(Protocol):
    attempt_id: str
    task_root: Path
    runtime_config: SandboxRuntimeConfig


@dataclass
class _AttemptState:
    attempt_id: str
    task_root: Path
    runtime_config: SandboxRuntimeConfig
    profile_name: str
    sid: str
    manifest_path: Path
    job_handle: NativeJob | None = None
    process: NativeProcess | None = None
    cancel_event: asyncio.Event | None = None
    running: bool = False


class _OutputCollector:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.truncated = False
        self._lock = threading.Lock()

    def read(self, stream: object, target: bytearray) -> None:
        while True:
            chunk = stream.read(65536)  # type: ignore[attr-defined]
            if not chunk:
                return
            with self._lock:
                remaining = self.limit - len(self.stdout) - len(self.stderr)
                if remaining > 0:
                    target.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True


def _has_parent_segment(path: Path) -> bool:
    return ".." in path.parts


def _final_directory_path(path: Path) -> Path:
    if os.name != "nt":
        return path.resolve(strict=True)

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    get_final_path.restype = ctypes.c_uint32
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    file_read_attributes = 0x0080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    backup_semantics = 0x02000000
    handle = create_file(
        str(path),
        file_read_attributes,
        share_all,
        None,
        open_existing,
        backup_semantics,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise SandboxValidationError(f"cannot open cwd: WinError {ctypes.get_last_error()}")
    try:
        required = get_final_path(handle, None, 0, 0)
        if required == 0:
            raise SandboxValidationError(
                f"cannot resolve cwd: WinError {ctypes.get_last_error()}"
            )
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise SandboxValidationError(
                f"cannot resolve cwd: WinError {ctypes.get_last_error()}"
            )
        final_path = buffer.value
    finally:
        close_handle(handle)

    if final_path.startswith("\\\\?\\UNC\\"):
        final_path = "\\\\" + final_path[8:]
    elif final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    return Path(final_path)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def validate_command(attempt: _AttemptView, command: SandboxCommand) -> SandboxCommand:
    """Validate paths and return the exact argv that may be launched without a shell."""

    executable_input = Path(command.argv[0])
    if _has_parent_segment(executable_input):
        raise SandboxValidationError("entry program must not contain parent segments")
    executable = _resolve_path(executable_input, "entry program", directory=False)

    registered = [
        _resolve_path(
            attempt.runtime_config.powershell_executable,
            "powershell_executable",
            directory=False,
        ),
        _resolve_path(
            attempt.runtime_config.python_executable,
            "python_executable",
            directory=False,
        ),
    ]
    if attempt.runtime_config.search_executable is not None:
        registered.append(
            _resolve_path(
                attempt.runtime_config.search_executable,
                "search_executable",
                directory=False,
            )
        )
    if not any(_same_path(executable, candidate) for candidate in registered):
        raise SandboxValidationError("entry program is not registered")

    cwd_input = Path(command.cwd)
    if _has_parent_segment(cwd_input):
        raise SandboxValidationError("cwd must not contain parent segments")
    _reject_special_path(cwd_input, "cwd")
    _reject_reparse_points(cwd_input, "cwd")
    cwd = _final_directory_path(cwd_input)

    task_root_input = Path(attempt.task_root)
    _reject_special_path(task_root_input, "task_root")
    _reject_reparse_points(task_root_input, "task_root")
    task_root = _final_directory_path(task_root_input)
    try:
        cwd.relative_to(task_root)
    except ValueError as exc:
        raise SandboxValidationError("cwd must be inside the Attempt task root") from exc

    arguments = command.argv[1:]
    powershell = registered[0]
    if _same_path(executable, powershell):
        arguments = ("-NoLogo", "-NoProfile", "-NonInteractive", *arguments)
    return replace(command, argv=(str(executable), *arguments), cwd=cwd)


def build_sandbox_environment(
    attempt: _AttemptView, command: SandboxCommand
) -> dict[str, str]:
    """Build a non-inheriting environment block rooted inside the Attempt directory."""

    caller_environment: dict[str, str] = {}
    for raw_name, value in command.env.items():
        if not isinstance(raw_name, str) or not isinstance(value, str):
            raise SandboxValidationError("environment names and values must be strings")
        name = raw_name.upper()
        if name in _RESERVED_ENVIRONMENT:
            raise SandboxValidationError(f"environment variable {raw_name} is reserved")
        if name not in _ALLOWED_TASK_ENVIRONMENT:
            raise SandboxValidationError(f"environment variable {raw_name} is not allowed")
        caller_environment[name] = value

    sandbox_root = Path(attempt.task_root) / ".sandbox"
    environment = {
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
    }
    environment.update(caller_environment)
    return environment


class WindowsSandboxRunner:
    """Own AppContainer identities and resources for sandbox Attempts."""

    def __init__(
        self,
        runtime_config: SandboxRuntimeConfig,
        *,
        state_root: Path,
        limits: SandboxLimits | None = None,
    ) -> None:
        if os.name != "nt":
            raise SandboxUnavailableError("Windows AppContainer isolation is not supported")
        self.runtime_config = validate_runtime_config(runtime_config)
        self.limits = limits or SandboxLimits()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._attempts: dict[str, _AttemptState] = {}

    @staticmethod
    def profile_name_for(attempt_id: str) -> str:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise SandboxValidationError("attempt_id must be a non-empty string")
        digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:32]
        return f"dm.sandbox.{digest}"

    def _manifest_path(self, profile_name: str) -> Path:
        return self.state_root / f"{profile_name}.json"

    def _write_manifest(self, state: _AttemptState) -> None:
        data = {
            "profile_name": state.profile_name,
            "sid": state.sid,
            "task_root": str(state.task_root),
            "runtime_root": str(state.runtime_config.runtime_root),
            "created_at": datetime.now(UTC).isoformat(),
        }
        temporary = state.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, state.manifest_path)

    def prepare_attempt(self, attempt_id: str, task_root: Path) -> None:
        if attempt_id in self._attempts:
            raise SandboxValidationError("Attempt is already prepared")
        profile_name = self.profile_name_for(attempt_id)
        manifest_path = self._manifest_path(profile_name)
        if manifest_path.exists():
            raise SandboxValidationError("Attempt already has persistent sandbox state")
        resolved_task_root = _resolve_path(Path(task_root), "task_root", directory=True)
        sandbox_root = resolved_task_root / ".sandbox"
        directories = (
            sandbox_root,
            sandbox_root / "temp",
            sandbox_root / "home",
            sandbox_root / "local-app-data",
        )
        created_directories: list[Path] = []
        sid: str | None = None
        try:
            for directory in directories:
                if not directory.exists():
                    directory.mkdir()
                    created_directories.append(directory)
            sid = create_profile(profile_name)
            state = _AttemptState(
                attempt_id=attempt_id,
                task_root=resolved_task_root,
                runtime_config=self.runtime_config,
                profile_name=profile_name,
                sid=sid,
                manifest_path=manifest_path,
            )
            self._write_manifest(state)
            grant_task_access(sid, resolved_task_root)
            grant_runtime_access(sid, self.runtime_config.runtime_root)
        except Exception:
            if sid is not None:
                for root in (resolved_task_root, self.runtime_config.runtime_root):
                    try:
                        revoke_access(sid, root)
                    except Exception:
                        pass
                try:
                    delete_profile(profile_name)
                except Exception:
                    pass
            manifest_path.unlink(missing_ok=True)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
        self._attempts[attempt_id] = state

    def _state_from_manifest(self, manifest_path: Path) -> _AttemptState:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if set(data) != {"profile_name", "sid", "task_root", "runtime_root", "created_at"}:
                raise ValueError("manifest fields do not match the schema")
            profile_name = str(data["profile_name"])
            if re.fullmatch(r"dm\.sandbox\.[0-9a-f]{32}", profile_name) is None:
                raise ValueError("invalid profile name")
            if manifest_path != self._manifest_path(profile_name):
                raise ValueError("manifest filename does not match profile")
            sid = str(data["sid"])
            if sid != derive_profile_sid(profile_name):
                raise ValueError("manifest SID does not match profile")
            task_root = Path(data["task_root"])
            _reject_special_path(task_root, "manifest task_root")
            _reject_reparse_points(task_root, "manifest task_root")
            runtime_root = Path(data["runtime_root"])
            if not _same_path(runtime_root, self.runtime_config.runtime_root):
                raise ValueError("manifest runtime root does not match configured runtime")
            datetime.fromisoformat(str(data["created_at"]))
            return _AttemptState(
                attempt_id="",
                task_root=task_root,
                runtime_config=self.runtime_config,
                profile_name=profile_name,
                sid=sid,
                manifest_path=manifest_path,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SandboxUnavailableError(f"invalid sandbox state manifest: {manifest_path}") from exc

    def _cleanup_state(self, state: _AttemptState) -> None:
        errors: list[Exception] = []
        if state.job_handle is not None:
            try:
                state.job_handle.close()
            except Exception as exc:
                errors.append(exc)
            state.job_handle = None
        for root in (state.task_root, state.runtime_config.runtime_root):
            try:
                revoke_access(state.sid, root)
            except Exception as exc:
                errors.append(exc)
        try:
            delete_profile(state.profile_name)
        except Exception as exc:
            errors.append(exc)
        if not errors:
            state.manifest_path.unlink(missing_ok=True)
        if errors:
            raise SandboxUnavailableError(
                "sandbox cleanup did not complete: " + "; ".join(str(error) for error in errors)
            )

    def cleanup_attempt(self, attempt_id: str) -> None:
        state = self._attempts.pop(attempt_id, None)
        if state is None:
            manifest_path = self._manifest_path(self.profile_name_for(attempt_id))
            if not manifest_path.exists():
                return
            state = self._state_from_manifest(manifest_path)
        self._cleanup_state(state)

    def cleanup_stale_attempts(self) -> None:
        active_manifests = {state.manifest_path for state in self._attempts.values()}
        for manifest_path in sorted(self.state_root.glob("dm.sandbox.*.json")):
            if manifest_path not in active_manifests:
                self._cleanup_state(self._state_from_manifest(manifest_path))

    @staticmethod
    def _task_disk_bytes(root: Path) -> int:
        try:
            _reject_reparse_points(root, "task root")
        except SandboxValidationError as exc:
            raise SandboxUnavailableError("task tree contains a reparse point") from exc
        total = 0
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            information = entry.stat(follow_symlinks=False)
                            reparse_attribute = getattr(
                                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400
                            )
                            if entry.is_symlink() or (
                                getattr(information, "st_file_attributes", 0)
                                & reparse_attribute
                            ):
                                raise SandboxUnavailableError(
                                    "task tree contains a reparse point"
                                )
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += information.st_size
                        except FileNotFoundError:
                            continue
            except FileNotFoundError:
                continue
        return total

    @staticmethod
    async def _wait_for_job_exit(
        process: NativeProcess, job: NativeJob, *, timeout_seconds: float = 5.0
    ) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            exit_code = process.poll()
            if exit_code is not None and job.active_processes() == 0:
                return exit_code
            await asyncio.sleep(0.01)
        raise SandboxUnavailableError("sandbox process did not terminate after Job shutdown")

    async def run(self, attempt_id: str, command: SandboxCommand) -> SandboxResult:
        state = self._attempts.get(attempt_id)
        if state is None:
            raise SandboxValidationError("Attempt has not been prepared")
        if state.running:
            raise SandboxValidationError("Attempt is already running a command")

        validated_command = validate_command(state, command)
        environment = build_sandbox_environment(state, validated_command)
        if (
            validated_command.timeout_seconds is not None
            and validated_command.timeout_seconds > self.limits.command_timeout_seconds
        ):
            raise SandboxValidationError("command timeout exceeds the configured limit")
        initial_disk_bytes = await asyncio.to_thread(self._task_disk_bytes, state.task_root)
        if initial_disk_bytes > self.limits.soft_disk_bytes:
            raise SandboxValidationError("task directory already exceeds the soft disk limit")
        timeout = validated_command.timeout_seconds or self.limits.command_timeout_seconds
        collector = _OutputCollector(self.limits.output_bytes)
        cancel_event = asyncio.Event()
        state.cancel_event = cancel_event
        state.running = True
        started_at = datetime.now(UTC)
        process: NativeProcess | None = None
        job: NativeJob | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        termination_reason: str | None = None
        timed_out = False
        cancelled = False
        killed = False
        exit_code: int | None = None
        try:
            job = create_job(
                active_process_limit=self.limits.active_process_limit,
                memory_bytes=self.limits.job_memory_bytes,
            )
            state.job_handle = job
            process = create_suspended_process(
                sid=state.sid,
                argv=validated_command.argv,
                cwd=validated_command.cwd,
                environment=environment,
            )
            state.process = process
            job.assign(process)
            process.resume()
            stdout_task = asyncio.create_task(
                asyncio.to_thread(collector.read, process.stdout, collector.stdout)
            )
            stderr_task = asyncio.create_task(
                asyncio.to_thread(collector.read, process.stderr, collector.stderr)
            )

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            next_disk_check = loop.time()
            while True:
                exit_code = process.poll()
                now = loop.time()
                if exit_code is not None:
                    break
                if cancel_event.is_set():
                    cancelled = True
                    killed = True
                    termination_reason = "cancelled"
                    job.terminate()
                    break
                if now >= deadline:
                    timed_out = True
                    killed = True
                    termination_reason = "timeout"
                    job.terminate()
                    break
                if now >= next_disk_check:
                    disk_bytes = await asyncio.to_thread(self._task_disk_bytes, state.task_root)
                    next_disk_check = now + self.limits.disk_poll_seconds
                    if disk_bytes > self.limits.soft_disk_bytes:
                        killed = True
                        termination_reason = "disk_limit"
                        job.terminate()
                        break
                await asyncio.sleep(min(0.05, self.limits.disk_poll_seconds))

            if killed:
                exit_code = await self._wait_for_job_exit(process, job)

            if stdout_task is not None and stderr_task is not None:
                await asyncio.gather(stdout_task, stderr_task)

            if termination_reason is None:
                disk_bytes = await asyncio.to_thread(self._task_disk_bytes, state.task_root)
                if disk_bytes > self.limits.soft_disk_bytes:
                    termination_reason = "disk_limit"
                elif exit_code not in (None, 0):
                    peak_memory = job.peak_memory()
                    if peak_memory >= int(self.limits.job_memory_bytes * 0.9):
                        termination_reason = "memory_limit"
                        killed = True
                    else:
                        termination_reason = "process_failed"

            return SandboxResult(
                exit_code=exit_code,
                stdout=collector.stdout.decode("utf-8", errors="replace"),
                stderr=collector.stderr.decode("utf-8", errors="replace"),
                started_at=started_at,
                finished_at=datetime.now(UTC),
                timed_out=timed_out,
                cancelled=cancelled,
                killed=killed,
                output_truncated=collector.truncated,
                termination_reason=termination_reason,
            )
        finally:
            state.running = False
            state.cancel_event = None
            state.process = None
            if process is not None and process.poll() is None:
                try:
                    if job is not None:
                        job.terminate()
                        await self._wait_for_job_exit(process, job)
                    else:
                        process.terminate()
                except Exception:
                    pass
            output_tasks = [
                task for task in (stdout_task, stderr_task) if task is not None
            ]
            if output_tasks:
                await asyncio.gather(*output_tasks, return_exceptions=True)
            if process is not None:
                process.close()
            if job is not None:
                job.close()
            state.job_handle = None

    async def cancel(self, attempt_id: str) -> bool:
        state = self._attempts.get(attempt_id)
        if state is None or not state.running or state.cancel_event is None:
            return False
        state.cancel_event.set()
        return True
