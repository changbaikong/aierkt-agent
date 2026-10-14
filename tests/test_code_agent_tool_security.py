from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.actions import ActionGateway
from agent_core.legacy_code_agent.code_agent_tools import CodeAgentToolContext, make_code_agent_tools
from agent_core.contracts import ActionRequest, Scope
from agent_core.tools import DefaultRiskPolicy, ToolRegistry
from agent_core.workspace_snapshot import WorkspaceSnapshotService


class NeverSandbox:
    async def run(self, attempt_id, command):
        raise AssertionError("unsafe command reached sandbox")

    async def cancel(self, attempt_id):
        return True


def make_gateway(tmp_path: Path):
    allowed = tmp_path / "allowed"
    project = allowed / "repo"
    project.mkdir(parents=True)
    (project / "safe.txt").write_text("safe", encoding="utf-8")
    snapshot = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]).create("attempt", project)
    context = CodeAgentToolContext(
        attempt_id="attempt",
        snapshot=snapshot,
        sandbox_runner=NeverSandbox(),
        python_executable=Path("C:/managed/python.exe"),
        powershell_executable=Path("C:/managed/pwsh.exe"),
        search_executable=None,
    )
    registry = ToolRegistry()
    for spec in make_code_agent_tools(context):
        registry.register(spec)
    return snapshot, ActionGateway.standalone(registry, risk_policy=DefaultRiskPolicy())


async def call(gateway, name, arguments, origin="code_agent"):
    return await gateway.invoke(
        ActionRequest(
            action_id=f"{name}-{repr(arguments)}",
            name=name,
            arguments=arguments,
            scope=Scope(owner_id="alice", channel="web", conversation_id="chat"),
            origin=origin,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["../outside.txt", "C:/outside.txt", "\\\\server\\share\\x"])
async def test_file_tools_reject_paths_outside_snapshot(tmp_path: Path, unsafe: str) -> None:
    _, gateway = make_gateway(tmp_path)
    result = await call(gateway, "code__read", {"path": unsafe})
    assert result.status == "failed"
    assert result.error_kind in {"permission", "permanent"}


@pytest.mark.asyncio
async def test_wrong_origin_is_rejected_before_file_access(tmp_path: Path) -> None:
    _, gateway = make_gateway(tmp_path)
    result = await call(gateway, "code__read", {"path": "safe.txt"}, origin="chat_model")
    assert result.status == "rejected"
    assert result.error == "origin_not_allowed"


@pytest.mark.asyncio
async def test_file_tools_reject_symlink_added_to_snapshot(tmp_path: Path) -> None:
    snapshot, gateway = make_gateway(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = snapshot.workspace_root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("creating a symlink requires unavailable privileges")
    result = await call(gateway, "code__read", {"path": "link.txt"})
    assert result.status == "failed"
    assert "reparse" in result.error.lower()


@pytest.mark.asyncio
async def test_run_rejects_unknown_runtime_and_parent_cwd(tmp_path: Path) -> None:
    _, gateway = make_gateway(tmp_path)
    unknown = await call(
        gateway, "code__run", {"runtime": "cmd", "args": ["/c", "dir"], "cwd": "."}
    )
    assert unknown.status == "rejected"

    escaped = await call(
        gateway, "code__run", {"runtime": "python", "args": [], "cwd": ".."}
    )
    assert escaped.status == "failed"
