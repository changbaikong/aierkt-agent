"""集成测试：验证多模块注册和工具命名空间隔离。"""
from __future__ import annotations

import unittest

from agent_core.contracts import Scope
from agent_core.modules import ModuleRegistry
from agent_tools import discover_modules, register_modules
from agent_tools.modules import build_search_module
from agent_tools.netease import Track


class MockSearchTool:
    """模拟搜索工具以避免 BOCHA_API_KEY 依赖。"""
    @staticmethod
    def build():
        from agent_core.contracts import ToolSpec
        return ToolSpec(
            name="web",
            schema={"type": "function", "function": {"name": "web", "parameters": {"type": "object"}}},
            handler=lambda args, scope: {"status": "ok", "output": {"results": ["mock result"]}},
        )


class MultiModuleIntegrationTests(unittest.TestCase):
    def test_builtin_discovery_returns_module_metadata_and_skips_disabled_modules(self) -> None:
        discovered, disabled = discover_modules(disabled_modules={"search"})
        ids = {spec.module_id for spec in discovered}
        self.assertIn("system", ids)
        self.assertIn("netease", ids)
        self.assertNotIn("search", ids)
        self.assertIn("search", disabled)
        system = next(spec for spec in discovered if spec.module_id == "system")
        self.assertTrue(system.description)

    def test_multiple_modules_register_without_collision(self) -> None:
        """验证多个模块可以同时注册，工具名使用模块前缀避免冲突。"""
        from examples.netease_mock import MockNeteaseCatalog
        from agent_tools.modules import build_netease_module

        modules = ModuleRegistry()
        modules.register(build_search_module(MockSearchTool.build))
        modules.register(build_netease_module(MockNeteaseCatalog()))

        registry = modules.tools_for({"search", "netease"})

        # 验证工具名带模块前缀
        self.assertIsNotNone(registry.resolve("search__web"))
        self.assertIsNotNone(registry.resolve("netease__search_tracks"))
        # 验证无前缀的原始名称无法解析
        self.assertIsNone(registry.resolve("web"))
        self.assertIsNone(registry.resolve("search_tracks"))

    def test_module_tools_are_independently_resolvable(self) -> None:
        """验证只启用部分模块时，只有对应的工具可用。"""
        from examples.netease_mock import MockNeteaseCatalog
        from agent_tools.modules import build_netease_module

        modules = ModuleRegistry()
        modules.register(build_search_module(MockSearchTool.build))
        modules.register(build_netease_module(MockNeteaseCatalog()))

        # 只启用 netease 模块
        registry = modules.tools_for({"netease"})

        self.assertIsNone(registry.resolve("search__web"))
        self.assertIsNotNone(registry.resolve("netease__search_tracks"))

    def test_netease_tool_handler_returns_tracks(self) -> None:
        """验证 netease 工具可以正常调用 provider。"""
        from examples.netease_mock import MockNeteaseCatalog
        from agent_tools.modules import build_netease_module

        modules = ModuleRegistry()
        modules.register(build_netease_module(MockNeteaseCatalog()))
        registry = modules.tools_for({"netease"})

        tool = registry.resolve("netease__search_tracks")
        self.assertIsNotNone(tool)

        result = tool.handler(
            {"query": "blue", "limit": 2},
            Scope("owner", "desktop", "chat", "netease"),
        )

        self.assertEqual(result["status"], "ok")
        self.assertIn("tracks", result["output"])
        self.assertEqual(len(result["output"]["tracks"]), 2)
        self.assertEqual(result["output"]["tracks"][0]["title"], "So What")

    def test_register_modules_integrates_both_search_and_netease(self) -> None:
        """验证 register_modules() 能同时注册 search 和 netease（mock）。"""
        modules = ModuleRegistry()
        enabled = register_modules(modules)

        # netease 应该成功（有 mock provider）
        self.assertIn("netease", enabled)
        registry = modules.tools_for(set(enabled))
        self.assertIsNotNone(registry.resolve("netease__search_tracks"))

    def test_register_modules_includes_media_by_default(self) -> None:
        modules = ModuleRegistry()
        enabled = register_modules(modules)
        self.assertIn("media", enabled)
        self.assertIsNotNone(modules.tools_for(set(enabled)).resolve("media__submit_tts"))

    def test_register_modules_preserves_task_llm_provider(self) -> None:
        modules = ModuleRegistry()
        provider = object()
        enabled = register_modules(modules, provider=provider)
        self.assertIn("tasks", enabled)
        self.assertIs(modules.task_service.provider, provider)

    def test_register_modules_wires_task_registry_after_composition(self) -> None:
        modules = ModuleRegistry()
        enabled = register_modules(modules)
        registry = modules.tools_for(set(enabled))
        self.assertIsNotNone(modules.task_service.registry_provider().resolve("tasks__get"))

    def test_register_modules_wires_condition_registry_after_composition(self) -> None:
        modules = ModuleRegistry()
        enabled = register_modules(modules)

        self.assertIn("conditions", enabled)
        self.assertIsNotNone(modules.condition_task_service.registry_provider().resolve("conditions__list"))


def test_register_modules_exposes_every_work_tool_to_the_chat(monkeypatch, tmp_path) -> None:
    """CLI 和 Web 走同一条注册链：work 模块产出**精确 4 个** work__* 工具。

    2026-09-14 之前，5 个单步任务工具是 web_api 私有注册的，CLI 只有计划工具；
    现在两边都从模块拿，两个入口的工具集必须一致。

    预期名单**从能力目录读**（唯一一份人手写的声明），不在这里再写一遍 ——
    测试自己变成第四份名单，就会在"有人改了目录"时给出一次假绿。
    """
    from agent_core.capability_catalog import build_catalog

    monkeypatch.setenv("AGENT_WORK_DB", str(tmp_path / "work.db"))
    modules = ModuleRegistry()

    enabled = register_modules(modules)
    registry = modules.tools_for(set(enabled))

    expected = build_catalog().entry("work").declared_tools
    assert expected == ("submit", "plan_submit", "status", "cancel")

    assert "work" in enabled
    for name in expected:
        assert registry.resolve(f"work__{name}") is not None, f"work__{name} 没注册"
    assert len([spec for spec in registry if spec.name.startswith("work__")]) == len(expected)


if __name__ == "__main__":
    unittest.main()
