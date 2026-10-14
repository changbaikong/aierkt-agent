from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.modules import ModuleRegistry
from agent_core.task_service import TaskService
from agent_core.task_persistence import TaskStore
from agent_core.tools import ToolRegistry
from agent_tools.modules import (
    build_condition_task_module,
    build_location_module,
    build_navigation_module,
    build_search_module,
    build_task_module,
)
from agent_tools.tasks import make_task_tools


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ToolSpec("safe", {"type": "function", "function": {"name": "safe", "parameters": {"type": "object"}}}, lambda _args, _scope: {"value": 1}))
    registry.register(ToolSpec("danger", {"type": "function", "function": {"name": "danger", "parameters": {"type": "object"}}}, lambda _args, _scope: {}, risk="high"))
    return registry


@pytest.mark.asyncio
async def test_create_rejects_high_risk_background_tool(tmp_path: Path) -> None:
    service = TaskService(provider=None, registry_provider=_registry, store=TaskStore(tmp_path / "tasks.db"))
    with pytest.raises(ValueError, match="not allowed"):
        await service.create_task(
            scope=Scope(), title="x", request_text="x", delivery_mode="execute",
            schedule={"run_at": "2030-01-01T09:00:00+00:00"}, allowed_tools=["danger"],
        )


def test_task_module_namespaces_management_tools(tmp_path: Path) -> None:
    modules = ModuleRegistry()
    spec, _service = build_task_module(db_path=tmp_path / "tasks.db", registry_provider=_registry)
    modules.register(spec)
    registry = modules.tools_for({"tasks"})
    assert registry.resolve("tasks__create") is not None
    assert registry.resolve("tasks__get") is not None


def test_condition_task_module_namespaces_weather_tools(tmp_path: Path) -> None:
    modules = ModuleRegistry()
    spec, _service = build_condition_task_module(db_path=tmp_path / "conditions.db", registry_provider=_registry)
    modules.register(spec)
    registry = modules.tools_for({"conditions"})

    assert registry.resolve("conditions__create_weather") is not None
    assert registry.resolve("conditions__run_now") is not None


def test_create_task_schema_describes_supported_schedule_forms(tmp_path: Path) -> None:
    service = TaskService(provider=None, registry_provider=_registry, store=TaskStore(tmp_path / "tasks.db"))
    create = next(tool for tool in make_task_tools(service) if tool.name == "create")

    schedule = create.schema["function"]["parameters"]["properties"]["schedule"]

    assert schedule["description"]
    assert set(schedule["properties"]) >= {"run_at", "interval_seconds", "cron", "timezone"}


def test_navigation_module_composes_location_and_route_tools() -> None:
    modules = ModuleRegistry()
    spec, _store = build_navigation_module(api_key="test-key")
    modules.register(build_location_module(_store))
    modules.register(spec)
    registry = modules.tools_for({"location", "navigation"})
    assert registry.resolve("location__current") is not None
    assert registry.resolve("navigation__route") is not None


def test_navigation_module_requires_api_key() -> None:
    with __import__("pytest").raises(ValueError, match="AMAP_WEB_SERVICE_KEY"):
        build_navigation_module(api_key="")


# ----------------------------------------------------------------------
# 正式模块产出的 list 工具必须自带"只读 + 确定性路由"标记
# ----------------------------------------------------------------------
# 只断言 ToolSpec 上的标记，不看测试自己手工拼的 spec —— 回归正是
# "测试带了标记、正式模块没带" 这种形态，手工 spec 断言等于没测。
def _stub_search_tool() -> ToolSpec:
    return ToolSpec(
        name="web",
        schema={"type": "function", "function": {"name": "web", "description": "search",
                                                  "parameters": {"type": "object", "properties": {}}}},
        handler=lambda _arguments, _scope: {"status": "ok", "output": {"results": []}},
        risk="low",
    )


def _namespaced_registry(tmp_path: Path) -> ToolRegistry:
    module_ids = {"tasks", "conditions", "search"}
    modules = ModuleRegistry()
    modules.register(build_search_module(_stub_search_tool))

    def registry_provider():
        return modules.tools_for(set(module_ids))

    modules.register(build_task_module(
        db_path=tmp_path / "tasks.db", registry_provider=registry_provider
    )[0])
    modules.register(build_condition_task_module(
        db_path=tmp_path / "conditions.db", registry_provider=registry_provider
    )[0])
    return modules.tools_for(set(module_ids))


def test_official_task_list_tool_declares_read_effect_fast_route(tmp_path: Path) -> None:
    registry = _namespaced_registry(tmp_path)
    spec = registry.resolve("tasks__list")

    assert spec is not None
    assert spec.effect == EFFECT_READ
    assert spec.deterministic_route_allowed is True
    assert spec.risk == "low"


def test_official_condition_list_tool_declares_read_effect_fast_route(tmp_path: Path) -> None:
    registry = _namespaced_registry(tmp_path)
    spec = registry.resolve("conditions__list")

    assert spec is not None
    assert spec.effect == EFFECT_READ
    assert spec.deterministic_route_allowed is True
    assert spec.risk == "low"


def test_official_task_get_is_read_but_not_deterministic(tmp_path: Path) -> None:
    """get 要 task_id，只有模型在拿到 id 之后才调得动 —— 不能进确定性路由。"""
    registry = _namespaced_registry(tmp_path)
    spec = registry.resolve("tasks__get")

    assert spec is not None
    assert spec.effect == EFFECT_READ
    assert spec.deterministic_route_allowed is False


def test_official_task_write_tools_keep_high_risk(tmp_path: Path) -> None:
    """本轮只放只读工具，写操作的确认闸门一个都不许动。"""
    registry = _namespaced_registry(tmp_path)

    for name in ("tasks__create", "tasks__cancel", "tasks__run_now",
                 "conditions__create_weather", "conditions__cancel", "conditions__run_now"):
        spec = registry.resolve(name)
        assert spec is not None, name
        assert spec.risk == "high", name
        assert spec.deterministic_route_allowed is False, name
