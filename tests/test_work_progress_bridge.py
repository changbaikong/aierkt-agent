"""代码任务过程桥的行为测试。

原则与 tests/test_steward_progress.py 一致：用**可控时钟**跑，不真等 4 秒。
措辞已经改成模型现场生成（见 agent_core/steward.StagePhraseGenerator），
所以这里用 tests/support/stage_phrases.StagePhraseProvider 当假模型：
气泡里必须是它生成的那句话；模型失败 / 超时 / 复读时宁可不说，也不回退固定句。

这里验证的是"什么算可信阶段、什么时候不许说话、终态之后一句都不许再有"。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_core.code_task_lifecycle import CodeTaskLifecycle
from agent_core.contracts import Scope
from agent_core.steward import (
    STEWARD_PROGRESS_KIND,
    ChatStagePublisher,
    RecentPhraseMemory,
    StagePhraseGenerator,
    StewardContext,
    StewardNarrator,
    StewardReporter,
    template_line,
)
from agent_core.steward_progress import (
    STAGE_WORK_DELIVER,
    STAGE_WORK_EDIT,
    STAGE_WORK_INSPECT,
    STAGE_WORK_REPAIR,
    STAGE_WORK_VERIFY,
    STATUS_CLOSED,
    STATUS_QUEUED,
    STATUS_SKIPPED,
    StageReport,
    conversation_identity,
)
from agent_core.work_progress_bridge import (
    WorkProgressBridge,
    stage_for_event,
    stage_for_tool_kind,
)
from support.stage_phrases import StagePhraseProvider, stage_line

CHAT_MODULE = "companion"
WORK_MODULE = "work"


# ----------------------------------------------------------------------
# 可控时钟（与 test_steward_progress.FakeClock 同一套写法）
# ----------------------------------------------------------------------
class FakeClock:
    """可推进的时钟 + 配套 sleeper：advance() 让等待中的任务按时间醒过来。"""

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
    """把已经就绪的任务跑到下一个 await 点。

    桥的首次通知（0 秒门槛）也是这样落库的：note() 只负责排队，
    发送发生在一个独立任务里，所以断言前必须给它一次调度机会。
    """
    for _ in range(rounds):
        await asyncio.sleep(0)


# ----------------------------------------------------------------------
# 测试替身：只替换"落库"和"事件流"，其余走真实实现
# ----------------------------------------------------------------------
class Sink:
    """模拟对话仓库：stable_key 重复时返回 False（真实仓库就是这个语义）。"""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.messages: list[tuple[Scope, dict[str, Any]]] = []

    def __call__(self, scope: Scope, message: dict[str, Any], stable_key: str) -> bool:
        if stable_key in self.keys:
            return False
        self.keys.append(stable_key)
        self.messages.append((scope, dict(message)))
        return True

    @property
    def contents(self) -> list[str]:
        return [str(message.get("content") or "") for _scope, message in self.messages]


class FailingSink:
    """落库通道故障：桥必须自己吞掉，不能把异常传回任务，也不能发内部错误。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, scope: Scope, message: dict[str, Any], stable_key: str) -> bool:
        self.calls += 1
        raise RuntimeError("sink down")


class FakeHost:
    def __init__(self, ledger: Any = None) -> None:
        self.ledger = ledger


def make_reporter(sink: Any, provider: StagePhraseProvider | None = None) -> StewardReporter:
    """带模型的报告器。

    动态措辞必须有 provider：没有模型时生成器只回空串，气泡永远发不出来 ——
    所以测试替身必须挂在 narrator 上，而不是另塞一个句库。
    """
    return StewardReporter(
        StewardNarrator(provider or StagePhraseProvider()),
        sink=sink,
        event_bus=None,
    )


def make_bridge(
    clock: FakeClock,
    sink: Any,
    *,
    host: Any = None,
    provider: StagePhraseProvider | None = None,
    memory: RecentPhraseMemory | None = None,
) -> WorkProgressBridge:
    """装配桥。

    措辞生成器由发布器从 reporter 的 narrator 自己建（StagePhraseGenerator.from_narrator），
    这里只负责给一个假模型；``memory`` 可选，不传就由桥自己新建一份。
    """
    return WorkProgressBridge(
        host if host is not None else FakeHost(),
        make_reporter(sink, provider),
        memory=memory,
        clock=clock,
        sleeper=clock.sleep,
    )


def tool_event(
    kind: Any = "read",
    *,
    phase: str = "started",
    attempt_id: str = "attempt-1",
    title: str = "",
    work_item_id: str = "work-1",
    conversation_id: str = "chat",
    owner_id: str = "alice",
    no_scope: bool = False,
) -> dict[str, Any]:
    """Host 广播出来的工具事件（_attempt_meta 已把四元组补进信封）。"""
    event: dict[str, Any] = {
        "type": "provider_tool",
        "attempt_id": attempt_id,
        "work_item_id": work_item_id,
        "payload": {
            "tool_call_id": "t1",
            "title": title,
            "kind": kind,
            "status": "in_progress",
            "phase": phase,
        },
    }
    if no_scope:
        return event
    event.update(
        {
            "owner_id": owner_id,
            "channel": "web",
            "conversation_id": conversation_id,
            "module_id": WORK_MODULE,
            "objective": "把 app.py 里的 value 改成 2",
            "task_type": "code.agent",
        }
    )
    return event


def progress_event(
    phase: str,
    *,
    attempt_id: str = "attempt-1",
    no_scope: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "progress",
        "attempt_id": attempt_id,
        "work_item_id": "work-1",
        "payload": {"phase": phase, **extra},
    }
    if not no_scope:
        event.update(
            {
                "owner_id": "alice",
                "channel": "web",
                "conversation_id": "chat",
                "module_id": WORK_MODULE,
                "task_type": "code.agent",
            }
        )
    return event


def terminal_event(status: str, *, attempt_id: str = "attempt-1") -> dict[str, Any]:
    return {
        "type": status,
        "attempt_id": attempt_id,
        "work_item_id": "work-1",
        "owner_id": "alice",
        "channel": "web",
        "conversation_id": "chat",
        "module_id": WORK_MODULE,
        "task_type": "code.agent",
        "payload": {},
    }


def plan_ledger(plan_id: str = "plan-1") -> Any:
    """最小计划账本：只实现桥要用的 get_plan_unscoped（失败路径也靠它）。"""

    class Plan:
        scope = Scope("alice", "web", "chat", WORK_MODULE)
        objective = "把 app.py 里的 value 改成 2"

    class Ledger:
        def get_plan_unscoped(self, requested: str) -> Any:
            return Plan() if requested == plan_id else None

    return Ledger()


# ----------------------------------------------------------------------
# 纯函数：什么算可信阶段
# ----------------------------------------------------------------------
def test_read_search_fetch_are_inspect_and_edit_kinds_are_edit():
    assert stage_for_tool_kind("read") == STAGE_WORK_INSPECT
    assert stage_for_tool_kind("search") == STAGE_WORK_INSPECT
    assert stage_for_tool_kind("fetch") == STAGE_WORK_INSPECT
    assert stage_for_tool_kind("edit") == STAGE_WORK_EDIT
    assert stage_for_tool_kind("delete") == STAGE_WORK_EDIT
    assert stage_for_tool_kind("move") == STAGE_WORK_EDIT
    assert stage_for_tool_kind("READ") == STAGE_WORK_INSPECT


def test_unknown_kind_and_execute_are_never_guessed():
    """不认识的操作类型：什么都不说。任意执行命令不算验证。"""
    for kind in ("", None, "other", "execute", "think", "File: app.py", "运行 pytest -q"):
        assert stage_for_tool_kind(kind) == ""


def test_finished_tool_event_is_not_a_stage_signal():
    assert stage_for_event("provider_tool", {"phase": "finished", "kind": "read"}) == ""
    assert stage_for_event("provider_tool", {"phase": "started", "kind": "read"}) == STAGE_WORK_INSPECT


def test_verification_and_patch_events_map_to_their_stages():
    assert stage_for_event("progress", {"phase": "verification_started"}) == STAGE_WORK_VERIFY
    assert stage_for_event("progress", {"phase": "verification_finished"}) == ""
    assert stage_for_event("progress", {"phase": "patch_ready"}) == STAGE_WORK_DELIVER
    assert stage_for_event("progress", {"phase": "anything_else"}) == ""
    assert stage_for_event("provider_activity", {"phase": "started"}) == ""


# ----------------------------------------------------------------------
# 四个阶段：真实信号 → 一条气泡
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_read_tool_start_publishes_inspect():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    status = await bridge.handle_event(tool_event("read"))
    assert status == STATUS_QUEUED  # 先排队，发送在同一个 tick 内完成
    await settle()
    assert len(sink.messages) == 1
    scope, message = sink.messages[0]
    # 身份镜像 work_bridge：module 换回聊天模块，四元组其余部分不变
    assert (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id) == (
        "alice",
        "web",
        "chat",
        CHAT_MODULE,
    )
    assert message["kind"] == STEWARD_PROGRESS_KIND
    assert message["phase"] == STAGE_WORK_INSPECT
    line = sink.contents[0]
    # 气泡里就是模型这一阶段生成的那句话（没有固定句库可以"查表"）
    assert line == stage_line(STAGE_WORK_INSPECT)
    # 公开文字：不许出现路径、命令、工具输出、任务目标原文
    for forbidden in ("app.py", "pytest", "value", "Traceback", "/", "\\"):
        assert forbidden not in line


@pytest.mark.asyncio
async def test_edit_tool_start_publishes_edit():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(tool_event("edit")) == STATUS_QUEUED
    await settle()
    assert sink.messages[0][1]["phase"] == STAGE_WORK_EDIT


@pytest.mark.asyncio
async def test_unknown_kind_publishes_nothing():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    for kind in ("execute", "other", "", None):
        assert await bridge.handle_event(tool_event(kind)) == STATUS_SKIPPED
    assert sink.messages == []


@pytest.mark.asyncio
async def test_free_text_title_that_looks_like_a_command_publishes_nothing():
    """标题是自由文本：哪怕写着"运行 pytest"也不许当成验证/编辑。"""
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    for title in ("运行 pytest -q", "Edit app.py", "cat README.md"):
        assert await bridge.handle_event(tool_event("", title=title)) == STATUS_SKIPPED
    assert sink.messages == []


@pytest.mark.asyncio
async def test_verification_started_publishes_verify():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(progress_event("verification_started")) == STATUS_QUEUED
    await settle()
    assert sink.messages[0][1]["phase"] == STAGE_WORK_VERIFY


@pytest.mark.asyncio
async def test_patch_ready_publishes_deliver():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(progress_event("patch_ready", changed=2)) == STATUS_QUEUED
    await settle()
    assert sink.messages[0][1]["phase"] == STAGE_WORK_DELIVER


@pytest.mark.asyncio
async def test_verification_started_is_not_published_when_verification_is_not_run(tmp_path: Path):
    """没有验证配置时宿主一条事件都不发 —— 聊天侧也就没有"正在跑测试"。"""
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")

    from agent_core.workspace_snapshot import WorkspaceSnapshotService

    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = snapshots.create("attempt-1", project)
    events: list[Any] = []

    async def emit(event: Any) -> None:
        events.append(event)

    lifecycle = CodeTaskLifecycle(
        snapshot_service=snapshots,
        artifact_dir=tmp_path / "artifacts",
        verification_service=None,
    )
    payload, record = await lifecycle.verify_snapshot("attempt-1", snapshot, emit)

    assert payload["status"] == "not_run"
    assert record is None
    assert [event.payload.get("phase") for event in events] == []
    assert sink.messages == []
    # 桥对这条路由本身没有阶段：事件都不存在，自然没有"正在跑测试"的气泡
    assert await bridge.handle_event(progress_event("verification_started")) == STATUS_QUEUED
    await settle()
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_lifecycle_emits_verification_started_before_running_the_verifier(tmp_path: Path):
    """宿主真的启动验证时，先发开始事件；桥据此才敢说"我在跑测试"。"""
    from agent_core.code_agent_verification import VerificationRecord
    from agent_core.workspace_snapshot import WorkspaceSnapshotService

    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    snapshot = snapshots.create("attempt-1", project)
    events: list[Any] = []

    class Verifier:
        async def verify(self, attempt_id, snapshot):  # noqa: ANN001
            return VerificationRecord(
                status="verified",
                reason="ok",
                project_root=str(snapshot.project_root),
                before_fingerprint="a" * 64,
                after_fingerprint="a" * 64,
                commands=(),
            )

    async def emit(event: Any) -> None:
        events.append(event)

    lifecycle = CodeTaskLifecycle(
        snapshot_service=snapshots,
        artifact_dir=tmp_path / "artifacts",
        verification_service=Verifier(),
    )
    payload, record = await lifecycle.verify_snapshot("attempt-1", snapshot, emit)

    assert payload["status"] == "verified"
    assert record is not None
    assert [event.payload.get("phase") for event in events] == [
        "verification_started",
        "verification_finished",
    ]
    assert events[1].payload["verification_status"] == "verified"
    assert await bridge.handle_event(progress_event("verification_started")) == STATUS_QUEUED
    await settle()
    assert sink.messages[0][1]["phase"] == STAGE_WORK_VERIFY


# ----------------------------------------------------------------------
# 一个阶段只说一次 + 相邻至少 4 秒
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_same_stage_is_published_at_most_once():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()
    for _ in range(3):
        assert await bridge.handle_event(tool_event("search")) == STATUS_SKIPPED
    await clock.advance(10)
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_adjacent_notices_stay_four_seconds_apart():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()
    assert await bridge.handle_event(tool_event("edit")) == STATUS_QUEUED
    # edit 被排进等待：还没到 4 秒
    await clock.advance(1)
    assert len(sink.messages) == 1
    await clock.advance(3)
    assert len(sink.messages) == 2
    assert [message["phase"] for _scope, message in sink.messages] == [
        STAGE_WORK_INSPECT,
        STAGE_WORK_EDIT,
    ]


@pytest.mark.asyncio
async def test_four_stages_are_reported_once_each_with_four_second_gaps():
    """长任务的完整链：四个阶段各一条，不套用聊天的"两条中段"上限。"""
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    await bridge.handle_event(tool_event("read"))
    await settle()
    await bridge.handle_event(tool_event("edit"))
    await settle()
    await clock.advance(4)
    await bridge.handle_event(progress_event("verification_started"))
    await settle()
    await clock.advance(4)
    await bridge.handle_event(progress_event("patch_ready"))
    await clock.advance(4)
    await settle()

    assert [message["phase"] for _scope, message in sink.messages] == [
        STAGE_WORK_INSPECT,
        STAGE_WORK_EDIT,
        STAGE_WORK_VERIFY,
        STAGE_WORK_DELIVER,
    ]
    # 每条都是模型对**那个阶段**生成的那句话
    assert sink.contents == [
        stage_line(STAGE_WORK_INSPECT),
        stage_line(STAGE_WORK_EDIT),
        stage_line(STAGE_WORK_VERIFY),
        stage_line(STAGE_WORK_DELIVER),
    ]


@pytest.mark.asyncio
async def test_fast_task_merges_instead_of_spamming():
    """一个窗口内跑完四个阶段：只发最新那条，不刷屏。"""
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    await bridge.handle_event(tool_event("read"))
    await settle()
    await bridge.handle_event(tool_event("edit"))
    await bridge.handle_event(progress_event("verification_started"))
    await bridge.handle_event(progress_event("patch_ready"))
    # 第一次已经发出去（首版 0 秒门槛），后面三条只留最新一条
    assert len(sink.messages) == 1
    await clock.advance(4)
    assert len(sink.messages) == 2
    assert sink.messages[1][1]["phase"] == STAGE_WORK_DELIVER


@pytest.mark.asyncio
async def test_two_attempts_in_one_conversation_do_not_cancel_each_other():
    """同一会话里两个并行任务：各自按自己的节奏报阶段，互不吞掉对方的待发消息。

    措辞是**按会话**去重的（同一段对话里不复读），所以这里给四个任务各一句不同的话；
    本用例只看"谁发了哪一条阶段"，不看说了什么。
    """
    clock, sink = FakeClock(), Sink()
    provider = StagePhraseProvider(
        replies=["我先看项目里的文件", "我开始改这几处代码", "另一条线也在查资料", "另一条线开始动代码"]
    )
    bridge = make_bridge(clock, sink, provider=provider)

    await bridge.handle_event(tool_event("read", attempt_id="attempt-1", work_item_id="work-1"))
    await settle()
    await bridge.handle_event(tool_event("edit", attempt_id="attempt-1", work_item_id="work-1"))
    await clock.advance(4)
    await bridge.handle_event(tool_event("read", attempt_id="attempt-2", work_item_id="work-2"))
    await settle()
    await bridge.handle_event(tool_event("edit", attempt_id="attempt-2", work_item_id="work-2"))
    await clock.advance(4)
    await settle()

    published = [(message["run_id"], message["phase"]) for _scope, message in sink.messages]
    # 每个 attempt 各拿到"查看"和"修改"两条：会话共用桶会把其中一条挤掉
    assert published == [
        ("attempt-1", STAGE_WORK_INSPECT),
        ("attempt-1", STAGE_WORK_EDIT),
        ("attempt-2", STAGE_WORK_INSPECT),
        ("attempt-2", STAGE_WORK_EDIT),
    ]
    assert sink.contents == [
        "我先看项目里的文件",
        "我开始改这几处代码",
        "另一条线也在查资料",
        "另一条线开始动代码",
    ]


# ----------------------------------------------------------------------
# 动态措辞：话由模型现场生成，拿不到就不说
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_stage_bubble_is_written_by_the_model():
    """一次阶段事件 = 一次措辞调用，气泡里就是模型生成的那句（不是句库里的固定句）。"""
    clock, sink = FakeClock(), Sink()
    provider = StagePhraseProvider()
    bridge = make_bridge(clock, sink, provider=provider)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()

    assert sink.contents == [stage_line(STAGE_WORK_INSPECT)]
    # 一次阶段只说一次，也只问模型一次
    assert provider.stage_prompts_seen() == [STAGE_WORK_INSPECT]
    # 喂给模型的是安全事实：没有路径、命令、工具名、任务正文、执行身份
    asked = " ".join(str(message.get("content") or "") for message in provider.stage_calls[0])
    for forbidden in ("app.py", "pytest", "value", "read", "attempt-1", "provider_tool", "Traceback"):
        assert forbidden not in asked


@pytest.mark.asyncio
async def test_model_failure_publishes_nothing_but_still_consumes_the_stage():
    """模型报错 → 这条静默跳过（不回退固定句），但阶段节点照样算用掉了。"""
    clock, sink = FakeClock(), Sink()
    provider = StagePhraseProvider(fail=True)
    bridge = make_bridge(clock, sink, provider=provider)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()

    # 措辞层手里确实有一句兜底模板可用；它没有被拿来补位，也没有别的气泡。
    fallback = template_line(StewardContext(phase=STAGE_WORK_INSPECT))
    assert fallback and fallback not in sink.contents
    assert sink.messages == []

    # 节点已经消耗：同一次尝试的同一阶段再来，既不发也不再占用模型
    calls = len(provider.stage_calls)
    assert calls
    assert await bridge.handle_event(tool_event("read")) == STATUS_SKIPPED
    assert await bridge.handle_event(tool_event("search")) == STATUS_SKIPPED
    await clock.advance(30)
    assert sink.messages == []
    assert len(provider.stage_calls) == calls


@pytest.mark.asyncio
async def test_stage_phrase_timeout_is_skipped_silently():
    """模型超时 → 这句话就不说（不回退固定句）；排队和任务都不受影响。"""
    clock, sink = FakeClock(), Sink()
    loop = asyncio.get_running_loop()
    # 桥用的模型慢到测试结束都回不来：用它证明"等模型"不发生在主路径上。
    bridge = make_bridge(clock, sink, provider=StagePhraseProvider(delay=5.0))

    started = loop.time()
    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    assert loop.time() - started < 0.5  # 排队立刻返回，没有等模型
    await settle()
    assert sink.messages == []  # 生成还没回来，就没有气泡

    # "超时即静默跳过"由发布器这一层直接证明：0.01 秒超时（实现有 0.1 秒下限）
    # 远小于模型的 1 秒 → compose 回空串，调用方据此跳过整条（generator 覆盖默认模型）。
    publisher = ChatStagePublisher(
        make_reporter(sink),
        scope=Scope("alice", "web", "chat", CHAT_MODULE),
        generator=StagePhraseGenerator(StagePhraseProvider(delay=1.0), timeout_seconds=0.01),
    )
    report = StageReport(STAGE_WORK_INSPECT, node="work_inspect")
    text = await publisher.compose(report)
    assert text == ""
    assert await publisher.publish(report, text) == ""
    assert sink.messages == []

    # 任务不受影响：桥照常收下新阶段、照常收口；两个慢生成都不会补发。
    assert await bridge.handle_event(tool_event("edit", attempt_id="attempt-2")) == STATUS_QUEUED
    assert await bridge.close_attempt("attempt-1") == STATUS_CLOSED
    assert await bridge.close_attempt("attempt-2") == STATUS_CLOSED
    await settle()
    assert sink.messages == []


@pytest.mark.asyncio
async def test_wording_too_close_to_recent_memory_is_skipped():
    """模型又吐最近说过的那句：两次尝试都被判复读 → 这条不说，也不换别的句子顶上。"""
    clock, sink = FakeClock(), Sink()
    scope = Scope("alice", "web", "chat", CHAT_MODULE)
    memory = RecentPhraseMemory()
    memory.remember(conversation_identity(scope), "同一句")
    provider = StagePhraseProvider(replies=["同一句", "同一句"])
    bridge = make_bridge(clock, sink, provider=provider, memory=memory)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()

    assert provider.stage_calls  # 确实问了模型，只是它两次都给同一句
    assert sink.messages == []
    # 阶段节点照样被消耗：同一次尝试的同一阶段再来也不发
    assert await bridge.handle_event(tool_event("read")) == STATUS_SKIPPED
    await clock.advance(30)
    assert sink.messages == []


@pytest.mark.asyncio
async def test_wording_regenerated_after_a_duplicate_is_published():
    """第一次复读被拒、换了一句说法 → 这句发出去；下一阶段也不会复读它。"""
    clock, sink = FakeClock(), Sink()
    scope = Scope("alice", "web", "chat", CHAT_MODULE)
    memory = RecentPhraseMemory()
    memory.remember(conversation_identity(scope), "同一句")
    provider = StagePhraseProvider(replies=["同一句", "换一句说法了"])
    bridge = make_bridge(clock, sink, provider=provider, memory=memory)

    await bridge.handle_event(tool_event("read"))
    await settle()
    assert sink.contents == ["换一句说法了"]  # 采纳的是换过说法的那次
    # 发出去之后才进"最近说过的话"，成为下一阶段的生成输入
    assert memory.recent(conversation_identity(scope))[-1] == "换一句说法了"

    await bridge.handle_event(tool_event("edit"))
    await clock.advance(4)
    # replies 用完了，这一阶段回到它自己那句测试话术；关键是它不等于上一句
    assert sink.contents == ["换一句说法了", stage_line(STAGE_WORK_EDIT)]
    assert sink.contents[0] != sink.contents[1]  # 同一会话里不重复上一句


# ----------------------------------------------------------------------
# 终态 / 审批收口
# ----------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["waiting_approval", "completed", "failed", "cancelled", "interrupted"])
async def test_terminal_or_approval_closes_the_lifecycle(status: str):
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    await bridge.handle_event(tool_event("read"))
    await settle()
    await bridge.handle_event(tool_event("edit"))  # 还在等待窗口里
    assert await bridge.handle_event(terminal_event(status)) == STATUS_CLOSED
    await clock.advance(30)

    # 待发的那条被取消；终态本身不由过程桥发（回执是 work_bridge 的事）
    assert [message["phase"] for _scope, message in sink.messages] == [STAGE_WORK_INSPECT]
    # 收口之后再来什么阶段都不发
    for event in (tool_event("edit"), progress_event("patch_ready"), progress_event("verification_started")):
        assert await bridge.handle_event(event) == STATUS_CLOSED
    await clock.advance(30)
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_approval_then_a_new_attempt_publishes_again():
    """批准之后是新的执行尝试：跟着新生命周期继续报阶段。"""
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    await bridge.handle_event(tool_event("edit", attempt_id="attempt-1"))
    await settle()
    await bridge.handle_event(terminal_event("waiting_approval", attempt_id="attempt-1"))
    assert await bridge.handle_event(tool_event("edit", attempt_id="attempt-2")) == STATUS_QUEUED
    await settle()
    assert sink.messages[-1][1]["phase"] == STAGE_WORK_EDIT


@pytest.mark.asyncio
async def test_new_attempt_does_not_inherit_the_old_pending_notice():
    """新尝试开始时旧尝试还没发出去的那条作废，不许串到新尝试头上。"""
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    await bridge.handle_event(tool_event("read", attempt_id="attempt-1"))
    await settle()
    await bridge.handle_event(tool_event("edit", attempt_id="attempt-1"))  # 待发
    await bridge.handle_event(tool_event("edit", attempt_id="attempt-2"))  # 新尝试
    await settle()
    await clock.advance(30)

    assert [message["phase"] for _scope, message in sink.messages] == [
        STAGE_WORK_INSPECT,
        STAGE_WORK_EDIT,
    ]
    assert sink.messages[-1][1]["run_id"] == "attempt-2"


@pytest.mark.asyncio
async def test_close_attempt_is_a_deterministic_hook_for_the_terminal_path():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    await bridge.handle_event(tool_event("read", attempt_id="attempt-1"))
    await settle()
    await bridge.handle_event(tool_event("edit", attempt_id="attempt-1"))
    assert await bridge.close_attempt("attempt-1") == STATUS_CLOSED
    await clock.advance(30)

    assert [message["phase"] for _scope, message in sink.messages] == [STAGE_WORK_INSPECT]
    assert await bridge.handle_event(tool_event("edit", attempt_id="attempt-1")) == STATUS_CLOSED


@pytest.mark.asyncio
async def test_terminal_event_cancels_a_generation_in_flight():
    """生成还没回来就收到终态：这句直接掐掉，终态路径绝不等模型。"""
    clock, sink = FakeClock(), Sink()
    loop = asyncio.get_running_loop()
    provider = StagePhraseProvider(delay=0.5)
    bridge = make_bridge(clock, sink, provider=provider)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()
    assert provider.stage_calls  # 生成已经开始（模型还在慢慢想）
    assert sink.messages == []
    generating = len(asyncio.all_tasks())
    assert generating >= 2  # 除了测试自己，还挂着那条没回来的生成

    started = loop.time()
    closed = await asyncio.wait_for(bridge.handle_event(terminal_event("completed")), timeout=0.25)
    assert closed == STATUS_CLOSED
    # 模型还要 0.5 秒才回话，收口却是立刻返回的：主任务没有等措辞。
    assert loop.time() - started < 0.25
    # 没生成完的那条任务在关闸时就没了，不是留在后台慢慢跑
    assert len(asyncio.all_tasks()) < generating

    # 等到那句话本该生成完的时刻：它已经被取消，终态之后一句都不会冒出来。
    await asyncio.sleep(0.6)
    assert sink.messages == []

    # 收口之后同一次尝试的阶段事件也不会补发
    assert await bridge.handle_event(tool_event("read")) == STATUS_CLOSED
    assert await bridge.close_attempt("attempt-1") == STATUS_CLOSED
    await settle()
    assert sink.messages == []


@pytest.mark.asyncio
async def test_close_is_idempotent_and_survives_unknown_attempt():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.close_attempt("never-seen") == STATUS_CLOSED
    assert await bridge.close_attempt("") == STATUS_CLOSED


@pytest.mark.asyncio
async def test_build_helper_needs_both_a_stream_and_a_reporter():
    """装配辅助：缺事件源或缺发布通道时不建桥（调用方据此跳过启动任务）。"""
    from agent_core.work_progress_bridge import build_work_progress_bridge

    class Host:
        async def events(self):
            yield {}

    assert build_work_progress_bridge(None, make_reporter(Sink())) is None
    assert build_work_progress_bridge(Host(), None) is None
    assert build_work_progress_bridge(object(), make_reporter(Sink())) is None
    assert build_work_progress_bridge(Host(), make_reporter(Sink())) is not None


# ----------------------------------------------------------------------
# 重放 / 重启 / 身份解析不到
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_replayed_events_do_not_publish_twice():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    event = tool_event("read")
    assert await bridge.handle_event(event) == STATUS_QUEUED
    await settle()
    assert await bridge.handle_event(event) == STATUS_SKIPPED
    await clock.advance(30)
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_restart_does_not_replay_old_progress():
    """新桥实例：内存记录为空，只认新到的事件；重放靠落库去重键兜住。"""
    clock, sink = FakeClock(), Sink()
    first = make_bridge(clock, sink)
    assert await first.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()

    restarted = make_bridge(clock, sink)
    # 进程内没有记录，也不会去重播历史 —— 只有新事件才会被处理
    assert await restarted.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()
    # 但落库键是稳定的：同一条不会出现第二个气泡
    assert len(sink.messages) == 1


@pytest.mark.asyncio
async def test_unresolvable_conversation_writes_nothing():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(tool_event("read", no_scope=True)) == STATUS_SKIPPED
    assert await bridge.handle_event(progress_event("patch_ready", no_scope=True)) == STATUS_SKIPPED
    assert sink.messages == []


@pytest.mark.asyncio
async def test_missing_attempt_id_writes_nothing():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink)

    event = tool_event("read")
    event["attempt_id"] = ""
    assert await bridge.handle_event(event) == STATUS_SKIPPED
    assert sink.messages == []


# ----------------------------------------------------------------------
# 失败路径：桥不能连累任务
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sink_failure_does_not_break_the_bridge_or_leak_an_error_bubble():
    clock = FakeClock()
    sink = FailingSink()
    bridge = make_bridge(clock, sink)

    assert await bridge.handle_event(tool_event("read")) == STATUS_QUEUED
    await settle()
    await bridge.handle_event(tool_event("edit"))
    await clock.advance(10)

    assert sink.calls >= 1
    # 落库失败没有被写成气泡，桥还活着，还能继续处理事件
    assert await bridge.handle_event(terminal_event("completed")) == STATUS_CLOSED
    assert await bridge.handle_event(tool_event("read", attempt_id="attempt-2")) == STATUS_QUEUED
    await settle()
    assert sink.calls >= 2


@pytest.mark.asyncio
async def test_broken_plan_lookup_is_not_fatal():
    class BrokenLedger:
        def get_plan_unscoped(self, plan_id: str) -> Any:
            raise RuntimeError("ledger down")

    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink, host=FakeHost(BrokenLedger()))

    plan_event = {
        "type": "step.retrying",
        "plan_id": "plan-1",
        "step_id": "step-1",
        "work_item_id": "work-1",
        "attempt_id": "attempt-2",
        "payload": {"error": "boom", "attempts": 1, "max_attempts": 3},
    }
    assert await bridge.handle_event(plan_event) == STATUS_SKIPPED
    assert sink.messages == []


# ----------------------------------------------------------------------
# 修复阶段：只在"真的失败过又换了一次尝试"时说
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step_retrying_publishes_repair_for_the_new_attempt():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink, host=FakeHost(plan_ledger()))

    plan_event = {
        "type": "step.retrying",
        "plan_id": "plan-1",
        "step_id": "step-1",
        "work_item_id": "work-1",
        "attempt_id": "attempt-2",
        "payload": {"error": "boom", "attempts": 1, "max_attempts": 3},
    }
    assert await bridge.handle_event(plan_event) == STATUS_QUEUED
    await settle()
    scope, message = sink.messages[0]
    assert message["phase"] == STAGE_WORK_REPAIR
    assert message["run_id"] == "attempt-2"
    assert scope.module_id == CHAT_MODULE


@pytest.mark.asyncio
async def test_other_plan_events_never_publish_a_stage():
    clock, sink = FakeClock(), Sink()
    bridge = make_bridge(clock, sink, host=FakeHost(plan_ledger()))

    for event_type in ("step.failed", "step.started", "plan.started", "step.unknown_result"):
        event = {
            "type": event_type,
            "plan_id": "plan-1",
            "step_id": "step-1",
            "work_item_id": "work-1",
            "attempt_id": "attempt-1",
            "payload": {},
        }
        assert await bridge.handle_event(event) == STATUS_SKIPPED
    assert sink.messages == []


# ----------------------------------------------------------------------
# 订阅：桥只处理自己那条流，不抢结果桥的事件
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_run_consumes_the_host_stream_and_survives_bad_events():
    clock, sink = FakeClock(), Sink()

    class Host:
        def __init__(self) -> None:
            self.queue: asyncio.Queue[Any] = asyncio.Queue()
            self.subscriptions = 0

        async def events(self):
            self.subscriptions += 1
            while True:
                yield await self.queue.get()

    host = Host()
    bridge = make_bridge(clock, sink, host=host)
    task = asyncio.create_task(bridge.run())
    await settle(2)
    assert host.subscriptions == 1

    await host.queue.put("not-a-mapping")  # 非映射：直接跳过，不能弄死桥
    await host.queue.put({"type": "provider_tool", "payload": "boom"})  # 坏载荷：只记日志
    await host.queue.put(tool_event("read"))
    await settle()
    assert len(sink.messages) == 1

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ----------------------------------------------------------------------
# 执行层：完成事件必须带上从同一条工具记录继承的 kind
# ----------------------------------------------------------------------
def _harness_run(emit: Any) -> Any:
    """最小可用的执行运行态：只喂 _handle_tool_call* 真正会读的字段。

    不启 ACP 子进程：这两条回调是纯事件投影，直接调用比拉起 mock agent 更稳。
    """
    from agent_core.deepseek_harness_provider import _HarnessRun
    from agent_core.work_provider import WorkRequest

    request = WorkRequest(
        work_item_id="work-1",
        attempt_id="attempt-1",
        scope=Scope("alice", "web", "chat", WORK_MODULE),
        objective="看看这个项目",
        task_type="code.agent",
        provider_id="deepseek_harness",
    )
    return _HarnessRun(
        attempt_id="attempt-1",
        request=request,
        objective=request.objective,
        snapshot=SimpleNamespace(workspace_root="."),
        lease=SimpleNamespace(),
        activity=SimpleNamespace(status="idle"),
        emit=emit,
    )


@pytest.mark.asyncio
async def test_finished_tool_event_inherits_the_structured_kind():
    """更新事件本身不带 kind：从同一次工具记录继承，绝不去解析自由文本。"""
    from agent_core.deepseek_harness_provider import DeepSeekHarnessProvider
    from agent_core.harness_config import HarnessConfig

    events: list[Any] = []

    async def emit(event: Any) -> None:
        events.append(event)

    run = _harness_run(emit)
    provider = DeepSeekHarnessProvider.__new__(DeepSeekHarnessProvider)
    # 只需要"事件投影 + 配置上限"这条链路：不探测沙箱、不起 ACP 会话。
    provider.config = HarnessConfig(enabled=False)
    provider.lifecycle = CodeTaskLifecycle(
        snapshot_service=None, artifact_dir="."
    )
    provider._publish_activity = _noop_publish_activity  # type: ignore[method-assign]

    await provider._handle_tool_call(
        run,
        {
            "toolCallId": "t1",
            "title": "运行 pytest -q",  # 自由文本标题：谁都不许解析它
            "kind": "read",
            "status": "in_progress",
        },
    )
    await provider._handle_tool_call_update(
        run,
        {
            "toolCallId": "t1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
        },
    )

    payloads = [dict(event.payload) for event in events]
    assert [payload["phase"] for payload in payloads] == ["started", "finished"]
    # 完成事件的载荷里一个结构化 kind 都没有 —— 只能从同一条记录继承
    assert {payload["kind"] for payload in payloads} == {"read"}
    # 既有字段一个都没动
    assert {payload["title"] for payload in payloads} == {"运行 pytest -q"}
    assert payloads[1]["status"] == "completed"
    assert payloads[1]["summary"] == "ok"
    assert payloads[1]["tool_call_id"] == "t1"
    # 未知工具（没有记录）不会凭空造出一个 kind
    await provider._handle_tool_call_update(run, {"toolCallId": "t9", "status": "failed"})
    assert dict(events[-1].payload)["kind"] == ""


async def _noop_publish_activity(*args: Any, **kwargs: Any) -> None:
    return None


# ----------------------------------------------------------------------
# 终态收口顺序：先关进度，再写回执
# ----------------------------------------------------------------------
class _OrderRecordingRepository:
    """只记"什么时候写了一条终态回执"。"""

    def __init__(self, order: list[str]) -> None:
        self.order = order

    def append_message(self, scope, message, stable_key="", **kwargs):
        self.order.append("receipt")
        return True


@pytest.mark.asyncio
async def test_terminal_receipt_closes_the_progress_before_writing():
    """结果桥写终态回执之前，先通过 progress_hook 把过程进度收口。"""
    from agent_core.work_bridge import WorkResultBridge

    order: list[str] = []
    clock = FakeClock()
    sink = Sink()
    progress_bridge = make_bridge(clock, sink)

    async def hook(attempt_id: str) -> None:
        order.append("close-progress")
        await progress_bridge.close_attempt(attempt_id)

    result_bridge = WorkResultBridge(
        FakeHost(), _OrderRecordingRepository(order), None, progress_hook=hook,
    )
    # 第一条（查看项目）已经发出去，第二条（修改代码）还在 4 秒间隔里排队。
    await progress_bridge.handle_event(tool_event("read"))
    await settle()
    await progress_bridge.handle_event(tool_event("edit"))
    assert len(sink.messages) == 1

    await result_bridge._handle({
        "type": "completed",
        "attempt_id": "attempt-1",
        "work_item_id": "work-1",
        "owner_id": "alice",
        "channel": "web",
        "conversation_id": "chat",
        "module_id": WORK_MODULE,
        "task_type": "code.agent",
        "objective": "把 app.py 里的 value 改成 2",
        "payload": {"output": {"summary": "改好了", "patch": "diff --git a/app.py b/app.py"}},
    })

    assert order == ["close-progress", "receipt"], order
    # 待发的那条"修改代码"就此作废：回执之后不会再冒进度
    await clock.advance(30.0)
    assert len(sink.messages) == 1
