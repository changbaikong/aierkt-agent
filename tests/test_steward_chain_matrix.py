"""长链路 / 短链路 × 普通搜索 / 代码代理：管家到底呈现出什么状态。

四格矩阵（每格都用可控时钟，不真等、不联网）：

|            | 短链路                                   | 长链路                                                     |
| ---------- | ---------------------------------------- | ---------------------------------------------------------- |
| 普通搜索   | 1 秒答完 → 零过程气泡                    | 查→再查→读原文 → 正在查 / 取得材料 / 正在读原文             |
| 代码代理   | 看文件+改代码挤在 1 秒 → 只冒第一条        | 看→改→验证→补丁 → 四条阶段各一次，回执排在最后              |

断言的是改造计划里的既定规则：首次等待满 1.5 秒才说、相邻至少 4 秒、
聊天一轮最多"一条首次 + 两条中段"；代码任务四类阶段各一次、不套聊天上限；
气泡里不许出现查询词、网址、路径、命令或工具名。

措辞现在是**模型现场生成**的，所以这里给两个 reporter 都接上测试替身
（:class:`StagePhraseProvider`：按"当前阶段"回一句测试话术，生产路径没有句库）。
断言因此写成 ``content == stage_line(stage)``：既不认死某一个句子，
也仍然盯住"哪一步说了什么"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_core.contracts import (
    AgentRequest,
    ProviderResponse,
    Scope,
    ToolResult,
    ToolSpec,
)
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.steward import (
    RecentPhraseMemory,
    StewardNarrator,
    StewardReporter,
)
from agent_core.steward_progress import (
    STAGE_MATERIALS,
    STAGE_READING,
    STAGE_SEARCHING,
    STAGE_WORK_DELIVER,
    STAGE_WORK_EDIT,
    STAGE_WORK_INSPECT,
    STAGE_WORK_VERIFY,
)
from agent_core.tools import ToolRegistry
from agent_core.work_bridge import WorkResultBridge
from agent_core.work_progress_bridge import WorkProgressBridge
from support.stage_phrases import StagePhraseProvider, stage_line

SCOPE = Scope("alice", "web", "chain-matrix", "companion")
WORK_MODULE = "work"


# ----------------------------------------------------------------------
# 可控时钟 + 观测替身
# ----------------------------------------------------------------------
class ChainClock:
    """可推进的时钟：advance() 会让等待中的进度计时器按时间醒过来。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)
        self._sleepers: list[list[Any]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        if delay <= 0:
            await asyncio.sleep(0)
            return
        entry: list[Any] = [self.now + float(delay), asyncio.get_running_loop().create_future()]
        self._sleepers.append(entry)
        try:
            await entry[1]
        finally:
            if entry in self._sleepers:
                self._sleepers.remove(entry)

    async def advance(self, delta: float) -> None:
        self.now += float(delta)
        for entry in list(self._sleepers):
            if entry[0] <= self.now + 1e-9:
                self._sleepers.remove(entry)
                if not entry[1].done():
                    entry[1].set_result(None)
        await settle()


async def settle(rounds: int = 12) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def tick_for(clock: "ChainClock", seconds: float, step: float = 0.25) -> None:
    """让时间"走"过 seconds 秒，而不是一步跳过去。

    一步跳 5 秒的话，等待中的通知只会在工具结束时才被唤醒，1.5 秒 / 4 秒这两个
    门槛就测不出来（真实世界里时间是一格一格走的）。
    """
    remaining = float(seconds)
    while remaining > 0:
        chunk = min(step, remaining)
        await clock.advance(chunk)
        remaining -= chunk


class Sink:
    """记下每条真正落库的过程消息与它发生的时刻。"""

    def __init__(self, clock: ChainClock) -> None:
        self.clock = clock
        self.messages: list[dict[str, Any]] = []

    def __call__(self, scope, message, stable_key):
        self.messages.append({
            "at": self.clock.now,
            "phase": str(message.get("phase") or ""),
            "content": str(message.get("content") or ""),
            "kind": str(message.get("kind") or ""),
        })
        return True

    @property
    def phases(self) -> list[str]:
        return [message["phase"] for message in self.messages]

    @property
    def contents(self) -> list[str]:
        return [message["content"] for message in self.messages]


def _stage_reporter(sink: Sink) -> StewardReporter:
    """接上测试替身模型的过程 reporter。

    中段措辞由模型现场生成：发布器从 reporter 的 narrator 复用模型，
    所以不接模型就一条气泡都出不来（生产路径同样如此，只是换成真模型）。
    """
    return StewardReporter(StewardNarrator(StagePhraseProvider()), sink=sink)


class ScriptedProvider(LLMProvider):
    """按脚本返回：工具调用与最终回答交替。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.index = 0
        self.calls = 0

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls += 1
        if self.index >= len(self.responses):
            raise RuntimeError("no more scripted responses")
        response = self.responses[self.index]
        self.index += 1
        return ProviderResponse(message=response, usage={"prompt_tokens": 1, "completion_tokens": 1})


def _tool_call(name: str, call_id: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    import json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def _search_registry(clock: ChainClock, *, seconds: float, with_read: bool = False) -> ToolRegistry:
    """一次搜索：花 seconds 秒，带回两条材料；带 with_read 时再挂一个原文读取工具。"""

    async def handler(call, scope):
        await tick_for(clock, seconds)
        return ToolResult(status="ok", output={
            "query": "论文",
            "result_count": 2,
            "results": [
                {"title": "材料一", "url": "https://example.com/a", "snippet": "片段一"},
                {"title": "材料二", "url": "https://example.com/b", "snippet": "片段二"},
            ],
        })

    async def read_handler(call, scope):
        await tick_for(clock, 0.5)
        return ToolResult(status="ok", output={
            "url": "https://example.com/a",
            "fetch_status": "ok",
            "title": "原文标题",
            "content": "正文内容：这里写着事情的来龙去脉。",
        })

    registry = ToolRegistry()
    registry.register(ToolSpec(
        "search__web",
        {"name": "search__web", "description": "search", "input_schema": {"type": "object", "properties": {}}},
        handler,
        risk="low",
    ))
    if with_read:
        registry.register(ToolSpec(
            "search__read",
            {"name": "search__read", "description": "read", "input_schema": {"type": "object", "properties": {}}},
            read_handler,
            risk="low",
        ))
    return registry


async def _search_loop(tmp_path: Path, clock: ChainClock, sink: Sink, provider, registry) -> AgentLoop:
    return AgentLoop(
        provider,
        registry,
        SCOPE,
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        steward=_stage_reporter(sink),
        progress_clock=clock,
        progress_sleeper=clock.sleep,
    )


# ----------------------------------------------------------------------
# 代码代理侧：真实 WorkProgressBridge + 真实终态回执桥
# ----------------------------------------------------------------------
class FakeHost:
    def __init__(self) -> None:
        self.ledger = None


def _work_tool_event(kind: str, *, phase: str = "started") -> dict[str, Any]:
    return {
        "type": "provider_tool",
        "attempt_id": "attempt-1",
        "work_item_id": "work-1",
        "owner_id": "alice",
        "channel": "web",
        "conversation_id": "chain-matrix",
        "module_id": WORK_MODULE,
        "task_type": "code.agent",
        "objective": "把 app.py 里的 value 改成 2",
        "payload": {
            "tool_call_id": "t1",
            "title": "运行 pytest -q",   # 自由文本标题：谁都不许当依据
            "kind": kind,
            "status": "in_progress",
            "phase": phase,
        },
    }


def _work_progress_event(phase: str) -> dict[str, Any]:
    return {
        "type": "progress",
        "attempt_id": "attempt-1",
        "work_item_id": "work-1",
        "owner_id": "alice",
        "channel": "web",
        "conversation_id": "chain-matrix",
        "module_id": WORK_MODULE,
        "task_type": "code.agent",
        "payload": {"phase": phase},
    }


def _work_terminal_event(status: str = "completed") -> dict[str, Any]:
    return {
        "type": status,
        "attempt_id": "attempt-1",
        "work_item_id": "work-1",
        "owner_id": "alice",
        "channel": "web",
        "conversation_id": "chain-matrix",
        "module_id": WORK_MODULE,
        "task_type": "code.agent",
        "objective": "把 app.py 里的 value 改成 2",
        "payload": {"output": {"summary": "改好了", "patch": "diff --git a/app.py b/app.py"}},
    }


class ReceiptRepository:
    """只记"什么时候写了一条终态回执"。"""

    def __init__(self, journal: list[str]) -> None:
        self.journal = journal

    def append_message(self, scope, message, stable_key="", **kwargs):
        self.journal.append("回执")
        return True


def _work_bridges(clock: ChainClock, sink: Sink, journal: list[str]) -> tuple[WorkProgressBridge, WorkResultBridge]:
    # 代码任务的过程措辞同样现场生成：桥从 reporter 的 narrator 建生成器，
    # 自己只留一份"同一会话最近说过什么"（避免复读）。
    progress = WorkProgressBridge(
        FakeHost(),
        _stage_reporter(sink),
        memory=RecentPhraseMemory(),
        clock=clock,
        sleeper=clock.sleep,
    )

    async def hook(attempt_id: str) -> None:
        journal.append("先关进度")
        await progress.close_attempt(attempt_id)

    result = WorkResultBridge(
        FakeHost(), ReceiptRepository(journal), None, progress_hook=hook,
    )
    return progress, result


# ----------------------------------------------------------------------
# 四格
# ----------------------------------------------------------------------
async def scenario_search_short(tmp_path: Path) -> dict[str, Any]:
    """普通搜索 · 短链路：1 秒答完，一条过程气泡都不该冒。"""
    clock = ChainClock()
    sink = Sink(clock)
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("search__web", "c1")]},
        {"role": "assistant", "content": "这件事材料里写得很清楚，我直接说结论。"},
        {"role": "assistant", "content": "这件事材料里写得很清楚，我直接说结论。"},
    ])
    loop = await _search_loop(
        tmp_path, clock, sink, provider, _search_registry(clock, seconds=1.0),
    )
    started = clock.now
    outcome = await loop.run(AgentRequest("你去看一下这篇论文", scope=loop.scope))
    return {
        "name": "普通搜索 · 短链路",
        "detail": "1 次搜索（1 秒）后直接回答",
        "seconds": clock.now - started,
        "sink": sink,
        "outcome": outcome,
        "loop": loop,
    }


async def scenario_search_long(tmp_path: Path) -> dict[str, Any]:
    """普通搜索 · 长链路：查一次 → 再查一次 → 读原文 → 回答。"""
    clock = ChainClock()
    sink = Sink(clock)
    provider = ScriptedProvider([
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("search__web", "c1")]},
        {"role": "assistant", "content": "", "tool_calls": [_tool_call("search__web", "c2")]},
        {"role": "assistant", "content": "", "tool_calls": [
            _tool_call("search__read", "c3", {"url": "https://example.com/a"}),
        ]},
        {"role": "assistant", "content": "材料里说的是这件事的来龙去脉，我按顺序讲。"},
        {"role": "assistant", "content": "材料里说的是这件事的来龙去脉，我按顺序讲。"},
    ])
    registry = _search_registry(clock, seconds=5.0, with_read=True)
    loop = await _search_loop(tmp_path, clock, sink, provider, registry)
    started = clock.now
    outcome = await loop.run(AgentRequest("你去看一下这篇论文再核对原文", scope=loop.scope))
    return {
        "name": "普通搜索 · 长链路",
        "detail": "两次搜索（各 5 秒）+ 读一份原文",
        "seconds": clock.now - started,
        "sink": sink,
        "outcome": outcome,
        "loop": loop,
    }


async def scenario_code_short() -> dict[str, Any]:
    """代码代理 · 短链路：看文件 + 改代码挤在 1 秒内，只冒第一条。"""
    clock = ChainClock()
    sink = Sink(clock)
    journal: list[str] = []
    progress, result = _work_bridges(clock, sink, journal)

    await progress.handle_event(_work_tool_event("read"))
    await settle()
    await clock.advance(1.0)
    await progress.handle_event(_work_tool_event("edit"))
    await settle()
    await clock.advance(0.5)
    await result._handle(_work_terminal_event("completed"))
    await clock.advance(30.0)

    return {
        "name": "代码代理 · 短链路",
        "detail": "看文件 → 改代码（1 秒内）→ 完成回执",
        "seconds": clock.now,
        "sink": sink,
        "journal": journal,
    }


async def scenario_code_long() -> dict[str, Any]:
    """代码代理 · 长链路：看 → 改 → 验证 → 补丁 → 回执。"""
    clock = ChainClock()
    sink = Sink(clock)
    journal: list[str] = []
    progress, result = _work_bridges(clock, sink, journal)

    for event in (
        _work_tool_event("read"),
        _work_tool_event("edit"),
        _work_progress_event("verification_started"),
        _work_progress_event("patch_ready"),
    ):
        await progress.handle_event(event)
        await settle()
        await clock.advance(5.0)
    await result._handle(_work_terminal_event("completed"))
    await clock.advance(30.0)

    return {
        "name": "代码代理 · 长链路",
        "detail": "看文件 → 改代码 → 跑验证 → 补丁生成（每步 5 秒）",
        "seconds": clock.now,
        "sink": sink,
        "journal": journal,
    }


async def run_matrix(tmp_path: Path) -> list[dict[str, Any]]:
    """依次跑完四格，返回观测结果（脚本用它打印对照表）。"""
    return [
        await scenario_search_short(tmp_path),
        await scenario_search_long(tmp_path),
        await scenario_code_short(),
        await scenario_code_long(),
    ]


def format_matrix(results: list[dict[str, Any]]) -> str:
    """把四格结果排成一张能直接看的表。"""
    lines: list[str] = []
    for result in results:
        sink: Sink = result["sink"]
        lines.append(f"\n【{result['name']}】{result['detail']}")
        lines.append(f"  实际耗时: {result['seconds']:.2f}s   过程气泡: {len(sink.messages)} 条")
        if not sink.messages:
            lines.append("  （零过程气泡，直接给最终结果）")
        for index, message in enumerate(sink.messages):
            offset = message["at"] - sink.messages[0]["at"]
            gap = "" if index == 0 else f"  间隔 {message['at'] - sink.messages[index - 1]['at']:.2f}s"
            lines.append(f"  +{offset:5.2f}s  [{message['phase']}]  {message['content']}{gap}")
        if result.get("journal"):
            lines.append(f"  终态收口顺序: {' → '.join(result['journal'])}")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# 断言：四格各自该是什么样
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_short_search_chain_stays_silent(tmp_path: Path):
    result = await scenario_search_short(tmp_path)
    assert result["sink"].messages == [], "1 秒答完的短链路不该有过程气泡"


@pytest.mark.asyncio
async def test_long_search_chain_speaks_real_stages_in_order(tmp_path: Path):
    result = await scenario_search_long(tmp_path)
    sink: Sink = result["sink"]

    assert sink.phases == [STAGE_SEARCHING, STAGE_MATERIALS, STAGE_READING]
    # 一条首次 + 两条中段，正好顶到上限但没超
    assert len(sink.messages) == 3
    for stage, content in zip(sink.phases, sink.contents):
        assert content == stage_line(stage)
    gaps = [
        sink.messages[index + 1]["at"] - sink.messages[index]["at"]
        for index in range(len(sink.messages) - 1)
    ]
    assert all(gap >= 4.0 for gap in gaps), gaps
    # 短链路那条的措辞不许退回"准备开始查"
    assert "准备" not in "".join(sink.contents[1:])
    for forbidden in ("论文", "http", "example.com", "search__"):
        assert all(forbidden not in content for content in sink.contents), forbidden


@pytest.mark.asyncio
async def test_short_code_chain_merges_into_one_bubble_and_closes_before_the_receipt():
    result = await scenario_code_short()
    sink: Sink = result["sink"]

    assert sink.phases == [STAGE_WORK_INSPECT], "1 秒内挤在一起的阶段只冒第一条"
    assert sink.contents[0] == stage_line(STAGE_WORK_INSPECT)
    assert result["journal"] == ["先关进度", "回执"]
    for forbidden in ("app.py", "pytest", "value", "/", "\\"):
        assert forbidden not in sink.contents[0]


@pytest.mark.asyncio
async def test_long_code_chain_reports_each_stage_once_with_four_second_gaps():
    result = await scenario_code_long()
    sink: Sink = result["sink"]

    assert sink.phases == [
        STAGE_WORK_INSPECT, STAGE_WORK_EDIT, STAGE_WORK_VERIFY, STAGE_WORK_DELIVER,
    ]
    for stage, content in zip(sink.phases, sink.contents):
        assert content == stage_line(stage)
    gaps = [
        sink.messages[index + 1]["at"] - sink.messages[index]["at"]
        for index in range(len(sink.messages) - 1)
    ]
    assert all(gap >= 4.0 for gap in gaps), gaps
    # 只有补丁真的生成后才说"准备交付"，回执永远排在最后
    assert result["journal"] == ["先关进度", "回执"]
    for forbidden in ("app.py", "pytest", "Traceback"):
        assert all(forbidden not in content for content in sink.contents), forbidden


@pytest.mark.asyncio
async def test_matrix_shows_short_chains_quiet_and_long_chains_narrated(tmp_path: Path):
    """一张表看清四格差别：短的两格安静，长的两格分段说话。"""
    results = await run_matrix(tmp_path)
    table = format_matrix(results)
    assert "零过程气泡" in table
    quiet, narrated = results[0]["sink"], results[1]["sink"]
    assert len(quiet.messages) < len(narrated.messages)
    assert len(results[2]["sink"].messages) == 1
    assert len(results[3]["sink"].messages) == 4
