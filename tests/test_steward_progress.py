"""本回合过程通知控制的行为测试。

用**可控时钟**跑：既不真的等 1.5 秒 / 4 秒，也不依赖 sleep 抖动。
这里只验证控制规则（时间、合并、上限、收口、隔离），
不验证措辞好坏 —— 措辞在 tests/test_steward.py。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_core.contracts import Scope, ToolResult
from agent_core.steward_progress import (
    STAGE_MATERIALS,
    STAGE_READ_DONE,
    STAGE_READING,
    STAGE_REDIRECT,
    STAGE_SEARCHING,
    STATUS_CLOSED,
    STATUS_QUEUED,
    STATUS_SENT,
    STATUS_SKIPPED,
    STATUS_SUPPRESSED,
    ProgressBoard,
    ProgressPolicy,
    StageReport,
    TurnProgress,
    conversation_identity,
    read_outcome,
    search_outcome,
    stage_for_results,
)


class FakeClock:
    """可推进的时钟 + 配套 sleeper：advance() 会让等待中的任务按时间醒过来。"""

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

    def sleeping(self) -> int:
        return len(self._sleepers)


async def settle(rounds: int = 12) -> None:
    """把已经就绪的任务跑到下一个 await 点。"""
    for _ in range(rounds):
        await asyncio.sleep(0)


def make_progress(clock: FakeClock, sent: list[str], **kwargs: Any) -> TurnProgress:
    async def emit(report: StageReport, text: str) -> None:
        sent.append(report.stage)

    return TurnProgress(
        turn_id="turn-1",
        conversation_key="c1",
        clock=clock,
        sleeper=clock.sleep,
        emit=emit,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_quick_turn_produces_no_bubble():
    """1.4 秒内就结束的回合：一条过程气泡都不发（时间不制造进展）。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    assert await progress.note(StageReport(STAGE_SEARCHING, node="first_query")) == STATUS_QUEUED
    await clock.advance(1.4)
    assert sent == []
    await progress.close()
    await settle()
    assert sent == []


@pytest.mark.asyncio
async def test_first_notice_fires_at_one_and_a_half_seconds():
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.4)
    assert sent == []
    await clock.advance(0.1)
    assert sent == [STAGE_SEARCHING]
    assert progress.pending is None
    await progress.close()


@pytest.mark.asyncio
async def test_pending_holds_only_latest_state():
    """1.5 秒前连着换了好几个阶段：只发到期时仍然成立的那一个。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.0)
    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    await clock.advance(0.2)
    await progress.note(StageReport(STAGE_READING, node="read:1", has_more=True))
    assert progress.pending is not None and progress.pending.stage == STAGE_READING
    await clock.advance(0.3)
    assert sent == [STAGE_READING]


@pytest.mark.asyncio
async def test_adjacent_notices_are_four_seconds_apart():
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)
    assert sent == [STAGE_SEARCHING]

    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    await clock.advance(3.9)
    assert sent == [STAGE_SEARCHING]
    await clock.advance(0.1)
    assert sent == [STAGE_SEARCHING, STAGE_MATERIALS]
    await progress.close()


@pytest.mark.asyncio
async def test_inside_the_gap_the_newest_state_wins():
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)
    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    await clock.advance(0.1)
    await progress.note(StageReport(STAGE_READING, node="read:1", has_more=True))
    await clock.advance(0.1)
    await progress.note(StageReport(STAGE_READ_DONE, node="read:done:1", has_more=True))
    await clock.advance(3.8)
    assert sent == [STAGE_SEARCHING, STAGE_READ_DONE]


@pytest.mark.asyncio
async def test_at_most_two_middle_notices_per_turn():
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)
    for index, stage in enumerate((STAGE_MATERIALS, STAGE_READING), start=1):
        assert await progress.note(
            StageReport(stage, node=f"node:{index}", has_more=True)
        ) == STATUS_QUEUED
        await clock.advance(4.0)
    assert sent == [STAGE_SEARCHING, STAGE_MATERIALS, STAGE_READING]
    assert await progress.note(
        StageReport(STAGE_REDIRECT, node="node:3")
    ) == STATUS_SUPPRESSED
    assert await progress.note(StageReport(STAGE_REDIRECT, node="node:4")) == STATUS_SUPPRESSED
    await clock.advance(10.0)
    assert sent == [STAGE_SEARCHING, STAGE_MATERIALS, STAGE_READING]
    await progress.close()


@pytest.mark.asyncio
async def test_stage_without_a_follow_up_is_not_queued():
    """马上就要给最终回答（后面没有查询/读取）时不补过程句。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)
    assert sent == [STAGE_SEARCHING]

    result = await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=False))
    assert result == STATUS_SKIPPED
    await clock.advance(30.0)
    assert sent == [STAGE_SEARCHING]
    await progress.close()


@pytest.mark.asyncio
async def test_same_node_is_reported_once():
    """模型自己查到的材料和补查查到的材料共用一个节点，不许重复说"找到了"。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    await clock.advance(1.5)
    assert sent == [STAGE_MATERIALS]
    assert await progress.note(
        StageReport(STAGE_MATERIALS, node="materials", has_more=True)
    ) == STATUS_SKIPPED
    await clock.advance(10.0)
    assert sent == [STAGE_MATERIALS]
    await progress.close()


@pytest.mark.asyncio
async def test_immediate_stage_ignores_pacing_and_cap():
    """等待用户确认这类必须告知的消息：不用等 1.5 秒，也不占中段名额。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    assert await progress.note(
        StageReport("waiting_approval", node="approval:1")
    ) == STATUS_SENT
    assert sent == ["waiting_approval"]
    assert progress.stats()["mid_sent"] == 0
    await progress.close()


@pytest.mark.asyncio
async def test_timing_starts_only_with_a_real_query():
    """还没开始真实查询时，先记的阶段不会自己冒出来；查询真开始了才排期。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    await clock.advance(30.0)
    assert sent == []
    progress.begin_query()
    await clock.advance(1.4)
    assert sent == []
    await clock.advance(0.2)
    assert sent == [STAGE_MATERIALS]
    await progress.close()


@pytest.mark.asyncio
async def test_progress_arriving_while_a_send_is_in_flight_is_not_lost():
    """落库要 await：这期间到达的新进展必须被继续处理，不能被丢掉。"""
    clock = FakeClock()
    sent: list[str] = []
    sending = asyncio.Event()
    release = asyncio.Event()

    async def emit(report: StageReport, text: str) -> None:
        sent.append(report.stage)
        if report.stage == STAGE_SEARCHING:
            sending.set()
            await release.wait()

    progress = TurnProgress(clock=clock, sleeper=clock.sleep, emit=emit)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)
    assert sending.is_set()
    assert sent == [STAGE_SEARCHING]

    # 第一条还在"落库"的路上，第二条进展已经到了
    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    release.set()
    await settle()
    await clock.advance(3.9)
    assert sent == [STAGE_SEARCHING]
    await clock.advance(0.2)
    assert sent == [STAGE_SEARCHING, STAGE_MATERIALS]
    await progress.close()


@pytest.mark.asyncio
async def test_close_is_final_and_cancels_the_pending_timer():
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await progress.close()
    assert progress.closed is True
    assert clock.sleeping() == 0
    await clock.advance(60.0)
    assert sent == []
    assert await progress.note(StageReport(STAGE_MATERIALS, node="materials")) == STATUS_CLOSED
    await clock.advance(60.0)
    assert sent == []


@pytest.mark.asyncio
async def test_close_waits_for_the_notice_already_being_stored():
    """已经在落库路上的那条要收口，不能跑到最终回答后面去。"""
    done: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def emit(report: StageReport, text: str) -> None:
        started.set()
        await release.wait()
        done.append(report.stage)

    progress = TurnProgress(turn_id="t", emit=emit)
    progress.begin_query()
    noting = asyncio.ensure_future(progress.note(StageReport("waiting_approval", node="approval")))
    await started.wait()
    closing = asyncio.ensure_future(progress.close())
    await asyncio.sleep(0.05)
    assert closing.done() is False, "落库还没收口，close 不能先返回"
    release.set()
    await asyncio.gather(noting, closing)
    assert done == ["waiting_approval"]


@pytest.mark.asyncio
async def test_emitter_failure_does_not_break_the_main_flow():
    """通知通道故障：主任务继续，失败也不变成内部错误气泡。"""

    async def emit(report: StageReport, text: str) -> None:
        raise RuntimeError("sink down")

    clock = FakeClock()
    progress = TurnProgress(clock=clock, sleeper=clock.sleep, emit=emit)
    progress.begin_query()
    assert await progress.note(StageReport("waiting_approval", node="approval")) == STATUS_SENT
    assert progress.failures == 1
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    # 上一条（必须告知的那条）也算"相邻过程消息"，所以这里仍要等满 4 秒。
    await clock.advance(4.5)
    assert progress.failures == 2
    await progress.close()


# ----------------------------------------------------------------------
# 动态措辞：先生成、再发布（生成可取消，发布有闸）
# ----------------------------------------------------------------------
def _two_phase(clock: FakeClock, sent: list[tuple[str, str]], compose) -> TurnProgress:
    async def emit(report: StageReport, text: str) -> None:
        sent.append((report.stage, text))

    return TurnProgress(
        turn_id="turn-1",
        conversation_key="c1",
        clock=clock,
        sleeper=clock.sleep,
        compose=compose,
        emit=emit,
    )


@pytest.mark.asyncio
async def test_generated_phrase_is_published_with_its_text():
    """措辞由 compose 现场生成，发出去的就是这一句（不是固定模板）。"""
    clock = FakeClock()
    sent: list[tuple[str, str]] = []

    async def compose(report: StageReport) -> str:
        return f"针对{report.stage}现写的一句。"

    progress = _two_phase(clock, sent, compose)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)

    assert sent == [(STAGE_SEARCHING, "针对searching现写的一句。")]
    assert progress.composed == ["针对searching现写的一句。"]
    assert progress.failures == 0
    await progress.close()


@pytest.mark.asyncio
async def test_generation_failure_is_skipped_without_any_fallback():
    """生成失败 / 超时（compose 返回空串）→ 静默跳过，绝不回退固定句。"""
    clock = FakeClock()
    sent: list[tuple[str, str]] = []

    async def compose(report: StageReport) -> str:
        return ""

    progress = _two_phase(clock, sent, compose)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)

    assert sent == []
    assert progress.failures == 1
    # 节点身份仍然被消耗：同一阶段不会因为生成失败而反复占用后台模型
    assert await progress.note(StageReport(STAGE_SEARCHING, node="first_query")) == STATUS_SKIPPED
    await clock.advance(30.0)
    assert sent == []
    await progress.close()


@pytest.mark.asyncio
async def test_compose_exception_is_swallowed():
    """措辞层抛异常也一样：跳过这一条，主任务不受影响。"""
    clock = FakeClock()
    sent: list[tuple[str, str]] = []

    async def compose(report: StageReport) -> str:
        raise RuntimeError("model exploded")

    progress = _two_phase(clock, sent, compose)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)

    assert sent == []
    assert progress.failures == 1
    await progress.close()


@pytest.mark.asyncio
async def test_close_cancels_an_unfinished_generation():
    """终态关闸：还没生成完的那句直接被取消，一条都不发。"""
    clock = FakeClock()
    sent: list[tuple[str, str]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def compose(report: StageReport) -> str:
        started.set()
        await release.wait()
        return "这句不该发出去。"

    progress = _two_phase(clock, sent, compose)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)          # 到点，worker 开始生成
    await started.wait()
    await progress.close()            # 生成还没结束就收口
    release.set()
    await settle()

    assert sent == []
    assert progress.published == []
    assert await progress.note(StageReport(STAGE_MATERIALS, node="materials")) == STATUS_CLOSED


@pytest.mark.asyncio
async def test_publish_gate_blocks_a_phrase_that_finished_during_close():
    """发布闸：生成赶在关闸那一刻完成，也不许再落库。"""
    clock = FakeClock()
    sent: list[tuple[str, str]] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def compose(report: StageReport) -> str:
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # 模拟"其实已经生成好了，只是没来得及返回"：取消被吞掉，正常返回。
            await asyncio.sleep(0)
        return "生成好了但已经过时的一句。"

    progress = _two_phase(clock, sent, compose)
    progress.begin_query()
    await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(1.5)
    await started.wait()
    await progress.close()
    release.set()
    await settle()

    assert sent == [], "关闸之后生成出来的旧通知不许落库"


@pytest.mark.asyncio
async def test_note_does_not_wait_for_generation():
    """主任务不等待措辞：note() 立刻返回，生成在后台跑。"""
    clock = FakeClock()
    sent: list[tuple[str, str]] = []
    release = asyncio.Event()

    async def compose(report: StageReport) -> str:
        await release.wait()
        return "慢慢想出来的一句。"

    progress = _two_phase(clock, sent, compose)
    progress.begin_query()
    assert await progress.note(StageReport(STAGE_SEARCHING, node="first_query")) == STATUS_QUEUED
    await clock.advance(1.5)
    # 生成还卡着，主任务该干什么干什么 —— 这里只证明"没有等它"
    assert sent == []
    release.set()
    await settle()
    assert sent == [(STAGE_SEARCHING, "慢慢想出来的一句。")]
    await progress.close()


@pytest.mark.asyncio
async def test_new_turn_does_not_inherit_the_old_pending_state():
    clock = FakeClock()
    sent: list[str] = []
    board = ProgressBoard(clock=clock, sleeper=clock.sleep)

    async def emit(report: StageReport, text: str) -> None:
        sent.append(report.stage)

    turn_a = await board.begin("c1", turn_id="turn-a", emit=emit)
    turn_a.begin_query()
    await turn_a.note(StageReport(STAGE_SEARCHING, node="first_query"))

    turn_b = await board.begin("c1", turn_id="turn-b", emit=emit)
    await clock.advance(10.0)
    assert sent == [], "回合 A 的待发消息不许进入回合 B"
    assert turn_a.closed is True
    assert await turn_a.note(StageReport(STAGE_MATERIALS, node="materials")) == STATUS_CLOSED

    turn_b.begin_query()
    await turn_b.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(2.0)
    assert sent == [STAGE_SEARCHING]
    await board.close_all()


@pytest.mark.asyncio
async def test_conversations_are_isolated():
    clock = FakeClock()
    sent: list[str] = []
    board = ProgressBoard(clock=clock, sleeper=clock.sleep)

    async def emit(report: StageReport, text: str) -> None:
        sent.append(f"{report.node}:{report.stage}")

    left = await board.begin("owner|web|c1|", turn_id="a", emit=emit)
    right = await board.begin("owner|web|c2|", turn_id="b", emit=emit)
    left.begin_query()
    right.begin_query()
    await left.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await right.note(StageReport(STAGE_SEARCHING, node="first_query"))
    await clock.advance(2.0)
    assert sorted(sent) == ["first_query:searching", "first_query:searching"]
    assert len(sent) == 2
    # 另一边关掉不影响这一边
    await board.close("owner|web|c1|")
    assert board.current("owner|web|c1|") is None
    assert board.current("owner|web|c2|") is right
    await board.close_all()


@pytest.mark.asyncio
async def test_flush_reads_the_latest_state_not_the_state_at_wait_start():
    """等待开始时是"取得材料"，到期前变成"正在读原文"：发出去的是后者。"""
    clock = FakeClock()
    sent: list[str] = []
    progress = make_progress(clock, sent)
    progress.begin_query()

    await progress.note(StageReport(STAGE_MATERIALS, node="materials", has_more=True))
    await settle()
    assert clock.sleeping() == 1
    await clock.advance(0.2)
    await progress.note(StageReport(STAGE_READING, node="read:1", has_more=True))
    await clock.advance(1.4)
    assert sent == [STAGE_READING]


def test_conversation_identity_separates_scopes():
    one = Scope(owner_id="u1", channel="web", conversation_id="c1", module_id="")
    two = Scope(owner_id="u1", channel="web", conversation_id="c2", module_id="")
    assert conversation_identity(one) != conversation_identity(two)


def test_policy_rejects_negative_values():
    policy = ProgressPolicy(first_wait_seconds=-1.0, min_gap_seconds=-2.0, max_mid_notices=-3)
    assert policy.first_wait_seconds == 0.0
    assert policy.min_gap_seconds == 0.0
    assert policy.max_mid_notices == 0


# ----------------------------------------------------------------------
# 工具结果 → 阶段判断：只看结构，不认自由文本
# ----------------------------------------------------------------------
def _tool(status="ok", output=None):
    return ToolResult(status=status, output=output or {})


def test_search_materials_are_found_even_without_a_top_level_title():
    """成功结果只有嵌套材料列表、没有顶层标题：不能误报"没有找到"。"""
    assert search_outcome(_tool(output={"data": {"results": [{"link": "https://a"}]}})) == "materials"
    assert search_outcome(_tool(output={"items": [{"title": "标题"}]})) == "materials"
    assert search_outcome(_tool(output={"results": [{"snippet": "只有片段"}]})) == "materials"


def test_query_only_or_zero_rows_is_not_evidence():
    assert search_outcome(_tool(output={"query": "关键词"})) == "unknown"
    assert search_outcome(_tool(output={"query": "关键词", "results": []})) == "empty"


def test_failed_search_is_failed_not_empty_material():
    assert search_outcome(_tool(status="failed", output={"query": "x"})) == "failed"
    assert search_outcome(_tool(status="rejected", output={})) == "failed"


def test_platform_name_list_is_not_evidence_material():
    """web 搜索的 output["sources"] 是平台名，不是材料列表。"""
    assert search_outcome(_tool(output={"sources": ["bocha", "bing"]})) == "unknown"


def test_read_outcome_separates_started_from_finished():
    assert read_outcome(_tool(output={"fetch_status": "ok", "content": "正文"})) == "read"
    assert read_outcome(_tool(output={"fetch_status": "truncated", "content": "正文"})) == "read"
    assert read_outcome(_tool(output={"fetch_status": "not_found"})) == "failed"
    assert read_outcome(_tool(status="failed", output={"content": "正文"})) == "failed"
    # 状态成功但正文是空的：不许说"读完了"
    assert read_outcome(_tool(output={"fetch_status": "ok", "content": "   "})) == "unread"


def test_stage_for_results_prefers_read_then_materials_then_redirect():
    search_tools = {"search__web"}
    read_tools = {"search__read"}
    assert stage_for_results(
        [("search__web", _tool(output={"results": [{"url": "https://a"}]}))],
        search_tools=search_tools, read_tools=read_tools,
    ) == STAGE_MATERIALS
    assert stage_for_results(
        [
            ("search__web", _tool(output={"results": [{"url": "https://a"}]})),
            ("search__read", _tool(output={"content": "正文", "fetch_status": "ok"})),
        ],
        search_tools=search_tools, read_tools=read_tools,
    ) == STAGE_READ_DONE
    assert stage_for_results(
        [("search__web", _tool(status="failed"))],
        search_tools=search_tools, read_tools=read_tools,
    ) == STAGE_REDIRECT
    # 结构不认识：什么都不说，绝不冒充"取得材料"
    assert stage_for_results(
        [("search__web", _tool(output={"query": "x"}))],
        search_tools=search_tools, read_tools=read_tools,
    ) == ""
