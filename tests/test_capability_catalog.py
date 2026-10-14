"""能力目录：模块与工具的**唯一**声明来源。

【为什么这个文件必须存在】

在这个文件之前，"系统里有哪些模块 / 每个模块有哪些工具"这件事有**两份**真相：

    真实工厂列表           agent_tools/__init__.py 的 discover_modules()/register_modules()
    静态声明清单           agent_tools/__init__.py 的 BUILTIN_MODULE_MANIFESTS

两份都要手改，改漏一份就出洞。2026-09-21 那个 weather 的 `KeyError`
就是这么来的：静态清单里漏了 weather，而报告诊断的代码正好去查那份清单 ——
**报错的代码自己炸了**。当时的止血是给清单补一行 + 用 `.get` 兜底，
根因（两份真相）没动。

这个文件守的就是"只有一份"。具体守三条：

1. 目录里声明的模块集合 == 真实能构建出来的模块集合（双向相等，不是"包含"）；
2. 目录里声明的工具名 == 每个模块真实产出的工具名（双向相等）；
3. 静态清单那份**第二真相已被删除**，且不许再加回来。

第 3 条是关键：前两条用"目录 vs 真实工厂"对账，如果两份清单都还在，
很容易把目录改成"跟着工厂抄一遍"，洞只是从清单挪到对账代码里。

【与 tests/test_capabilities.py 的分工】

那个文件守的是**注册之后**的状态投影（declared → registration → advertised 三态、
诊断码、探测降级）。这个文件守的是**注册之前**的声明本身。
两者红的原因完全不同，所以分开：一个红说明"状态表算错了"，
一个红说明"声明与实现脱节了"。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_TOOLS_INIT = REPO_ROOT / "agent_tools" / "__init__.py"


# ── 第三条：静态清单不许复活 ────────────────────────────────────────


def test_the_second_source_of_truth_is_gone() -> None:
    """`BUILTIN_MODULE_MANIFESTS` 必须已经从 `agent_tools` 里消失。

    它是"模块集合的第二份真相"。留着它，任何新模块都要记得改两处，
    而漏改的症状（诊断里少一行、`KeyError`）都发生在**报告故障的路径**上 ——
    最不容易被发现的地方。
    """
    import agent_tools

    assert not hasattr(agent_tools, "BUILTIN_MODULE_MANIFESTS"), (
        "静态清单又回来了。模块与工具的声明只能有 CapabilityCatalog 一份，"
        "否则新模块要改两处、漏改的症状出现在诊断路径上（见 weather 的 KeyError）。"
    )


def test_no_static_manifest_remains_in_the_source() -> None:
    """源码层面也确认：不许换个名字再写一份静态清单。

    动态 `hasattr` 只能抓当前作用域的名字；把清单塞进另一个函数或另一个模块
    就能绕开。所以再扫一遍源码文本，抓"模块 id → 工具名元组"这种字面量形状。
    """
    source = AGENT_TOOLS_INIT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            name = getattr(target, "id", "")
            if "MANIFEST" in name.upper():
                offenders.append(f"{AGENT_TOOLS_INIT.name}:{node.lineno} 定义了 {name}")
    assert offenders == [], (
        "又出现了静态清单式声明：\n" + "\n".join(offenders)
        + "\n模块与工具的声明请放进 CapabilityCatalog。"
    )


# ── 第一条：目录声明的模块集合 == 真实构建出来的模块集合 ─────────────


def test_catalog_and_reality_agree_on_the_module_set() -> None:
    """目录里写了哪些模块，就真能构建出哪些模块；反之亦然。**双向相等。**

    单向（"目录里的都能建出来"）不够：那意味着**新增一个模块却忘了写进目录**
    也能通过 —— 而那正是要把模块从模型视野里藏起来的反面（模块存在但不可见）。
    """
    from agent_core.capability_catalog import build_catalog
    from agent_tools import discover_modules

    declared = set(build_catalog().module_ids())
    discovered, failures = discover_modules()
    actual = {module.module_id for module in discovered} | set(failures)
    assert declared == actual, (
        f"目录与真实模块集合不一致\n"
        f"  只在目录里：{sorted(declared - actual)}\n"
        f"  只在实现里：{sorted(actual - declared)}"
    )


def test_every_module_in_the_catalog_declares_its_tools() -> None:
    """目录里每个模块都必须声明工具清单（哪怕是空元组也要显式写）。

    允许空元组：有些模块确实只在生命周期里有意义（没有对模型暴露的工具）。
    但不允许"没声明"—— 那样这个模块的工具在能力快照里既不算 declared 也不算 missing，
    前端只会看到一行空白。
    """
    from agent_core.capability_catalog import build_catalog

    catalog = build_catalog()
    for entry in catalog.entries():
        assert entry.declared_tools is not None, (
            f"模块 {entry.module_id} 没声明 declared_tools。"
            "工厂挂掉时就没有『本该有哪些工具』可报，诊断只能写一行空白。"
        )


# ── 第二条：目录声明的工具名 == 真实产出的工具名 ─────────────────────


def test_catalog_and_reality_agree_on_tool_names() -> None:
    """目录声明的工具名，必须与模块真实 `build_tools()` 产出的一致。

    这是 weather 那个 bug 的直接守门：清单里少一个模块名 → 这里就红。
    """
    from agent_core.capability_catalog import build_catalog
    from agent_tools import discover_modules

    catalog = build_catalog()
    discovered, _failures = discover_modules()
    mismatches: list[str] = []
    for module in discovered:
        entry = catalog.entry(module.module_id)
        if entry is None:
            continue  # 由上一条用例负责报"目录里没有它"
        declared = set(entry.declared_tools)
        built = {str(getattr(tool, "name", "")).rsplit(".", 1)[-1] for tool in module.build_tools()}
        if declared != built:
            mismatches.append(
                f"  {module.module_id}: 只在目录里 {sorted(declared - built)}；"
                f"只在实现里 {sorted(built - declared)}"
            )
    assert mismatches == [], "目录声明的工具与真实产出不一致：\n" + "\n".join(mismatches)


@pytest.mark.parametrize("module_id", ["work", "netease", "media", "weather", "tasks", "conditions"])
def test_high_value_modules_are_present_in_the_catalog(module_id: str) -> None:
    """逐模块点名：这几个是"漏了会被用户立刻发现"的模块，不许从目录里消失。

    参数化而不是写一个集合断言，是为了失败信息直接指出是哪个模块没了。
    """
    from agent_core.capability_catalog import build_catalog

    assert build_catalog().entry(module_id) is not None, f"{module_id} 不在能力目录里"
