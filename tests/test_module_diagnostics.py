"""模块工厂失败时，**报告故障的代码自己不能再抛异常**。

【2026-09-21 查明的线上 bug】

`agent_tools/__init__.py` 的 `_record_unavailable` 去查静态清单
`BUILTIN_MODULE_MANIFESTS[module_id]`，但清单里没有 `weather`：

    BUILTIN_MODULE_MANIFESTS 实际键
      = [search, netease, system, media, location, navigation, tasks, conditions, work]

而 `register_modules` 在天气工厂失败、或天气被 `AGENT_DISABLED_MODULES` 禁用时，
照样调 `_record_unavailable(registry, "weather", ...)` → `KeyError: 'weather'`。

后果不是"天气坏了"，而是**天气坏了之后，负责报告这件事的代码崩了**：
能力快照拿不到任何答案，`/api/health` 上的表现是"莫名其妙少一块"，
而不是"天气不可用，原因 X"。

根因与本次工具链封口同源：**模块集合有两份真相**
（真实工厂列表 vs 这份静态清单），加模块时改一处就漏。

【临时修复的边界】

这里断言的是"不再抛异常 + 天气在快照里可见"，
不锁定清单的具体内容 —— 后续 `CapabilityCatalog` 落地时清单会被删掉，
这些测试应当继续通过（它们不关心声明从哪来，只关心故障可报告）。
"""

from __future__ import annotations

import logging

import pytest


def test_weather_factory_failure_does_not_raise_from_diagnostics(monkeypatch, caplog, tmp_path) -> None:
    """天气工厂抛异常时，诊断记录本身不许再抛 KeyError。"""
    import agent_tools
    import agent_tools.modules as module_factories

    def broken_weather_module(**kwargs):
        raise RuntimeError("weather factory detail")

    monkeypatch.setattr(module_factories, "build_weather_module", broken_weather_module, raising=False)
    caplog.set_level(logging.WARNING, logger="agent_tools")

    registry = __import__("agent_core.modules", fromlist=["ModuleRegistry"]).ModuleRegistry()

    # 这里以前是 `KeyError: 'weather'` —— 报告故障的代码自己炸了。
    agent_tools.register_modules(registry, provider=None)

    diagnostics = registry.diagnostics()
    assert "weather" in diagnostics, "天气不可用必须能在诊断里看见"
    assert diagnostics["weather"]["status"] == "error"
    assert diagnostics["weather"].get("reason"), "必须带上人能看懂的原因"


def test_disabled_weather_module_does_not_raise_from_diagnostics(monkeypatch) -> None:
    """`AGENT_DISABLED_MODULES=weather` 同样不许抛。"""
    import agent_tools

    monkeypatch.setenv("AGENT_DISABLED_MODULES", "weather")

    registry = __import__("agent_core.modules", fromlist=["ModuleRegistry"]).ModuleRegistry()
    agent_tools.register_modules(registry, provider=None)

    diagnostics = registry.diagnostics()
    assert "weather" in diagnostics
    assert diagnostics["weather"]["status"] == "error"


def test_weather_is_visible_in_the_registration_snapshot(monkeypatch) -> None:
    """禁用天气后，能力快照里必须有一行说明它为什么不在。

    这条锁的是"用户看到的是原因，而不是功能凭空消失"。
    """
    import agent_tools

    monkeypatch.setenv("AGENT_DISABLED_MODULES", "weather")

    registry = __import__("agent_core.modules", fromlist=["ModuleRegistry"]).ModuleRegistry()
    agent_tools.register_modules(registry, provider=None)

    snapshot = registry.capability_snapshot(__import__("agent_core.tools", fromlist=["ToolRegistry"]).ToolRegistry())
    ids = {module["id"] for module in snapshot["modules"]}
    assert "weather" in ids, f"天气不该从能力快照里凭空消失；当前可见模块：{sorted(ids)}"


@pytest.mark.parametrize("module_id", ["weather", "location", "navigation", "tasks", "conditions", "media", "search", "netease", "system", "work"])
def test_every_module_can_report_its_own_unavailability(module_id: str) -> None:
    """对**每一个**模块，"报告不可用"这条路径都必须走得通。

    **写法刻意不读任何清单**：逐个模块试一遍，
    漏了声明的模块会直接失败，而不是等人去比对两份列表。

    这条用例是 2026-09-21 那个 `KeyError: 'weather'` 的直接守门。
    当时它打的是 `agent_tools._record_unavailable`（那份实现查静态清单）；
    §2 之后"本该有哪些工具"由能力目录提供，所以这里改打目录那条路：
    `capability_runtime._record_unavailable(catalog, registry, entry, reason)`。

    要保证的东西没变：**模块从目录里取不到工具明细这种事不存在**。
    差别是现在取不到在结构上就不可能 —— 对这个 module_id 没有 entry 时
     `_record_unavailable` 压根不会被调到（below 的断言会先失败）。
    """
    from agent_core.capability_catalog import build_catalog
    from agent_core.capability_runtime import _record_unavailable
    from agent_core.modules import ModuleRegistry

    catalog = build_catalog()
    entry = catalog.entry(module_id)
    assert entry is not None, f"{module_id} 不在能力目录里 —— 它出问题时就无法报告缺了哪些工具"

    registry = ModuleRegistry()
    _record_unavailable(catalog, registry, entry, "module factory failed")
    assert registry.diagnostics()[module_id]["status"] == "error"
    # 明细必须来自目录，且要真的带出工具清单（空清单 = 诊断只能写一行空白）。
    unavailable = registry._unavailable_modules[module_id]
    assert tuple(unavailable["declared_tools"]) == entry.declared_tools
