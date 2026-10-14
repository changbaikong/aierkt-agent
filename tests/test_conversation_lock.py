"""Tests for conversation locking mechanism."""

import asyncio
import pytest

from agent_core.conversation_manager import ConversationManager, ConversationLock
from agent_core.contracts import AgentOutcome, AgentRequest, Scope


class MockSession:
    """Mock session that returns a fixed outcome."""

    def __init__(self, scope: Scope, outcome: AgentOutcome | None = None):
        self.scope = scope
        self.outcome = outcome or AgentOutcome("completed", "test-turn", reply="ok")

    async def run(self, request: AgentRequest) -> AgentOutcome:
        await asyncio.sleep(0.01)  # Simulate work
        return self.outcome


@pytest.mark.asyncio
async def test_lock_prevents_new_turns():
    """锁定的会话应拒绝新的 turn 提交"""
    manager = ConversationManager(lambda scope: MockSession(scope))
    scope = Scope(owner_id="user1", conversation_id="conv1")

    # 手动锁定会话
    manager.lock_conversation("conv1", "manual_pause", "action-123")

    # 尝试提交 turn
    handle = await manager.submit(AgentRequest(scope=scope, user_text="hello"))

    # 应该被拒绝
    assert not handle.accepted
    assert "会话已锁定" in handle.reason
    assert "action-123" in handle.reason

    # 结果应该是 rejected
    outcome = await handle.result
    assert outcome.status == "rejected"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_waiting_confirmation_does_not_block_a_new_turn():
    """A durable confirmation may remain pending while the conversation continues."""
    manager = ConversationManager(lambda scope: MockSession(scope))
    scope = Scope(owner_id="user1", conversation_id="conv1")
    manager.lock_conversation("conv1", "waiting_confirmation", "action-123")

    handle = await manager.submit(AgentRequest(scope=scope, user_text="换个话题"))

    assert handle.accepted
    assert (await handle.result).status == "completed"
    assert manager.is_locked("conv1")
    await manager.shutdown()


@pytest.mark.asyncio
async def test_unlock_allows_new_turns():
    """解锁后的会话应允许新的 turn 提交"""
    manager = ConversationManager(lambda scope: MockSession(scope))
    scope = Scope(owner_id="user1", conversation_id="conv1")

    # 锁定会话
    manager.lock_conversation("conv1", "manual_pause", "action-123")

    # 解锁
    manager.unlock_conversation("conv1")

    # 现在应该可以提交
    handle = await manager.submit(AgentRequest(scope=scope, user_text="hello"))
    assert handle.accepted

    outcome = await handle.result
    assert outcome.status == "completed"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_lock_is_conversation_specific():
    """锁应该只影响特定会话，不影响其他会话"""
    manager = ConversationManager(lambda scope: MockSession(scope))
    scope1 = Scope(owner_id="user1", conversation_id="conv1")
    scope2 = Scope(owner_id="user1", conversation_id="conv2")

    # 只锁定 conv1
    manager.lock_conversation("conv1", "manual_pause", "action-123")

    # conv1 应该被拒绝
    handle1 = await manager.submit(AgentRequest(scope=scope1, user_text="hello"))
    assert not handle1.accepted

    # conv2 应该可以提交
    handle2 = await manager.submit(AgentRequest(scope=scope2, user_text="hello"))
    assert handle2.accepted

    outcome2 = await handle2.result
    assert outcome2.status == "completed"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_is_locked_query():
    """is_locked() 应该正确反映锁状态"""
    manager = ConversationManager(lambda scope: MockSession(scope))

    assert not manager.is_locked("conv1")

    manager.lock_conversation("conv1", "test", "action-1")
    assert manager.is_locked("conv1")

    manager.unlock_conversation("conv1")
    assert not manager.is_locked("conv1")

    await manager.shutdown()


@pytest.mark.asyncio
async def test_get_lock_returns_info():
    """get_lock() 应该返回完整的锁信息"""
    manager = ConversationManager(lambda scope: MockSession(scope))

    # 没有锁时返回 None
    assert manager.get_lock("conv1") is None

    # 锁定后返回 ConversationLock
    manager.lock_conversation("conv1", "waiting_confirmation", "action-123")
    lock = manager.get_lock("conv1")

    assert lock is not None
    assert lock.conversation_id == "conv1"
    assert lock.reason == "waiting_confirmation"
    assert lock.action_id == "action-123"
    assert lock.locked_at > 0

    await manager.shutdown()


@pytest.mark.asyncio
async def test_unlock_idempotent():
    """多次解锁同一会话不应报错"""
    manager = ConversationManager(lambda scope: MockSession(scope))

    manager.lock_conversation("conv1", "test", "action-1")
    manager.unlock_conversation("conv1")
    manager.unlock_conversation("conv1")  # 第二次解锁
    manager.unlock_conversation("conv1")  # 第三次解锁

    # 不应报错，会话应该是解锁状态
    assert not manager.is_locked("conv1")

    await manager.shutdown()


@pytest.mark.asyncio
async def test_lock_during_execution():
    """在 turn 执行期间锁定会话，不影响当前 turn，但阻止后续 turn"""

    class SlowSession:
        def __init__(self, scope: Scope):
            self.scope = scope

        async def run(self, request: AgentRequest) -> AgentOutcome:
            await asyncio.sleep(0.1)  # 模拟长时间运行
            return AgentOutcome("completed", "test-turn", reply="done")

    manager = ConversationManager(lambda scope: SlowSession(scope))
    scope = Scope(owner_id="user1", conversation_id="conv1")

    # 提交第一个 turn（开始执行）
    handle1 = await manager.submit(AgentRequest(scope=scope, user_text="first"))
    assert handle1.accepted

    # 等待一小段时间确保开始执行
    await asyncio.sleep(0.02)

    # 在执行期间锁定会话
    manager.lock_conversation("conv1", "paused", "action-1")

    # 尝试提交第二个 turn（应该被拒绝）
    handle2 = await manager.submit(AgentRequest(scope=scope, user_text="second"))
    assert not handle2.accepted
    assert "会话已锁定" in handle2.reason

    # 第一个 turn 应该正常完成（不受锁影响）
    outcome1 = await handle1.result
    assert outcome1.status == "completed"

    await manager.shutdown()


@pytest.mark.asyncio
async def test_multiple_lock_overwrites():
    """多次锁定同一会话应覆盖之前的锁信息"""
    manager = ConversationManager(lambda scope: MockSession(scope))

    manager.lock_conversation("conv1", "reason1", "action-1")
    lock1 = manager.get_lock("conv1")
    assert lock1.reason == "reason1"
    assert lock1.action_id == "action-1"

    # 第二次锁定应覆盖
    manager.lock_conversation("conv1", "reason2", "action-2")
    lock2 = manager.get_lock("conv1")
    assert lock2.reason == "reason2"
    assert lock2.action_id == "action-2"

    await manager.shutdown()
