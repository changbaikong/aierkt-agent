"""本回合过程通知控制（阶段记录 → 发送 / 暂存合并 / 跳过）。

【它解决什么】
一个长回合里真正发生的事是分阶段的：开始查 → 拿到材料 → 读原文 → 换来源。
"现在该不该说一句"如果散落在业务流程里，会同时出现两种毛病：

- 说漏：17 秒、29 秒的回合里只有一句接手，后面做了什么没人知道；
- 说错：把"开始读原文"说成"读完了"，把"没抽到摘要"说成"没找到"。

本模块把"该不该说、什么时候说、还能说几句"收成一处：

    业务流程 --note(StageReport)--> 发送 / 暂存合并 / 跳过
                                      |
                                      v
                            统一交给 steward 措辞、落库、广播

【三条纪律】
1. 只收"已经确认的事实"：阶段记录里不能出现网页正文、查询参数、本机路径、
   异常栈或模型推理；这些一旦进来就有被写进气泡的风险。
2. 时间只决定值不值得说，不制造进展：没有新的阶段变化，等再久也不发；
   长时间单次查询可以只有"正在查"和最终回答。
3. 终态先关闸：最终回答 / 取消 / 失败之前先 :meth:`TurnProgress.close`；
   之后一条都不许再发，已经在落库路上的那条要等它收口再交付最终回答。

【首版体验参数（只影响密度，不影响事实判断）】
- 第一次通知：本回合第一次真实查询开始后累计等待达到 1.5 秒仍未结束才说；
- 相邻两条过程消息至少隔 4 秒；
- 一次聊天回合最多一条首次通知 + 两条中段通知；
- 间隔内出现的新进展覆盖待发旧进展，到期只发"当时仍然成立"的最新状态。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 阶段常量
# ----------------------------------------------------------------------
# 与 steward.PHASE_* 是同一套取值里的"聊天查询专用"部分：
#   searching   第一次查询正在执行（接手后的真实开头，不是空口"准备开始"）
#   materials   搜索确实返回了材料，而且还有下一步
#   reading     已经开始读某份原文，但正文还没回来
#   read_done   正文确实拿到了，而且还要读下一份
#   redirect    这条线索为空 / 读取失败，且替代动作确实会执行
STAGE_SEARCHING = "searching"
STAGE_MATERIALS = "materials_found"
STAGE_READING = "reading_source"
STAGE_READ_DONE = "source_read"
STAGE_REDIRECT = "redirect"
#: 已识别具体矛盾，而且确实还会补查。只是"发现说法有出入"但预算用完时，
#: 不许用它 —— 那属于最终回答要说清楚的事，不是过程中的承诺。
STAGE_CONFLICT = "conflict"

# ----------------------------------------------------------------------
# 代码长任务（执行进程里跑的那种）的阶段
# ----------------------------------------------------------------------
# 它们跟随**任务与执行尝试**的生命周期，而不是聊天回合：
# 主聊天回复"已经交给代码代理"不等于任务终态，不能据此关闭通知。
STAGE_WORK_INSPECT = "work_inspect"      # 查看项目：明确的读取或搜索操作开始
STAGE_WORK_EDIT = "work_edit"            # 修改代码：明确的编辑或写入操作开始
STAGE_WORK_VERIFY = "work_verify"        # 验证改动：宿主真的开始跑测试
STAGE_WORK_DELIVER = "work_deliver"      # 准备交付：补丁确实已经生成
STAGE_WORK_REPAIR = "work_repair"        # 失败后确实进入修复（额外允许一条）

#: 必须告知、不受普通限频和上限约束的阶段（等用户确认、终态说明等）。
#: "等待用户确认等必须告知的消息保留现有即时规则" —— 就是这一组。
IMMEDIATE_STAGES = frozenset(
    {"waiting_approval", "cancelled", "failed", "interrupted", "completed"}
)

#: note() 的四种决定。
STATUS_SENT = "sent"                # 已经（或立即）发出去
STATUS_QUEUED = "queued"            # 暂存，等到时间门槛再发（队列只留最新一条）
STATUS_SKIPPED = "skipped"          # 重复节点 / 没有阶段 / 队列里已有同节点
STATUS_SUPPRESSED = "suppressed"    # 达到中段上限，不再发
STATUS_CLOSED = "closed"            # 本回合已经关闭，永远不发


# ----------------------------------------------------------------------
# 输入 / 规则
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class StageReport:
    """一条可信的阶段记录。**只允许放公开、已确认的信息。**

    :param stage: 阶段取值见上（STAGE_*）。
    :param node: 稳定逻辑节点身份，同一回合内同一件事必须用同一个名字，
        例如 ``first_query`` / ``materials`` / ``read:1`` / ``redirect:1``。
        不许用每次随机工具编号 —— 那样去重会失效。
    :param summary: 已经确认的公开摘要（可以为空，空就只讲当前动作）。
    :param next_action: 确实会执行的下一步（没有就留空，不要写"继续手上的事"）。
    :param has_more: 之后是否还有查询或读取。默认 True（还有）；显式传 False
        表示"这条之后就要交付最终回答了"，此时不补过程句。
    """

    stage: str
    node: str = ""
    summary: str = ""
    next_action: str = ""
    has_more: bool = True

    def normalized(self) -> "StageReport":
        stage = str(self.stage or "").strip()
        node = str(self.node or "").strip() or stage
        summary = str(self.summary or "").strip()
        next_action = str(self.next_action or "").strip()
        if (stage, node, summary, next_action) == (
            self.stage,
            self.node,
            self.summary,
            self.next_action,
        ):
            return self
        return replace(self, stage=stage, node=node, summary=summary, next_action=next_action)


@dataclass(frozen=True)
class ProgressPolicy:
    """首版体验参数。改这里只改变密度，不改变"进展真假"。"""

    first_wait_seconds: float = 1.5
    min_gap_seconds: float = 4.0
    max_mid_notices: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "first_wait_seconds", max(0.0, float(self.first_wait_seconds)))
        object.__setattr__(self, "min_gap_seconds", max(0.0, float(self.min_gap_seconds)))
        object.__setattr__(self, "max_mid_notices", max(0, int(self.max_mid_notices)))


EmitFn = Callable[[StageReport, str], Awaitable[Any]]
ComposeFn = Callable[[StageReport], Awaitable[str]]


#: 阶段 → 交给措辞层的**安全事实**（摘要, 下一步）。
#: 它们是事实标签，不是要发出去的话：模型结合阶段、事实和近期已发句重新表达。
#: 这里刻意只放结构性事实，任何查询词、标题、路径、正文都不会进来。
STAGE_FACTS: dict[str, tuple[str, str]] = {
    STAGE_SEARCHING: ("第一次查询正在执行，还没有结果", "等这次查询返回"),
    STAGE_MATERIALS: ("搜索确实返回了可用材料", "继续筛选或读取原文"),
    STAGE_READING: ("已经选定来源并开始读取，正文还没回来", "等这份原文取回来"),
    STAGE_READ_DONE: ("这份原文已经成功取回", "还要看下一份来源"),
    STAGE_REDIRECT: ("这条线索没有提供足够材料", "换一个来源继续查"),
    STAGE_CONFLICT: ("两份材料的说法有出入", "再补查一次核对"),
    STAGE_WORK_INSPECT: ("已经开始读取或搜索项目里的内容", "看清问题出在哪再动手"),
    STAGE_WORK_EDIT: ("已经开始修改文件", "改完之后检查改动"),
    STAGE_WORK_VERIFY: ("宿主真的开始跑验证", "等验证结果"),
    STAGE_WORK_DELIVER: ("补丁确实已经生成", "整理改动和验证情况交付"),
    STAGE_WORK_REPAIR: ("上一版没有通过，进入了修复", "改一版再验一次"),
}


# ----------------------------------------------------------------------
# 工具结果 → 安全阶段判断
# ----------------------------------------------------------------------
#: 常见搜索工具放材料列表的字段名。
#: **故意不含 sources**：web 搜索的 output["sources"] 是平台名列表，不是材料。
_MATERIAL_KEYS = ("results", "items", "materials", "rows", "entries", "hits")
#: 材料列表可能被包一层（result / data / output …）。
_CONTAINER_KEYS = ("data", "result", "output", "payload", "response")
#: 一行材料里只要有一个非空文本字段，就算这条材料真的存在。
_ROW_TEXT_KEYS = (
    "url", "link", "href", "title", "content", "text", "description", "snippet", "summary",
)
_OK_STATUSES = frozenset({"ok", "success", "completed"})
_BAD_FETCH = frozenset({"failed", "not_found", "empty"})


def _tool_status(result: Any) -> str:
    return str(getattr(result, "status", "") or "").strip().lower()


def _tool_output(result: Any) -> dict[str, Any]:
    output = getattr(result, "output", None)
    return output if isinstance(output, dict) else {}


def find_material_rows(output: Any, depth: int = 0) -> list[Any] | None:
    """从已知结构里取出"材料列表"；结构不认识时返回 None。

    只看结构，不看自由文本：**成功结果只有嵌套材料列表、没有顶层标题**
    同样算拿到了材料 —— 不能因为没抽到 summary/title 就误报"没有找到"。
    """
    if not isinstance(output, dict) or depth > 2:
        return None
    for key in _MATERIAL_KEYS:
        value = output.get(key)
        if isinstance(value, list):
            return value
    for key in _CONTAINER_KEYS:
        value = output.get(key)
        if isinstance(value, dict):
            found = find_material_rows(value, depth + 1)
            if found is not None:
                return found
    return None


def _row_has_material(row: Any) -> bool:
    if isinstance(row, str):
        return bool(row.strip())
    if isinstance(row, dict):
        return any(str(row.get(key) or "").strip() for key in _ROW_TEXT_KEYS)
    return False


def search_outcome(result: Any) -> str:
    """这次搜索到底发生了什么。只认结构，不认自由文本。

    - ``materials``：确实带回了可用材料（含只有嵌套列表、没有顶层标题的情况）；
    - ``empty``    ：材料列表是空的；
    - ``failed``   ：失败状态；
    - ``unknown``  ：不是已知的搜索结构 —— 调用方据此**什么都不说**，
      不把任意字段当成事实。
    """
    if _tool_status(result) not in _OK_STATUSES:
        return "failed"
    rows = find_material_rows(_tool_output(result))
    if rows is None:
        return "unknown"
    return "materials" if any(_row_has_material(row) for row in rows) else "empty"


def read_outcome(result: Any) -> str:
    """一次原文 / 视频详情读取到底走到了哪一步。

    - ``read``   ：正文确实取回来了（有正文，或视频详情有简介）；
    - ``failed`` ：读取失败 / 页面不存在 / 空内容；
    - ``unread`` ：状态是成功但没拿到正文 —— 这时**不许**说"读完了"。
    """
    if _tool_status(result) not in _OK_STATUSES:
        return "failed"
    output = _tool_output(result)
    if str(output.get("fetch_status") or "").strip().lower() in _BAD_FETCH:
        return "failed"
    for key in ("content", "description", "text"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return "read"
    return "unread"


def stage_for_results(results: Iterable[tuple[str, Any]], *, search_tools: Iterable[str], read_tools: Iterable[str]) -> str:
    """把一轮工具结果归成一个阶段：读取成功 > 有材料 > 空手/失败。

    只回答"真实发生了什么"，**不回答"后面还有没有事"** ——
    后者由调用方在确实还要继续时才决定要不要说出口。
    """
    search_names = {str(name) for name in search_tools}
    read_names = {str(name) for name in read_tools}
    saw_read = read_ok = saw_search = saw_material = saw_barren = False
    for name, result in results:
        tool = str(name or "")
        if tool in read_names:
            saw_read = True
            state = read_outcome(result)
            if state == "read":
                read_ok = True
            elif state == "failed":
                saw_barren = True
        elif tool in search_names:
            saw_search = True
            state = search_outcome(result)
            if state == "materials":
                saw_material = True
            elif state in {"empty", "failed"}:
                saw_barren = True
    if read_ok:
        return STAGE_READ_DONE
    if saw_material:
        return STAGE_MATERIALS
    if (saw_search or saw_read) and saw_barren:
        return STAGE_REDIRECT
    return ""


#: 阶段 → 本回合内的稳定逻辑节点身份。同一件事整轮只报一次。
STAGE_NODES = {
    STAGE_SEARCHING: "first_query",
    STAGE_MATERIALS: "materials",
    STAGE_READING: "reading",
    STAGE_READ_DONE: "read_done",
    STAGE_REDIRECT: "redirect",
    STAGE_CONFLICT: "conflict",
    STAGE_WORK_INSPECT: "work_inspect",
    STAGE_WORK_EDIT: "work_edit",
    STAGE_WORK_VERIFY: "work_verify",
    STAGE_WORK_DELIVER: "work_deliver",
    STAGE_WORK_REPAIR: "work_repair",
}


def conversation_identity(scope: Any) -> str:
    """会话身份：回合隔离、选句记录、去重都按它分桶。"""
    return "|".join(
        (
            str(getattr(scope, "owner_id", "") or ""),
            str(getattr(scope, "channel", "") or ""),
            str(getattr(scope, "conversation_id", "") or ""),
            str(getattr(scope, "module_id", "") or ""),
        )
    )


# ----------------------------------------------------------------------
# 本回合控制器
# ----------------------------------------------------------------------
class TurnProgress:
    """管一个回合的过程通知：时间门槛、合并、上限、收口。

    措辞分两段，主任务只参与第一段的排队：

    1. ``compose``（可选）：在**后台任务**里按阶段动态生成一句话术（可取消、有短超时）。
       生成失败 / 超时 / 不合格 / 近期重复 → 返回空串，这条就静默跳过，不回退固定句；
    2. ``emit``：把已经生成并通过校验的话落库 + 广播（快，且不响应取消 —— 半路
       中断会出现"写了库没广播"）。

    典型用法::

        progress = TurnProgress(compose=announcer.compose, emit=announcer.publish)
        progress.begin_query()                       # 第一次真实查询开始
        await progress.note(StageReport(STAGE_SEARCHING, node="first_query"))
        await progress.note(StageReport(STAGE_MATERIALS, node="materials"))
        await progress.close()                       # 交付最终回答之前：取消没生成完的
    """

    def __init__(
        self,
        *,
        turn_id: str = "",
        conversation_key: str = "",
        policy: ProgressPolicy | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
        compose: ComposeFn | None = None,
        emit: EmitFn | None = None,
        immediate_stages: Iterable[str] | None = None,
    ) -> None:
        self.turn_id = str(turn_id or "")
        self.conversation_key = str(conversation_key or "")
        self.policy = policy or ProgressPolicy()
        self._clock: Callable[[], float] = clock or time.monotonic
        self._sleeper: Callable[[float], Awaitable[None]] = sleeper or asyncio.sleep
        self._compose: ComposeFn | None = compose
        self._emit: EmitFn | None = emit
        self.immediate_stages = frozenset(
            str(stage) for stage in (IMMEDIATE_STAGES if immediate_stages is None else immediate_stages)
        )

        self._closed = False
        self._first_query_at: float | None = None
        self._last_sent_at: float | None = None
        self._first_sent = False
        self._mid_sent = 0
        self._published_nodes: set[str] = set()
        self._pending: StageReport | None = None
        self._pending_due: float | None = None
        self._worker: asyncio.Task | None = None
        self._emitting: asyncio.Task | None = None

        #: 观测用：真正发出去的阶段顺序、措辞失败次数。测试与排障都读它。
        self.published: list[str] = []
        self.composed: list[str] = []
        self.failures = 0

    # ── 状态 ────────────────────────────────────────────────────────
    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> StageReport | None:
        """当前待发的最新状态（没有就是 None）。"""
        return self._pending

    def stats(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "closed": self._closed,
            "first_query_at": self._first_query_at,
            "last_sent_at": self._last_sent_at,
            "first_sent": self._first_sent,
            "mid_sent": self._mid_sent,
            "pending": self._pending.stage if self._pending else "",
            "published": list(self.published),
            "composed": list(self.composed),
            "failures": self.failures,
        }

    # ── 时间 ────────────────────────────────────────────────────────
    def _now(self, now: float | None = None) -> float:
        return float(self._clock() if now is None else now)

    def begin_query(self, *, now: float | None = None) -> None:
        """记下本回合**第一次真实查询**的开始时间（只记第一次）。

        它只用于"等待够不够久"的判断，不代表任何进展。
        """
        if self._closed:
            return
        if self._first_query_at is None:
            self._first_query_at = self._now(now)
        if self._pending is not None:
            self._schedule(now=now)

    def _due_at(self, now: float | None = None) -> float | None:
        """待发状态最早可以发送的时刻；None 表示"还不能计时"。"""
        if self._pending is None or self._first_query_at is None:
            return None
        due = self._first_query_at + self.policy.first_wait_seconds
        if self._first_sent and self._last_sent_at is not None:
            due = max(due, self._last_sent_at + self.policy.min_gap_seconds)
        return due

    # ── 主入口 ──────────────────────────────────────────────────────
    async def note(self, report: StageReport, *, now: float | None = None) -> str:
        """记录一条真实进展，返回 :data:`STATUS_*` 里的决定。"""
        if self._closed:
            return STATUS_CLOSED
        item = report.normalized() if isinstance(report, StageReport) else StageReport(**report)
        if not item.stage:
            return STATUS_SKIPPED

        # 必须告知的阶段：立即发，不排队、不计中段上限。
        if item.stage in self.immediate_stages:
            return await self._send(item, now=now)

        if item.node in self._published_nodes:
            # 同一件事只说一次：模型自己查到的材料和补查查到的材料共用一个节点。
            return STATUS_SKIPPED
        if not item.has_more:
            # 没有后续查询或读取 → 紧接着就是最终回答，不补过程句。
            return STATUS_SKIPPED
        if self._pending is not None and self._pending.node == item.node:
            # 同一节点的新描述覆盖待发旧描述，不新起等待、也不多发一条。
            self._pending = item
            self._schedule(now=now)
            return STATUS_QUEUED
        if self._first_sent and self._mid_sent >= self.policy.max_mid_notices:
            return STATUS_SUPPRESSED

        self._pending = item
        self._schedule(now=now)
        return STATUS_QUEUED

    async def close(self) -> None:
        """关闸：取消待发、等待已在落库路上的那条收口。

        交付最终回答 / 取消 / 失败之前必须调用它；之后再 note() 一律返回
        :data:`STATUS_CLOSED`，不会出现"最终回答之后又冒出旧进度"。

        关闸前先让出**一次**事件循环：如果有一条"已经到点、只是还没被调度到"的
        进展，给它这一拍的机会（当场生成完就发出去；措辞慢的话下面照样取消）。
        这只让出一拍，绝不等待 I/O，所以主任务不会被措辞拖住。
        """
        if not self._closed and self._due_and_waiting():
            await asyncio.sleep(0)
        self._closed = True
        self._pending = None
        self._pending_due = None
        worker, self._worker = self._worker, None
        if worker is not None and not worker.done():
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await worker
        emitting = self._emitting
        if emitting is not None and not emitting.done():
            # 已经在落库/广播路上的那条必须等它走完，否则会跑到最终回答后面。
            with contextlib.suppress(Exception):
                await asyncio.shield(emitting)
        self._emitting = None

    def _due_and_waiting(self) -> bool:
        """有没有"已经到点、却还没被调度"的待发进展。"""
        if self._pending is None:
            return False
        due = self._due_at()
        return due is not None and due <= self._clock()

    # ── 内部 ────────────────────────────────────────────────────────
    def _schedule(self, *, now: float | None = None) -> None:
        if self._closed or self._pending is None:
            return
        self._pending_due = self._due_at(now)
        if self._pending_due is None:
            # 还没有真实查询开始 —— 等 begin_query() 再排。
            return
        if self._worker is None or self._worker.done():
            self._worker = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        """可取消的轻量等待：到点重新读**最新**状态再决定发不发。

        注意这里是循环而不是"发一条就退出"：发送过程本身要 await（落库 + 广播），
        这期间到达的新进展必须被这一轮继续处理 —— 否则
        "note() 看到 worker 还没结束、于是不新建等待" 就会把那条进展永久丢掉。
        """
        try:
            while True:
                if self._closed or self._pending is None:
                    return
                due = self._due_at()
                if due is None:
                    # 还没有真实查询开始；等 begin_query() 再来排。
                    return
                delay = due - self._clock()
                if delay > 0:
                    await self._sleeper(delay)
                    continue
                item, self._pending = self._pending, None
                self._pending_due = None
                if self._closed or item is None:
                    return
                await self._send(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("stage progress waiter failed", exc_info=True)

    async def _send(self, item: StageReport, *, now: float | None = None) -> str:
        """到点发一条：先让措辞层生成（可取消），再落库广播（不响应取消）。

        三步都必须在这里说清楚，少一步就会出问题：

        1. **配额先扣**：节点身份、首次/中段计数在这一步就定下来，生成失败也不重试，
           否则一次失败会让同一阶段反复占用后台模型；
        2. **生成可取消**：终态关闸时会取消 worker，把还没生成完的那句直接掐掉 ——
           主任务不等待措辞；
        3. **发布闸**：生成完成后重新看一次 ``closed``；已经关闸就不许再落库，
           这样"最终回答之后又冒出旧进度"在时序上不可能发生。
        """
        if self._closed:
            return STATUS_CLOSED
        started_at = self._now(now)
        if self._first_sent:
            self._mid_sent += 1
        else:
            self._first_sent = True
        self._published_nodes.add(item.node)

        text = ""
        compose = self._compose
        if compose is not None:
            try:
                text = str(await compose(item) or "").strip()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.failures += 1
                logger.debug("stage phrase composition failed", exc_info=True)
                return STATUS_SENT
            if not text:
                # 超时 / 不合格 / 近期重复：静默跳过，不回退固定句。
                self.failures += 1
                return STATUS_SENT
            self.composed.append(text)

        if self._closed:
            # 发布闸：生成期间回合已经收口，这条旧通知不许再出现。
            return STATUS_CLOSED

        emit = self._emit
        if emit is None:
            self._last_sent_at = started_at
            self.published.append(item.stage)
            return STATUS_SENT
        task = asyncio.ensure_future(emit(item, text))
        self._emitting = task
        try:
            # shield：已经交给落库的这条必须走完，半路取消会出现"写了库没广播"。
            await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
            raise
        except Exception:
            # 通知通道故障不能让主任务挂掉，也不能变成内部错误气泡。
            self.failures += 1
            logger.debug("stage progress emission failed", exc_info=True)
        finally:
            if self._emitting is task and task.done():
                self._emitting = None
        # 节奏按**真正发出去的时刻**算，措辞耗时不会让两条挤在一起。
        self._last_sent_at = self._now()
        self.published.append(item.stage)
        return STATUS_SENT


# ----------------------------------------------------------------------
# 按会话分桶的回合管理
# ----------------------------------------------------------------------
class ProgressBoard:
    """按会话保存"当前回合"的控制器，负责回合与回合之间的隔离。

    - :meth:`begin` 开新回合时先把旧回合关掉：旧回合的待发消息不许串到新回合；
    - 不同会话（owner/channel/conversation/module）各自一份，互不影响。
    """

    def __init__(
        self,
        *,
        policy: ProgressPolicy | None = None,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.policy = policy or ProgressPolicy()
        self._clock = clock
        self._sleeper = sleeper
        self._turns: dict[str, TurnProgress] = {}

    def current(self, key: str) -> TurnProgress | None:
        return self._turns.get(str(key or ""))

    async def begin(
        self,
        key: str,
        *,
        turn_id: str = "",
        compose: ComposeFn | None = None,
        emit: EmitFn | None = None,
    ) -> TurnProgress:
        """开一个新回合；同会话的旧回合先收口。"""
        bucket = str(key or "")
        old = self._turns.get(bucket)
        if old is not None:
            await old.close()
        turn = TurnProgress(
            turn_id=turn_id,
            conversation_key=bucket,
            policy=self.policy,
            clock=self._clock,
            sleeper=self._sleeper,
            compose=compose,
            emit=emit,
        )
        self._turns[bucket] = turn
        return turn

    async def close(self, key: str) -> None:
        bucket = str(key or "")
        turn = self._turns.pop(bucket, None)
        if turn is not None:
            await turn.close()

    async def close_all(self) -> None:
        for turn in list(self._turns.values()):
            await turn.close()
        self._turns.clear()
