from __future__ import annotations

import asyncio
import logging

import pytest

from agent_core.contracts import Scope, ToolSpec
from agent_core.modules import ModuleRegistry, ModuleSpec
def _work_tool_names() -> tuple[str, ...]:
    """work 的预期工具名。**从能力目录读**，不再从 `agent_tools.work` 读。

    目录是唯一一份人手写的预期声明；模块的 `declared_tools` 是从真实产出提取的。
    测试要验的是"这三者一致"，所以它自己不能变成第四份名单 ——
    写死四个名字会让"有人改了目录却没改这里"变成一次静默的假绿。
    """
    from agent_core.capability_catalog import build_catalog

    entry = build_catalog().entry("work")
    assert entry is not None, "work 必须在能力目录里"
    return entry.declared_tools


def _tool(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        schema={"type": "function", "function": {"name": name, "parameters": {"type": "object"}}},
        handler=lambda _arguments, _scope: {"ok": True},
    )


def test_declared_tool_missing_from_builder_is_visible_as_unregistered() -> None:
    modules = ModuleRegistry()
    modules.register(
        ModuleSpec(
            "music",
            lambda: [],
            description="Music controls",
            declared_tools=("player_status",),
        )
    )

    registry = modules.tools_for({"music"})
    snapshot = modules.capability_snapshot(registry)

    assert list(registry) == []
    assert snapshot["modules"] == [
        {
            "id": "music",
            "description": "Music controls",
            "status": "degraded",
            "tools": [
                {
                    "name": "music__player_status",
                    "declared": True,
                    "registration": "unregistered",
                    "advertised_to_model": False,
                    "code": "tool_unregistered",
                    "reason": "tool was declared but not registered",
                }
            ],
        }
    ]


def test_unavailable_module_keeps_its_declared_tools_in_snapshot() -> None:
    modules = ModuleRegistry()
    modules.record_unavailable_module(
        "music",
        description="Music controls",
        declared_tools=("player_status", "play"),
        code="module_factory_failed",
        reason="module factory failed",
    )

    snapshot = modules.capability_snapshot(__import__("agent_core.tools", fromlist=["ToolRegistry"]).ToolRegistry())

    assert snapshot["modules"] == [
        {
            "id": "music",
            "description": "Music controls",
            "status": "degraded",
            "tools": [
                {
                    "name": "music__play",
                    "declared": True,
                    "registration": "unregistered",
                    "advertised_to_model": False,
                    "code": "module_factory_failed",
                    "reason": "module factory failed",
                },
                {
                    "name": "music__player_status",
                    "declared": True,
                    "registration": "unregistered",
                    "advertised_to_model": False,
                    "code": "module_factory_failed",
                    "reason": "module factory failed",
                },
            ],
        }
    ]


def test_registered_tool_missing_from_advertised_registry_is_degraded() -> None:
    from agent_core.tools import ToolRegistry

    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [_tool("search")], declared_tools=("search",)))
    modules.tools_for({"music"})

    snapshot = modules.capability_snapshot(ToolRegistry())

    assert snapshot["modules"][0]["status"] == "degraded"
    assert snapshot["modules"][0]["tools"] == [{
        "name": "music__search",
        "declared": True,
        "registration": "registered",
        "advertised_to_model": False,
        "code": "tool_not_advertised",
        "reason": "registered tool is not advertised to the model",
    }]


def test_unavailable_required_module_still_participates_in_readiness() -> None:
    modules = ModuleRegistry()
    modules.record_unavailable_module(
        "music",
        description="Music controls",
        declared_tools=("status",),
        code="module_factory_failed",
        reason="module factory failed",
        required_for_readiness=True,
    )

    assert modules.required_for_readiness("music") is True


@pytest.mark.asyncio
async def test_capability_snapshot_attaches_generic_runtime_probe_results() -> None:
    calls: list[bool] = []

    async def probe(*, force: bool = False):
        calls.append(force)
        return [{
            "name": "mcp",
            "status": "healthy",
            "code": "",
            "reason": "",
        }]

    modules = ModuleRegistry()
    modules.register(
        ModuleSpec(
            "music",
            lambda: [_tool("search")],
            declared_tools=("search",),
            probe=probe,
        )
    )
    registry = modules.tools_for({"music"})

    snapshot = await modules.capability_snapshot_async(registry, force=True)

    assert calls == [True]
    assert snapshot["modules"][0]["status"] == "healthy"
    assert snapshot["modules"][0]["dependencies"] == [
        {"name": "mcp", "status": "healthy", "code": "", "reason": ""}
    ]


@pytest.mark.asyncio
async def test_capability_snapshot_projects_external_tool_execution_state() -> None:
    async def probe(*, force: bool = False):
        return [
            {
                "name": "mcp",
                "status": "healthy",
                "code": "",
                "reason": "",
                "tools": ["cloud_music_play", "cloud_music_player_status"],
            },
            {"name": "player", "status": "healthy", "code": "", "reason": ""},
        ]

    modules = ModuleRegistry()
    modules.register(ModuleSpec(
        "music",
        lambda: [_tool("play"), _tool("player_status")],
        declared_tools=("play", "player_status"),
        probe=probe,
        external_tool_names={
            "play": "cloud_music_play",
            "player_status": "cloud_music_player_status",
        },
    ))

    snapshot = await modules.capability_snapshot_async(modules.tools_for({"music"}))

    execution = {tool["name"]: tool["execution"] for tool in snapshot["modules"][0]["tools"]}
    assert execution == {
        "music__play": "catalog_available",
        "music__player_status": "verified",
    }


@pytest.mark.asyncio
async def test_capability_snapshot_bounds_stalled_generic_probes(monkeypatch) -> None:
    import agent_core.modules as module_api

    async def stalled_probe(*, force: bool = False):
        await asyncio.sleep(0.1)
        return []

    monkeypatch.setattr(module_api, "CAPABILITY_PROBE_TIMEOUT_SECONDS", 0.01, raising=False)
    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [_tool("search")], probe=stalled_probe))

    snapshot = await modules.capability_snapshot_async(modules.tools_for({"music"}), force=True)

    assert snapshot["modules"][0]["dependencies"] == [{
        "name": "runtime",
        "status": "unhealthy",
        "code": "probe_timeout",
        "reason": "capability probe timed out",
    }]


def test_tool_builder_failure_is_logged_without_exposing_exception(caplog) -> None:
    def broken_builder():
        raise RuntimeError("private launch path")

    caplog.set_level(logging.ERROR, logger="agent_core.modules")
    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", broken_builder, declared_tools=("search",)))

    modules.tools_for({"music"})

    assert "capability tool builder failed (music)" in caplog.messages
    assert modules.capability_snapshot(__import__("agent_core.tools", fromlist=["ToolRegistry"]).ToolRegistry())["modules"][0]["tools"][0]["reason"] == "tool builder failed before registration"


def test_duplicate_tool_registration_keeps_working_tool_and_records_failure() -> None:
    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [_tool("search"), _tool("search")], declared_tools=("search",)))

    registry = modules.tools_for({"music"})
    snapshot = modules.capability_snapshot(registry)

    assert snapshot["modules"] == [{
        "id": "music",
        "description": "",
        "status": "degraded",
        "code": "tool_registration_failed",
        "reason": "one or more tools could not be registered",
        "registration_errors": [{
            "name": "music__search",
            "code": "tool_registration_failed",
            "reason": "tool could not be registered",
        }],
            "tools": [{
                "name": "music__search",
                "declared": True,
                "registration": "registered",
            "advertised_to_model": True,
            "code": "",
            "reason": "",
        }],
    }]


def test_snapshot_distinguishes_declared_tools_from_builder_only_tools() -> None:
    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [_tool("search"), _tool("internal")], declared_tools=("search",)))

    snapshot = modules.capability_snapshot(modules.tools_for({"music"}))

    states = {tool["name"]: tool["declared"] for tool in snapshot["modules"][0]["tools"]}
    assert states == {"music__internal": False, "music__search": True}


def test_snapshot_retains_an_invalid_builder_tool() -> None:
    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [_tool("not-valid")], declared_tools=("search",)))

    snapshot = modules.capability_snapshot(modules.tools_for({"music"}))

    assert snapshot["modules"][0]["status"] == "degraded"
    assert snapshot["modules"][0]["tools"] == [{
        "name": "music__not-valid",
        "declared": False,
        "registration": "invalid",
        "advertised_to_model": False,
        "code": "tool_registration_failed",
        "reason": "tool could not be registered",
    }, {
        "name": "music__search",
        "declared": True,
        "registration": "unregistered",
        "advertised_to_model": False,
        "code": "tool_unregistered",
        "reason": "tool was declared but not registered",
    }]


def test_module_factory_failure_is_logged_without_exposing_exception(monkeypatch, caplog) -> None:
    import agent_tools
    import agent_tools.modules as module_factories

    def broken_search_module():
        raise RuntimeError("private factory detail")

    monkeypatch.setattr(module_factories, "build_search_module", broken_search_module)
    # 日志记录器搬到能力目录了（失败由 `CapabilityCatalog.resolve_module` 捕获）。
    caplog.set_level(logging.ERROR, logger="agent_core.capability_catalog")

    _, failures = agent_tools.discover_modules()

    assert failures["search"].startswith("RuntimeError:")
    assert "capability module factory failed (search)" in caplog.messages


def test_builtin_modules_declare_their_expected_tool_names(tmp_path) -> None:
    from agent_core.media_module import build_media_module
    from agent_core.work_module import build_work_module
    from agent_tools.modules import (
        build_condition_task_module,
        build_location_module,
        build_navigation_module,
        build_netease_module,
        build_search_module,
        build_system_module,
        build_task_module,
    )
    from agent_tools.location import LocationStore

    # 搜索模块现含四个工具：网页搜索、B站搜索、B站视频详情、网页原文读取。
    assert build_search_module(lambda: _tool("web")).declared_tools == ("web", "bilibili", "bilibili_video", "read")
    assert "player_status" in build_netease_module(object()).declared_tools
    assert build_system_module().declared_tools == ("local_info",)
    assert build_location_module(LocationStore()).declared_tools == ("current",)
    assert build_navigation_module(api_key="test-key")[0].declared_tools == ("route",)
    assert build_task_module(db_path=tmp_path / "tasks.db")[0].declared_tools == (
        "create", "list", "get", "cancel", "run_now",
    )
    assert build_condition_task_module(db_path=tmp_path / "conditions.db")[0].declared_tools == (
        "create_weather", "list", "cancel", "run_now",
    )
    # ASR 未实现，submit_asr 暂不注册/声明（见 agent_core/media_module.py 的注释）。
    assert build_media_module(db_path=tmp_path / "media.db").declared_tools == (
        "submit_tts", "get_status", "cancel", "get_artifact",
    )
    # work 是聊天侧后台任务的**唯一**注册来源，且声明**恰好 4 个**入口
    # （2026-09-22 从 9 个收窄：删 approve、把 list/get 并进 status、
    # 把 cancel/plan_cancel 并进 cancel；理由见 agent_tools/work.py 的模块头）。
    assert build_work_module(db_path=tmp_path / "work.db")[0].declared_tools == _work_tool_names()


# ──────────────────────── work 入口收拢（2026-09-14） ────────────────────────


def test_work_manifest_matches_the_module_declaration(tmp_path) -> None:
    """能力目录里的 work 声明必须和 work 模块自己的 declared_tools 逐字一致。

    **2026-09-21 改写**：原来比的是 `agent_tools.BUILTIN_MODULE_MANIFESTS`，
    那份静态清单已删（它是"第二份真相"，漏改就出洞 —— weather 的 KeyError）。
    现在唯一的声明在 `CapabilityCatalog` 里。

    被保证的东西没变：**存在一份"工厂挂了也能报出缺哪些工具"的声明**，
    且它与真实实现一致。差别是现在只有一份，不可能与自己漂移。
    跨模块的同一条保证由 `tests/test_capability_catalog.py` 守（它遍历全部模块）。
    """
    from agent_core.capability_catalog import build_catalog
    from agent_core.work_module import build_work_module

    spec, _host = build_work_module(db_path=tmp_path / "work.db")
    catalog_entry = build_catalog().entry("work")
    expected = _work_tool_names()

    assert catalog_entry is not None, "work 必须在能力目录里"
    assert catalog_entry.declared_tools == expected
    # 模块的声明是**从真实产出提取**的，所以这一条同时把"产出"也钉住了：
    # 三者（目录声明 / 模块声明 / 真实产出）任意两个不一致都会红。
    assert spec.declared_tools == expected
    assert tuple(item.name for item in spec.build_tools()) == expected


def test_work_module_builds_every_declared_tool_without_duplicate_registration(tmp_path) -> None:
    from agent_core.work_module import build_work_module

    spec, _host = build_work_module(db_path=tmp_path / "work.db")
    modules = ModuleRegistry()
    modules.register(spec)

    registry = modules.tools_for({"work"})
    for name in spec.declared_tools:
        assert registry.resolve(f"work__{name}") is not None, f"work__{name} 没被构造出来"

    # 再构建一次：同一个模块重复产工具必须仍然只有一份（重名注册会当场 raise）。
    again = modules.tools_for({"work"})
    assert len(list(again)) == len(spec.declared_tools)

    snapshot = modules.capability_snapshot(again)
    assert snapshot["modules"][0]["status"] == "healthy"


def test_disabled_work_module_still_declares_every_tool(monkeypatch, tmp_path) -> None:
    """显式禁用 work 时，聊天工具整体消失，但能力快照必须仍然列出"本该有哪些"。"""
    from agent_core.tools import ToolRegistry
    from agent_tools import register_modules

    monkeypatch.setenv("AGENT_DISABLED_MODULES", "work")
    monkeypatch.setenv("AGENT_WORK_DB", str(tmp_path / "work.db"))
    modules = ModuleRegistry()

    enabled = register_modules(modules)
    snapshot = modules.capability_snapshot(ToolRegistry())

    assert "work" not in enabled
    work = next(module for module in snapshot["modules"] if module["id"] == "work")
    assert work["status"] == "degraded"
    assert [tool["name"] for tool in work["tools"]] == sorted(f"work__{name}" for name in _work_tool_names())
    assert {tool["code"] for tool in work["tools"]} == {"module_disabled"}
    assert all(tool["advertised_to_model"] is False for tool in work["tools"])


def test_disabled_builtin_module_remains_visible_in_registration_snapshot(monkeypatch) -> None:
    from agent_core.tools import ToolRegistry
    from agent_tools import register_modules

    monkeypatch.setenv("AGENT_DISABLED_MODULES", "netease")
    modules = ModuleRegistry()

    enabled = register_modules(modules)
    snapshot = modules.capability_snapshot(ToolRegistry())

    assert "netease" not in enabled
    netease = next(module for module in snapshot["modules"] if module["id"] == "netease")
    assert netease["status"] == "degraded"
    player = next(tool for tool in netease["tools"] if tool["name"] == "netease__player_status")
    assert player == {
        "name": "netease__player_status",
        "declared": True,
        "registration": "unregistered",
        "advertised_to_model": False,
        "code": "module_disabled",
        "reason": "module is disabled by configuration",
    }
