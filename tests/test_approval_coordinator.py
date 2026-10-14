"""审批的唯一用户入口：聊天确认与 Work 审批摆在同一张台面上。

【为什么需要一个协调器，而不是"两个接口各修各的"】

系统里本来有**两套**审批，各自都有查询与决策接口：

    聊天确认  GET  /api/conversations/{cid}/confirmations   POST .../decision
    Work 审批 POST /api/work/approvals/{approval_id}

它们连**词表都不一样**：聊天那条收 `confirmed` / `rejected`，
Work 那条收 `approved` / `rejected` / `expired`。前端要按"这是哪种审批"
选不同的地址、不同的字段名。于是"用户点了确认"这件事在两端各写了一遍，
而每一遍都要自己答对"重复点会不会跑两次""拒绝之后会不会偷偷执行"。
补丁打在一边，另一边不会跟着好。

协调器把这件事收成一次：**按 `approval_id` 操作，词表只有 confirmed / rejected**。
两边的持久化语义一个字没改（聊天确认带暂停快照与账号锁，Work 审批在账本里分期），
协调器只负责"这一票投给谁、投完怎么记账"。

【这个文件守的几条】

1. **跨作用域拿不到**：别人的会话/别的账号的审批，`get` 直接返回 None；
2. **重复点击只执行一次**：第二次调用回 `unchanged`，且**不会再碰执行路径**；
3. **拒绝不执行**：rejected 只落状态，一次"批准"都不许发生；
4. **重启后还能办**：换一个协调器实例、同一批存储，待办仍在、还能决策；
5. **调用方只说"哪一票"**：想执行什么由**存储里的那条记录**决定，
   调用方带不进来任何参数 —— 带得进来就等于给了一条"拿审批编号配一组新参数"的旁路。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_core.approval_coordinator import (
    APPROVAL_KIND_CHAT,
    APPROVAL_KIND_WORK,
    DECISION_CONFIRMED,
    DECISION_REJECTED,
    ApprovalCoordinator,
)
from agent_core.contracts import AgentRequest, Scope
from agent_core.persistence import SQLiteConfirmationStore

SCOPE = Scope("owner-1", "desktop", "chat-1", "companion")
OTHER = Scope("owner-1", "desktop", "chat-2", "companion")


class RecordingWorkHost:
    """只记账、不跑任务的 WorkHost 替身。"""

    def __init__(self) -> None:
        self.decisions: list[tuple[str, str, str]] = []
        self.pending: list[dict[str, Any]] = []

    def get_approval(self, approval_id: str, scope: Scope) -> dict[str, Any] | None:
        for item in self.pending:
            if item["approval_id"] == approval_id and item["scope"] == scope:
                return item
        return None

    def pending_approvals(self, scope: Scope) -> list[dict[str, Any]]:
        return [item for item in self.pending if item["scope"] == scope]

    async def decide_approval(self, approval_id: str, scope: Scope, decision: str) -> dict[str, Any]:
        self.decisions.append((approval_id, decision, scope.conversation_id))
        for item in self.pending:
            if item["approval_id"] == approval_id:
                item["status"] = "approved" if decision == "approved" else decision
        return {
            "status": "queued" if decision == "approved" else decision,
            "approval_id": approval_id,
        }


class RecordingChatResume:
    """记录"聊天那条链路被要求续跑了几次"。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, action_id: str, scope: Scope, decision: str) -> dict[str, Any]:
        self.calls.append((action_id, decision))
        return {"status": "completed", "reply": "好了", "action_id": action_id}


def _chat_confirmation(store: SQLiteConfirmationStore, scope: Scope) -> str:
    record = store.create(
        run_id="run-1",
        scope=scope,
        tool_call_id="call-1",
        tool="netease__set_volume",
        arguments={"percent": 30},
        title="调整音量",
    )
    return record.action_id


def _coordinator(
    path: Path,
    *,
    work_host: RecordingWorkHost | None = None,
) -> tuple[ApprovalCoordinator, RecordingChatResume, RecordingWorkHost]:
    resume = RecordingChatResume()
    host = work_host or RecordingWorkHost()
    coordinator = ApprovalCoordinator(
        confirmation_store=SQLiteConfirmationStore(path / "core.db"),
        work_host=host,
        resume_chat=resume,
    )
    return coordinator, resume, host


# ── 1. 查询：两种来源合并，但各自带类型 ─────────────────────────────


def test_pending_merges_both_surfaces_and_tags_the_kind(tmp_path: Path) -> None:
    coordinator, _, host = _coordinator(tmp_path)
    chat_id = _chat_confirmation(coordinator.confirmation_store, SCOPE)
    host.pending.append(
        {"approval_id": "work-1", "status": "pending", "scope": SCOPE, "title": "整理文件"}
    )

    items = coordinator.pending(SCOPE)

    kinds = {item["approval_id"]: item["kind"] for item in items}
    assert kinds == {chat_id: APPROVAL_KIND_CHAT, "work-1": APPROVAL_KIND_WORK}
    assert all(item["status"] == "pending" for item in items)


# ── 2. 跨作用域：拿不到 ─────────────────────────────────────────────


async def test_an_approval_from_another_conversation_is_not_visible(tmp_path: Path) -> None:
    """同一个账号、另一个会话的审批，在这个会话里既看不到也办不了。

    这条同时挡住"枚举编号"：`get` 对"不存在"和"不属于你"回同一个 None，
    调用方分不出来，也就问不出别人有哪些审批在等。
    """
    coordinator, resume, host = _coordinator(tmp_path)
    other_id = _chat_confirmation(coordinator.confirmation_store, OTHER)
    host.pending.append({"approval_id": "work-2", "status": "pending", "scope": OTHER})

    assert coordinator.pending(SCOPE) == []
    assert coordinator.get(other_id, SCOPE) is None
    assert coordinator.get("work-2", SCOPE) is None

    with pytest.raises(KeyError):
        await coordinator.decide(other_id, SCOPE, DECISION_CONFIRMED)
    assert resume.calls == []
    assert host.decisions == []


# ── 3. 重复点击只执行一次 ───────────────────────────────────────────


async def test_clicking_twice_only_executes_once(tmp_path: Path) -> None:
    coordinator, resume, _ = _coordinator(tmp_path)
    action_id = _chat_confirmation(coordinator.confirmation_store, SCOPE)

    first = await coordinator.decide(action_id, SCOPE, DECISION_CONFIRMED)
    second = await coordinator.decide(action_id, SCOPE, DECISION_CONFIRMED)

    assert first["status"] == "completed"
    assert second["status"] == "unchanged"
    assert resume.calls == [(action_id, DECISION_CONFIRMED)], "第二次点击又跑了一遍"


# ── 4. 拒绝不执行 ───────────────────────────────────────────────────


async def test_rejection_never_approves_anything(tmp_path: Path) -> None:
    coordinator, resume, host = _coordinator(tmp_path)
    chat_id = _chat_confirmation(coordinator.confirmation_store, SCOPE)
    host.pending.append({"approval_id": "work-3", "status": "pending", "scope": SCOPE})

    await coordinator.decide(chat_id, SCOPE, DECISION_REJECTED)
    await coordinator.decide("work-3", SCOPE, DECISION_REJECTED)

    # 聊天那条：续跑函数被调到了，但带的是 rejected —— 执行与否由它自己按这个决定分流。
    assert resume.calls == [(chat_id, DECISION_REJECTED)]
    # Work 那条：账本收到的是 rejected，绝不是 approved。
    assert host.decisions == [("work-3", DECISION_REJECTED, SCOPE.conversation_id)]
    assert all(item.get("status") != "approved" for item in host.pending)


# ── 5. 重启后还能办 ─────────────────────────────────────────────────


async def test_a_restart_does_not_lose_pending_approvals(tmp_path: Path) -> None:
    first, _, host = _coordinator(tmp_path)
    action_id = _chat_confirmation(first.confirmation_store, SCOPE)
    host.pending.append({"approval_id": "work-4", "status": "pending", "scope": SCOPE})

    # 换一个全新的协调器实例（模拟进程重启），存储还是同一批。
    restarted, resume, _ = _coordinator(tmp_path, work_host=host)

    assert {item["approval_id"] for item in restarted.pending(SCOPE)} == {action_id, "work-4"}
    assert (await restarted.decide(action_id, SCOPE, DECISION_CONFIRMED))["status"] == "completed"
    assert resume.calls == [(action_id, DECISION_CONFIRMED)]


# ── 6. 调用方带不进参数 ─────────────────────────────────────────────


def test_decide_takes_only_an_id_and_a_vote() -> None:
    """决策入口的签名里**不许**出现参数、工具名、run_id 之类的东西。

    这条是静态断言，因为它守的是一个**结构事实**：只要签名里能塞东西，
    就存在"拿一个审批编号配一组新参数去执行"的旁路 ——
    而那种旁路在行为测试里很难被穷举出来（要枚举所有能塞的字段）。
    """
    import inspect

    names = set(inspect.signature(ApprovalCoordinator.decide).parameters) - {"self"}

    assert names == {"approval_id", "scope", "decision"}, names


# ── 7. 端到端：真 loop 挂起 → 协调器决策 ────────────────────────────


class _QueueProvider:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.queue = list(messages)

    async def complete(self, messages: list[dict[str, Any]], **_kwargs: Any) -> Any:
        from agent_core.contracts import ProviderResponse

        message = self.queue.pop(0) if self.queue else {"role": "assistant", "content": "好的"}
        return ProviderResponse(message=message, usage={})


def _loop_resume(loop: Any) -> Any:
    """`AgentLoop.resume` → 协调器要的那张"回执字典"。

    这层适配是**必须显式写出来**的：协调器的约定是"续跑回调返回一个映射"，
    它不认识 `AgentOutcome`（那是 Chat 那条链自己的形状）。
    把 `AgentOutcome` 直接塞进协调器还能跑，就等于让协调器隐式依赖
    Chat 的数据结构 —— 那正是"两套东西粘在一起"的开始。
    """

    async def resume(action_id: str, scope: Scope, decision: str) -> dict[str, Any]:
        outcome = await loop.resume(action_id, decision)
        return {
            "status": outcome.status,
            "reply": outcome.reply,
            "action_id": outcome.action_id,
        }

    return resume


def _high_risk_loop(tmp_path: Path, executed: list[dict[str, Any]]) -> Any:
    """一个会因为高风险工具停下来等确认的 loop。"""
    import json

    from agent_core.contracts import ProviderResponse, ToolSpec
    from agent_core.loop import AgentLoop
    from agent_core.persistence import SQLiteRunStore
    from agent_core.tools import DefaultRiskPolicy, ToolRegistry

    async def handler(arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        executed.append(dict(arguments))
        return {"status": "ok", "output": {"organized": True}}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "file__organize",
            {"type": "function", "function": {"name": "organize", "parameters": {"type": "object"}}},
            handler,
            risk="high",
            confirmation_title="整理文件",
        )
    )
    provider = _QueueProvider(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "file__organize", "arguments": json.dumps({"directory": "D:/x"})},
                    }
                ],
            },
            {"role": "assistant", "content": "整理好了"},
        ]
    )
    return AgentLoop(
        provider,
        registry,
        SCOPE,
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        risk_policy=DefaultRiskPolicy(),
    )


async def test_a_real_pause_can_be_confirmed_through_the_coordinator(tmp_path: Path) -> None:
    """整条链路：loop 挂起 → 协调器确认 → 工具真的跑了一次。"""
    executed: list[dict[str, Any]] = []
    loop = _high_risk_loop(tmp_path, executed)

    paused = await loop.run(AgentRequest("帮我整理文件", scope=SCOPE))
    assert paused.status == "paused", paused.status
    assert executed == [], "还没确认就把文件动了"

    coordinator = ApprovalCoordinator(
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        resume_chat=_loop_resume(loop),
    )
    pending = coordinator.pending(SCOPE)
    assert [item["approval_id"] for item in pending] == [paused.action_id]

    result = await coordinator.decide(paused.action_id, SCOPE, DECISION_CONFIRMED)

    assert result["kind"] == APPROVAL_KIND_CHAT
    assert executed == [{"directory": "D:/x"}], "确认之后工具应该跑一次"
    assert coordinator.pending(SCOPE) == [], "办完的审批不该还挂在待办里"


async def test_a_real_pause_can_be_rejected_and_nothing_runs(tmp_path: Path) -> None:
    """拒绝那条路：状态要真的被改成 rejected，而且一次都不许执行。

    这条专门盯 `AgentLoop.resume` 对"状态已经等于这一票"的容忍 ——
    协调器是**先认领再续跑**的，拒绝进来时状态已经是 rejected 了。
    以前那一分支只容忍 `confirmed`，于是第一次拒绝会被判成"已经不是待确认"，
    结果既没执行也没人知道它为什么没执行。
    """
    executed: list[dict[str, Any]] = []
    loop = _high_risk_loop(tmp_path, executed)

    paused = await loop.run(AgentRequest("帮我整理文件", scope=SCOPE))
    coordinator = ApprovalCoordinator(
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
        resume_chat=_loop_resume(loop),
    )

    await coordinator.decide(paused.action_id, SCOPE, DECISION_REJECTED)

    assert executed == [], "拒绝了还把文件动了"
    assert coordinator.get(paused.action_id, SCOPE)["status"] == "rejected"
    assert coordinator.pending(SCOPE) == []


async def test_an_unknown_decision_word_is_rejected(tmp_path: Path) -> None:
    """Work 那套词（approved / expired）与空串都不许被当成本地词收下。"""
    coordinator, resume, _ = _coordinator(tmp_path)
    action_id = _chat_confirmation(coordinator.confirmation_store, SCOPE)

    for word in ("approved", "expired", "yes", "confirmed ", ""):
        with pytest.raises(ValueError):
            await coordinator.decide(action_id, SCOPE, word)

    assert resume.calls == []
    assert coordinator.get(action_id, SCOPE)["status"] == "pending"
