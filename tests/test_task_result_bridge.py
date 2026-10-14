"""定时任务终态回执桥（agent_core/task_result_bridge.py）。

这一组钉的是**公开边界**，不是某几句话的措辞：
  1. 失败 / 取消句里不含异常原文、堆栈、本机绝对路径、换行；
  2. 成功句后面能跟的补充说明必须来自已确认业务输出且过洁净度检查；
  3. 同一终态只写一次（stable_key 幂等）——重发事件不会自激成死循环；
  4. 写回用的是任务**自己**的 Scope，且不刷新 last_activity；
  5. 广播出去的刷新事件只带稳定字段，不夹带原始 error / result。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_core.contracts import Scope
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.task_result_bridge import (
    TASK_RESULT_KIND,
    TaskResultBridge,
    public_error_code,
    public_task_result,
)

#: 一段"真实形状"的内部诊断：异常类名 + 本机绝对路径 + 换行 + 堆栈。
DIRTY_ERROR = (
    "RuntimeError: [Errno 2] No such file or directory: "
    "'C:\\Users\\\\user\\Desktop\\secret.txt'\n"
    "Traceback (most recent call last):\n  File \"loop.py\", line 1, in run\n"
    "provider error: AmapNavigationClient"
)

_LEAKS = ("RuntimeError", "Traceback", "Errno", "C:\\Users", "secret.txt", "loop.py",
          "provider error", "AmapNavigationClient", "\n")


def _scope() -> Scope:
    return Scope("alice", "web", "chat-1", "companion")


class FakeRepository:
    """最小 ConversationRepository 替身：只需支持 stable_key 幂等。"""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_message(self, scope, message, *, stable_key=None, touch_activity=True):
        if any(row["stable_key"] == stable_key for row in self.rows):
            return False
        self.rows.append({
            "scope": scope, "message": message,
            "stable_key": stable_key, "touch_activity": touch_activity,
        })
        return True


def _event(event_type: EventType, data: dict, scope: Scope | None = None) -> Event:
    scope = scope or _scope()
    return Event(
        event_type,
        datetime.now(timezone.utc),
        {"owner_id": scope.owner_id, "channel": scope.channel,
         "conversation_id": scope.conversation_id, "module_id": scope.module_id},
        data,
    )


# ----------------------------------------------------------------------
# 1. 纯函数：固定措辞
# ----------------------------------------------------------------------
def test_task_result_lines_are_fixed() -> None:
    assert public_task_result("completed", "每晚总结") == "「每晚总结」已经办好了。"
    assert public_task_result("failed", "每晚总结", "permanent") == (
        "「每晚总结」这次没办成。已经记录了情况，稍后可以重试。"
    )
    assert public_task_result("cancelled", "每晚总结") == "「每晚总结」已经停下来了。"


def test_unknown_status_produces_nothing() -> None:
    """认不出的状态就不说话 —— 硬凑一句比不说更糟。"""
    assert public_task_result("running", "每晚总结") == ""
    assert public_task_result("", "每晚总结") == ""


def test_failed_line_ignores_internal_detail() -> None:
    line = public_task_result("failed", "每晚总结", "permanent", DIRTY_ERROR)

    for leak in _LEAKS:
        assert leak not in line
    assert line == "「每晚总结」这次没办成。已经记录了情况，稍后可以重试。"


def test_cancelled_line_ignores_internal_detail() -> None:
    line = public_task_result("cancelled", "每晚总结", "permanent", DIRTY_ERROR)

    for leak in _LEAKS:
        assert leak not in line


def test_completed_summary_only_from_public_business_output() -> None:
    ok = public_task_result("completed", "每晚总结", "ok", "今天的三条都记下来了。")
    assert ok == "「每晚总结」已经办好了。今天的三条都记下来了。"

    # 摘要里带异常原文 / 路径 → 整段丢掉，只留固定句（截断清洗也还是泄漏）。
    bad = public_task_result("completed", "每晚总结", "ok", DIRTY_ERROR)
    assert bad == "「每晚总结」已经办好了。"


def test_title_that_is_not_public_text_falls_back() -> None:
    line = public_task_result("completed", "C:\\Users\\secret\\notes.txt")

    assert "未命名任务" in line
    assert "secret" not in line


def test_long_title_and_summary_are_truncated() -> None:
    line = public_task_result("completed", "很长的标题" * 20, "ok", "很长的补充说明" * 20)

    # 标题 40 字 + 固定句 7 字 + 摘要 60 字，再多就不该有。
    assert line.startswith("「")
    assert len(line) <= 40 + len("「」已经办好了。") + 60


def test_public_error_code_maps_without_echoing_text() -> None:
    assert public_error_code("") == ""
    assert public_error_code("asyncio.TimeoutError: tool timed out") == "timeout"
    assert public_error_code("PermissionError: [WinError 5] Access is denied") == "permission"
    assert public_error_code("KeyError: 'directory'") == "internal"
    # 稳定码里绝不能出现原文片段
    assert "WinError" not in public_error_code("PermissionError: [WinError 5] Access is denied")


# ----------------------------------------------------------------------
# 2. 桥：写回、幂等、不自激
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bridge_writes_public_line_not_internal_error() -> None:
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_FAILED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结",
        "error": DIRTY_ERROR, "error_code": "permanent",
    }))

    assert len(repository.rows) == 1
    row = repository.rows[0]
    content = row["message"]["content"]
    assert content == "「每晚总结」这次没办成。已经记录了情况，稍后可以重试。"
    for leak in _LEAKS:
        assert leak not in content
    assert row["message"]["kind"] == TASK_RESULT_KIND
    assert row["message"]["task_status"] == "failed"
    assert row["touch_activity"] is False
    assert row["stable_key"] == "task-result:t1:r1:failed"
    assert row["scope"] == _scope()


@pytest.mark.asyncio
async def test_bridge_writes_to_the_task_own_scope() -> None:
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()
    other = Scope("bob", "web", "chat-9", "companion")

    await bus.emit(_event(EventType.TASK_COMPLETED, {"task_id": "t1", "title": "A"}, other))

    assert repository.rows[0]["scope"] == other


@pytest.mark.asyncio
async def test_bridge_dedupes_repeated_terminal_event() -> None:
    """同一 run 的同一终态发两次，只写一条；重发不会自激成死循环。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()
    payload = {"task_id": "t1", "run_id": "r1", "title": "每晚总结", "error": DIRTY_ERROR}

    await bus.emit(_event(EventType.TASK_FAILED, payload))
    await bus.emit(_event(EventType.TASK_FAILED, payload))

    assert len(repository.rows) == 1


@pytest.mark.asyncio
async def test_bridge_emits_one_task_result_with_only_stable_fields() -> None:
    """写库成功后只发一条 ``task.result``，且它不许夹带 error / result / 路径。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()
    seen: list[Event] = []
    bus.subscribe(EventType.TASK_RESULT, lambda event: seen.append(event))
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_FAILED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结",
        "error": DIRTY_ERROR, "error_code": "permanent",
    }))

    assert len(seen) == 1
    data = seen[0].data
    for leaked in ("error", "result", "title", "exception", "traceback"):
        assert leaked not in data
    assert data == {
        "task_id": "t1",
        "run_id": "r1",
        "status": "failed",
        "result_code": "permanent",
        "history_written": True,
    }
    for leak in _LEAKS:
        assert leak not in str(data)


@pytest.mark.asyncio
async def test_terminal_event_stays_internal_and_is_not_rebroadcast() -> None:
    """原始终态只发一次：普通内部订阅者看到一条，且看不到 history_written。"""
    bus = InMemoryEventBus()
    terminal: list[Event] = []
    refresh: list[Event] = []
    bus.subscribe(EventType.TASK_FAILED, lambda event: terminal.append(event))
    bus.subscribe(EventType.TASK_RESULT, lambda event: refresh.append(event))
    bridge = TaskResultBridge(bus, FakeRepository())
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_FAILED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结", "error": DIRTY_ERROR,
    }))

    # 终态没有被重发：订阅者只看到 TaskService 发的那一条
    assert len(terminal) == 1
    assert terminal[0].data.get("error") == DIRTY_ERROR  # 内部消费才拿得到原文
    assert "history_written" not in terminal[0].data
    # 刷新通知是另一条独立事件
    assert len(refresh) == 1
    assert refresh[0].type is EventType.TASK_RESULT


@pytest.mark.asyncio
async def test_task_result_arrives_after_the_history_row_exists() -> None:
    """task.result 到达时回执必须已经落库 —— 否则前端拉历史会扑空。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()
    seen: list[Event] = []
    bus.subscribe(EventType.TASK_RESULT, lambda event: seen.append(event))
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_COMPLETED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结", "result": "都记下来了。",
    }))

    assert len(repository.rows) == 1  # 事件送达的那一刻，库里已经有一条
    assert repository.rows[0]["message"]["content"] == "「每晚总结」已经办好了。都记下来了。"


@pytest.mark.asyncio
async def test_replayed_terminal_writes_once_and_refreshes_once() -> None:
    """重放同一终态：不重复写聊天，也不重复发 task.result。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()
    seen: list[Event] = []
    bus.subscribe(EventType.TASK_RESULT, lambda event: seen.append(event))
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()
    payload = {"task_id": "t1", "run_id": "r1", "title": "每晚总结", "error": DIRTY_ERROR}

    await bus.emit(_event(EventType.TASK_FAILED, payload))
    await bus.emit(_event(EventType.TASK_FAILED, payload))
    await bus.emit(_event(EventType.TASK_FAILED, payload))

    assert len(repository.rows) == 1
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_task_result_is_scoped_to_the_owning_conversation() -> None:
    """别人的任务结果不会跑到这个对话里来刷新。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()
    seen: list[Event] = []
    other = Scope("bob", "web", "chat-9", "companion")
    bus.subscribe(EventType.TASK_RESULT, lambda event: seen.append(event),
                  owner_id="alice", conversation_id="chat-1")
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_COMPLETED, {"task_id": "t1", "title": "A"}, other))

    assert repository.rows[0]["scope"] == other
    assert seen == []  # alice/chat-1 的订阅者一条都没收到


@pytest.mark.asyncio
async def test_bridge_completed_line_uses_confirmed_output() -> None:
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_COMPLETED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结",
        "result": "今天的三条都记下来了。",
    }))

    assert repository.rows[0]["message"]["content"] == (
        "「每晚总结」已经办好了。今天的三条都记下来了。"
    )


@pytest.mark.asyncio
async def test_bridge_completed_line_drops_dirty_result() -> None:
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_COMPLETED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结", "result": DIRTY_ERROR,
    }))

    content = repository.rows[0]["message"]["content"]
    assert content == "「每晚总结」已经办好了。"
    for leak in _LEAKS:
        assert leak not in content


@pytest.mark.asyncio
async def test_bridge_cancelled_line() -> None:
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_CANCELLED, {"task_id": "t1", "run_id": "r1", "title": "每晚总结"}))

    assert repository.rows[0]["message"]["content"] == "「每晚总结」已经停下来了。"


@pytest.mark.asyncio
async def test_bridge_ignores_events_without_a_conversation() -> None:
    """没有 owner / conversation 就写不回去 —— 宁可少写，也不能写错地方。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()
    bridge = TaskResultBridge(bus, repository)
    bridge.subscribe()

    await bus.emit(Event(EventType.TASK_FAILED, datetime.now(timezone.utc),
                         {"owner_id": "alice"}, {"task_id": "t1", "title": "X"}))

    assert repository.rows == []


@pytest.mark.asyncio
async def test_bridge_without_a_store_writes_nothing_and_broadcasts_nothing() -> None:
    """没有落库能力就不发刷新通知：没写进历史却叫前端去拉，等于空刷新。"""
    bus = InMemoryEventBus()
    seen: list[Event] = []
    bus.subscribe(EventType.TASK_RESULT, lambda event: seen.append(event))
    bridge = TaskResultBridge(bus, None)
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_FAILED, {"task_id": "t1", "run_id": "r1", "title": "X"}))

    assert seen == []


@pytest.mark.asyncio
async def test_bridge_subscribe_is_idempotent_and_unsubscribe_cleans_up() -> None:
    bus = InMemoryEventBus()
    bridge = TaskResultBridge(bus, FakeRepository())

    first = bridge.subscribe()
    second = bridge.subscribe()

    assert len(first) == 3
    assert second == first
    assert len(bus._subscriptions) == 3
    bridge.unsubscribe()
    assert bus._subscriptions == {}


@pytest.mark.asyncio
async def test_bridge_never_self_excites_even_with_a_non_idempotent_store() -> None:
    """环的终止条件必须握在桥自己手里。

    写这一层时踩过：替身落库层"永远返回写入成功"（不按 stable_key 去重），
    广播出去的事件又被桥自己收到 → 再写 → 再广播 → 测试直接挂死。
    真实落库层确实是幂等的，但环的终止不该押在别人身上。
    """
    bus = InMemoryEventBus()
    calls: list[int] = []

    class NeverDedupingManager:
        def append_message(self, *_args, **_kwargs):
            calls.append(1)
            return True

    bridge = TaskResultBridge(bus, None, conversation_manager=NeverDedupingManager())
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_COMPLETED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结", "result": "都记下来了。",
    }))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_bridge_prefers_conversation_manager_when_present() -> None:
    """有 manager 时走它：除了落库还要更新进程内对话缓存。"""
    bus = InMemoryEventBus()
    calls: list[tuple] = []

    class FakeManager:
        def append_message(self, scope, role, content, *, stable_key=None, touch_activity=True, **extra):
            calls.append((scope, role, content, stable_key, touch_activity, extra))
            return True

    bridge = TaskResultBridge(bus, FakeRepository(), conversation_manager=FakeManager())
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_FAILED, {"task_id": "t1", "run_id": "r1", "title": "每晚总结"}))

    assert len(calls) == 1
    scope, role, content, stable_key, touch_activity, extra = calls[0]
    assert role == "assistant"
    assert stable_key == "task-result:t1:r1:failed"
    assert touch_activity is False
    assert extra["kind"] == TASK_RESULT_KIND
    assert "RuntimeError" not in content

@pytest.mark.anyio
async def test_terminal_sink_reports_only_public_fields() -> None:
    """终态回调拿到的只能是公开字段：任务号、终态、标题、已确认产出摘要。"""
    bus = InMemoryEventBus()
    seen: list[tuple[dict, Scope]] = []

    def sink(payload, scope):
        seen.append((payload, scope))

    bridge = TaskResultBridge(bus, FakeRepository(), on_terminal=sink)
    bridge.subscribe()
    await bus.emit(_event(EventType.TASK_FAILED, {
        "task_id": "t1", "run_id": "r1", "title": "每晚总结", "error": DIRTY_ERROR,
    }))

    assert len(seen) == 1
    payload, scope = seen[0]
    assert payload["status"] == "failed"
    assert payload["task_id"] == "t1"
    assert scope == _scope()
    for leak in _LEAKS:
        assert leak not in str(payload)


@pytest.mark.anyio
async def test_progress_events_do_not_reach_the_terminal_sink() -> None:
    bus = InMemoryEventBus()
    seen: list[dict] = []
    bridge = TaskResultBridge(bus, FakeRepository(), on_terminal=lambda payload, scope: seen.append(payload))
    bridge.subscribe()

    await bus.emit(_event(EventType.TASK_STARTED, {"task_id": "t1", "run_id": "r1", "title": "每晚总结"}))

    assert seen == []


@pytest.mark.anyio
async def test_a_failing_sink_never_breaks_the_receipt() -> None:
    """记忆入口是第二个消费口，它失败不能把任务回执也带走。"""
    bus = InMemoryEventBus()
    repository = FakeRepository()

    async def broken_sink(payload, scope):
        raise RuntimeError("memory sink down")

    bridge = TaskResultBridge(bus, repository, on_terminal=broken_sink)
    bridge.subscribe()
    await bus.emit(_event(EventType.TASK_FAILED, {"task_id": "t2", "run_id": "r2", "title": "每晚总结"}))

    assert len(repository.rows) == 1
