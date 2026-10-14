"""开场白链路：线索、联想闸门与冷却、JSON 决策、台账、缓存与 HTTP 接口。

分五层测，因为它们失败的方式完全不同：
- 纯函数（线索剪枝、输出判定、JSON 解析）错了 → 说出一句不像人说的话，
  或者把一个编造的 cue_id 记成"引用过的旧事"；
- 闸门错了 → 每次进门都翻旧账，或者反过来永远不联想；
- 台账错了 → 同一件事被反复提起，两个账号互相看到对方的记录；
- 缓存错了 → 每次刷新都花一次 Token；
- 接口错了 → 前端拿到 422 或一句空话。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from agent_core.contracts import ActionObservation, ProviderResponse, Scope, ToolSpec, EFFECT_READ
from agent_core.conversation_repository import ConversationRepository
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_writes import MemoryWriter
from agent_core.opening_greeting import (
    DEFAULT_GREETING,
    OpeningGreetingCache,
    OpeningGreetingService,
    _abandon,
    _match_cue,
    fact_cues,
    greeting_issue,
    is_low_value_reply,
    is_one_off_command,
    normalize_greeting,
    rank_cues,
    topic_cues,
)
from agent_core.opening_pool import GREETING_POOL, prepared_greeting
from agent_core.opening_recall import (
    GREETING_FACT,
    GREETING_PLAIN,
    GREETING_TOPIC,
    CueSelection,
    OpeningCue,
    OpeningRecallStore,
    available_cues,
    cooldown_blocklist,
    cue_dedupe_key,
    normalize_cue_key,
    parse_cue_selection,
    parse_narration,
    should_allow_recall,
)

ACCOUNT = "acct"
CONVERSATION = "web-stage"
OTHER_ACCOUNT = "other"
VISIT = "visit-abcdefgh"

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _at(*, days_ago: float = 0, hours_ago: float = 0) -> datetime:
    return NOW - timedelta(days=days_ago, hours=hours_ago)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _message(message_id: int, content: str, *, days_ago: float = 0, **extra) -> dict:
    return {
        "role": "user",
        "content": content,
        "message_id": message_id,
        "created_at": _iso(_at(days_ago=days_ago)),
        **extra,
    }


class Embed:
    """3 维假嵌入，和测试里 vector_size=3 对齐（不联网）。"""

    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class ScriptedProvider:
    """按脚本回话，并记录每次调用的完整参数。

    开场白是**两次调用**：先选题（只回一个线索 id），再措辞（只回一句话）。
    所以这里按请求属于哪一阶段回不同的内容 —— 判据是提示词里有没有
    ``selected_cue_id`` 这个字段名（只有选题阶段要求它输出这个字段）。

    默认输出两段都是合法格式；要造非法输出就传 ``raw``（措辞阶段）
    或 ``selector_raw``（选题阶段）。
    """

    def __init__(
        self,
        greeting: str = "哦，是你啊。事情都办完了吗，说说看？",
        *,
        cue_id: str | None = None,
        raw: str | None = None,
        selector_raw: str | None = None,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        # 措辞阶段的输出**只有一个 greeting 字段**：引用了哪条线索、算哪件事，
        # 都由服务端在选题阶段定，模型没有申报的资格（也没有这个字段可填）。
        self.raw = raw if raw is not None else json.dumps({"greeting": greeting}, ensure_ascii=False)
        self.selector_raw = selector_raw if selector_raw is not None else json.dumps(
            {"selected_cue_id": cue_id}, ensure_ascii=False,
        )
        self.delay = delay
        self.error = error
        self.calls: list[dict] = []

    @property
    def greeting(self) -> str:
        """脚本里那句开场白（供断言复用）。"""
        try:
            return str(json.loads(self.raw).get("greeting") or "")
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def _stage(messages: list[dict]) -> str:
        return "selector" if any(
            "selected_cue_id" in str(item.get("content") or "") for item in messages
        ) else "narrator"

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        stage = self._stage(messages)
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stage": stage,
        })
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        content = self.selector_raw if stage == "selector" else self.raw
        return ProviderResponse(message={"role": "assistant", "content": content})

    def prompt_text_of(self, stage: str) -> str:
        """某一阶段最后一次请求的全文（选题线索进没进提示词，要按阶段看）。"""
        return "\n".join(
            str(item.get("content") or "")
            for call in self.calls if call["stage"] == stage
            for item in call["messages"]
        )

    @property
    def stages(self) -> list[str]:
        return [call["stage"] for call in self.calls]

    @property
    def prompt_text(self) -> str:
        """**所有**请求的全文。

        只看最后一次调用是不够的：开场白现在问两次（选题、措辞），
        一条不该出现的线索只要出现在**任意一次**请求里，它就已经出去了。
        """
        return "\n".join(
            str(item.get("content") or "")
            for call in self.calls
            for item in call["messages"]
        )


class BrokenStore:
    """写入永远失败的台账。"""

    def recent_events(self, scope, **kwargs):
        return []

    def record(self, scope, **kwargs):
        return False


class FailingStore:
    """读取永远失败的台账。"""

    def recent_events(self, scope, **kwargs):
        raise RuntimeError("台账读不出来")

    def record(self, scope, **kwargs):  # pragma: no cover - 不该被调到
        raise AssertionError("读取失败之后不该继续写")


def _scope(account: str = ACCOUNT, conversation: str = CONVERSATION) -> Scope:
    return Scope(account, "web", conversation, "companion")


@pytest.fixture()
def conversation_repository(tmp_path) -> ConversationRepository:
    return ConversationRepository(tmp_path / "core.db")


@pytest.fixture()
def repositories(tmp_path):
    repo = initialize_memory_repositories(tmp_path / "memory.db", tmp_path / "vectors", vector_size=3)
    yield repo
    repo.close()


@pytest.fixture()
def recall_store(tmp_path) -> OpeningRecallStore:
    return OpeningRecallStore(tmp_path / "core.db")


def _writer(repositories) -> MemoryWriter:
    return MemoryWriter(repositories, Embed())


def _save_fact(repositories, scope: Scope, content: str) -> dict:
    return _writer(repositories).save_fact(scope, content, confidence=0.95)


def _service(provider, **kwargs) -> OpeningGreetingService:
    return OpeningGreetingService(provider, **kwargs)


class OpeningToolProvider:
    def __init__(self, first, second=None):
        self.replies = [first] + ([second] if second is not None else [])
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools})
        return ProviderResponse(message=self.replies.pop(0))


class OpeningToolGateway:
    def __init__(self):
        self.calls = []
        specs = {}
        for name in ("search__web", "weather__query", "location__current"):
            specs[name] = ToolSpec(
                name=name, effect=EFFECT_READ,
                schema={"type": "function", "function": {"name": name, "parameters": {"type": "object", "properties": {}}}},
                handler=lambda args, scope: {"status": "ok", "output": {"summary": "晴朗"}},
            )
        self.registry = type("Registry", (), {"resolve": lambda _, name: specs.get(name)})()

    async def invoke(self, request):
        self.calls.append(request)
        return ActionObservation(action_id=request.action_id, name=request.name, status="ok", output={"summary": "晴朗"})


@pytest.mark.asyncio
async def test_opening_uses_at_most_one_read_tool_and_summarizes_result(repositories, recall_store):
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    provider = OpeningToolProvider(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "search__web", "arguments": '{"cue_id":"message:11","topic":"考试备考"}'}},
            {"id": "call-2", "type": "function", "function": {"name": "weather__query", "arguments": '{"area":"北京"}'}},
        ]},
        {"role": "assistant", "content": '{"greeting":"你上次说的考试，准备得怎么样啦？","selected_cue_id":"message:11"}'},
    )
    gateway = OpeningToolGateway()
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       prepared_plain=lambda _: "回来啦，坐会儿？", opening_gateway=gateway)
    result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要考试")], now=NOW)
    assert "考试" in result
    assert len(gateway.calls) == 1
    assert gateway.calls[0].arguments["query"] == "考试备考"
    assert provider.calls[1]["tools"] is None


@pytest.mark.asyncio
async def test_opening_does_not_offer_location_without_a_fresh_shared_snapshot(repositories, recall_store):
    provider = OpeningToolProvider({"role": "assistant", "content": '{"greeting":"回来啦，坐会儿？","selected_cue_id":null}'})
    gateway = OpeningToolGateway()
    location = type("Location", (), {"get": lambda _, scope: None})()
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       prepared_plain=lambda _: "回来啦，坐会儿？", opening_gateway=gateway,
                       location_store=location)
    await service.generate(Scope(ACCOUNT, "web", CONVERSATION), visit_id=VISIT,
                           history=[_message(11, "我下周要考试")], now=NOW)
    assert "location__current" not in [item["function"]["name"] for item in provider.calls[0]["tools"] or []]
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_opening_tool_failure_still_uses_the_recent_outline(repositories, recall_store):
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    provider = OpeningToolProvider(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "weather__query", "arguments": '{"area":"北京","cue_id":"message:11"}'}},
        ]},
        {"role": "assistant", "content": '{"greeting":"你说的考试，准备得怎么样啦？","selected_cue_id":"message:11"}'},
    )
    gateway = OpeningToolGateway()
    async def failed(request):
        gateway.calls.append(request)
        raise TimeoutError("weather unavailable")
    gateway.invoke = failed
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       prepared_plain=lambda _: "回来啦，坐会儿？", opening_gateway=gateway)
    result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要考试")], now=NOW)
    assert "考试" in result
    assert len(gateway.calls) == 1
    assert '"status": "unavailable"' in provider.calls[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_opening_model_timeout_returns_prepared_greeting(repositories, recall_store):
    class SlowProvider:
        async def complete(self, messages, **kwargs):
            await asyncio.sleep(0.3)
            return ProviderResponse(message={"role": "assistant", "content": '{"greeting":"迟到的问候？","selected_cue_id":null}'})
    service = _service(SlowProvider(), repositories=repositories, recall_store=recall_store,
                       opening_gateway=OpeningToolGateway(), prepared_plain=lambda _: "回来啦，坐会儿？",
                       total_timeout_seconds=0.1)
    started = time.monotonic()
    result = await service.generate(Scope(ACCOUNT, "web", CONVERSATION), visit_id=VISIT, now=NOW)
    assert result == "回来啦，坐会儿？"
    assert time.monotonic() - started < 0.3


@pytest.mark.asyncio
async def test_opening_may_use_existing_authorized_location(repositories, recall_store):
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    provider = OpeningToolProvider(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "loc-1", "type": "function", "function": {"name": "location__current", "arguments": '{"cue_id":"message:11"}'}},
        ]},
        {"role": "assistant", "content": '{"greeting":"你说的出行计划，还顺利吗？","selected_cue_id":"message:11"}'},
    )
    location = type("Location", (), {"get": lambda _, scope: {"expired": False}})()
    gateway = OpeningToolGateway()
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       opening_gateway=gateway, location_store=location,
                       prepared_plain=lambda _: "回来啦，坐会儿？")
    result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要出门旅行")], now=NOW)
    assert "出行计划" in result
    assert gateway.calls[0].name == "location__current"
    assert gateway.calls[0].arguments == {}


@pytest.mark.asyncio
async def test_opening_rejects_unlisted_cue_without_recording_it(repositories, recall_store):
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    provider = OpeningToolProvider({"role": "assistant", "content": '{"greeting":"你上次说的考试怎么样啦？","selected_cue_id":"message:999"}'})
    gateway = OpeningToolGateway()
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       opening_gateway=gateway, prepared_plain=lambda _: "回来啦，坐会儿？")
    result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要考试")], now=NOW)
    assert result == "回来啦，坐会儿？"
    assert gateway.calls == []


@pytest.mark.asyncio
async def test_opening_accepts_cue_id_reported_without_the_prefix(repositories, recall_store):
    """模型经常把 ``message:11`` 抄成 ``11`` —— 服务端按后缀认回去，不许整句作废。

    （真实模型五个样本全部丢前缀；旧行为是把写好的个性化问候静默扔掉。）
    """
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    provider = OpeningToolProvider(
        {"role": "assistant", "content": '{"greeting":"你上次说的考试，准备得怎么样啦？","selected_cue_id":"11"}'}
    )
    gateway = OpeningToolGateway()
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       opening_gateway=gateway, prepared_plain=lambda _: "回来啦，坐会儿？")
    result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要考试")], now=NOW)
    assert "考试" in result
    events = recall_store.recent_events(scope, now=NOW)
    assert [event["cue_id"] for event in events] == ["message:11"]
    assert [event["greeting_kind"] for event in events] == [GREETING_TOPIC]


@pytest.mark.asyncio
async def test_opening_tool_path_accepts_cue_id_without_the_prefix(repositories, recall_store):
    """工具路径同样不许前缀差异整句作废：参数里和最终回报里丢了前缀都要认得出。"""
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    provider = OpeningToolProvider(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "weather__query", "arguments": '{"area":"北京","cue_id":"11"}'}},
        ]},
        {"role": "assistant", "content": '{"greeting":"你说的考试，准备得怎么样啦？","selected_cue_id":"11"}'},
    )
    gateway = OpeningToolGateway()
    service = _service(provider, repositories=repositories, recall_store=recall_store,
                       opening_gateway=gateway, prepared_plain=lambda _: "回来啦，坐会儿？")
    result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要考试")], now=NOW)
    assert "考试" in result
    assert len(gateway.calls) == 1
    assert [event["cue_id"] for event in recall_store.recent_events(scope, now=NOW)] == ["message:11"]


def test_cue_matching_tolerates_missing_prefix_but_not_ambiguity():
    """后缀匹配只认**唯一**命中：``11`` 同时是两条线索的尾巴时宁可认不出。"""
    topic = OpeningCue(cue_id="message:11", source="conversation", summary="我下周要考试", occurred_at="")
    fact = OpeningCue(cue_id="fact:11", source="memory", summary="用户对花生过敏", occurred_at="")
    both = {"message:11": topic, "fact:11": fact}
    assert _match_cue("message:11", both) is topic
    assert _match_cue("11", both) is None  # 含糊：两条都可能是它
    assert _match_cue("999", both) is None  # 编外编号照旧拒绝
    only_topic = {"message:11": topic}
    assert _match_cue("11", only_topic) is topic  # 唯一命中，前缀丢了也认
    assert _match_cue(None, only_topic) is None
    assert _match_cue("", only_topic) is None


@pytest.mark.asyncio
async def test_opening_does_not_repeat_the_same_recent_cue(repositories, recall_store):
    scope = Scope(ACCOUNT, "web", CONVERSATION)
    gateway = OpeningToolGateway()
    first = OpeningToolProvider({"role": "assistant", "content": '{"greeting":"你说的考试，准备得怎么样啦？","selected_cue_id":"message:11"}'})
    service = _service(first, repositories=repositories, recall_store=recall_store,
                       opening_gateway=gateway, prepared_plain=lambda _: "回来啦，坐会儿？")
    first_result = await service.generate(scope, visit_id=VISIT, history=[_message(11, "我下周要考试")], now=NOW)
    assert "考试" in first_result
    second = OpeningToolProvider({"role": "assistant", "content": '{"greeting":"回来啦，坐会儿？","selected_cue_id":null}'})
    service.provider = second
    second_result = await service.generate(scope, visit_id="visit-next-abcde", history=[_message(11, "我下周要考试")], now=NOW)
    assert second_result == "回来啦，坐会儿？"
    assert "message:11" not in str(second.calls[0]["messages"])


_VISIT_SEQUENCE = itertools.count()


def _recall(
    store,
    scope,
    *,
    days_ago: float = 0,
    kind: str = GREETING_TOPIC,
    cue_id: str | None = "message:11",
    cue_key: str | None = "我下周三去拔牙",
) -> None:
    """往台账里塞一条历史记录。visit_id 用自增序号保证每条都真的落库。"""
    store.record(
        scope,
        visit_id=f"visit-seed-{next(_VISIT_SEQUENCE):04d}",
        greeting_kind=kind,
        cue_id=cue_id,
        cue_key=cue_key,
        now=_at(days_ago=days_ago),
    )


# ── 线索：对话 ──────────────────────────────────────────────────────────
def test_topic_cues_keeps_only_user_lines_in_chronological_order() -> None:
    history = [
        _message(1, "我周五要考运筹学"),
        {"role": "assistant", "content": "嗯，交给我吧。", "message_id": 2, "created_at": _iso(_at())},
        _message(3, "我妈这周末过来住"),
    ]

    cues = topic_cues(history, now=NOW)

    assert [cue.cue_id for cue in cues] == ["message:1", "message:3"]
    assert [cue.summary for cue in cues] == ["我周五要考运筹学", "我妈这周末过来住"]
    assert all(cue.source == "conversation" for cue in cues)


def test_topic_cues_drops_system_narration() -> None:
    """管家进度与任务回执是系统旁白，不是用户说过的话。"""
    history = [
        _message(1, "我下周三去拔牙"),
        {"role": "assistant", "content": "我去看看。", "message_id": 2, "created_at": _iso(_at()), "kind": "steward_progress"},
        {"role": "assistant", "content": "任务已完成。", "message_id": 3, "created_at": _iso(_at()), "kind": "task_result"},
    ]

    assert [cue.summary for cue in topic_cues(history, now=NOW)] == ["我下周三去拔牙"]


@pytest.mark.parametrize("content", ["好的", "嗯", "谢谢", "知道了", "嗯嗯嗯", "好的好的", "   ", "ok"])
def test_topic_cues_drops_low_information_replies(content: str) -> None:
    assert topic_cues([_message(1, content)], now=NOW) == ()


@pytest.mark.parametrize("content", ["考试", "搬家", "面试", "体检"])
def test_topic_cues_keeps_short_but_meaningful_topics(content: str) -> None:
    # 判据是清单而不是"长度短就扔"：两字话题恰恰是很好的联想素材。
    assert [cue.summary for cue in topic_cues([_message(1, content)], now=NOW)] == [content]


def test_topic_cues_drops_one_off_tool_commands() -> None:
    """「帮我查一下明天的天气」说过就过去，下周再提只会显得没在听。"""
    history = [
        _message(1, "帮我查一下明天的天气", days_ago=1),
        _message(2, "我下周三去拔牙", days_ago=1),
    ]

    assert [cue.summary for cue in topic_cues(history, now=NOW)] == ["我下周三去拔牙"]


def test_rank_cues_puts_topics_first_and_facts_by_importance() -> None:
    def fact(cue_id: str, *, importance: float, days_ago: float) -> OpeningCue:
        return OpeningCue(
            cue_id=cue_id, source="memory", summary=f"事实{cue_id}",
            occurred_at=_iso(_at(days_ago=days_ago)), importance=importance,
        )

    cues = [
        fact("fact:old-high", importance=0.9, days_ago=9),
        fact("fact:new-low", importance=0.2, days_ago=1),
        OpeningCue(cue_id="message:1", source="conversation", summary="三天前的事", occurred_at=_iso(_at(days_ago=3))),
        OpeningCue(cue_id="message:2", source="conversation", summary="今天的事", occurred_at=_iso(_at())),
    ]

    # 话题一律排在事实前：开场时提一句"你前几天说的那件事"远比念档案自然。
    # 事实之间先比重要度、再比时间；最后拿 cue_id 兜平局，保证顺序可复现。
    assert [cue.cue_id for cue in rank_cues(cues)] == [
        "message:2", "message:1", "fact:old-high", "fact:new-low",
    ]


def test_rank_cues_only_six_candidates_reach_the_selector() -> None:
    cues = [
        OpeningCue(
            cue_id=f"message:{index}", source="conversation", summary=f"近况{index}",
            occurred_at=_iso(_at(days_ago=index * 0.1)),
        )
        for index in range(1, 11)
    ]

    shortlist = rank_cues(cues)

    assert len(shortlist) == 6
    # 截的是最旧的：留下的一定是"最近说的"那几条。
    assert [cue.cue_id for cue in shortlist] == ["message:1", "message:2", "message:3", "message:4", "message:5", "message:6"]


def test_is_low_value_reply_collapses_word_level_repeats() -> None:
    assert is_low_value_reply("好的好的") is True
    assert is_low_value_reply("嗯嗯嗯") is True
    assert is_low_value_reply("准备周五的考试") is False


def test_topic_cues_keeps_only_the_latest_of_duplicate_text() -> None:
    history = [_message(1, "准备周五的考试", days_ago=3), _message(2, "准备周五的考试", days_ago=1)]

    assert [cue.cue_id for cue in topic_cues(history, now=NOW)] == ["message:2"]


def test_topic_cues_drops_anything_older_than_the_window() -> None:
    history = [_message(1, "上个月说过的旧事", days_ago=20), _message(2, "三天前说的事", days_ago=3)]

    assert [cue.cue_id for cue in topic_cues(history, now=NOW)] == ["message:2"]


def test_topic_cues_caps_count_and_truncates_summary() -> None:
    # days_ago 递减 → 列表本身就是时间正序（1 最旧、6 最新）。
    history = [_message(index, f"话题{index}" + "很" * 100, days_ago=7 - index) for index in range(1, 7)]

    cues = topic_cues(history, now=NOW, limit=2, max_chars=10)

    assert len(cues) == 2
    assert all(len(cue.summary) == 10 for cue in cues)
    # 取的是最近两条，并按时间正序返回：越靠后越新。
    assert [cue.cue_id for cue in cues] == ["message:5", "message:6"]


def test_topic_cues_does_not_trust_the_input_order() -> None:
    # 传一个时间倒序的列表，"最近两条"不该变成"最旧两条"。
    history = [_message(1, "最早说的事", days_ago=5), _message(2, "最近说的事", days_ago=1)]

    assert [cue.cue_id for cue in topic_cues(history, now=NOW)] == ["message:1", "message:2"]


# ── 线索：事实 ──────────────────────────────────────────────────────────
def test_fact_cues_carry_stable_id_time_and_category(repositories) -> None:
    record = _save_fact(repositories, _scope(), "用户养了一只叫团子的猫")

    cues = fact_cues([record])

    assert len(cues) == 1
    assert cues[0].cue_id == f"fact:{record['id']}"
    assert cues[0].source == "memory"
    assert cues[0].summary == "用户养了一只叫团子的猫"
    assert cues[0].occurred_at == record["created_at"]


# ── 输出判定 ────────────────────────────────────────────────────────────
def test_normalize_greeting_strips_wrappers_and_keeps_first_line() -> None:
    assert normalize_greeting('```\n开场白：「哦，来了啊。」\n第二句\n```') == "哦，来了啊。"


def test_short_natural_greetings_are_now_acceptable() -> None:
    # 下限从 12 改到 4 就是为了放行这一类。
    for line in ("回来啦？", "在忙吗？", "嗯，来了啊。", "今天过得怎么样？"):
        assert greeting_issue(line) is None, f"{line} 应当合格"


def test_greeting_issue_classifies_every_rejection() -> None:
    cases = {
        "": "empty",
        "嗯。": "too_short",
        "这是一句刻意写得非常长的开场白，长到远远超过三十五个字的上限，用来验证服务层会把它判为不合格。": "too_long",
        "好的，有什么可以帮您的吗？": "service_tone",
        "亲爱的，今天也一起来聊聊天好不好呀。": "intimate",
        "我看看 work_item 那个步骤走到哪了。": "internal",
    }
    for text, expected in cases.items():
        assert greeting_issue(text) == expected, f"{text!r} 应判为 {expected}"


def test_a_time_word_is_no_longer_judged_against_the_tier() -> None:
    # 档位停用（2026-09-18）：原来这一句在 immediate 档会被判 time_conflict
    # （"刚刷新页面却说昨天"）。现在档位不参与判断，两种档位下都不再因此不合格。
    line = "昨天那个天气查询的活，后来顺利吗？"

    assert greeting_issue(line, absence_tier="immediate") is None
    assert greeting_issue(line, absence_tier="very_long") is None


# ── 措辞输出解析 ────────────────────────────────────────────────────────
def test_parse_narration_reads_a_plain_greeting() -> None:
    assert parse_narration('{"greeting": "回来啦？"}') == "回来啦？"


def test_parse_narration_reads_a_recall_through_code_fences() -> None:
    assert parse_narration('```json\n{"greeting": "嗯，周五那场考试准备得怎么样了？"}\n```') == "嗯，周五那场考试准备得怎么样了？"


def test_parse_narration_tolerates_text_around_the_json() -> None:
    assert parse_narration('好的，这是结果：{"greeting": "回来啦？"} 希望有帮助。') == "回来啦？"


def test_parse_narration_ignores_a_self_reported_cue() -> None:
    """模型多报回来的 id 与大纲**不读**：引用了哪件事由服务端选题时定。

    留着这条断言是因为它挡的是一类会"看起来完全正常"的回归 —— 只要有人重新
    去读这些字段，一次编造就会被记成"引用过的旧事"，之后几轮都按错的台账判重。
    """
    raw = '{"greeting": "回来啦？", "used_cue_id": "message:999", "cue_key": "瞎编的一件事"}'

    assert parse_narration(raw) == "回来啦？"


@pytest.mark.parametrize(
    "raw",
    [
        "哦，来了啊。",
        "[1, 2, 3]",
        '{"greeting": 42}',
        '{"used_cue_id": null}',
        "",
    ],
    ids=["not_json", "not_object", "greeting_not_str", "no_greeting", "empty"],
)
def test_parse_narration_rejects_malformed_payloads(raw: str) -> None:
    assert parse_narration(raw) is None


# ── 判重键：由服务端从线索摘要算出 ──────────────────────────────────────
def test_the_dedupe_key_is_the_normalized_summary() -> None:
    cue = OpeningCue(cue_id="message:1", source="conversation", summary=" 我下周三去拔牙。 ", occurred_at="")

    assert cue_dedupe_key(cue) == "我下周三去拔牙"


def test_a_long_summary_is_capped_at_the_key_limit() -> None:
    cue = OpeningCue(cue_id="message:1", source="conversation", summary="事" * 60, occurred_at="")

    assert len(cue_dedupe_key(cue)) == 40


@pytest.mark.parametrize("summary", ["。", "...", "，。、；：", "   ", "——"])
def test_a_summary_that_cannot_be_keyed_is_never_a_candidate(summary: str) -> None:
    """定不出判重键的线索不进候选：提过之后记不住，等于下一次还能再提一遍。"""
    cue = OpeningCue(cue_id="fact:1", source="memory", summary=summary, occurred_at="")

    assert available_cues([cue], []) == []


def test_the_key_written_and_the_key_filtered_are_the_same_function(recall_store) -> None:
    """台账里存的键必须正是筛候选时比对的那把 —— 两处各算一遍就会悄悄分叉。"""
    scope = _scope()
    cue = OpeningCue(
        cue_id="message:11", source="conversation", summary="我下周三去拔牙", occurred_at=_iso(_at()),
    )
    recall_store.record(
        scope, visit_id="visit-key", greeting_kind=GREETING_TOPIC,
        cue_id=cue.cue_id, cue_key=cue_dedupe_key(cue), now=_at(days_ago=1),
    )

    assert available_cues([cue], recall_store.recent_events(scope, now=NOW), now=NOW) == []


# ── 闸门与冷却 ──────────────────────────────────────────────────────────
def test_first_visit_may_recall() -> None:
    assert should_allow_recall([], absence_tier="long") is True


def test_absence_tier_no_longer_gates_recall() -> None:
    # 停用前：immediate 档一律不允许联想（那个档位意味着"可能只是刷新了页面"）。
    # 停用后这一条随档位一起退场 —— "刷新不重演问候"由前端进入槽负责，
    # 闸门只剩"隔开一次普通问候"这一条。判据就是"档位不再改变结果"。
    for events in ([], [{"greeting_kind": GREETING_PLAIN}], [{"greeting_kind": GREETING_PLAIN}] * 5):
        assert should_allow_recall(events, absence_tier="immediate") == should_allow_recall(
            events, absence_tier="very_long"
        ), "档位不该再影响这个判断"

    # 而且首次进入（没有任何记录）在两种档位下都可联想。
    assert should_allow_recall([], absence_tier="immediate") is True


def test_one_plain_greeting_is_enough_before_recalling_again() -> None:
    topic_first = [{"greeting_kind": GREETING_TOPIC}]
    one_plain = [{"greeting_kind": GREETING_PLAIN}, *topic_first]
    two_plain = [{"greeting_kind": GREETING_PLAIN}, {"greeting_kind": GREETING_PLAIN}, *topic_first]

    assert should_allow_recall(topic_first, absence_tier="long") is False
    assert should_allow_recall(one_plain, absence_tier="long") is True
    assert should_allow_recall(two_plain, absence_tier="long") is True


def test_topic_cooldown_lasts_seven_days() -> None:
    events = [{"greeting_kind": GREETING_TOPIC, "cue_id": "message:1", "cue_key": "查明天的天气", "created_at": _iso(_at(days_ago=3))}]

    ids, keys = cooldown_blocklist(events, now=NOW)

    assert ids == frozenset({"message:1"})
    assert keys == frozenset({normalize_cue_key("查明天的天气")})


def test_topic_cooldown_expires_after_seven_days() -> None:
    events = [{"greeting_kind": GREETING_TOPIC, "cue_id": "message:1", "cue_key": "查明天的天气", "created_at": _iso(_at(days_ago=8))}]

    assert cooldown_blocklist(events, now=NOW) == (frozenset(), frozenset())


def test_fact_cooldown_outlasts_a_topic_of_the_same_age() -> None:
    fact = {"greeting_kind": GREETING_FACT, "cue_id": "fact:a", "cue_key": "对花生过敏", "created_at": _iso(_at(days_ago=29))}
    topic = {"greeting_kind": GREETING_TOPIC, "cue_id": "message:1", "cue_key": "查明天的天气", "created_at": _iso(_at(days_ago=29))}

    ids, _ = cooldown_blocklist([fact, topic], now=NOW)

    # 同样 29 天：事实还在 30 天冷却里，对话早已出了 7 天冷却 —— 这正是两档的意义。
    assert ids == frozenset({"fact:a"})


def test_plain_greetings_never_block_anything() -> None:
    events = [{"greeting_kind": GREETING_PLAIN, "cue_id": None, "cue_key": None, "created_at": _iso(_at())}]

    assert cooldown_blocklist(events, now=NOW) == (frozenset(), frozenset())


def test_a_rewritten_cue_key_is_still_blocked() -> None:
    """换一种说法讲同一件事，也要被认出来。"""
    events = [{"greeting_kind": GREETING_TOPIC, "cue_id": "message:1", "cue_key": "  准备周五的考试。 ", "created_at": _iso(_at(days_ago=1))}]

    _, keys = cooldown_blocklist(events, now=NOW)

    assert normalize_cue_key("准备 周五的考试") in keys


# ── 台账 ────────────────────────────────────────────────────────────────
def test_recording_the_same_visit_twice_leaves_one_row(recall_store) -> None:
    scope = _scope()

    for _ in range(3):
        assert recall_store.record(scope, visit_id=VISIT, greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW) is True

    with recall_store._pool.get_connection() as db:
        rows = db.execute("SELECT visit_id FROM agent_core_opening_recall_events").fetchall()
    assert [row["visit_id"] for row in rows] == [VISIT]


def test_recall_history_is_shared_across_conversations_but_not_accounts(recall_store) -> None:
    recall_store.record(_scope(conversation="web-a"), visit_id="visit-aaaaaaaa", greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW)
    recall_store.record(_scope(account=OTHER_ACCOUNT), visit_id="visit-bbbbbbbb", greeting_kind=GREETING_TOPIC, cue_id="message:9", cue_key="别人的事", now=NOW)

    events = recall_store.recent_events(_scope(conversation="web-b"), now=NOW)

    # 换会话也要看到（否则切个会话就能把同一件事再提一遍）；换账号看不到。
    assert [event["cue_id"] for event in events] == ["message:1"]


def test_events_older_than_the_retention_window_are_purged_on_write(recall_store) -> None:
    scope = _scope()
    with recall_store._pool.get_connection() as db:
        db.execute(
            """INSERT INTO agent_core_opening_recall_events
               (event_id, owner_id, channel, conversation_id, module_id, visit_id,
                greeting_kind, cue_id, cue_key, created_at)
               VALUES ('old', ?, 'web', ?, 'companion', 'visit-old', 'plain', NULL, NULL, ?)""",
            (ACCOUNT, CONVERSATION, _iso(_at(days_ago=200))),
        )

    recall_store.record(scope, visit_id=VISIT, greeting_kind=GREETING_PLAIN, now=NOW)

    with recall_store._pool.get_connection() as db:
        remaining = db.execute("SELECT visit_id FROM agent_core_opening_recall_events").fetchall()
    assert [row["visit_id"] for row in remaining] == [VISIT]


# ── 服务层：提示词 ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_topic_cue_reaches_the_selector_with_id_and_relative_time(repositories, recall_store) -> None:
    """id 与相对时间都是选题阶段的东西：id 是它要照抄的答案，时间是算好的素材。"""
    provider = ScriptedProvider(cue_id="message:11", greeting="拔牙约在周三了吧，定了吗？")
    service = _service(provider, repositories=repositories, recall_store=recall_store)

    await service.generate(
        _scope(),
        visit_id=VISIT,
        absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)],
        now=NOW,
    )

    prompt = provider.prompt_text_of("selector")
    assert "message:11" in prompt
    assert "昨天" in prompt                      # 相对时间由本地算好，不让模型自己减日期
    assert "我下周三去拔牙" in prompt
    # 措辞阶段只要这一件事本身，不要再把 id 递给它当靶子。
    assert "message:11" not in provider.prompt_text_of("narrator")


@pytest.mark.asyncio
async def test_the_two_prompts_hold_different_rules(repositories, recall_store) -> None:
    """选题段只有选择权，措辞段只有措辞权 —— 两段的禁令各自写死。"""
    provider = ScriptedProvider()

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    selector = provider.prompt_text_of("selector")
    assert "只选择一个适合在开场时自然提起的话题" in selector
    assert "不要选临时查询、工具命令、普通寒暄" in selector
    assert "没有合适内容就选 null" in selector
    assert "不要写开场白" in selector

    narrator = provider.prompt_text_of("narrator")
    assert "不要扩写" in narrator
    assert "不要执行" in narrator
    assert "真实出现过" in narrator
    assert "selected_cue_id" not in narrator      # 措辞段没有申报线索的字段
    assert '{"greeting": "那一句话"}' in narrator


@pytest.mark.asyncio
async def test_only_confirmed_shared_facts_become_cues(repositories, recall_store) -> None:
    scope = _scope()
    _save_fact(repositories, scope, "用户养了一只叫团子的猫")
    _writer(repositories).save_candidate(scope, "用户可能在准备考试", confidence=0.5)
    _save_fact(repositories, _scope(account=OTHER_ACCOUNT), "另一个账号的秘密")
    expired = _save_fact(repositories, scope, "用户上个月在减肥")
    archived = _save_fact(repositories, scope, "用户以前住在城西")
    with repositories.connect() as db:
        db.execute("UPDATE memory_records SET status='expired' WHERE id=?", (expired["id"],))
        db.execute("UPDATE memory_records SET layer='cold', status='archived' WHERE id=?", (archived["id"],))

    provider = ScriptedProvider()
    service = _service(provider, repositories=repositories, recall_store=recall_store)
    material = service.collect_material(scope)
    await service.generate(scope, visit_id=VISIT, absence_tier="long", now=NOW)

    assert [cue.summary for cue in material.facts] == ["用户养了一只叫团子的猫"]
    for forbidden in ("用户可能在准备考试", "另一个账号的秘密", "用户上个月在减肥", "用户以前住在城西"):
        assert forbidden not in provider.prompt_text, f"{forbidden} 不该进入提示词"


@pytest.mark.asyncio
async def test_a_sensitive_but_confirmed_fact_may_be_recalled(repositories, recall_store) -> None:
    """健康、情绪、金钱不做类别级排除 —— 只要它是用户明确说过并被确认的。"""
    record = _save_fact(repositories, _scope(), "用户对花生过敏")
    provider = ScriptedProvider(cue_id=f"fact:{record['id']}")

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long", now=NOW
    )

    assert "用户对花生过敏" in provider.prompt_text
    assert greeting == provider.greeting


@pytest.mark.asyncio
async def test_a_brand_new_conversation_can_use_the_accounts_facts(repositories, recall_store) -> None:
    _save_fact(repositories, _scope(conversation="web-old"), "用户对花生过敏")
    provider = ScriptedProvider()

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(conversation="web-brand-new"), visit_id=VISIT, absence_tier="long", now=NOW
    )

    assert "用户对花生过敏" in provider.prompt_text


@pytest.mark.asyncio
async def test_no_material_at_all_still_produces_a_plain_greeting(repositories, recall_store) -> None:
    provider = ScriptedProvider("嗯，来了啊。今天想聊点什么？")

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long", now=NOW
    )

    assert greeting == "嗯，来了啊。今天想聊点什么？"
    assert "没有可用的旧事素材" in provider.prompt_text


@pytest.mark.asyncio
async def test_prepared_plain_greeting_skips_model_and_records_plain_visit(repositories, recall_store) -> None:
    provider = ScriptedProvider(error=RuntimeError("model unavailable"))
    service = _service(
        provider,
        repositories=repositories,
        recall_store=recall_store,
        prepared_plain=lambda visit_id: "哟，来啦。",
    )

    greeting = await service.generate(_scope(), visit_id=VISIT, now=NOW)

    assert greeting == "哟，来啦。"
    assert provider.calls == []
    assert recall_store.recent_events(_scope(), now=NOW)[0]["greeting_kind"] == GREETING_PLAIN


@pytest.mark.asyncio
async def test_prepared_plain_greeting_avoids_second_model_call_after_null_selection(repositories, recall_store) -> None:
    provider = ScriptedProvider(selector_raw='{"selected_cue_id": null}')
    service = _service(
        provider,
        repositories=repositories,
        recall_store=recall_store,
        prepared_plain=lambda visit_id: "嗯，你来了。",
    )

    greeting = await service.generate(
        _scope(), visit_id=VISIT, history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "嗯，你来了。"
    assert provider.stages == ["selector"]


@pytest.mark.asyncio
async def test_provider_is_called_without_tools_and_with_fixed_budget(repositories, recall_store) -> None:
    provider = ScriptedProvider()

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, now=NOW
    )

    call = provider.calls[0]
    assert call["tools"] is None
    assert call["max_tokens"] == 96
    assert call["temperature"] == 0.4


# ── 服务层：闸门接进链路 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_absence_tier_no_longer_withholds_cues(repositories, recall_store) -> None:
    # 停用前：immediate 档即使有话题也不下发线索（"刚刷新页面，别提旧事"）。
    # 停用后档位不再参与判断 —— 首次进入（没有任何记录）本来就允许联想，
    # 所以同样的素材在 immediate 档下也会进提示词。
    # 真正承担"刷新不重演"的是前端进入槽：同一次进入不会再问第二次。
    provider = ScriptedProvider("回来啦？")

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="immediate",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert "message:11" in provider.prompt_text_of("selector")


@pytest.mark.asyncio
async def test_a_cooling_cue_is_withheld_but_a_fresh_one_is_offered(repositories, recall_store) -> None:
    scope = _scope()
    # 只把 message:11 记成"提过"（3 天前，还在 7 天冷却里）；message:12 从未提过。
    _recall(recall_store, scope, days_ago=3, cue_id="message:11", cue_key="我下周三去拔牙")
    # 用过线索之后要隔开一次普通问候才允许再联想。
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    provider = ScriptedProvider()

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=3), _message(12, "我妈这周末过来住", days_ago=3)],
        now=NOW,
    )

    selector = provider.prompt_text_of("selector")
    assert "message:11" not in selector   # 3 天前提过同一件事，还在 7 天冷却里
    assert "message:12" in selector       # 另一件事没提过，可以联想


@pytest.mark.asyncio
async def test_the_recall_gate_skips_the_selector_entirely(repositories, recall_store) -> None:
    """闸门关着时候选就是空的 —— 连"挑一件"这一步都不必问。"""
    scope = _scope()
    _recall(recall_store, scope, days_ago=30, cue_id="message:11", cue_key="我下周三去拔牙")
    provider = ScriptedProvider("回来啦？")

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    # 上一次线索之后还没隔开一次普通问候：闸门关着，一条候选都不下发。
    assert provider.stages == ["narrator"]
    assert _cue_lines(provider.prompt_text) == []


@pytest.mark.asyncio
async def test_cooling_outlines_never_reach_a_model(repositories, recall_store) -> None:
    """冷却大纲是**服务端筛候选用的**，即使这一步正常问了模型也不给它看。

    以前会把最近提过的那几件事列进提示词、要求模型"沿用同样的措辞别撞车" ——
    那是因为它有权自己挑一件事并申报大纲。现在选题与判重都在服务端，
    把这些大纲交给模型只剩一条坏处：它完全可以照着大纲把刚提过的事
    换个说法再讲一遍，而服务端无从判起。
    """
    scope = _scope()
    _recall(recall_store, scope, days_ago=1, cue_id="message:9", cue_key="准备周五的考试")
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    provider = ScriptedProvider(cue_id="message:11")

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[
            _message(11, "我下周三去拔牙", days_ago=1),
            _message(12, "准备周五的考试", days_ago=0.5),
        ],
        now=NOW,
    )

    assert "selector" in provider.stages            # 闸门是开着的，确实问了
    selector = provider.prompt_text_of("selector")
    assert "message:11" in selector                  # 能提的那条照常下发
    assert "准备周五的考试" not in provider.prompt_text, "正在冷却的事一个字都不该出去"
    assert "message:12" not in selector


@pytest.mark.asyncio
async def test_a_fact_cue_is_shared_across_conversations(repositories, recall_store) -> None:
    """在另一个会话里提过的事实，换个会话也不该再提一遍。"""
    record = _save_fact(repositories, _scope(), "用户养了一只叫团子的猫")
    recall_store.record(
        _scope(conversation="web-other"),
        visit_id="visit-other",
        greeting_kind=GREETING_FACT,
        cue_id=f"fact:{record['id']}",
        cue_key="养了一只叫团子的猫",
        now=_at(days_ago=2),
    )
    provider = ScriptedProvider()

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long", now=NOW
    )

    assert f"fact:{record['id']}" not in provider.prompt_text, "别处刚提过，正冷却，不该出现在候选里"


# ── 服务层：失败与降级 ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "provider_kwargs",
    [
        {"error": RuntimeError("模型挂了")},
        {"raw": ""},
        {"raw": "这不是 JSON"},
        {"raw": '{"greeting": 42}'},
        {"raw": '{"text": "回来啦？"}'},
        {"raw": '{"greeting": "这是一句刻意写得非常长的开场白，长到远远超过三十五个字的上限，用来验证服务层会把它判为不合格。"}'},
        {"raw": '{"greeting": "好的，有什么可以帮您的吗？"}'},
    ],
    ids=["model_error", "empty", "not_json", "greeting_not_str", "no_greeting", "overlong", "service_tone"],
)
@pytest.mark.asyncio
async def test_bad_model_output_falls_back_to_the_fixed_line(repositories, recall_store, provider_kwargs) -> None:
    greeting = await _service(ScriptedProvider(**provider_kwargs), repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == DEFAULT_GREETING


@pytest.mark.asyncio
async def test_a_slow_model_times_out_into_the_fixed_line(repositories, recall_store) -> None:
    provider = ScriptedProvider(delay=0.5)

    # 措辞段有**自己的**预算（narration_timeout_seconds）：两段式之后不再共用
    # 一个 4 秒总预算，所以这里必须掐措辞那一段，而不是笼统的 timeout_seconds。
    greeting = await _service(
        provider, repositories=repositories, recall_store=recall_store, narration_timeout_seconds=0.05
    ).generate(_scope(), visit_id=VISIT, now=NOW)

    assert greeting == DEFAULT_GREETING


@pytest.mark.asyncio
async def test_a_failed_retrieval_falls_back_without_calling_the_model() -> None:
    class BrokenRepository:
        def recent_user_messages(self, scope, **kwargs):
            raise RuntimeError("对话库读不出来")

        def history(self, scope):
            raise RuntimeError("对话库读不出来")

    class BrokenMemory:
        def confirmed_shared_facts(self, scope, *, limit):
            raise RuntimeError("记忆库读不出来")

    provider = ScriptedProvider()

    greeting = await _service(
        provider, conversation_repository=BrokenRepository(), repositories=BrokenMemory()
    ).generate(_scope(), visit_id=VISIT, now=NOW)

    assert greeting == DEFAULT_GREETING
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_failed_recall_history_falls_back_without_calling_the_model(repositories) -> None:
    """"不知道提过什么"只能闭嘴 —— 随口说一句就可能把上周刚提过的事再说一遍。"""
    provider = ScriptedProvider()

    greeting = await _service(provider, repositories=repositories, recall_store=FailingStore()).generate(
        _scope(), visit_id=VISIT, absence_tier="long", now=NOW
    )

    assert greeting == DEFAULT_GREETING
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_missing_recall_store_disables_recall_but_keeps_the_greeting(repositories) -> None:
    """没有台账 → 整条个性化链路关掉，普通问候照常。

    没有记录就没有"这件事提过没有"的答案。这时提起旧事，同一件事下周还会再提
    一遍 —— 那种情况下宁可只说句普通问候。
    """
    provider = ScriptedProvider("回来啦？")

    greeting = await _service(provider, repositories=repositories, recall_store=None).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "回来啦？"
    assert provider.stages == ["narrator"], "连选题都不必问：候选是空的"
    assert "message:11" not in provider.prompt_text


@pytest.mark.asyncio
async def test_a_missing_provider_still_returns_the_fixed_line(repositories, recall_store) -> None:
    greeting = await _service(None, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, now=NOW
    )

    assert greeting == DEFAULT_GREETING


@pytest.mark.asyncio
async def test_a_plain_greeting_is_still_returned_when_the_record_cannot_be_written(repositories) -> None:
    """普通问候的正确性不依赖记录，少记一次只影响节奏。"""
    provider = ScriptedProvider("回来啦？")

    greeting = await _service(provider, repositories=repositories, recall_store=BrokenStore()).generate(
        _scope(), visit_id=VISIT, absence_tier="long", now=NOW
    )

    assert greeting == "回来啦？"


@pytest.mark.asyncio
async def test_generating_a_greeting_writes_neither_history_nor_memory(conversation_repository, repositories, recall_store) -> None:
    scope = _scope()
    conversation_repository.insert_message(scope, {"role": "user", "content": "我下周三去拔牙"})
    before_history = len(conversation_repository.history(scope))
    before_memory = len(repositories.recent_hot_records(scope))

    await _service(
        ScriptedProvider(),
        conversation_repository=conversation_repository,
        repositories=repositories,
        recall_store=recall_store,
    ).generate(scope, visit_id=VISIT, absence_tier="long", now=NOW)

    assert len(conversation_repository.history(scope)) == before_history
    assert len(repositories.recent_hot_records(scope)) == before_memory


@pytest.mark.asyncio
async def test_every_successful_greeting_is_recorded_with_its_kind(repositories, recall_store) -> None:
    provider = ScriptedProvider(cue_id="message:11")

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    events = recall_store.recent_events(_scope(), now=NOW)
    # cue_key 不是模型报回来的，是服务端从选中那条线索的摘要算的。
    assert [(event["greeting_kind"], event["cue_id"], event["cue_key"]) for event in events] == [
        (GREETING_TOPIC, "message:11", normalize_cue_key("我下周三去拔牙"))
    ]


@pytest.mark.asyncio
async def test_the_history_read_is_limited_and_excludes_the_assistant(conversation_repository, repositories, recall_store) -> None:
    scope = _scope()
    for index in range(20):
        conversation_repository.insert_message(scope, {"role": "user", "content": f"第 {index} 个话题"})
        conversation_repository.insert_message(scope, {"role": "assistant", "content": f"第 {index} 个回复"})

    captured: dict = {}
    real = conversation_repository.recent_user_messages

    def spy(*args, **kwargs):
        rows = real(*args, **kwargs)
        captured["limit"] = kwargs.get("limit")
        captured["across_conversations"] = kwargs.get("across_conversations")
        captured["rows"] = rows
        return rows

    conversation_repository.recent_user_messages = spy  # type: ignore[method-assign]

    await _service(
        ScriptedProvider(),
        conversation_repository=conversation_repository,
        repositories=repositories,
        recall_store=recall_store,
    ).generate(scope, visit_id=VISIT, absence_tier="long", now=NOW)

    assert captured["limit"] == 60
    assert len(captured["rows"]) == 20
    assert all(row["role"] == "user" for row in captured["rows"])
    # 素材必须**跨会话**读：重开 Web 版是一个新会话，只按当前会话读会永远无素材。
    assert captured["across_conversations"] is True


@pytest.mark.asyncio
async def test_tool_commands_do_not_hide_an_older_real_topic(repositories, recall_store) -> None:
    messages = [_message(1, "我下周三去拔牙", days_ago=1)]
    messages.extend(_message(index, "帮我查一下明天的天气", days_ago=1) for index in range(2, 15))

    class RecentRepository:
        def recent_user_messages(self, scope, *, limit, **kwargs):
            return messages[-limit:]

    provider = ScriptedProvider(cue_id="message:1")
    await _service(
        provider, conversation_repository=RecentRepository(),
        repositories=repositories, recall_store=recall_store,
    ).generate(_scope(), visit_id=VISIT, now=NOW)

    assert "message:1" in provider.prompt_text_of("selector")


@pytest.mark.asyncio
async def test_a_brand_new_conversation_still_sees_the_previous_topics(
    conversation_repository, repositories, recall_store
) -> None:
    """重开 Web 版是一个**新会话**，但开场白仍要记得上个会话里说过的事。

    这条界限是 2026-09-18 定的：叉掉标签页 = 这次会话结束、重开 Web 版算新会话
    （会话 id 落在 sessionStorage），旧对话留在历史会话列表里可以切回。
    新会话里一条消息都没有 —— 素材若只按当前会话读，开场白就永远是"哦，来了啊"。
    """
    conversation_repository.insert_message(
        _scope(conversation="web-yesterday"),
        {"role": "user", "content": "我妈这周末过来住"},
    )

    provider = ScriptedProvider()
    await _service(
        provider,
        conversation_repository=conversation_repository,
        repositories=repositories,
        recall_store=recall_store,
    ).generate(_scope(conversation="web-today"), visit_id=VISIT, now=NOW)

    assert "我妈这周末过来住" in provider.prompt_text


# ── 缓存 ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_same_visit_id_calls_the_model_once_even_when_concurrent() -> None:
    provider = ScriptedProvider(delay=0.05)
    service = _service(provider)
    cache = OpeningGreetingCache()

    results = await asyncio.gather(*[
        cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))
        for _ in range(5)
    ])

    assert set(results) == {provider.greeting}
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_the_same_visit_id_reuses_the_cached_result_on_retry() -> None:
    provider = ScriptedProvider()
    service = _service(provider)
    cache = OpeningGreetingCache()

    first = await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))
    second = await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))

    assert first == second == provider.greeting
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_different_visits_call_the_model_separately() -> None:
    provider = ScriptedProvider()
    service = _service(provider)
    cache = OpeningGreetingCache()

    await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))
    await cache.run("acct|web|conv|visit-2", lambda: service.generate(_scope(), visit_id="visit-2"))

    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_expired_entries_are_regenerated_but_running_ones_are_kept() -> None:
    provider = ScriptedProvider()
    service = _service(provider)
    cache = OpeningGreetingCache(ttl_seconds=0.01)

    await cache.run("key", lambda: service.generate(_scope(), visit_id="visit-1"))
    await asyncio.sleep(0.05)
    await cache.run("key", lambda: service.generate(_scope(), visit_id="visit-1"))

    assert len(provider.calls) == 2
    assert len(cache) == 1


@pytest.mark.asyncio
async def test_the_cache_never_exceeds_its_entry_limit() -> None:
    provider = ScriptedProvider()
    service = _service(provider)
    cache = OpeningGreetingCache(max_entries=2)

    for index in range(5):
        await cache.run(f"key-{index}", lambda: service.generate(_scope(), visit_id=f"visit-{index}"))

    assert len(cache) == 2


@pytest.mark.asyncio
async def test_a_failed_generation_is_not_cached() -> None:
    cache = OpeningGreetingCache()
    attempts = 0

    async def boom() -> str:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("炸了")

    assert await cache.run("key", boom) == DEFAULT_GREETING
    assert len(cache) == 0
    assert await cache.run("key", boom) == DEFAULT_GREETING
    assert attempts == 2


# ── HTTP 接口 ───────────────────────────────────────────────────────────
def _url(conversation: str = CONVERSATION) -> str:
    return f"/api/conversations/{conversation}/opening-greeting"


@pytest.fixture()
def greeting_client(conversation_repository, repositories, recall_store, monkeypatch):
    """把假 provider 与临时台账装进接口，其余按真实路径走（Scope 组装、校验、缓存）。"""

    def build(provider: ScriptedProvider):
        import web_api

        monkeypatch.setattr(web_api, "conversation_repository", conversation_repository)
        monkeypatch.setattr(web_api, "manager", None)
        monkeypatch.setattr(web_api, "_repositories", repositories)
        monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
        monkeypatch.setattr(web_api, "_opening_recall_store", recall_store)
        monkeypatch.setattr(
            web_api,
            "_opening_greeting_service",
            OpeningGreetingService(
                provider,
                conversation_repository=conversation_repository,
                repositories=repositories,
                recall_store=recall_store,
            ),
        )
        monkeypatch.setattr(web_api, "_opening_greeting_cache", OpeningGreetingCache())
        return TestClient(web_api.app), provider

    return build


def test_the_endpoint_returns_the_greeting_and_echoes_the_visit_id(greeting_client) -> None:
    client, provider = greeting_client(ScriptedProvider("回来啦？"))
    response = client.post(_url(), json={"account_id": ACCOUNT, "visit_id": VISIT, "absence_tier": "short"})

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": CONVERSATION,
        "visit_id": VISIT,
        "greeting": "回来啦？",
    }
    assert provider.calls[0]["tools"] is None


def test_a_repeated_visit_calls_the_model_once_and_records_once(greeting_client, recall_store) -> None:
    """React 重挂载、网络重试都带同一个 visit_id，只该花一次 Token、留一条记录。"""
    client, provider = greeting_client(ScriptedProvider())
    payload = {"account_id": ACCOUNT, "visit_id": VISIT, "absence_tier": "medium"}

    first = client.post(_url(), json=payload)
    second = client.post(_url(), json=payload)

    assert first.json()["greeting"] == second.json()["greeting"]
    assert len(provider.calls) == 1
    with recall_store._pool.get_connection() as db:
        rows = db.execute("SELECT visit_id FROM agent_core_opening_recall_events").fetchall()
    assert [row["visit_id"] for row in rows] == [VISIT]


def test_the_same_visit_id_is_isolated_per_account_and_conversation(greeting_client) -> None:
    client, provider = greeting_client(ScriptedProvider())
    payload = {"account_id": ACCOUNT, "visit_id": VISIT, "absence_tier": "medium"}

    client.post(_url(), json=payload)
    client.post(_url("web-elsewhere"), json=payload)
    client.post(_url(), json={**payload, "account_id": OTHER_ACCOUNT})

    assert len(provider.calls) == 3


def test_invalid_visit_ids_are_rejected(greeting_client) -> None:
    client, _ = greeting_client(ScriptedProvider())

    for bad in ("", "short", "有中文的-visit-id", "x" * 65, "has space here"):
        response = client.post(_url(), json={"account_id": ACCOUNT, "visit_id": bad, "absence_tier": "medium"})
        assert response.status_code == 422, f"visit_id={bad!r} 应当被拒绝"


def test_an_unknown_absence_tier_is_rejected_instead_of_silently_downgraded(greeting_client) -> None:
    client, provider = greeting_client(ScriptedProvider())

    response = client.post(_url(), json={"account_id": ACCOUNT, "visit_id": VISIT, "absence_tier": "yesterday"})

    assert response.status_code == 422
    assert provider.calls == []


def test_the_endpoint_uses_a_prepared_line_when_the_model_fails(greeting_client) -> None:
    client, _ = greeting_client(ScriptedProvider(error=RuntimeError("模型挂了")))

    response = client.post(_url(), json={"account_id": ACCOUNT, "visit_id": VISIT, "absence_tier": "long"})

    assert response.status_code == 200
    assert response.json()["greeting"] in GREETING_POOL


def test_the_endpoint_uses_a_prepared_line_when_generation_times_out(
    greeting_client, conversation_repository, monkeypatch
) -> None:
    conversation_repository.insert_message(_scope(), {"role": "user", "content": "我下周三要去拔牙"})
    client, provider = greeting_client(
        ScriptedProvider(delay=0.6, selector_raw='{"selected_cue_id":null}')
    )
    import web_api
    monkeypatch.setattr(web_api._opening_greeting_service, "narration_timeout_seconds", 0.4)

    response = client.post(_url(), json={"account_id": ACCOUNT, "visit_id": VISIT})

    assert response.status_code == 200
    assert provider.stages == ["selector", "narrator"]
    assert response.json()["greeting"] == prepared_greeting(VISIT)


# ── 可靠性修复：读取路径、派生数据生命周期、服务端判重 ──────────────────
def _event_rows(store, *, conversation: str = CONVERSATION) -> list[tuple]:
    with store._pool.get_connection() as db:
        rows = db.execute(
            "SELECT cue_id, cue_key FROM agent_core_opening_recall_events WHERE conversation_id=?",
            (conversation,),
        ).fetchall()
    return [(row["cue_id"], row["cue_key"]) for row in rows]


def test_the_endpoint_reads_from_the_database_not_the_live_manager(greeting_client, conversation_repository, monkeypatch) -> None:
    """内存态消息可能还没落库、因而没有 created_at —— 拿它当素材来源，
    结果会是"越新的消息越容易被丢掉"。接口改为统一从数据库限量读。"""
    client, provider = greeting_client(ScriptedProvider())
    import web_api

    class ExplodingManager:
        def history(self, scope):
            raise AssertionError("开场白不该再读内存态历史")

    monkeypatch.setattr(web_api, "manager", ExplodingManager())
    conversation_repository.insert_message(_scope(), {"role": "user", "content": "库里的话题：帮我订张车票"})

    response = client.post(_url(), json={"account_id": ACCOUNT, "visit_id": VISIT, "absence_tier": "long"})

    assert response.status_code == 200
    assert "帮我订张车票" in provider.prompt_text


def test_clearing_a_conversation_drops_its_recall_events(conversation_repository, recall_store) -> None:
    scope = _scope()
    keep = _scope(conversation="web-keep")
    recall_store.record(scope, visit_id="visit-aaaaaaaa", greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW)
    recall_store.record(keep, visit_id="visit-bbbbbbbb", greeting_kind=GREETING_TOPIC, cue_id="message:2", cue_key="别的事", now=NOW)

    conversation_repository.clear(scope)

    assert _event_rows(recall_store) == []
    assert _event_rows(recall_store, conversation="web-keep") == [("message:2", "别的事")]


def test_deleting_a_conversation_drops_its_recall_events(conversation_repository, recall_store) -> None:
    scope = _scope()
    conversation_repository.insert_message(scope, {"role": "user", "content": "一段要删掉的对话"})
    recall_store.record(scope, visit_id="visit-aaaaaaaa", greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW)

    conversation_repository.delete(scope)

    assert _event_rows(recall_store) == []


def test_automatic_cleanup_drops_orphan_recall_events(tmp_path, recall_store) -> None:
    """会话被自动清理之后，它派生出来的联想记录不能留在库里。"""
    repository = ConversationRepository(tmp_path / "core.db", auto_archive_days=1, archive_retention_days=1)
    scope = _scope()
    repository.insert_message(scope, {"role": "user", "content": "一个会过期的会话"})
    recall_store.record(scope, visit_id="visit-aaaaaaaa", greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW)

    repository.cleanup(now=NOW + timedelta(days=60))    # 归档
    repository.cleanup(now=NOW + timedelta(days=120))   # 到期删除

    assert repository.get(scope) is None
    assert _event_rows(recall_store) == []


def test_archiving_a_conversation_keeps_its_recall_events(conversation_repository, recall_store) -> None:
    """归档只是"收起来"，会话还能恢复 —— 记录跟着删掉会让恢复后的开场白突然失忆。"""
    scope = _scope()
    conversation_repository.insert_message(scope, {"role": "user", "content": "先归档的对话"})
    recall_store.record(scope, visit_id="visit-aaaaaaaa", greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW)

    conversation_repository.cleanup(now=NOW + timedelta(days=60))
    conversation_repository.archive(scope)

    assert _event_rows(recall_store) == [("message:1", "查明天的天气")]


def test_the_same_visit_accepts_an_identical_decision_and_rejects_a_different_one(recall_store) -> None:
    """同一次进入被重复请求时，模型可能给出不同决策：台账不能悄悄存旧的那份。"""
    scope = _scope()

    assert recall_store.record(scope, visit_id=VISIT, greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW) is True
    # 同样的决策，大纲只差标点 —— 算一致
    assert recall_store.record(scope, visit_id=VISIT, greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key=" 查明天的天气。 ", now=NOW) is True
    # 不同的决策 —— 失败，并且不改写已有记录
    assert recall_store.record(scope, visit_id=VISIT, greeting_kind=GREETING_TOPIC, cue_id="message:2", cue_key="别的什么事", now=NOW) is False
    assert recall_store.record(scope, visit_id=VISIT, greeting_kind=GREETING_PLAIN, now=NOW) is False

    assert _event_rows(recall_store) == [("message:1", "查明天的天气")]


def test_a_second_visit_can_still_record_its_own_decision(recall_store) -> None:
    scope = _scope()

    assert recall_store.record(scope, visit_id="visit-aaaaaaaa", greeting_kind=GREETING_TOPIC, cue_id="message:1", cue_key="查明天的天气", now=NOW) is True
    assert recall_store.record(scope, visit_id="visit-bbbbbbbb", greeting_kind=GREETING_TOPIC, cue_id="message:2", cue_key="别的什么事", now=NOW) is True

    assert len(_event_rows(recall_store)) == 2


@pytest.mark.asyncio
async def test_the_same_topic_under_a_new_message_id_is_blocked_by_its_key(repositories, recall_store) -> None:
    """换一条消息、同样的规范化大纲 —— 这就是"同一件事换种说法再说"，必须拦。"""
    scope = _scope()
    _recall(recall_store, scope, days_ago=3, cue_id="message:99", cue_key="我下周三去拔牙")
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    seeded = len(_event_rows(recall_store))
    # message:11 与 3 天前那次提过的事是同一句话，message:12 是另一件新事
    provider = ScriptedProvider(cue_id="message:12")

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[
            _message(11, "我下周三去拔牙", days_ago=1),
            _message(12, "我妈这周末过来住", days_ago=1),
        ],
        now=NOW,
    )

    assert "message:11" not in provider.prompt_text, "同一件事换个 id 也进不了候选"
    assert "message:12" in provider.prompt_text_of("selector"), "另一件事照常可以提"
    assert _event_rows(recall_store)[seeded:] == [("message:12", normalize_cue_key("我妈这周末过来住"))]
    assert greeting != DEFAULT_GREETING


@pytest.mark.asyncio
async def test_every_cooling_key_in_the_window_blocks_not_just_the_latest_few(repositories, recall_store) -> None:
    """判重覆盖冷却窗口内的**全部**大纲，不管它排第几。"""
    scope = _scope()
    # 六件提过的事：事情A 最旧、事情F 最新
    for index, label in enumerate(["事情A", "事情B", "事情C", "事情D", "事情E", "事情F"]):
        _recall(recall_store, scope, days_ago=6 - index, cue_id=f"message:{100 + index}", cue_key=label)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    provider = ScriptedProvider(cue_id="message:12")

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[
            _message(11, "事情A", days_ago=1),
            _message(12, "我下周三去拔牙", days_ago=1),
        ],
        now=NOW,
    )

    assert "事情A" not in provider.prompt_text, "最旧的那件也在冷却，不下发"
    assert "message:11" not in provider.prompt_text
    assert "message:12" in provider.prompt_text_of("selector")
    assert greeting != DEFAULT_GREETING


@pytest.mark.asyncio
async def test_expired_facts_do_not_crowd_out_an_older_valid_one(repositories, recall_store) -> None:
    """几条刚到期的、较新的记录不该把一条较旧但有效的事实挤出 limit 名额。"""
    scope = _scope()
    valid = _save_fact(repositories, scope, "用户对花生过敏")
    stale = _save_fact(repositories, scope, "用户这个月在出差")
    with repositories.connect() as db:
        db.execute("UPDATE memory_records SET created_at=? WHERE id=?", (_iso(_at(days_ago=5)), valid["id"]))
        db.execute("UPDATE memory_records SET created_at=? WHERE id=?", (_iso(_at(days_ago=1)), stale["id"]))
        db.execute("UPDATE memory_records SET expires_at=? WHERE id=?", (_iso(_at(days_ago=1)), stale["id"]))

    facts = repositories.confirmed_shared_facts(scope, limit=1, now=NOW)

    assert [row["content"] for row in facts] == ["用户对花生过敏"]


def test_a_fact_with_an_unparseable_expiry_is_kept(repositories) -> None:
    """异构格式（老数据）无法可靠比较，按"未过期"保留 —— 这是既有的兼容规则。"""
    scope = _scope()
    record = _save_fact(repositories, scope, "用户对花生过敏")
    with repositories.connect() as db:
        db.execute("UPDATE memory_records SET expires_at='明年再说' WHERE id=?", (record["id"],))

    facts = repositories.confirmed_shared_facts(scope, limit=5, now=NOW)

    assert [row["content"] for row in facts] == ["用户对花生过敏"]


@pytest.mark.asyncio
async def test_a_cached_fallback_is_dropped_so_the_next_attempt_retries() -> None:
    provider = ScriptedProvider(error=RuntimeError("模型挂了"))
    service = _service(provider)
    cache = OpeningGreetingCache()

    first = await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))
    second = await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))

    assert first == second == DEFAULT_GREETING
    assert len(provider.calls) == 2, "兜底没被缓存，第二次照样重新调模型"
    assert len(cache) == 0


@pytest.mark.asyncio
async def test_concurrent_fallbacks_still_share_one_model_call() -> None:
    """"兜底不缓存"不能牺牲"并发只调一次"。"""
    provider = ScriptedProvider(error=RuntimeError("模型挂了"), delay=0.05)
    service = _service(provider)
    cache = OpeningGreetingCache()

    results = await asyncio.gather(*[
        cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))
        for _ in range(4)
    ])

    assert set(results) == {DEFAULT_GREETING}
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_successful_greeting_is_still_cached() -> None:
    """只有兜底不留缓存：正常问候该省的那次调用还是要省。"""
    provider = ScriptedProvider()
    service = _service(provider)
    cache = OpeningGreetingCache()

    await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))
    await cache.run("acct|web|conv|visit-1", lambda: service.generate(_scope(), visit_id="visit-1"))

    assert len(provider.calls) == 1
    assert len(cache) == 1


# ── 接口层的构造路径（回归：这个 bug 单元测试抓不到） ────────────────────
def test_resolving_the_greeting_service_walks_the_real_construction_path(monkeypatch) -> None:
    """真实构造路径必须能跑通 —— 这里曾经引用了一个**不存在**的模块级名字。

    ``_resolve_opening_greeting_service`` 原来写的是 ``OpeningGreetingService(provider, ...)``，
    而 ``provider`` 只是 ``_initialize_runtime`` 的**局部变量**（模块级叫
    ``worker_provider``）。后果是：每次请求都在构造服务之前抛 ``NameError`` →
    接口稳定 500 → 前端把非 2xx 一律换成兜底句 —— 用户看到的就是"开场白永远是
    那一句"，而所有日志里连一行都没有。

    它躲过了整套单元测试，因为那些用例一律把 ``_opening_greeting_service``
    直接注入进去（monkeypatch），**从没走过真实构造**。这条用例只做一件事：
    让构造真的发生一次。
    """
    import web_api

    marker = object()
    monkeypatch.setattr(web_api, "worker_provider", marker)
    monkeypatch.setattr(web_api, "conversation_repository", None)
    monkeypatch.setattr(web_api, "_repositories", None)
    monkeypatch.setattr(web_api, "_tool_registry", None)
    gateway = OpeningToolGateway()
    monkeypatch.setattr(web_api, "_opening_gateway", gateway)
    # 非 None 即可，避免真的去建台账表
    monkeypatch.setattr(web_api, "_opening_recall_store", object())
    monkeypatch.setattr(web_api, "_opening_greeting_service", None)

    service = web_api._resolve_opening_greeting_service()

    assert isinstance(service, OpeningGreetingService)
    assert service.provider is marker, "构造用的必须是模块级真实存在的那个 provider 引用"
    assert service.opening_gateway is gateway


# ── 选题与措辞分离（两段式） ────────────────────────────────────────────
SELECTOR_BUDGET = 48


def _cue_lines(prompt: str) -> list[str]:
    """提示词里实际下发的线索行（``- <cue_id> ｜ …`` 形状）。"""
    return [
        line for line in prompt.splitlines()
        if line.startswith("- message:") or line.startswith("- fact:")
    ]


@pytest.mark.parametrize(
    "raw",
    [
        '{"selected_cue_id":"message:11"}',
        '{"selected_cue_id": "message:11" }',
        '{"selected_cue_id":"message:11","reason":"随手多写的"}',
    ],
    ids=["bare", "spaced", "extra_field"],
)
def test_parse_cue_selection_accepts_a_whitelisted_id(raw: str) -> None:
    assert parse_cue_selection(raw, allowed_cue_ids=["message:11", "message:12"]) == CueSelection("message:11")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        '```json\n{"selected_cue_id":"message:11"}\n```',
        '好的，我的选择是 {"selected_cue_id":"message:11"}',
        "{}",
        '{"selected_cue_id":"message:999"}',
        '{"selected_cue_id":11}',
        '{"selected_cue_id":["message:11"]}',
        '{"selected_cue_id":true}',
        '{"selected_cue_id":"   "}',
        '{"used_cue_id":"message:11"}',
        "[1,2]",
    ],
)
def test_parse_cue_selection_fails_closed(raw: str) -> None:
    """认不出的一律当作"没选"。选题模型没有权力指定白名单外的东西。

    带围栏也算认不出：选题阶段要求的是一行裸 JSON，
    模型开始"贴代码块"就说明它没在按格式作答，这时宁可重来一次普通问候。
    """
    assert parse_cue_selection(raw, allowed_cue_ids=["message:11"]) is None


def test_parse_cue_selection_reads_explicit_null() -> None:
    assert parse_cue_selection('{"selected_cue_id":null}', allowed_cue_ids=["message:11"]) == CueSelection(None)
    assert parse_cue_selection('{"selected_cue_id":null}', allowed_cue_ids=[]) == CueSelection(None)


@pytest.mark.parametrize(
    "content",
    ["帮我查一下明天的天气", "查天气", "搜一下那篇论文", "放一首周杰伦的歌", "打开音乐",
     "把这个文件挪到下载目录", "截个图", "把音量调小一点"],
)
def test_is_one_off_command_covers_obvious_tool_commands(content: str) -> None:
    assert is_one_off_command(content) is True


@pytest.mark.parametrize(
    "content",
    ["我周五要考运筹学", "最近在读《万历十五年》", "我打算下周去看牙医", "我妈这周末过来住",
     "帮我订张车票", "上次说的那个项目结案了"],
)
def test_is_one_off_command_does_not_eat_real_topics(content: str) -> None:
    """窄到只挡"说完就过去"的工具命令，不能顺手把真实近况一起挡掉。"""
    assert is_one_off_command(content) is False


@pytest.mark.asyncio
async def test_hard_filters_run_before_the_selector_sees_anything(repositories, recall_store) -> None:
    scope = _scope()
    # 冷却中的线索、一次性工具命令、低信息应答、带 kind 的系统旁白、重复文本
    # —— 五类都不该进选题提示词。
    _recall(recall_store, scope, days_ago=3, kind=GREETING_TOPIC, cue_id="message:10", cue_key="我妈这周末过来住")
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    provider = ScriptedProvider(cue_id="message:20")
    history = [
        _message(10, "我妈这周末过来住", days_ago=3),
        _message(11, "帮我查一下明天的天气", days_ago=1),
        _message(12, "嗯嗯", days_ago=1),
        _message(14, "我周五要考运筹学", days_ago=1, kind="steward_progress"),
        _message(20, "我周五要考运筹学", days_ago=0.5),
    ]

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long", history=history, now=NOW,
    )

    selector_prompt = provider.prompt_text_of("selector")
    assert len(_cue_lines(selector_prompt)) == 1
    assert "message:20" in _cue_lines(selector_prompt)[0]
    assert "我周五要考运筹学" in _cue_lines(selector_prompt)[0]
    assert "message:10" not in selector_prompt, "冷却中的不提"
    assert "message:11" not in selector_prompt, "一次性工具命令不提"
    assert "message:12" not in selector_prompt, "低信息应答不提"
    assert "message:14" not in selector_prompt, "系统旁白不是用户说过的事"


@pytest.mark.asyncio
async def test_selector_is_short_budgeted_toolless_and_capped_at_six_cues(repositories, recall_store) -> None:
    scope = _scope()
    provider = ScriptedProvider()
    service = _service(provider, repositories=repositories, recall_store=recall_store, cue_limit=6)
    for record in ("用户养了一只叫团子的猫", "用户下个月要搬家", "用户偏好安静"):
        _save_fact(repositories, scope, record)
    history = [_message(index, f"第 {index} 个近况", days_ago=index * 0.01) for index in range(1, 9)]

    await service.generate(scope, visit_id=VISIT, absence_tier="long", history=history, now=NOW)

    call = next(item for item in provider.calls if item["stage"] == "selector")
    assert call["tools"] is None
    assert call["temperature"] == 0
    assert call["max_tokens"] == SELECTOR_BUDGET
    assert len(_cue_lines(provider.prompt_text_of("selector"))) <= 6


@pytest.mark.asyncio
async def test_narrator_sees_exactly_the_selected_cue(repositories, recall_store) -> None:
    scope = _scope()
    _recall(recall_store, scope, days_ago=1, cue_id="message:9", cue_key="准备周五的考试")
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    provider = ScriptedProvider(cue_id="message:12")

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[
            _message(11, "最近在读《万历十五年》", days_ago=2),
            _message(12, "我下周三去拔牙", days_ago=1),
        ],
        now=NOW,
    )

    narrator_prompt = provider.prompt_text_of("narrator")
    assert "我下周三去拔牙" in narrator_prompt
    assert "最近在读《万历十五年》" not in narrator_prompt
    assert "message:11" not in narrator_prompt
    # 冷却大纲是服务端用来筛候选的，不给模型看：给了等于教它换个说法再提一遍
    assert "准备周五的考试" not in narrator_prompt


@pytest.mark.asyncio
async def test_a_null_selection_narrates_without_any_cue_material(repositories, recall_store) -> None:
    scope = _scope()
    _recall(recall_store, scope, days_ago=1, cue_id="message:9", cue_key="准备周五的考试")
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    _recall(recall_store, scope, kind=GREETING_PLAIN, cue_id=None, cue_key=None)
    seeded = len(_event_rows(recall_store))
    provider = ScriptedProvider("回来啦？", selector_raw='{"selected_cue_id":null}')

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        scope, visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "回来啦？"
    narrator_prompt = provider.prompt_text_of("narrator")
    assert "我下周三去拔牙" not in narrator_prompt
    assert "准备周五的考试" not in narrator_prompt
    assert _event_rows(recall_store)[seeded:] == [(None, None)]


@pytest.mark.asyncio
async def test_a_malformed_selection_narrates_without_any_cue_material(repositories, recall_store) -> None:
    provider = ScriptedProvider("回来啦？", cue_id="message:11", selector_raw="我觉得可以聊聊别的")

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "回来啦？"
    assert "我下周三去拔牙" not in provider.prompt_text_of("narrator")


@pytest.mark.asyncio
async def test_a_slow_selection_falls_back_to_a_plain_greeting(repositories, recall_store) -> None:
    """选题超时 → 不再花第二次**选题**，但会换成一次不带旧事的措辞。见下方预算组。"""
    provider = ScriptedProvider("回来啦？", cue_id="message:11", delay=0.2)

    greeting = await _service(
        provider, repositories=repositories, recall_store=recall_store, selector_timeout_seconds=0.05
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert provider.stages == ["selector", "narrator"], "选题只问一次，接着走普通问候"
    assert greeting == "回来啦？"


@pytest.mark.asyncio
async def test_the_narrator_cannot_change_which_cue_was_used(repositories, recall_store) -> None:
    """措辞模型报回别的 id/大纲也不作数：台账记的是服务端选中的那一条。"""
    provider = ScriptedProvider(
        "拔牙这事儿定了吗？",
        cue_id="message:11",
        raw='{"greeting": "拔牙这事儿定了吗？", "used_cue_id": "message:999", "cue_key": "瞎编的一件事"}',
    )

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "拔牙这事儿定了吗？"
    assert _event_rows(recall_store) == [("message:11", normalize_cue_key("我下周三去拔牙"))]


@pytest.mark.asyncio
async def test_a_selected_cue_is_recorded_before_the_greeting_is_returned(repositories, recall_store) -> None:
    """台账写不进去就不能把这句个性化问候交出去 —— 否则下次判重会漏掉它。"""
    provider = ScriptedProvider("拔牙这事儿定了吗？", cue_id="message:11")

    greeting = await _service(provider, repositories=repositories, recall_store=BrokenStore()).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == DEFAULT_GREETING
    assert provider.stages == ["selector", "narrator"]


# ═══════════════ 总预算：用户侧最多等 8 秒 ═══════════════
# 后端预算 7.5 秒 = 前端 8 秒 - 0.5 秒网络回程。
#
# 为什么必须**从 generate() 进入时**开始算，而不是"选题 2.5 秒 + 措辞 5 秒"：
# 两次超时相加是 7.5 秒没错，但真实耗时还要加上素材读取与台账写入 ——
# 分开计时的话，模型各跑满自己的预算，总时间仍然会超。只有一个总预算，
# 才能保证"用户最多等多久"是个能兑现的承诺。


class _BlockingStore:
    """台账替身：读可选阻塞、写按 ``write_delay`` 阻塞（同步阻塞，模拟真的 SQLite 卡住）。"""

    def __init__(self, *, delay: float = 0.0, write_delay: float | None = None) -> None:
        self.delay = delay
        self.write_delay = delay if write_delay is None else write_delay
        self.recorded = 0

    def recent_events(self, scope, **kwargs):
        time.sleep(self.delay)
        return []

    def record(self, scope, **kwargs):
        time.sleep(self.write_delay)
        self.recorded += 1
        return True


class _BlockingRepository:
    """历史读取会阻塞的仓储替身。"""

    def __init__(self, *, delay: float = 0.0, messages: list[dict] | None = None) -> None:
        self.delay = delay
        self._messages = list(messages or [])

    def recent_user_messages(self, scope, **kwargs):
        time.sleep(self.delay)
        return list(self._messages)


@pytest.mark.asyncio
async def test_slow_history_read_returns_the_prepared_line_within_the_budget() -> None:
    provider = ScriptedProvider()
    service = _service(
        provider,
        conversation_repository=_BlockingRepository(delay=0.6),
        prepared_plain=prepared_greeting,
        total_timeout_seconds=0.1,
    )

    started = time.monotonic()
    greeting = await service.generate(_scope(), visit_id=VISIT, now=NOW)
    elapsed = time.monotonic() - started

    assert greeting == prepared_greeting(VISIT)
    assert elapsed < 0.4, f"读取对话卡住时仍等待了 {elapsed:.2f} 秒"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_slow_recall_read_returns_the_prepared_line_within_the_budget() -> None:
    provider = ScriptedProvider(cue_id="message:11")
    service = _service(
        provider,
        recall_store=_BlockingStore(delay=0.6, write_delay=0.0),
        prepared_plain=prepared_greeting,
        total_timeout_seconds=0.1,
    )

    started = time.monotonic()
    greeting = await service.generate(
        _scope(), visit_id=VISIT,
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )
    elapsed = time.monotonic() - started

    assert greeting == prepared_greeting(VISIT)
    assert elapsed < 0.4, f"读取联想记录卡住时仍等待了 {elapsed:.2f} 秒"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_abandoned_background_task_observes_a_late_error(caplog) -> None:
    future = asyncio.get_running_loop().create_future()

    with caplog.at_level(logging.DEBUG, logger="agent_core.opening_greeting"):
        _abandon(future)
        future.set_exception(RuntimeError("迟到的读取错误"))
        await asyncio.sleep(0)

    assert any("被放弃的后台任务抛出异常" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_many_stalled_opening_reads_do_not_fill_the_shared_thread_pool() -> None:
    class HeldRepository:
        def __init__(self) -> None:
            self.gate = threading.Event()
            self.lock = threading.Lock()
            self.started = 0

        def recent_user_messages(self, scope, **kwargs):
            with self.lock:
                self.started += 1
            self.gate.wait(timeout=2.0)
            return []

    repository = HeldRepository()
    service = _service(
        ScriptedProvider(),
        conversation_repository=repository,
        prepared_plain=prepared_greeting,
        total_timeout_seconds=0.1,
    )
    visits = [f"visit-{index:08d}" for index in range(6)]

    try:
        greetings = await asyncio.gather(*[
            service.generate(_scope(), visit_id=visit_id, now=NOW) for visit_id in visits
        ])
        assert greetings == [prepared_greeting(visit_id) for visit_id in visits]
        assert repository.started <= 4, "持续卡库时开场白不能占满共享工作线程"
    finally:
        repository.gate.set()


@pytest.mark.asyncio
async def test_many_stalled_plain_records_do_not_fill_the_shared_thread_pool() -> None:
    class HeldStore:
        def __init__(self) -> None:
            self.gate = threading.Event()
            self.lock = threading.Lock()
            self.started = 0

        def recent_events(self, scope, **kwargs):
            return []

        def record(self, scope, **kwargs):
            with self.lock:
                self.started += 1
            self.gate.wait(timeout=2.0)
            return True

    store = HeldStore()
    service = _service(
        ScriptedProvider(),
        recall_store=store,
        prepared_plain=prepared_greeting,
        total_timeout_seconds=0.1,
    )
    visits = [f"visit-{index:08d}" for index in range(6)]

    try:
        greetings = await asyncio.gather(*[
            service.generate(_scope(), visit_id=visit_id, now=NOW) for visit_id in visits
        ])
        assert greetings == [prepared_greeting(visit_id) for visit_id in visits]
        assert store.started <= 4, "持续卡库时普通问候的后台记账不能占满共享工作线程"
    finally:
        store.gate.set()


@pytest.mark.asyncio
async def test_prepared_plain_greeting_does_not_wait_for_a_slow_ledger(repositories) -> None:
    store = _BlockingStore(write_delay=1.0)
    service = _service(
        ScriptedProvider(), repositories=repositories, recall_store=store,
        prepared_plain=lambda visit_id: "哟，来啦。",
    )

    started = time.monotonic()
    greeting = await service.generate(_scope(), visit_id=VISIT, now=NOW)

    assert greeting == "哟，来啦。"
    assert time.monotonic() - started < 0.6


@pytest.mark.asyncio
async def test_the_total_budget_caps_selector_plus_narration(repositories, recall_store) -> None:
    """总预算掐得住：选题跑满 2.5 秒后，措辞只能用剩下的时间。"""
    provider = ScriptedProvider("回来啦？", cue_id="message:11", delay=10)

    started = time.monotonic()
    greeting = await _service(
        provider,
        repositories=repositories,
        recall_store=recall_store,
        total_timeout_seconds=0.4,
        selector_timeout_seconds=0.25,   # 选题最多 0.25 秒
        narration_timeout_seconds=10.0,  # 措辞"想"要 10 秒，但总预算只剩一点点
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )
    elapsed = time.monotonic() - started

    assert greeting == DEFAULT_GREETING
    assert elapsed < 2.0, f"总预算没兜住，实际耗时 {elapsed:.2f} 秒"
    assert provider.stages == ["selector"], "选题超时后不该再花第二次调用"


@pytest.mark.asyncio
async def test_the_selector_gets_at_most_its_own_share(repositories, recall_store) -> None:
    """选题最多用自己那一份，不侵占措辞的时间 —— 掐掉选题后措辞仍有预算可用。

    断言的是**两段各花多久**，不是总时长：总时长的上限由下面那组预算用例管。
    这里给措辞留 1 秒（高于最小措辞预算），如果选题把预算吃光，措辞就没机会被调用。
    """
    provider = ScriptedProvider("回来啦？", cue_id="message:11", delay=10)

    started = time.monotonic()
    greeting = await _service(
        provider,
        repositories=repositories,
        recall_store=recall_store,
        total_timeout_seconds=10.0,
        selector_timeout_seconds=0.3,
        narration_timeout_seconds=1.0,
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )
    elapsed = time.monotonic() - started

    assert provider.stages == ["selector", "narrator"], "选题花掉自己的份额后，措辞仍该有机会跑"
    assert greeting == DEFAULT_GREETING
    assert elapsed < 6.0, f"两段各自被自己的份额掐住，总耗时不该失控（实际 {elapsed:.2f} 秒）"


@pytest.mark.asyncio
async def test_a_null_selection_still_narrates_a_plain_greeting(repositories, recall_store) -> None:
    """选题明确回 null（这次不提旧事）→ 仍要让措辞模型说一句普通问候。"""
    provider = ScriptedProvider("回来啦？", selector_raw=json.dumps({"selected_cue_id": None}))

    greeting = await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "回来啦？"
    assert provider.stages == ["selector", "narrator"]
    assert "我下周三去拔牙" not in provider.prompt_text_of("narrator"), "普通问候不该带任何旧事"


@pytest.mark.asyncio
async def test_a_slow_selection_still_gets_one_plain_greeting(repositories, recall_store) -> None:
    """选题超时但**总预算还有**→ 仍要调一次措辞，说一句不带旧事的普通问候。

    原来这里直接兜底，代价是"模型慢一次"= "用户永远收到同一句话"：
    抓不到旧事不该顺带把问候也放弃掉。
    """
    provider = ScriptedProvider("回来啦？", cue_id="message:11", delay=0.2)

    greeting = await _service(
        provider,
        repositories=repositories,
        recall_store=recall_store,
        total_timeout_seconds=10.0,
        selector_timeout_seconds=0.05,   # 掐掉选题
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "回来啦？", "选题失败不该连普通问候一起放弃"
    assert provider.stages == ["selector", "narrator"]
    assert "我下周三去拔牙" not in provider.prompt_text_of("narrator")


@pytest.mark.asyncio
async def test_an_exhausted_budget_returns_the_fixed_line(repositories, recall_store) -> None:
    """总预算真的耗尽时，还是那句固定兜底。"""
    provider = ScriptedProvider("回来啦？", cue_id="message:11", delay=10)

    greeting = await _service(
        provider,
        repositories=repositories,
        recall_store=recall_store,
        total_timeout_seconds=0.15,
        selector_timeout_seconds=0.05,
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == DEFAULT_GREETING


@pytest.mark.asyncio
async def test_a_slow_recall_write_drops_the_personalised_line(repositories) -> None:
    """台账写入没在预算内确认 → 个性化那句不能交出去（宁可退回固定问候）。

    "先记账后展示"是硬约束：记录没落库就把它讲出来，下一次判重会漏掉这次，
    同一件事会被反复提起 —— 那正是这套闸门要消灭的体验。
    """
    provider = ScriptedProvider("拔牙这事儿定了吗？", cue_id="message:11")
    slow_store = _BlockingStore(write_delay=5.0)

    greeting = await _service(
        provider,
        repositories=repositories,
        recall_store=slow_store,
        total_timeout_seconds=2.0,
        selector_timeout_seconds=0.1,
        narration_timeout_seconds=0.5,
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == DEFAULT_GREETING, "台账没确认写入就不能把那句个性化问候交出去"
    assert provider.stages == ["selector", "narrator"], "模型确实说了一句好的，只是台账没跟上"


@pytest.mark.asyncio
async def test_a_plain_greeting_survives_a_slow_recall_write(repositories) -> None:
    """普通问候不依赖记录：写入慢照样返回，否则库抖一下用户就一直听同一句。"""
    provider = ScriptedProvider("回来啦？", selector_raw=json.dumps({"selected_cue_id": None}))
    slow_store = _BlockingStore(write_delay=5.0)

    greeting = await _service(
        provider,
        repositories=repositories,
        recall_store=slow_store,
        total_timeout_seconds=2.0,
        selector_timeout_seconds=0.1,
        narration_timeout_seconds=0.5,
    ).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
    )

    assert greeting == "回来啦？"


@pytest.mark.asyncio
async def test_the_narrator_only_ever_sees_one_cue(repositories, recall_store) -> None:
    """措辞模型最多看一条线索；普通问候则一条都不给（总预算改动不许松开这条）。"""
    provider = ScriptedProvider("拔牙这事儿定了吗？", cue_id="message:11")

    await _service(provider, repositories=repositories, recall_store=recall_store).generate(
        _scope(), visit_id=VISIT, absence_tier="long",
        history=[
            _message(11, "我下周三去拔牙", days_ago=1),
            _message(12, "我妈这周末过来住", days_ago=2),
        ], now=NOW,
    )

    narrator_prompt = provider.prompt_text_of("narrator")
    assert "我下周三去拔牙" in narrator_prompt
    assert "我妈这周末过来住" not in narrator_prompt, "措辞只能看见被选中那一条"


@pytest.mark.asyncio
async def test_the_plain_greeting_logs_do_not_contain_user_text(repositories, recall_store, caplog) -> None:
    """日志只记阶段与兜底原因，不记用户原话、候选内容或模型原始输出。"""
    provider = ScriptedProvider("回来啦？", selector_raw=json.dumps({"selected_cue_id": None}))

    with caplog.at_level(logging.INFO, logger="agent_core.opening_greeting"):
        await _service(provider, repositories=repositories, recall_store=recall_store).generate(
            _scope(), visit_id=VISIT, absence_tier="long",
            history=[_message(11, "我下周三去拔牙", days_ago=1)], now=NOW,
        )

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "我下周三去拔牙" not in text, "日志不能记用户原话"
    assert "回来啦" not in text, "日志不能记模型原始输出"
