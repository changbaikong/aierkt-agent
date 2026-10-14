"""运行时装配：Web、CLI、子代理共用的**唯一**构建函数。

【为什么必须只有一个】

Web 与 CLI 是同一个 Agent 的两个入口。用户视角里"换个入口能力应该一样"，
但代码里它们各自装配了一遍，于是实测出了分叉：

    Web 有 TaskRouter，CLI 没有
    Web 用默认任务库路径，CLI 读 AGENT_TASK_DB

分叉的后果不是"CLI 少个功能"那么轻：**同一个目标在 Web 上走确定性路由、
在 CLI 上交给模型**。同一个 bug 只在其中一个入口复现，排障时两边对不上。

所以这个模块的存在理由不是"少写重复代码"，而是"让分叉无处可生"：
两个入口调同一个函数，参数由同一个函数从环境变量解释一次。

【依赖怎么传：显式上下文，没有模块级全局槽】

原来的做法是 `agent_tools._permission_layer` 这个全局槽 —— 入口在注册模块
之前 `set_permission_layer(...)`，work 模块在 discover 期间把它 get 出来。
能跑，但"谁在什么时候设的"只能靠读调用顺序，而调用顺序会被下一个改动打乱。

现在改成 `BuildContext` 一路显式传下去。代价是签名长一点；
收益是"work 模块拿到的权限层是哪个"在调用点上就能看见。

【TaskRouter：内核组件，装不上就报错】

它与外部可选服务（高德 key、MCP）的失败处理**刻意不同**：

- 外部服务装不上 → 降级。没高德 key 就"天气不可用"，其余照常。
- TaskRouter 装不上 → **显式报运行时初始化失败**。它不是可选项：
  少了它，确定性路由整条不生效，系统会静默换一套行为。
  用户看到的是"同一个问句今天和昨天的处理方式不一样"，而这不会报任何错。

所以这里不写 `try: router = ... except Exception: router = None`。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .capability_catalog import BuildContext, build_catalog
from .modules import ModuleRegistry
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

#: 任务库路径的环境变量名。**只在这里出现一次** —— 两个入口各写一遍
#: 就会出现"改了一边忘了另一边"，表现为"Web 上建的定时任务在 CLI 里看不见"。
ENV_CORE_DB = "AGENT_CORE_DB"
ENV_TASK_DB = "AGENT_TASK_DB"
ENV_CONDITION_TASK_DB = "AGENT_CONDITION_TASK_DB"
ENV_MEDIA_TASK_DB = "MEDIA_TASK_DB"
ENV_WORK_ROOTS = "AGENT_WORK_ROOTS"
ENV_DISABLED_MODULES = "AGENT_DISABLED_MODULES"
ENV_CODE_AGENT_ENABLED = "CODE_AGENT_ENABLED"
ENV_CODE_RUNTIME_ROOT = "CODE_AGENT_RUNTIME_ROOT"
ENV_CODE_POWERSHELL = "CODE_AGENT_POWERSHELL"
ENV_CODE_PYTHON = "CODE_AGENT_PYTHON"
ENV_CODE_SEARCH = "CODE_AGENT_SEARCH"
ENV_CODE_SNAPSHOT_ROOT = "CODE_AGENT_SNAPSHOT_ROOT"
ENV_CODE_SANDBOX_STATE_ROOT = "CODE_AGENT_SANDBOX_STATE_ROOT"
ENV_CODE_ARTIFACT_DIR = "CODE_AGENT_ARTIFACT_DIR"
ENV_CODE_VERIFICATION_CONFIG = "CODE_AGENT_VERIFICATION_CONFIG"
ENV_CODE_AUTO_APPLY_LOW_RISK = "CODE_AGENT_AUTO_APPLY_LOW_RISK"
ENV_CODE_APPLY_STATE_ROOT = "CODE_AGENT_APPLY_STATE_ROOT"
ENV_CODE_WORKERS = "CODE_AGENT_WORKERS"
ENV_ROUTING_MODE = "AGENT_ROUTING_MODE"
ENV_LEGACY_SHADOW_ENABLED = "AGENT_LEGACY_SHADOW_ENABLED"


class RuntimeBootstrapError(RuntimeError):
    """装配失败。**不吞掉**，让入口显式报"运行时初始化失败"。

    单独一个异常类型而不是裸 RuntimeError：调用方要能区分
    "内核组件装配失败（必须报错）"与"某个业务模块挂了（记诊断即可）"。
    """


def _strict_env_bool(source: dict[str, str], name: str, *, default: bool) -> bool:
    if name not in source:
        return default
    value = str(source.get(name, "")).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeBootstrapError(
        f"{name} must be a boolean value, got {value!r}"
    )


@dataclass
class RuntimeBundle:
    """一次装配的产物。两个入口拿到的就是这些。"""

    catalog: Any
    context: BuildContext
    modules: ModuleRegistry
    registry: ToolRegistry
    #: 动作网关。**Chat 与后台能力共用这同一个实例** ——
    #: 分开建两个网关等于把"同一个动作编号不跑第二次"的保护劈成两半
    #: （两边的审计表各记一半，谁都看不全）。
    gateway: Any = None
    enabled_modules: list[str] = field(default_factory=list)
    registration_failures: dict[str, str] = field(default_factory=dict)
    task_router: Any = None
    location_store: Any = None
    task_service: Any = None
    condition_task_service: Any = None
    work_host: Any = None
    media_module: Any = None


def build_build_context(
    *,
    provider: Any = None,
    event_bus: Any = None,
    permission_layer: Any = None,
    registry_provider: Any = None,
    env: dict[str, str] | None = None,
) -> BuildContext:
    """从环境变量解释出装配上下文。**默认值只在这里写一次。**

    解释顺序固定：显式参数优先，其次环境变量，最后默认值。
    两个入口都调它，所以"CLI 和 Web 读到同一批变量"是结构性保证，
    而不是靠两边都记得写对。

    `env` 是**覆盖层**不是替换层（2026-09-21 修）：原先写成
    `source = os.environ if env is None else env`，于是调用方只要传一个
    任务库路径，就把整份环境（含 `AGENT_DISABLED_MODULES`）抹掉了 ——
    症状是"配了禁用某模块但完全不生效"，而且不报任何错。
    """
    source: dict[str, str] = dict(os.environ)
    if env:
        source.update(env)
    roots_raw = str(source.get(ENV_WORK_ROOTS, "") or "")
    roots = tuple(
        str(Path(item).expanduser().resolve())
        for item in roots_raw.split(";")
        if item.strip()
    )
    disabled = frozenset(
        item.strip() for item in str(source.get(ENV_DISABLED_MODULES, "")).split(",") if item.strip()
    )
    routing_mode = str(source.get(ENV_ROUTING_MODE, "legacy") or "legacy").strip().lower()
    if routing_mode not in {"legacy", "model"}:
        raise RuntimeBootstrapError(
            f"{ENV_ROUTING_MODE} must be 'legacy' or 'model', got {routing_mode!r}"
        )
    return BuildContext(
        provider=provider,
        registry_provider=registry_provider,
        event_bus=event_bus,
        permission_layer=permission_layer,
        work_roots=roots,
        core_db=str(source.get(ENV_CORE_DB, "agent_core.db")),
        task_db=str(source.get(ENV_TASK_DB, "agent_tasks.db")),
        condition_task_db=str(source.get(ENV_CONDITION_TASK_DB, "agent_condition_tasks.db")),
        media_task_db=str(source.get(ENV_MEDIA_TASK_DB, "./data/media_tasks.db")),
        code_agent_enabled=str(source.get(ENV_CODE_AGENT_ENABLED, "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        code_runtime_root=str(source.get(ENV_CODE_RUNTIME_ROOT, "")),
        code_powershell_executable=str(source.get(ENV_CODE_POWERSHELL, "")),
        code_python_executable=str(source.get(ENV_CODE_PYTHON, "")),
        code_search_executable=str(source.get(ENV_CODE_SEARCH, "")),
        code_snapshot_root=str(source.get(ENV_CODE_SNAPSHOT_ROOT, "./data/code_snapshots")),
        code_sandbox_state_root=str(source.get(ENV_CODE_SANDBOX_STATE_ROOT, "./data/code_sandbox_state")),
        code_artifact_dir=str(source.get(ENV_CODE_ARTIFACT_DIR, "./data/code_artifacts")),
        code_verification_config=str(source.get(ENV_CODE_VERIFICATION_CONFIG, "")),
        code_agent_auto_apply_low_risk=str(
            source.get(ENV_CODE_AUTO_APPLY_LOW_RISK, "false")
        ).strip().lower()
        in {"1", "true", "yes", "on"},
        code_apply_state_root=str(
            source.get(ENV_CODE_APPLY_STATE_ROOT, "./data/code_apply_state")
        ),
        code_workers=max(1, min(4, int(source.get(ENV_CODE_WORKERS, "1") or 1))),
        harness_enabled=str(source.get("AGENT_HARNESS_ENABLED", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        routing_mode=routing_mode,
        legacy_shadow_enabled=_strict_env_bool(
            source, ENV_LEGACY_SHADOW_ENABLED, default=True
        ),
        disabled_modules=disabled,
    )


def build_runtime(
    *,
    provider: Any = None,
    event_bus: Any = None,
    permission_layer: Any = None,
    env: dict[str, str] | None = None,
    catalog: Any = None,
    install_task_router: bool = True,
) -> RuntimeBundle:
    """构建整个运行时能力面。**Web 与 CLI 都调这一个。**

    步骤（顺序有讲究）：
    1. 建目录（纯声明，不碰环境）；
    2. 解释上下文（默认值只此一处）；
    3. 逐模块构建 → 失败的记诊断，不抛；
    4. 组装工具注册表；
    5. 安装 TaskRouter（**失败就抛**，见模块头注释）。

    Args:
        install_task_router: 只有测试会传 False（要单独验证"没有 router 时
            注册表长什么样"）。生产入口永远走默认的 True。

    Raises:
        RuntimeBootstrapError: 内核组件（TaskRouter）装配失败。
    """
    active_catalog = catalog or build_catalog()
    context = build_build_context(
        provider=provider,
        event_bus=event_bus,
        permission_layer=permission_layer,
        env=env,
    )
    modules = ModuleRegistry()
    enabled: list[str] = []
    failures: dict[str, str] = {}

    # registry_provider 要在工具注册表建好之后才有意义，所以给它一个转发闭包；
    # gateway 也一样 —— 它需要 registry 去查 spec，但 conditions 模块在
    # **构建期**就要拿到 gateway。**两者互相依赖**（注册表自身却又来自模块），
    # 所以各自拿一个"延迟到调用时才求值"的代理，把它们两个解耦开。
    #
    # 顺序陷阱：网关若在这轮 for 循环之后才建，conditions 会拿到 gateway=None，
    # 构造不报错、`tools_for` 也正常，直到第一条定时任务真的触发才炸 ——
    # 而且症状是"这条提醒从此再也不响"（异常被 `_check_guarded` 吞掉），
    # 本地几乎不可能复现。守门见
    # tests/test_execution_paths.py::test_production_assembly_wires_the_gateway_into_condition_tasks
    holder: dict[str, Any] = {}

    class _RegistryProxy:
        """把"查工具"转发给当时已经建好的那个注册表。

        只转发 `resolve` 与迭代（这两个是网关与 `tools_for` 真正用到的）；
        其余属性报错而不是静默返回 None —— 静默 None 会把"代理不支持这个操作"
        变成"工具不存在"，两者在现象上完全一样。
        """

        def resolve(self, name: str) -> Any:
            target = holder.get("registry")
            return target.resolve(name) if target is not None else None

        def __iter__(self) -> Any:
            target = holder.get("registry")
            return iter(target) if target is not None else iter(())

    proxy = _RegistryProxy()
    context.registry_provider = lambda: holder.get("registry") or ToolRegistry()
    context.gateway = _build_gateway(proxy, permission_layer, context)

    for entry in active_catalog.entries():
        module, reason = active_catalog.resolve_module(entry.module_id, context)
        if module is None:
            failures[entry.module_id] = reason
            _record_unavailable(active_catalog, modules, entry, reason)
            continue
        try:
            modules.register(module)
            modules.set_diagnostic(entry.module_id, status="registered", reason="")
            enabled.append(entry.module_id)
        except Exception as exc:
            failures[entry.module_id] = f"{type(exc).__name__}: {exc}"
            logger.exception("capability module registration failed (%s)", entry.module_id)
            _record_unavailable(active_catalog, modules, entry, "module registration failed")

    modules.registration_failures = failures
    # 到这里 modules 已经全部登记完，工具注册表一次成型。
    # proxy 从此之后转发的就是这个实例。
    registry = modules.tools_for(set(enabled))
    holder["registry"] = registry

    gateway = context.gateway
    holder["gateway"] = gateway

    task_router = None
    needs_legacy_router = context.routing_mode == "legacy" or context.legacy_shadow_enabled
    if install_task_router and needs_legacy_router:
        task_router = _install_task_router(registry, context)

    shared = context._shared
    return RuntimeBundle(
        catalog=active_catalog,
        context=context,
        modules=modules,
        registry=registry,
        gateway=gateway,
        enabled_modules=enabled,
        registration_failures=failures,
        task_router=task_router,
        location_store=shared.get("location_store"),
        task_service=shared.get("task_service"),
        condition_task_service=shared.get("condition_task_service"),
        work_host=_resolve_work_host(modules),
        media_module=modules.resolve("media"),
    )


def _build_gateway(registry: ToolRegistry, permission_layer: Any, ctx: BuildContext) -> Any:
    """建动作网关。

    三个注入物的取舍，每条都有具体理由：

    1. **risk_policy 来自 permission_layer**。账号在完全访问模式下不该被
       高风险确认拦住；把 policy 换成默认策略会让 Web 的设置失效 ——
       那种症状是"我明明关了确认它还在问"，而且只在 Web 上重现。
       permission_layer 缺失时（测试/离线）退回默认策略，这是唯一允许的退化。

    2. **journal 必须接**。它承担两件网关赖以应对现实的事：
       重复编号不重跑（防"重复点确认=双跑"），以及重启时把遗留的 `running`
       对账成 `outcome_unknown`。不接账本的话这两条保证都不存在，
       而它们恰恰是最难在事后发现的失效。

    3. **approval_sink 暂为 None** —— 审批入口搬迁在 §4。现在是"高风险动作
       只能等待"，用户在聊天里还没法点头。这是**已知缺口，不是遗忘**；
       `_wait_for_approval` 在没有 sink 时会明确拒绝而不是放行。
    """
    from .action_journal import ActionJournal
    from .actions import ActionGateway
    from .tools import DefaultRiskPolicy

    policy = getattr(permission_layer, "policy", None) or DefaultRiskPolicy()
    # 审计库跟别的库放同一处（agent_core.db 的邻居），便于备份与排查时一起看。
    journal = ActionJournal(_core_db_parent(ctx) / "action_journal.db")
    return ActionGateway(
        registry=registry, risk_policy=policy, journal=journal, approval_sink=None,
    )


def _core_db_parent(ctx: BuildContext) -> Path:
    """主库所在目录。计算任务库之外的地方时用这个做落点。"""
    return Path(ctx.core_db).parent if ctx.core_db else Path(".")


def _install_task_router(registry: ToolRegistry, context: BuildContext) -> Any:
    """安装确定性任务路由器。**失败就抛，不降级。**

    它不是外部可选服务：少了它，"多步 + 依赖 → 建议计划编排"这类确定性分岔
    整条不生效，系统会静默换一套行为（同一个问句两个入口处理不同）。
    所以这里刻意**不**捕获异常。

    `allowed_roots` 必须与 Worker 看到的 roots **同源**（都来自
    `BuildContext.work_roots`）：路由说"这个目录合法"而 Worker 拒绝执行，
    是自相矛盾的组合，比直接拒绝更难查。
    """
    from .routing import TaskRouter

    try:
        return TaskRouter(
            registry=registry,
            allowed_roots=list(context.work_roots) or None,
        )
    except Exception as exc:
        raise RuntimeBootstrapError(
            f"TaskRouter could not be installed: {type(exc).__name__}: {exc}"
        ) from exc


def _resolve_work_host(modules: ModuleRegistry) -> Any:
    """取 work 模块自建的 Host。

    **不再自建第二个**：两个 host 各自 `start()` 时都会跑 `recover_runtime()`，
    互相把对方 running 的 attempt 判成 interrupted（`claim_attempt` 是原子的，
    但 recover 不是）。所以 Host 只有一个来源 —— work 模块。
    """
    spec = modules.resolve("work")
    return spec.runtime if spec is not None else None


def _record_unavailable(
    catalog: Any, modules: ModuleRegistry, entry: Any, reason: str
) -> None:
    """工厂挂了也要在诊断里占一行，且**工具明细来自目录**（不再有第二份清单）。

    这是 weather 那个 `KeyError` 的根因所在：旧实现去查一份手工维护的静态清单，
    清单里没有 weather 就炸。现在工具清单来自目录里**唯一**那份声明，
    而目录条目是按模块 id 取的 —— 取不到只可能是"没声明这个模块"，
    那种情况下 `entry` 本身就不存在，函数不会被调到。
    """
    code = "module_disabled" if "disabled" in reason else "module_factory_failed"
    public_reason = (
        "module is disabled by configuration" if "disabled" in reason else "module factory failed"
    )
    modules.record_unavailable_module(
        entry.module_id,
        description=entry.description,
        declared_tools=entry.declared_tools,
        code=code,
        reason=public_reason,
        required_for_readiness=entry.required_for_readiness,
    )


def resolve_work_roots(env: dict[str, str] | None = None) -> list[Path]:
    """Work 允许触达的目录。**与 TaskRouter / Worker 同源。**

    越权目录在提交前就要降级：`TaskRouter.allowed_roots` 与 Worker 必须看
    同一份 roots，否则会出现"路由说合法、Worker 拒绝"这种自相矛盾。
    """
    source = os.environ if env is None else env
    raw = str(source.get(ENV_WORK_ROOTS, "") or "")
    if not raw.strip():
        return [Path.cwd()]
    return [Path(item).expanduser().resolve() for item in raw.split(";") if item.strip()]


__all__ = [
    "ENV_CONDITION_TASK_DB",
    "ENV_CORE_DB",
    "ENV_DISABLED_MODULES",
    "ENV_CODE_VERIFICATION_CONFIG",
    "ENV_CODE_AUTO_APPLY_LOW_RISK",
    "ENV_CODE_APPLY_STATE_ROOT",
    "ENV_MEDIA_TASK_DB",
    "ENV_TASK_DB",
    "ENV_WORK_ROOTS",
    "RuntimeBootstrapError",
    "RuntimeBundle",
    "build_build_context",
    "build_runtime",
    "resolve_work_roots",
]
