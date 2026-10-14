"""Small fail-closed Win32 wrappers used by the Windows sandbox runner."""

from __future__ import annotations

import ctypes
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping, Sequence

from agent_core.sandbox_contracts import SandboxUnavailableError


if os.name == "nt":
    import msvcrt
    import ntsecuritycon
    import win32security
    import winreg


_ERROR_ALREADY_EXISTS_HRESULT = 0x800700B7
_NOT_FOUND_HRESULTS = {0x80070002, 0x80070490}


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


class _SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", ctypes.c_uint32),
        ("Reserved", ctypes.c_uint32),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


@dataclass
class NativeProcess:
    process_handle: int
    thread_handle: int
    process_id: int
    stdout: BinaryIO
    stderr: BinaryIO

    def poll(self) -> int | None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        wait.restype = ctypes.c_uint32
        result = wait(self.process_handle, 0)
        if result == 0x00000102:
            return None
        if result != 0:
            raise SandboxUnavailableError(
                f"cannot wait for sandbox process: WinError {ctypes.get_last_error()}"
            )
        exit_code = ctypes.c_uint32()
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        get_exit_code.restype = ctypes.c_int
        if not get_exit_code(self.process_handle, ctypes.byref(exit_code)):
            raise SandboxUnavailableError(
                f"cannot read sandbox exit code: WinError {ctypes.get_last_error()}"
            )
        return int(exit_code.value)

    def resume(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        resume = kernel32.ResumeThread
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_uint32
        if resume(self.thread_handle) == 0xFFFFFFFF:
            raise SandboxUnavailableError(
                f"cannot resume sandbox process: WinError {ctypes.get_last_error()}"
            )

    def terminate(self, exit_code: int = 1) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        terminate = kernel32.TerminateProcess
        terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        terminate.restype = ctypes.c_int
        if not terminate(self.process_handle, exit_code):
            raise SandboxUnavailableError(
                f"cannot terminate sandbox process: WinError {ctypes.get_last_error()}"
            )

    def close(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        for stream in (self.stdout, self.stderr):
            try:
                stream.close()
            except OSError:
                pass
        for handle in (self.thread_handle, self.process_handle):
            if handle:
                close(handle)


@dataclass
class NativeJob:
    handle: int
    memory_limit: int

    def assign(self, process: NativeProcess) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        assign = kernel32.AssignProcessToJobObject
        assign.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        assign.restype = ctypes.c_int
        if not assign(self.handle, process.process_handle):
            process.terminate()
            raise SandboxUnavailableError(
                f"cannot assign sandbox process to Job Object: WinError {ctypes.get_last_error()}"
            )

    def terminate(self, exit_code: int = 1) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        terminate = kernel32.TerminateJobObject
        terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        terminate.restype = ctypes.c_int
        if not terminate(self.handle, exit_code):
            raise SandboxUnavailableError(
                f"cannot terminate sandbox Job Object: WinError {ctypes.get_last_error()}"
            )

    def active_processes(self) -> int:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel32.QueryInformationJobObject
        query.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        query.restype = ctypes.c_int
        information = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not query(
            self.handle,
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            raise SandboxUnavailableError(
                f"cannot query sandbox Job Object: WinError {ctypes.get_last_error()}"
            )
        return int(information.ActiveProcesses)

    def peak_memory(self) -> int:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel32.QueryInformationJobObject
        query.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        query.restype = ctypes.c_int
        information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not query(
            self.handle,
            9,
            ctypes.byref(information),
            ctypes.sizeof(information),
            None,
        ):
            return 0
        return int(information.PeakJobMemoryUsed)

    def close(self) -> None:
        if not self.handle:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close = kernel32.CloseHandle
        close.argtypes = [ctypes.c_void_p]
        close.restype = ctypes.c_int
        close(self.handle)
        self.handle = 0


def _require_windows() -> None:
    if os.name != "nt":
        raise SandboxUnavailableError("Windows AppContainer isolation is not supported")


def _hresult_code(value: int) -> int:
    return value & 0xFFFFFFFF


def _sid_pointer_to_text(pointer: ctypes.c_void_p) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    convert.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p
    text_pointer = ctypes.c_void_p()
    if not convert(pointer, ctypes.byref(text_pointer)):
        raise SandboxUnavailableError(
            f"cannot convert AppContainer SID: WinError {ctypes.get_last_error()}"
        )
    try:
        return ctypes.wstring_at(text_pointer.value)
    finally:
        local_free(text_pointer)


def _derive_profile_sid(name: str) -> str:
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    derive = userenv.DeriveAppContainerSidFromAppContainerName
    derive.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
    derive.restype = ctypes.c_long
    free_sid = advapi32.FreeSid
    free_sid.argtypes = [ctypes.c_void_p]
    free_sid.restype = ctypes.c_void_p
    sid_pointer = ctypes.c_void_p()
    result = derive(name, ctypes.byref(sid_pointer))
    if result < 0:
        raise SandboxUnavailableError(
            f"cannot derive AppContainer SID: HRESULT 0x{_hresult_code(result):08X}"
        )
    try:
        return _sid_pointer_to_text(sid_pointer)
    finally:
        free_sid(sid_pointer)


def derive_profile_sid(name: str) -> str:
    """Derive the deterministic SID for an AppContainer profile name."""

    _require_windows()
    return _derive_profile_sid(name)


def create_profile(name: str) -> str:
    """Create an AppContainer profile with zero capabilities and return its SID."""

    _require_windows()
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    create = userenv.CreateAppContainerProfile
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create.restype = ctypes.c_long
    free_sid = advapi32.FreeSid
    free_sid.argtypes = [ctypes.c_void_p]
    free_sid.restype = ctypes.c_void_p
    sid_pointer = ctypes.c_void_p()
    result = create(name, name, "DM code agent sandbox", None, 0, ctypes.byref(sid_pointer))
    code = _hresult_code(result)
    if result < 0:
        if code == _ERROR_ALREADY_EXISTS_HRESULT:
            return _derive_profile_sid(name)
        raise SandboxUnavailableError(
            f"cannot create AppContainer profile: HRESULT 0x{code:08X}"
        )
    try:
        return _sid_pointer_to_text(sid_pointer)
    finally:
        free_sid(sid_pointer)


def delete_profile(name: str) -> bool:
    """Ask Windows to delete one AppContainer profile."""

    _require_windows()
    userenv = ctypes.WinDLL("userenv", use_last_error=True)
    delete = userenv.DeleteAppContainerProfile
    delete.argtypes = [ctypes.c_wchar_p]
    delete.restype = ctypes.c_long
    result = delete(name)
    code = _hresult_code(result)
    if result >= 0:
        return True
    if code in _NOT_FOUND_HRESULTS:
        return False
    raise SandboxUnavailableError(
        f"cannot delete AppContainer profile: HRESULT 0x{code:08X}"
    )


def profile_exists(name: str) -> bool:
    """Return whether Windows still has a profile mapping for this AppContainer."""

    _require_windows()
    sid = _derive_profile_sid(name)
    mapping_path = (
        r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion"
        rf"\AppContainer\Mappings\{sid}"
    )
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, mapping_path):
            return True
    except FileNotFoundError:
        return False


def _sid_text(ace: tuple[object, ...]) -> str | None:
    try:
        return win32security.ConvertSidToStringSid(ace[2])
    except (IndexError, TypeError):
        return None


def _grant_access(sid: str, root: Path, access_mask: int) -> None:
    _require_windows()
    try:
        descriptor = win32security.GetNamedSecurityInfo(
            str(root),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise SandboxUnavailableError("refusing to modify a null DACL")
        inheritance = ntsecuritycon.CONTAINER_INHERIT_ACE | ntsecuritycon.OBJECT_INHERIT_ACE
        matching_allowed_indexes: list[int] = []
        allowed_types = {
            win32security.ACCESS_ALLOWED_ACE_TYPE,
            getattr(win32security, "ACCESS_ALLOWED_OBJECT_ACE_TYPE", 5),
        }
        denied_types = {
            win32security.ACCESS_DENIED_ACE_TYPE,
            getattr(win32security, "ACCESS_DENIED_OBJECT_ACE_TYPE", 6),
        }
        for index in range(dacl.GetAceCount()):
            ace = dacl.GetAce(index)
            if _sid_text(ace) != sid:
                continue
            ace_type, ace_flags = ace[0]
            if ace_type in denied_types:
                raise SandboxUnavailableError("refusing to override an existing deny ACE")
            if ace_flags & 0x10:
                raise SandboxUnavailableError("refusing to replace an inherited sandbox ACE")
            if ace_type not in allowed_types:
                raise SandboxUnavailableError("unsupported existing ACE for sandbox SID")
            matching_allowed_indexes.append(index)
        for index in reversed(matching_allowed_indexes):
            dacl.DeleteAce(index)
        sid_object = win32security.ConvertStringSidToSid(sid)
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            inheritance,
            access_mask,
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
    except SandboxUnavailableError:
        raise
    except Exception as exc:
        raise SandboxUnavailableError(f"cannot grant sandbox ACL on {root}: {exc}") from exc


def grant_task_access(sid: str, root: Path) -> None:
    _grant_access(sid, root, ntsecuritycon.FILE_ALL_ACCESS)


def grant_runtime_access(sid: str, root: Path) -> None:
    _grant_access(
        sid,
        root,
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE,
    )


def revoke_access(sid: str, root: Path) -> None:
    """Remove only ACEs belonging to this Attempt SID, preserving the full existing DACL."""

    _require_windows()
    try:
        if not Path(root).exists():
            return
        descriptor = win32security.GetNamedSecurityInfo(
            str(root),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        if dacl is None:
            raise SandboxUnavailableError("refusing to modify a null DACL")
        changed = False
        for index in range(dacl.GetAceCount() - 1, -1, -1):
            if _sid_text(dacl.GetAce(index)) == sid:
                dacl.DeleteAce(index)
                changed = True
        if changed:
            win32security.SetNamedSecurityInfo(
                str(root),
                win32security.SE_FILE_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
                None,
                None,
                dacl,
                None,
            )
    except SandboxUnavailableError:
        raise
    except Exception as exc:
        raise SandboxUnavailableError(f"cannot revoke sandbox ACL on {root}: {exc}") from exc


def create_job(*, active_process_limit: int, memory_bytes: int) -> NativeJob:
    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateJobObjectW
    create.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    handle = create(None, None)
    if not handle:
        raise SandboxUnavailableError(
            f"cannot create Job Object: WinError {ctypes.get_last_error()}"
        )
    information = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    information.BasicLimitInformation.LimitFlags = 0x00002000 | 0x00000008 | 0x00000200
    information.BasicLimitInformation.ActiveProcessLimit = active_process_limit
    information.JobMemoryLimit = memory_bytes
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    set_information.restype = ctypes.c_int
    if not set_information(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise SandboxUnavailableError(f"cannot configure Job Object: WinError {error}")
    return NativeJob(int(handle), memory_bytes)


def _create_pipe() -> tuple[int, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_pipe = kernel32.CreatePipe
    create_pipe.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        ctypes.c_uint32,
    ]
    create_pipe.restype = ctypes.c_int
    attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, 1)
    read_handle = ctypes.c_void_p()
    write_handle = ctypes.c_void_p()
    if not create_pipe(
        ctypes.byref(read_handle), ctypes.byref(write_handle), ctypes.byref(attributes), 0
    ):
        raise SandboxUnavailableError(
            f"cannot create output pipe: WinError {ctypes.get_last_error()}"
        )
    set_handle_information = kernel32.SetHandleInformation
    set_handle_information.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    set_handle_information.restype = ctypes.c_int
    if not set_handle_information(read_handle, 0x00000001, 0):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(read_handle)
        kernel32.CloseHandle(write_handle)
        raise SandboxUnavailableError(f"cannot protect output pipe: WinError {error}")
    return int(read_handle.value), int(write_handle.value)


def _open_inheritable_null() -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    attributes = _SECURITY_ATTRIBUTES(ctypes.sizeof(_SECURITY_ATTRIBUTES), None, 1)
    handle = create_file("NUL", 0x80000000, 0x00000001 | 0x00000002, ctypes.byref(attributes), 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise SandboxUnavailableError(
            f"cannot open sandbox stdin: WinError {ctypes.get_last_error()}"
        )
    return int(handle)


def _environment_block(environment: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    entries = [f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].casefold())]
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")


def create_suspended_process(
    *,
    sid: str,
    argv: Sequence[str],
    cwd: Path,
    environment: Mapping[str, str],
) -> NativeProcess:
    """Create an AppContainer process suspended with only standard pipe handles inherited."""

    _require_windows()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    stdout_read = stdout_write = stderr_read = stderr_write = stdin_handle = 0
    attribute_list = None
    sid_pointer = ctypes.c_void_p()
    process_info = _PROCESS_INFORMATION()
    process_created = False
    stdout_fd = -1
    stderr_fd = -1
    stdout_stream: BinaryIO | None = None
    stderr_stream: BinaryIO | None = None
    try:
        stdout_read, stdout_write = _create_pipe()
        stderr_read, stderr_write = _create_pipe()
        stdin_handle = _open_inheritable_null()

        convert_sid = advapi32.ConvertStringSidToSidW
        convert_sid.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p)]
        convert_sid.restype = ctypes.c_int
        if not convert_sid(sid, ctypes.byref(sid_pointer)):
            raise SandboxUnavailableError(
                f"cannot parse AppContainer SID: WinError {ctypes.get_last_error()}"
            )

        initialize = kernel32.InitializeProcThreadAttributeList
        initialize.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_size_t)]
        initialize.restype = ctypes.c_int
        attribute_bytes = ctypes.c_size_t()
        initialize(None, 2, 0, ctypes.byref(attribute_bytes))
        attribute_list = ctypes.create_string_buffer(attribute_bytes.value)
        if not initialize(attribute_list, 2, 0, ctypes.byref(attribute_bytes)):
            raise SandboxUnavailableError(
                f"cannot initialize process attributes: WinError {ctypes.get_last_error()}"
            )

        update = kernel32.UpdateProcThreadAttribute
        update.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        update.restype = ctypes.c_int
        capabilities = _SECURITY_CAPABILITIES(sid_pointer, None, 0, 0)
        if not update(
            attribute_list,
            0,
            0x00020009,
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            None,
            None,
        ):
            raise SandboxUnavailableError(
                f"cannot set AppContainer process attribute: WinError {ctypes.get_last_error()}"
            )
        inherited_handles = (ctypes.c_void_p * 3)(stdin_handle, stdout_write, stderr_write)
        if not update(
            attribute_list,
            0,
            0x00020002,
            inherited_handles,
            ctypes.sizeof(inherited_handles),
            None,
            None,
        ):
            raise SandboxUnavailableError(
                f"cannot set inherited-handle allowlist: WinError {ctypes.get_last_error()}"
            )

        startup = _STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = 0x00000100
        startup.StartupInfo.hStdInput = stdin_handle
        startup.StartupInfo.hStdOutput = stdout_write
        startup.StartupInfo.hStdError = stderr_write
        startup.lpAttributeList = ctypes.cast(attribute_list, ctypes.c_void_p)
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
        environment_block = _environment_block(environment)
        create_process = kernel32.CreateProcessW
        create_process.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(_STARTUPINFOEXW),
            ctypes.POINTER(_PROCESS_INFORMATION),
        ]
        create_process.restype = ctypes.c_int
        flags = 0x00000004 | 0x00080000 | 0x00000400 | 0x08000000
        if not create_process(
            str(argv[0]),
            command_line,
            None,
            None,
            1,
            flags,
            environment_block,
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            raise SandboxUnavailableError(
                f"cannot create AppContainer process: WinError {ctypes.get_last_error()}"
            )
        process_created = True
        kernel32.CloseHandle(stdout_write)
        stdout_write = 0
        kernel32.CloseHandle(stderr_write)
        stderr_write = 0
        kernel32.CloseHandle(stdin_handle)
        stdin_handle = 0
        stdout_fd = msvcrt.open_osfhandle(stdout_read, os.O_RDONLY | os.O_BINARY)
        stdout_read = 0
        stderr_fd = msvcrt.open_osfhandle(stderr_read, os.O_RDONLY | os.O_BINARY)
        stderr_read = 0
        stdout_stream = os.fdopen(stdout_fd, "rb", buffering=0)
        stdout_fd = -1
        stderr_stream = os.fdopen(stderr_fd, "rb", buffering=0)
        stderr_fd = -1
        return NativeProcess(
            int(process_info.hProcess),
            int(process_info.hThread),
            int(process_info.dwProcessId),
            stdout_stream,
            stderr_stream,
        )
    except Exception:
        for stream in (stdout_stream, stderr_stream):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for descriptor in (stdout_fd, stderr_fd):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if process_created:
            kernel32.TerminateProcess(process_info.hProcess, 1)
            kernel32.CloseHandle(process_info.hThread)
            kernel32.CloseHandle(process_info.hProcess)
        raise
    finally:
        if attribute_list is not None:
            delete_attributes = kernel32.DeleteProcThreadAttributeList
            delete_attributes.argtypes = [ctypes.c_void_p]
            delete_attributes(attribute_list)
        if sid_pointer.value:
            kernel32.LocalFree(sid_pointer)
        for handle in (stdout_read, stdout_write, stderr_read, stderr_write, stdin_handle):
            if handle:
                kernel32.CloseHandle(handle)
