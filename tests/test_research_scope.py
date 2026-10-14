from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from agent_core.contracts import EFFECT_READ, ProviderResponse, Scope, ToolSpec
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.tools import ToolRegistry
from agent_tools.research_scope import (
    ResearchScope,
    ResearchScopeDraft,
    SQLiteResearchScopeStore,
    build_scope_question,
    parse_scope_confirmation,
    scope_requires_confirmation,
)


def test_vague_query_requires_scope_confirmation():
    # 普通问题直接查（范围由系统决定），只有重度研究才追问。
    assert scope_requires_confirmation("查一下人工智能最近的发展") is False


def test_heavy_research_without_scope_dimensions_still_asks():
    assert scope_requires_confirmation("对AI Agent技术路线的深入研究") is True


def test_explicit_query_does_not_require_scope_confirmation():
    assert scope_requires_confirmation("查近一个月中文技术社区对 Agent Reach 的讨论") is False


def test_scope_question_is_single_actionable_question():
    question = build_scope_question("人工智能最近的发展")
    assert "范围" in question
    assert question.endswith("？")
    assert "平台" in question


def test_confirmation_parses_natural_language_into_scope():
    draft = ResearchScopeDraft(
        scope=Scope("u", "web", "c", "companion"),
        query="人工智能最近的发展",
        created_at=datetime.now(timezone.utc),
    )
    parsed = parse_scope_confirmation("按全网，重点看中文社区，最近一年", draft)
    assert parsed is not None
    assert parsed.query == draft.query
    assert "web" in parsed.platforms
    assert parsed.time_range == "oneYear"


def test_expired_draft_cannot_be_confirmed():
    draft = ResearchScopeDraft(
        scope=Scope(),
        query="x",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=31),
    )
    assert parse_scope_confirmation("按全网", draft) is None


def test_scope_serializes_for_persistence():
    scope = ResearchScope(
        query="x", platforms=("web", "github"), time_range="oneMonth", depth="deep"
    )
    restored = ResearchScope.from_dict(scope.to_dict())
    assert restored == scope


def test_draft_store_isolated_by_scope(tmp_path):
    store = SQLiteResearchScopeStore(tmp_path / "research.db")
    draft = ResearchScopeDraft(Scope("u", "web", "c1", "companion"), "topic", datetime.now(timezone.utc))
    store.save(draft)
    assert store.get(draft.scope) == draft
    assert store.get(Scope("u", "web", "c2", "companion")) is None
    store.clear(draft.scope)
    assert store.get(draft.scope) is None


def test_agent_loop_asks_before_heavy_research_web_search(tmp_path):
    class Provider:
        async def complete(self, *args, **kwargs):
            raise AssertionError("model should not be called for the scope question")

    scope = Scope("u", "web", "c", "companion")
    loop = AgentLoop(
        Provider(), ToolRegistry(), scope,
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
    )
    import asyncio
    outcome = asyncio.run(loop.run("搜索对AI Agent技术路线的深入研究"))
    assert outcome.status == "completed"
    assert "搜索范围" in outcome.reply


def test_agent_loop_consumes_scope_confirmation_on_followup_turn(tmp_path):
    calls = []

    async def complete(*args, **kwargs):
        return ProviderResponse(message={"role": "assistant", "content": "已查到。"}, usage={})

    def search(arguments, scope):
        calls.append(arguments)
        return {"status": "ok", "output": {"results": [], "result_count": 0}}

    registry = ToolRegistry()
    registry.register(ToolSpec(
        "search__web", {"type": "function", "function": {"name": "search__web", "parameters": {"type": "object"}}},
        search, effect=EFFECT_READ, deterministic_route_allowed=True,
    ))
    scope = Scope("u", "web", "c", "companion")
    db = tmp_path / "core.db"
    loop = AgentLoop(
        type("Provider", (), {"complete": complete})(), registry, scope,
        run_store=SQLiteRunStore(db), confirmation_store=SQLiteConfirmationStore(db),
    )

    first = asyncio.run(loop.run("搜索对AI Agent技术路线的深入研究"))
    assert "搜索范围" in first.reply
    second = asyncio.run(loop.run("按全网，最近一年"))

    assert second.status == "completed"
    assert calls and calls[0]["scope"]["time_range"] == "oneYear"


def test_pending_scope_does_not_intercept_unrelated_readonly_request(tmp_path):
    class Provider:
        async def complete(self, *args, **kwargs):
            return ProviderResponse(message={"role": "assistant", "content": "普通回复"}, usage={})

    scope = Scope("u", "web", "c", "companion")
    db = tmp_path / "core.db"
    loop = AgentLoop(
        Provider(), ToolRegistry(), scope,
        run_store=SQLiteRunStore(db), confirmation_store=SQLiteConfirmationStore(db),
    )
    asyncio.run(loop.run("搜索对AI Agent技术路线的深入研究"))

    response = asyncio.run(loop.run("现在几点？"))

    assert "搜索范围" not in response.reply
