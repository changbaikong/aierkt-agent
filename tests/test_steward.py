"""管家式过程表达（agent_core/steward.py + loop 接入 + WorkResultBridge）。

这一组用例对应验收清单里"说话方式"那一部分，核心是三件事：
  1. **该说的时候说**：接手 / 拿到证据且还有下一步 / 改道 / 待审批 / 完成 / 失败 / 中断；
  2. **不该说的时候闭嘴**：普通工具完成、单步快速任务、重复阶段；
  3. **什么都不能漏出去**：工具名、Worker/Provider/Host、JSON、异常栈、思考链。
"""
import asyncio
import json

import pytest

from agent_core.contracts import (
    AgentRequest,
    EFFECT_READ,
    ProviderResponse,
    Scope,
    ToolCall,
    ToolResult,
    ToolSpec,
)
from agent_core.connection_pool import close_all_pools
from agent_core.events import EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.steward import (
    PHASE_ACCEPTED,
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_CONFLICT,
    PHASE_EVIDENCE_FOUND,
    PHASE_FAILED,
    PHASE_INTERRUPTED,
    PHASE_MATERIALS,
    PHASE_READ_DONE,
    PHASE_READING,
    PHASE_REDIRECT,
    PHASE_SEARCHING,
    PHASE_WAITING_APPROVAL,
    STEWARD_PROGRESS_KIND,
    ChatStagePublisher,
    RecentPhraseMemory,
    StagePhraseGenerator,
    StewardContext,
    StewardNarrator,
    StewardReporter,
    normalize_steward_line,
    phrase_too_similar,
    steward_line_issue,
    template_line,
)
from agent_core.steward_progress import StageReport
from agent_core.tools import ToolRegistry
from agent_core.work_bridge import WorkResultBridge, render_work_reply, safe_reason_text
from agent_core import fastpath as fast
from support.stage_phrases import StagePhraseProvider, stage_line


def _dynamic_stage_reply(messages) -> str:
    """假模型的阶段话术：按提示词里的"当前阶段"回一句对应的测试话术。"""
    stage = StagePhraseProvider.stage_of(messages)
    return stage_line(stage) if stage else "我先看看情况。"


class ScriptedProvider(LLMProvider):
    """按脚本返回回复的假模型。

    主回复与"管家话术"走同一个 provider，所以用 ``steward_reply`` 单独指定
    话术调用该返回什么（``None`` 表示让主回复顶上，用来测超长/不合格回退）。
    """

    def __init__(self, responses, *, steward_reply=None, steward_error=None, steward_delay=0.0):
        self.responses = list(responses)
        self.steward_reply = steward_reply
        self.steward_error = steward_error
        self.steward_delay = steward_delay
        self.calls: list[dict] = []
        self.steward_calls: list[list] = []
        self.index = 0

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools, "max_tokens": max_tokens})
        is_steward = bool(messages) and "报一句进度" in str(messages[0].get("content") or "")
        if is_steward:
            self.steward_calls.append(messages)
            if self.steward_delay:
                await asyncio.sleep(self.steward_delay)
            if self.steward_error is not None:
                raise self.steward_error
            reply = self.steward_reply(messages) if callable(self.steward_reply) else self.steward_reply
            if reply is None and "最近已经说过的话" in str(messages[0].get("content") or ""):
                # 动态措辞调用：缺省按当前阶段回一句测试话术，回合用例才有气泡可断言。
                reply = _dynamic_stage_reply(messages)
            return ProviderResponse(
                message={"role": "assistant", "content": reply or ""},
                usage={"prompt_tokens": 5, "completion_tokens": 5},
            )
        if self.index >= len(self.responses):
            raise RuntimeError("no more scripted responses")
        response = self.responses[self.index]
        self.index += 1
        return ProviderResponse(message=response, usage={"prompt_tokens": 10, "completion_tokens": 20})


class RecordingSink:
    def __init__(self):
        self.messages: list[dict] = []
        self.keys: list[str] = []
        self.reject_next = False

    def __call__(self, scope, message, stable_key):
        self.keys.append(stable_key)
        if self.reject_next:
            return False
        self.messages.append({"scope": scope, **message})
        return True


def _registry(*, risk="low", name="search__web"):
    async def handler(call: ToolCall, scope: Scope) -> ToolResult:
        return ToolResult(status="ok", output={"title": "论文标题 A", "total": 3})

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name=name,
        schema={"name": name, "description": "search", "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk=risk,
    ))
    return registry


def _tool_call(name="search__web", call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def _location_registry():
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="location__current",
        schema={"name": "location__current", "description": "location", "input_schema": {"type": "object"}},
        handler=lambda _arguments, _scope: {
            "status": "ok",
            "output": {
                "latitude": 31.2304,
                "longitude": 121.4737,
                "accuracy_m": 20.0,
                "source": "browser",
                "age_seconds": 0.58,
            },
        },
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    ))
    return registry


# ----------------------------------------------------------------------
# 1. 触发时机
# ----------------------------------------------------------------------
class _StageClock:
    """可控时钟：让"1.5 秒 / 4 秒"这类体验参数在回合测试里也能立刻验证。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)
        self._sleepers: list[list] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        entry = [self.now + float(delay), asyncio.get_running_loop().create_future()]
        self._sleepers.append(entry)
        try:
            await entry[1]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)

    async def advance(self, delta: float) -> None:
        """走过一段时间；等待中的进度计时器会在这里被唤醒。"""
        self.now += float(delta)
        for entry in list(self._sleepers):
            if entry[0] <= self.now + 1e-9:
                self._sleepers.remove(entry)
                if not entry[1].done():
                    entry[1].set_result(None)
        for _ in range(12):
            await asyncio.sleep(0)


def _timed_registry(clock: _StageClock, *, seconds: float, result=None, name="search__web"):
    """一个"要花时间"的搜索工具：让等待门槛在测试里真的能跨过去。

    结果形状照真实搜索工具来：材料放在 ``results`` 列表里。
    """
    payload = {
        "query": "论文",
        "result_count": 1,
        "results": [{"title": "论文标题 A", "url": "https://example.com/a", "snippet": "片段"}],
    } if result is None else result

    async def handler(call: ToolCall, scope: Scope) -> ToolResult:
        await clock.advance(seconds)
        return ToolResult(status="ok", output=payload)

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name=name,
        schema={"name": name, "description": "search", "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk="low",
    ))
    return registry


@pytest.mark.asyncio
async def test_fast_turn_produces_no_progress_bubble(tmp_path):
    """1.4 秒内就结束的查询：直接给最终结果，一条过程气泡都不发。"""
    clock = _StageClock()
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
        {"role": "assistant", "content": "看完了。"},
    ])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        _timed_registry(clock, seconds=1.4),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    await loop.run(AgentRequest("你去看一下这篇论文", scope=loop.scope))

    assert sink.messages == []
    assert provider.index == 2


@pytest.mark.asyncio
async def test_slow_turn_speaks_the_real_stage_without_tool_names(tmp_path):
    """等待超过 1.5 秒：动态生成一句"正在查"，不泄露工具名、不照抄问题。"""
    clock = _StageClock()
    provider = ScriptedProvider(
        [
            {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
            {"role": "assistant", "content": "看完了。"},
        ],
    )
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        _timed_registry(clock, seconds=2.0),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(
            StewardNarrator(provider, persona="爱尔奎特", forbidden_terms={"search__web"}),
            sink=sink,
        ),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    await loop.run(AgentRequest("你去看一下这篇论文", scope=loop.scope))

    assert len(sink.messages) == 1
    message = sink.messages[0]
    assert message["phase"] == PHASE_SEARCHING
    assert message["content"] == stage_line(PHASE_SEARCHING)
    assert "search__web" not in message["content"]   # 不泄露工具名
    assert "论文" not in message["content"]            # 不照抄用户的问题
    assert message["kind"] == STEWARD_PROGRESS_KIND
    # 措辞是模型现场生成的：这一条恰好花了一次措辞调用
    assert len(provider.steward_calls) == 1
    assert provider.index == 2


@pytest.mark.asyncio
async def test_materials_notice_only_when_another_step_follows(tmp_path):
    """确实带回材料、而且后面还有事 → 说"取得材料"；不许把标题当成已证实的答案。"""
    clock = _StageClock()
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call(call_id="call-2")]},
        {"role": "assistant", "content": "结论是……"},
    ])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        _timed_registry(clock, seconds=5.0),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(
            StewardNarrator(provider, forbidden_terms={"search__web"}), sink=sink
        ),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    await loop.run(AgentRequest("研究一下这篇论文", scope=loop.scope))

    assert [item["phase"] for item in sink.messages] == [PHASE_SEARCHING, PHASE_MATERIALS]
    materials = sink.messages[-1]["content"]
    assert materials == stage_line(PHASE_MATERIALS)
    assert "论文标题 A" not in materials     # 标题不是已经证实的答案
    assert "找到" not in materials            # 拿到材料 ≠ 确认结论
    # 两句各自花了一次措辞调用：措辞不再是本地固定句
    assert len(provider.steward_calls) == 2


@pytest.mark.asyncio
async def test_search_with_material_but_direct_answer_has_no_middle_message(tmp_path):
    """搜索结果已经够用、模型直接回答 → 不补"取得材料"的中段。"""
    clock = _StageClock()
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
        {"role": "assistant", "content": "直接回答。"},
    ])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        _timed_registry(clock, seconds=2.0),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    await loop.run(AgentRequest("研究一下这篇论文", scope=loop.scope))

    assert [item["phase"] for item in sink.messages] == [PHASE_SEARCHING]


@pytest.mark.asyncio
async def test_empty_search_and_another_step_speaks_redirect(tmp_path):
    """上一轮空手而归、模型还要继续 → 说"换个来源"，而不是"取得材料"。"""
    clock = _StageClock()

    async def failing(call: ToolCall, scope: Scope) -> ToolResult:
        await clock.advance(5.0)
        return ToolResult(status="failed", error="查不到")

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="search__web",
        schema={"name": "search__web", "description": "s", "input_schema": {"type": "object", "properties": {}}},
        handler=failing,
        risk="low",
    ))
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call(call_id="call-2")]},
        {"role": "assistant", "content": "没能查到。"},
    ])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(
            StewardNarrator(provider, forbidden_terms={"search__web"}), sink=sink
        ),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )
    await loop.run(AgentRequest("查一下", scope=loop.scope))

    phases = [item["phase"] for item in sink.messages]
    assert PHASE_REDIRECT in phases
    assert PHASE_MATERIALS not in phases
    redirect = next(item for item in sink.messages if item["phase"] == PHASE_REDIRECT)
    assert redirect["content"] == stage_line(PHASE_REDIRECT)
    assert "找到" not in redirect["content"]


@pytest.mark.asyncio
async def test_single_step_turn_produces_no_middle_message(tmp_path):
    """单步快速任务（模型一轮就答完）不产生任何过程消息。"""
    provider = ScriptedProvider([{"role": "assistant", "content": "今天天气不错。"}])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        _registry(),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
    )
    await loop.run(AgentRequest("在吗", scope=loop.scope))
    assert sink.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["我在哪里", "我的位置", "我现在在哪", "我现在在什么位置呢"])
async def test_location_questions_reply_directly_in_plain_language_without_progress(question, tmp_path):
    """位置快查直接说人话：不调用模型，也不为了一个瞬时结果多冒一条过程气泡。"""
    provider = ScriptedProvider([])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        _location_registry(),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
    )

    outcome = await loop.run(AgentRequest(question, scope=loop.scope))

    assert provider.calls == []
    assert sink.messages == []
    # 粗粒度位置 + 区间精度：坐标本身**不出现在气泡里**。
    assert "上海附近" in outcome.reply
    assert "大约 20 米" in outcome.reply
    assert "31.2304" not in outcome.reply
    assert "121.4737" not in outcome.reply
    assert "latitude" not in outcome.reply
    assert "age_seconds" not in outcome.reply
    assert "{" not in outcome.reply


@pytest.mark.asyncio
async def test_unavailable_location_is_explained_without_internal_error(tmp_path):
    provider = ScriptedProvider([])
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="location__current",
        schema={"name": "location__current", "description": "location", "input_schema": {"type": "object"}},
        handler=lambda _arguments, _scope: {
            "status": "unavailable",
            "output": {"reason": "location_unavailable"},
            "error": "No recent browser location is available.",
        },
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    ))
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
    )

    outcome = await loop.run(AgentRequest("我现在在哪", scope=loop.scope))

    assert provider.calls == []
    assert outcome.reply == "这会儿我还读不到你的位置。你那边是不是还没开位置共享？"
    assert "No recent" not in outcome.reply
    assert "location_unavailable" not in outcome.reply


@pytest.mark.asyncio
async def test_repeated_phase_writes_only_one_message(tmp_path):
    """同一个逻辑节点（本回合的"取得材料"）只写一条，不管后面又查了几次。"""
    clock = _StageClock()
    provider = ScriptedProvider(
        [
            {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call(call_id="call-2")]},
            {"role": "assistant", "content": "", "tool_calls": [_tool_call(call_id="call-3")]},
            {"role": "assistant", "content": "好了。"},
        ],
        steward_reply="嗯，交给我吧。我先去看看。",
    )
    sink = RecordingSink()
    reporter = StewardReporter(StewardNarrator(provider), sink=sink)
    loop = AgentLoop(
        provider,
        _timed_registry(clock, seconds=2.0),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=reporter,
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )
    await loop.run(AgentRequest("查点东西", scope=loop.scope))

    phases = [item["phase"] for item in sink.messages]
    assert phases.count(PHASE_MATERIALS) <= 1
    assert phases.count(PHASE_SEARCHING) == 1
    # 直接再发一次同阶段：sink 一次都不会收到
    before = len(sink.messages)
    assert await reporter.publish(
        loop.scope,
        StewardContext(phase=PHASE_SEARCHING),
        run_id=loop.run_id,
        step_id="first_query",
    ) == ""
    assert len(sink.messages) == before


@pytest.mark.asyncio
async def test_progress_messages_do_not_cross_conversations(tmp_path):
    """不同 Scope 的过程消息不会串到别的对话。"""
    provider = ScriptedProvider([{"role": "assistant", "content": "ok"}], steward_reply="我先去看看。")
    seen: dict[str, list] = {}

    def sink(scope, message, stable_key):
        seen.setdefault(scope.conversation_id, []).append(message["content"])
        return True

    reporter = StewardReporter(StewardNarrator(provider), sink=sink)
    await reporter.publish(Scope("alice", "web", "chat-a", "companion"),
                           StewardContext(phase=PHASE_ACCEPTED), run_id="r1")
    await reporter.publish(Scope("alice", "web", "chat-b", "companion"),
                           StewardContext(phase=PHASE_ACCEPTED), run_id="r1")
    assert list(seen) == ["chat-a", "chat-b"]
    assert len(seen["chat-a"]) == 1 and len(seen["chat-b"]) == 1


# ----------------------------------------------------------------------
# 2. 气泡洁净度
# ----------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    '思考过程：先找论文，再读摘要。',
    '{"status":"ok","output":{"title":"x"}}',
    'role: tool 返回了结果',
    'Traceback (most recent call last): File "x.py"',
    '交给我吧，我会调用 search__web 去查一下。',
    'Worker 正在处理，Provider 返回了结果。',
])
def test_internal_text_never_reaches_the_bubble(raw):
    """原始 reasoning、工具协议、JSON、异常栈、工具名、内部角色都进不了气泡。"""
    assert steward_line_issue(raw, forbidden_terms={"search__web"}) is not None


def test_no_claim_without_evidence():
    """没拿到结果前不许说"找到了"。"""
    assert steward_line_issue("找到了，论文在这里。", phase=PHASE_ACCEPTED) == "premature"
    assert steward_line_issue("找到了，论文在这里。", phase=PHASE_EVIDENCE_FOUND, has_evidence=True) is None


def test_overlong_line_is_rejected():
    assert steward_line_issue("我去查一下" * 20) == "long"


def test_normalize_keeps_two_sentences_and_drops_fences():
    raw = '```json\n找到了。它主要在讨论 A。我还要再看参考文献。\n```'
    assert normalize_steward_line(raw) == "找到了。它主要在讨论 A。"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", '{"a":1}', "我调用了 search__web 这个工具去查。"])
async def test_falls_back_to_template_when_model_misbehaves(bad):
    """模型返回不合格文本（或超时）时使用角色化模板。"""
    provider = ScriptedProvider([], steward_reply=bad)
    narrator = StewardNarrator(provider, persona="爱尔奎特", forbidden_terms={"search__web"})
    line = await narrator.narrate(StewardContext(phase=PHASE_ACCEPTED, goal="看这篇论文"))
    assert line == template_line(StewardContext(phase=PHASE_ACCEPTED, goal="看这篇论文"))
    assert "search__web" not in line


@pytest.mark.asyncio
async def test_timeout_falls_back_to_template():
    provider = ScriptedProvider([], steward_reply="我来慢慢想。", steward_delay=1.0)
    narrator = StewardNarrator(provider, timeout_seconds=0.05)
    line = await narrator.narrate(StewardContext(phase=PHASE_ACCEPTED, goal="看论文"))
    assert line == template_line(StewardContext(phase=PHASE_ACCEPTED, goal="看论文"))


@pytest.mark.asyncio
async def test_provider_error_falls_back_to_template():
    provider = ScriptedProvider([], steward_error=RuntimeError("boom"))
    narrator = StewardNarrator(provider)
    line = await narrator.narrate(StewardContext(phase=PHASE_FAILED))
    assert "没能" in line and "boom" not in line


def test_templates_differ_per_phase():
    lines = {
        phase: template_line(StewardContext(phase=phase, goal="看论文", evidence=("共 3 项",)))
        for phase in (PHASE_ACCEPTED, PHASE_EVIDENCE_FOUND, PHASE_REDIRECT,
                      PHASE_WAITING_APPROVAL, PHASE_COMPLETED, PHASE_FAILED,
                      PHASE_CANCELLED, PHASE_INTERRUPTED)
    }
    assert len(set(lines.values())) == len(lines)
    assert lines[PHASE_EVIDENCE_FOUND].startswith("找到了")
    assert "确认" in lines[PHASE_WAITING_APPROVAL] or "看一眼" in lines[PHASE_WAITING_APPROVAL]
    assert "Traceback" not in lines[PHASE_FAILED]


# ----------------------------------------------------------------------
# 3. 等待审批 / 失败 / 中断
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_high_risk_tool_pause_speaks_waiting_approval(tmp_path):
    provider = ScriptedProvider(
        [{"role": "assistant", "content": "", "tool_calls": [_tool_call(name="file__organize")]}],
        steward_reply="我先停一下。真正动手之前，想让你先看一眼。",
    )
    sink = RecordingSink()
    registry = _registry(name="file__organize", risk="high")
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(
            StewardNarrator(provider, forbidden_terms={"file__organize"}), sink=sink
        ),
    )
    outcome = await loop.run(AgentRequest("帮我整理文件", scope=loop.scope))
    assert outcome.status == "paused"
    assert sink.messages[-1]["phase"] == PHASE_WAITING_APPROVAL
    assert "file__organize" not in sink.messages[-1]["content"]


@pytest.mark.asyncio
async def test_failure_and_interruption_have_their_own_voice(tmp_path):
    class BrokenProvider(LLMProvider):
        async def complete(self, messages, **kwargs):
            if "报一句进度" in str(messages[0].get("content") or ""):
                return ProviderResponse(message={"role": "assistant", "content": "这次没能把内容完整取回来。"},
                                        usage={"completion_tokens": 5})
            raise RuntimeError("provider is down")

    sink = RecordingSink()
    loop = AgentLoop(
        BrokenProvider(),
        _registry(),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(BrokenProvider()), sink=sink),
    )
    outcome = await loop.run(AgentRequest("查一下", scope=loop.scope))
    assert outcome.status == "failed"
    assert sink.messages[-1]["phase"] == PHASE_FAILED
    assert "RuntimeError" not in sink.messages[-1]["content"]
    assert "provider is down" not in sink.messages[-1]["content"]


# ----------------------------------------------------------------------
# 4. 事件
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_steward_progress_event_carries_phase_and_content():
    provider = ScriptedProvider([], steward_reply="嗯，交给我吧。我先把论文找出来。")
    bus = InMemoryEventBus()
    events = []
    bus.subscribe(EventType.STEWARD_PROGRESS, lambda event: events.append(event))
    reporter = StewardReporter(StewardNarrator(provider), sink=RecordingSink(), event_bus=bus)
    await reporter.publish(
        Scope("alice", "web", "chat-1", "companion"),
        StewardContext(phase=PHASE_ACCEPTED, goal="看论文"),
        run_id="run-9",
        step_id="step-0",
        turn_id="turn-1",
    )
    assert len(events) == 1
    assert events[0].data["phase"] == PHASE_ACCEPTED
    assert events[0].data["content"].startswith("嗯，交给我吧")
    assert events[0].data["run_id"] == "run-9"
    assert events[0].scope["conversation_id"] == "chat-1"


@pytest.mark.asyncio
async def test_steward_dedupe_includes_channel_and_module_scope():
    provider = ScriptedProvider([], steward_reply="我先去看看。")
    sink = RecordingSink()
    reporter = StewardReporter(StewardNarrator(provider), sink=sink)
    context = StewardContext(phase=PHASE_ACCEPTED, goal="查资料")
    await reporter.publish(Scope("alice", "web", "same", "companion"), context, run_id="run-1")
    await reporter.publish(Scope("alice", "desktop", "same", "companion"), context, run_id="run-1")
    await reporter.publish(Scope("alice", "web", "same", "work"), context, run_id="run-1")
    assert len(sink.messages) == 3


@pytest.mark.asyncio
async def test_sink_rejection_suppresses_the_event():
    """存储层说"这条已经写过了" → 不再广播，前端不会多出气泡。"""
    provider = ScriptedProvider([], steward_reply="我先去看看。")
    bus = InMemoryEventBus()
    events = []
    bus.subscribe(EventType.STEWARD_PROGRESS, lambda event: events.append(event))
    sink = RecordingSink()
    sink.reject_next = True
    reporter = StewardReporter(StewardNarrator(provider), sink=sink, event_bus=bus)
    text = await reporter.publish(Scope("a", "web", "c", "companion"), StewardContext(phase=PHASE_ACCEPTED))
    assert text == ""
    assert events == []


# ----------------------------------------------------------------------
# 5. 后台任务回执（WorkResultBridge）
# ----------------------------------------------------------------------
class _FakeHost:
    def __init__(self, events):
        self._events = events

    async def events(self):
        for event in self._events:
            yield event


class _FakeRepository:
    def __init__(self):
        self.written: list[dict] = []
        self.touch: list[bool] = []

    def append_message(self, scope, message, *, stable_key=None, touch_activity=True):
        for existing in self.written:
            if existing["stable_key"] == stable_key:
                return False
        self.written.append({"scope": scope, "stable_key": stable_key, **message})
        self.touch.append(touch_activity)
        return True


@pytest.mark.asyncio
async def test_bridge_updates_running_conversation_history():
    from agent_core.conversation_manager import ConversationManager
    from types import SimpleNamespace

    manager = ConversationManager(lambda _scope: SimpleNamespace())
    scope = Scope("alice", "web", "chat-live", "companion")
    state = manager._states[scope] = SimpleNamespace(history=[], last_activity=0.0, worker=None)
    repository = _FakeRepository()
    bridge = WorkResultBridge(_FakeHost([]), repository, None, conversation_manager=manager)
    await bridge._handle({
        "type": "completed", "conversation_id": "chat-live", "owner_id": "alice", "channel": "web",
        "attempt_id": "att-live", "task_type": "file.list", "objective": "列文件", "payload": {"total": 1},
    })
    assert state.history and state.history[-1]["kind"] == "work_result"
    manager._states.clear()
    await manager.shutdown()


def test_evidence_template_does_not_claim_a_result_without_evidence():
    line = template_line(StewardContext(phase=PHASE_EVIDENCE_FOUND))
    assert "查到" not in line
    assert "找到" not in line


@pytest.mark.asyncio
async def test_bridge_completion_uses_the_same_voice():
    provider = ScriptedProvider([], steward_reply="我看完了。文件清单已经整理好了。")
    repository = _FakeRepository()
    bridge = WorkResultBridge(_FakeHost([]), repository, None, steward=StewardNarrator(provider))
    await bridge._handle({
        "type": "completed",
        "conversation_id": "chat-1",
        "owner_id": "alice",
        "channel": "web",
        "attempt_id": "att-1",
        "task_type": "file.list",
        "objective": "整理文件",
        "payload": {"total": 3},
    })
    assert repository.written[-1]["content"].startswith("我看完了")
    # 系统说的话不算用户活动
    assert repository.touch == [False]


@pytest.mark.asyncio
async def test_bridge_waiting_approval_asks_before_acting():
    provider = ScriptedProvider([], steward_reply="文件清单已经整理好了。真正移动之前，我想让你先看一眼。")
    repository = _FakeRepository()
    bridge = WorkResultBridge(_FakeHost([]), repository, None, steward=StewardNarrator(provider))
    await bridge._handle({
        "type": "waiting_approval",
        "conversation_id": "chat-1",
        "owner_id": "alice",
        "channel": "web",
        "attempt_id": "att-2",
        "task_type": "file.organize",
        "objective": "整理文件",
        "payload": {},
    })
    assert "看一眼" in repository.written[-1]["content"]


@pytest.mark.asyncio
async def test_bridge_failure_hides_the_stack_trace():
    """失败时不暴露异常栈。"""
    provider = ScriptedProvider([], steward_reply="这次没能把内容完整取回来。我换一种方式再查。")
    repository = _FakeRepository()
    bridge = WorkResultBridge(_FakeHost([]), repository, None, steward=StewardNarrator(provider))
    await bridge._handle({
        "type": "failed",
        "conversation_id": "chat-1",
        "owner_id": "alice",
        "channel": "web",
        "attempt_id": "att-3",
        "task_type": "file.list",
        "objective": "整理文件",
        "payload": {"error": 'Traceback (most recent call last):\n  File "x.py", line 1\nboom'},
    })
    content = repository.written[-1]["content"]
    assert "Traceback" not in content and "x.py" not in content
    assert safe_reason_text('Traceback ... File "x.py"') == ""
    # 确定性播报本身也不再带异常栈
    assert "Traceback" not in render_work_reply("failed", "整理", "file.list", {}, safe_reason_text("boom"))


@pytest.mark.asyncio
async def test_bridge_reports_each_attempt_once():
    repository = _FakeRepository()
    bridge = WorkResultBridge(_FakeHost([]), repository, None)
    event = {
        "type": "completed",
        "conversation_id": "chat-1",
        "owner_id": "alice",
        "channel": "web",
        "attempt_id": "att-4",
        "task_type": "file.list",
        "objective": "整理文件",
        "payload": {"total": 1},
    }
    await bridge._handle(event)
    await bridge._handle(event)
    assert len(repository.written) == 1


# ----------------------------------------------------------------------
# 6. 与长期记忆的边界
# ----------------------------------------------------------------------
def test_progress_kind_is_excluded_from_memory_extraction():
    from agent_core.memory_extract import NON_MEMORY_KINDS

    assert STEWARD_PROGRESS_KIND in NON_MEMORY_KINDS


@pytest.mark.asyncio
async def test_bridge_reports_plan_completion_into_the_originating_chat():
    """计划事件只带 plan_id：桥要回账本查出对话，用同一张嘴说结果。"""
    from agent_core.contracts import Scope as _Scope
    from agent_core.control_plan import ControlPlan

    plan = ControlPlan(
        plan_id="plan-1",
        scope=_Scope("alice", "web", "chat-1", "work"),
        objective="整理桌面并截屏",
        status="completed",
        created_at="2026-09-14T00:00:00",
        updated_at="2026-09-14T00:00:00",
    )

    class Ledger:
        def get_plan_unscoped(self, plan_id):
            return plan if plan_id == plan.plan_id else None

    class Host:
        ledger = Ledger()

        async def events(self):
            if False:
                yield {}

    provider = ScriptedProvider([], steward_reply="我看完了。整理桌面并截屏已经做完了。")
    repository = _FakeRepository()
    bridge = WorkResultBridge(Host(), repository, None, steward=StewardNarrator(provider))
    await bridge._handle({"type": "plan.completed", "plan_id": "plan-1", "payload": {"status": "completed"}})

    written = repository.written[-1]
    assert written["scope"].conversation_id == "chat-1"
    assert written["scope"].module_id == "companion"  # 写回聊天分区，不是 work 分区
    assert "整理桌面" in written["content"]
    assert written["stable_key"] == "work-result:plan-1:completed"
    # 同一条计划事件重放一次，不该再写一条
    await bridge._handle({"type": "plan.completed", "plan_id": "plan-1", "payload": {"status": "completed"}})
    assert len(repository.written) == 1


@pytest.mark.asyncio
async def test_bridge_cancellation_is_not_reported_as_failure():
    """取消不是失败：不能说"这次没能取回来"。"""
    provider = ScriptedProvider([], steward_reply="好，这件事我停下来了。已经做完的部分都还在。")
    repository = _FakeRepository()
    bridge = WorkResultBridge(_FakeHost([]), repository, None, steward=StewardNarrator(provider))
    await bridge._handle({
        "type": "cancelled",
        "conversation_id": "chat-1",
        "owner_id": "alice",
        "channel": "web",
        "attempt_id": "att-5",
        "task_type": "file.organize",
        "objective": "整理文件",
        "payload": {},
    })
    content = repository.written[-1]["content"]
    assert "没能" not in content
    assert "停下来" in content


def test_temp_sqlite_is_reusable_after_tests(tmp_path):
    close_all_pools()
    assert json.dumps({"ok": True}) == '{"ok": true}'


# ----------------------------------------------------------------------
# 导航失败：按稳定错误码说话，不让模型解释 raw error
# ----------------------------------------------------------------------
NAVIGATION_LINES = {
    "location_unavailable": "我还没有拿到你的当前位置。打开位置共享后，我再给你规划。",
    "location_stale": "我这里的起点有一会儿没更新了。重新共享位置后，我再继续。",
    "destination_not_found": "这个目的地我没定位准。换一个更完整的名称或地址，我再找。",
    "walking_range_exceeded": "这段路超出了步行规划范围。我可以改成驾车路线再看。",
    "navigation_unavailable": "这次路线服务没有响应。我保留了目的地，稍后可以接着查。",
    "destination_required": "你还没说要去哪儿。告诉我目的地，我马上给你规划。",
    "destination_invalid": "这个目的地我读不出来。换个更完整的写法，我再找。",
}


@pytest.mark.parametrize("reason,expected", sorted(NAVIGATION_LINES.items()))
def test_navigation_failure_lines_are_fixed(reason, expected):
    """每个稳定码对应一句固定中文，且一句内部细节都不带。"""
    reply = fast.narrate(fast.navigation_summary(reason))

    assert reply == expected
    for leak in ("_", "reason", "{", "}"):
        assert leak not in reply


def test_unknown_navigation_reason_does_not_guess():
    """认不出的原因码不猜 —— 退回不含事实的兜底话术。"""
    reply = fast.narrate(fast.navigation_summary("provider_exploded"))

    assert reply
    assert "provider_exploded" not in reply
    assert "{" not in reply


@pytest.mark.parametrize("reason,expected_status", [
    ("location_unavailable", fast.STATUS_UNAVAILABLE),
    ("location_stale", fast.STATUS_STALE),
    ("navigation_unavailable", fast.STATUS_FAILED),
])
def test_navigation_summary_status_matches_the_user_action(reason, expected_status):
    """"没开共享"和"服务挂了"必须分档：用户能做的事完全不同。"""
    summary = fast.navigation_summary(reason)

    assert summary.status == expected_status
    assert summary.confirmed is (expected_status not in fast.NOT_CONFIRMED_STATUSES)


def _navigation_registry(result: dict):
    registry = ToolRegistry()

    async def handler(_arguments, _scope):
        return result

    registry.register(ToolSpec(
        name="navigation__route",
        schema={"name": "navigation__route", "description": "route",
                "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk="low",
    ))
    return registry


@pytest.mark.asyncio
async def test_navigation_failure_is_never_handed_to_the_model(tmp_path):
    """导航失败时不再让模型写解释 —— 否则它会把高德的原文复述出来。"""
    provider = ScriptedProvider([
        {"role": "assistant", "tool_calls": [_tool_call("navigation__route", "call-nav")]},
    ])
    sink = RecordingSink()
    registry = _navigation_registry({
        "status": "failed",
        "output": {"reason": "walking_range_exceeded"},
        "error": "walking_range_exceeded",
        "error_kind": "permanent",
    })
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
    )

    outcome = await loop.run(AgentRequest("导航到广州塔", scope=loop.scope))

    assert outcome.status == "completed"
    assert outcome.reply == "这段路超出了步行规划范围。我可以改成驾车路线再看。"
    # 只有"决定调工具"那一次主循环调用；工具执行之后模型一次都没被再叫过。
    # （不能去查 messages 里有没有失败细节：complete() 收到的是同一个
    #  列表对象，工具回执会就地追加进去，看到的必然是"事后"状态。）
    main_calls = [call for call in provider.calls if call["tools"] is not None]
    assert len(main_calls) == 1
    assert "walking_range_exceeded" not in outcome.reply
    # 瞬时失败不值得冒过程气泡：失败说明由最终回复承担
    assert sink.messages == []


@pytest.mark.asyncio
async def test_navigation_failure_bubble_has_no_external_service_text(tmp_path):
    provider = ScriptedProvider([
        {"role": "assistant", "tool_calls": [_tool_call("navigation__route", "call-nav")]},
    ])
    registry = _navigation_registry({
        "status": "failed",
        "output": {"reason": "navigation_unavailable"},
        "error": "navigation_unavailable",
        "error_kind": "transient",
    })
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
    )

    outcome = await loop.run(AgentRequest("导航到广州塔", scope=loop.scope))

    assert outcome.reply == "这次路线服务没有响应。我保留了目的地，稍后可以接着查。"
    for leak in ("Amap", "HTTP", "RuntimeError", "navigation_unavailable", "provider"):
        assert leak not in outcome.reply


@pytest.mark.asyncio
async def test_navigation_failure_without_a_stable_code_falls_back(tmp_path):
    """没有稳定码（老结果形状）就不说 —— 交给原链路，措辞层不许猜。"""
    provider = ScriptedProvider([
        {"role": "assistant", "tool_calls": [_tool_call("navigation__route", "call-nav")]},
        {"role": "assistant", "content": "这次没查到路线。"},
    ])
    registry = _navigation_registry({
        "status": "failed", "output": {}, "error": "boom", "error_kind": "permanent",
    })
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
    )

    outcome = await loop.run(AgentRequest("导航到广州塔", scope=loop.scope))

    assert outcome.reply == "这次没查到路线。"
    assert len([call for call in provider.calls if call["tools"] is not None]) == 2


# ----------------------------------------------------------------------
# 7. 动态措辞：模型负责怎么说，程序负责说什么
# ----------------------------------------------------------------------
def test_recent_phrase_memory_is_bounded_and_isolated_per_conversation():
    memory = RecentPhraseMemory(max_conversations=8)
    memory.remember("a", "我在查资料，先看看能找到什么。")
    assert memory.recent("b") == ()
    assert memory.recent("a") == ("我在查资料，先看看能找到什么。",)
    for index in range(30):
        memory.remember(f"chat-{index}", f"句子{index}")
    # 会话太多时整体清掉：不为了"最近几句别重复"把内存撑大。
    assert memory.recent("chat-0") == ()
    assert len(memory.recent("chat-29")) <= 3


def test_phrase_too_similar_catches_repeats_and_near_repeats():
    recent = ("我在查资料，先看看能找到什么。",)
    assert phrase_too_similar("我在查资料，先看看能找到什么。", recent)
    assert phrase_too_similar("我在查资料，先看看能找到什么", recent)      # 只差标点
    assert phrase_too_similar("我在查资料，先看看能找到什么线索。", recent)  # 近似
    assert not phrase_too_similar("这条线索不够，我换个来源再看。", recent)


@pytest.mark.asyncio
async def test_stage_generator_generates_a_line_per_stage():
    provider = StagePhraseProvider()
    generator = StagePhraseGenerator(provider)

    for stage in (PHASE_SEARCHING, PHASE_MATERIALS, PHASE_READING, PHASE_REDIRECT):
        line = await generator.compose(StewardContext(phase=stage, goal="看这篇论文"))
        assert line == stage_line(stage)

    assert provider.stage_prompts_seen() == [
        PHASE_SEARCHING, PHASE_MATERIALS, PHASE_READING, PHASE_REDIRECT,
    ]


@pytest.mark.asyncio
async def test_stage_generator_only_receives_safe_public_information():
    provider = StagePhraseProvider()
    generator = StagePhraseGenerator(provider, persona="爱尔奎特", forbidden_terms={"search__web"})

    await generator.compose(
        StewardContext(
            phase=PHASE_READING,
            goal="帮我看一下这篇论文",
            next_action="等这份原文取回来",
            evidence=("这份原文已经成功取回",),
        ),
        recent=("我在查这件事，先看看有什么线索。",),
    )

    prompt = "\n".join(str(message.get("content") or "") for message in provider.stage_calls[0])
    assert "帮我看一下这篇论文" in prompt          # 目标摘要
    assert "等这份原文取回来" in prompt             # 确实会执行的下一步
    assert "这份原文已经成功取回" in prompt         # 可公开的事实摘要
    assert "我在查这件事，先看看有什么线索。" in prompt  # 近期已发过程句
    # 工具名、路径、异常、正文一个都不能进去
    for forbidden in ("search__web", "http", "C:\\", "Traceback", "content", "调用"):
        assert forbidden not in prompt, forbidden


@pytest.mark.asyncio
async def test_stage_generator_returns_empty_without_a_model():
    """没有模型就一个字都不说 —— 不回退任何固定句。"""
    generator = StagePhraseGenerator(None)
    assert await generator.compose(StewardContext(phase=PHASE_SEARCHING)) == ""


@pytest.mark.asyncio
async def test_stage_generator_failure_timeout_and_bad_output_all_return_empty():
    failing = StagePhraseGenerator(StagePhraseProvider(fail=True))
    assert await failing.compose(StewardContext(phase=PHASE_SEARCHING)) == ""

    slow = StagePhraseGenerator(StagePhraseProvider(delay=0.3), timeout_seconds=0.1)
    assert await slow.compose(StewardContext(phase=PHASE_SEARCHING)) == ""

    # 不合格：没有证据就说"找到了" / 输出协议文本
    for bad in ("找到了，就在正文里。", "```json\n{}\n```"):
        generator = StagePhraseGenerator(StagePhraseProvider(replies=[bad, bad]))
        assert await generator.compose(StewardContext(phase=PHASE_SEARCHING)) == ""


@pytest.mark.asyncio
async def test_stage_generator_retries_a_duplicate_then_gives_up():
    recent = ("我在查这件事，先看看有什么线索。",)

    duplicate = StagePhraseGenerator(
        StagePhraseProvider(replies=["我在查这件事，先看看有什么线索。", "我在查这件事，先看看有什么线索。"]),
    )
    assert await duplicate.compose(StewardContext(phase=PHASE_SEARCHING), recent=recent) == ""

    retried = StagePhraseGenerator(
        StagePhraseProvider(replies=["我在查这件事，先看看有什么线索。", "换个说法：我在翻相关材料。"]),
    )
    assert await retried.compose(StewardContext(phase=PHASE_SEARCHING), recent=recent) == "换个说法：我在翻相关材料。"


@pytest.mark.asyncio
async def test_stage_generator_tells_the_model_why_the_last_try_was_rejected():
    """重试不是原样再问一次：要把它自己那句为什么不能用说清楚。"""
    provider = StagePhraseProvider(replies=["找到了，就在正文里。", "我还在翻，先看看有什么。"])
    generator = StagePhraseGenerator(provider)

    line = await generator.compose(StewardContext(phase=PHASE_SEARCHING))

    assert line == "我还在翻，先看看有什么。"
    second_prompt = "\n".join(
        str(message.get("content") or "") for message in provider.stage_calls[-1]
    )
    assert "上一句为什么不能用" in second_prompt
    assert "找到了" in second_prompt       # 指名道姓说清哪类词不能用


@pytest.mark.asyncio
async def test_chat_stage_publisher_composes_then_publishes_and_remembers():
    provider = StagePhraseProvider()
    sink = RecordingSink()
    reporter = StewardReporter(StewardNarrator(provider), sink=sink)
    publisher = ChatStagePublisher(
        reporter,
        scope=Scope("alice", "web", "chat-1", "companion"),
        goal="看这篇论文",
        run_id="run-1",
        turn_id="turn-1",
    )
    report = StageReport(PHASE_READING, node="reading", next_action="等这份原文取回来")

    text = await publisher.compose(report)
    assert text == stage_line(PHASE_READING)
    published = await publisher.publish(report, text)

    assert published == text
    assert sink.messages[0]["phase"] == PHASE_READING
    assert sink.messages[0]["step_id"] == "reading"
    assert sink.messages[0]["kind"] == STEWARD_PROGRESS_KIND
    # 发出去之后进入"近期已发"：下一句不许复读它
    assert publisher.memory.recent(publisher.conversation_key) == (text,)
    assert await publisher.compose(StageReport(PHASE_READING, node="read:2")) == ""


@pytest.mark.asyncio
async def test_chat_stage_publisher_publishes_nothing_without_a_model():
    """措辞没模型：不硬凑一句，也不写任何东西。"""
    sink = RecordingSink()
    publisher = ChatStagePublisher(
        StewardReporter(StewardNarrator(None), sink=sink),
        scope=Scope("alice", "web", "chat-1", "companion"),
    )

    report = StageReport(PHASE_READING, node="reading")
    assert await publisher.compose(report) == ""
    assert await publisher.publish(report, "") == ""
    assert sink.messages == []


@pytest.mark.asyncio
async def test_publish_text_keeps_dedupe_and_sink_rejection_behaviour():
    sink = RecordingSink()
    reporter = StewardReporter(StewardNarrator(None), sink=sink)
    scope = Scope("alice", "web", "chat-1", "companion")

    first = await reporter.publish_text(
        scope, "我在查资料，先看看能找到什么。",
        phase=PHASE_SEARCHING, run_id="run-1", step_id="first_query",
    )
    repeat = await reporter.publish_text(
        scope, "换个说法也不许再发",
        phase=PHASE_SEARCHING, run_id="run-1", step_id="first_query",
    )
    assert first and repeat == ""

    sink.reject_next = True
    rejected = await reporter.publish_text(
        scope, "我在读原文，核对里面的说法。",
        phase=PHASE_READING, run_id="run-1", step_id="read:1",
    )
    assert rejected == ""
    assert len(sink.messages) == 1


# ----------------------------------------------------------------------
# 8. 收口：进度一定在最终回答之前，取消/下一轮不补发
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stage_notice_lands_before_the_final_answer(tmp_path):
    """落库顺序与即时事件顺序一致：进度在前，最终回答在后。"""
    clock = _StageClock()
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
        {"role": "assistant", "content": "看完了。"},
    ])
    sink = RecordingSink()
    bus = InMemoryEventBus()
    order: list[str] = []
    for event_type in (EventType.STEWARD_PROGRESS, EventType.CHAT_DELTA):
        bus.subscribe(event_type, lambda event: order.append(event.type.value))

    loop = AgentLoop(
        provider,
        _timed_registry(clock, seconds=5.0),
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        event_bus=bus,
        steward=StewardReporter(StewardNarrator(provider), sink=sink, event_bus=bus),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    outcome = await loop.run(AgentRequest("你去看一下这篇论文", scope=loop.scope))

    assert outcome.reply
    assert order == [EventType.STEWARD_PROGRESS.value, EventType.CHAT_DELTA.value]
    assert sink.messages[0]["phase"] == PHASE_SEARCHING
    assert sink.messages[0]["content"] == stage_line(PHASE_SEARCHING)


@pytest.mark.asyncio
async def test_cancelled_turn_never_sends_the_pending_notice(tmp_path):
    """取消：待发的那条进度必须停住，取消之后一条都不许再冒出来。"""
    clock = _StageClock()
    release = asyncio.Event()

    async def handler(call: ToolCall, scope: Scope) -> ToolResult:
        await release.wait()
        return ToolResult(status="ok", output={"results": [{"url": "https://a"}]})

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="search__web",
        schema={"name": "search__web", "description": "s", "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk="low",
    ))
    provider = ScriptedProvider([{"role": "assistant", "content": "", "tool_calls": [_tool_call()]}])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    task = asyncio.ensure_future(loop.run(AgentRequest("你去看一下这篇论文", scope=loop.scope)))
    for _ in range(20):
        await asyncio.sleep(0)
    await clock.advance(0.5)          # 还没到 1.5 秒，进度还在待发
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await clock.advance(60.0)
    assert sink.messages == []


@pytest.mark.asyncio
async def test_next_turn_does_not_inherit_a_pending_notice(tmp_path):
    """跨回合隔离：上一轮没发出去的进度不许拖到下一轮才冒出来。"""
    clock = _StageClock()
    calls = {"n": 0}

    async def handler(call: ToolCall, scope: Scope) -> ToolResult:
        calls["n"] += 1
        await clock.advance(1.0 if calls["n"] == 1 else 5.0)
        return ToolResult(status="ok", output={"results": [{"url": "https://a"}]})

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="search__web",
        schema={"name": "search__web", "description": "s", "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk="low",
    ))
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
        {"role": "assistant", "content": "第一轮结束。"},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("search__web", "call-2")]},
        {"role": "assistant", "content": "第二轮结束。"},
    ])
    sink = RecordingSink()
    loop = AgentLoop(
        provider,
        registry,
        Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(StewardNarrator(provider), sink=sink),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )

    await loop.run(AgentRequest("第一轮", scope=loop.scope))
    assert sink.messages == []          # 1.0 秒就结束：没有过程气泡
    await loop.run(AgentRequest("第二轮", scope=loop.scope))

    phases = [message["phase"] for message in sink.messages]
    assert phases == [PHASE_SEARCHING]
    assert len(sink.messages) == 1
