"""
Agent 生命周期事件的运行时事件总线。

【在系统里的位置】
AgentLoop 是它的主要生产者：每个回合会依次 emit turn.started / chat.delta /
tool.started / portrait.changed / turn.completed 等事件；
前端（SSE）、Web API、任务服务、媒体服务都是订阅者。
也就是说 —— **对话的"过程"是靠这个总线对外广播的**，而"结果"是通过 AgentOutcome 返回的。

【两种订阅粒度】
subscribe 除了事件类型，还可以带 **filters（关键字过滤）**，支持两种键：
- scope 里的键（owner_id / channel / conversation_id / module_id）→ 按身份过滤；
- data 里的键（run_id / turn_id 等）→ 按内容过滤。
只要有一个 filter 键在两边都找不到（见 Event.matches 的 else 分支），就**不匹配**。
这条规则是有意的：写错键名不会"默默全匹配"，而是干脆收不到事件，更容易发现。

【错误隔离】
`emit` 不把订阅者的异常往上抛：
- 同步 handler 抛异常 → 直接吞掉（try/except + pass）；
- 异步 handler → gather 时 return_exceptions=True。
这是刻意的：一个订阅者（比如某个日志钩子）崩了，不该把用户这一轮对话带崩。

Supports synchronous and asynchronous event handlers,
event filtering, and structured event data.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Coroutine, Protocol


class EventType(str, Enum):
    """Agent runtime event types.

    继承 str 是为了直接序列化给前端（JSON 里就是字符串，不需要额外转换）。
    按前缀分五组：
      turn.*        回合的开始/结束/失败/暂停（"暂停"= 等人工确认）
      chat.*        可见文本的流式增量（chat.delta 是打字机效果的数据源）
      portrait.*    立绘表情切换
      tool.*        工具调用的开始/结束/失败
      media.* / task.* / conversation.*  异步任务、定时任务、对话锁
    """
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    TURN_PAUSED = "turn.paused"
    CHAT_STARTED = "chat.started"
    CHAT_DELTA = "chat.delta"
    CHAT_COMPLETED = "chat.completed"
    CHAT_FAILED = "chat.failed"
    PORTRAIT_CHANGED = "portrait.changed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    MEDIA_QUEUED = "media.queued"
    MEDIA_COMPLETED = "media.completed"
    MEDIA_FAILED = "media.failed"
    CONVERSATION_LOCKED = "conversation.locked"
    CONVERSATION_UNLOCKED = "conversation.unlocked"
    TASK_DUE = "task.due"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"
    # 定时任务终态的**刷新通知**：由 TaskResultBridge 在写库成功之后发出。
    # 上面三个 task.completed/failed/cancelled 是**业务终态**，只给内部服务
    # 消费（桥自己、任务存储等）—— 它们带 error / result 这类内部诊断，
    # 不能跨过浏览器边界。前端只认这一条：收到就意味着"回执已经落库，
    # 可以重新拉历史了"。两者分开，终态才不会因为被重发而产生重复气泡。
    TASK_RESULT = "task.result"
    # 后台工作项（WorkHost）的终态 / 待审批回执：由 WorkResultBridge 发出，
    # 前端收到后重新拉对话历史 —— 这是"硬拦说完成后告诉你"的兑现通道。
    WORK_RESULT = "work.result"
    # 管家式过程表达：StewardReporter 写库成功后广播，前端收到后把这句
    # 角色化进度插进当前聊天（替换空泛的"正在思考"）。
    STEWARD_PROGRESS = "steward.progress"


@dataclass
class Event:
    """Runtime event with structured data."""
    type: EventType
    timestamp: datetime
    scope: dict[str, str]  # owner_id, channel, conversation_id, module_id
    # 注意 scope 是 dict 而不是 contracts.Scope：事件要被 JSON 序列化后发给前端，
    # 所以生产者统一用 `asdict(self.scope)` 转成普通字典再塞进来。
    data: dict[str, Any] = field(default_factory=dict)

    def matches(self, **filters: Any) -> bool:
        """Check if event matches all given filters.

        先查 scope 再查 data；两者都没有这个键就返回 False（见上面的说明）。
        多个 filter 之间是"与"关系：全部命中才算匹配。
        """
        for key, value in filters.items():
            if key in self.scope:
                if self.scope[key] != value:
                    return False
            elif key in self.data:
                if self.data[key] != value:
                    return False
            else:
                return False
        return True


# Handler can be sync or async
EventHandler = Callable[[Event], None] | Callable[[Event], Coroutine[Any, Any, None]]


class EventBus(Protocol):
    """Event bus protocol.

    只定接口不定实现，是因为测试里需要可控的假总线（同步、即时、可断言），
    而生产用 InMemoryEventBus（或在别处换成带持久化的实现）。
    """

    def subscribe(
        self,
        event_type: EventType | str,
        handler: EventHandler,
        **filters: Any
    ) -> str:
        """Subscribe to events. Returns subscription ID."""
        ...

    def unsubscribe(self, subscription_id: str) -> bool:
        """Unsubscribe by ID. Returns True if found."""
        ...

    async def emit(self, event: Event) -> None:
        """Emit an event to all matching subscribers."""
        ...

    async def shutdown(self) -> None:
        """Shutdown the event bus."""
        ...


@dataclass
class _Subscription:
    """Internal subscription record."""
    id: str
    event_type: EventType | str
    handler: EventHandler
    filters: dict[str, Any]


class InMemoryEventBus:
    """In-memory event bus with sync/async handler support.

    进程内实现：订阅表只活在内存里，进程重启即清空（重启后重新 subscribe 即可）。
    """

    def __init__(self):
        self._subscriptions: dict[str, _Subscription] = {}
        self._next_id = 0  # 用自增序号生成 sub_N，而不是 uuid：日志里更好读、更好比对
        self._shutdown = False

    def subscribe(
        self,
        event_type: EventType | str,
        handler: EventHandler,
        **filters: Any
    ) -> str:
        """Subscribe to events matching type and optional filters."""
        sub_id = f"sub_{self._next_id}"
        self._next_id += 1

        self._subscriptions[sub_id] = _Subscription(
            id=sub_id,
            event_type=event_type,
            handler=handler,
            filters=filters
        )
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove subscription by ID."""
        # pop(..., None) + is not None：一次拿到"删掉了什么"和"有没有删到"，不用先判存在。
        return self._subscriptions.pop(subscription_id, None) is not None

    async def emit(self, event: Event) -> None:
        """Emit event to all matching subscribers."""
        # 已 shutdown 就当空操作：避免关闭过程中新事件又把订阅者拉起来。
        if self._shutdown:
            return

        # Find matching subscriptions
        tasks = []
        for sub in self._subscriptions.values():
            if sub.event_type != event.type:
                continue
            if not event.matches(**sub.filters):
                continue

            # Call handler (sync or async)
            # 两类 handler 的处理方式不同：
            # 异步的收进 tasks，最后一次性 gather 并发等待；
            # 同步的当场调用，异常直接丢弃，不让它影响 emit 的调用方。
            if asyncio.iscoroutinefunction(sub.handler):
                tasks.append(sub.handler(event))
            else:
                try:
                    sub.handler(event)  # type: ignore
                except Exception:
                    pass  # Don't let handler errors break emission

        # Await all async handlers
        # return_exceptions=True 同样是为了隔离：某个订阅者抛异常不影响其他订阅者。
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Shutdown the event bus."""
        self._shutdown = True
        self._subscriptions.clear()
