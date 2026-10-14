"""即插即用的工具包：只依赖 agent_core 的公开契约，绝不抛异常、绝不污染主体。

【2026-09-21 起：装配搬去了 agent_core.capability_runtime】

本模块原来是"系统里有哪些模块"的**第二份真相**：一份 `discover_modules()`
写真实工厂，一份 `BUILTIN_MODULE_MANIFESTS` 静态清单写工具名。两份都要手改，
漏改一份就出洞 —— weather 的 `KeyError` 就是这么来的（清单里没它，
而报告"天气不可用"的代码正好去查这份清单）。

现在声明收敛到 `agent_core.capability_catalog`，装配收敛到
`agent_core.capability_runtime`。本模块只留两件事：

1. **向后兼容的薄转发**：`discover_modules` / `register_modules` 仍可调，
   但它们不再自己维护模块列表，而是转给目录。
   旧调用点（含若干测试）因此不必一次性改完。
2. **`register_modules` 的签名冻结**：它被 Web / CLI / 测试用固定签名调了很多次。
   签名不变是刻意的 —— 一次性改掉所有调用点会把"装配搬了家"和
   "调用点写错了"两种红混在一起。

【为什么没有 `set_permission_layer` 了】

那是个模块级全局槽：入口在注册前 set，work 模块在 discover 期间 get。
能跑，但"谁在什么时候设的"只能靠读调用顺序。现在权限层走
`BuildContext.permission_layer` 显式传参。

【为什么没有 `TOOL_BUILDERS` / `register_tools` 了】

它们只服务 web_search 一个工具，而那个工具本来就是一个模块
（`search` 模块，见目录）。留着会变成"第三种声明工具的方式"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent_core.capability_catalog import build_catalog
from agent_core.modules import ModuleRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]


def discover_modules(*, disabled_modules: set[str] | None = None) -> tuple[list[Any], dict[str, str]]:
    """构建所有内置模块。返回 (模块列表, 失败原因表)。

    **声明来自目录，不是这里。** 想加模块请改 `agent_core/capability_catalog.py`，
    不要在这里再写一份列表 —— 那正是被删掉的那份静态清单的老路。

    失败不抛：模块工厂挂掉是正常情况（key 没配、依赖没装），
    核心服务要照样 ready。
    """
    from agent_core.capability_catalog import BuildContext
    from agent_core.capability_runtime import build_build_context

    catalog = build_catalog()
    ctx = build_build_context(
        env={} if disabled_modules is None else {"AGENT_DISABLED_MODULES": ",".join(sorted(disabled_modules))},
    )
    discovered: list[Any] = []
    failures: dict[str, str] = {}
    for entry in catalog.entries():
        module, reason = catalog.resolve_module(entry.module_id, ctx)
        if module is None:
            failures[entry.module_id] = reason
        else:
            discovered.append(module)
    return discovered, failures


def register_modules(
    registry: ModuleRegistry,
    *,
    provider: object = None,
    registry_provider: Callable[[], object] | None = None,
    event_bus: object = None,
    task_db: str = "agent_tasks.db",
    condition_task_db: str = "agent_condition_tasks.db",
) -> list[str]:
    """把模块登记进 ModuleRegistry，返回成功启用的模块 id 列表。

    **签名与行为保持不变**（旧调用点很多，一次改完会把"装配搬家"与
    "调用点写错"两种红混在一起）。内部改为走目录 + 目录装配，
    所以 Web 与 CLI 从这里拿到的模块集合必然一致。

    注意：真正的统一装配入口是 `agent_core.capability_runtime.build_runtime`。
    本函数是给尚未迁移的调用点用的兼容层 —— 它只登记模块，**不装 TaskRouter**，
    所以新代码请直接用 `build_runtime`。
    """
    from agent_core.capability_catalog import BuildContext
    from agent_core.capability_runtime import build_build_context

    enabled: list[str] = []
    failures: dict[str, str] = {}
    registry.registration_failures = failures
    catalog = build_catalog()

    # tasks / conditions 要在运行时才知道"当前有哪些工具"（它们会把自己转派给
    # 其它模块的工具）。此时注册表还没建好，所以给的是一个**延迟到调用时才求值**
    # 的闭包，而不是现在的快照 —— 用快照的话会比字面也好不到哪去：
    # 闭包看到的是"启用列表已经长全之后"的注册表，快照看到的是空表。
    # 这条 wiring 必须保住：塌掉之后 `task_service.registry_provider()` 会
    # 返回一个空注册表，表现为"定时任务触发时转派什么都不做"，且不报错。
    if registry_provider is None:
        def _late_registry() -> Any:
            return registry.tools_for(set(enabled))

        resolved_registry_provider: Callable[[], Any] = _late_registry
    else:
        resolved_registry_provider = registry_provider
    ctx = build_build_context(
        provider=provider,
        event_bus=event_bus,
        permission_layer=None,
        env={
            "AGENT_TASK_DB": task_db,
            "AGENT_CONDITION_TASK_DB": condition_task_db,
        },
    )
    ctx.registry_provider = resolved_registry_provider

    for entry in catalog.entries():
        module, reason = catalog.resolve_module(entry.module_id, ctx)
        if module is None:
            failures[entry.module_id] = reason
            _record_unavailable(catalog, registry, entry, reason)
            continue
        try:
            registry.register(module)
            enabled.append(entry.module_id)
            registry.set_diagnostic(entry.module_id, status="registered", reason="")
        except Exception as exc:
            failures[entry.module_id] = f"{type(exc).__name__}: {exc}"
            _record_unavailable(catalog, registry, entry, "module registration failed")

    # 把这次装配产生的运行时句柄挂回注册表，供 /api 读取（沿用旧行为）。
    shared = ctx._shared
    for attr, key in (
        ("location_store", "location_store"),
        ("task_service", "task_service"),
        ("condition_task_service", "condition_task_service"),
    ):
        value = shared.get(key)
        if value is not None:
            setattr(registry, attr, value)
    media_spec = registry.resolve("media")
    if media_spec is not None:
        setattr(registry, "media_module", media_spec)
    work_spec = registry.resolve("work")
    if work_spec is not None:
        setattr(registry, "work_host", work_spec.runtime)
    return enabled


def _record_unavailable(catalog: Any, registry: ModuleRegistry, entry: Any, reason: str) -> None:
    """工厂挂了也要在诊断里占一行。工具明细来自目录（不再有第二份清单）。

    weather 那个 `KeyError` 的根因就在这里：旧实现查一份手工清单，
    清单里没有 weather 就炸。现在 `entry` 是按模块 id 从目录取的，
    取不到的情形根本不会传进来。
    """
    code = "module_disabled" if "disabled" in reason else "module_factory_failed"
    public_reason = (
        "module is disabled by configuration" if "disabled" in reason else "module factory failed"
    )
    registry.record_unavailable_module(
        entry.module_id,
        description=entry.description,
        declared_tools=entry.declared_tools,
        code=code,
        reason=public_reason,
        required_for_readiness=entry.required_for_readiness,
    )


__all__ = ["discover_modules", "register_modules"]
