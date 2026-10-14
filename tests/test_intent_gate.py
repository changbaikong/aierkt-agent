"""意图闸门与轻量会话状态的回归测试。

覆盖方案测试计划里的每一条，外加两条工程性回归：
闸门未注入时行为完全不变、置信度过低时本轮连工具清单都不给模型。

【读这份测试的顺序】
先跑 ClassifierTests 看评分是怎么来的（每个数字都能手算核对），
再跑 GateTests 看三档阈值，最后跑 LoopTests 看整条链路合起来是什么行为。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from agent_core.contracts import (
    AgentRequest,
    ConversationIntentState,
    EFFECT_READ,
    IntentDecision,
    IntentPolicy,
    ProviderResponse,
    Scope,
    ToolSpec,
)
from agent_core.intent import (
    CALL,
    CHAT,
    CLARIFY,
    CONVERSATION_INTENT,
    InMemoryIntentStateStore,
    IntentGate,
    RuleIntentClassifier,
    SQLiteIntentStateStore,
    render_state_summary,
)
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.tools import ToolRegistry

SCOPE = Scope(owner_id="tester", channel="test", conversation_id="conv-1")
OTHER_SCOPE = Scope(owner_id="tester", channel="test", conversation_id="conv-2")

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


def _schema(name: str, properties: dict | None = None) -> dict:
    """替身工具的 schema。**`properties` 必须照真实工具写。**

    真实工具的 schema 都带 `additionalProperties: False`，而网关会真的按
    JSON Schema 校验参数（`ToolExecutor` 从来不校验）。于是"properties 空着"
    等于声明"这个工具不接受任何参数" —— 以前没人发现，因为没人校验；
    迁到网关之后，凡是带参数的调用都会被判成非法参数而拒绝，
    症状是"工具一次都没被调用"，看不出是被校验挡掉的。
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": dict(properties or {}),
                "additionalProperties": False,
            },
        },
    }


def _web_tool(calls: list[dict], *, failing: bool = False) -> ToolSpec:
    def handler(arguments: dict, scope: Scope) -> dict:
        calls.append(dict(arguments))
        if failing:
            return {"status": "failed", "error": "网络不可用"}
        return {"status": "ok", "output": {"results": [{"title": "佛山 晴 28℃", "url": "https://example.com/w"}]}}

    return ToolSpec(
        name="search__web",
        schema=_schema("search__web", {"query": {"type": "string"}}),
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
        intent_policies=(WEATHER_POLICY,),
    )


def _weather_tool(calls: list[dict], *, failing: bool = False) -> ToolSpec:
    """天气工具的最小替身。

    改造前这一组用例注册的是网页搜索工具 —— 因为那时天气只能落到搜索上
    （`_run_intent_query` 拼一句"佛山 天气"丢过去）。现在天气有自己的工具，
    断言的重点没变（"工具被调用了、参数是用户说的那个地点"），
    只是参数从 `{"query": "佛山 天气"}` 变成了 `{"area": "佛山"}`。

    `intent_policies` 必须带上：意图闸门靠它把 weather_query 派给这个工具。
    """
    def handler(arguments: dict, scope: Scope) -> dict:
        calls.append(dict(arguments))
        if failing:
            return {
                "status": "failed", "output": {"reason": "weather_unavailable"},
                "error": "weather_unavailable", "error_kind": "transient",
            }
        return {"status": "ok", "output": {
            "kind": "live", "area_code": "440600", "province": "广东省", "city": "佛山市",
            "weather": "晴", "temperature_c": 28, "wind_direction": "东风",
            "wind_power": "3", "humidity": "55", "report_time": "2026-09-20 12:00:00",
        }}

    return ToolSpec(
        name="weather__query",
        schema=_schema("weather__query", {"area": {"type": "string"}, "kind": {"type": "string"}}),
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
        intent_policies=(WEATHER_POLICY,),
    )


def _music_tool(calls: list[dict]) -> ToolSpec:
    def handler(arguments: dict, scope: Scope) -> dict:
        calls.append(dict(arguments))
        return {"status": "ok", "output": {"tracks": [{"title": "晴天", "artist": "周杰伦"}]}}

    return ToolSpec(
        name="netease__search",
        schema=_schema("netease__search", {"query": {"type": "string"}, "limit": {"type": "integer"}}),
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
        intent_policies=(MUSIC_POLICY,),
    )


def _local_info_tool(calls: list[dict]) -> ToolSpec:
    def handler(arguments: dict, scope: Scope) -> dict:
        calls.append(dict(arguments))
        return {"status": "ok", "output": {"local_time": "2026-09-13T13:45:00+08:00", "timezone": "Asia/Shanghai"}}

    return ToolSpec(
        name="system__local_info",
        schema=_schema("system__local_info", {"kind": {"type": "string"}}),
        handler=handler,
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    )


class ScriptedProvider(LLMProvider):
    """按脚本顺序吐回复，并把每次请求记下来（用来断言 tools 有没有被屏蔽）。"""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.requests: list[dict] = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.requests.append({"messages": list(messages), "tools": tools})
        content = self.replies.pop(0) if self.replies else "好的，我明白了。"
        return ProviderResponse(message={"role": "assistant", "content": content}, usage={"completion_tokens": 1})


class _LoopTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _loop(self, provider, registry, *, store=None, scope: Scope = SCOPE) -> AgentLoop:
        """建一个不会污染项目根 agent_core.db 的 loop。

        store 默认给内存实现；传 None 之外的假值要显式写清楚，
        因为"不注入 store"正是闸门关闭的开关。
        """
        return AgentLoop(
            provider,
            registry,
            scope,
            run_store=SQLiteRunStore(self.directory / "agent.db"),
            confirmation_store=SQLiteConfirmationStore(self.directory / "agent.db"),
            intent_state_store=InMemoryIntentStateStore() if store is None else store,
        )


class ClassifierTests(unittest.TestCase):
    """评分公式的逐项核对：每个数字都能手算出来（常量见 intent.py）。"""

    def setUp(self) -> None:
        self.classifier = RuleIntentClassifier(POLICIES)

    def test_explicit_request_scores_highest(self) -> None:
        # 0.10 基线 + 0.35 动作词 + 0.30 对象词 + 0.23 参数齐全 = 0.98
        decision = self.classifier.classify("查询佛山的天气")
        self.assertEqual(decision.intent, "weather_query")
        self.assertAlmostEqual(decision.confidence, 0.98, places=4)
        self.assertEqual(decision.slots, {"location": "佛山"})
        self.assertFalse(decision.ambiguous)

    def test_missing_location_drops_into_ambiguous_band(self) -> None:
        # 0.10 + 0.35 + 0.30 - 0.10 缺参数 = 0.65
        decision = self.classifier.classify("天气查询")
        self.assertEqual(decision.intent, "weather_query")
        self.assertAlmostEqual(decision.confidence, 0.65, places=4)
        self.assertTrue(decision.ambiguous)

    def test_chatter_about_weather_bureau_is_conversation(self) -> None:
        # "天气局"被 negative_terms 排除，又没有动作词 → 0.10 - 0.05 = 0.05
        decision = self.classifier.classify("我昨天去天气局上班啦")
        self.assertEqual(decision.intent, CONVERSATION_INTENT)
        self.assertAlmostEqual(decision.confidence, 0.98, places=4)

    def test_question_about_weather_bureau_is_conversation(self) -> None:
        decision = self.classifier.classify("天气局是做什么的")
        self.assertEqual(decision.intent, CONVERSATION_INTENT)
        self.assertAlmostEqual(decision.confidence, 0.96, places=4)

    def test_music_request_extracts_clean_query(self) -> None:
        decision = self.classifier.classify("播放周杰伦的歌")
        self.assertEqual(decision.intent, "music_query")
        self.assertEqual(decision.slots, {"query": "周杰伦"})

    def test_topic_conflict_pulls_explicit_request_into_clarify_band(self) -> None:
        state = ConversationIntentState(
            topic="我昨天去天气局上班啦",
            topic_terms=("天气",),
            active_intent=CONVERSATION_INTENT,
        )
        # 基础 0.98，扣 0.20 冲突后夹到 0.78：仍是歧义档，不许直接调用
        decision = self.classifier.classify("查询佛山的天气", state=state)
        self.assertAlmostEqual(decision.confidence, 0.78, places=4)
        self.assertTrue(decision.ambiguous)
        self.assertIn("冲突", decision.reason)

    def test_topic_conflict_is_relaxed_after_non_question_reply(self) -> None:
        """旧话题本身不该永久拦住一个明确请求。

        会让这条测试失败的生产改动：忽略上一条助手消息，继续把所有同词话题
        都当成冲突。只有助手刚就这个话题提出问题时，用户的下一句才可能是在回答它。
        """
        state = ConversationIntentState(
            topic="我昨天去天气局上班啦",
            topic_terms=("天气",),
            active_intent=CONVERSATION_INTENT,
        )
        decision = self.classifier.classify(
            "查询佛山的天气",
            state=state,
            assistant_text="知道了，辛苦啦。",
        )
        self.assertAlmostEqual(decision.confidence, 0.98, places=4)
        self.assertFalse(decision.ambiguous)

    def test_topic_conflict_keeps_clarification_after_related_question(self) -> None:
        state = ConversationIntentState(
            topic="我昨天去天气局上班啦",
            topic_terms=("天气",),
            active_intent=CONVERSATION_INTENT,
        )
        decision = self.classifier.classify(
            "查询佛山的天气",
            state=state,
            assistant_text="你在天气局具体负责什么？",
        )
        self.assertAlmostEqual(decision.confidence, 0.78, places=4)
        self.assertTrue(decision.ambiguous)

    def test_no_policies_means_everything_is_conversation(self) -> None:
        decision = RuleIntentClassifier(()).classify("查询佛山的天气")
        self.assertEqual(decision.intent, CONVERSATION_INTENT)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = IntentGate(POLICIES)

    def test_high_confidence_calls(self) -> None:
        verdict = self.gate.decide(IntentDecision("weather_query", 0.98, {"location": "佛山"}))
        self.assertEqual(verdict.action, CALL)
        self.assertFalse(verdict.block_all)

    def test_ambiguous_asks(self) -> None:
        verdict = self.gate.decide(IntentDecision("weather_query", 0.65, ambiguous=True))
        self.assertEqual(verdict.action, CLARIFY)
        self.assertIn("候选动作", verdict.clarify_hint)

    def test_low_confidence_blocks_every_tool(self) -> None:
        # 规则评分区间里最低也有 0.65，这档是留给将来的 LLM 二次分类的：
        # 无论如何，低于 0.55 一律不许碰工具。
        verdict = self.gate.decide(IntentDecision("weather_query", 0.30))
        self.assertEqual(verdict.action, CHAT)
        self.assertTrue(verdict.block_all)

    def test_conversation_never_auto_calls(self) -> None:
        verdict = self.gate.decide(IntentDecision(CONVERSATION_INTENT, 0.98))
        self.assertEqual(verdict.action, CHAT)
        self.assertFalse(verdict.block_all)  # 模型想自己搜是它的判断，拦的只是规则误触发


class StateStoreTests(unittest.TestCase):
    """两种存储必须行为一致：TTL、轮数、作用域隔离。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "agent.db"
        self.stores = {
            "memory": InMemoryIntentStateStore(),
            "sqlite": SQLiteIntentStateStore(path),
        }

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_preserves_every_field(self) -> None:
        for name, store in self.stores.items():
            with self.subTest(store=name):
                store.save(SCOPE, ConversationIntentState(
                    topic="我昨天去天气局上班啦",
                    topic_terms=("天气",),
                    active_intent="weather_query",
                    slots={"location": "佛山"},
                    pending_intent="weather_query",
                    pending_question="你是想查天气吗？",
                    confidence=0.78,
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                    turns=1,
                ))
                state = store.load(SCOPE)
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(state.topic, "我昨天去天气局上班啦")
                self.assertEqual(state.topic_terms, ("天气",))
                self.assertEqual(state.slots, {"location": "佛山"})
                self.assertEqual(state.pending_intent, "weather_query")

    def test_expired_state_is_dropped(self) -> None:
        stale = (datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds")
        for name, store in self.stores.items():
            with self.subTest(store=name):
                store.save(SCOPE, ConversationIntentState(active_intent=CONVERSATION_INTENT, updated_at=stale))
                self.assertIsNone(store.load(SCOPE))

    def test_turn_limit_is_enforced(self) -> None:
        for name, store in self.stores.items():
            with self.subTest(store=name):
                store.save(SCOPE, ConversationIntentState(
                    active_intent=CONVERSATION_INTENT,
                    updated_at=datetime.now().isoformat(timespec="seconds"),
                    turns=5,
                ))
                self.assertIsNone(store.load(SCOPE))

    def test_scopes_are_isolated(self) -> None:
        for name, store in self.stores.items():
            with self.subTest(store=name):
                store.save(SCOPE, ConversationIntentState(
                    topic="A", updated_at=datetime.now().isoformat(timespec="seconds"),
                ))
                self.assertIsNone(store.load(OTHER_SCOPE))
                self.assertTrue(store.clear(SCOPE))
                self.assertIsNone(store.load(SCOPE))

    def test_summary_never_leaks_raw_json(self) -> None:
        text = render_state_summary(ConversationIntentState(
            topic="天气局工作",
            slots={"location": "佛山"},
            pending_intent="weather_query",
        ))
        self.assertIn("当前话题：天气局工作", text)
        self.assertIn("待确认意图", text)
        self.assertIn("佛山", text)
        self.assertNotIn("topic_terms", text)
        self.assertNotIn("{", text)


class IntentOrphanCleanupTests(unittest.TestCase):
    """定时清理：只删"会话已经不在了"的意图状态，归档会话的状态必须留着。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.database = Path(self._tmp.name) / "agent.db"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clear_orphans_removes_only_deleted_conversation_states(self) -> None:
        from agent_core.conversation_repository import ConversationRepository

        repository = ConversationRepository(self.database)
        store = SQLiteIntentStateStore(self.database)
        active = Scope("owner", "web", "active", "companion")
        archived = Scope("owner", "web", "archived", "companion")
        deleted = Scope("owner", "web", "deleted", "companion")
        for scope in (active, archived, deleted):
            repository.ensure(scope)
            store.save(scope, ConversationIntentState(
                active_intent="weather_query",
                pending_intent="weather_query",
                updated_at=datetime.now().isoformat(timespec="seconds"),
            ))
            # 预热读缓存：clear() 走的是同一个 _cache，孤儿清理漏了缓存的话
            # 本进程还会把已删会话的"待确认意图"读出来接着用。
            store.load(scope)
        repository.archive(archived)
        repository.delete(deleted)

        self.assertEqual(store.clear_orphans(), 1)

        # 用"重建父行再读"来证明缓存也清了：库里那行已经没了，
        # 只有缓存没清才会读到旧状态。
        repository.ensure(deleted)
        self.assertIsNone(store.load(deleted))
        self.assertIsNone(SQLiteIntentStateStore(self.database).load(deleted))
        for scope in (active, archived):
            state = store.load(scope)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.pending_intent, "weather_query")

    def test_clear_orphans_is_idempotent_when_nothing_was_deleted(self) -> None:
        from agent_core.conversation_repository import ConversationRepository

        repository = ConversationRepository(self.database)
        store = SQLiteIntentStateStore(self.database)
        repository.ensure(SCOPE)
        store.save(SCOPE, ConversationIntentState(
            active_intent="weather_query", updated_at=datetime.now().isoformat(timespec="seconds"),
        ))
        self.assertEqual(store.clear_orphans(), 0)
        self.assertEqual(store.clear_orphans(), 0)
        self.assertIsNotNone(store.load(SCOPE))


class IntentLoopTests(_LoopTestCase):
    async def test_explicit_weather_request_calls_the_weather_tool(self) -> None:
        """明确的天气请求直接查天气 —— 不再拼成"佛山 天气"丢给网页搜索。

        以前那个拼串动作会让用户拿到一堆网页链接；现在参数是结构化的地名，
        结果也是结构化的（天气 / 气温 / 风级），模板直接出固定格式的句子。
        """
        calls: list[dict] = []
        provider = ScriptedProvider()
        loop = self._loop(provider, self._registry(_weather_tool(calls)))
        outcome = await loop.run("查询佛山的天气")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(calls, [{"area": "佛山"}])
        # 快路径是**零模型**的：连"润色"那一趟都不走 —— 这正是方案里
        # "不调用模型"那条要求。
        self.assertEqual(provider.requests, [])

    async def test_weather_bureau_question_does_not_call_search(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("天气局是负责气象观测的机构。")
        loop = self._loop(provider, self._registry(_web_tool(calls)))
        outcome = await loop.run("天气局是干什么的")
        self.assertEqual(calls, [])
        self.assertEqual(outcome.reply, "天气局是负责气象观测的机构。")

    async def test_weather_bureau_chat_hides_and_rejects_weather_search(self) -> None:
        """模型即使违规返回被屏蔽的搜索调用，也不能真的执行。"""
        calls: list[dict] = []

        class OvereagerProvider(LLMProvider):
            def __init__(self) -> None:
                self.requests: list[dict] = []
                self.count = 0

            async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
                self.requests.append({"tools": tools})
                self.count += 1
                if self.count == 1:
                    return ProviderResponse(message={
                        "role": "assistant",
                        "tool_calls": [{
                            "id": "forbidden-weather-search",
                            "type": "function",
                            "function": {
                                "name": "search__web",
                                "arguments": '{"query":"天气局"}',
                            },
                        }],
                    }, usage={"completion_tokens": 1})
                return ProviderResponse(message={"role": "assistant", "content": "天气局负责气象服务。"}, usage={"completion_tokens": 1})

        provider = OvereagerProvider()
        loop = self._loop(provider, self._registry(_web_tool(calls)))
        outcome = await loop.run("天气局是干什么的")

        self.assertEqual(calls, [])
        self.assertIsNone(provider.requests[0]["tools"])
        self.assertEqual(outcome.reply, "天气局负责气象服务。")

    async def test_topic_conflict_asks_instead_of_calling(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("上班辛苦了。", "你是想查佛山的天气，还是继续聊天气局的工作？")
        store = InMemoryIntentStateStore()
        loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

        await loop.run("我昨天去天气局上班啦")
        outcome = await loop.run("天气查询")

        self.assertEqual(calls, [], "歧义时不能自作主张调工具")
        self.assertEqual(outcome.reply, "你是想查佛山的天气，还是继续聊天气局的工作？")
        state = store.load(SCOPE)
        assert state is not None
        self.assertEqual(state.pending_intent, "weather_query")
        self.assertEqual(state.pending_question, outcome.reply)

    async def test_non_question_assistant_history_does_not_block_explicit_weather_request(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("佛山天气信息已查到。")
        store = InMemoryIntentStateStore()
        store.save(SCOPE, ConversationIntentState(
            topic="我昨天去天气局上班啦",
            topic_terms=("天气",),
            active_intent=CONVERSATION_INTENT,
            updated_at=datetime.now().isoformat(timespec="seconds"),
        ))
        loop = self._loop(provider, self._registry(_weather_tool(calls)), store=store)

        await loop.run(AgentRequest(
            "查询佛山的天气",
            scope=SCOPE,
            history=[{"role": "assistant", "content": "知道了，辛苦啦。"}],
        ))

        self.assertEqual(calls, [{"area": "佛山"}])

    async def test_explicit_weather_request_is_not_blocked_by_topic_conflict(self) -> None:
        """上一句聊的是"天气局"，下一句"查询佛山的天气"仍然直接执行。

        改造前这条路要过话题冲突判定：两句都含"天气"，闸门会判成歧义、先追问一句。
        可用户已经把城市说清楚了，再问一次纯粹是耽误。天气改走确定性快路径之后
        它连闸门都不经过，这个停顿自然就没了。
        """
        calls: list[dict] = []
        provider = ScriptedProvider("上班辛苦了。")
        store = InMemoryIntentStateStore()
        loop = self._loop(provider, self._registry(_weather_tool(calls)), store=store)

        await loop.run("我昨天去天气局上班啦")
        await loop.run("查询佛山的天气")

        self.assertEqual(calls, [{"area": "佛山"}], "说了城市就该直接查，不该再追问一遍")

    async def test_pending_weather_without_location_stays_in_clarification(self) -> None:
        """A generic confirmation must not turn a missing city into a malformed search."""
        calls: list[dict] = []
        provider = ScriptedProvider(
            "上班辛苦了。",
            "你是想查天气，还是继续聊天气局？",
            "你想查询哪个城市的天气？",
        )
        store = InMemoryIntentStateStore()
        loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

        await loop.run("我昨天去天气局上班啦")
        await loop.run("天气查询")
        outcome = await loop.run("查天气")

        self.assertEqual(calls, [])
        self.assertEqual(outcome.reply, "你想查询哪个城市的天气？")

    async def test_follow_up_can_decline_pending_intent(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("上班辛苦了。", "你是想查天气，还是继续聊天气局？", "那我们接着聊上班的事。")
        store = InMemoryIntentStateStore()
        loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

        await loop.run("我昨天去天气局上班啦")
        await loop.run("查询佛山的天气")
        await loop.run("我说的是上班的事")

        self.assertEqual(calls, [])
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent)
        self.assertEqual(state.active_intent, CONVERSATION_INTENT)

    @staticmethod
    def _seed_pending(store, *, intent, slots, question) -> None:
        store.save(SCOPE, ConversationIntentState(
            active_intent=intent,
            slots=dict(slots),
            pending_intent=intent,
            pending_question=question,
            updated_at=datetime.now().isoformat(timespec="seconds"),
            turns=1,
        ))

    async def test_location_only_reply_resumes_pending_weather(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider()
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(provider, self._registry(_weather_tool(calls)), store=store)

        await loop.run("北京")

        self.assertEqual(calls, [{"area": "北京"}])
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent)

    async def test_decline_clears_pending_intent_without_calling_tool(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("好，那我们不查了。")
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

        await loop.run("算了，不查了")

        self.assertEqual(calls, [])
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent)
        self.assertEqual(state.active_intent, CONVERSATION_INTENT)

    async def test_unrecognized_short_reply_keeps_pending_and_reasks(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("那你想查哪个城市的天气呢？")
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

        outcome = await loop.run("不知道")

        self.assertEqual(calls, [])
        state = store.load(SCOPE)
        assert state is not None
        self.assertEqual(state.pending_intent, "weather_query")
        self.assertIn("城市", outcome.reply)

    async def test_query_only_reply_resumes_pending_music(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider()
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="music_query", slots={}, question="你想听谁的歌？")
        loop = self._loop(provider, self._registry(_music_tool(calls)), store=store)

        await loop.run("周杰伦")

        self.assertTrue(calls, "只答歌手名也应恢复待执行的歌曲搜索")
        self.assertEqual(calls[0].get("query"), "周杰伦")
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent)

    # ── 追问后的"自然改口"：句中含"不是/不用"不等于撤回 ──
    # 这一组用例来自真实误判："不是北京，是上海"被当成撤回，用户被迫重说一遍。

    async def test_corrected_location_with_negation_still_queries(self) -> None:
        """`不是北京，是上海` 是改口，不是撤回 —— 应改查上海。"""
        calls: list[dict] = []
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(ScriptedProvider(), self._registry(_weather_tool(calls)), store=store)

        await loop.run("不是北京，是上海")

        self.assertEqual(calls, [{"area": "上海"}], "改口句里的新地点必须被用上")
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent, "改口是完成一次追问，不是放弃")

    async def test_restated_location_still_queries(self) -> None:
        """`我说的是北京` 是重申，不是换话题。"""
        calls: list[dict] = []
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(ScriptedProvider(), self._registry(_weather_tool(calls)), store=store)

        await loop.run("我说的是北京")

        self.assertEqual(calls, [{"area": "北京"}])
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent)

    async def test_swapped_location_with_negation_still_queries(self) -> None:
        """`不用北京，查上海` 是换地点，不是取消查询。"""
        calls: list[dict] = []
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(ScriptedProvider(), self._registry(_weather_tool(calls)), store=store)

        await loop.run("不用北京，查上海")

        self.assertEqual(calls, [{"area": "上海"}])

    async def test_corrected_music_query_still_searches(self) -> None:
        """`不是五月天，是周杰伦` 在音乐意图上同样应改查周杰伦。"""
        calls: list[dict] = []
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="music_query", slots={}, question="你想听谁的歌？")
        loop = self._loop(ScriptedProvider(), self._registry(_music_tool(calls)), store=store)

        await loop.run("不是五月天，是周杰伦")

        self.assertTrue(calls, "改口后的歌手名必须触发搜索")
        self.assertEqual(calls[0].get("query"), "周杰伦")

    async def test_explicit_cancel_still_clears_pending(self) -> None:
        """明确的整句取消仍然要撤销待确认意图。"""
        for text in ("算了，不查了", "取消", "不用了"):
            with self.subTest(text=text):
                calls: list[dict] = []
                store = InMemoryIntentStateStore()
                self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
                loop = self._loop(ScriptedProvider("好，那我们不查了。"), self._registry(_web_tool(calls)), store=store)

                await loop.run(text)

                self.assertEqual(calls, [], f"{text!r} 不该调工具")
                state = store.load(SCOPE)
                assert state is not None
                self.assertIsNone(state.pending_intent, f"{text!r} 应清掉 pending")
                self.assertEqual(state.active_intent, CONVERSATION_INTENT)

    async def test_topic_switch_sentence_still_clears_pending(self) -> None:
        """`我说的是上班的事` 是换话题，不是地名，必须保持原来的撤回语义。"""
        calls: list[dict] = []
        provider = ScriptedProvider("那我们接着聊上班的事。")
        store = InMemoryIntentStateStore()
        self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
        loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

        await loop.run("我说的是上班的事")

        self.assertEqual(calls, [], "换话题句不该被当成地名去查天气")
        state = store.load(SCOPE)
        assert state is not None
        self.assertIsNone(state.pending_intent)
        self.assertEqual(state.active_intent, CONVERSATION_INTENT)

    async def test_non_location_work_talk_is_not_a_city(self) -> None:
        """`工作/话题/聊天` 这类词明显不是地名，仍应保持追问而不是拿它去查。"""
        for text in ("我说的是工作", "我说的话题不是这个", "你怎么老想聊天"):
            with self.subTest(text=text):
                calls: list[dict] = []
                provider = ScriptedProvider("那你想查哪个城市的天气呢？")
                store = InMemoryIntentStateStore()
                self._seed_pending(store, intent="weather_query", slots={}, question="你想查询哪个城市的天气？")
                loop = self._loop(provider, self._registry(_web_tool(calls)), store=store)

                await loop.run(text)

                self.assertEqual(calls, [], f"{text!r} 里的词不是地名，不该触发查询")

    async def test_expired_state_does_not_block_explicit_request(self) -> None:
        calls: list[dict] = []
        store = InMemoryIntentStateStore()
        store.save(SCOPE, ConversationIntentState(
            topic="我昨天去天气局上班啦",
            topic_terms=("天气",),
            active_intent=CONVERSATION_INTENT,
            updated_at=(datetime.now() - timedelta(minutes=30)).isoformat(timespec="seconds"),
        ))
        loop = self._loop(ScriptedProvider(), self._registry(_weather_tool(calls)), store=store)
        await loop.run("查询佛山的天气")
        self.assertEqual(calls, [{"area": "佛山"}])

    async def test_failed_weather_tool_reports_a_stable_failure(self) -> None:
        """工具没调通时不能假装查到了。

        天气改走确定性快路径之后不再写意图状态（那条链路只属于闸门），所以这里
        验证更靠近用户的那一层：回复是固定的中文失败话术，且不含稳定码或接口原文。
        "意图状态不假装完成"那一层由走闸门的用例继续守着。
        """
        calls: list[dict] = []
        loop = self._loop(ScriptedProvider(), self._registry(_weather_tool(calls, failing=True)))
        outcome = await loop.run("查询佛山的天气")

        self.assertEqual(calls, [{"area": "佛山"}], "工具确实被调用了，但没调通")
        self.assertEqual(outcome.status, "completed")
        self.assertIn("天气服务", outcome.reply, "失败要说清楚，别含糊")
        self.assertNotIn("weather_unavailable", outcome.reply, "稳定码只进日志，不进气泡")

    async def test_clarification_lands_in_public_history(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("上班辛苦了。", "你是想查天气，还是继续聊天气局？")
        loop = self._loop(provider, self._registry(_web_tool(calls)))

        await loop.run("我昨天去天气局上班啦")
        await loop.run("天气查询")

        # 公开历史不能缺这一轮：用户说了"天气查询"，界面上必须看到它和对应的追问
        self.assertEqual(loop.public_messages[-2], {"role": "user", "content": "天气查询"})
        self.assertEqual(loop.public_messages[-1]["role"], "assistant")
        self.assertEqual(loop.public_messages[-1]["content"], "你是想查天气，还是继续聊天气局？")

    async def test_strong_routes_still_run_before_the_gate(self) -> None:
        calls: list[dict] = []
        web_calls: list[dict] = []
        provider = ScriptedProvider()
        registry = self._registry(_local_info_tool(calls), _web_tool(web_calls))
        loop = self._loop(provider, registry)
        outcome = await loop.run("现在几点")
        self.assertEqual(calls, [{"kind": "time"}])
        self.assertEqual(web_calls, [])
        self.assertIn("13:45", outcome.reply)

    async def test_gate_off_keeps_legacy_behaviour(self) -> None:
        """没注入 store 时闸门关闭，原来的事实查询路由该调还是调。"""
        calls: list[dict] = []
        from agent_core.contracts import AgentRequest  # noqa: F401  (说明这轮走的是同一入口)

        registry = self._registry(_web_tool(calls))
        loop = AgentLoop(
            ScriptedProvider(),
            registry,
            SCOPE,
            run_store=SQLiteRunStore(self.directory / "agent.db"),
            confirmation_store=SQLiteConfirmationStore(self.directory / "agent.db"),
        )
        await loop.run("帮我搜索一下佛山美食")
        # _strip_search_prefix 会剥掉"帮我搜索一下"，剩下的才是查询串；
        # 这里断言的重点是：闸门没开时，原来的事实查询路由照旧触发。
        # 刻意**不用**天气句：天气现在有自己的确定性快路径，而它排在闸门**之前**，
        # 用天气句测不出"闸门关闭时的旧行为"。
        self.assertEqual(calls, [{"query": "佛山美食"}])

    async def test_low_confidence_hides_the_whole_tool_list(self) -> None:
        calls: list[dict] = []
        provider = ScriptedProvider("我就当你在聊天啦。")
        loop = self._loop(provider, self._registry(_web_tool(calls)))
        loop._tools_disabled = True  # 等价于闸门判成 block_all
        await loop.run("随便说点什么")
        self.assertEqual(calls, [])
        self.assertIsNone(provider.requests[-1]["tools"])

    @staticmethod
    def _registry(*specs: ToolSpec) -> ToolRegistry:
        registry = ToolRegistry()
        for spec in specs:
            registry.register(spec)
        return registry


if __name__ == "__main__":
    unittest.main()
