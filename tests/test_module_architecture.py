from __future__ import annotations

import sys
import tempfile
import types
import unittest
import re
import asyncio
from pathlib import Path


# The workspace Python runtime used in CI verification does not bundle Qdrant.
# These tests exercise core contracts only, so provide its import surface without
# replacing any production behavior.
# IMPORTANT: Only mock if qdrant_client is truly unavailable. If it's already loaded
# (e.g., by other tests), don't replace it to avoid breaking tests that need real Qdrant.
try:
    import qdrant_client
except ImportError:
    sys.modules["qdrant_client"] = types.SimpleNamespace(
        QdrantClient=object,
        models=types.SimpleNamespace(),
    )

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.modules import ModuleRegistry, ModuleSpec
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.runtime import AgentEvent, InMemoryRuntimeStore
from agent_core.tools import ToolRegistry
from agent_tools.modules import build_search_module
from agent_tools.netease import Track, make_netease_search_tool


class ModuleScopeTests(unittest.TestCase):
    def test_runtime_state_is_isolated_by_complete_scope(self) -> None:
        runtime = InMemoryRuntimeStore()
        search = Scope("owner", "desktop", "chat", "search")
        music = Scope("owner", "desktop", "chat", "netease")

        runtime.publish(AgentEvent("turn.started", search, turn_id="search-turn"))
        runtime.publish(AgentEvent("turn.started", music, turn_id="music-turn"))

        self.assertEqual(runtime.state(search).active_turn_id, "search-turn")
        self.assertEqual(runtime.state(music).active_turn_id, "music-turn")

    def test_run_store_rejects_a_different_module_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteRunStore(Path(directory) / "runs.db")
            search = Scope("owner", "desktop", "chat", "search")
            store.save(__import__("agent_core.contracts", fromlist=["RunSnapshot"]).RunSnapshot(
                "run-1", search, [], 0, 0, "completed"
            ))

            self.assertIsNotNone(store.load("run-1", search))
            self.assertIsNone(store.load("run-1", Scope("owner", "desktop", "chat", "netease")))

    def test_confirmation_store_rejects_a_different_module_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteConfirmationStore(Path(directory) / "runs.db")
            search = Scope("owner", "desktop", "chat", "search")
            request = store.create(
                run_id="run-1", scope=search, tool_call_id="call-1",
                tool="search__web", arguments={}, title="Search",
            )

            self.assertEqual(request.module_id, "search")
            self.assertIsNotNone(store.get(request.action_id, search))
            self.assertIsNone(store.get(request.action_id, Scope("owner", "desktop", "chat", "netease")))


class ModuleRegistryTests(unittest.TestCase):
    def test_specs_returns_stable_metadata(self) -> None:
        modules = ModuleRegistry()
        modules.register(ModuleSpec("zeta", lambda: [], description="Z"))
        modules.register(ModuleSpec("alpha", lambda: [], description="A"))
        self.assertEqual([item.module_id for item in modules.specs()], ["alpha", "zeta"])
        self.assertEqual(modules.specs()[0].description, "A")

    @staticmethod
    def _tool(name: str) -> ToolSpec:
        return ToolSpec(
            name=name,
            schema={"type": "function", "function": {"name": name, "parameters": {"type": "object"}}},
            handler=lambda _arguments, _scope: {"ok": True},
        )

    def test_module_registry_namespaces_tools_and_builds_selected_registry(self) -> None:
        modules = ModuleRegistry()
        modules.register(ModuleSpec("search", lambda: [self._tool("web")], description="联网搜索公开信息"))
        modules.register(ModuleSpec("netease", lambda: [self._tool("search")]))

        registry = modules.tools_for({"search", "netease"})

        self.assertIsNotNone(registry.resolve("search__web"))
        self.assertIsNotNone(registry.resolve("netease__search"))

    def test_module_registry_lifecycle_is_idempotent_and_stops_in_reverse_order(self) -> None:
        events: list[str] = []

        async def start(name: str) -> None:
            events.append(f"start:{name}")

        async def stop(name: str) -> None:
            events.append(f"stop:{name}")

        modules = ModuleRegistry()
        modules.register(ModuleSpec("alpha", lambda: [], start=lambda: start("alpha"), stop=lambda: stop("alpha")))
        modules.register(ModuleSpec("beta", lambda: [], start=lambda: start("beta"), stop=lambda: stop("beta")))

        asyncio.run(modules.start_all())
        asyncio.run(modules.start_all())
        asyncio.run(modules.stop_all())
        asyncio.run(modules.stop_all())

        self.assertEqual(events, ["start:alpha", "start:beta", "stop:beta", "stop:alpha"])

    def test_namespaced_schema_describes_capability_owner(self) -> None:
        modules = ModuleRegistry()
        modules.register(ModuleSpec("search", lambda: [self._tool("web")], description="联网搜索公开信息"))

        tool = modules.tools_for({"search"}).resolve("search__web")

        self.assertIsNotNone(tool)
        self.assertEqual(
            tool.schema["function"]["description"],
            "【联网搜索公开信息】工具 web。",
        )

    def test_namespacing_preserves_policy_fields(self) -> None:
        """模块包装（_namespaced_tool）必须把授权与路由策略字段原样搬过去。

        回归保护：2026-09-13 实测，包装层漏搬 intent_policies 让意图闸门在生产
        空转（registry.intent_policies() 恒为空）、漏搬 effect+deterministic_route_allowed
        让只读强路由静默回落给模型。新增 ToolSpec 策略字段时，此用例与
        _namespaced_tool 的字段清单要同步扩展。

        `argument_validator` 是同一类字段，而且漏搬的后果更隐蔽：校验函数还挂在
        裸 spec 上、单测照过，只有生产路径（模型走网关）上它不见了。
        """
        from agent_core.contracts import IntentPolicy, ToolArgumentViolation

        policy = IntentPolicy(
            intent_name="weather_query",
            object_terms=("天气",),
            action_terms=("查询",),
            required_slots=("location",),
        )

        def validator(_arguments):
            return ToolArgumentViolation(error="demo")

        source = ToolSpec(
            name="web",
            schema={"type": "function", "function": {"name": "web", "parameters": {"type": "object"}}},
            handler=lambda _arguments, _scope: {"ok": True},
            effect=EFFECT_READ,
            hard_routable=True,
            deterministic_route_allowed=True,
            query_extractor="fact",
            intent_policies=(policy,),
            argument_validator=validator,
        )
        modules = ModuleRegistry()
        modules.register(ModuleSpec("search", lambda: [source], description="联网搜索公开信息"))

        tool = modules.tools_for({"search"}).resolve("search__web")

        self.assertIsNotNone(tool)
        self.assertEqual(tool.effect, EFFECT_READ)
        self.assertTrue(tool.hard_routable)
        self.assertTrue(tool.deterministic_route_allowed)
        self.assertEqual(tool.query_extractor, "fact")
        self.assertEqual(tool.intent_policies, (policy,))
        self.assertIs(tool.argument_validator, validator)

    def test_tool_registry_rejects_duplicate_names(self) -> None:
        registry = ToolRegistry()
        registry.register(self._tool("search__web"))

        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register(self._tool("search__web"))

    def test_search_module_exposes_a_namespaced_web_tool(self) -> None:
        modules = ModuleRegistry()
        modules.register(build_search_module(lambda: self._tool("web")))

        registry = modules.tools_for({"search"})

        tool = registry.resolve("search__web")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.schema["function"]["name"], "search__web")
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name))

    def test_netease_read_tool_returns_provider_tracks(self) -> None:
        class Catalog:
            def search_tracks(self, query: str, *, limit: int) -> list[Track]:
                return [Track("1", f"{query} song", "artist", "album")][:limit]

        spec = make_netease_search_tool(Catalog())
        result = spec.handler({"query": "jazz", "limit": 5}, Scope("owner", "desktop", "chat", "netease"))

        self.assertEqual(spec.name, "search_tracks")
        self.assertEqual(result["output"]["tracks"][0]["title"], "jazz song")


# ──────────────────────── work__* 的注册来源（单点） ────────────────────────


def test_work_tools_have_exactly_one_registration_source() -> None:
    """源码扫描：没有任何地方直接注册字面量 ``work__*`` 工具名。

    2026-09-14 之前，5 个单步任务工具由 `web_api.py` 私有注册，和 work 模块产出的
    计划工具形成**两个来源**：CLI 与 Web 的工具集分叉，重名只能靠 "先 resolve 再注册"
    这种运行时判断绕开，能力快照也数不全。现在唯一来源是 `agent_tools/work.py` ——
    它产出的是**本地名**（`submit` …），由模块注册流程统一加 `work__` 前缀。

    所以这条不是"允许某个文件注册"，而是"谁都不许直接注册"：新增一个
    `ToolSpec("work__xxx", ...)` 会当场把这个用例打红。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    pattern = re.compile(r"ToolSpec\(\s*[\"']work__")
    search_roots = [
        *(root / name for name in ("agent_core", "agent_tools", "agent_core_adapters", "examples")),
        *root.glob("*.py"),
    ]
    offenders: list[str] = []
    for target in search_roots:
        paths = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if pattern.search(text):
                offenders.append(path.relative_to(root).as_posix())

    assert offenders == [], f"work__* 又被直接注册了，唯一来源应是 agent_tools/work.py：{offenders}"


if __name__ == "__main__":
    unittest.main()
