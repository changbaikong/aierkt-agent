from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from agent_core.actions import ActionGateway
from agent_core.legacy_code_agent.code_agent_tools import CodeAgentToolContext, make_code_agent_tools
from agent_core.contracts import ActionRequest, Scope
from agent_core.tools import DefaultRiskPolicy, ToolRegistry
from agent_core.workspace_snapshot import WorkspaceSnapshotService


@dataclass
class Retrieval:
    hot: list
    warm: list
    cold_evidence: list
    citations: list
    metadata: dict


class RecordingRetriever:
    def __init__(self) -> None:
        self.calls = []

    def retrieve(self, scope, query, *, token_budget=None):
        self.calls.append((scope, query, token_budget))
        return Retrieval(
            hot=[{"id": "h1", "content": "recent"}],
            warm=[{"id": "w1", "content": "known"}],
            cold_evidence=[],
            citations=[{"id": "w1"}],
            metadata={"estimated_tokens": 5},
        )


def make_gateway(tmp_path: Path):
    allowed = tmp_path / "allowed"
    project = allowed / "repo"
    project.mkdir(parents=True)
    snapshot = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]).create("a", project)
    original_scope = Scope(
        owner_id="alice", channel="web", conversation_id="original", module_id="companion"
    )
    retriever = RecordingRetriever()
    context = CodeAgentToolContext(
        attempt_id="a",
        snapshot=snapshot,
        sandbox_runner=object(),
        python_executable=Path("C:/python.exe"),
        powershell_executable=Path("C:/pwsh.exe"),
        submit_scope=original_scope,
        memory_retriever=retriever,
    )
    registry = ToolRegistry()
    for spec in make_code_agent_tools(context):
        registry.register(spec)
    return (
        ActionGateway.standalone(registry, risk_policy=DefaultRiskPolicy()),
        retriever,
        original_scope,
    )


@pytest.mark.asyncio
async def test_knowledge_search_uses_original_submission_scope(tmp_path: Path) -> None:
    gateway, retriever, original_scope = make_gateway(tmp_path)
    caller_scope = Scope(owner_id="mallory", channel="cli", conversation_id="other")
    result = await gateway.invoke(
        ActionRequest(
            action_id="knowledge-1",
            name="code__knowledge_search",
            arguments={"query": "previous decision", "token_budget": 100},
            scope=caller_scope,
            origin="code_agent",
        )
    )

    assert result.status == "completed"
    assert retriever.calls == [(original_scope, "previous decision", 100)]
    assert result.output["warm"][0]["content"] == "known"
    assert result.output["citations"] == [{"id": "w1"}]


def test_knowledge_schema_does_not_accept_scope_fields(tmp_path: Path) -> None:
    gateway, _, _ = make_gateway(tmp_path)
    spec = gateway.registry.resolve("code__knowledge_search")
    properties = spec.schema["function"]["parameters"]["properties"]
    assert not {"owner_id", "channel", "conversation_id", "module_id"} & set(properties)

