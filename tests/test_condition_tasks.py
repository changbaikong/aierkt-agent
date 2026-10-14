from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from agent_core.contracts import AccountTaskScope, Scope, ToolSpec
from agent_core.events import EventType, InMemoryEventBus
from agent_core.tools import ToolRegistry
from agent_tools.condition_tasks import ConditionTaskService, ConditionTaskStore, make_condition_task_tools


def _scope() -> Scope:
    return Scope("alice", "desktop", "chat", "companion")


def _search_tool(reply: str) -> ToolSpec:
    async def search(_arguments, _scope):
        return {"status": "ok", "output": {"results": [{"snippet": reply}]}}

    return ToolSpec("search__web", {"type": "function", "function": {"name": "web"}}, search)


@pytest.mark.asyncio
async def test_weather_task_requires_city(tmp_path) -> None:
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"),
        registry_provider=ToolRegistry,
    )

    with pytest.raises(ValueError, match="city"):
        await service.create_weather_task(
            scope=_scope(), title="晒被子", request_text="提醒我晒被子", city="",
        )


@pytest.mark.asyncio
async def test_weather_task_triggers_once_for_clear_weather_and_is_scope_isolated(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("上海今天天气晴朗，适合晾晒。"))
    bus = InMemoryEventBus()
    received = []
    bus.subscribe(EventType.TASK_DUE, received.append)
    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"),
        registry_provider=lambda: registry,
        event_bus=bus,
        now=lambda: now,
    )

    task = await service.create_weather_task(
        scope=_scope(), title="晒被子", request_text="天气好就晒被子", city="上海",
        daily_window={"start": "08:00", "end": "18:00"}, allowed_tools=["search__web"],
    )

    assert await service.tick() == 1
    assert service.store.get(task.task_id, _scope()).status == "completed"
    assert service.store.get(task.task_id, Scope("bob", "desktop", "chat", "companion")) is None
    assert received[0].data["title"] == "晒被子"
    assert received[0].data["city"] == "上海"


@pytest.mark.asyncio
async def test_ambiguous_weather_reschedules_without_notifying(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("上海今日天气信息更新中。"))
    bus = InMemoryEventBus()
    received = []
    bus.subscribe(EventType.TASK_DUE, received.append)
    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"),
        registry_provider=lambda: registry,
        event_bus=bus,
        now=lambda: now,
    )
    task = await service.create_weather_task(
        scope=_scope(), title="晒被子", request_text="天气好就晒被子", city="上海",
    )

    await service.tick()

    current = service.store.get(task.task_id, _scope())
    assert current.status == "scheduled"
    assert current.last_summary == "天气条件不明确"
    assert current.next_check_at > now.isoformat()
    assert received == []


@pytest.mark.asyncio
async def test_condition_task_tools_create_list_cancel_and_manual_check(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("上海天气晴朗"))
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry,
        now=lambda: datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    tools = {tool.name: tool for tool in make_condition_task_tools(service)}

    created = await tools["create_weather"].handler({
        "title": "晒被子", "request_text": "提醒晒被子", "city": "上海",
        "allowed_tools": ["search__web"],
    }, _scope())
    task_id = created["output"]["task_id"]
    listed = await tools["list"].handler({}, _scope())

    assert listed["output"]["tasks"][0]["task_id"] == task_id
    assert await tools["cancel"].handler({"task_id": task_id}, _scope()) == {
        "status": "ok", "output": {"cancelled": True, "task_id": task_id},
    }


@pytest.mark.asyncio
async def test_condition_task_list_is_shared_across_ingress_channels(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("天气信息更新中"))
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry,
        now=lambda: datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc),
    )
    task = await service.create_weather_task(scope=_scope(), title="晒被子", request_text="提醒", city="上海")
    other_channel = Scope("alice", "web", "web-chat", "companion")
    assert service.store.get(task.task_id, AccountTaskScope("alice", "companion")) is not None
    assert service.store.list(AccountTaskScope("alice", "companion"))[0].task_id == task.task_id
    assert service.store.get(task.task_id, AccountTaskScope("bob", "companion")) is None


@pytest.mark.asyncio
async def test_search_failure_reschedules_condition_task(tmp_path) -> None:
    async def failing_search(_arguments, _scope):
        return {"status": "failed", "error": "network unavailable"}

    registry = ToolRegistry()
    registry.register(ToolSpec("search__web", {"type": "function", "function": {"name": "web"}}, failing_search))
    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry,
        now=lambda: now,
    )
    task = await service.create_weather_task(scope=_scope(), title="晒被子", request_text="提醒", city="上海")

    await service.tick()

    current = service.store.get(task.task_id, _scope())
    assert current.status == "scheduled"
    assert "network unavailable" in current.last_error
    assert current.next_check_at > now.isoformat()


@pytest.mark.asyncio
async def test_missing_frozen_search_tool_blocks_existing_task(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("上海天气晴朗"))
    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry,
        now=lambda: now,
    )
    task = await service.create_weather_task(scope=_scope(), title="晒被子", request_text="提醒", city="上海")
    service.registry_provider = ToolRegistry

    await service.tick()

    current = service.store.get(task.task_id, _scope())
    assert current.status == "blocked"
    assert "authorized search__web tool is unavailable" == current.last_error


@pytest.mark.asyncio
async def test_condition_checks_are_limited_to_two_concurrent_searches(tmp_path) -> None:
    active = 0
    maximum = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_search(_arguments, _scope):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            started.set()
        await release.wait()
        active -= 1
        return {"status": "ok", "output": {"results": [{"snippet": "天气晴朗"}]}}

    registry = ToolRegistry()
    registry.register(ToolSpec("search__web", {"type": "function", "function": {"name": "web"}}, slow_search))
    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry,
        now=lambda: now, max_concurrent_checks=2,
    )
    for city in ("上海", "北京", "广州"):
        await service.create_weather_task(scope=_scope(), title=city, request_text="提醒", city=city)

    ticking = asyncio.create_task(service.tick())
    await asyncio.wait_for(started.wait(), timeout=1)
    assert maximum == 2
    release.set()
    assert await ticking == 3


@pytest.mark.asyncio
async def test_service_restart_recovers_interrupted_check(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("上海天气信息更新中"))
    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    store = ConditionTaskStore(tmp_path / "conditions.db")
    first = ConditionTaskService(store=store, registry_provider=lambda: registry, now=lambda: now)
    task = await first.create_weather_task(scope=_scope(), title="晒被子", request_text="提醒", city="上海")
    assert store.claim(task, now.isoformat(), lease_seconds=1)

    recovered_now = now.replace(second=2)
    ConditionTaskService(store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry, now=lambda: recovered_now)

    recovered = store.get(task.task_id, _scope())
    assert recovered.status == "scheduled"
    assert recovered.next_check_at == recovered_now.isoformat()


@pytest.mark.asyncio
async def test_check_exception_reschedules_instead_of_sticking_checking(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_search_tool("上海天气晴朗"))

    class BrokenBus:
        async def emit(self, _event):
            raise RuntimeError("event sink offline")

    now = datetime(2030, 1, 1, 1, 0, tzinfo=timezone.utc)
    service = ConditionTaskService(
        store=ConditionTaskStore(tmp_path / "conditions.db"), registry_provider=lambda: registry,
        event_bus=BrokenBus(), now=lambda: now,
    )
    task = await service.create_weather_task(scope=_scope(), title="晒被子", request_text="提醒", city="上海")

    await service.tick()

    current = service.store.get(task.task_id, _scope())
    assert current.status == "completed"
    assert current.next_check_at is None
