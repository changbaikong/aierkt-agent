"""WorkHost 结果回传桥：把后台任务的终态与待审批回执写回发起它的对话。

为什么需要这一层
------------------
路由层硬拦（或模型提交）之后，对话里只留下一句"完成后会告诉你结果"。
但 WorkHost 的事件只喂给工作页的 SSE —— 对话侧没有任何通道。用户不去
工作页，就永远等不到结果：完成了不知道、失败了不知道、卡在
waiting_approval 更不知道。

本模块订阅 ``host.events()``，筛出需要用户知晓的状态，做两件事：

  1. 把一条 assistant 消息写进发起对话（``stable_key`` 幂等，桥重启不重写）；
  2. 向事件总线发 ``work.result``，前端收到后重新拉对话历史。

scope 映射是关键：work item 存的是 work scope（module_id="work"），
而对话历史挂在 chat scope（module_id="companion"）下，所以这里要换掉
module_id 再写 —— owner / channel / conversation_id 三元组保持不变。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict
from datetime import datetime
from typing import Any, Mapping

from .contracts import Scope
from .events import Event, EventBus, EventType
from .steward import (
    PHASE_CANCELLED,
    PHASE_COMPLETED,
    PHASE_FAILED,
    PHASE_INTERRUPTED,
    PHASE_WAITING_APPROVAL,
    StewardContext,
    StewardNarrator,
)

logger = logging.getLogger(__name__)

# 哪些状态需要回传。waiting_approval 必须在内：用户不知道"它在等你"，
# 那句"完成后会告诉你结果"就成了永久悬空。
REPORTABLE_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "waiting_approval"}
)

# 桥写回的消息带的 kind 标记。这是**单一真相**：记忆提取器（memory_extract）
# 靠它把这类系统回执挡在提炼之外 —— 它们是后台状态播报，不是用户的偏好或事实，
# 混进记忆库会污染跨会话的长期记忆。模型上下文则**不过滤**：模型需要看见
# "有个任务在等你确认"，才能在用户问起时说清楚卡在哪一步。
# （它不再需要知道"怎么去批准"—— 模型侧已经没有那个动作了，
# 审批由用户走统一审批控制面。）
WORK_RESULT_KIND = "work_result"

#: 代码任务回执里带上执行者结论的长度上限。只读检查类任务的**唯一**产出就是
#: 这段话；不带它，回执里就只剩"没有生成补丁 / 测试未执行"，模型会把这句读成
#: "这次白跑了"，于是自己换个说法再提交一次（实测发生过）。
_CODE_ANSWER_LIMIT = 400


def code_task_answer(output: Mapping[str, Any]) -> str:
    """执行者自己的结论（Harness 的最终消息，其次摘要）；没有就返回空串。"""
    for key in ("message", "summary"):
        value = str(output.get(key) or "").strip()
        if value:
            return value[:_CODE_ANSWER_LIMIT] + ("…" if len(value) > _CODE_ANSWER_LIMIT else "")
    return ""


def render_work_reply(
    status: str,
    objective: str,
    task_type: str,
    output: dict[str, Any] | None,
    error: str = "",
) -> str:
    """把一个 WorkHost 状态渲染成对话里的一句人话。纯函数，方便单测。"""
    output = dict(output or {})
    label = (objective or "").strip() or task_type or "后台任务"
    if status == "completed":
        if task_type == "code.agent":
            verification = str(output.get("verification_status") or "")
            verified = verification == "verified"
            patched = bool(output.get("patch"))
            applied = output.get("apply_status")
            answer = code_task_answer(output)
            # 「原项目没被动过」只有在真核对过之后才能说；扫不动就照实说扫不动。
            scope_status = str((output.get("project_scope") or {}).get("status") or "intact")
            scope_unverified = scope_status == "unverifiable"
            if patched:
                if verified:
                    test_result = "可信测试通过"
                elif verification == "not_run":
                    # 没配可信测试 ≠ 测试没通过。读成"未通过"会把一次成功的
                    # 修改说成可疑结果。
                    test_result = "未配置可信测试"
                else:
                    test_result = "可信测试未通过或未执行"
                if scope_unverified:
                    location = "原项目是否被改动没能核对"
                elif applied == "patch_only":
                    location = "原项目未改动"
                else:
                    location = "请在工作页查看回写状态"
                text = f"「{label}」完成了：{test_result}，补丁已生成；{location}。"
            else:
                # 只读任务本来就不该有补丁：说"没有生成补丁"听起来像失败。
                if scope_unverified:
                    text = f"「{label}」完成了：改动情况没能核对。"
                elif applied == "patch_only":
                    text = f"「{label}」完成了：这次没有改动任何文件。"
                else:
                    text = f"「{label}」完成了。"
            return f"{text}\n{answer}" if answer else text
        if task_type == "file.list":
            total = output.get("total")
            if isinstance(total, int):
                return f"「{label}」完成了，共 {total} 项。"
        if task_type == "file.organize":
            applied = output.get("applied")
            if isinstance(applied, list):
                return f"「{label}」整理完成，共移动 {len(applied)} 个文件。"
        if task_type == "screen.capture":
            path = output.get("path") or output.get("artifact_path")
            if path:
                return f"截屏完成，文件保存在 {path}。"
        if task_type == "system.volume.adjust":
            percent = output.get("percent", output.get("value"))
            if percent is not None:
                return f"音量已调到 {percent}%。"
            return "音量已调整完成。"
        return f"「{label}」完成了。"
    if status == "failed":
        return f"「{label}」失败了：{error or '原因未知'}"
    if status == "cancelled":
        return f"「{label}」已取消。"
    if status == "interrupted":
        return f"「{label}」被中断了（服务重启），可以在工作页重试。"
    if status == "waiting_approval":
        return f"「{label}」需要你确认后才会执行：可以到工作页审批，或直接在这里说『批准』/『拒绝』。"
    return ""


def safe_reason_text(error: str) -> str:
    """把失败原因收拾成"能给人看"的一句短话。

    后台任务的 error 可能是异常栈、路径、SQL 片段 —— 直接写进聊天气泡
    既难看又可能泄密。这里只留第一行、截到 24 字，并把多行 / 花括号 /
    "Traceback" 这类特征整体丢掉：拿不准就**不说原因**，而不是说错。
    """
    value = re.sub(r"\s+", " ", str(error or "")).strip()
    if not value:
        return ""
    lowered = value.lower()
    if "traceback" in lowered or "{" in value or "file \"" in lowered:
        return ""
    value = value.split(";")[0].strip()
    return value[:24] + "…" if len(value) > 24 else value


def _status_phase(status: str) -> str:
    """后台状态 → 管家阶段。

    cancelled 必须单独一类：它是"对方叫停/审批被拒"，不是失败。
    混进 failed 会说出"这次没能把内容取回来"这种与事实相反的话。
    """
    if status == "completed":
        return PHASE_COMPLETED
    if status == "waiting_approval":
        return PHASE_WAITING_APPROVAL
    if status == "cancelled":
        return PHASE_CANCELLED
    if status == "interrupted":
        return PHASE_INTERRUPTED
    return PHASE_FAILED


#: 计划（ControlPlan）事件 → 后台状态。
#: 计划事件不带 scope / objective（只有 plan_id），所以桥要自己补上，
#: 否则"多步计划跑完了"这件事在对话里永远没人说。
PLAN_EVENT_STATUSES = {
    "plan.completed": "completed",
    "plan.failed": "failed",
    "plan.cancelled": "cancelled",
    "plan.interrupted": "interrupted",
    "step.waiting_approval": "waiting_approval",
}


class WorkResultBridge:
    """把 WorkHost 事件投影回对话历史 + 事件总线。

    用法::

        bridge = WorkResultBridge(host, conversation_repository, event_bus)
        task = asyncio.create_task(bridge.run())
        ...
        task.cancel()  # 关停
    """

    def __init__(
        self,
        host: Any,
        repository: Any,
        event_bus: EventBus | None,
        *,
        chat_module_id: str = "companion",
        steward: StewardNarrator | None = None,
        conversation_manager: Any = None,
        progress_hook: Any = None,
    ) -> None:
        self.host = host
        self.repository = repository
        self.event_bus = event_bus
        self.chat_module_id = chat_module_id
        # 后台任务的结果同样要说人话："我看完了，……" / "这次没能把内容取回来"。
        # 没有 narrator 就退回 render_work_reply 那句确定性播报。
        # 这里不走 StewardReporter：桥自己有 stable_key 与 work.result 事件，
        # 再套一层 Reporter 只会让同一条结果落两次库。
        self.steward = steward
        self.conversation_manager = conversation_manager
        # 终态收口钩子（代码任务过程桥的 close_attempt）：写回执**之前**先关掉
        # 那次执行的过程进度 —— 否则待发的那条进度可能排到最终回执后面。
        self.progress_hook = progress_hook

    async def run(self) -> None:
        """订阅 Host 事件流直到被取消。单条失败不终止桥。"""
        async for event in self.host.events():
            try:
                await self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("work result bridge failed on event: %r", event)

    async def _phrase(self, status: str, objective: str, facts: str) -> str:
        """把确定性事实播报换成同一人物的说法。

        输入只有"已经确认的事实"（render_work_reply 的产出），
        失败/中断时这些事实里不含任何异常栈（见 safe_reason_text）。
        """
        if self.steward is None:
            return facts
        try:
            line = await self.steward.narrate(
                StewardContext(
                    phase=_status_phase(status),
                    goal=objective,
                    stage="后台这件事刚落地",
                    evidence=(facts,),
                )
            )
        except Exception:
            logger.debug("work result narration failed, using deterministic reply", exc_info=True)
            return facts
        # narrator 永远有话说（不合格就回退模板），空串只可能是意外 —— 那就用事实播报。
        return line or facts

    async def _handle(self, event: dict[str, Any]) -> None:
        raw_type = str(event.get("type") or "")
        status = PLAN_EVENT_STATUSES.get(raw_type, raw_type)
        if status not in REPORTABLE_STATUSES:
            return
        plan_id = str(event.get("plan_id") or "")
        conversation_id = str(event.get("conversation_id") or "")
        owner_id = str(event.get("owner_id") or "")
        channel = str(event.get("channel") or "web")
        objective = str(event.get("objective") or "")
        if not conversation_id or not owner_id:
            # 计划事件只带 plan_id：回账本把发起它的对话找回来。
            resolved = self._resolve_plan(plan_id)
            if resolved is None:
                # 反查不到发起对话（老 Host / 元数据缺失）：宁可少通知，不写错地方。
                return
            scope, plan_objective = resolved
            conversation_id = scope.conversation_id
            owner_id = scope.owner_id
            channel = scope.channel
            objective = objective or plan_objective
        payload = dict(event.get("payload") or {})
        task_type = str(event.get("task_type") or "")
        objective = objective or str(event.get("objective") or "")
        attempt_id = str(event.get("attempt_id") or "")
        if self.progress_hook is not None:
            # 先把这条终态对应的过程进度关掉（并等已在落库路上的那条收口），
            # 再写下面这条最终回执：顺序反了就会出现"回执之后又冒出一条正在改代码"。
            try:
                await self.progress_hook(attempt_id)
            except Exception:
                logger.debug("work progress hook failed", exc_info=True)
        # 失败原因先过一遍清洗再进提示词：异常栈不许出现在气泡里。
        reason = safe_reason_text(str(payload.get("error") or ""))
        render_payload: Mapping[str, Any] = payload
        nested_output = payload.get("output")
        if task_type == "code.agent" and isinstance(nested_output, Mapping):
            render_payload = {**payload, **dict(nested_output)}
        facts = render_work_reply(
            status,
            objective,
            task_type,
            dict(render_payload),
            reason,
        )
        if not facts:
            return
        reply = facts if task_type == "code.agent" else await self._phrase(status, objective, facts)
        chat_scope = Scope(owner_id, channel, conversation_id, self.chat_module_id)
        # 计划事件没有 attempt_id：退到 plan_id，否则同一对话下所有计划的
        # 回执会共用一个 stable_key，只有第一条写得进去。
        subject = attempt_id or str(event.get("work_item_id") or "") or plan_id
        stable_key = f"work-result:{subject}:{status}"
        message = {
                "role": "assistant",
                "content": reply,
                "kind": WORK_RESULT_KIND,
                "work_item_id": str(event.get("work_item_id") or ""),
                "plan_id": plan_id,
                "step_id": str(event.get("step_id") or ""),
                "task_type": task_type,
                "work_status": status,
        }
        if self.conversation_manager is not None:
            accepted = self.conversation_manager.append_message(
                chat_scope,
                "assistant",
                reply,
                stable_key=stable_key,
                touch_activity=False,
                **{key: value for key, value in message.items() if key not in {"role", "content"}},
            )
        else:
            accepted = self.repository.append_message(
                chat_scope,
                message,
                stable_key=stable_key,
                # 系统回执不算用户活动：不刷新 last_activity_at，
                # 否则后台任务完成会把一个没人说话的对话顶到会话列表最前面。
                touch_activity=False,
            )
        if not accepted:
            return  # 同一条结果已经写过（stable_key 撞了），不再重复通知
        if self.event_bus is not None:
            await self.event_bus.emit(
                Event(
                    type=EventType.WORK_RESULT,
                    timestamp=datetime.now(),
                    scope=asdict(chat_scope),
                    data={
                        "work_item_id": str(event.get("work_item_id") or ""),
                        "attempt_id": attempt_id,
                        "plan_id": plan_id,
                        "status": status,
                        "task_type": task_type,
                        "reply": reply,
                    },
                )
            )

    def _resolve_plan(self, plan_id: str) -> tuple[Scope, str] | None:
        """按 plan_id 回账本查出计划属于哪个对话。

        计划事件是"广播"性质的（前端靠它刷新工作页），不带发起对话的四元组；
        桥要把它写回对话，只能反查。查不到就返回 None（调用方直接放弃通知）。
        """
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
        if plan is None:
            return None
        scope = getattr(plan, "scope", None)
        if scope is None or not getattr(scope, "conversation_id", "") or not getattr(scope, "owner_id", ""):
            return None
        return scope, str(getattr(plan, "objective", "") or "")
