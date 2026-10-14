from __future__ import annotations

import asyncio
import json
import msvcrt
import os
import shutil
import socket
import sys
import threading
import winreg
from pathlib import Path
from types import SimpleNamespace

import pytest
import ntsecuritycon
import win32security

from agent_core.sandbox_contracts import (
    SandboxCommand,
    SandboxRuntimeConfig,
    SandboxUnavailableError,
    SandboxValidationError,
)
from agent_core.windows_sandbox import WindowsSandboxRunner
from agent_core.windows_sandbox_native import (
    create_profile,
    delete_profile,
    grant_runtime_access,
    grant_task_access,
    profile_exists,
    revoke_access,
)


def _fake_version_probe(argv: list[str], **_: object) -> SimpleNamespace:
    executable = Path(argv[0]).name.casefold()
    version = "PowerShell 7.6.5" if executable == "pwsh.exe" else "Python 3.12.0"
    return SimpleNamespace(returncode=0, stdout=version, stderr="")


def _create_fake_runtime(root: Path) -> SandboxRuntimeConfig:
    powershell = root / "powershell" / "pwsh.exe"
    python = root / "python" / "python.exe"
    powershell.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    powershell.touch()
    python.touch()
    return SandboxRuntimeConfig(root, powershell, python)


def _ace_count_for_sid(path: Path, sid_text: str) -> int:
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    return sum(
        1
        for index in range(dacl.GetAceCount())
        if win32security.ConvertSidToStringSid(dacl.GetAce(index)[2]) == sid_text
    )


def _acl_snapshot(path: Path) -> list[tuple[object, ...]]:
    descriptor = win32security.GetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    snapshot: list[tuple[object, ...]] = []
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        snapshot.append((*ace[:2], win32security.ConvertSidToStringSid(ace[2])))
    return snapshot


def _make_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WindowsSandboxRunner:
    monkeypatch.setattr("agent_core.sandbox_contracts.subprocess.run", _fake_version_probe)
    return WindowsSandboxRunner(
        _create_fake_runtime(tmp_path / "runtime"),
        state_root=tmp_path / "state",
    )


def test_prepare_and_cleanup_manage_real_profile_acl_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _make_runner(tmp_path, monkeypatch)
    task_root = tmp_path / "task"
    task_root.mkdir()
    attempt_id = "attempt / with user text"
    task_acl_before = _acl_snapshot(task_root)
    runtime_acl_before = _acl_snapshot(runner.runtime_config.runtime_root)

    runner.prepare_attempt(attempt_id, task_root)

    manifest_path = runner.state_root / f"{runner.profile_name_for(attempt_id)}.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["profile_name"] == runner.profile_name_for(attempt_id)
    assert attempt_id not in manifest["profile_name"]
    assert len(manifest["profile_name"].removeprefix("dm.sandbox.")) == 32
    assert manifest["task_root"] == str(task_root.resolve())
    assert manifest["runtime_root"] == str(runner.runtime_config.runtime_root)
    assert _ace_count_for_sid(task_root, manifest["sid"]) == 1
    assert _ace_count_for_sid(runner.runtime_config.runtime_root, manifest["sid"]) == 1
    assert profile_exists(manifest["profile_name"])

    runner.cleanup_attempt(attempt_id)

    assert not manifest_path.exists()
    assert _ace_count_for_sid(task_root, manifest["sid"]) == 0
    assert _ace_count_for_sid(runner.runtime_config.runtime_root, manifest["sid"]) == 0
    assert _acl_snapshot(task_root) == task_acl_before
    assert _acl_snapshot(runner.runtime_config.runtime_root) == runtime_acl_before
    assert not profile_exists(manifest["profile_name"])


def test_cleanup_stale_attempts_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_runner = _make_runner(tmp_path, monkeypatch)
    task_root = tmp_path / "task"
    task_root.mkdir()
    first_runner.prepare_attempt("stale-attempt", task_root)
    manifest_path = first_runner.state_root / f"{first_runner.profile_name_for('stale-attempt')}.json"
    sid_text = json.loads(manifest_path.read_text(encoding="utf-8"))["sid"]

    restarted_runner = WindowsSandboxRunner(
        first_runner.runtime_config,
        state_root=first_runner.state_root,
    )
    restarted_runner.cleanup_stale_attempts()
    restarted_runner.cleanup_stale_attempts()

    assert not manifest_path.exists()
    assert _ace_count_for_sid(task_root, sid_text) == 0


def test_prepare_failure_rolls_back_profile_acl_and_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _make_runner(tmp_path, monkeypatch)
    task_root = tmp_path / "task"
    task_root.mkdir()
    attempt_id = "rollback-attempt"
    captured_sid: list[str] = []

    original_grant_task_access = __import__(
        "agent_core.windows_sandbox", fromlist=["grant_task_access"]
    ).grant_task_access

    def capture_task_access(sid: str, root: Path) -> None:
        captured_sid.append(sid)
        original_grant_task_access(sid, root)

    monkeypatch.setattr("agent_core.windows_sandbox.grant_task_access", capture_task_access)
    monkeypatch.setattr(
        "agent_core.windows_sandbox.grant_runtime_access",
        lambda *_: (_ for _ in ()).throw(SandboxUnavailableError("policy denied ACL")),
    )

    with pytest.raises(SandboxUnavailableError, match="policy denied"):
        runner.prepare_attempt(attempt_id, task_root)

    assert captured_sid
    assert _ace_count_for_sid(task_root, captured_sid[0]) == 0
    assert not (task_root / ".sandbox").exists()
    assert not (runner.state_root / f"{runner.profile_name_for(attempt_id)}.json").exists()
    assert not profile_exists(runner.profile_name_for(attempt_id))


def test_acl_upgrade_does_not_accept_an_insufficient_existing_ace(tmp_path: Path) -> None:
    root = tmp_path / "acl-root"
    root.mkdir()
    profile_name = "dm.sandbox." + "a" * 32
    sid = create_profile(profile_name)
    try:
        grant_runtime_access(sid, root)
        grant_task_access(sid, root)
        descriptor = win32security.GetNamedSecurityInfo(
            str(root),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        assert dacl is not None
        masks = [
            dacl.GetAce(index)[1]
            for index in range(dacl.GetAceCount())
            if win32security.ConvertSidToStringSid(dacl.GetAce(index)[2]) == sid
        ]
        assert len(masks) == 1
        assert any(mask & ntsecuritycon.FILE_ALL_ACCESS == ntsecuritycon.FILE_ALL_ACCESS for mask in masks)
    finally:
        revoke_access(sid, root)
        delete_profile(profile_name)


def test_acl_grant_fails_closed_when_same_sid_has_a_deny_ace(tmp_path: Path) -> None:
    root = tmp_path / "acl-deny-root"
    root.mkdir()
    profile_name = "dm.sandbox." + "b" * 32
    sid = create_profile(profile_name)
    sid_object = win32security.ConvertStringSidToSid(sid)
    descriptor = win32security.GetNamedSecurityInfo(
        str(root),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    dacl.AddAccessDeniedAceEx(
        win32security.ACL_REVISION_DS,
        ntsecuritycon.CONTAINER_INHERIT_ACE | ntsecuritycon.OBJECT_INHERIT_ACE,
        ntsecuritycon.FILE_GENERIC_WRITE,
        sid_object,
    )
    win32security.SetNamedSecurityInfo(
        str(root),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        dacl,
        None,
    )
    try:
        with pytest.raises(SandboxUnavailableError, match="deny ACE"):
            grant_task_access(sid, root)
    finally:
        revoke_access(sid, root)
        delete_profile(profile_name)


def test_stale_cleanup_rejects_tampered_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _make_runner(tmp_path, monkeypatch)
    task_root = tmp_path / "task"
    task_root.mkdir()
    attempt_id = "tampered-manifest"
    runner.prepare_attempt(attempt_id, task_root)
    manifest_path = runner.state_root / f"{runner.profile_name_for(attempt_id)}.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["runtime_root"] = str(task_root)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    restarted = WindowsSandboxRunner(runner.runtime_config, state_root=runner.state_root)
    with pytest.raises(SandboxUnavailableError, match="manifest"):
        restarted.cleanup_stale_attempts()

    data["runtime_root"] = str(runner.runtime_config.runtime_root)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    restarted.cleanup_stale_attempts()


@pytest.fixture(scope="module")
def managed_runtime(tmp_path_factory: pytest.TempPathFactory) -> SandboxRuntimeConfig:
    root = tmp_path_factory.mktemp("managed-sandbox-runtime")
    python_source = Path(sys.executable).resolve().parent
    powershell_executable = shutil.which("pwsh")
    assert powershell_executable is not None, "PowerShell 7 is required for Windows sandbox tests"
    powershell_source = Path(powershell_executable).resolve().parent
    python_target = root / "python"
    powershell_target = root / "powershell"
    python_target.mkdir()
    for source in python_source.iterdir():
        if source.is_file() and source.suffix.casefold() in {".exe", ".dll"}:
            shutil.copy2(source, python_target / source.name)
    shutil.copytree(
        python_source / "Lib",
        python_target / "Lib",
        ignore=shutil.ignore_patterns("site-packages", "__pycache__", "*.pyc"),
    )
    shutil.copytree(python_source / "DLLs", python_target / "DLLs")
    shutil.copytree(powershell_source, powershell_target)
    return SandboxRuntimeConfig(
        root,
        powershell_target / "pwsh.exe",
        python_target / "python.exe",
    )


def _real_runner(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig, attempt_id: str
) -> tuple[WindowsSandboxRunner, Path]:
    runner = WindowsSandboxRunner(managed_runtime, state_root=tmp_path / "state")
    task_root = tmp_path / "task"
    task_root.mkdir()
    runner.prepare_attempt(attempt_id, task_root)
    return runner, task_root


@pytest.mark.asyncio
async def test_real_appcontainer_token_and_file_boundary(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig
) -> None:
    runner, task_root = _real_runner(tmp_path, managed_runtime, "real-boundary")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("host secret", encoding="utf-8")
    script = r'''
import ctypes, json, pathlib, sys
from ctypes import wintypes

task = pathlib.Path(sys.argv[1])
outside = pathlib.Path(sys.argv[2])
(task / "inside.txt").write_text("inside", encoding="utf-8")
try:
    outside.read_text(encoding="utf-8")
    outside_read = True
except OSError:
    outside_read = False
try:
    outside.write_text("changed", encoding="utf-8")
    outside_write = True
except OSError:
    outside_write = False

token = wintypes.HANDLE()
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi.OpenProcessToken.restype = wintypes.BOOL
advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi.GetTokenInformation.restype = wintypes.BOOL
advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
kernel.LocalFree.argtypes = [ctypes.c_void_p]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
is_appcontainer = wintypes.DWORD()
size = wintypes.DWORD()
advapi.GetTokenInformation(token, 29, ctypes.byref(is_appcontainer), ctypes.sizeof(is_appcontainer), ctypes.byref(size))
advapi.GetTokenInformation(token, 31, None, 0, ctypes.byref(size))
buffer = ctypes.create_string_buffer(size.value)
advapi.GetTokenInformation(token, 31, buffer, size, ctypes.byref(size))
sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
sid_text_pointer = ctypes.c_void_p()
advapi.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_text_pointer))
sid = ctypes.wstring_at(sid_text_pointer.value)
kernel.LocalFree(sid_text_pointer)
kernel.CloseHandle(token)
print(json.dumps({"outside_read": outside_read, "outside_write": outside_write, "is_appcontainer": bool(is_appcontainer.value), "sid": sid}))
'''
    try:
        result = await runner.run(
            "real-boundary",
            SandboxCommand(
                (
                    str(runner.runtime_config.python_executable),
                    "-I",
                    "-c",
                    script,
                    str(task_root),
                    str(outside),
                ),
                task_root,
            ),
        )
        manifest = json.loads(
            (runner.state_root / f"{runner.profile_name_for('real-boundary')}.json").read_text(
                encoding="utf-8"
            )
        )
    finally:
        runner.cleanup_attempt("real-boundary")

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert (task_root / "inside.txt").read_text(encoding="utf-8") == "inside"
    assert outside.read_text(encoding="utf-8") == "host secret"
    assert payload == {
        "outside_read": False,
        "outside_write": False,
        "is_appcontainer": True,
        "sid": manifest["sid"],
    }


@pytest.mark.asyncio
async def test_real_python_and_powershell_cannot_create_children(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig
) -> None:
    runner, task_root = _real_runner(tmp_path, managed_runtime, "no-children")
    python_script = (
        "import subprocess,sys; "
        "\ntry: subprocess.run([sys.executable,'-c','print(1)'],check=True); print('SPAWNED')"
        "\nexcept OSError: print('BLOCKED')"
    )
    powershell_script = (
        "$ErrorActionPreference='Stop'; try { "
        "Start-Process -FilePath $env:SystemRoot\\System32\\cmd.exe "
        "-ArgumentList '/c','echo child' -Wait; 'SPAWNED' } catch { 'BLOCKED' }"
    )
    try:
        python_result = await runner.run(
            "no-children",
            SandboxCommand(
                (str(runner.runtime_config.python_executable), "-I", "-c", python_script),
                task_root,
            ),
        )
        powershell_result = await runner.run(
            "no-children",
            SandboxCommand(
                (
                    str(runner.runtime_config.powershell_executable),
                    "-Command",
                    powershell_script,
                ),
                task_root,
            ),
        )
    finally:
        runner.cleanup_attempt("no-children")

    assert python_result.stdout.strip() == "BLOCKED", python_result.stderr
    assert powershell_result.stdout.strip() == "BLOCKED", powershell_result.stderr


@pytest.mark.asyncio
async def test_real_appcontainer_has_no_loopback_or_host_identity_leaks(
    tmp_path: Path,
    managed_runtime: SandboxRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, task_root = _real_runner(tmp_path, managed_runtime, "network-and-identity")
    monkeypatch.setenv("HOST_SECRET", "must-not-leak")
    monkeypatch.setenv("HTTPS_PROXY", "http://host-proxy.invalid")
    registry_path = r"Software\dm-agent-sandbox-test"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, registry_path) as key:
        winreg.SetValueEx(key, "HostMarker", 0, winreg.REG_SZ, "must-not-leak")

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    accepted: list[bool] = []

    def accept_once() -> None:
        try:
            connection, _ = server.accept()
        except OSError:
            accepted.append(False)
        else:
            accepted.append(True)
            connection.close()

    thread = threading.Thread(target=accept_once)
    thread.start()
    script = r'''
import json, os, socket, sys, winreg
connected = False
sock = socket.socket()
sock.settimeout(1)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1])))
    connected = True
except OSError:
    pass
finally:
    sock.close()
registry_visible = False
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\dm-agent-sandbox-test") as key:
        registry_visible = winreg.QueryValueEx(key, "HostMarker")[0] == "must-not-leak"
except OSError:
    pass
print(json.dumps({
    "connected": connected,
    "host_secret": os.environ.get("HOST_SECRET"),
    "proxy": os.environ.get("HTTPS_PROXY"),
    "profile": os.environ.get("USERPROFILE"),
    "registry_visible": registry_visible,
}))
'''
    try:
        result = await runner.run(
            "network-and-identity",
            SandboxCommand(
                (str(runner.runtime_config.python_executable), "-I", "-c", script, str(port)),
                task_root,
            ),
        )
    finally:
        runner.cleanup_attempt("network-and-identity")
        server.close()
        thread.join(timeout=3)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_path)

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["connected"] is False
    assert accepted == [False]
    assert payload["host_secret"] is None
    assert payload["proxy"] is None
    assert Path(payload["profile"]).is_relative_to(task_root)
    assert payload["registry_visible"] is False


@pytest.mark.asyncio
async def test_handle_allowlist_excludes_other_inheritable_host_handles(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig
) -> None:
    runner, task_root = _real_runner(tmp_path, managed_runtime, "handle-allowlist")
    outside = tmp_path / "host-handle-secret.txt"
    outside.write_text("must-not-be-readable", encoding="utf-8")
    descriptor = os.open(outside, os.O_RDONLY | os.O_BINARY)
    host_handle = msvcrt.get_osfhandle(descriptor)
    os.set_handle_inheritable(host_handle, True)
    script = r'''
import ctypes, json, sys
from ctypes import wintypes
flags = wintypes.DWORD()
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetHandleInformation.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel.GetHandleInformation.restype = wintypes.BOOL
try:
    visible = bool(kernel.GetHandleInformation(wintypes.HANDLE(int(sys.argv[1])), ctypes.byref(flags)))
except OSError:
    visible = False
print(json.dumps({"visible": visible}))
'''
    try:
        result = await runner.run(
            "handle-allowlist",
            SandboxCommand(
                (
                    str(runner.runtime_config.python_executable),
                    "-I",
                    "-c",
                    script,
                    str(host_handle),
                ),
                task_root,
            ),
        )
    finally:
        os.close(descriptor)
        runner.cleanup_attempt("handle-allowlist")

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == {"visible": False}


@pytest.mark.asyncio
async def test_isolation_setup_failure_never_runs_an_ordinary_process(
    tmp_path: Path,
    managed_runtime: SandboxRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, task_root = _real_runner(tmp_path, managed_runtime, "fail-closed")
    marker = task_root / "must-not-exist.txt"
    monkeypatch.setattr(
        "agent_core.windows_sandbox.create_job",
        lambda **_: (_ for _ in ()).throw(SandboxUnavailableError("job policy unavailable")),
    )
    try:
        with pytest.raises(SandboxUnavailableError, match="job policy unavailable"):
            await runner.run(
                "fail-closed",
                SandboxCommand(
                    (
                        str(runner.runtime_config.python_executable),
                        "-I",
                        "-c",
                        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')",
                    ),
                    task_root,
                ),
            )
    finally:
        runner.cleanup_attempt("fail-closed")

    assert not marker.exists()


@pytest.mark.asyncio
async def test_real_runner_rejects_unregistered_and_reparse_paths(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig
) -> None:
    runner, task_root = _real_runner(tmp_path, managed_runtime, "path-gates")
    linked = task_root / "linked"
    real = task_root / "real"
    real.mkdir()
    completed = __import__("subprocess").run(
        [
            str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"),
            "/d",
            "/c",
            "mklink",
            "/J",
            str(linked),
            str(real),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    system_cmd = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
    try:
        with pytest.raises(SandboxValidationError, match="registered"):
            await runner.run(
                "path-gates",
                SandboxCommand((str(system_cmd), "/c", "echo unsafe"), task_root),
            )
        with pytest.raises(SandboxValidationError, match="reparse"):
            await runner.run(
                "path-gates",
                SandboxCommand((str(runner.runtime_config.python_executable), "-c", "pass"), linked),
            )
        with pytest.raises(SandboxUnavailableError, match="reparse"):
            await runner.run(
                "path-gates",
                SandboxCommand(
                    (str(runner.runtime_config.python_executable), "-c", "pass"),
                    task_root,
                ),
            )
        for unsafe in (Path(r"\\server\share"), Path(r"\\?\C:\Windows")):
            with pytest.raises(SandboxValidationError, match="UNC|device"):
                await runner.run(
                    "path-gates",
                    SandboxCommand(
                        (str(runner.runtime_config.python_executable), "-c", "pass"),
                        unsafe,
                    ),
                )
    finally:
        runner.cleanup_attempt("path-gates")
