"""Asynchronous per-conversation turn queues with a bounded runtime cache."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Protocol

from .contracts import AccountTaskScope, AgentOutcome, AgentRequest, Scope
from .conversation_repository import ConversationRepository
from .events import Event, EventBus, EventType
from .intent import IntentStateStore
from .presentation_policy import expression_for_event, expression_for_user_message


@dataclass
class ConversationLock:
    """Lock information for a paused conversation."""

    conversation_id: str
    locked_at: float
    reason: str
    action_id: str
    owner_id: str = ""
    module_id: str = ""
    channel: str = ""


class ConversationSession(Protocol):
    async def run(self, request: AgentRequest) -> AgentOutcome: ...


SessionFactory = Callable[[Scope], ConversationSession]


@dataclass
class TurnHandle:
    """Receipt returned immediately after a turn is accepted or rejected."""

    accepted: bool
    turn_id: str
    position: int
    result: asyncio.Future[AgentOutcome]
    reason: str = ""


@dataclass
class _QueuedTurn:
    turn_id: str
    request: AgentRequest
    result: asyncio.Future[AgentOutcome]


@dataclass
class _ConversationState:
    scope: Scope
    queue: asyncio.Queue[_QueuedTurn]
    history: list[dict[str, Any]] = field(default_factory=list)
    worker: asyncio.Task[None] | None = None
    active: bool = False
    pending_count: int = 0
    last_activity: float = field(default_factory=time.monotonic)


class ConversationManager:
    """Run turns serially per complete Scope and cache only active runtime state."""

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        max_queue_size: int = 20,
        confirmation_store: Any = None,
        event_bus: EventBus | None = None,
        conversation_repository: ConversationRepository | None = None,
        intent_state_store: IntentStateStore | None = None,
        idle_ttl_seconds: float | None = None,
        turn_timeout_seconds: float | None = None,
    ) -> None:
        if max_queue_size < 1:
            raise ValueError("max_queue_size must be at least 1")
        self.session_factory = session_factory
        self.max_queue_size = max_queue_size
        self.confirmation_store = confirmation_store
        self.event_bus = event_bus
        self.conversation_repository = conversation_repository
        # 意图状态是"这段对话正在聊什么"的运行期线索，会话没了它就该一起没。
        # 清它的责任放在管理器（而不是 ConversationRepository）：让存储层去
        # import 意图模块会造出一条反向依赖，而生命周期编排本来就属于运行时。
        self.intent_state_store = intent_state_store
        self.idle_ttl_seconds = float(
            os.getenv("CONVERSATION_IDLE_TTL_SECONDS", "900") if idle_ttl_seconds is None else idle_ttl_seconds
        )
        if self.idle_ttl_seconds <= 0:
            raise ValueError("idle_ttl_seconds must be positive")
        self.turn_timeout_seconds = float(
            os.getenv("CONVERSATION_TURN_TIMEOUT_SECONDS", "180")
            if turn_timeout_seconds is None else turn_timeout_seconds
        )
        if self.turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds must be positive")
        self._states: dict[Scope, _ConversationState] = {}
        # Scope keys are authoritative. String keys preserve old in-process
        # callers that did not pass a Scope; web/API code never writes them.
        self._locks: dict[Scope | str, ConversationLock] = {}
        # 事件发射任务的强引用集：asyncio 只持弱引用，裸 create_task 的任务可能
        # 在执行中被 GC 掉（事件悄悄丢失），其异常也无人 retrieve（刷警告）。
        self._pending_emits: set[asyncio.Task[None]] = set()
        self._state_lock = asyncio.Lock()
        self._closed = False

    @staticmethod
    def _cancelled_outcome(turn_id: str) -> AgentOutcome:
        return AgentOutcome("cancelled", turn_id, reply="Turn cancelled before completion.")

    @staticmethod
    def _failed_outcome(turn_id: str, exc: Exception) -> AgentOutcome:
        return AgentOutcome("failed", turn_id, reply="回复生成失败，请稍后重试。")

    @staticmethod
    def _new_future() -> asyncio.Future[AgentOutcome]:
        return asyncio.get_running_loop().create_future()

    @staticmethod
    def _cap_history(history: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
        return history[-maximum:]

    def _rejected(self, reason: str) -> TurnHandle:
        turn_id = uuid.uuid4().hex
        result = self._new_future()
        result.set_result(AgentOutcome("rejected", turn_id, reply=reason))
        return TurnHandle(False, turn_id, 0, result, reason)

    async def _state_for(self, scope: Scope) -> _ConversationState:
        async with self._state_lock:
            state = self._states.get(scope)
            if state is not None:
                return state
            history = self.conversation_repository.history(scope) if self.conversation_repository is not None else []
            if self.conversation_repository is not None:
                self.conversation_repository.ensure(scope)
            state = _ConversationState(
                scope,
                asyncio.Queue(maxsize=self.max_queue_size + 1),
                history=self._cap_history(history, self._history_limit()),
            )
            self._states[scope] = state
            return state

    def _history_limit(self) -> int:
        return self.conversation_repository.history_max_messages if self.conversation_repository is not None else 1000

    def _scope_lock(self, scope: Scope) -> ConversationLock | None:
        return self._locks.get(scope)

    def _legacy_lock(self, scope: Scope) -> ConversationLock | None:
        return self._locks.get(scope.conversation_id)

    def _durable_lock(self, scope: Scope) -> Any:
        if self.confirmation_store is None:
            return None
        active_lock = getattr(self.confirmation_store, "active_lock", None)
        if not callable(active_lock):
            return None
        try:
            lock = active_lock(scope)
        except (AttributeError, TypeError):
            lock = active_lock(AccountTaskScope(scope.owner_id, scope.module_id))
        if lock is None:
            return None
        if (
            lock.account_id == scope.owner_id
            and lock.channel == scope.channel
            and lock.conversation_id == scope.conversation_id
            and lock.module_id == scope.module_id
        ):
            return lock
        return None

    def _has_pending_confirmation(self, scope: Scope) -> bool:
        if self._scope_lock(scope) is not None:
            return True
        if self.confirmation_store is None:
            return False
        pending = getattr(self.confirmation_store, "pending_for_scope", None)
        return bool(pending(scope)) if callable(pending) else self._durable_lock(scope) is not None

    async def submit(self, request: AgentRequest) -> TurnHandle:
        if self._closed:
            return self._rejected("conversation manager is shutting down")
        scope = request.scope or Scope()
        scope_lock = self._scope_lock(scope)
        if scope_lock is not None:
            return self._rejected(f"会话已锁定：{scope_lock.reason}，请先完成 action {scope_lock.action_id}")
        legacy_lock = self._legacy_lock(scope)
        if legacy_lock is not None:
            if legacy_lock.reason != "waiting_confirmation" or self.confirmation_store is not None:
                return self._rejected(f"会话已锁定：{legacy_lock.reason}，请先完成 action {legacy_lock.action_id}")
        durable_lock = self._durable_lock(scope)
        if durable_lock is not None:
            self._locks.setdefault(
                scope,
                ConversationLock(
                    scope.conversation_id,
                    time.time(),
                    durable_lock.reason,
                    durable_lock.action_id,
                    scope.owner_id,
                    scope.module_id,
                    scope.channel,
                ),
            )
            return self._rejected(f"会话已锁定：{durable_lock.reason}，请先完成 action {durable_lock.action_id}")
        state = await self._state_for(scope)
        if state.pending_count >= self.max_queue_size + 1:
            return self._rejected(f"conversation queue is full (max {self.max_queue_size} pending turns)")
        turn_id = uuid.uuid4().hex
        result = self._new_future()
        await state.queue.put(_QueuedTurn(turn_id, replace(request, scope=scope), result))
        state.pending_count += 1
        state.last_activity = time.monotonic()
        position = state.pending_count
        if self.event_bus:
            await self.event_bus.emit(Event(
                type=EventType.TURN_STARTED,
                timestamp=datetime.now(),
                scope=asdict(scope),
                data={"turn_id": turn_id, "position": position},
            ))
            await self.event_bus.emit(Event(
                type=EventType.CHAT_STARTED,
                timestamp=datetime.now(),
                scope=asdict(scope),
                data={"turn_id": turn_id, "position": position},
            ))
            expression, reason = expression_for_user_message(request.user_text)
            await self.event_bus.emit(Event(
                type=EventType.PORTRAIT_CHANGED,
                timestamp=datetime.now(),
                scope=asdict(scope),
                data={"turn_id": turn_id, "expression": expression, "portrait_key": expression, "phase": "thinking", "reason": reason},
            ))
        if state.worker is None or state.worker.done():
            state.worker = asyncio.create_task(self._worker(state), name=f"conversation-worker-{scope.conversation_id}")
        return TurnHandle(True, turn_id, position, result)

    def _persist_delta(
        self,
        state: _ConversationState,
        public_messages: list[dict[str, Any]],
        request_history: list[dict[str, Any]],
        turn_id: str,
    ) -> None:
        public = [dict(item) for item in public_messages if item.get("role") != "system"]
        prefix_length = len(request_history) if public[:len(request_history)] == request_history else 0
        for index, message in enumerate(public[prefix_length:]):
            stable_key = f"turn:{turn_id}:{index}"
            if self.conversation_repository is None:
                state.history.append(message)
                continue
            message_id = self.conversation_repository.insert_message(
                state.scope, message, stable_key=stable_key
            )
            if message_id is None:
                # stable_key 撞车 = 这条已经在账本里了，内存侧也不要重复追加。
                continue
            # 内存历史必须带上 message_id：舞台的播放游标按它前进，
            # 缺了这个字段，刚播完的助手消息在前端既无法确认、也算不出下一条。
            # 只额外加这一个键 —— 多带别的字段会让下一轮的 request_history
            # 与本轮 public 前缀比较失败，历史消息会被当成新消息重复落库。
            state.history.append({**message, "message_id": message_id})
        state.history = self._cap_history(state.history, self._history_limit())
        state.last_activity = time.monotonic()

    @staticmethod
    def _fallback_messages(request: AgentRequest, outcome: AgentOutcome) -> list[dict[str, Any]]:
        messages = [{"role": "user", "content": request.user_text}]
        if outcome.reply:
            messages.append({"role": "assistant", "content": outcome.reply,
                             "sources": list(getattr(outcome, "sources", ()) or ()),
                             "answer_status": getattr(outcome, "answer_status", "answered")})
        return messages

    async def _worker(self, state: _ConversationState) -> None:
        while True:
            try:
                item = await asyncio.wait_for(state.queue.get(), timeout=self.idle_ttl_seconds)
            except asyncio.TimeoutError:
                async with self._state_lock:
                    if (
                        self._states.get(state.scope) is state
                        and state.pending_count == 0
                        and not state.active
                        and not self._has_pending_confirmation(state.scope)
                    ):
                        self._states.pop(state.scope, None)
                        return
                continue
            state.active = True
            request_history = list(state.history)
            try:
                if self.conversation_repository is not None:
                    self.conversation_repository.mark_runtime(state.scope, "running")
                session = self.session_factory(state.scope)
                request = replace(item.request, history=request_history, turn_id=item.turn_id)
                # 过程消息可能在 session.run() 尚未结束时就写入会话。先把本轮用户
                # 原话落库，才能保证刷新后的顺序始终是“用户提问 → 管家进度 → 最终回答”。
                # 结尾的 _persist_delta 会用同一个 stable_key 再遇到这条用户消息，
                # 数据库唯一索引会将它当作重复项跳过。
                if self.conversation_repository is not None:
                    user_extra: dict[str, Any] = {}
                    if request.attachment_metadata:
                        user_extra["attachments"] = [dict(item) for item in request.attachment_metadata]
                    self.append_message(
                        state.scope,
                        "user",
                        request.user_text,
                        stable_key=f"turn:{item.turn_id}:0",
                        **user_extra,
                    )
                try:
                    outcome = await asyncio.wait_for(
                        session.run(request), timeout=self.turn_timeout_seconds
                    )
                except asyncio.TimeoutError as exc:
                    logger.error(
                        "conversation turn timed out",
                        extra={
                            "turn_id": item.turn_id,
                            "conversation_id": state.scope.conversation_id,
                            "timeout_seconds": self.turn_timeout_seconds,
                        },
                    )
                    raise exc
                loop = getattr(session, "loop", None)
                public_messages = getattr(loop, "public_messages", None)
                if isinstance(public_messages, list):
                    self._persist_delta(state, public_messages, request_history, item.turn_id)
                elif outcome.status in {"completed", "paused"}:
                    self._persist_delta(state, self._fallback_messages(request, outcome), [], item.turn_id)
                if outcome.status == "paused" and outcome.action_id:
                    self.lock_conversation(state.scope, "waiting_confirmation", outcome.action_id)
                    if self.conversation_repository is not None:
                        self.conversation_repository.mark_runtime(state.scope, "paused")
                elif self.conversation_repository is not None:
                    self.conversation_repository.mark_runtime(state.scope, "idle")
                if self.event_bus:
                    # Deterministic/tool-only paths may not invoke a streaming provider.
                    loop_stream_seq = int(getattr(getattr(session, "loop", None), "_stream_seq", 0) or 0)
                    if outcome.reply and outcome.status in {"completed", "paused"} and loop_stream_seq == 0:
                        await self.event_bus.emit(Event(
                            type=EventType.CHAT_DELTA,
                            timestamp=datetime.now(),
                            scope=asdict(state.scope),
                            data={"turn_id": item.turn_id, "seq": 1, "delta": outcome.reply},
                        ))
                    terminal_type = EventType.CHAT_COMPLETED if outcome.status in {"completed", "paused"} else EventType.CHAT_FAILED
                    terminal_data = {"turn_id": item.turn_id, "status": outcome.status, "reply": outcome.reply,
                                     "sources": list(getattr(outcome, "sources", ()) or ()),
                                     "answer_status": getattr(outcome, "answer_status", "answered")}
                    if terminal_type == EventType.CHAT_FAILED:
                        terminal_data.update({
                            "error_code": "turn_failed",
                            "message": "回复生成失败，请稍后重试。",
                            "retryable": True,
                        })
                    await self.event_bus.emit(Event(
                        type=terminal_type,
                        timestamp=datetime.now(),
                        scope=asdict(state.scope),
                        data=terminal_data,
                    ))
                    policy_event = "chat.completed" if terminal_type == EventType.CHAT_COMPLETED else "chat.failed"
                    expression, reason = expression_for_event(policy_event, {"reply": outcome.reply})
                    if terminal_type == EventType.CHAT_FAILED or not getattr(loop, "_model_reply_seen", False):
                        await self.event_bus.emit(Event(
                            type=EventType.PORTRAIT_CHANGED,
                            timestamp=datetime.now(),
                            scope=asdict(state.scope),
                            data={"turn_id": item.turn_id, "expression": expression, "portrait_key": expression, "phase": "reply", "reason": reason},
                        ))
                    await self.event_bus.emit(Event(
                        type=EventType.TURN_COMPLETED,
                        timestamp=datetime.now(),
                        scope=asdict(state.scope),
                        data={"turn_id": item.turn_id, "status": outcome.status},
                    ))
                if not item.result.done():
                    item.result.set_result(outcome)
            except asyncio.CancelledError:
                if self.conversation_repository is not None:
                    self.conversation_repository.mark_runtime(state.scope, "interrupted")
                if not item.result.done():
                    item.result.set_result(self._cancelled_outcome(item.turn_id))
                raise
            except Exception as exc:
                if self.conversation_repository is not None:
                    self.conversation_repository.mark_runtime(state.scope, "interrupted")
                    self.append_message(
                        state.scope,
                        "assistant",
                        "回复生成失败，请稍后重试。",
                        stable_key=f"turn:{item.turn_id}:1",
                    )
                if self.event_bus:
                    await self.event_bus.emit(Event(
                        type=EventType.CHAT_FAILED,
                        timestamp=datetime.now(),
                        scope=asdict(state.scope),
                        data={
                            "turn_id": item.turn_id,
                            "error_code": "turn_failed",
                            "message": "回复生成失败，请稍后重试。",
                            "retryable": True,
                        },
                    ))
                    await self.event_bus.emit(Event(
                        type=EventType.PORTRAIT_CHANGED,
                        timestamp=datetime.now(),
                        scope=asdict(state.scope),
                        data={"turn_id": item.turn_id, "expression": "concern", "portrait_key": "concern", "phase": "failure", "reason": "failure"},
                    ))
                    await self.event_bus.emit(Event(
                        type=EventType.TURN_FAILED,
                        timestamp=datetime.now(),
                        scope=asdict(state.scope),
                        data={"turn_id": item.turn_id, "error_code": "turn_failed", "message": "回复生成失败，请稍后重试。"},
                    ))
                if not item.result.done():
                    item.result.set_result(self._failed_outcome(item.turn_id, exc))
            finally:
                state.active = False
                state.pending_count -= 1
                state.last_activity = time.monotonic()
                state.queue.task_done()

    async def wait_idle(self, scope: Scope) -> None:
        state = self._states.get(scope)
        if state is not None:
            await state.queue.join()

    async def evict_idle(self) -> int:
        """Close safely-idle workers now instead of waiting for their timeout."""
        evicted: list[asyncio.Task[None]] = []
        now = time.monotonic()
        async with self._state_lock:
            for scope, state in list(self._states.items()):
                if (
                    state.pending_count == 0
                    and not state.active
                    and now - state.last_activity >= self.idle_ttl_seconds
                    and not self._has_pending_confirmation(scope)
                ):
                    self._states.pop(scope, None)
                    if state.worker is not None and not state.worker.done():
                        evicted.append(state.worker)
        for worker in evicted:
            worker.cancel()
        if evicted:
            await asyncio.gather(*evicted, return_exceptions=True)
        return len(evicted)

    def is_resident(self, scope: Scope) -> bool:
        return scope in self._states

    def is_busy(self, scope: Scope) -> bool:
        state = self._states.get(scope)
        return bool((state is not None and (state.active or state.pending_count > 0)) or self._has_pending_confirmation(scope))

    def history(self, scope: Scope) -> list[dict[str, Any]]:
        state = self._states.get(scope)
        if state is not None:
            return list(state.history)
        return self.conversation_repository.history(scope) if self.conversation_repository is not None else []

    def append_message(
        self,
        scope: Scope,
        role: str,
        content: str,
        *,
        stable_key: str | None = None,
        touch_activity: bool = True,
        **extra: Any,
    ) -> bool:
        """追加一条公开消息。

        ``touch_activity=False`` 给"系统自己说的话"用（后台任务回执、
        管家过程句）：它们要出现在对话里，但不该把没人说话的对话顶到列表最前面。

        返回是否真的写进去了：``stable_key`` 撞车时为 False，
        调用方据此判断"这条已经写过了"，不再重复广播。

        返回值刻意保持布尔 —— 回执桥和管家 sink 都用 ``bool(...)`` 判断，
        而舞台播放游标需要的是 ``message_id``；两个需求拆开表达（布尔给调用方、
        id 进内存历史），比让同一个返回值身兼两职可靠。
        """
        message = {"role": role, "content": content, **{key: value for key, value in extra.items() if value is not None}}
        state = self._states.get(scope)
        if self.conversation_repository is None:
            if state is not None:
                state.history.append(message)
                state.history = self._cap_history(state.history, self._history_limit())
                state.last_activity = time.monotonic()
            return True
        message_id = self.conversation_repository.insert_message(
            scope, message, stable_key=stable_key, touch_activity=touch_activity
        )
        if message_id is None:
            return False
        if state is not None:
            # 同 _persist_delta：内存条目只多一个 message_id，
            # 多带字段会让下一轮的历史前缀比较失配、重复落库。
            state.history.append({**message, "message_id": message_id})
            state.history = self._cap_history(state.history, self._history_limit())
            state.last_activity = time.monotonic()
        return True

    def _clear_intent_state(self, scope: Scope) -> None:
        """清掉一个 Scope 的意图状态；失败只记日志。

        调用点都在消息已经删除/清空之后。此刻把异常抛给调用方，会让 HTTP 接口
        报"清空失败"，而消息其实已经没了 —— 重试还会撞上不同的失败模式。
        状态本身有 TTL 兜底，最坏结果是下一轮多问一次。
        """
        if self.intent_state_store is None:
            return
        try:
            self.intent_state_store.clear(scope)
        except Exception:
            logging.getLogger(__name__).exception("clear intent state failed for scope=%s", scope)

    def clear_history(self, scope: Scope) -> None:
        if self.conversation_repository is not None:
            self.conversation_repository.clear(scope)
        self._clear_intent_state(scope)
        state = self._states.get(scope)
        if state is not None:
            state.history.clear()
            state.last_activity = time.monotonic()

    async def forget(self, scope: Scope) -> None:
        """彻底收掉一个会话：意图状态、运行态缓存、进程内锁、空闲 worker。

        只给"会话已经被删除"之后调用 —— 调用方必须先确认 `is_busy(scope)` 为
        False，否则这里撤掉的锁会让一个正在等确认的动作失去承载对象。
        运行态缓存必须一起撤：``_states`` 里留着整段历史，等于删掉的对话还能
        从内存里被下一轮带回去。
        """
        self._clear_intent_state(scope)
        async with self._state_lock:
            state = self._states.pop(scope, None)
        # 新旧两种锁键都要清：Scope 键是当前的，裸 conversation_id 键
        # 是早期调用方留下的，漏一个就等于"锁还在但没人持有会话"。
        self._locks.pop(scope, None)
        self._locks.pop(scope.conversation_id, None)
        worker = state.worker if state is not None else None
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    def _emit_event(self, event: Event) -> None:
        """后台发射一个事件：持有强引用防 GC 丢任务，异常记日志不静默。

        单条事件失败不应影响调用方（锁已落盘/弹出，事件只是广播），
        所以在这里吞掉并记日志，而不是让回调里冒出无人处理的异常。
        """
        task = asyncio.create_task(self._emit_and_log(event))
        self._pending_emits.add(task)
        task.add_done_callback(self._pending_emits.discard)

    async def _emit_and_log(self, event: Event) -> None:
        try:
            await self.event_bus.emit(event)
        except Exception:
            logging.getLogger(__name__).exception("conversation event emit failed: %s", event.type)

    def lock_conversation(
        self,
        conversation: Scope | str,
        reason: str,
        action_id: str,
        *,
        scope: Scope | None = None,
    ) -> None:
        """Lock one complete Scope; string-only calls are legacy compatibility."""
        resolved_scope = conversation if isinstance(conversation, Scope) else scope
        conversation_id = resolved_scope.conversation_id if resolved_scope is not None else str(conversation)
        key: Scope | str = resolved_scope if resolved_scope is not None else conversation_id
        self._locks[key] = ConversationLock(
            conversation_id=conversation_id,
            locked_at=time.time(),
            reason=reason,
            action_id=action_id,
            owner_id=resolved_scope.owner_id if resolved_scope is not None else "",
            module_id=resolved_scope.module_id if resolved_scope is not None else "",
            channel=resolved_scope.channel if resolved_scope is not None else "",
        )
        if resolved_scope is not None and self.confirmation_store is not None:
            create_lock = getattr(self.confirmation_store, "create_lock", None)
            if callable(create_lock):
                try:
                    create_lock(AccountTaskScope(resolved_scope.owner_id, resolved_scope.module_id), action_id, resolved_scope, reason=reason)
                except Exception:
                    # AgentLoop.pause normally writes the lock atomically.
                    pass
        if self.event_bus:
            self._emit_event(Event(
                type=EventType.CONVERSATION_LOCKED,
                timestamp=datetime.now(),
                scope=asdict(resolved_scope) if resolved_scope is not None else {"conversation_id": conversation_id},
                data={"reason": reason, "action_id": action_id},
            ))

    def unlock_conversation(self, conversation: Scope | str) -> None:
        self._locks.pop(conversation, None)
        conversation_id = conversation.conversation_id if isinstance(conversation, Scope) else conversation
        if isinstance(conversation, Scope):
            # Clear a pre-Scope legacy lock only when this caller explicitly
            # asks to unlock its Scope. New locks are never keyed this way.
            self._locks.pop(conversation_id, None)
        if self.event_bus:
            scope = asdict(conversation) if isinstance(conversation, Scope) else {"conversation_id": conversation_id}
            self._emit_event(Event(
                type=EventType.CONVERSATION_UNLOCKED,
                timestamp=datetime.now(),
                scope=scope,
                data={},
            ))

    def unlock_account(self, scope: AccountTaskScope) -> None:
        """Legacy helper retained for non-web callers that intentionally unlock an account."""
        for key, lock in list(self._locks.items()):
            if lock.owner_id == scope.account_id and lock.module_id == scope.module_id:
                self.unlock_conversation(key)

    def is_locked(self, conversation: Scope | str) -> bool:
        if isinstance(conversation, Scope):
            return conversation in self._locks
        return conversation in self._locks or any(
            isinstance(key, Scope) and key.conversation_id == conversation for key in self._locks
        )

    def get_lock(self, conversation: Scope | str) -> ConversationLock | None:
        lock = self._locks.get(conversation)
        if lock is not None or isinstance(conversation, Scope):
            return lock
        return next(
            (lock for key, lock in self._locks.items() if isinstance(key, Scope) and key.conversation_id == conversation),
            None,
        )

    async def _persist_resumed_messages(self, scope: Scope, session: Any, turn_id: str) -> None:
        loop = getattr(session, "loop", None)
        public_messages = getattr(loop, "public_messages", None)
        if not isinstance(public_messages, list):
            return
        state = await self._state_for(scope)
        self._persist_delta(state, public_messages, list(state.history), turn_id)

    async def resume(self, action_id: str, scope: Scope, decision: str) -> AgentOutcome:
        if self.confirmation_store is None:
            raise RuntimeError("ConversationManager was not initialized with a confirmation_store")
        confirmation = self.confirmation_store.get(action_id, scope)
        if confirmation is None:
            return AgentOutcome("failed", "", reply=f"确认请求 {action_id} 不存在或已过期")
        if confirmation.status != "pending":
            return AgentOutcome("failed", "", reply=f"确认请求 {action_id} 状态为 {confirmation.status}，无法恢复")
        updated = self.confirmation_store.decide(action_id, scope, decision)
        if updated is None:
            return AgentOutcome("failed", "", reply=f"无法更新确认请求 {action_id}")
        original_scope = Scope(confirmation.owner_id, confirmation.channel, confirmation.conversation_id, confirmation.module_id)
        self.unlock_conversation(original_scope)
        if updated.status in {"rejected", "expired"}:
            if self.conversation_repository is not None:
                self.conversation_repository.mark_runtime(original_scope, "idle")
            return AgentOutcome("cancelled", updated.run_id, reply="高风险操作已取消，未执行工具。", action_id=action_id, confirmation=updated)
        if self.conversation_repository is not None:
            self.conversation_repository.mark_runtime(original_scope, "running")
        session = self.session_factory(original_scope)
        resumable = getattr(session, "resume", None)
        if callable(resumable):
            loop = getattr(session, "loop", None)
            if loop is not None:
                loop.run_id = confirmation.run_id
            outcome = await resumable(action_id, decision)
            if self.conversation_repository is not None:
                self.conversation_repository.mark_runtime(original_scope, "idle" if outcome.status == "completed" else "interrupted")
            await self._persist_resumed_messages(original_scope, session, f"resume:{action_id}")
            return outcome
        handle = await self.submit(AgentRequest(user_text=f"[Resume] Decision for action {action_id}: {decision}", scope=original_scope))
        if not handle.accepted:
            return AgentOutcome("failed", handle.turn_id, reply=handle.reason)
        return await handle.result

    async def reset_pending(self, scope: Scope) -> int:
        if self.confirmation_store is None:
            raise RuntimeError("ConversationManager was not initialized with a confirmation_store")
        count = int(self.confirmation_store.reject_pending(scope))
        self.unlock_conversation(scope)
        return count

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        workers = [state.worker for state in self._states.values() if state.worker is not None]
        for worker in workers:
            if worker is not None and not worker.done():
                worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        for state in self._states.values():
            while True:
                try:
                    item = state.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if not item.result.done():
                    item.result.set_result(self._cancelled_outcome(item.turn_id))
                state.pending_count -= 1
                state.queue.task_done()
