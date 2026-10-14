"""舞台播放投影：把「消息账本」翻译成「前端这一刻该看到什么」。

为什么要有这一层
----------------
消息账本是**事实**：用户说了什么、助手回了什么，写过就不该改。
舞台是**播放进度**：用户当前看到哪一句、下一句是什么。

过去这两件事被合成了一个 —— 前端直接渲染 ``messages[messages.length - 1]``，
于是"最后一句"既是最新事实、又承担播放位置，后果是：

- 两条管家进度前后脚到达，后一条立刻顶掉前一条，用户只看得见半句；
- 刷新一次把整段历史重新播一遍；
- 后台任务在网页关闭期间跑完，结果直接被当作"当前状态"闪现出来。

本模块只做一次纯函数投影，输入是账本消息 + 已读游标 + 运行状态，
输出是"可见消息 / 当前正在播哪条 / 还有几条没播 / 现在该不该说'我还在处理'"。
它不写库、不调工具、不碰 HTTP，方便单测也方便复用（CLI、监视器都能用同一套口径）。

播放顺序的约定
--------------
**只排助手消息。** 用户自己发的话不需要"播"也不需要"确认"——它一直是可见的。
需要两段式点击（先补全、再确认已读）的是助手说的每一句：
管家进度、最终回复、后台任务回执，全都一样走这条路。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

# ----------------------------------------------------------------------
# 活动状态
# ----------------------------------------------------------------------
#: 对话空闲：没有在跑的回合，也没有活跃的后台任务。
ACTIVITY_IDLE = "idle"
#: 正在处理：本轮对话在跑，或后台任务还在队列/执行中。前端据此显示"我还在处理"。
ACTIVITY_RUNNING = "running"
#: 停在等用户确认（高风险操作审批）。这时不该说"我还在处理" ——
#: 卡住的是用户，不是我们。
ACTIVITY_PAUSED = "paused"
#: 被中断（进程重启、回合异常终止）。必须与"还在处理"分开显示，
#: 否则用户会一直等一件永远不会完成的事。
ACTIVITY_INTERRUPTED = "interrupted"

#: 哪些状态该显示"我还在处理 · N秒"。刻意只留 running：
#: paused / interrupted 都是"没有在推进"的状态，用同一句话会让用户误以为还在跑。
WAITING_ACTIVITIES = frozenset({ACTIVITY_RUNNING})

#: 后台任务的活跃状态取值。与 work_ledger 的终态集合互补。
BACKGROUND_ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_approval", "pending"})


def _message_id(message: Any) -> int:
    """取消息的稳定身份；拿不到就当 0（= 无法参与播放游标）。"""
    try:
        value = int((message or {}).get("message_id") or 0)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def normalize_activity(runtime_state: str, *, background_active: bool = False) -> str:
    """把"对话运行态 + 后台任务是否活跃"合成一个前端可判定的状态。

    优先级刻意是 **后台活跃 > 中断**：进程重启会把上一轮标记成 interrupted，
    但如果此刻确实有后台任务在跑，说"任务已中断"就是错的 —— 有活干就是有活干。
    """
    if background_active:
        return ACTIVITY_RUNNING
    state = str(runtime_state or "").strip().lower()
    if state == ACTIVITY_INTERRUPTED:
        return ACTIVITY_INTERRUPTED
    if state == ACTIVITY_PAUSED:
        return ACTIVITY_PAUSED
    if state == ACTIVITY_RUNNING:
        return ACTIVITY_RUNNING
    return ACTIVITY_IDLE


def is_waiting(activity: str) -> bool:
    """这个状态该不该显示"我还在处理"。"""
    return str(activity or "") in WAITING_ACTIVITIES


# ----------------------------------------------------------------------
# 投影
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class PresentationSnapshot:
    """前端渲染舞台与履历所需的全部信息。"""

    messages: list[dict[str, Any]] = field(default_factory=list)
    acknowledged_message_id: int = 0
    current_message_id: int | None = None
    pending_message_ids: list[int] = field(default_factory=list)
    pending_count: int = 0
    activity: str = ACTIVITY_IDLE
    waiting: bool = False
    background: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "acknowledged_message_id": self.acknowledged_message_id,
            "current_message_id": self.current_message_id,
            "pending_message_ids": list(self.pending_message_ids),
            "pending_count": self.pending_count,
            "activity": self.activity,
            "waiting": self.waiting,
            "background": dict(self.background),
        }


def pending_assistant_messages(
    messages: Iterable[dict[str, Any]],
    acknowledged_message_id: int,
) -> list[dict[str, Any]]:
    """还没播的助手消息，按 ``message_id`` 升序。

    没有 ``message_id`` 的助手消息（理论上只在内存里、尚未落库）**不算待播**：
    它无法被确认，留在队列里会把后面所有消息永远堵死。
    """
    try:
        acknowledged = max(0, int(acknowledged_message_id or 0))
    except (TypeError, ValueError):
        acknowledged = 0
    pending = [
        message
        for message in messages
        if str((message or {}).get("role")) == "assistant" and _message_id(message) > acknowledged
    ]
    return sorted(pending, key=_message_id)


def visible_messages(
    messages: Iterable[dict[str, Any]],
    acknowledged_message_id: int,
    current_message_id: int | None,
) -> list[dict[str, Any]]:
    """履历该显示的消息。

    规则（对应"尚未轮到的气泡不提前出现"）：
    - 用户消息：一直可见；
    - 助手消息：``message_id <= 游标`` 的（已播过）可见；
    - 当前正在播的那一条也可见（用户正看着它）；
    - 队列里排在后面的助手消息，**一条都不给** —— 提前出现就等于剧透，
      而且用户会在履历里看到自己还没确认过的话。
    """
    visible: list[dict[str, Any]] = []
    for message in messages:
        if str((message or {}).get("role")) != "assistant":
            visible.append(message)
            continue
        identifier = _message_id(message)
        if identifier <= 0:
            # 没有身份的助手消息无法参与游标，筛掉就等于让它在履历里永久消失；
            # 放它过去是更安全的一侧：它至少是已经存在的事实。
            visible.append(message)
            continue
        if identifier <= max(0, int(acknowledged_message_id or 0)) or identifier == current_message_id:
            visible.append(message)
    return visible


def project_presentation(
    messages: list[dict[str, Any]],
    *,
    acknowledged_message_id: int = 0,
    runtime_state: str = ACTIVITY_IDLE,
    background: dict[str, Any] | None = None,
) -> PresentationSnapshot:
    """账本消息 + 播放游标 + 运行状态 → 前端快照。"""
    background = dict(background or {})
    background_active = bool(background.get("active"))
    activity = normalize_activity(runtime_state, background_active=background_active)
    try:
        acknowledged = max(0, int(acknowledged_message_id or 0))
    except (TypeError, ValueError):
        acknowledged = 0
    public = [message for message in messages if str((message or {}).get("role")) != "system"]
    pending = pending_assistant_messages(public, acknowledged)
    current_message_id = _message_id(pending[0]) or None if pending else None
    return PresentationSnapshot(
        messages=visible_messages(public, acknowledged, current_message_id),
        acknowledged_message_id=acknowledged,
        current_message_id=current_message_id,
        pending_message_ids=[_message_id(message) for message in pending],
        pending_count=len(pending),
        activity=activity,
        waiting=is_waiting(activity),
        background=background,
    )


def background_summary(items: Iterable[dict[str, Any]], *, plans: Iterable[dict[str, Any]] = ()) -> dict[str, Any]:
    """后台任务列表 → 舞台用的一行摘要。

    只回答两件事：**有没有活跃任务**（决定要不要继续显示"我还在处理"）、
    **活跃的有几条**。任务 id、目标、错误一律不进这个字典 ——
    前端只需要一个开关和可以显示的条数，多给字段就是多一个泄漏面。
    """
    active = 0
    for item in list(items) + list(plans):
        status = str((item or {}).get("status") or "").strip().lower()
        if status in BACKGROUND_ACTIVE_STATUSES:
            active += 1
    return {"active": active > 0, "active_count": active}
