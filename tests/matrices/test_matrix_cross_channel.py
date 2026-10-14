"""套6 · 跨渠道隔离矩阵（单元部分）。

钉 IntentStateStore 的跨渠道/跨 owner/跨会话隔离口径。每条 case 带 case_id
（M6-CHAN-xxx）+ @pytest.mark.matrix。

分叉点（gap4 + E30-32）：
  M6-CHAN-001~004  store 按 scope 四元组（owner/channel/conversation/module）隔离
  M6-CHAN-005      同 owner 不同 channel 不串
  M6-CHAN-006      同 channel 不同 conversation 不串
  M6-CHAN-007      同 conversation 不同 module 不串

E30-32 入口注册差异（CLI 必注册 subagent__dispatch vs Web 失败仅 warning /
intent_state_store 两入口共享同 DB / work__* 由 work 模块统一注册，
2026-09-14 收拢后两入口工具集一致）依赖 web_api.py / chat_cli.py 的 lifespan
集成，本矩阵聚焦 store 契约层，入口级集成建议 test_web_api / test_chat_cli 扩展。

LLM 行为层（不同会话记忆不串）在 evals/matrix_cross_channel.py。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent_core.contracts import ConversationIntentState, Scope
from agent_core.intent import InMemoryIntentStateStore

pytestmark = pytest.mark.matrix

_NOW = datetime(2026, 9, 14, 12, 0, 0)


def _store() -> InMemoryIntentStateStore:
    return InMemoryIntentStateStore(now=lambda: _NOW)


def _state(topic: str) -> ConversationIntentState:
    return ConversationIntentState(topic=topic, updated_at=_NOW.isoformat(), turns=1)


SCOPE_A = Scope("alice", "web", "conv-1", "companion")
SCOPE_B = Scope("alice", "web", "conv-2", "companion")  # 同 owner/channel/module，不同 conversation
SCOPE_CLI = Scope("alice", "desktop", "conv-1", "companion")  # 同 owner，不同 channel
SCOPE_OTHER_OWNER = Scope("bob", "web", "conv-1", "companion")  # 不同 owner
SCOPE_OTHER_MODULE = Scope("alice", "web", "conv-1", "task")  # 同 conversation，不同 module


# ──────────────────────── scope 四元组隔离 ────────────────────────


def test_M6_CHAN_001_different_conversation_isolated() -> None:
    """同 owner/channel/module，不同 conversation_id → state 不串。"""
    store = _store()
    store.save(SCOPE_A, _state("话题A"))
    store.save(SCOPE_B, _state("话题B"))
    assert store.load(SCOPE_A).topic == "话题A"
    assert store.load(SCOPE_B).topic == "话题B"


def test_M6_CHAN_002_different_channel_isolated() -> None:
    """同 owner，不同 channel（web vs desktop）→ state 不串。"""
    store = _store()
    store.save(SCOPE_A, _state("web话题"))
    store.save(SCOPE_CLI, _state("cli话题"))
    assert store.load(SCOPE_A).topic == "web话题"
    assert store.load(SCOPE_CLI).topic == "cli话题"


def test_M6_CHAN_003_different_owner_isolated() -> None:
    """不同 owner_id → state 不串（用户 A 看不到用户 B 的状态）。"""
    store = _store()
    store.save(SCOPE_A, _state("alice的"))
    store.save(SCOPE_OTHER_OWNER, _state("bob的"))
    assert store.load(SCOPE_A).topic == "alice的"
    assert store.load(SCOPE_OTHER_OWNER).topic == "bob的"


def test_M6_CHAN_004_different_module_isolated() -> None:
    """同 conversation，不同 module_id → state 不串。"""
    store = _store()
    store.save(SCOPE_A, _state("companion"))
    store.save(SCOPE_OTHER_MODULE, _state("task"))
    assert store.load(SCOPE_A).topic == "companion"
    assert store.load(SCOPE_OTHER_MODULE).topic == "task"


# ──────────────────────── clear 不影响其他 scope ────────────────────────


def test_M6_CHAN_005_clear_one_scope_keeps_others() -> None:
    """清一个 scope 不影响其他 scope（跨渠道清理的边界）。"""
    store = _store()
    store.save(SCOPE_A, _state("A"))
    store.save(SCOPE_B, _state("B"))
    assert store.clear(SCOPE_A) is True
    assert store.load(SCOPE_A) is None
    assert store.load(SCOPE_B) is not None  # B 还在


def test_M6_CHAN_006_clear_missing_scope_is_safe() -> None:
    """清不存在的 scope → False，不抛（跨渠道操作的安全边界）。"""
    store = _store()
    assert store.clear(SCOPE_A) is False


# ──────────────────────── 衰减仍按 scope 独立 ────────────────────────


def test_M6_CHAN_007_decay_is_per_scope() -> None:
    """TTL 过期只作废对应 scope，不影响其他 scope。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(ttl_seconds=900, now=lambda: now)
    fresh = ConversationIntentState(topic="新", updated_at=now.isoformat(), turns=1)
    stale = ConversationIntentState(
        topic="旧", updated_at=(now - timedelta(seconds=1000)).isoformat(), turns=1
    )
    store.save(SCOPE_A, fresh)
    store.save(SCOPE_B, stale)
    assert store.load(SCOPE_A) is not None  # fresh
    assert store.load(SCOPE_B) is None  # stale 过期
