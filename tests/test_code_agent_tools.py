from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_core.actions import ActionGateway
from agent_core.legacy_code_agent.code_agent_tools import CodeAgentToolContext, make_code_agent_tools
from agent_core.contracts import ActionRequest, Scope
from agent_core.sandbox_contracts import SandboxResult
from agent_core.tools import DefaultRiskPolicy, ToolRegistry
from agent_core.workspace_snapshot import WorkspaceSnapshotService


class FakeSandbox:
    def __init__(self) -> None:
        self.commands = []

    async def run(self, attempt_id, command):
        self.commands.append((attempt_id, command))
        now = datetime.now(timezone.utc)
        return SandboxResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            started_at=now,
            finished_at=now,
            timed_out=False,
            cancelled=False,
            killed=False,
            output_truncated=False,
            termination_reason=None,
        )

    async def cancel(self, attempt_id):
        return True


def setup_gateway(tmp_path: Path):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshot = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]).create(
        "attempt-tools", project
    )
    sandbox = FakeSandbox()
    context = CodeAgentToolContext(
        attempt_id="attempt-tools",
        snapshot=snapshot,
        sandbox_runner=sandbox,
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
        search_executable=Path("C:/managed/rg.exe"),
    )
    registry = ToolRegistry()
    specs = make_code_agent_tools(context)
    for spec in specs:
        registry.register(spec)
    gateway = ActionGateway.standalone(registry, risk_policy=DefaultRiskPolicy())
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1")
    return snapshot, sandbox, specs, gateway, scope


async def invoke(gateway, scope, name, arguments, *, origin="code_agent"):
    return await gateway.invoke(
        ActionRequest(
            action_id=f"{name}-{len(str(arguments))}",
            name=name,
            arguments=arguments,
            scope=scope,
            origin=origin,
        )
    )


@pytest.mark.asyncio
async def test_file_tools_list_read_search_and_atomic_write(tmp_path: Path) -> None:
    snapshot, _, _, gateway, scope = setup_gateway(tmp_path)

    listing = await invoke(gateway, scope, "code__list", {"path": "src"})
    assert listing.status == "completed"
    assert listing.output["entries"][0]["path"] == "src/app.py"

    read = await invoke(gateway, scope, "code__read", {"path": "src/app.py"})
    assert read.output["text"].replace("\r\n", "\n") == "value = 1\n"

    search = await invoke(
        gateway, scope, "code__search", {"pattern": r"value\s*=\s*1", "path": "src"}
    )
    assert search.output["matches"][0]["path"] == "src/app.py"

    written = await invoke(
        gateway,
        scope,
        "code__write",
        {"path": "src/app.py", "text": "value = 2\n"},
    )
    assert written.status == "completed"
    assert (snapshot.workspace_root / "src" / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not list((snapshot.workspace_root / "src").glob("*.tmp"))


@pytest.mark.asyncio
async def test_apply_patch_changes_snapshot_only(tmp_path: Path) -> None:
    snapshot, _, _, gateway, scope = setup_gateway(tmp_path)
    patch = (
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 3\n"
    )

    result = await invoke(gateway, scope, "code__apply_patch", {"patch": patch})

    assert result.status == "completed"
    assert (snapshot.workspace_root / "src" / "app.py").read_text(encoding="utf-8") == "value = 3\n"
    assert (snapshot.project_root / "src" / "app.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_run_uses_registered_executable_and_snapshot_cwd(tmp_path: Path) -> None:
    snapshot, sandbox, _, gateway, scope = setup_gateway(tmp_path)

    result = await invoke(
        gateway,
        scope,
        "code__run",
        {"runtime": "python", "args": ["-c", "print('ok')"], "cwd": "src"},
    )

    assert result.status == "completed"
    _, command = sandbox.commands[0]
    assert command.argv[0] == "C:\\managed\\python.exe"
    assert command.cwd == snapshot.workspace_root / "src"
    assert result.output["exit_code"] == 0


def test_all_code_tools_are_internal_to_code_agent_origin(tmp_path: Path) -> None:
    _, _, specs, _, _ = setup_gateway(tmp_path)
    assert {spec.name for spec in specs} == {
        "code__list",
        "code__read",
        "code__search",
        "code__write",
        "code__apply_patch",
        "code__run",
    }
    assert all(spec.allowed_origins == frozenset({"code_agent"}) for spec in specs)
