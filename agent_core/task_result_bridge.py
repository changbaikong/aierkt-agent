"""定时任务终态回执桥：把任务结果写成对话里一句**公开**的话。

为什么需要这一层
------------------
定时任务跑完（或被取消、或失败）时，TaskService 会往事件总线上发一条
``task.completed`` / ``task.failed`` / ``task.cancelled``。这条事件带的是
**内部诊断**：``error`` 是 ``RuntimeError: [Errno 2] No such file: C:\\...``
这种异常原文，``result`` 是子 loop 里模型的原话。

过去 web_api 里有个 ``_task_event_message()``，直接把这些原文拼成
"任务失败：标题（RuntimeError: ... C:\\Users\\...）" 写进对话历史 —— 用户会
在聊天气泡里看到异常类名、本机绝对路径，有时候还带换行和堆栈片段。
而且它从来没被 lifespan 调用过（只在测试里被叫），真实链路上任务结果
在对话里既不出现、也无法保证干净。

本模块做三件事，每件都对应上面一个坑：

  1. **单点出口**：所有任务终态在对话里的说法只由 :func:`public_task_result`
     生成。它收不到 error，签名上就没有这个口子。
  2. **固定措辞 + 白名单摘要**：成功句后面能跟的"业务输出"必须先过
     :func:`public_safe_summary` 的洁净度检查；失败/取消一律说固定的话，
     不解释原因。原因是内部诊断，只进日志和 TaskStore。
  3. **幂等写回**：``stable_key = task-result:{task_id}:{run_id}:{status}``，
     桥重启不重写；同一终态被重放时，桥自己记的 key 也能挡住第二次写库。

写库成功之后，桥发一条**独立的** ``task.result`` 刷新通知（不重发业务终态 ——
见 :meth:`TaskResultBridge._broadcast`）。三个业务终态仍然只由 TaskService
产生一次，前端不再直接消费它们。

scope 口径：任务事件带的 scope 就是**发起它的那个对话**的 scope，
所以直接照着写回去，不做 module 转换（这点和 WorkResultBridge 不同 ——
那边 work item 挂在 ``module_id="work"`` 分区，才需要换成 companion）。
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from .contracts import Scope
from .events import Event, EventBus, EventType
from .fastpath import is_safe_public_text

logger = logging.getLogger(__name__)

#: 桥写回的消息带的 kind。这是**单一真相**：记忆提取器（memory_extract）
#: 靠它把这类系统回执挡在长期记忆之外 —— "「每晚总结」已经办好了"是后台
#: 状态播报，不是用户的偏好或事实，混进记忆库只会越用越脏。
#: 模型上下文**不过滤**：用户接着说"那再设一个"时，模型需要看见刚才发生了什么。
TASK_RESULT_KIND = "task_result"

STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

#: 终态事件 → 公开状态。三个都在内：取消也是一次结果，用户必须知道。
TERMINAL_EVENT_STATUSES: dict[EventType, str] = {
    EventType.TASK_COMPLETED: STATUS_COMPLETED,
    EventType.TASK_FAILED: STATUS_FAILED,
    EventType.TASK_CANCELLED: STATUS_CANCELLED,
}

#: 固定公开措辞。失败句刻意**不含原因**：原因是内部诊断，说了只会泄漏
#: 异常类名 / 路径 / Provider 名，对用户没有任何可操作价值。
_COMPLETED_LINE = "「{title}」已经办好了。"
_FAILED_LINE = "「{title}」这次没办成。已经记录了情况，稍后可以重试。"
_CANCELLED_LINE = "「{title}」已经停下来了。"

#: 标题长度上限。标题是用户自己起的，但派生的后台任务标题可能很长，
#: 直接塞进气泡会把话淹掉。
_TITLE_LIMIT = 40
#: 成功摘要上限。它只是一句补充说明，不该喧宾夺主。
_SUMMARY_LIMIT = 60

_TITLE_FALLBACK = "未命名任务"

#: 进程内已处理 key 的上限。超过就整体清空（见 TaskResultBridge._handle）——
#: 它只防自激，不承担长期幂等；长期幂等由落库层的 stable_key 负责。
_HANDLED_LIMIT = 4096

_WHITESPACE = re.compile(r"\s+")


def _flatten(value: Any) -> str:
    """折成一行：换行、制表、连续空格都压掉。"""
    return _WHITESPACE.sub(" ", str(value or "")).strip()


def public_title(value: Any) -> str:
    """任务标题 → 能进气泡的短标题。过不了洁净度检查就用兜底名。"""
    text = _flatten(value)
    if not text or not is_safe_public_text(text):
        return _TITLE_FALLBACK
    return text[:_TITLE_LIMIT]


def public_safe_summary(value: Any) -> str:
    """已确认的业务输出 → 一句补充说明；不干净就整段丢掉。

    "整段丢掉"而不是"截断清洗"是刻意的：摘要里混进路径或异常原文时，
    截断只会把泄漏截短一点，还是泄漏。宁可少说一句。
    """
    text = _flatten(value)
    if not text or not is_safe_public_text(text):
        return ""
    return text[:_SUMMARY_LIMIT]


def public_task_result(
    status: str,
    title: Any,
    result_code: str = "",
    safe_summary: Any = "",
) -> str:
    """任务终态 → 用户看到的那句话。纯函数，方便单测。

    ``result_code`` 是**稳定错误码**（timeout / permission / permanent /
    transient），不为措辞服务 —— 它是给日志和前端分类用的。这里收下它
    只是为了让调用方不必分两个函数，绝不把它拼进句子。

    未识别的状态返回空串：调用方据此跳过写回，而不是硬凑一句话。
    """
    code = _flatten(result_code)
    label = public_title(title)
    if status == STATUS_COMPLETED:
        line = _COMPLETED_LINE.format(title=label)
        summary = public_safe_summary(safe_summary)
        text = f"{line}{summary}" if summary else line
    elif status == STATUS_FAILED:
        text = _FAILED_LINE.format(title=label)
    elif status == STATUS_CANCELLED:
        text = _CANCELLED_LINE.format(title=label)
    else:
        return ""
    # 双保险：模板是本地写死的，但"写死"不等于"写对"，标题又是外部输入。
    if not is_safe_public_text(text):
        return ""
    return text


def public_error_code(raw_error: Any) -> str:
    """内部 error 原文 → 稳定错误码。HTTP 任务详情用它替代 last_error。

    只做形状识别、不回传任何原文：上游存的可能是
    ``RuntimeError: [Errno 2] No such file: C:\\...``，用户和前端都不该看到它，
    但"上次是因为超时还是因为权限"这件事值得留一个可枚举的值。
    """
    text = _flatten(raw_error).lower()
    if not text:
        return ""
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "permission" in text or "denied" in text:
        return "permission"
    if "unavailable" in text or "not found" in text or "no such" in text:
        return "unavailable"
    return "internal"


class TaskResultBridge:
    """把任务终态事件投影成对话历史里的一句公开回执。

    用法::

        bridge = TaskResultBridge(event_bus, conversation_repository,
                                  conversation_manager=manager)
        bridge.subscribe()
        ...
        bridge.unsubscribe()  # 关停

    与 WorkResultBridge 的差别：那个拉的是 ``host.events()`` 异步流，
    需要自己开一个 task 去消费；这个订阅的是进程内事件总线，订阅本身
    就是接入方式（总线支持 async handler），不需要常驻任务。
    """

    def __init__(
        self,
        event_bus: EventBus | None,
        repository: Any = None,
        *,
        conversation_manager: Any = None,
        on_terminal: Any = None,
    ) -> None:
        self.event_bus = event_bus
        self.repository = repository
        #: 终态事件的第二个消费口：收到「任务真的结束了」的通知，与写回执互不影响。
        #: 传 None（默认）时行为与以前完全一致 —— 这是个只加不改的挂点。
        #: 回调拿到的 payload 只含公开字段：task_id / run_id / status / title /
        #: result / error_code，异常原文不进这里。
        self.on_terminal = on_terminal
        # 有 manager 时优先走它：除了落库，它还要更新进程内对话缓存
        # （否则同进程里紧接着的下一轮对话看不到这条回执）。
        self.conversation_manager = conversation_manager
        self._subscription_ids: list[str] = []
        #: 进程内已处理过的 stable_key。
        #: 落库层（ConversationRepository / ConversationManager）确实按
        #: stable_key 幂等，但"写回 → 广播 → 自己再收到"这个环的终止条件
        #: 完全押在它身上：一旦哪天换了个不幂等的实现，桥就会无限自激
        #: （真事：写这一层时用一个"永远返回 True"的替身，测试直接挂死）。
        #: 环的终止条件必须握在桥自己手里，顺带还省掉一次重复落库往返。
        self._handled: set[str] = set()

    # ------------------------------------------------------------------
    # 接入 / 断开
    # ------------------------------------------------------------------
    def subscribe(self) -> list[str]:
        """订阅三种终态事件。重复调用不会重复订阅。"""
        if self.event_bus is None or self._subscription_ids:
            return list(self._subscription_ids)
        self._subscription_ids = [
            self.event_bus.subscribe(event_type, self._on_event)
            for event_type in TERMINAL_EVENT_STATUSES
        ]
        return list(self._subscription_ids)

    def unsubscribe(self) -> None:
        if self.event_bus is None:
            return
        for subscription_id in self._subscription_ids:
            try:
                self.event_bus.unsubscribe(subscription_id)
            except Exception:
                logger.debug("task result bridge unsubscribe failed", exc_info=True)
        self._subscription_ids = []

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    async def _on_event(self, event: Any) -> None:
        """单条事件失败不终止桥，也不让异常冒到 emit 的调用方。"""
        try:
            await self._handle(event)
        except Exception:
            logger.exception("task result bridge failed on event: %r", event)

    async def _handle(self, event: Any) -> None:
        status = TERMINAL_EVENT_STATUSES.get(getattr(event, "type", None))
        if status is None:
            return
        scope_data = dict(getattr(event, "scope", None) or {})
        owner_id = str(scope_data.get("owner_id") or "")
        conversation_id = str(scope_data.get("conversation_id") or "")
        if not owner_id or not conversation_id:
            # 没身份就没法写回任何对话 —— 宁可少写一条，也不能写错地方。
            return
        scope = Scope(
            owner_id,
            str(scope_data.get("channel") or "web"),
            conversation_id,
            str(scope_data.get("module_id") or "companion"),
        )
        data = dict(getattr(event, "data", None) or {})
        task_id = str(data.get("task_id") or "")
        run_id = str(data.get("run_id") or "")
        result_code = str(data.get("error_code") or data.get("result_code") or "")
        # 成功句后面的补充说明只能来自**已确认的业务输出**（子 loop 的最终回复），
        # 而且还要过洁净度检查；失败/取消一个字的内部原因都不许带。
        safe_summary = data.get("result") if status == STATUS_COMPLETED else ""
        reply = public_task_result(status, data.get("title"), result_code, safe_summary)
        if not reply:
            return
        if status == STATUS_FAILED:
            # 内部诊断的唯一出口：日志。异常原文、路径、Provider 名到此为止。
            logger.info(
                "scheduled task failed: task_id=%s run_id=%s code=%s detail=%s",
                task_id, run_id, result_code, _flatten(data.get("error"))[:200],
            )
        stable_key = f"task-result:{task_id}:{run_id}:{status}"
        if stable_key in self._handled:
            return  # 这个终态已经处理过（同一条终态事件被重放）
        if len(self._handled) > _HANDLED_LIMIT:
            # 进程内记忆只是个"防自激"的短期集合，不是账本。撑到上限就整体
            # 清空：清空后最坏情况是重放一条旧事件，那时落库层的 stable_key
            # 幂等兜底，不会重复写进对话。
            self._handled.clear()
        self._handled.add(stable_key)
        if not self._write(scope, reply, stable_key, task_id, run_id, status, result_code):
            return  # 落库层说"这条写过了"（桥重启后重放旧事件的情形），不再广播
        await self._broadcast(scope, status, task_id, run_id, result_code)
        await self._notify_terminal(scope, status, data)

    # ------------------------------------------------------------------
    # 落库 / 广播
    # ------------------------------------------------------------------
    def _write(
        self,
        scope: Scope,
        reply: str,
        stable_key: str,
        task_id: str,
        run_id: str,
        status: str,
        result_code: str,
    ) -> bool:
        """写回原对话。返回 False 表示"这条已经写过了"。"""
        extra = {
            "kind": TASK_RESULT_KIND,
            "task_id": task_id,
            "run_id": run_id,
            "task_status": status,
            "result_code": result_code,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if self.conversation_manager is not None:
            append = getattr(self.conversation_manager, "append_message", None)
            if not callable(append):
                return False
            # touch_activity=False：系统回执不该把没人说话的对话顶到列表最前面。
            return bool(append(scope, "assistant", reply, stable_key=stable_key, touch_activity=False, **extra))
        if self.repository is None:
            # 没有落库能力就什么都不做 —— 广播出去也没人负责去重，会自激。
            return False
        message = {"role": "assistant", "content": reply, **extra}
        return bool(self.repository.append_message(scope, message, stable_key=stable_key, touch_activity=False))

    async def _notify_terminal(self, scope: "Scope", status: str, data: dict) -> None:
        """把终态告诉第二个消费口（业务结果记忆入口）。

        这里**只报事实**：任务号、终态、标题与已确认的产出摘要。
        异常原文、绝对路径不进 payload —— 那是日志的事。
        回调抛异常不影响已经写好的回执：捕获并记一条日志就结束。
        """
        if self.on_terminal is None:
            return
        payload = {
            "task_id": str(data.get("task_id") or ""),
            "run_id": str(data.get("run_id") or ""),
            "status": status,
            "title": data.get("title"),
            "conclusion": data.get("result") if status == STATUS_COMPLETED else "",
            "failure_reason": "",
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        }
        try:
            outcome = self.on_terminal(payload, scope)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:  # noqa: BLE001 - 记忆入口失败不影响任务回执
            logger.warning("task terminal sink failed", exc_info=True)

    async def _broadcast(
        self,
        scope: Scope,
        status: str,
        task_id: str,
        run_id: str,
        result_code: str,
    ) -> None:
        """写库成功后发一条 ``task.result``：告诉前端"可以重新拉历史了"。

        为什么是**新事件**而不是重发原终态：原终态（task.completed/failed/
        cancelled）是业务事件，TaskService 只发一次，订阅者（包括前端）每收到
        一次就该理解为"发生了一件新的事"。过去桥把它原样重发一次，前端因此
        在同一个终态上收到两帧、刷两次，也把"业务事件"和"刷新通知"两种语义
        混在了一个通道里。现在两者分开：终态给内部服务消费，``task.result``
        只给前端当刷新信号。

        为什么不会自激：桥只订阅三个终态事件，**不订阅** ``task.result``，
        所以这条通知不会被自己再收一次。``self._handled`` 仍然保留 —— 它防的是
        同一条终态事件被重放（重放时根本走不到这里）。

        为什么只有这五个字段：前端拿到它要做的唯一一件事就是重新拉一次历史。
        error / result / 标题 / 本机路径全都不需要；少给一个字段，就少一处
        可能把内部诊断漏到浏览器里的口子。
        """
        if self.event_bus is None:
            return
        try:
            await self.event_bus.emit(
                Event(
                    type=EventType.TASK_RESULT,
                    timestamp=datetime.now(timezone.utc),
                    scope=asdict(scope),
                    data={
                        "task_id": task_id,
                        "run_id": run_id,
                        "status": status,
                        "result_code": result_code,
                        "history_written": True,
                    },
                )
            )
        except Exception:
            logger.debug("task result broadcast failed", exc_info=True)
