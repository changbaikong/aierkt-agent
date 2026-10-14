"""代码任务过程桥：把执行层的**真实阶段事件**变成发起会话里的一句进度。

【它解决什么】
代码代理的工具循环跑在独立执行进程里，聊天侧此前只有两句话：
"已经交给代码代理"（接手）和最终回执（完成 / 失败 / 等待确认）。中间那几分钟
完全沉默 —— 用户既不知道它在看文件、在改代码，还是在跑测试。

本模块订阅 ``host.events()``（与 :class:`~agent_core.work_bridge.WorkResultBridge`
各持一个队列，互不抢事件），只认**结构化事实**，产出一句公开人话：

    provider_tool(kind=read/search/fetch, phase=started) → 查看项目
    provider_tool(kind=edit/delete/move, phase=started)   → 修改代码
    progress(phase=verification_started)                  → 验证改动
    progress(phase=patch_ready)                           → 准备交付
    step.retrying（失败之后的新一次尝试）                  → 修复

【三条纪律】
1. **不猜**：只看结构化字段。工具的自由文本标题（"运行 pytest -q"）永远不解析；
   没有``kind``就什么都不说。终态回执由 work_bridge 负责，这里一句都不写。
2. **不刷屏**：生命周期挂在"任务 + 执行尝试"上，不跟聊天回合走。四个阶段各最多
   一次（同一 stage 的稳定节点身份天然去重），相邻两条至少隔 4 秒，缺失阶段直接跳过，
   快速完成的合并成一条。长代码任务不套用聊天的"最多两条中段"上限。
3. **先关闸再写回执**：见到 waiting_approval / completed / failed / cancelled /
   interrupted 就立刻关闭该尝试的进度，不等回执落库 —— 这样"过程句"永远排在
   "最终回执"前面。收口顺序是**先关进度、再写回执**；回执本身由
   :class:`~agent_core.work_bridge.WorkResultBridge` 写。任务身份解析不到时
   宁可什么都不发，也不写进猜测的会话。

【重启不重播】
进程内记着已经发过的 (attempt_id, 节点)，另外每个节点的落库去重键是稳定的
（attempt_id + 阶段节点 + 阶段），重启后即使同一事件再来也不会出现第二条气泡。
本模块不持有任何需要恢复的状态。

【为什么不用 ProgressBoard.begin() 开回合】
``ProgressBoard`` 按**会话**分桶，begin() 会把该会话的旧回合关掉。同一会话里
两个任务并行时，B 的第一条阶段会把 A 还没发出去的那条关没，节奏也变成按会话
共用。这里改成按 attempt_id 分桶、另存一张"会话 → 当前尝试"的索引：
作用一样（新尝试不继承旧尝试的待发消息），但并行任务互不干扰。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Mapping

from .contracts import Scope
from .steward import ChatStagePublisher, RecentPhraseMemory, StewardReporter
from .steward_progress import (
    STAGE_FACTS,
    STAGE_NODES,
    STAGE_WORK_DELIVER,
    STAGE_WORK_EDIT,
    STAGE_WORK_INSPECT,
    STAGE_WORK_REPAIR,
    STAGE_WORK_VERIFY,
    STATUS_CLOSED,
    STATUS_SENT,
    STATUS_SKIPPED,
    ProgressPolicy,
    StageReport,
    TurnProgress,
    conversation_identity,
)

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 事件取值
# ----------------------------------------------------------------------
#: 执行层的工具活动事件。``phase`` 有两种取值，**只有 started 是阶段信号**：
#: finished 只说明工具跑完了，不能据此宣布"找到了原因"或"测试通过了"。
EVENT_PROVIDER_TOOL = "provider_tool"
PHASE_TOOL_STARTED = "started"

#: 代码任务生命周期里的通用进展事件（见 code_task_lifecycle.py）。
EVENT_PROGRESS = "progress"
#: 宿主真的开始调用验证器之前发的事件。"任意执行命令不算验证" ——
#: 只有这个事件能触发"验证改动"。
PHASE_VERIFICATION_STARTED = "verification_started"
#: 补丁确实生成之后发的事件。
PHASE_PATCH_READY = "patch_ready"

#: 计划调度器的"失败之后换一次新尝试"。只有它算"修复"，
#: 用户主动重试（同样带 previous_error）不算"我们刚发现问题又改了一版"。
EVENT_STEP_RETRYING = "step.retrying"

#: 见到就要立刻关闭进度的状态。waiting_approval 也在内：审批回执自己会说话，
#: 普通进度必须停下来；批准之后跟着新的执行尝试重新开始。
CLOSE_EVENT_TYPES = frozenset(
    {"waiting_approval", "completed", "failed", "cancelled", "interrupted"}
)

#: ACP 的结构化操作类型 → 阶段。
#: 取值来自 ACP 的 ToolKind 枚举，是协议字段，不是工具标题里的自由文本。
_INSPECT_KINDS = frozenset({"read", "search", "fetch"})
_EDIT_KINDS = frozenset({"edit", "delete", "move"})

#: 触发阶段时用什么节点身份发布。与 steward_progress.STAGE_NODES 同源：
#: 同一阶段在同一个生命周期里只说一次，靠的就是这个稳定名字。
_STAGE_NODE = STAGE_NODES


def stage_for_tool_kind(kind: Any) -> str:
    """结构化操作类型 → 阶段；不认识就返回空串（**不猜**）。

    只认 ACP 的 ``kind``：``read/search/fetch`` 是看，``edit/delete/move`` 是改。
    ``execute``（任意执行命令）刻意不映射 —— 跑一条命令不等于跑测试。
    """
    value = str(kind or "").strip().casefold()
    if value in _INSPECT_KINDS:
        return STAGE_WORK_INSPECT
    if value in _EDIT_KINDS:
        return STAGE_WORK_EDIT
    return ""


def stage_for_event(event_type: str, payload: Mapping[str, Any]) -> str:
    """一个广播事件对应哪个阶段；没有可信阶段就返回空串。

    纯函数，方便单测；**不做任何自由文本解析**。
    """
    if event_type == EVENT_PROVIDER_TOOL:
        if str(payload.get("phase") or "") != PHASE_TOOL_STARTED:
            return ""
        return stage_for_tool_kind(payload.get("kind"))
    if event_type == EVENT_STEP_RETRYING:
        # 计划调度器只在"这一步真的失败过、现在换一次新尝试"时才发它。
        return STAGE_WORK_REPAIR
    if event_type == EVENT_PROGRESS:
        phase = str(payload.get("phase") or "")
        if phase == PHASE_VERIFICATION_STARTED:
            return STAGE_WORK_VERIFY
        if phase == PHASE_PATCH_READY:
            return STAGE_WORK_DELIVER
    return ""


class WorkProgressBridge:
    """把代码任务的过程事件投影成发起会话里的阶段消息。

    用法（与结果桥并列，各自独立订阅同一份事件流）::

        bridge = WorkProgressBridge(host, reporter, chat_module_id="companion")
        task = asyncio.create_task(bridge.run(), name="work-progress-bridge")
        ...
        task.cancel()  # 关停

    终态路径也可以在写回执之前显式调用 :meth:`close_attempt`，
    把"先关进度、再写回执"的顺序固定下来。
    """

    def __init__(
        self,
        host: Any,
        reporter: StewardReporter | None = None,
        *,
        chat_module_id: str = "companion",
        memory: RecentPhraseMemory | None = None,
        policy: ProgressPolicy | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        max_remembered: int = 512,
    ) -> None:
        self.host = host
        self.reporter = reporter
        self.chat_module_id = str(chat_module_id or "companion")
        #: 同一会话最近几句已发过程话术：作为措辞输入，避免复读。
        self.memory = memory or RecentPhraseMemory()
        #: 长代码任务：第一次阶段立即说，之后相邻两条至少隔 4 秒；
        #: 四个阶段各最多一条，再留一点余量给"修复"。
        self.policy = policy or ProgressPolicy(
            first_wait_seconds=0.0, min_gap_seconds=4.0, max_mid_notices=4
        )
        self._clock = clock
        self._sleeper = sleeper
        self._max_remembered = max(8, int(max_remembered))
        #: attempt_id → 该次执行的进度控制器；值只在本进程内有效。
        self._progress: dict[str, TurnProgress] = {}
        #: conversation_key → attempt_id：用于"新尝试不许继承旧尝试待发消息"。
        self._by_conversation: dict[str, str] = {}
        #: 已经发过的 (attempt_id, 节点)。重启后为空 —— 只认新到的事件。
        self._published: set[tuple[str, str]] = set()
        self._published_order: list[tuple[str, str]] = []
        #: 已经收口的 attempt_id。终态之后同一次尝试再来的事件一律丢弃：
        #: 任务重试 / 审批续跑都会**新建** attempt_id，所以关掉旧的不影响新的一次。
        self._closed: set[str] = set()
        self._closed_order: list[str] = []

    # ── 订阅 ────────────────────────────────────────────────────────
    async def run(self) -> None:
        """订阅 Host 事件流直到被取消。单条事件失败不终止桥。"""
        async for event in self.host.events():
            if not isinstance(event, Mapping):
                continue
            try:
                await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 进度是旁路：任何失败都只记日志，绝不影响任务本身。
                logger.exception("work progress bridge failed on event: %r", event)

    async def close(self) -> None:
        """关停：把还活着的进度控制器全部收口（不再发任何待发消息）。"""
        for attempt_id in list(self._progress):
            await self._close_attempt(attempt_id)

    # ── 主入口 ──────────────────────────────────────────────────────
    async def handle_event(self, event: Mapping[str, Any]) -> str:
        """处理一条广播事件，返回 :data:`~agent_core.steward_progress.STATUS_*`。

        单条事件的成败都不抛出（除非被取消）：桥挂了不能连累任务。
        """
        event_type = str(event.get("type") or "")
        payload = dict(event.get("payload") or {})
        if event_type in CLOSE_EVENT_TYPES:
            return await self._close_attempt(self._attempt_id(event))
        stage = stage_for_event(event_type, payload)
        if not stage:
            return STATUS_SKIPPED
        return await self._publish_stage(event, payload, stage)

    async def close_attempt(self, attempt_id: str) -> str:
        """终态路径的确定性钩子：**先关进度，再写最终回执**。

        写回执的人（work_bridge）拿到终态事件时先调它，就能保证过程句
        永远排在回执前面；事件流本身也会触发同一段收口（幂等）。
        """
        return await self._close_attempt(str(attempt_id or ""))

    # ── 身份 ────────────────────────────────────────────────────────
    @staticmethod
    def _attempt_id(event: Mapping[str, Any]) -> str:
        return str(event.get("attempt_id") or "")

    def _chat_scope(self, event: Mapping[str, Any]) -> Scope | None:
        """事件 → 发起它的**聊天** scope；解析不到就返回 None。

        与 work_bridge 完全同一套口径：任务事件带完整的四元组（module_id="work"），
        这里把 module 换回聊天模块；计划级事件只带 plan_id，回账本反查。
        解析不到时宁可少通知，也不写到猜测的会话里。
        """
        owner_id = str(event.get("owner_id") or "")
        channel = str(event.get("channel") or "")
        conversation_id = str(event.get("conversation_id") or "")
        if not owner_id or not conversation_id:
            resolved = self._resolve_plan(str(event.get("plan_id") or ""))
            if resolved is None:
                return None
            owner_id, channel, conversation_id = resolved
        return Scope(owner_id, channel or "web", conversation_id, self.chat_module_id)

    def _resolve_plan(self, plan_id: str) -> tuple[str, str, str] | None:
        """按 plan_id 回账本查出计划属于哪个对话（老 Host / 元数据缺失时为 None）。"""
        if not plan_id:
            return None
        try:
            ledger = getattr(self.host, "ledger", None)
            getter = getattr(ledger, "get_plan_unscoped", None)
            if not callable(getter):
                return None
            plan = getter(plan_id)
        except Exception:
            logger.debug("plan lookup failed for %s", plan_id, exc_info=True)
            return None
        scope = getattr(plan, "scope", None) if plan is not None else None
        if scope is None or not getattr(scope, "owner_id", "") or not getattr(scope, "conversation_id", ""):
            return None
        return (
            str(scope.owner_id),
            str(getattr(scope, "channel", "") or "web"),
            str(scope.conversation_id),
        )

    # ── 发布 ────────────────────────────────────────────────────────
    async def _publish_stage(
        self, event: Mapping[str, Any], payload: Mapping[str, Any], stage: str
    ) -> str:
        attempt_id = self._attempt_id(event)
        if not attempt_id:
            return STATUS_SKIPPED  # 没有执行尝试就没有生命周期，不发
        scope = self._chat_scope(event)
        if scope is None:
            return STATUS_SKIPPED  # 身份解析不到：不写进猜测的会话

        node = _STAGE_NODE.get(stage, stage)
        marker = (attempt_id, node)
        if marker in self._published:
            return STATUS_SKIPPED  # 重放 / 重复回调：同一件事只说一次
        if attempt_id in self._closed:
            return STATUS_CLOSED  # 这次执行已经收口，之后一条都不发
        if self.reporter is None:
            return STATUS_SKIPPED

        # 发布器只做"怎么说、怎么落库"：动态生成 → 公开文字校验 → publish_text。
        # 身份（attempt_id）进 run_id，与 chat 回合完全解耦。
        publisher = ChatStagePublisher(
            self.reporter,
            scope=scope,
            conversation_key=conversation_identity(scope),
            memory=self.memory,
            run_id=attempt_id,
        )
        conversation_key = publisher.conversation_key
        progress = await self._progress_for(attempt_id, conversation_key, publisher)
        if progress is None or progress.closed:
            return STATUS_CLOSED

        # 本生命周期第一次有真实阶段 → 时间门槛从这里开始算（首版为 0 秒）。
        progress.begin_query()
        summary, next_action = STAGE_FACTS.get(stage, ("", ""))
        decision = await progress.note(StageReport(
            stage, node=node, summary=summary, next_action=next_action, has_more=True,
        ))
        if decision == STATUS_SENT:
            self._remember(marker)
        return decision

    async def _progress_for(
        self, attempt_id: str, conversation_key: str, publisher: ChatStagePublisher
    ) -> TurnProgress | None:
        """取该次执行的进度控制器；换新尝试时先收口旧尝试的待发消息。"""
        existing = self._progress.get(attempt_id)
        if existing is not None and not existing.closed:
            return existing

        previous_id = self._by_conversation.get(conversation_key)
        if previous_id and previous_id != attempt_id:
            # 同一会话的新执行尝试：旧尝试还没发出去的那条作废，
            # 不许串到新尝试头上。按 attempt 分桶意味着并行任务不受影响。
            await self._close_attempt(previous_id)
        turn = TurnProgress(
            turn_id=attempt_id,
            conversation_key=conversation_key,
            policy=self.policy,
            clock=self._clock,
            sleeper=self._sleeper,
            compose=publisher.compose,
            emit=publisher.publish,
        )
        self._progress[attempt_id] = turn
        self._by_conversation[conversation_key] = attempt_id
        return turn

    def _remember(self, marker: tuple[str, str]) -> None:
        self._published.add(marker)
        self._published_order.append(marker)
        while len(self._published_order) > self._max_remembered:
            self._published.discard(self._published_order.pop(0))

    async def _close_attempt(self, attempt_id: str) -> str:
        """关闭一次执行的进度：取消待发、等在途落库收口，之后一条都不发。"""
        if not attempt_id:
            return STATUS_CLOSED
        self._remember_closed(attempt_id)
        turn = self._progress.pop(attempt_id, None)
        if turn is not None:
            conversation_key = turn.conversation_key
            if self._by_conversation.get(conversation_key) == attempt_id:
                self._by_conversation.pop(conversation_key, None)
            try:
                await turn.close()
            except Exception:
                logger.debug("closing work progress failed attempt=%s", attempt_id, exc_info=True)
        # 已经发出去的节点留在 _published 里：迟到的重放不会再来一条。
        return STATUS_CLOSED

    def _remember_closed(self, attempt_id: str) -> None:
        if attempt_id in self._closed:
            return
        self._closed.add(attempt_id)
        self._closed_order.append(attempt_id)
        while len(self._closed_order) > self._max_remembered:
            self._closed.discard(self._closed_order.pop(0))


def build_work_progress_bridge(
    host: Any,
    reporter: StewardReporter | None,
    *,
    memory: RecentPhraseMemory | None = None,
    chat_module_id: str = "companion",
) -> WorkProgressBridge | None:
    """装配辅助：没有事件源或没有发布通道时返回 None（调用方据此跳过建桥）。"""
    if host is None or not callable(getattr(host, "events", None)):
        return None
    if reporter is None:
        return None
    return WorkProgressBridge(
        host,
        reporter,
        chat_module_id=chat_module_id,
        memory=memory,
    )
