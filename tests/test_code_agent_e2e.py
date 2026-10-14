from __future__ import annotations

import json
import shutil
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.actions import ActionGateway
from agent_core.legacy_code_agent.code_agent_contracts import CodeAgentBudget
from agent_core.legacy_code_agent.code_agent_loop import CodeAgentLoop
from agent_core.legacy_code_agent.code_agent_provider import CodeAgentProvider
from agent_core.legacy_code_agent.code_agent_tools import CodeAgentToolContext, make_code_agent_tools
from agent_core.code_agent_verification import (
    CodeAgentVerificationService,
    VerificationConfig,
)
from agent_core.contracts import ProviderResponse, Scope
from agent_core.sandbox_contracts import SandboxRuntimeConfig
from agent_core.tools import DefaultRiskPolicy, ToolRegistry
from agent_core.windows_sandbox import WindowsSandboxRunner
from agent_core.work_provider import WorkRequest
from agent_core.workspace_snapshot import WorkspaceSnapshotService
from agent_core.workspace_apply import (
    AutoApplyPolicy,
    CodeAgentAutoApplyCoordinator,
    WorkspaceApplyService,
)


class ScriptedModel:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        return ProviderResponse(message=self.responses.pop(0), usage={"total_tokens": 10})


class RecordingRetriever:
    def __init__(self) -> None:
        self.calls: list[tuple[Scope, str, int]] = []

    def retrieve(self, scope: Scope, query: str, *, token_budget: int):
        self.calls.append((scope, query, token_budget))
        return SimpleNamespace(
            hot=({"content": "Keep the original project untouched."},),
            warm=(),
            cold_evidence=(),
            citations=(),
            metadata={},
        )


def tool_call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


@pytest.fixture(scope="module")
def managed_runtime(tmp_path_factory: pytest.TempPathFactory) -> SandboxRuntimeConfig:
    root = tmp_path_factory.mktemp("code-agent-e2e-runtime")
    python_source = Path(sys.base_prefix).resolve()
    powershell_executable = shutil.which("pwsh")
    assert powershell_executable is not None, "PowerShell 7 is required"
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


@pytest.mark.asyncio
async def test_code_agent_edits_only_snapshot_and_verifies_in_real_appcontainer(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig
) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    source = project / "app.py"
    source.write_text("value = 1\n", encoding="utf-8")
    original = source.read_bytes()
    scope = Scope("alice", "web", "conversation-1", "work")
    retriever = RecordingRetriever()
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
    verify_script = r'''
import ctypes, json, pathlib, socket, sys
from ctypes import wintypes
assert pathlib.Path("app.py").read_text(encoding="utf-8") == "value = 2\n"
token = wintypes.HANDLE()
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi.OpenProcessToken.restype = wintypes.BOOL
advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi.GetTokenInformation.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
is_appcontainer = wintypes.DWORD()
size = wintypes.DWORD()
advapi.GetTokenInformation(token, 29, ctypes.byref(is_appcontainer), ctypes.sizeof(is_appcontainer), ctypes.byref(size))
connected = False
sock = socket.socket(); sock.settimeout(1)
try:
    sock.connect(("127.0.0.1", int(sys.argv[1]))); connected = True
except OSError:
    pass
finally:
    sock.close(); kernel.CloseHandle(token)
print(json.dumps({"is_appcontainer": bool(is_appcontainer.value), "connected": connected}))
'''
    model = ScriptedModel(
        [
            tool_call("read", "code__read", {"path": "app.py"}),
            tool_call("knowledge", "code__knowledge_search", {"query": "project safety"}),
            tool_call("write", "code__write", {"path": "app.py", "text": "value = 2\n"}),
            tool_call(
                "run",
                "code__run",
                {"runtime": "python", "args": ["-I", "-c", verify_script, str(port)]},
            ),
            {"role": "assistant", "content": "修改完成，测试通过。"},
        ]
    )
    runner = WindowsSandboxRunner(managed_runtime, state_root=tmp_path / "sandbox-state")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])

    def loop_factory(request: WorkRequest, snapshot):
        context = CodeAgentToolContext(
            attempt_id=request.attempt_id,
            snapshot=snapshot,
            sandbox_runner=runner,
            python_executable=managed_runtime.python_executable,
            powershell_executable=managed_runtime.powershell_executable,
            submit_scope=request.scope,
            memory_retriever=retriever,
        )
        tools = make_code_agent_tools(context)
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        gateway = ActionGateway.standalone(registry, risk_policy=DefaultRiskPolicy())
        return CodeAgentLoop(model, gateway, tools, CodeAgentBudget(max_steps=8))

    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=loop_factory,
        sandbox_runner=runner,
        artifact_dir=tmp_path / "artifacts",
    )
    request = WorkRequest(
        work_item_id="work-e2e",
        attempt_id="attempt-e2e",
        scope=scope,
        objective="change the value and run the test",
        task_type="code.agent",
        input={"project_root": str(project)},
    )
    try:
        result = await provider.execute(request, lambda event: None)
    finally:
        server.close()
        thread.join(timeout=3)

    assert result.status == "completed", result.error
    # 这个 provider 没接验证器：检查没跑过，状态是 not_run 而不是 unverified。
    assert result.output["verification_status"] == "not_run", result.evidence
    assert source.read_bytes() == original
    assert "-value = 1" in result.output["patch"]
    assert "+value = 2" in result.output["patch"]
    assert retriever.calls == [(scope, "project safety", 2000)]
    run_evidence = next(item for item in result.evidence if item["name"] == "code__run")
    payload = json.loads(run_evidence["output"]["stdout"])
    assert payload == {"is_appcontainer": True, "connected": False}
    assert accepted == [False]


@pytest.mark.asyncio
async def test_low_risk_change_is_applied_only_after_two_real_sandbox_verifications(
    tmp_path: Path, managed_runtime: SandboxRuntimeConfig
) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    source = project / "app.py"
    source.write_bytes(b"value = 1\n")
    verify_script = r'''
import ctypes, json, pathlib
from ctypes import wintypes
assert pathlib.Path("app.py").read_text(encoding="utf-8") == "value = 2\n"
token = wintypes.HANDLE()
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi.OpenProcessToken.restype = wintypes.BOOL
advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi.GetTokenInformation.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
value = wintypes.DWORD(); size = wintypes.DWORD()
advapi.GetTokenInformation(token, 29, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size))
kernel.CloseHandle(token)
print(json.dumps({"is_appcontainer": bool(value.value)}))
'''
    config_path = tmp_path / "verification.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {
                        "project_root": str(project),
                        "commands": [
                            {"runtime": "python", "args": ["-I", "-c", verify_script]}
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    model = ScriptedModel(
        [
            tool_call("write", "code__write", {"path": "app.py", "text": "value = 2\n"}),
            {"role": "assistant", "content": "修改完成，等待宿主验证。"},
        ]
    )
    runner = WindowsSandboxRunner(managed_runtime, state_root=tmp_path / "sandbox-state")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    verifier = CodeAgentVerificationService(
        config=VerificationConfig.load(config_path),
        sandbox_runner=runner,
        python_executable=managed_runtime.python_executable,
        powershell_executable=managed_runtime.powershell_executable,
    )
    coordinator = CodeAgentAutoApplyCoordinator(
        enabled=True,
        snapshot_service=snapshots,
        sandbox_runner=runner,
        verification_service=verifier,
        apply_service=WorkspaceApplyService(tmp_path / "apply-state"),
        policy=AutoApplyPolicy(),
    )

    def loop_factory(request: WorkRequest, snapshot):
        context = CodeAgentToolContext(
            attempt_id=request.attempt_id,
            snapshot=snapshot,
            sandbox_runner=runner,
            python_executable=managed_runtime.python_executable,
            powershell_executable=managed_runtime.powershell_executable,
            submit_scope=request.scope,
        )
        tools = make_code_agent_tools(context)
        registry = ToolRegistry()
        for tool in tools:
            registry.register(tool)
        return CodeAgentLoop(
            model,
            ActionGateway.standalone(registry, risk_policy=DefaultRiskPolicy()),
            tools,
            CodeAgentBudget(max_steps=4),
        )

    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=loop_factory,
        sandbox_runner=runner,
        artifact_dir=tmp_path / "artifacts",
        verification_service=verifier,
        auto_apply_coordinator=coordinator,
    )
    result = await provider.execute(
        WorkRequest(
            work_item_id="work-apply",
            attempt_id="attempt-apply",
            scope=Scope("alice", "web", "conversation-apply", "work"),
            objective="change the value",
            task_type="code.agent",
            input={"project_root": str(project)},
        ),
        lambda event: None,
    )

    assert result.status == "completed", (result.error, result.output, result.checkpoint)
    assert result.output["apply_status"] == "applied_verified", result.output
    assert source.read_bytes() == b"value = 2\n"
    initial = json.loads(result.output["verification"]["commands"][0]["stdout"])
    post = json.loads(result.output["apply"]["verification"]["commands"][0]["stdout"])
    assert initial == {"is_appcontainer": True}
    assert post == {"is_appcontainer": True}
