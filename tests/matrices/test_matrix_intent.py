"""套2 · 意图闸门矩阵。

把 agent_core/intent.py 的 RuleIntentClassifier + IntentGate + InMemoryIntentStateStore
全部分叉点系统化钉一遍，每条 case 带 case_id（M2-INTENT-xxx）+ @pytest.mark.matrix。

分叉点对照（B 层意图，详见 plan）：
  B17  三档：CALL(>=0.85 且 not ambiguous) / CLARIFY(>=0.55) / CHAT(<0.55, block_all)
  +    conversation intent → CHAT（不 block_all，但 blocked_intents 非空）
  +    冲突 penalty 把显式请求夹进 [0.55, 0.84] → ambiguous → CLARIFY
  衰减  TTL 超限 / 轮数超限 → _state_is_fresh False → pending_intent 作废
  B18  CHAT + block_all → 工具清单都不给模型（IntentGate 层断言 block_all=True）

评分常量（intent.py，逐项可手算）：
  base 0.10 + action 0.35 + object 0.30 + slots_full 0.23 = 0.98（CALL）
  base 0.10 + action 0.35 + object 0.30 - slots_missing 0.10 = 0.65（CLARIFY）
  base 0.10 - no_action 0.05 = 0.05（CHAT block_all，无动作词）
  陈述句 0.98 / 疑问句 0.96 / 中性 0.70（conversation）

pending_intent 跨轮合并逻辑在 loop.py（_route_by_intent），本矩阵聚焦 IntentGate 判定 +
状态衰减，合并用 ConversationIntentState.pending_intent 字段可设来间接钉。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent_core.contracts import (
    CONVERSATION_INTENT,
    ConversationIntentState,
    IntentDecision,
    IntentPolicy,
    Scope,
)
from agent_core.intent import (
    CALL,
    CHAT,
    CLARIFY,
    InMemoryIntentStateStore,
    IntentGate,
    RuleIntentClassifier,
)

pytestmark = pytest.mark.matrix

# ── 与 test_intent_gate.py 对齐的 policy 配置 ──
WEATHER_POLICY = IntentPolicy(
    intent_name="weather_query",
    object_terms=("天气",),
    negative_terms=("天气局", "气象局"),
    required_slots=("location",),
)
MUSIC_POLICY = IntentPolicy(
    intent_name="music_query",
    object_terms=("歌", "歌曲", "音乐"),
    action_terms=("播放", "放首", "来首", "听首", "搜索", "搜"),
    required_slots=("query",),
)
POLICIES = (WEATHER_POLICY, MUSIC_POLICY)
SCOPE = Scope(owner_id="tester", channel="test", conversation_id="conv-1")


def _classifier() -> RuleIntentClassifier:
    return RuleIntentClassifier(POLICIES)


def _gate() -> IntentGate:
    return IntentGate(POLICIES)


# ──────────────────────── B17 三档 ────────────────────────


def test_M2_INTENT_001_explicit_request_is_call() -> None:
    """B17 CALL：'查询佛山的天气' 0.10+0.35+0.30+0.23=0.98 → CALL，不 block。"""
    decision = _classifier().classify("查询佛山的天气")
    assert decision.intent == "weather_query"
    assert abs(decision.confidence - 0.98) < 0.001
    assert not decision.ambiguous
    gate = _gate().decide(decision)
    assert gate.action == CALL
    assert not gate.block_all


def test_M2_INTENT_002_missing_location_is_clarify() -> None:
    """B17 CLARIFY：'天气查询' 0.10+0.35+0.30-0.10=0.65 缺地点 → CLARIFY + clarify_hint。"""
    decision = _classifier().classify("天气查询")
    assert decision.intent == "weather_query"
    assert abs(decision.confidence - 0.65) < 0.001
    assert decision.ambiguous
    gate = _gate().decide(decision)
    assert gate.action == CLARIFY
    assert gate.clarify_hint


def test_M2_INTENT_003_chatter_about_bureau_is_chat_not_block_all() -> None:
    """B17 CHAT(conversation)：'天气局上班啦' negative 排除对象词 → conversation CHAT，
    不 block_all（工具清单照给模型），但 blocked_intents 含 weather_query。"""
    decision = _classifier().classify("我昨天去天气局上班啦")
    assert decision.intent == CONVERSATION_INTENT
    gate = _gate().decide(decision)
    assert gate.action == CHAT
    assert not gate.block_all
    assert "weather_query" in gate.blocked_intents


def test_M2_INTENT_004_low_confidence_is_chat_block_all() -> None:
    """B18 CHAT block_all：构造置信度 < 0.55 的 decision → block_all=True（工具清单不给模型）。"""
    low = IntentDecision(intent="weather_query", confidence=0.30, reason="低置信")
    gate = _gate().decide(low)
    assert gate.action == CHAT
    assert gate.block_all is True


def test_M2_INTENT_005_music_request_is_call_with_query_slot() -> None:
    """B17 CALL(music)：'播放周杰伦的歌' → music_query, query=周杰伦。"""
    decision = _classifier().classify("播放周杰伦的歌")
    assert decision.intent == "music_query"
    assert decision.slots.get("query") == "周杰伦"
    gate = _gate().decide(decision)
    assert gate.action == CALL


# ──────────────────────── 评分公式核对 ────────────────────────


def test_M2_INTENT_006_scores_add_up_to_098() -> None:
    """公式核对：base 0.10 + action 0.35 + object 0.30 + slots_full 0.23 = 0.98。"""
    decision = _classifier().classify("查询佛山的天气")
    assert abs(decision.confidence - 0.98) < 0.001


def test_M2_INTENT_007_missing_slot_drops_to_065() -> None:
    """公式核对：base+action+object - slots_missing 0.10 = 0.65，ambiguous=True。"""
    decision = _classifier().classify("天气查询")
    assert abs(decision.confidence - 0.65) < 0.001
    assert decision.ambiguous


def test_M2_INTENT_008_declarative_conversation_098() -> None:
    """陈述句 conversation：'啦'触发 declarative → 0.98。"""
    decision = _classifier().classify("我昨天去天气局上班啦")
    assert decision.intent == CONVERSATION_INTENT
    assert abs(decision.confidence - 0.98) < 0.001


def test_M2_INTENT_009_interrogative_conversation_096() -> None:
    """疑问句 conversation：'什么'触发 interrogative → 0.96。"""
    decision = _classifier().classify("天气局是做什么的")
    assert decision.intent == CONVERSATION_INTENT
    assert abs(decision.confidence - 0.96) < 0.001


def test_M2_INTENT_010_neutral_conversation_070() -> None:
    """中性 conversation：无陈述/疑问标记 → 0.70。"""
    decision = _classifier().classify("天气")
    assert decision.intent == CONVERSATION_INTENT
    assert abs(decision.confidence - 0.70) < 0.001


def test_M2_INTENT_011_topic_conflict_clamps_to_078() -> None:
    """冲突 penalty：上一轮闲聊'天气'话题，这轮'查询佛山的天气'撞同一对象词
    → 0.98 - 0.20 = 0.78，夹进 [0.55, 0.84]，ambiguous=True → CLARIFY。"""
    state = ConversationIntentState(
        topic="我昨天去天气局上班啦",
        topic_terms=("天气",),
        active_intent=CONVERSATION_INTENT,
    )
    decision = _classifier().classify("查询佛山的天气", state=state)
    assert abs(decision.confidence - 0.78) < 0.001
    assert decision.ambiguous
    gate = _gate().decide(decision)
    assert gate.action == CLARIFY


# ──────────────────────── 衰减：TTL / 轮数 / store ────────────────────────


def test_M2_INTENT_012_ttl_expiry_discards_state() -> None:
    """衰减 TTL：state.updated_at 比 now 早超过 ttl_seconds → load 返回 None（作废）。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(ttl_seconds=900, max_turns=5, now=lambda: now)
    stale = ConversationIntentState(
        topic="旧话题", updated_at=(now - timedelta(seconds=1000)).isoformat(), turns=1
    )
    store.save(SCOPE, stale)
    assert store.load(SCOPE) is None  # 1000s > 900s ttl


def test_M2_INTENT_013_fresh_within_ttl_is_loaded() -> None:
    """衰减对照：updated_at 在 ttl 内 → load 返回 state。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(ttl_seconds=900, max_turns=5, now=lambda: now)
    fresh = ConversationIntentState(
        topic="新话题", updated_at=(now - timedelta(seconds=100)).isoformat(), turns=1
    )
    store.save(SCOPE, fresh)
    loaded = store.load(SCOPE)
    assert loaded is not None
    assert loaded.topic == "新话题"


def test_M2_INTENT_014_turn_overflow_discards_state() -> None:
    """衰减轮数：turns >= max_turns → load 返回 None。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(ttl_seconds=900, max_turns=5, now=lambda: now)
    over = ConversationIntentState(
        topic="轮数超限", updated_at=now.isoformat(), turns=6
    )
    store.save(SCOPE, over)
    assert store.load(SCOPE) is None


def test_M2_INTENT_015_store_isolates_by_scope() -> None:
    """store 隔离：不同 conversation_id 的 state 不串。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(now=lambda: now)
    other = Scope(owner_id="tester", channel="test", conversation_id="conv-2")
    store.save(SCOPE, ConversationIntentState(topic="A", updated_at=now.isoformat(), turns=1))
    store.save(other, ConversationIntentState(topic="B", updated_at=now.isoformat(), turns=1))
    assert store.load(SCOPE).topic == "A"
    assert store.load(other).topic == "B"


def test_M2_INTENT_016_store_clear_removes_state() -> None:
    """store clear：清除后 load 返回 None，clear 返回 True。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(now=lambda: now)
    store.save(SCOPE, ConversationIntentState(topic="待清", updated_at=now.isoformat(), turns=1))
    assert store.clear(SCOPE) is True
    assert store.load(SCOPE) is None
    assert store.clear(SCOPE) is False  # 已清空


def test_M2_INTENT_017_store_clear_on_missing_scope_returns_false() -> None:
    """store clear 对照：不存在的 scope → False，不抛。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(now=lambda: now)
    assert store.clear(SCOPE) is False


# ──────────────────────── pending_intent 字段（合并逻辑在 loop）────────────────────────


def test_M2_INTENT_018_pending_intent_field_roundtrip() -> None:
    """ConversationIntentState.pending_intent 可设可读（跨轮合并逻辑在 loop._route_by_intent，
    本矩阵只钉字段往返不丢）。"""
    now = datetime(2026, 9, 14, 12, 0, 0)
    store = InMemoryIntentStateStore(now=lambda: now)
    state = ConversationIntentState(
        topic="天气查询",
        active_intent="weather_query",
        pending_intent="weather_query",
        pending_question="你想查哪里的天气？",
        slots={"location": "佛山"},
        updated_at=now.isoformat(),
        turns=2,
    )
    store.save(SCOPE, state)
    loaded = store.load(SCOPE)
    assert loaded.pending_intent == "weather_query"
    assert loaded.pending_question == "你想查哪里的天气？"
    assert loaded.slots == {"location": "佛山"}


# ──────────────────────── GateDecision 边界 ────────────────────────


def test_M2_INTENT_019_gate_call_has_no_blocked_intents() -> None:
    """GateDecision CALL：不 block 任何意图，不 block_all。"""
    decision = _classifier().classify("查询佛山的天气")
    gate = _gate().decide(decision)
    assert gate.action == CALL
    assert gate.blocked_intents == ()
    assert not gate.block_all


def test_M2_INTENT_020_gate_clarify_carries_hint() -> None:
    """GateDecision CLARIFY：带 clarify_hint（候选动作/已识别/依据）。"""
    decision = _classifier().classify("天气查询")
    gate = _gate().decide(decision)
    assert gate.action == CLARIFY
    assert "查询实时天气" in gate.clarify_hint  # INTENT_LABELS 的中文说法


def test_M2_INTENT_021_gate_chat_block_all_when_below_clarify() -> None:
    """GateDecision CHAT(block_all)：置信度 < CLARIFY_THRESHOLD(0.55) → block_all=True。"""
    low = IntentDecision(intent="weather_query", confidence=0.40, reason="远低于阈值")
    gate = _gate().decide(low)
    assert gate.action == CHAT
    assert gate.block_all is True


def test_M2_INTENT_022_gate_chat_conversation_blocks_related_only() -> None:
    """GateDecision CHAT(conversation)：不 block_all，但 blocked_intents 含相关意图
    （'天气局是做什么的' 模型仍可自己搜，拦的只是规则误触发）。"""
    decision = _classifier().classify("天气局是做什么的")
    gate = _gate().decide(decision)
    assert gate.action == CHAT
    assert not gate.block_all
    assert "weather_query" in gate.blocked_intents
