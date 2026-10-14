"""路由层必须用**真实运行时**构建的工具集来验证。

【为什么要单独一个文件】

2026-09-21 查明：`test_routing.py` 里那个 `full_registry()` 把
`PLAN_SUBMIT_TOOL` 这个常量**自己**喂回注册表，于是断言测的是
"常量与它自己一致"，而不是"常量与生产注册表一致"。
`routing.py:61` 的 `PLAN_SUBMIT_TOOL = "plan_submit"` 缺了 `work__` 前缀，
在这套测试里完全看不出来 —— 真实注册表经过命名空间包装后只有
`work__plan_submit`，`_has_tool` 永远返回 False，
「多步 + 依赖 → 建议计划编排」那条软路由分支从来没进过。

所以这个文件只做一件事：**用生产链构建注册表，再问路由层认不认得出工具。**
手工造 `ToolSpec` 一律不许出现在这里。
"""

from __future__ import annotations

import pytest

from agent_core.modules import ModuleRegistry
from agent_core.routing import (
    PLAN_SUBMIT_TOOL,
    SUBAGENT_TOOL,
    WORK_SUBMIT_TOOL,
    RoutePath,
    RouteStrength,
    TaskRouter,
)


def _real_work_registry(tmp_path):
    """用生产链构建"只有 work 模块"的注册表。

    刻意不手工拼 ToolSpec：那样会把常量错配掩盖掉，正是要根治的病。
    """
    modules = ModuleRegistry()
    modules.register(_real_work_module(tmp_path))
    return modules.tools_for({"work"})


def _real_work_module(tmp_path):
    """构建真实的 work 模块（不启动 WorkHost，只取工具集）。"""
    from agent_core.work_module import build_work_module

    spec, _host = build_work_module(
        db_path=tmp_path / "work.db",
        allowed_roots=[tmp_path],
        permission_layer=_null_layer(),
        core_db_path=tmp_path / "core.db",
    )
    return spec


def _null_layer():
    from agent_core.permission_mode import PermissionLayer

    return PermissionLayer(db_path=":memory:", web_host="127.0.0.1")


# ──────────────────────── 真实工具名 ────────────────────────


def test_plan_submit_constant_matches_the_real_namespaced_tool(tmp_path) -> None:
    """路由常量必须等于真实注册表里的名字。

    这是本次止血的核心断言：`PLAN_SUBMIT_TOOL` 缺 `work__` 前缀时，
    真实注册表里根本 resolve 不到它，软路由分支静默失效。
    """
    registry = _real_work_registry(tmp_path)
    assert registry.resolve(PLAN_SUBMIT_TOOL) is not None, (
        f"路由常量 PLAN_SUBMIT_TOOL={PLAN_SUBMIT_TOOL!r} 在真实注册表里不存在；"
        f"真实可用的 work 工具名是 {sorted(spec.name for spec in registry)}"
    )


def test_work_submit_constant_matches_the_real_namespaced_tool(tmp_path) -> None:
    registry = _real_work_registry(tmp_path)
    assert registry.resolve(WORK_SUBMIT_TOOL) is not None


@pytest.mark.parametrize("name", [WORK_SUBMIT_TOOL, PLAN_SUBMIT_TOOL, SUBAGENT_TOOL])
def test_every_routing_constant_uses_the_module_namespace(name: str) -> None:
    """路由常量一律是 `模块__工具` 形状。

    subagent__dispatch 由 subagent.py 自己注册，不在 work 模块里，
    但它同样属于命名空间形态 —— 这条断言不依赖任何注册表，纯查形状。
    """
    assert "__" in name, f"{name!r} 不在 `模块__工具` 命名空间里"


# ──────────────────────── 软路由真的进得去 ────────────────────────


async def test_multi_step_request_reaches_the_plan_branch(tmp_path) -> None:
    """多步 + 有先后标记 → 必须给出 plan 引导，而不是静默降级。

    常量错配时这条会失败：`has_plan_submit` 恒为 False，
    请求掉到单步分支，hint 里没有 plan_submit。
    """
    registry = _real_work_registry(tmp_path)
    router = TaskRouter(registry=registry)
    decision = await router.decide("先整理 D:\\Downloads，然后截个图给我")

    assert decision.path is RoutePath.WORK_HOST
    assert decision.strength is RouteStrength.SOFT
    assert PLAN_SUBMIT_TOOL.rsplit("__", 1)[-1] in decision.hint


async def test_router_sees_the_plan_tool_in_the_real_registry(tmp_path) -> None:
    """直接问路由层：它认不认得计划工具。"""
    registry = _real_work_registry(tmp_path)
    router = TaskRouter(registry=registry)
    assert router._has_tool(PLAN_SUBMIT_TOOL) is True
