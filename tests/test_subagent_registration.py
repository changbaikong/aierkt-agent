"""子代理派发工具只能有一个注册来源。

【现状（2026-09-22 之前）】

`web_api.py:610` 与 `chat_cli.py:470` 各自调一次 `register_subagent_tool(...)`
往自己的注册表里塞一遍。这跟 work 模块 2026-09-14 之前的问题**一模一样**：

- Web 那条包在 `try/except` 里，注册失败只记一条 warning —— 于是"Web 上没有
  子代理"是一种**静默降级**，能力快照上也看不出来；
- CLI 那条不包 try，失败的姿势又不一样；
- 两处传的参数还不同（Web 传 `run_store`/`confirmation_store` 显式复用同一批存储，
  CLI 不传 → 走默认 `agent_core.db`）。`AGENT_CORE_DB` 一改，两个入口连的库就岔了。

修法和 work 那条一样：**唯一来源是能力目录**，两个入口都不许再自己注册。

【这个文件守三条】

1. 生产装配（`build_runtime`）产出的注册表里必须有 `subagent__dispatch` ——
   防的是"迁移完就没人注册了"；
2. 两个入口的源码里不许再出现 `register_subagent_tool` ——
   防的是"目录注册了、入口又注册一遍"（重名会直接把启动炸掉，
   但在只有一端跑的场景里不炸，会静默变成两条分叉）；
3. 目录声明的工具名与模块真实产出对不上就报错 —— 跟 work 那条同一把尺子。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _called_name(node: Any) -> str:
    """`ast.Call.func` 的名字：`foo(...)` 与 `a.b.foo(...)` 都取最右边那段。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def test_production_assembly_registers_the_dispatch_tool() -> None:
    """装配完之后，注册表里必须真的有子代理派发工具。"""
    from agent_core.capability_runtime import build_runtime

    runtime = build_runtime()

    assert runtime.registry.resolve("subagent__dispatch") is not None, (
        "装配完没有 subagent__dispatch —— 迁移到目录之后没人注册它了"
    )


def test_no_entry_point_registers_the_dispatch_tool_itself() -> None:
    """两个入口都不许再自己注册一遍（重名会炸，或者更糟：静默分叉）。

    按 **AST 调用点**判，不做文本包含 —— 文本包含会把"注释里提到这个函数名"
    也判成违规（本文件自己的说明里就提到了），一条会误报的检查很快就会被
    加 skip 绕过去，等于没有。
    """
    offenders: list[str] = []
    for name in ("web_api.py", "chat_cli.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node.func) == "register_subagent_tool":
                offenders.append(f"{name}:{node.lineno}")
    assert offenders == [], f"这些入口还在自己注册子代理，唯一来源应是能力目录：{offenders}"


def test_declared_tool_names_match_what_the_module_produces() -> None:
    """目录里声明的工具名，必须和模块 build_tools() 真产出的名字一致。"""
    from agent_core.capability_catalog import build_catalog
    from agent_core.subagent_module import build_subagent_module

    catalog = build_catalog()
    entry = catalog.entry("subagent")
    assert entry is not None, "能力目录里没有 subagent 这个模块"

    module = build_subagent_module()
    produced = tuple(spec.name for spec in module.build_tools())

    assert entry.declared_tools == produced, (
        f"目录声明 {entry.declared_tools} 与真实产出 {produced} 不一致"
    )
