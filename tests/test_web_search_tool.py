"""web_search 工具的纯逻辑测试 + 即插即用/降级/超时隔离测试（离线，无真实网络）。"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

from agent_core.contracts import Scope, ToolCall
from agent_core.modules import ModuleRegistry
from agent_core.tools import ToolExecutor, ToolRegistry

from agent_tools.web_search import (
    BingRssSearchProvider,
    FRESHNESS_OPTIONS,
    MAX_RESULTS_DEFAULT,
    MAX_RESULTS_MAX,
    MAX_RESULTS_MIN,
    SearchResult,
    WEB_SEARCH_SCHEMA,
    build_web_search_tool,
    make_web_search_tool,
    MultiSourceWebSearchProvider,
)
from agent_tools.agent_reach_router import AgentReachRouter


class _FakeProvider:
    """手写 fake provider：可配置结果 / 异常 / 延迟，并记录每次调用。"""

    def __init__(self, results=None, error=None, delay: float = 0.0):
        self._results = results or []
        self._error = error
        self._delay = delay
        self.calls: list[tuple] = []

    def search(self, query, *, max_results=MAX_RESULTS_DEFAULT, freshness="noLimit"):
        if self._delay:
            time.sleep(self._delay)
        if self._error:
            raise self._error
        self.calls.append((query, max_results, freshness))
        return self._results


class _NamedProvider(_FakeProvider):
    def __init__(self, name, results=None, error=None):
        super().__init__(results=results, error=error)
        self.name = name


def _execute(spec, arguments: dict) -> "any":
    """让 ToolExecutor 走完整归一化路径（含超时兜底）。"""
    return ToolExecutor().execute(
        spec,
        ToolCall(call_id="c1", name="web_search", arguments=arguments),
        Scope(),
    )


class SchemaTests(unittest.TestCase):
    def test_schema_shape(self):
        self.assertEqual(WEB_SEARCH_SCHEMA["type"], "function")
        function = WEB_SEARCH_SCHEMA["function"]
        self.assertEqual(function["name"], "web_search")
        params = function["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["required"], ["query"])
        self.assertIs(params["additionalProperties"], False)
        self.assertEqual(params["properties"]["freshness"]["enum"], list(FRESHNESS_OPTIONS))

    def test_default_risk_is_low(self):
        spec = make_web_search_tool(_FakeProvider())
        self.assertEqual(spec.risk, "low")
        self.assertFalse(spec.requires_confirmation)


class HandlerTests(unittest.TestCase):
    def test_results_carry_platform_and_publish_date(self):
        """结果新增 platform / published_at 字段；旧字段原样保留（对外兼容）。"""
        provider = _FakeProvider([SearchResult(title="T", url="https://t.com", snippet="s", published_at="2026-10-01")])
        spec = make_web_search_tool(provider)
        result = spec.handler({"query": "hello"}, Scope())
        row = result["output"]["results"][0]
        self.assertEqual(row["platform"], "web")
        self.assertEqual(row["published_at"], "2026-10-01")
        for legacy_key in ("title", "url", "snippet", "summary"):
            self.assertIn(legacy_key, row)

    def test_happy_path_passes_arguments_to_provider(self):
        provider = _FakeProvider([SearchResult(title="T", url="https://t.com", snippet="s")])
        spec = make_web_search_tool(provider)
        result = spec.handler({"query": "hello", "max_results": 3}, Scope())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"]["result_count"], 1)
        self.assertEqual(result["output"]["results"][0]["title"], "T")
        self.assertEqual(provider.calls, [("hello", 3, "noLimit")])

    def test_validation_errors_return_failed(self):
        spec = make_web_search_tool(_FakeProvider())
        cases = [
            {},  # 缺 query
            {"query": ""},  # 空 query
            {"query": "   "},  # 全空白 query
            {"query": 123},  # 类型错
            {"query": "x" * 501},  # 超长
            {"query": "q", "max_results": "5"},  # 非整数
            {"query": "q", "max_results": True},  # bool 是 int 子类，要拒绝
            {"query": "q", "freshness": "bad"},  # 非法枚举
        ]
        for args in cases:
            with self.subTest(args=args):
                result = spec.handler(args, Scope())
                self.assertEqual(result["status"], "failed")

    def test_long_research_query_within_limit_is_accepted(self):
        provider = _FakeProvider()
        spec = make_web_search_tool(provider)
        result = spec.handler({"query": "x" * 500}, Scope())
        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.calls[0][0], "x" * 500)

    def test_subagent_research_query_is_compacted_to_topic(self):
        provider = _FakeProvider()
        spec = make_web_search_tool(provider)
        result = spec.handler(
            {"query": "任务：只读研究，不修改任何文件。\n目标：查找数据库迁移失败原因。\n1. 查询多个来源并比较。"},
            Scope(module_id="companion/subabcd"),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.calls[0][0], "查找数据库迁移失败原因。")

    def test_schema_accepts_long_delegated_task_envelope(self):
        query = "研究数据库迁移失败原因。\n" + ("要求查询多个来源并比较。" * 80)
        self.assertGreater(len(query), 500)
        self.assertLessEqual(
            len(query), WEB_SEARCH_SCHEMA["function"]["parameters"]["properties"]["query"]["maxLength"]
        )

    def test_subagent_prefers_quoted_search_terms(self):
        provider = _FakeProvider()
        spec = make_web_search_tool(provider)
        result = spec.handler(
            {"query": "任务：比较学习曲线。\n查询：`Python vs Rust learning curve`、`Rust difficulty`。"},
            Scope(module_id="companion/subabcd"),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.calls[0][0], "Python vs Rust learning curve Rust difficulty")

    def test_subagent_extracts_unquoted_latin_topics(self):
        provider = _FakeProvider()
        spec = make_web_search_tool(provider)
        result = spec.handler(
            {"query": "比较远程办公（remote work / WFH）效率的结论，参考 NBER 和 Stanford。"},
            Scope(module_id="companion/subabcd"),
        )
        self.assertEqual(result["status"], "ok")
        self.assertIn("remote work", provider.calls[0][0])
        self.assertIn("NBER", provider.calls[0][0])

    def test_clamp_max_results(self):
        provider = _FakeProvider()
        spec = make_web_search_tool(provider)
        spec.handler({"query": "q", "max_results": 0}, Scope())  # 0 → 1
        spec.handler({"query": "q", "max_results": 100}, Scope())  # 100 → 10
        self.assertEqual(provider.calls[0][1], MAX_RESULTS_MIN)
        self.assertEqual(provider.calls[1][1], MAX_RESULTS_MAX)

    def test_provider_error_returns_failed(self):
        spec = make_web_search_tool(_FakeProvider(error=RuntimeError("network down")))
        result = spec.handler({"query": "q"}, Scope())
        self.assertEqual(result["status"], "failed")
        self.assertIn("web_search provider error", result["error"])

    def test_scope_is_echoed_with_coverage_metadata(self):
        provider = _FakeProvider([SearchResult(title="T", url="https://t.com")])
        spec = make_web_search_tool(provider)
        result = spec.handler(
            {"query": "q", "scope": {"platforms": ["web", "github"], "time_range": "oneMonth"}},
            Scope(),
        )
        assert result["output"]["scope"]["platforms"] == ["web", "github"]
        assert result["output"]["coverage"]["requested_platforms"] == ["web", "github"]

    def test_scope_time_range_is_applied_to_provider_freshness(self):
        provider = _FakeProvider([SearchResult(title="T", url="https://t.com")])
        spec = make_web_search_tool(provider)
        result = spec.handler(
            {"query": "q", "scope": {"platforms": ["web"], "time_range": "oneYear"}},
            Scope(),
        )
        assert result["status"] == "ok"
        assert provider.calls[0][2] == "oneYear"

    def test_scope_without_time_range_keeps_explicit_freshness(self):
        provider = _FakeProvider()
        spec = make_web_search_tool(provider)
        result = spec.handler(
            {"query": "q", "freshness": "oneWeek", "scope": {"platforms": ["web"]}},
            Scope(),
        )
        assert result["status"] == "ok"
        assert provider.calls[0][2] == "oneWeek"

    def test_agent_reach_availability_is_reported_without_claiming_fetch(self):
        provider = _FakeProvider([SearchResult(title="T", url="https://t.com")])
        router = AgentReachRouter(runner=lambda _: '{"github": {"active_backend": "gh"}}')
        router.available_platforms()  # 暖缓存：搜索热路径只读缓存，不内联等 doctor
        spec = make_web_search_tool(provider, router=router)
        result = spec.handler({"query": "q", "scope": {"platforms": ["web", "github"]}}, Scope())
        coverage = result["output"]["coverage"]
        assert coverage["available_platforms"] == ["github"]
        assert coverage["active_platforms"] == ["web"]


class MultiSourceProviderTests(unittest.TestCase):
    def test_merges_independent_sources_and_deduplicates_urls(self):
        provider = MultiSourceWebSearchProvider((
            ("bocha", _NamedProvider("bocha", [
                SearchResult(title="A", url="https://same.example/a", snippet="one"),
            ])),
            ("bing", _NamedProvider("bing", [
                SearchResult(title="A copy", url="https://same.example/a", snippet="duplicate"),
                SearchResult(title="B", url="https://other.example/b", snippet="two"),
            ])),
        ))

        rows = provider.search("topic", max_results=5)

        self.assertEqual([row.url for row in rows], ["https://same.example/a", "https://other.example/b"])
        self.assertEqual([row.platform for row in rows], ["web:bocha", "web:bing"])

    def test_one_source_failure_keeps_other_source_results(self):
        provider = MultiSourceWebSearchProvider((
            ("bocha", _NamedProvider("bocha", error=RuntimeError("down"))),
            ("bing", _NamedProvider("bing", [
                SearchResult(title="B", url="https://other.example/b", snippet="two"),
            ])),
        ))

        rows = provider.search("topic", max_results=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].url, "https://other.example/b")

    def test_bing_rss_parser_extracts_items(self):
        xml = b"""<?xml version='1.0'?><rss><channel>
        <item><title>Result 1</title><link>https://example.com/1</link>
        <description>Snippet 1</description><pubDate>Wed, 08 Oct 2026 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
        provider = BingRssSearchProvider(transport=__import__("httpx").MockTransport(
            lambda request: __import__("httpx").Response(200, content=xml)
        ))

        rows = provider.search("topic", max_results=5)

        self.assertEqual(rows[0].title, "Result 1")
        self.assertEqual(rows[0].platform, "web:bing")


class ExecutorTests(unittest.IsolatedAsyncioTestCase):
    """过 ToolExecutor：验证归一化规则 + 超时兜底不崩。"""

    async def test_executor_normalizes_ok_result(self):
        provider = _FakeProvider([SearchResult(title="T", url="https://t.com")])
        spec = make_web_search_tool(provider)
        result = await _execute(spec, {"query": "hi"})
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.output["results"][0]["title"], "T")

    async def test_executor_normalizes_failed_result(self):
        spec = make_web_search_tool(_FakeProvider(error=RuntimeError("boom")))
        result = await _execute(spec, {"query": "hi"})
        self.assertEqual(result.status, "failed")
        self.assertIn("web_search provider error", result.error)

    async def test_timeout_is_isolated(self):
        spec = make_web_search_tool(_FakeProvider(delay=0.2), timeout_seconds=0.01)
        result = await _execute(spec, {"query": "q"})
        self.assertEqual(result.status, "failed")
        self.assertIn("timed out", result.error)


class RegistrationTests(unittest.TestCase):
    """即插即用/可选降级：search 模块在目录里的注册绝不抛异常。

    **2026-09-21 改写**：原来是 `register_tools(registry)` + patch
    `agent_tools.TOOL_BUILDERS`。那份主动登记机制已被能力目录取代 ——
    `search` 现在就是一个目录条目（像其它九个模块一样），
    不再有"第三种登记工具的方式"。

    被保证的三件事没变，只是换成了目录这条唯一的路：

    1. 没配 key → 该模块在这一次装配里不可用，但**不抛**、不拖垮别人；
    2. 配了 key → 工具真的注册进来，名字是 `search__web`（带命名空间前缀）；
    3. 工厂本身炸了 → 只这一个模块降级，其余照常。

    注释里保留原来那三个名字（`register_tools` / `TOOL_BUILDERS`）作为
    "这里原本测的是什么"的线索，免得下次读时找不到对应关系。
    """

    def _build_search(self, owner: ModuleRegistry) -> ToolRegistry:
        """通过目录装配 search 模块，返回它的工具注册表。"""
        from agent_core.capability_catalog import BuildContext, build_catalog

        catalog = build_catalog()
        module, reason = catalog.resolve_module("search", BuildContext())
        assert module is not None, f"search 模块本身应该总是能建出来：{reason}"
        owner.register(module)
        return owner.tools_for({"search"})

    def test_search_without_key_registers_module_but_no_tool(self):
        """没配 key → **模块**照样注册，`search__web` 这个工具缺位。

        这是"可选能力"的既定降级形状：缺 key 只让这个工具消失，
        不该让整个 search 模块（以及依赖同批装配的其它模块）一起不见。
        所以断言的是"模块在、工具不在"，而不是"什么都不注册"。
        """
        owner = ModuleRegistry()
        with mock.patch(
            "agent_tools.web_search.BoChaWebSearchProvider.from_env", return_value=None
        ):
            registry = self._build_search(owner)
        self.assertIsNone(registry.resolve("search__web"))
        self.assertIsNotNone(owner.resolve("search"), "模块本身要还在")

    def test_search_with_provider_registers_under_namespace(self):
        """配了 key → 工具注册进来，名字带命名空间前缀 `search__`。"""
        owner = ModuleRegistry()
        fake = _FakeProvider([SearchResult(title="T", url="https://t.com")])
        with mock.patch(
            "agent_tools.web_search.BoChaWebSearchProvider.from_env", return_value=fake
        ):
            registry = self._build_search(owner)
        spec = registry.resolve("search__web")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.handler({"query": "q"}, Scope())["status"], "ok")

    def test_broken_search_factory_yields_no_tool_without_raising(self):
        """工厂本身抛异常 → 只没了工具，**不抛**、不波及主体。

        原来 patch 的是 `agent_tools.TOOL_BUILDERS`（一炸就跳过整个注册）；
        现在 `build_search_module.build_tools` 自己吞掉异常返回空列表，
        差别是"降级形状"从"模块消失"变成"工具缺位"——后者在能力快照里
        会显示成 `search__web: tool_unregistered`，比前者信息更多。
        """
        owner = ModuleRegistry()
        with mock.patch(
            "agent_tools.web_search.BoChaWebSearchProvider.from_env",
            side_effect=ImportError("simulated broken module"),
        ):
            registry = self._build_search(owner)  # 不该抛
        self.assertIsNone(registry.resolve("search__web"))
        # 模块级诊断要能说出"一个工具都没注册成功"。
        self.assertEqual(owner.diagnostics()["search"]["status"], "tool_unregistered")

    def test_build_web_search_tool_injectable(self):
        """provider 可注入（测试/替换供应商用），无需真实 key。"""
        spec = build_web_search_tool(_FakeProvider())
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, "web_search")


if __name__ == "__main__":
    unittest.main()
