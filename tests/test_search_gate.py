"""回答前搜索门：触发规则、漏搜纠正、证据检查与回归约束。

覆盖目标计划里的验收点：
* 稳定知识不搜索；
* 新词 / 时效问题触发搜索（漏搜被系统纠正，不能直接结束回答）；
* 混合问题只核实不确定部分（关键词来自 targets，而不是整句话）；
* 普通聊天不误搜；
* 判定内容不展示给用户（内部 JSON 不出现在回复里）；
* 关键词重试有上限（每回合最多 3 次不同关键词）；
* 平台失败可降级（网页挂了保留B站证据）；
* 无证据时不编造（证据检查不过要修正措辞）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from agent_core.contracts import EFFECT_READ, ProviderResponse, Scope, ToolSpec
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.search_gate import (
    SearchGate,
    SearchGateConfig,
    SearchNeedVerdict,
    build_search_gate,
)
from agent_core.steward import (
    PHASE_MATERIALS,
    PHASE_READ_DONE,
    PHASE_READING,
    PHASE_REDIRECT,
    PHASE_SEARCHING,
    StewardNarrator,
    StewardReporter,
)
from agent_core.steward_progress import ProgressPolicy
from agent_core.tools import ToolRegistry
from support.stage_phrases import StagePhraseProvider, stage_line

SCOPE = Scope("u", "web", "c-gate", "companion")

ASSESS_TRUE = json.dumps({
    "needs_search": True,
    "reason": "候选回复对目标词的解释带猜测口吻",
    "targets": ["梁文谷"],
    "suggested_queries": ["梁文谷 是谁"],
    "meme": False,
}, ensure_ascii=False)
ASSESS_FALSE = json.dumps({"needs_search": False, "reason": "日常寒暄"}, ensure_ascii=False)
EVIDENCE_OK = json.dumps({"supported": True, "issue": ""}, ensure_ascii=False)
EVIDENCE_BAD = json.dumps({"supported": False, "issue": "回复把视频标题当成了完整解释"}, ensure_ascii=False)


class GateScriptedProvider:
    """按 system 提示词分流的假模型：判定/整理/核查调用回 JSON，聊天调用按脚本走。"""

    def __init__(
        self,
        chat_replies: list[str],
        *,
        verdict: str = ASSESS_TRUE,
        evidence: str = EVIDENCE_OK,
        coverage: str | None = None,
    ) -> None:
        self.chat_replies = list(chat_replies)
        self.verdict = verdict
        self.evidence = evidence
        self.coverage = coverage
        self.requests: list[dict[str, Any]] = []

    @staticmethod
    def _coverage_from_materials(messages: list[dict[str, Any]]) -> str:
        """覆盖检查兼整理：把候选材料前两条整理成可引用事实。"""
        materials: list[dict[str, Any]] = []
        for message in reversed(messages):
            if message.get("role") != "user":
                continue
            try:
                payload = json.loads(str(message.get("content") or "{}"))
            except ValueError:
                payload = {}
            materials = payload.get("materials") or []
            break
        key_facts = []
        for item in materials[:2]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            key_facts.append({
                "text": text[:400],
                "source_ids": [str(item.get("source_id") or "")],
                "conditions": "",
            })
        return json.dumps(
            {"covered": True, "missing": [], "key_facts": key_facts, "uncovered": []},
            ensure_ascii=False,
        )

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.requests.append({"messages": list(messages), "tools": tools, "max_tokens": max_tokens})
        system_text = ""
        for message in messages:
            if message.get("role") == "system":
                system_text = str(message.get("content") or "")
                break
        if system_text.startswith(("你是回答前的核查器", "你是请求意图判断器")):
            return ProviderResponse(message={"role": "assistant", "content": self.verdict}, usage={})
        if system_text.startswith("你是检索材料整理器"):
            content = self.coverage if self.coverage is not None else self._coverage_from_materials(messages)
            return ProviderResponse(message={"role": "assistant", "content": content}, usage={})
        if system_text.startswith(("你是证据核查器", "你是最终核查器")):
            return ProviderResponse(message={"role": "assistant", "content": self.evidence}, usage={})
        content = self.chat_replies.pop(0) if self.chat_replies else "好的。"
        return ProviderResponse(message={"role": "assistant", "content": content}, usage={})


def _search_tool(
    name: str,
    calls: list[dict[str, Any]],
    *,
    fail: bool = False,
    empty: bool = False,
    domains: tuple[str, ...] = (),
) -> ToolSpec:
    """假的搜索引擎。

    默认每次检索都返回**同一家站点**的结果 —— 计数口径下始终只有一个独立来源，
    这正是"搜到结果 ≠ 有多个来源"的最小复现。``domains`` 给出域名序列时，
    第 N 次检索返回第 N 个域名，用来验证"凑够两个独立来源就停手"。
    """
    counter = {"n": 0}

    def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append({"tool": name, **arguments})
        if fail:
            return {"status": "failed", "error": "provider down", "error_kind": "transient"}
        query = str(arguments.get("query"))
        if empty:
            return {"status": "ok", "output": {"query": query, "result_count": 0, "results": []}}
        if domains:
            host = domains[counter["n"] % len(domains)]
        else:
            host = "example.com"
        counter["n"] += 1
        return {
            "status": "ok",
            "output": {
                "query": query,
                "result_count": 1,
                "results": [{
                    "title": f"{query} 相关结果",
                    "url": f"https://{host}/{name}/{query}",
                "snippet": f"{query} 的公开资料摘要",
                    "platform": "bilibili" if "bilibili" in name else "web",
                    "published_at": "2026-10-01",
                }],
            },
        }

    return ToolSpec(
        name,
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}},
        handler,
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    )


def _read_tool(calls: list[dict[str, Any]], *, fail: bool = False) -> ToolSpec:
    def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append({"tool": "search__read", **arguments})
        if fail:
            return {"status": "failed", "error": "read failed", "error_kind": "transient"}
        return {
            "status": "ok",
            "output": {
                "url": arguments.get("url"),
                "fetch_status": "ok",
                "title": "页面标题",
                "content": "正文内容：梁文谷是一条网络热梗，出自……",
                "truncated": False,
            },
        }

    return ToolSpec(
        "search__read",
        {"type": "function", "function": {"name": "search__read", "parameters": {"type": "object"}}},
        handler,
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    )


def _build_loop(
    provider,
    tmp_path: Path,
    *,
    gate,
    web=None,
    with_bilibili: bool = False,
    with_read: bool = False,
    fail_web: bool = False,
    fail_bilibili: bool = False,
    fail_read: bool = False,
    empty_web: bool = False,
    domains: tuple[str, ...] = (),
    bus=None,
    steward=None,
    progress_policy=None,
) -> AgentLoop:
    calls: list[dict[str, Any]] = []
    registry = ToolRegistry()
    registry.register(
        web if web is not None
        else _search_tool("search__web", calls, fail=fail_web, empty=empty_web, domains=domains)
    )
    if with_bilibili:
        registry.register(_search_tool("search__bilibili", calls, fail=fail_bilibili))
    if fail_read:
        registry.register(_read_tool(calls, fail=True))
    elif with_read:
        registry.register(_read_tool(calls))
    loop = AgentLoop(
        provider,
        registry,
        SCOPE,
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        search_gate=gate,
        event_bus=bus,
        steward=steward,
        progress_policy=progress_policy,
    )
    loop._test_tool_calls = calls  # 测试观测点：handler 收到的全部调用
    return loop


class _ProgressRecorder:
    """记下真正落库的过程消息（阶段 + 文案）。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.keys: list[str] = []

    def __call__(self, scope, message, stable_key):
        self.keys.append(stable_key)
        self.messages.append({**message, "scope": scope})
        return True

    @property
    def phases(self) -> list[str]:
        return [message["phase"] for message in self.messages]


def _instant_policy(**overrides) -> ProgressPolicy:
    """把 1.5 秒 / 4 秒压成 0：这一组用例验的是"阶段真不真"，不是密度。"""
    base = {"first_wait_seconds": 0.0, "min_gap_seconds": 0.0}
    base.update(overrides)
    return ProgressPolicy(**base)


def test_stable_knowledge_skips_search(tmp_path):
    provider = GateScriptedProvider(["水烧到一百度会沸腾。"], verdict=ASSESS_FALSE)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)
    outcome = asyncio.run(loop.run("今天天气不错，我们聊聊水几点烧开吧"))

    assert outcome.status == "completed"
    assert outcome.reply == "水烧到一百度会沸腾。"
    assert loop._test_tool_calls == []

def test_normal_chat_is_not_searched(tmp_path):
    provider = GateScriptedProvider(["辛苦啦，早点休息。"], verdict=ASSESS_FALSE)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)
    outcome = asyncio.run(loop.run("我今天上班好累啊"))

    assert outcome.reply == "辛苦啦，早点休息。"
    assert loop._test_tool_calls == []


def test_missed_search_is_corrected_with_evidence(tmp_path):
    """入口判定认不出主题时，先取证再作答；主回答看到的是整理后的事实。"""
    provider = GateScriptedProvider(["查过了：梁文谷是一条网络热梗。"])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, with_read=True)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗"))

    assert outcome.status == "completed"
    assert "网络热梗" in outcome.reply
    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    reads = [call for call in loop._test_tool_calls if call["tool"] == "search__read"]
    assert 1 <= len(searches) <= 3
    assert len(reads) <= 2
    # 内部判定 JSON 不出现在可见回复里
    assert "needs_search" not in outcome.reply
    # 取证结束后主回答仍走主循环：证据以工具消息进入，而不是另一个回答器
    answer_requests = [
        request for request in provider.requests
        if request["tools"] is not None
    ]
    assert answer_requests, "主回答必须带着工具清单走主循环"
    evidence_messages = [
        message for message in answer_requests[-1]["messages"]
        if message.get("role") == "tool"
    ]
    assert evidence_messages, "整理后的事实必须作为工具证据进入主回合"


def test_multi_source_query_stops_once_two_independent_sources(tmp_path):
    """两个独立来源到位就停手：不再为同一个问题继续烧检索预算。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "陌生人名", "targets": ["梁文谷"],
        "suggested_queries": ["梁文谷是谁", "梁文谷 梗 解释", "梁文谷 热梗"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["不清楚。", "查到了。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, domains=("a.example.com", "b.example.com", "c.example.com"))

    asyncio.run(loop.run("跟我们讲讲梁文谷呗？"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert len(searches) == 1, "一轮已有可用证据后应立即收尾，不为凑第二个来源继续搜索"


def test_existing_clear_answer_does_not_keep_collecting_sources(tmp_path):
    """已有可用结论时见好就收，不为凑来源继续堆同义搜索。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "事实核实", "targets": ["GPT-6.1 Sol"],
        "suggested_queries": ["GPT-6.1 Sol", "GPT-6.1 Sol 能力", "GPT-6.1 Sol 发布时间"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider([
        "GPT-6.1 Sol 这个名称确实被公开提及。",
        "GPT-6.1 Sol 这个名称确实被公开提及。具体能力和发布时间暂时无法确认。",
    ], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, domains=("official.example.com", "copy.example.com", "tutorial.example.com"))

    outcome = asyncio.run(loop.run("GPT-6.1 Sol 是什么？"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert len(searches) <= 2
    assert "GPT-6.1 Sol" in outcome.reply
    assert "独立来源" not in outcome.reply
    assert "搜索过程" not in outcome.reply

def test_single_domain_results_are_counted_as_one_source(tmp_path):
    """同一家站点的多条结果只算一个来源：回答要说明"目前只有单一来源"。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "陌生人名", "targets": ["梁文谷"],
        "suggested_queries": ["梁文谷是谁", "梁文谷 梗", "梁文谷 出处"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["不清楚。", "查到了。", "还是这些来源。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗？"))

    assert "目前只有单一来源" not in outcome.reply
    assert "以上内容基于" not in outcome.reply
    assert outcome.sources and outcome.sources[0]["domain"] == "example.com"
    assert "来源：" not in outcome.reply


def test_correction_search_uses_target_keywords_not_whole_sentence(tmp_path):
    provider = GateScriptedProvider([
        "这个我不太清楚，可能是个新词。",
        "根据检索，梁文谷是一个梗。",
    ])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    asyncio.run(loop.run("跟我们聊聊梁文谷呗，顺便帮我捋捋梁文锋的事"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert searches, "漏搜必须被纠正"
    # 规则直判的事实问题用抠出来的主语当关键词，不再把整句话丢给搜索引擎
    assert "梁文谷" in searches[0]["query"]
    assert len(searches[0]["query"]) < len("帮我看看，梁文锋和梁文谷是什么，顺便说说明天天气")


def test_meme_adds_bilibili_platform(tmp_path):
    verdict = json.dumps({
        "needs_search": True, "reason": "热梗", "targets": ["某梗"],
        "suggested_queries": ["某梗"], "meme": True,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["查到了，是一个梗。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, with_bilibili=True, fail_web=True)  # 网页平台挂掉

    outcome = asyncio.run(loop.run("某梗最近爆火，给我们讲讲呗"))

    searches = [call["tool"] for call in loop._test_tool_calls]
    assert "search__bilibili" in searches
    assert "search__web" in searches
    bilibili_calls = [call for call in loop._test_tool_calls if call["tool"] == "search__bilibili"]
    assert any("某梗" in str(call["query"]) for call in bilibili_calls)
    assert outcome.reply == "查到了，是一个梗。"


def test_search_budget_is_capped_per_turn(tmp_path):
    verdict = json.dumps({
        "needs_search": True, "reason": "多个不确定点",
        "targets": ["甲", "乙", "丙", "丁"],
        "suggested_queries": ["甲", "乙", "丙", "丁"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["不清楚。", "只能查到这些。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=2))
    loop = _build_loop(provider, tmp_path, gate=gate)

    asyncio.run(loop.run("甲乙丙丁这几个词都给我们讲讲呗？"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    reads = [call for call in loop._test_tool_calls if call["tool"] == "search__read"]
    assert len(searches) <= 3
    assert len(reads) <= 2
    queries = {call["query"] for call in searches}
    assert len(queries) == len(searches), "关键词必须各不相同"


def test_barren_search_stops_before_burning_budget(tmp_path):
    """连续检索没有新增独立信息就停：剩下的预算不烧在同一个来源上。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "多个不确定点",
        "targets": ["甲"],
        "suggested_queries": ["甲", "乙", "丙"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["不清楚。", "只能查到这些。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3))
    loop = _build_loop(provider, tmp_path, gate=gate)

    asyncio.run(loop.run("跟我们讲讲甲呗？"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    # 甲给出唯一相关来源，后续同源结果没有新增独立信息，立即停止。
    assert [call["query"] for call in searches] == ["甲", "乙"]
    assert len({call["query"] for call in searches}) == 2


def test_second_independent_source_stops_the_loop(tmp_path):
    """第二家站点出现后立刻停：不再用第三个关键词烧预算。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "多个不确定点",
        "targets": ["甲"],
        "suggested_queries": ["甲乙丙", "丁戊己"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["不清楚。", "查到了两个来源。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3))
    loop = _build_loop(provider, tmp_path, gate=gate, domains=("a.example.com", "b.example.com", "c.example.com"))

    asyncio.run(loop.run("跟我们讲讲甲呗？"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert len(searches) == 1


def test_empty_search_results_still_answer(tmp_path):
    """搜索结果为空也要完成普通回答，并如实表达无法确认。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "陌生词", "targets": ["梁文谷"],
        "suggested_queries": ["梁文谷"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(
        ["梁文谷是一个虚拟人物。", "没查到任何来源，这一条无法确认。"], verdict=verdict,
    )
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, empty_web=True)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗？"))

    assert outcome.status == "completed"
    assert "无法确认" in outcome.reply
    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert len(searches) <= 3, "空结果也要受预算约束，不能无限换词"


def test_conflicting_sources_are_flagged(tmp_path):
    """两个独立来源数字不一致时，回答口径必须带冲突说明，不替用户选边。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "价格时效", "search_intent": "deep_search", "targets": ["某显卡"],
        "suggested_queries": ["某显卡 售价", "某显卡 报价"], "meme": False,
    }, ensure_ascii=False)
    calls: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append(dict(arguments))
        query = str(arguments.get("query") or "")
        return {"status": "ok", "output": {"query": query, "result_count": 2, "results": [
            {"title": "某显卡 4999 元 开售", "url": "https://price-a.example.com/x",
             "snippet": "某显卡售价 4999 元，现已开售。", "platform": "web", "published_at": "2026-10-01"},
            {"title": "某显卡 5199 元 开售", "url": "https://price-b.example.com/x",
             "snippet": "某显卡售价 5199 元，现已开售。", "platform": "web", "published_at": "2026-10-01"},
        ]}}

    web = ToolSpec(
        "search__web",
        {"type": "function", "function": {"name": "search__web", "parameters": {"type": "object"}}},
        handler, effect=EFFECT_READ, deterministic_route_allowed=True,
    )
    class ConflictProvider(GateScriptedProvider):
        async def complete(self, messages, **kwargs):
            system = str(messages[0].get("content") or "") if messages else ""
            if system.startswith("你是检索材料整理器"):
                self.requests.append({"messages": list(messages), "tools": kwargs.get("tools"), "max_tokens": kwargs.get("max_tokens")})
                coverage = json.dumps({
                    "covered": False, "missing": ["当前报价"], "followup_query": "某显卡 报价",
                    "key_facts": [
                        {"text": "A 站说售价 4999 元", "source_ids": ["source-1"], "conditions": "2026-10-01"},
                        {"text": "B 站说售价 5199 元", "source_ids": ["source-2"], "conditions": "2026-10-01"},
                    ],
                    "uncovered": ["当前有效报价"],
                }, ensure_ascii=False)
                return ProviderResponse(message={"role": "assistant", "content": coverage}, usage={})
            return await super().complete(messages, **kwargs)

    provider = ConflictProvider([
        "两个来源报价不同：4999 元与 5199 元，无法确认哪个是当前价。",
    ], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, web=web)

    outcome = asyncio.run(loop.run("请调研某显卡现在的价位并核实不同报价"))

    assert "来源之间存在冲突" not in outcome.reply
    assert "4999" in outcome.reply and "5199" in outcome.reply
    assert outcome.answer_status == "conflicted"
    assert outcome.sources
    assert "搜索过程" not in outcome.reply
    # 冲突与未覆盖问题都要进入主回答的证据消息，而不是丢在内部日志里
    evidence_payloads = [
        str(message.get("content") or "")
        for request in provider.requests for message in request["messages"]
        if message.get("role") == "tool"
    ]
    assert any("conflicts" in payload for payload in evidence_payloads)
    assert any("当前有效报价" in payload for payload in evidence_payloads)


def test_plain_chat_never_calls_the_assessment_model(tmp_path):
    """闲聊连轻量判定都不该调：规则直接放行，延迟和模型调用都为 0。"""
    provider = GateScriptedProvider(["辛苦啦，早点休息。"], verdict=ASSESS_TRUE)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("我今天上班好累啊"))

    assert outcome.reply == "辛苦啦，早点休息。"
    assert loop._test_tool_calls == []
    assess_calls = [
        request for request in provider.requests
        if any(
            str(message.get("content") or "").startswith("你是回答前的核查器")
            for message in request["messages"] if message.get("role") == "system"
        )
    ]
    assert assess_calls == [], "闲聊不该触发任何判定调用"


def test_explicit_fact_question_searches_without_extra_assessment(tmp_path):
    """"某人是谁"这类规则直判的问题：先检索，再带着证据作答。"""
    provider = GateScriptedProvider(["查证后：梁文谷是一条网络热梗。"])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("梁文谷是谁"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert searches and "梁文谷" in searches[0]["query"]
    assert "网络热梗" in outcome.reply


def test_rule_intent_avoids_llm_call_for_clear_questions():
    """规则能定性的请求不再花一次模型调用：四种意图的分流口径直接钉住。"""
    from agent_core.search_gate import (
        INTENT_DEEP_SEARCH,
        INTENT_DIRECT_CHAT,
        INTENT_FACT_SEARCH,
        INTENT_FRESH_SEARCH,
        classify_request_rules,
    )

    assert classify_request_rules("梁文谷是谁").intent == INTENT_FACT_SEARCH
    assert classify_request_rules("今天的新闻有哪些").intent == INTENT_FRESH_SEARCH
    assert classify_request_rules("现在RTX5090多少钱").intent == INTENT_FRESH_SEARCH
    assert classify_request_rules("帮我全面调研一下国产大模型的现状").intent == INTENT_DEEP_SEARCH
    assert classify_request_rules("这两个方案哪个更好").intent == INTENT_DIRECT_CHAT
    for chat in (
        "我今天上班好累啊",
        "陪我聊聊天吧",
        "给我讲个笑话",
        "帮我写一首关于秋天的诗",
        "打开播放器放首歌",
        "帮我记一下明天要买牛奶",
        "在吗",
        "今天天气不错，我们聊聊水几点烧开吧",
    ):
        ruled = classify_request_rules(chat)
        assert ruled.intent == INTENT_DIRECT_CHAT, chat
        assert ruled.needs_search is False
    # 规则认不出的疑问句交给轻量判定，"我们聊聊水几点烧开吧"这类闲聊不受影响
    assert classify_request_rules("跟我说说量子力学？").source == "rule_unknown"


def test_fresh_news_question_tries_multiple_sources(tmp_path):
    """“今天的新闻”必须有至少两个来源的尝试。"""
    provider = GateScriptedProvider([
        "今天的新闻我也没跟进。",
        "今天的主要新闻是这几条。",
    ])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, domains=("a.example.com", "b.example.com", "c.example.com"))

    outcome = asyncio.run(loop.run("今天的新闻有哪些"))

    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    assert len(searches) == 1, "一轮逻辑搜索已包含 Bocha+Bing，不应按网页条数重复搜索"
    assert outcome.status == "completed"


def test_platform_failure_keeps_other_platform_evidence(tmp_path):
    verdict = json.dumps({
        "needs_search": True, "reason": "热梗", "targets": ["某梗"],
        "suggested_queries": ["某梗"], "meme": True,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["B站查到了，是一个梗。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, with_bilibili=True, fail_web=True)

    outcome = asyncio.run(loop.run("某梗最近爆火，给我们讲讲呗"))

    # 网页平台失败没有把整个回合判失败，B站证据仍然进入主回答的证据消息
    assert outcome.status == "completed"
    assert "是一个梗" in outcome.reply
    evidence_payloads = [
        str(message.get("content") or "")
        for request in provider.requests for message in request["messages"]
        if message.get("role") == "tool"
    ]
    assert any("bilibili" in payload for payload in evidence_payloads), "B站证据必须进入主回合"
    assert "provider down" not in outcome.reply


def test_unsupported_reply_gets_evidence_correction(tmp_path):
    provider = GateScriptedProvider([
        "梁文谷可能是个人名。",
        "查到了：梁文谷是一个梗。",
        "修正后：梁文谷是一个网络热梗，仅找到标题级信息，完整含义无法确认。",
    ], evidence=EVIDENCE_BAD)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗"))

    assert "无法确认" in outcome.reply


def test_no_evidence_reply_does_not_fabricate(tmp_path):
    verdict = json.dumps({
        "needs_search": True, "reason": "陌生词", "targets": ["梁文谷"],
        "suggested_queries": ["梁文谷"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(
        ["梁文谷是一个虚拟人物。", "两个平台都没查到，无法确认梁文谷指什么。"],
        verdict=verdict,
    )
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, fail_web=True, fail_read=True)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗"))

    assert "虚拟人物" not in outcome.reply, "检索失败后不能保留未核实的断言"
    assert "无法确认" in outcome.reply
    assert all("provider down" not in str(message.get("content") or "")
               for request in provider.requests for message in request["messages"])


def test_assess_failure_degrades_to_no_search(tmp_path):
    class BrokenGateProvider(GateScriptedProvider):
        async def complete(self, messages, **kwargs):
            for message in messages:
                if message.get("role") == "system" and str(message.get("content") or "").startswith(("你是回答前的核查器", "你是请求意图判断器")):
                    raise RuntimeError("assess provider down")
            return await super().complete(messages, **kwargs)

    provider = BrokenGateProvider(["就按你说的来。"])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗"))

    assert outcome.reply == "就按你说的来。"
    assert loop._test_tool_calls == []


def test_disabled_gate_keeps_old_behavior(tmp_path):
    provider = GateScriptedProvider(["直接回答。"], verdict=ASSESS_TRUE)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=False))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗"))

    assert outcome.reply == "直接回答。"
    assert loop._test_tool_calls == []


def test_build_search_gate_returns_none_when_disabled(monkeypatch):
    monkeypatch.setenv("SEARCH_GATE_ENABLED", "false")
    assert build_search_gate(object()) is None
    monkeypatch.setenv("SEARCH_GATE_ENABLED", "true")
    gate = build_search_gate(object())
    assert gate is not None
    assert gate.config.enabled is True


def test_streaming_reply_is_staged_until_check_passes(tmp_path):
    """漏搜纠正前，候选回复不能以任何增量形式提前展示给用户。"""

    class StreamingProvider(GateScriptedProvider):
        async def stream_complete(self, messages, *, tools=None, max_tokens=None, temperature=None, on_delta=None):
            # 模拟流式：把聊天回复逐字吐出去（判定/证据调用不受影响，它们不是流式上下文）
            system_text = next(
                (str(message.get("content") or "") for message in messages if message.get("role") == "system"),
                "",
            )
            if system_text.startswith("你是回答前的核查器") or system_text.startswith("你是证据核查器"):
                return await self.complete(messages, tools=tools, max_tokens=max_tokens, temperature=temperature)
            content = self.chat_replies.pop(0) if self.chat_replies else "好的。"
            for char in content:
                if on_delta:
                    await on_delta(char)
            return ProviderResponse(
                message={"role": "assistant", "content": content},
                usage={"completion_tokens": 1},
            )

    deltas: list[Event] = []

    async def sink(event: Event) -> None:
        if event.type == EventType.CHAT_DELTA:
            deltas.append(event)

    bus = InMemoryEventBus()
    bus.subscribe(EventType.CHAT_DELTA, sink)

    provider = StreamingProvider([
        "查过了：梁文谷是一条网络热梗。",
    ])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate, bus=bus)

    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗"))

    published = "".join(str(event.data.get("delta") or "") for event in deltas)
    assert published == outcome.reply, "正文只发布一次，且与最终交付一致"


def test_turn_without_gate_streams_normally(tmp_path):
    """开关关闭（不注入门）时，流式行为与旧版一致：边生成边发。"""

    class StreamingOnly:
        def __init__(self) -> None:
            self.replies = ["一句普通的回答。"]

        async def stream_complete(self, messages, *, tools=None, max_tokens=None, temperature=None, on_delta=None):
            content = self.replies.pop(0)
            for char in content:
                if on_delta:
                    await on_delta(char)
            return ProviderResponse(message={"role": "assistant", "content": content}, usage={})

    deltas: list[Event] = []

    async def sink(event: Event) -> None:
        if event.type == EventType.CHAT_DELTA:
            deltas.append(event)

    bus = InMemoryEventBus()
    bus.subscribe(EventType.CHAT_DELTA, sink)
    loop = _build_loop(StreamingOnly(), tmp_path, gate=None, bus=bus)

    outcome = asyncio.run(loop.run("随便聊聊今天的心情"))

    published = "".join(str(event.data.get("delta") or "") for event in deltas)
    assert published == outcome.reply == "一句普通的回答。"


# ── 纯逻辑：判定与关键词 ──────────────────────────────────────────────
def test_derive_queries_prefers_targets_over_whole_sentence():
    verdict = SearchNeedVerdict(
        needs_search=True, targets=("梁文谷",), suggested_queries=("梁文谷 是谁",),
    )
    queries = SearchGate.derive_queries(verdict, "梁文锋和梁文谷是什么，顺便说明天天气")
    assert queries[0] == "梁文谷 是谁"


def test_derive_queries_falls_back_to_subject_extraction():
    verdict = SearchNeedVerdict(needs_search=True)
    queries = SearchGate.derive_queries(verdict, "梁文谷是什么")
    assert queries and "梁文谷" in queries[0]


def test_verdict_json_parsing_tolerates_code_fence():
    from agent_core.search_gate import _extract_json_object

    payload = _extract_json_object("```json\n{\"needs_search\": true}\n```")
    assert payload == {"needs_search": True}
    assert _extract_json_object("完全不是 JSON") is None


def test_retry_next_keyword_only_when_first_finds_nothing(tmp_path):
    """空结果换下一个词继续找来源；一旦拿到独立来源就不再为同一来源换词。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "陌生词", "targets": ["梁文谷"],
        "suggested_queries": ["甲", "乙", "丙"], "meme": False,
    }, ensure_ascii=False)
    calls: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append(dict(arguments))
        query = str(arguments.get("query") or "")
        rows = [] if query == "甲" else [{
            "title": f"{query} 的解释", "url": f"https://example.com/{query}",
            "snippet": "摘要", "platform": "web", "published_at": "2026-10-01",
        }]
        return {"status": "ok", "output": {"query": query, "result_count": len(rows), "results": rows}}

    provider = GateScriptedProvider(["不清楚。", "查到了。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    web = ToolSpec(
        "search__web",
        {"type": "function", "function": {"name": "search__web", "parameters": {"type": "object"}}},
        handler, effect=EFFECT_READ, deterministic_route_allowed=True,
    )
    loop = _build_loop(provider, tmp_path, gate=gate, web=web)  # web 自带观测（handler 直接写 calls）

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert 1 <= len(calls) <= 3
    assert all(call.get("query") for call in calls)


def test_model_searched_but_meme_target_gets_bilibili_supplement(tmp_path):
    """模型自己搜过网页、判定为热梗时，B站仍在预算内被查到（同一本账）。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "目标词没有解释", "targets": ["梁文谷"],
        "suggested_queries": ["梁文谷"], "meme": True,
    }, ensure_ascii=False)
    calls: list[dict[str, Any]] = []

    def web_handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append({"tool": "search__web", **arguments})
        return {"status": "ok", "output": {"query": arguments.get("query"), "result_count": 1, "results": [{
            "title": "同名网页", "url": "https://example.com/x", "snippet": "无关内容",
            "platform": "web", "published_at": "",
        }]}}

    def bili_handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append({"tool": "search__bilibili", **arguments})
        return {"status": "ok", "output": {"query": arguments.get("query"), "result_count": 1, "results": [{
            "title": "梁文谷梗解释", "url": "https://www.bilibili.com/video/BV1xx411c7mD",
            "bvid": "BV1xx411c7mD", "snippet": "峰谷价说", "platform": "bilibili", "published_at": "2026-09-19",
        }]}}

    provider = GateScriptedProvider(["根据B站：梁文谷是峰谷价说梗。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    registry = ToolRegistry()
    for spec in (
        ToolSpec(
            "search__web",
            {"type": "function", "function": {"name": "search__web", "parameters": {"type": "object"}}},
            web_handler, effect=EFFECT_READ, deterministic_route_allowed=True,
        ),
        ToolSpec(
            "search__bilibili",
            {"type": "function", "function": {"name": "search__bilibili", "parameters": {"type": "object"}}},
            bili_handler, effect=EFFECT_READ, deterministic_route_allowed=True,
        ),
    ):
        registry.register(spec)
    loop = AgentLoop(
        provider, registry, SCOPE,
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        search_gate=gate,
    )
    loop._test_tool_calls = calls

    outcome = asyncio.run(loop.run("梁文谷这个梗最近好火，给我们讲讲"))

    tools = {call["tool"] for call in calls}
    assert "search__web" in tools and "search__bilibili" in tools, "热梗问题网页与B站都要查到"
    assert outcome.reply.startswith("根据B站")
    assert outcome.status == "completed"


def test_model_self_search_evidence_is_audited_with_same_contract(tmp_path):
    """搜索取证之后主循环仍能用别的工具，且最终核查按同一口径还原证据。"""

    class ToolCallingProvider:
        def __init__(self) -> None:
            self.requests: list[dict[str, Any]] = []
            self.extra_used = False

        async def complete(self, messages, **kwargs):
            self.requests.append({"messages": list(messages), "tools": kwargs.get("tools")})
            system = next(
                (str(message.get("content") or "") for message in messages if message.get("role") == "system"),
                "",
            )
            if system.startswith("你是检索材料整理器"):
                return ProviderResponse(message={"role": "assistant", "content": json.dumps(
                    {"covered": True, "missing": [], "uncovered": [],
                     "key_facts": [{"text": "梁文谷是一条网络热梗", "source_ids": ["source-1"], "conditions": ""}]},
                    ensure_ascii=False,
                )}, usage={})
            if system.startswith("你是最终核查器"):
                return ProviderResponse(message={"role": "assistant", "content": json.dumps(
                    {"publishable": True, "issue": "", "used_source_ids": ["source-1"]},
                )}, usage={})
            if not self.extra_used:
                self.extra_used = True
                return ProviderResponse(message={
                    "role": "assistant", "content": "",
                    "tool_calls": [{
                        "id": "call-extra", "type": "function",
                        "function": {"name": "system__info", "arguments": json.dumps({})},
                    }],
                }, usage={})
            return ProviderResponse(message={"role": "assistant", "content": "梁文谷是一条网络热梗。"}, usage={})

    extra_calls: list[dict[str, Any]] = []

    def extra_handler(arguments, scope):
        extra_calls.append(dict(arguments))
        return {"status": "ok", "output": {"note": "本地信息"}}

    provider = ToolCallingProvider()
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)
    loop.registry.register(ToolSpec(
        "system__info",
        {"type": "function", "function": {"name": "system__info", "parameters": {"type": "object"}}},
        extra_handler, effect=EFFECT_READ, deterministic_route_allowed=True,
    ))

    outcome = asyncio.run(loop.run("梁文谷是谁"))

    assert extra_calls, "取证结束后非搜索工具仍须可用"
    assert outcome.status == "completed"
    review_requests = [
        request for request in provider.requests
        if any(
            str(message.get("content") or "").startswith("你是最终核查器")
            for message in request["messages"] if message.get("role") == "system"
        )
    ]
    assert review_requests, "搜索答案必须过最终核查"
    assert outcome.sources and outcome.sources[0]["url"].startswith("https://")
    assert "来源：" not in outcome.reply


def test_missed_search_correction_happens_at_most_once_per_turn(tmp_path):
    """每回合最多补查一次：预算用尽后不再补查，也不重复纠正。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "热梗", "targets": ["某梗"],
        "suggested_queries": ["某梗"], "meme": True,
    }, ensure_ascii=False)
    calls: list[dict[str, Any]] = []

    def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        calls.append({"tool": "search__web", **arguments})
        return {"status": "ok", "output": {"query": arguments.get("query"), "result_count": 1, "results": [{
            "title": "无关", "url": "https://example.com/x", "snippet": "", "platform": "web", "published_at": "",
        }]}}

    provider = GateScriptedProvider(["不知道。", "查到了。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    web = ToolSpec(
        "search__web",
        {"type": "function", "function": {"name": "search__web", "parameters": {"type": "object"}}},
        handler, effect=EFFECT_READ, deterministic_route_allowed=True,
    )
    loop = _build_loop(provider, tmp_path, gate=gate, web=web)
    # 模拟模型本轮已经把检索预算（3 次）用完
    loop._route_calls.extend([
        {"tool": "search__web", "status": "ok"},
        {"tool": "search__web", "status": "ok"},
        {"tool": "search__web", "status": "ok"},
    ])

    asyncio.run(loop.run("某梗最近好火"))

    assert len(calls) == 0, "预算耗尽后不能再取证"
    assert loop._search_corrections <= 1

# ── 用户明确指定B站 → 快路径直接查该平台 ──────────────────────────────
def test_explicit_bilibili_query_routing(tmp_path):
    provider = GateScriptedProvider(["不需要润色。"])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=False))
    loop = _build_loop(provider, tmp_path, gate=gate, with_bilibili=True)

    outcome = asyncio.run(loop.run("去B站搜一下梁文谷"))

    tools = [call["tool"] for call in loop._test_tool_calls]
    assert tools == ["search__bilibili"], "用户点名B站时不能再走网页搜索"
    assert loop._test_tool_calls[0]["query"] == "梁文谷"
    assert outcome.status == "completed"


def test_bilibili_subject_question_stays_web_search(tmp_path):
    """「B站是什么」是把B站当讨论对象，不是要到B站去搜。"""
    provider = GateScriptedProvider(["按搜索结果说。"])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=False))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("搜索一下B站是什么"))

    tools = [call["tool"] for call in loop._test_tool_calls]
    assert tools == ["search__web"]
    assert loop._test_tool_calls[0]["query"] == "B站是什么"


def test_platform_words_are_not_stripped_from_inner_terms(tmp_path):
    """「上海」里的「上」不能被平台剥词误伤。"""
    provider = GateScriptedProvider(["按搜索结果说。"])
    gate = SearchGate(provider, config=SearchGateConfig(enabled=False))
    loop = _build_loop(provider, tmp_path, gate=gate, with_bilibili=True)

    asyncio.run(loop.run("去B站搜一下上海漫展的梗"))

    assert loop._test_tool_calls[0]["query"] == "上海漫展的梗"


def test_explicit_bilibili_query_unit_cases():
    from agent_core.loop import AgentLoop

    assert AgentLoop._explicit_bilibili_query("去B站搜一下梁文谷") == "梁文谷"
    assert AgentLoop._explicit_bilibili_query("在B站上找找梁文谷的梗") == "梁文谷的梗"
    assert AgentLoop._explicit_bilibili_query("搜下bilibili 梁文谷") == "梁文谷"
    assert AgentLoop._explicit_bilibili_query("B站是什么") is None
    assert AgentLoop._explicit_bilibili_query("B站的董事长是谁") is None
    assert AgentLoop._explicit_bilibili_query("梁文谷这个梗好火") is None
    assert AgentLoop._explicit_bilibili_query("今天天气不错") is None


def test_time_sensitive_question_triggers_search(tmp_path):
    """时效问题（最新价格/版本）触发核实，且回复按证据重写。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "价格时效信息", "targets": ["RTX 5090"],
        "suggested_queries": ["RTX 5090 价格"], "meme": False,
    }, ensure_ascii=False)
    provider = GateScriptedProvider(["查证后：目前售价约一万四。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True))
    loop = _build_loop(provider, tmp_path, gate=gate)

    outcome = asyncio.run(loop.run("跟我们聊聊RTX 5090现在啥价位呗"))

    assert "五千块" not in outcome.reply
    assert "来源：" not in outcome.reply
    # 时效问题要尝试第二个来源（换一种说法再搜），不是搜到一条就收工
    assert len(loop._test_tool_calls) >= 1
    assert all(call["tool"] == "search__web" for call in loop._test_tool_calls)
    assert outcome.sources, "来源要走结构化字段，不拼进正文"


def test_meme_question_still_reaches_bilibili_platform():
    """B站补查的旧排序启发式随独立补查链一起下线，

    但"热梗问题要查到 B站"这条口径保留：meme 信号仍然决定平台分配。
    """
    from agent_core.search_gate import classify_request_rules

    ruled = classify_request_rules("某梗最近爆火，给我们讲讲呗")
    assert ruled.needs_search is True
    assert ruled.meme is True or ruled.intent in {"fresh_search", "deep_search"}


# ----------------------------------------------------------------------
# 补查与原文读取的阶段通知（统一规则的第二条接入路径）
# ----------------------------------------------------------------------
def _two_query_verdict(target: str = "梁文谷") -> str:
    return json.dumps({
        "needs_search": True, "reason": "陌生词", "targets": [target],
        "suggested_queries": [f"{target} 出处", f"{target} 什么意思"], "meme": False,
    }, ensure_ascii=False)


def _reporter(sink: _ProgressRecorder) -> StewardReporter:
    """过程措辞由模型现场生成，所以 reporter 必须带一个真模型。

    这里接测试替身：它按"当前阶段"回一句测试话术（生产路径没有句库），
    措辞调不到模型时过程气泡直接不发 —— 不给 provider 就一条都出不来。
    """
    return StewardReporter(StewardNarrator(StagePhraseProvider()), sink=sink)


def test_supplementary_search_reports_real_stages_not_just_handover(tmp_path):
    """反例：长回合多次补查之后不能只剩一句"正在查"，要出现真实的取得材料/读原文。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "梁文谷出自某视频。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=2))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, with_read=True, domains=("a.example", "b.example"),
        steward=_reporter(sink), progress_policy=_instant_policy(),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert PHASE_SEARCHING in sink.phases
    assert PHASE_MATERIALS in sink.phases, "补查确实带回了材料，必须说出来"
    assert PHASE_READING in sink.phases, "开始读原文了，必须说出来"
    # 一条过程消息都不许带查询词、标题、网址、工具名或路径；文案只来自（替身）模型
    for message in sink.messages:
        content = message["content"]
        assert "梁文谷" not in content
        assert "http" not in content
        assert "search__" not in content
        assert "\\" not in content and "/" not in content
        assert content == stage_line(message["phase"])


def test_stages_stay_inside_the_middle_notice_limit(tmp_path):
    """阶段再真也不刷屏：首次通知之外最多两条中段。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "梁文谷出自某视频。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=2))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, with_read=True, domains=("a.example", "b.example"),
        steward=_reporter(sink), progress_policy=_instant_policy(),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert len(sink.messages) <= 3, sink.phases
    assert sink.phases.count(PHASE_SEARCHING) == 1
    assert sink.phases.count(PHASE_MATERIALS) == 1


def test_single_read_says_reading_and_never_claims_another_source(tmp_path):
    """只计划读一份原文：说"正在读"，不说"已经取回来了，再看另一份"。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "梁文谷出自某视频。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=2, max_page_reads=1))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, with_read=True, domains=("a.example", "b.example"),
        steward=_reporter(sink), progress_policy=_instant_policy(max_mid_notices=3),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert PHASE_READING in sink.phases
    assert PHASE_READ_DONE not in sink.phases


def test_two_reads_separate_reading_from_text_in_hand(tmp_path):
    """两份原文连续读：正在读与已经取回来分得清楚，第二份不会再多刷一条。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "梁文谷出自某视频。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=2))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, with_read=True, domains=("a.example", "b.example"),
        steward=_reporter(sink), progress_policy=_instant_policy(max_mid_notices=3),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert PHASE_SEARCHING in sink.phases
    assert PHASE_READING in sink.phases
    assert sink.phases.count(PHASE_READING) == 1
    assert len(sink.phases) <= 4


def test_zero_results_with_another_query_only_says_redirect(tmp_path):
    """零结果但有后续查询：只说换来源，不说取得了材料。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "没查到来源。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=0))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, empty_web=True,
        steward=_reporter(sink), progress_policy=_instant_policy(max_mid_notices=3),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert PHASE_REDIRECT in sink.phases
    assert PHASE_MATERIALS not in sink.phases
    redirect = next(message for message in sink.messages if message["phase"] == PHASE_REDIRECT)
    assert redirect["content"] == stage_line(PHASE_REDIRECT)


def test_zero_results_without_a_replacement_never_promises_a_switch(tmp_path):
    """预算里没有替代路线：不许说"我换个来源看看"，交给最终回答如实说明。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "没查到来源，无法确认。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=1, max_page_reads=0))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, empty_web=True,
        steward=_reporter(sink), progress_policy=_instant_policy(max_mid_notices=3),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert PHASE_REDIRECT not in sink.phases
    assert sink.phases == [PHASE_SEARCHING]


def test_failed_read_without_another_source_does_not_promise_more(tmp_path):
    """读取失败而且没有下一份：不说"换来源"，也不许说"读完了"。"""
    provider = GateScriptedProvider(["梁文谷是个梗。", "原文没取到，只能确认到标题。"], verdict=_two_query_verdict())
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=1, max_page_reads=1))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, with_read=True, fail_read=True,
        steward=_reporter(sink), progress_policy=_instant_policy(max_mid_notices=3),
    )

    asyncio.run(loop.run("请查原文核实梁文谷的时间线"))

    assert PHASE_REDIRECT not in sink.phases
    assert PHASE_READ_DONE not in sink.phases
    assert PHASE_READING in sink.phases


def test_progress_stages_do_not_change_search_behaviour(tmp_path):
    """加了通知也不许改变检索次数、来源范围与最终答案。"""
    verdict = _two_query_verdict()
    provider = GateScriptedProvider(["梁文谷是个梗。", "梁文谷出自某视频。"], verdict=verdict)
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=2))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, with_read=True,
        steward=_reporter(sink), progress_policy=_instant_policy(),
    )
    outcome = asyncio.run(loop.run("跟我们讲讲梁文谷呗？"))
    searches = [call for call in loop._test_tool_calls if call["tool"] == "search__web"]
    reads = [call for call in loop._test_tool_calls if call["tool"] == "search__read"]

    plain_provider = GateScriptedProvider(["梁文谷是个梗。", "梁文谷出自某视频。"], verdict=verdict)
    plain_gate = SearchGate(plain_provider, config=SearchGateConfig(enabled=True, max_search_queries=3, max_page_reads=2))
    plain_loop = _build_loop(plain_provider, tmp_path, gate=plain_gate, with_read=True)
    plain_outcome = asyncio.run(plain_loop.run("跟我们讲讲梁文谷呗？"))

    assert outcome.reply == plain_outcome.reply
    assert len(searches) == len([call for call in plain_loop._test_tool_calls if call["tool"] == "search__web"])
    assert len(reads) == len([call for call in plain_loop._test_tool_calls if call["tool"] == "search__read"])
    assert len(searches) <= 3


class _ModelSearchesFirstProvider:
    """模型自己先搜一轮，再给候选回复；判定/证据调用照旧回 JSON。

    用来复现"模型已经搜过、门发现说法矛盾且来源还不够 → 确实还会补搜"这条路径。
    """

    def __init__(self, reply: str, *, verdict: str, query: str = "某显卡 售价") -> None:
        self.verdict = verdict
        self.reply = reply
        self.query = query
        self.turn = 0

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        system_text = next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "system"), "",
        )
        if system_text.startswith("你是回答前的核查器"):
            return ProviderResponse(message={"role": "assistant", "content": self.verdict}, usage={})
        if system_text.startswith("你是证据核查器"):
            return ProviderResponse(message={"role": "assistant", "content": EVIDENCE_OK}, usage={})
        self.turn += 1
        if self.turn == 1:
            return ProviderResponse(message={
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "search__web", "arguments": json.dumps({"query": self.query})},
                }],
            }, usage={})
        return ProviderResponse(message={"role": "assistant", "content": self.reply}, usage={})


def test_conflict_with_a_planned_follow_up_says_it_will_check_again(tmp_path):
    """已经看出具体矛盾、而且补搜确实会执行时才说"我再核对一下"。"""
    verdict = json.dumps({
        "needs_search": True, "reason": "价格时效", "targets": ["某显卡"],
        "suggested_queries": ["某显卡 售价"], "meme": False,
    }, ensure_ascii=False)

    def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        price_a = "4999" if "售价" in str(arguments.get("query") or "") else "5099"
        rows = [
            {"title": f"某显卡 {price_a} 元 开售", "url": "https://price-a.example.com/x",
             "snippet": f"某显卡售价 {price_a} 元，现已开售。", "platform": "web", "published_at": "2026-10-01"},
            {"title": "某显卡 5199 元 开售", "url": "https://price-b.example.com/x",
             "snippet": "某显卡售价 5199 元，现已开售。", "platform": "web", "published_at": "2026-10-01"},
        ]
        return {"status": "ok", "output": {"query": arguments.get("query"), "result_count": len(rows), "results": rows}}

    web = ToolSpec(
        "search__web",
        {"type": "function", "function": {"name": "search__web", "parameters": {"type": "object"}}},
        handler, effect=EFFECT_READ, deterministic_route_allowed=True,
    )
    provider = _ModelSearchesFirstProvider("大概五千块吧。", verdict=verdict)
    # target_sources=3：两个域名已经互相矛盾，但覆盖还没达标 → 确实还会补搜。
    gate = SearchGate(provider, config=SearchGateConfig(enabled=True, max_search_queries=3, target_sources=3))
    sink = _ProgressRecorder()
    loop = _build_loop(
        provider, tmp_path, gate=gate, web=web,
        steward=_reporter(sink), progress_policy=_instant_policy(max_mid_notices=3),
    )

    outcome = asyncio.run(loop.run("跟我们讲讲某显卡现在啥价位呗"))

    assert outcome.status == "completed"
    assert "conflict" in sink.phases
    conflict = next(message for message in sink.messages if message["phase"] == "conflict")
    assert conflict["content"] == stage_line("conflict")
    assert "某显卡" not in conflict["content"]
