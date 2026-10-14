"""Tests for conversation resume mechanism."""

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from agent_core.contracts import AgentOutcome, AgentRequest, ProviderResponse, Scope
from agent_core.conversation_manager import ConversationManager
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore
from agent_core.persistence import SQLiteRunStore
from agent_core.task_persistence import TaskStore
from agent_core.task_service import TaskService
from agent_core.tools import ToolRegistry
from agent_tools.tasks import make_task_tools
from agent_tools.system_info import make_system_info_tool


class MockSession:
    """Mock session for testing."""

    def __init__(self, scope: Scope):
        self.scope = scope
        self.calls = []

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.calls.append(request)
        return AgentOutcome("completed", uuid.uuid4().hex, reply="mock response")


class ResumableSession(MockSession):
    async def resume(self, action_id: str, decision: str) -> AgentOutcome:
        self.calls.append((action_id, decision))
        return AgentOutcome("completed", "resumed-run", reply="resumed response")


class PausingSession(MockSession):
    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.calls.append(request)
        return AgentOutcome("paused", "paused-run", action_id="action-paused")


@pytest.fixture
def confirmation_store(tmp_path):
    """Create a temporary confirmation store."""
    db_path = tmp_path / "test_confirmations.db"
    store = SQLiteConfirmationStore(str(db_path))
    yield store
    # SQLiteConfirmationStore doesn't have a close() method


@pytest.fixture
def manager(confirmation_store):
    """Create a conversation manager with confirmation store."""

    def session_factory(scope: Scope):
        return MockSession(scope)

    mgr = ConversationManager(
        session_factory,
        max_queue_size=5,
        confirmation_store=confirmation_store,
    )
    yield mgr
    asyncio.run(mgr.shutdown())


@pytest.mark.asyncio
async def test_resume_unlocks_conversation(manager, confirmation_store):
    """Test that resume() unlocks the conversation."""
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv1", module_id="test")

    # 创建一个确认请求
    req = confirmation_store.create(
        run_id="run_1",
        scope=scope,
        tool_call_id="tool_call_123",
        tool="test_tool",
        arguments={"arg": "value"},
        title="Test confirmation",
        module_spec_json='{"id": "test", "name": "Test Module"}',
    )
    action_id = req.action_id

    # 锁定会话
    manager.lock_conversation(scope.conversation_id, "waiting_confirmation", action_id)
    assert manager.is_locked(scope.conversation_id)

    # 恢复会话
    outcome = await manager.resume(action_id, scope, "confirmed")

    # 验证会话已解锁
    assert not manager.is_locked(scope.conversation_id)
    assert outcome.status in ("completed", "failed")


@pytest.mark.asyncio
async def test_resume_nonexistent_action(manager, confirmation_store):
    """Test resume with nonexistent action_id."""
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv1", module_id="test")

    outcome = await manager.resume("nonexistent_action", scope, "approved")

    assert outcome.status == "failed"
    assert "不存在" in outcome.reply or "过期" in outcome.reply


@pytest.mark.asyncio
async def test_resume_already_decided(manager, confirmation_store):
    """Test resume with already decided confirmation."""
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv1", module_id="test")

    # 创建并决策确认请求
    req = confirmation_store.create(
        run_id="run_1",
        scope=scope,
        tool_call_id="tool_call_456",
        tool="test_tool",
        arguments={"arg": "value"},
        title="Test confirmation",
        module_spec_json='{"id": "test"}',
    )
    action_id = req.action_id
    confirmation_store.decide(action_id, scope, "confirmed")

    # 尝试再次恢复
    outcome = await manager.resume(action_id, scope, "confirmed")

    assert outcome.status == "failed"
    assert "状态" in outcome.reply or "无法恢复" in outcome.reply


@pytest.mark.asyncio
async def test_resume_without_confirmation_store():
    """Test that resume fails without confirmation store."""

    def session_factory(scope: Scope):
        return MockSession(scope)

    manager = ConversationManager(session_factory)
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv1")

    with pytest.raises(RuntimeError, match="confirmation_store"):
        await manager.resume("action_123", scope, "confirmed")

    await manager.shutdown()


@pytest.mark.asyncio
async def test_resume_submits_turn(manager, confirmation_store):
    """Test that resume submits a new turn after unlocking."""
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv1", module_id="test")

    # 创建确认请求
    req = confirmation_store.create(
        run_id="run_1",
        scope=scope,
        tool_call_id="tool_call_789",
        tool="test_tool",
        arguments={"arg": "value"},
        title="Test confirmation",
        module_spec_json='{"id": "test"}',
    )
    action_id = req.action_id

    # 锁定会话
    manager.lock_conversation(scope.conversation_id, "waiting_confirmation", action_id)

    # 恢复会话
    outcome = await manager.resume(action_id, scope, "confirmed")

    # 验证 turn 被提交
    assert outcome.status in ("completed", "failed")

    # 验证会话已解锁，可以提交新 turn
    request = AgentRequest(user_text="test after resume", scope=scope)
    handle = await manager.submit(request)
    assert handle.accepted


@pytest.mark.asyncio
async def test_resume_calls_session_resume_with_persisted_action(manager, confirmation_store):
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv-resume", module_id="test")
    session = ResumableSession(scope)
    manager.session_factory = lambda _scope: session
    req = confirmation_store.create(
        run_id="run-1", scope=scope, tool_call_id="tool-1", tool="test_tool", arguments={}, title="Test",
    )
    outcome = await manager.resume(req.action_id, scope, "confirmed")
    assert outcome.reply == "resumed response"
    assert session.calls == [(req.action_id, "confirmed")]


@pytest.mark.asyncio
async def test_paused_turn_locks_conversation(manager):
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv-paused", module_id="test")
    manager.session_factory = lambda _scope: PausingSession(scope)

    handle = await manager.submit(AgentRequest("requires confirmation", scope=scope))
    outcome = await handle.result

    assert outcome.status == "paused"
    lock = manager.get_lock(scope.conversation_id)
    assert lock is not None
    assert lock.reason == "waiting_confirmation"
    assert lock.action_id == "action-paused"


@pytest.mark.asyncio
async def test_reset_pending_rejects_requests_and_unlocks_conversation(manager, confirmation_store):
    scope = Scope(owner_id="user1", channel="ch1", conversation_id="conv-reset", module_id="test")
    request = confirmation_store.create(
        run_id="run-reset", scope=scope, tool_call_id="tool-reset", tool="test_tool", arguments={}, title="Test",
    )
    manager.lock_conversation(scope.conversation_id, "waiting_confirmation", request.action_id)

    count = await manager.reset_pending(scope)

    assert count == 1
    assert confirmation_store.get(request.action_id, scope).status == "rejected"
    assert not manager.is_locked(scope.conversation_id)


@pytest.mark.asyncio
async def test_confirmation_resume_creates_scheduled_task_end_to_end(tmp_path):
    class Provider:
        max_tokens = 512

        def __init__(self):
            self.responses = [
                ProviderResponse({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "task-call",
                        "function": {
                            "name": "tasks__create",
                            "arguments": '{"title": "Wake up", "request_text": "Wake up", "delivery_mode": "notify", "schedule": {"run_at": "2030-01-01T20:00:00+08:00"}, "allowed_tools": []}',
                        },
                    }],
                }),
                ProviderResponse({"role": "assistant", "content": "Reminder scheduled."}),
            ]

        async def complete(self, *_args, **_kwargs):
            return self.responses.pop(0)

    provider = Provider()
    registry = ToolRegistry()
    task_service = TaskService(
        provider=None, registry_provider=lambda: registry, store=TaskStore(tmp_path / "tasks.db"),
    )
    for spec in make_task_tools(task_service):
        registry.register(type(spec)(
            name=f"tasks__{spec.name}", schema=spec.schema, handler=spec.handler, risk=spec.risk,
            timeout_seconds=spec.timeout_seconds, confirmation_title=spec.confirmation_title,
        ))
    scope = Scope("user", "desktop", "chat")
    run_store = SQLiteRunStore(tmp_path / "core.db")
    confirmation_store = SQLiteConfirmationStore(tmp_path / "core.db")

    class Session:
        def __init__(self):
            self.loop = AgentLoop(provider, registry, scope, run_store=run_store, confirmation_store=confirmation_store)

        async def run(self, request):
            return await self.loop.run(request)

        async def resume(self, action_id, decision):
            return await self.loop.resume(action_id, decision)

    manager = ConversationManager(lambda _scope: Session(), confirmation_store=confirmation_store)
    try:
        paused = await manager.submit(AgentRequest("Set an alarm", scope=scope))
        paused_outcome = await paused.result
        assert paused_outcome.status == "paused"

        completed = await manager.resume(paused_outcome.action_id, scope, "confirmed")

        assert completed.status == "completed"
        tasks = task_service.store.list_tasks(scope)
        assert len(tasks) == 1
        assert tasks[0].status == "scheduled"
        assert tasks[0].next_run_at == "2030-01-01T12:00:00+00:00"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_relative_time_task_flow_reads_local_time_before_create(tmp_path):
    class Provider:
        max_tokens = 512

        def __init__(self):
            self.responses = [
                ProviderResponse({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "time-call",
                        "function": {
                            "name": "system__local_info",
                            "arguments": '{"kind": "time"}',
                        },
                    }],
                }),
                ProviderResponse({
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "task-call",
                        "function": {
                            "name": "tasks__create",
                            "arguments": '{"title": "提醒", "request_text": "5分钟后提醒我", "delivery_mode": "notify", "schedule": {"run_at": "2030-01-01T20:05:00+08:00", "timezone": "Asia/Shanghai"}, "allowed_tools": []}',
                        },
                    }],
                }),
                ProviderResponse({"role": "assistant", "content": "Reminder scheduled."}),
            ]
            self.seen_prompts = []

        async def complete(self, messages, **_kwargs):
            self.seen_prompts.append(messages)
            return self.responses.pop(0)

    provider = Provider()
    registry = ToolRegistry()
    task_service = TaskService(
        provider=None,
        registry_provider=lambda: registry,
        store=TaskStore(tmp_path / "tasks.db"),
    )
    for spec in make_task_tools(task_service):
        registry.register(type(spec)(
            name=f"tasks__{spec.name}", schema=spec.schema, handler=spec.handler, risk=spec.risk,
            timeout_seconds=spec.timeout_seconds, confirmation_title=spec.confirmation_title,
        ))
    system_tool = make_system_info_tool()
    registry.register(type(system_tool)(
        name="system__local_info",
        schema=system_tool.schema,
        handler=system_tool.handler,
        risk=system_tool.risk,
        timeout_seconds=system_tool.timeout_seconds,
        confirmation_title=system_tool.confirmation_title,
    ))

    scope = Scope("user", "desktop", "chat")
    run_store = SQLiteRunStore(tmp_path / "core.db")
    confirmation_store = SQLiteConfirmationStore(tmp_path / "core.db")

    class Session:
        def __init__(self):
            self.loop = AgentLoop(provider, registry, scope, run_store=run_store, confirmation_store=confirmation_store)

        async def run(self, request):
            return await self.loop.run(request)

        async def resume(self, action_id, decision):
            return await self.loop.resume(action_id, decision)

    manager = ConversationManager(lambda _scope: Session(), confirmation_store=confirmation_store)
    try:
        paused = await manager.submit(AgentRequest("5分钟后提醒我", scope=scope))
        paused_outcome = await paused.result
        assert paused_outcome.status == "paused"

        resumed = await manager.resume(paused_outcome.action_id, scope, "confirmed")

        assert resumed.status == "completed"
        tasks = task_service.store.list_tasks(scope)
        assert len(tasks) == 1
        assert tasks[0].status == "scheduled"
        assert tasks[0].schedule["kind"] == "run_at"
        assert datetime.fromisoformat(tasks[0].schedule["run_at"]) > datetime.now(timezone.utc)
        assert provider.seen_prompts
    finally:
        await manager.shutdown()
