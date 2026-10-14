"""集成测试：AgentLoop 与事件总线"""
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from agent_core.contracts import AgentRequest, ProviderResponse, Scope, ToolCall, ToolResult, ToolSpec
from agent_core.connection_pool import close_all_pools
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.tools import ToolRegistry


class MockProvider(LLMProvider):
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        if self.call_count >= len(self.responses):
            raise RuntimeError("no more mock responses")
        response = self.responses[self.call_count]
        self.call_count += 1
        return ProviderResponse(
            message=response,
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_loop_events.db"


@pytest.fixture
def event_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def registry() -> ToolRegistry:
    async def mock_tool_handler(call: ToolCall, scope: Scope) -> ToolResult:
        await asyncio.sleep(0.01)
        return ToolResult(status="success", output={"result": "mock"})

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="test_tool",
        schema={
            "name": "test_tool",
            "description": "A test tool",
            "input_schema": {"type": "object", "properties": {}}
        },
        handler=mock_tool_handler,
        risk="low",
    ))
    return reg


@pytest.fixture
def scope() -> Scope:
    return Scope(owner_id="test_owner", conversation_id="test_conv", module_id="core")


@pytest.mark.asyncio
async def test_turn_started_event_emitted(
    temp_db: Path,
    event_bus: InMemoryEventBus,
    registry: ToolRegistry,
    scope: Scope
):
    """验证 turn.started 事件被正确发出"""
    events_received = []

    async def listener(event: Event) -> None:
        events_received.append(event)

    event_bus.subscribe(EventType.TURN_STARTED, listener)

    # Mock provider that returns a final answer
    provider = MockProvider([
        {"role": "assistant", "content": "Hello", "stop_reason": "end_turn"}
    ])

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        event_bus=event_bus,
    )

    request = AgentRequest(user_text="hi", scope=scope)
    await loop.run(request)

    # 验证 turn.started 事件
    assert len(events_received) >= 1
    turn_started = events_received[0]
    assert turn_started.type == EventType.TURN_STARTED
    assert turn_started.scope["owner_id"] == "test_owner"
    assert turn_started.scope["conversation_id"] == "test_conv"


@pytest.mark.asyncio
async def test_tool_started_and_completed_events(
    temp_db: Path,
    event_bus: InMemoryEventBus,
    registry: ToolRegistry,
    scope: Scope
):
    """验证 tool.started 和 tool.completed 事件"""
    events_received = []

    async def listener(event: Event) -> None:
        events_received.append(event)

    event_bus.subscribe(EventType.TOOL_STARTED, listener)
    event_bus.subscribe(EventType.TOOL_COMPLETED, listener)
    event_bus.subscribe(EventType.PORTRAIT_CHANGED, listener)

    # Provider returns tool call, then final answer
    provider = MockProvider([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "test_tool", "arguments": "{}"}}],
            "stop_reason": "tool_use"
        },
        {"role": "assistant", "content": "Done", "stop_reason": "end_turn"}
    ])

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        event_bus=event_bus,
    )

    request = AgentRequest(user_text="use tool", scope=scope)
    await loop.run(request)

    # 验证至少有 tool.started 和 tool.completed
    event_types = [e.type for e in events_received]
    assert EventType.TOOL_STARTED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert EventType.PORTRAIT_CHANGED in event_types

    # 验证 tool.started 事件数据
    tool_started = next(e for e in events_received if e.type == EventType.TOOL_STARTED)
    assert isinstance(tool_started.timestamp, datetime)
    assert tool_started.data["tool_name"] == "test_tool"
    assert "tool_call_id" in tool_started.data

    # 验证 tool.completed 事件数据
    tool_completed = next(e for e in events_received if e.type == EventType.TOOL_COMPLETED)
    assert isinstance(tool_completed.timestamp, datetime)
    assert tool_completed.data["tool_name"] == "test_tool"
    assert "tool_call_id" in tool_completed.data


@pytest.mark.asyncio
async def test_turn_failed_event_on_provider_error(
    temp_db: Path,
    event_bus: InMemoryEventBus,
    registry: ToolRegistry,
    scope: Scope
):
    """验证 provider 失败时发出 turn.failed 事件"""
    events_received = []

    async def listener(event: Event) -> None:
        events_received.append(event)

    event_bus.subscribe(EventType.TURN_FAILED, listener)

    class FailingProvider(LLMProvider):
        async def complete(self, messages, **kwargs) -> ProviderResponse:
            raise RuntimeError("simulated provider error")

    loop = AgentLoop(
        provider=FailingProvider(),
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        event_bus=event_bus,
    )

    request = AgentRequest(user_text="test", scope=scope)

    # loop.py 会捕获异常并返回 failed outcome，不会向上抛出
    outcome = await loop.run(request)

    # 验证返回的 outcome 是 failed
    assert outcome.status == "failed"
    assert outcome.reply == "回复生成失败，请稍后重试。"
    assert "RuntimeError" not in outcome.reply
    assert loop.public_messages[-1] == {"role": "assistant", "content": outcome.reply}
    assert SQLiteRunStore(str(temp_db)).load(loop.run_id, scope).messages[-1] == loop.public_messages[-1]

    # 验证 turn.failed 事件
    assert len(events_received) >= 1
    turn_failed = events_received[0]
    assert turn_failed.type == EventType.TURN_FAILED
    assert turn_failed.data["error_code"] == "provider_failed"
    assert turn_failed.data["message"] == "回复生成失败，请稍后重试。"
    assert "error" not in turn_failed.data


@pytest.mark.asyncio
async def test_safety_budget_failure_is_saved_as_final_reply(
    temp_db: Path, registry: ToolRegistry, scope: Scope
):
    loop = AgentLoop(
        provider=MockProvider([]), registry=registry, scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        max_steps=0,
    )

    outcome = await loop.run(AgentRequest(user_text="test", scope=scope))

    assert outcome.status == "failed"
    assert loop.public_messages[-1] == {"role": "assistant", "content": outcome.reply}
    assert SQLiteRunStore(str(temp_db)).load(loop.run_id, scope).messages[-1] == loop.public_messages[-1]


@pytest.mark.asyncio
async def test_last_tool_step_reserves_a_reply_without_more_tools(temp_db, registry, scope):
    class Provider(MockProvider):
        async def complete(self, messages, *, tools=None, **kwargs):
            if self.call_count == 1:
                assert tools is None
            return await super().complete(messages, tools=tools, **kwargs)

    provider = Provider([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "test_tool", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "The result is mock."},
    ])
    loop = AgentLoop(provider=provider, registry=registry, scope=scope,
                     run_store=SQLiteRunStore(temp_db),
                     confirmation_store=SQLiteConfirmationStore(temp_db), max_steps=1)
    outcome = await loop.run(AgentRequest(user_text="test", scope=scope))
    assert outcome.status == "completed"
    assert outcome.reply == "The result is mock."
    assert provider.call_count == 2


@pytest.mark.asyncio
async def test_budget_death_still_delivers_a_summary_of_findings(temp_db, scope):
    """上下文预算耗尽时，收口要把已有发现总结给用户，而不是丢一句模板。

    回归背景（2026-09-30 验收第 15 批 WEB-SUBAGENT-02）：中文搜索结果按
    「1 字 1 token」估算，几条就把上下文吃光；旧逻辑连"总结已有发现"的
    补答都腾不出空间（补答条件还错绑在步数上限上），用户只收到一句
    英文失败提示。现在收口会逐级截短工具结果，保证补答发得出去。
    """

    class Provider(MockProvider):
        def __init__(self, responses) -> None:
            super().__init__(responses)
            self.final_request: list[dict[str, Any]] | None = None

        async def complete(self, messages, *, tools=None, **kwargs):
            if any("本轮工具调用已达到上限" in str(m.get("content") or "") for m in messages):
                assert tools is None  # 收口补答不许再带工具
                self.final_request = [dict(m) for m in messages]
            return await super().complete(messages, tools=tools, **kwargs)

    async def big_result_handler(call: ToolCall, _scope: Scope) -> ToolResult:
        return ToolResult(status="success", output={"result": "检索结果" * 800})

    reg = ToolRegistry()
    reg.register(ToolSpec(
        name="big_tool",
        schema={
            "name": "big_tool",
            "description": "A tool that returns a huge Chinese result",
            "input_schema": {"type": "object", "properties": {}},
        },
        handler=big_result_handler,
        risk="low",
    ))

    provider = Provider([
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "big_tool", "arguments": "{}"},
        }]},
        {"role": "assistant", "content": "根据已查到的结果，目前能确认的结论是：证据不足，需要缩小范围再查。"},
    ])
    loop = AgentLoop(provider=provider, registry=reg, scope=scope,
                     run_store=SQLiteRunStore(temp_db),
                     confirmation_store=SQLiteConfirmationStore(temp_db),
                     max_steps=5, max_total_tokens=600)
    outcome = await loop.run(AgentRequest(user_text="test", scope=scope))
    assert outcome.status == "completed"
    assert outcome.reply.startswith("根据已查到的结果")
    assert provider.call_count == 2
    # 工具结果确实被截短了：完整结果约几千字，收口消息里只装得下截断版。
    tool_messages = [m for m in (provider.final_request or []) if m.get("role") == "tool"]
    assert tool_messages, "收口请求里必须保留工具回执"
    assert all(len(str(m.get("content") or "")) <= 2000 for m in tool_messages)


@pytest.mark.asyncio
async def test_no_events_when_bus_is_none(
    temp_db: Path,
    registry: ToolRegistry,
    scope: Scope
):
    """验证 event_bus=None 时不会报错"""
    provider = MockProvider([
        {"role": "assistant", "content": "OK", "stop_reason": "end_turn"}
    ])

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        event_bus=None,  # 没有事件总线
    )

    request = AgentRequest(user_text="test", scope=scope)
    response = await loop.run(request)

    # 应该正常完成
    assert response.status in ["completed", "success"]


def test_temp_sqlite_file_can_be_deleted_after_store_use(temp_db: Path) -> None:
    store = SQLiteRunStore(str(temp_db))
    confirmation_store = SQLiteConfirmationStore(str(temp_db))
    del store
    del confirmation_store

    close_all_pools()
    temp_db.unlink()
