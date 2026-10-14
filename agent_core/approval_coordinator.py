"""审批协调器：聊天确认与 Work 审批的**唯一**用户入口。

【它解决的问题：一件事、两套接口、两套词】

系统里有两种"高风险动作要先问用户一句"，它们各自长出了自己的接口与词表：

    聊天确认（`ConfirmationStore`）  confirmed / rejected
    Work 审批（`work_ledger`）        approved / rejected / expired

前端拿到的是一堆"这一条该发哪个地址、字段叫什么"的知识，而每一端都要
自己答对同样两个问题：**重复点会不会跑两次**、**拒绝之后会不会偷偷执行**。
补丁打在一边，另一边不会跟着好 —— 这是"同一件事有多份实现"的典型症状，
和网关之前那四份 `ToolExecutor` 是同一种病。

【它不做什么】

- **不改两边的持久化语义**。聊天确认带"暂停时的消息快照 + 账号锁"，
  Work 审批带"账本里的 attempt 分期"，这两套东西是各自链路真正需要的，
  强行统一等于把其中一边的多余语义塞给另一边。协调器只做路由与记账。
- **不自己建审批记录**。创建那条记录是网关的 `approval_sink` 与 Work 账本的事。
- **不接受调用方传参数**。`decide` 只收 `(approval_id, scope, decision)` ——
  "当初想做什么"必须从存储里那条记录读，不能从请求体里重建。
  否则就有一条旁路：拿一个审批编号 + 自己现编一组参数去执行。

【词表只有两个】

对外只有 `confirmed` / `rejected`。Work 那边的 `approved` 由这里翻译过去，
`expired` 是账本内部的状态（超时），**不接受**从外面传进来 ——
让外界能把一条审批标成 expired，等于给了一条"绕过拒绝、直接判它过期"的路。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from .contracts import Scope
from .persistence import ConfirmationStore

logger = logging.getLogger(__name__)

DECISION_CONFIRMED = "confirmed"
DECISION_REJECTED = "rejected"
APPROVAL_DECISIONS = frozenset({DECISION_CONFIRMED, DECISION_REJECTED})

#: 审批的种类。前端按它决定展示方式（聊天确认要回一句续跑的回复，Work 审批只是排队）。
APPROVAL_KIND_CHAT = "chat"
APPROVAL_KIND_WORK = "work"

#: 交给 Work 账本的词。两套词表各自成立，翻译只在这一处发生。
_WORK_DECISION = {
    DECISION_CONFIRMED: "approved",
    DECISION_REJECTED: "rejected",
}

ChatResume = Callable[[str, Scope, str], Awaitable[dict[str, Any]]]


class ApprovalCoordinator:
    """按 `approval_id` 统一查询与决策。

    Attributes:
        confirmation_store: 聊天确认的持久化（`ConfirmationStore`）。
        work_host: Work 的 Host。

    两个依赖都允许为 None（"这一半没接上"）。缺哪一半，那一半就整条不可用：
    查询少一条来源、决策找不到编号 —— 而不是**用默认值悄悄建一个**。
    建错库比不建更难查（这是本项目反复踩到的同一类事故）。
    半边可用是有意的：只跑 Work 的场景不该因为聊天那半没初始化就整个挂掉。
        resume_chat: 让聊天那条链路续跑的回调，签名
            `(action_id, scope, decision) -> dict`。注入而不是直接依赖
            `ConversationManager`：协调器只要求"给我一个能续跑的东西"，
            谁提供它（Web 的 manager、CLI 的会话）与它无关。
            为 None 时聊天审批只能查、不能决 —— 明确报错，不假装成功。
    """

    def __init__(
        self,
        *,
        confirmation_store: ConfirmationStore | None = None,
        work_host: Any = None,
        resume_chat: ChatResume | None = None,
    ) -> None:
        self.confirmation_store = confirmation_store
        self.work_host = work_host
        self.resume_chat = resume_chat
        if confirmation_store is None:
            # 少一条来源是**可见的**降级，不是静默的：查询会少列聊天确认。
            # 记一条 warning 而不是抛，是因为"只跑 Work"确实是一个合法场景。
            logger.warning("approval coordinator has no confirmation store: chat confirmations are unavailable")

    # ── 查询 ────────────────────────────────────────────────────────

    def pending(self, scope: Scope) -> list[dict[str, Any]]:
        """该作用域下所有待办的审批，两种来源合并。

        刻意**不**按时间排序：两种来源的时间戳格式与时钟来源不同
        （一个是 ISO 秒、一个是账本自己的 `_iso`），混排出来的"先后"
        是假的。要排序由前端按自己拿到的字段排。
        """
        items = self._pending_chat(scope) + self._pending_work(scope)
        return items

    def get(self, approval_id: str, scope: Scope) -> dict[str, Any] | None:
        """按编号取一条。**不属于这个作用域的一律返回 None。**

        对"不存在"与"不属于你"回同一个值是有意的：分开能让调用方
        枚举出别人有哪些审批在等。
        """
        record = self._chat_record(approval_id, scope)
        if record is not None:
            return record
        return self._work_record(approval_id, scope)

    # ── 决策 ────────────────────────────────────────────────────────

    async def decide(self, approval_id: str, scope: Scope, decision: str) -> dict[str, Any]:
        """投票。`decision` 只认 `confirmed` / `rejected`。

        Raises:
            ValueError: 词表外的决定。
            KeyError: 编号不存在或不属于这个作用域。
        """
        if decision not in APPROVAL_DECISIONS:
            # 词表外的词必须在这里挡住。放进来的话，下游的
            # `decide_approval` 会拿它做 `!= "approved"` 的比较 ——
            # 一个拼错的词就变成了一次静默拒绝。
            raise ValueError(f"invalid approval decision: {decision!r}")

        record = self.get(approval_id, scope)
        if record is None:
            raise KeyError(approval_id)

        if record["kind"] == APPROVAL_KIND_WORK:
            return await self._decide_work(record, scope, decision)
        return await self._decide_chat(record, scope, decision)

    # ── 聊天确认 ────────────────────────────────────────────────────

    def _chat_record(self, action_id: str, scope: Scope) -> dict[str, Any] | None:
        if self.confirmation_store is None:
            return None
        pending = self.confirmation_store.get(action_id, scope)
        if pending is None:
            return None
        return self._chat_item(pending)

    @staticmethod
    def _chat_item(pending: Any) -> dict[str, Any]:
        """聊天确认 → 统一条目。

        多了两个字段，都各有用处：

        - `action_id` 与 `approval_id` **同时**出现：老前端读的是 `action_id`。
          这是迁移期的别名，等前端切完（§4 的最后一步）就该删。
        - `run_id` / `arguments`：老的 `/confirmations` 接口本来就返回它们，
          代理过去时不能比原来少东西，否则老前端会"迁移期突然坏了"。
        """
        return {
            "approval_id": pending.action_id,
            # 迁移期别名，前端切完即删。
            "action_id": pending.action_id,
            "kind": APPROVAL_KIND_CHAT,
            "status": pending.status,
            "title": pending.title,
            "tool": pending.tool,
            "code": pending.code,
            "expires_at": pending.expires_at,
            "module_id": pending.module_id,
            "run_id": pending.run_id,
            "arguments": dict(pending.arguments or {}),
        }

    def _pending_chat(self, scope: Scope) -> list[dict[str, Any]]:
        if self.confirmation_store is None:
            return []
        return [self._chat_item(item) for item in self.confirmation_store.pending_for_scope(scope)]

    async def _decide_chat(self, record: dict[str, Any], scope: Scope, decision: str) -> dict[str, Any]:
        if record["status"] != "pending":
            return self._unchanged(record)
        if self.resume_chat is None:
            # 明确报错，不假装成功。"已确认"却没有续跑，用户会以为事情在办。
            raise RuntimeError("chat confirmations cannot be decided: no resume callback is configured")
        # **先认领，再续跑**：`decide` 是一条 `UPDATE ... WHERE status='pending'`
        # 加 rowcount 判断，所以"谁先到"由数据库定，不由调用顺序定。
        # 反过来的话（先续跑再落状态），两次几乎同时到达的点击会一起穿过
        # "还是 pending"的检查 —— 那时只剩网关的原子登记在兜底，
        # 而兜底不该是常态。
        claimed = self.confirmation_store.decide(record["approval_id"], scope, decision)
        if claimed is None:
            # 认领失败 = 另一票已经赢了。回读取到的最新状态，不报错。
            latest = self.get(record["approval_id"], scope) or record
            return self._unchanged(latest)
        result = await self.resume_chat(record["approval_id"], scope, decision)
        return {"kind": APPROVAL_KIND_CHAT, "decision": decision, **dict(result or {})}

    # ── Work 审批 ───────────────────────────────────────────────────

    def _work_record(self, approval_id: str, scope: Scope) -> dict[str, Any] | None:
        if self.work_host is None:
            return None
        item = self.work_host.get_approval(approval_id, scope)
        if item is None:
            return None
        return {
            "approval_id": item["approval_id"],
            "kind": APPROVAL_KIND_WORK,
            "status": item.get("status", "pending"),
            "title": item.get("title") or item.get("objective") or "后台任务",
            "tool": item.get("task_type", ""),
            "payload": dict(item.get("payload") or {}),
        }

    def _pending_work(self, scope: Scope) -> list[dict[str, Any]]:
        if self.work_host is None:
            return []
        items = self.work_host.pending_approvals(scope)
        return [
            {
                "approval_id": item["approval_id"],
                "kind": APPROVAL_KIND_WORK,
                "status": item.get("status", "pending"),
                "title": item.get("title") or item.get("objective") or "后台任务",
                "tool": item.get("task_type", ""),
                "payload": dict(item.get("payload") or {}),
            }
            for item in items
        ]

    async def _decide_work(self, record: dict[str, Any], scope: Scope, decision: str) -> dict[str, Any]:
        if record["status"] != "pending":
            return self._unchanged(record)
        result = await self.work_host.decide_approval(
            record["approval_id"], scope, _WORK_DECISION[decision]
        )
        return {"kind": APPROVAL_KIND_WORK, "decision": decision, **dict(result or {})}

    # ── 公共小件 ────────────────────────────────────────────────────

    @staticmethod
    def _unchanged(record: dict[str, Any]) -> dict[str, Any]:
        """重复点击（或已经被处理过）的回应。

        回 `unchanged` 而不是报错：用户连点两下确认按钮是**正常行为**，
        为此弹一个错误框只会让人以为出了问题。真正的保护在于
        **这条路上没有任何执行**，而不在于有没有报错。
        """
        return {
            "kind": record["kind"],
            "approval_id": record["approval_id"],
            "status": "unchanged",
            "decision": record["status"],
        }


__all__ = [
    "APPROVAL_DECISIONS",
    "APPROVAL_KIND_CHAT",
    "APPROVAL_KIND_WORK",
    "DECISION_CONFIRMED",
    "DECISION_REJECTED",
    "ApprovalCoordinator",
    "ChatResume",
]
