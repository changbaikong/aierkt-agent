from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent_core.contracts import AccountTaskScope, IngressContext, Scope, ToolSpec
from agent_core.events import EventType, InMemoryEventBus
from agent_core.task_persistence import TaskStore
from agent_core.task_service import TaskService, normalize_schedule
from agent_core.tools import ToolRegistry
from agent_tools.system_info import local_time_info


def test_normalize_schedule_accepts_cron_and_timezone() -> None:
    schedule = normalize_schedule(
        {
            "cron": "0 9 * * 1",
            "timezone": "Asia/Shanghai",
        }
    )
    assert schedule["kind"] == "cron"
    assert schedule["timezone"] == "Asia/Shanghai"
    assert schedule["cron"] == "0 9 * * 1"


def test_normalize_schedule_rejects_multiple_schedule_kinds() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        normalize_schedule({"run_at": "2030-01-01T09:00:00+08:00", "interval_seconds": 60})


def test_normalize_schedule_migrates_legacy_hour_minute_form() -> None:
    schedule = normalize_schedule({"hour": 7, "minute": 0, "timezone": "Asia/Shanghai"})

    assert schedule["kind"] == "run_at"
    assert schedule["timezone"] == "Asia/Shanghai"
    assert schedule["run_at"].endswith("+00:00")


def test_task_store_scope_isolation_and_claim_are_atomic(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    other = Scope("bob", "desktop", "chat", "companion")
    task = store.create_task(
        scope=scope,
        title="提醒",
        request_text="喝水",
        delivery_mode="notify",
        schedule={"kind": "run_at", "run_at": "2020-01-01T09:00:00+00:00", "timezone": "UTC"},
        allowed_tools=[],
        next_run_at="2020-01-01T09:00:00+00:00",
    )

    assert store.get_task(task.task_id, other) is None
    assert store.claim_due_task(task.task_id, scope, lease_seconds=60) is not None
    assert store.claim_due_task(task.task_id, scope, lease_seconds=60) is None


def test_account_task_scope_shares_tasks_across_ingress_channels(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    web = Scope("account-1", "web", "chat-web", "companion")
    task = store.create_task(
        scope=web, title="x", request_text="x", delivery_mode="notify",
        schedule={"kind": "run_at", "run_at": "2099-01-01T09:00:00+00:00", "timezone": "UTC"},
        allowed_tools=[], next_run_at="2099-01-01T09:00:00+00:00",
    )
    assert [item.task_id for item in store.list_tasks(AccountTaskScope("account-1", "companion"))] == [task.task_id]
    assert store.list_tasks(AccountTaskScope("account-2", "companion")) == []


def test_ingress_context_exposes_separate_task_identity() -> None:
    context = IngressContext("account-1", "wechat", "chat-1", delivery_target="unified")
    assert context.scope() == Scope("account-1", "wechat", "chat-1", "companion")
    assert context.task_scope() == AccountTaskScope("account-1", "companion")


def test_expired_lease_reconciles_old_run_and_finish_is_cas_safe(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    task = store.create_task(
        scope=scope, title="x", request_text="x", delivery_mode="notify",
        schedule={"kind": "run_at", "run_at": "2020-01-01T09:00:00+00:00", "timezone": "UTC"},
        allowed_tools=[], next_run_at="2020-01-01T09:00:00+00:00",
    )
    first = store.claim_due_task(task.task_id, scope, lease_seconds=1,
                                 now=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert first is not None
    second = store.claim_due_task(task.task_id, scope, lease_seconds=60,
                                  now=datetime(2030, 1, 1, 9, 0, 2, tzinfo=timezone.utc))
    assert second is not None

    assert not store.finish_run(task.task_id, first.run_id, scope, lease_id=first.lease_id,
                                status="completed", result="stale")
    current = store.get_task(task.task_id, scope)
    assert current is not None and current.status == "running"
    assert store.finish_run(task.task_id, second.run_id, scope, lease_id=second.lease_id,
                            status="completed", result="fresh")
    runs = store.runs(task.task_id, scope)
    stale = next(run for run in runs if run.run_id == first.run_id)
    assert stale.status == "failed"
    assert "lease" in (stale.error or "")


@pytest.mark.asyncio
async def test_run_now_does_not_requeue_active_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    task = store.create_task(
        scope=scope, title="x", request_text="x", delivery_mode="notify",
        schedule={"kind": "interval", "interval_seconds": 60, "timezone": "UTC"},
        allowed_tools=[], next_run_at="2030-01-01T09:00:00+00:00",
    )
    service = TaskService(provider=None, registry_provider=lambda: ToolRegistry(), store=store,
                          now=lambda: datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))
    claimed = store.claim_due_task(task.task_id, scope, now=service.now())
    assert claimed is not None
    assert await service.run_now(task.task_id, scope) is False


@pytest.mark.asyncio
async def test_scheduler_tick_contains_event_failures(tmp_path) -> None:
    class BrokenBus:
        async def emit(self, _event):
            raise RuntimeError("event sink down")

    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    service = TaskService(
        provider=None, registry_provider=lambda: ToolRegistry(), store=store,
        event_bus=BrokenBus(), now=lambda: datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc),
    )
    task = await service.create_task(
        scope=scope, title="x", request_text="x", delivery_mode="notify",
        schedule={"run_at": "2030-01-01T09:00:00+00:00"}, allowed_tools=[],
    )
    assert await service.tick() == 1
    current = store.get_task(task.task_id, scope)
    assert current is not None and current.status == "completed"


@pytest.mark.asyncio
async def test_notify_task_emits_due_and_completes(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    bus = InMemoryEventBus()
    received = []
    bus.subscribe(EventType.TASK_DUE, received.append)
    completed = []
    bus.subscribe(EventType.TASK_COMPLETED, completed.append)
    scope = Scope("alice", "desktop", "chat", "companion")
    service = TaskService(
        provider=None,
        registry_provider=lambda: ToolRegistry(),
        store=store,
        event_bus=bus,
        now=lambda: datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc),
    )
    result = await service.create_task(
        scope=scope,
        title="提醒",
        request_text="喝水",
        delivery_mode="notify",
        schedule={"run_at": "2030-01-01T09:00:00+00:00"},
        allowed_tools=[],
    )
    assert result.status == "scheduled"
    await service.tick()
    task = store.get_task(result.task_id, scope)
    assert task is not None
    assert task.status == "completed"
    assert received and received[0].data["task_id"] == result.task_id
    assert completed and completed[0].data["title"] == "提醒"


def test_system_info_is_local_and_read() -> None:
    from agent_tools.system_info import make_system_info_tool

    result = make_system_info_tool().handler({"kind": "time"}, Scope())
    assert result["status"] == "ok"
    assert result["output"]["source"] == "local"
    assert "timezone" in result["output"]


def test_local_time_info_reports_a_clear_local_read_failure(monkeypatch) -> None:
    monkeypatch.setattr("agent_tools.system_info._read_local_time", lambda: (_ for _ in ()).throw(OSError("clock unavailable")))

    result = local_time_info()

    assert result["status"] == "failed"
    assert "local time unavailable" in result["error"]


def test_finish_run_cannot_overwrite_cancelled_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    task = store.create_task(scope=scope, title="x", request_text="x", delivery_mode="notify", schedule={"kind": "run_at", "run_at": "2020-01-01T09:00:00+00:00", "timezone": "UTC"}, allowed_tools=[], next_run_at="2020-01-01T09:00:00+00:00")
    run = store.claim_due_task(task.task_id, scope, now=datetime(2020, 1, 1, 9, tzinfo=timezone.utc))
    assert run is not None
    assert store.cancel_task(task.task_id, scope)
    store.finish_run(task.task_id, run.run_id, scope, status="completed", result="late")
    assert store.get_task(task.task_id, scope).status == "cancelled"


@pytest.mark.asyncio
async def test_run_now_does_not_requeue_running_task(tmp_path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    task = store.create_task(scope=scope, title="x", request_text="x", delivery_mode="notify", schedule={"kind": "run_at", "run_at": "2020-01-01T09:00:00+00:00", "timezone": "UTC"}, allowed_tools=[], next_run_at="2020-01-01T09:00:00+00:00")
    store.claim_due_task(task.task_id, scope, now=datetime(2020, 1, 1, 9, tzinfo=timezone.utc))
    service = TaskService(provider=None, registry_provider=lambda: ToolRegistry(), store=store, now=lambda: datetime(2020, 1, 1, 9, tzinfo=timezone.utc))
    assert not await service.run_now(task.task_id, scope)


@pytest.mark.asyncio
async def test_scheduled_task_loop_uses_the_same_turn_budget(monkeypatch, tmp_path) -> None:
    """后台定时任务与主聊天共用同一份整轮预算，不能各自维护一个数字。"""
    from agent_core import task_service as task_service_module
    from agent_core.loop import AgentLoop

    captured: dict = {}

    class StubLoop:
        def __init__(self, provider, registry, scope, **kwargs):
            captured.update(kwargs)
            captured["scope"] = scope

    class StubRunner:
        async def run_internal(self, loop, request):
            from types import SimpleNamespace

            return SimpleNamespace(status="completed", reply="ok")

    monkeypatch.setattr(task_service_module, "AgentLoop", StubLoop)
    store = TaskStore(tmp_path / "tasks.db")
    scope = Scope("alice", "desktop", "chat", "companion")
    task = store.create_task(
        scope=scope, title="x", request_text="x", delivery_mode="notify",
        schedule={"kind": "run_at", "run_at": "2020-01-01T09:00:00+00:00", "timezone": "UTC"},
        allowed_tools=[], next_run_at="2020-01-01T09:00:00+00:00",
    )
    service = TaskService(
        provider=object(), registry_provider=lambda: ToolRegistry(),
        store=store, runner=StubRunner(),
    )

    assert await service._execute(store.get_task(task.task_id, scope)) == "ok"
    assert captured["max_total_tokens"] == AgentLoop.MAX_TOTAL_TOKENS == 35000
