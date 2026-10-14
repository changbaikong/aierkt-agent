"""模块注册与"命名空间化的工具"组装。

【在系统里的位置】
一个"模块"（ModuleSpec）就是一个自包含的业务能力，比如网易云音乐、媒体任务、Work 计划编排。
这个文件负责把它们登记进 ModuleRegistry，并在注册时把模块内部的工具名
加上 `模块__` 前缀（`search` → `search__web`），避免不同模块撞名。

【四条设计约束，读代码时按这个去理解每个分支】

1. **可插拔，坏了不能拖垮主体**。
   模块由工厂函数构建，工厂可能抛异常（依赖没装、key 没配）。
   所以失败路径不是"启动报错"，而是**记录成诊断信息 + 记下这个模块本该有哪些工具**，
   让 /api/health 能报告"这个能力不可用，原因是 X"，而核心服务照样 ready。
   这就是 `_unavailable_modules` / `declared_tools` / `record_unavailable_module` 存在的理由。

2. **重名必须炸，不能静默覆盖**。
   `register()` 对 module_id、`ToolRegistry.register` 对工具名都是"已存在就 raise"。
   静默覆盖会让一个模块悄悄顶掉另一个模块的工具，而且从日志里看不出来。

3. **能力快照要区分三种状态**，这是前端能力面板的语义基础：
   - declared（声明了）→ registration（注册成功没）→ advertised_to_model（有没有真的给模型看见）
   只有三者都通才算 healthy。中间任何一环断了都算 degraded，并带上 code 供排查。
   "注册成功但没给模型看见"是一个真实存在过的故障模式（工具在 registry 里但不在 schema 列表里），
   所以它被单独标成 `tool_not_advertised`。

4. **探测不能执行真实业务动作**（见 capabilities.CapabilityProbe）。
   探测有 2.5 秒硬超时，且失败只降级不阻断 —— 因为探测本身是为了"报告健康"，
   它自己挂了不该把整个服务判死。
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Awaitable, Callable, Literal

from .capabilities import CapabilityProbe
from .contracts import ToolSpec
from .tools import ToolRegistry


ToolBuilder = Callable[[], list[ToolSpec]]
LifecycleHook = Callable[[], Awaitable[None]]
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
CAPABILITY_PROBE_TIMEOUT_SECONDS = 2.5
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleSpec:
    """A self-contained business capability available to the Agent.

    字段分三类，理解它们能看懂后面所有分支：
    - **构建类**：module_id / build_tools / declared_tools —— 这个模块有什么工具；
    - **策略类**：memory_policy / max_concurrency / default_visible —— 它的记忆可见性与并发上限；
    - **运维类**：start / stop / runtime / probe / required_for_readiness / external_tool_names
      —— 生命周期钩子与健康检查，不参与"给模型看什么"。
    """

    module_id: str
    build_tools: ToolBuilder
    # memory_policy 决定这个模块写进记忆的可视范围：
    #   shared_facts  = 用户偏好、身份、长期事实，跨模块都能读到
    #   private_state = 模块自己的临时状态（播放队列、TTS voice），只有本模块能读
    memory_policy: Literal["shared_facts", "private_state"] = "private_state"
    max_concurrency: int = 1
    description: str = ""
    default_visible: bool = True
    # 生命周期钩子：注册时只登记，start_all() 时才真正调用（且支持 async）。
    start: LifecycleHook | None = None
    stop: LifecycleHook | None = None
    # Optional runtime dependency for health probes. It is intentionally not
    # part of the tool schema or prompt surface.
    # （供健康探测使用的运行时依赖句柄；有意不进 schema、不进提示词面。）
    runtime: Any = None
    probe: CapabilityProbe | None = None
    # Expected local tool names make startup failures observable even when a
    # builder cannot return a ToolSpec instance.
    # （"本该有哪些工具"的清单：工厂挂掉时，靠它才能报出"缺了哪几个工具"。）
    declared_tools: tuple[str, ...] = ()
    # Optional capabilities do not prevent the core service from becoming
    # ready. Integrations can opt in when they are deployment requirements.
    # （默认 False：可选能力没起来不影响 /api/ready；只有把它当部署硬要求时才设 True。）
    required_for_readiness: bool = False
    # Maps local tool names to externally catalogued capability names. The
    # generic registry uses it to project catalog and RPC probe results.
    # （本地工具名 → 外部目录里的能力名，用于把外部 MCP 的能力清单对回本地工具。）
    external_tool_names: dict[str, str] = field(default_factory=dict)


class ModuleRegistry:
    """Keeps module ownership explicit and prevents tool-name collisions.

    它同时是**注册表**（有哪些模块）和**诊断账本**（每个模块/工具当前是什么状态）。
    四个字典各管一段，读代码时别混：
    - _modules / _module_order  —— 注册成功的模块（order 决定 start/stop 的先后）；
    - _diagnostics              —— 模块级状态（registered / error / tool_build_failed …）；
    - _tool_diagnostics         —— 工具级状态（registered / unregistered / build_failed / invalid）；
    - _unavailable_modules      —— **工厂都没跑起来**的模块（连 ModuleSpec 都没有），
                                   但仍要能报告"它本该提供哪些工具"。
    """

    def __init__(self) -> None:
        self._modules: dict[str, ModuleSpec] = {}
        self._module_order: list[str] = []
        self._started = False
        self._diagnostics: dict[str, dict] = {}
        self._tool_diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
        self._tool_registration_errors: dict[str, list[dict[str, str]]] = {}
        self._unavailable_modules: dict[str, dict[str, Any]] = {}

    def register(self, spec: ModuleSpec) -> None:
        """登记一个模块。**只登记，不构建工具、不启动**（那两件事分别在 tools_for / start_all）。

        module_id 必须是 ASCII 标识符：它会被拼进工具名（`模块__工具`），
        而 OpenAI 兼容接口对函数名有字符集限制，中文模块名会在调用那一刻才炸。
        """
        if not _IDENTIFIER.fullmatch(spec.module_id):
            raise ValueError("module_id must be an ASCII identifier (letters, digits, underscores)")
        if spec.module_id in self._modules:
            raise ValueError(f"module already registered: {spec.module_id}")
        self._modules[spec.module_id] = spec
        self._module_order.append(spec.module_id)
        self._diagnostics.setdefault(spec.module_id, {"status": "registered", "reason": ""})
        self._tool_diagnostics.setdefault(spec.module_id, {})
        self._tool_registration_errors.setdefault(spec.module_id, [])

    def set_diagnostic(self, module_id: str, *, status: str, reason: str = "", **extra) -> None:
        """覆盖写模块级诊断。**成功路径也会调它**（status="registered"），
        因为一个模块可能先失败、后被重新注册成功，诊断要能被后面的成功覆盖掉。"""
        item = {"status": status, "reason": reason}
        item.update(extra)
        self._diagnostics[module_id] = item

    def diagnostics(self) -> dict[str, dict]:
        """返回诊断的**副本**，防止调用方（尤其前端序列化路径）改到内部状态。"""
        return {key: dict(value) for key, value in self._diagnostics.items()}

    def record_unavailable_module(
        self,
        module_id: str,
        *,
        description: str,
        declared_tools: tuple[str, ...],
        code: str,
        reason: str,
        required_for_readiness: bool = False,
    ) -> None:
        """Keep a failed module factory visible to diagnostics consumers.

        用在"工厂函数抛异常，连 ModuleSpec 都造不出来"的场景（见 agent_tools/__init__.py
        的 _record_unavailable）。此时模块不在 _modules 里，但仍然要能在诊断里占一行，
        否则前端能力面板会**完全看不到**这个能力，用户只会觉得"莫名其妙少了功能"。
        所以这里手动记下 description + declared_tools，专门供快照拼出"缺了哪些工具"。
        """
        if not _IDENTIFIER.fullmatch(module_id):
            raise ValueError("module_id must be an ASCII identifier (letters, digits, underscores)")
        if module_id in self._modules:
            raise ValueError(f"module already registered: {module_id}")
        self._unavailable_modules[module_id] = {
            "id": module_id,
            "description": description,
            "declared_tools": tuple(declared_tools),
            "code": code,
            "reason": reason,
            "required_for_readiness": required_for_readiness,
        }
        self.set_diagnostic(module_id, status="error", reason=reason, code=code)

    def capability_snapshot(self, registry: ToolRegistry) -> dict[str, list[dict[str, Any]]]:
        """Return the declared, registered, and model-advertised tool state.

        这是 /api/health 与前端能力面板的数据源，**不执行任何业务动作**，纯读状态。

        三态推导规则（缺一个就可能出现"看起来正常其实不可用"）：
        - declared            : 该工具名在 spec.declared_tools 里（模块自己声明的"我本该有它"）
        - registration        : "registered" 才算真的进了 registry
        - advertised_to_model : 名字出现在 registry 里（即模型真能看见）

        两个刻意的补丁：
        1. 声明了但没注册（`tool_unregistered`）：工厂悄悄少产出一个工具时能看出来；
        2. 注册了但没给模型看见（`tool_not_advertised`）：registry 与 schema 列表脱节时能看出来。
        这两个都是真实发生过的故障形态，所以单独立码。
        """
        modules: list[dict[str, Any]] = []
        advertised = {tool.name for tool in registry}
        for spec in self.specs():
            declared_names = {f"{spec.module_id}__{name}" for name in spec.declared_tools}
            states = self._tool_diagnostics.get(spec.module_id, {})
            registration_errors = self._tool_registration_errors.get(spec.module_id, [])
            tools = [dict(states[name]) for name in sorted(states)]
            for local_name in spec.declared_tools:
                name = f"{spec.module_id}__{local_name}"
                if name in states:
                    continue
                tools.append({
                    "name": name,
                    "registration": "unregistered",
                    "advertised_to_model": False,
                    "code": "tool_unregistered",
                    "reason": "tool was declared but not registered",
                })
            tools.sort(key=lambda item: str(item["name"]))
            for tool in tools:
                tool["declared"] = tool["name"] in declared_names
                tool["advertised_to_model"] = tool["name"] in advertised
                if tool["registration"] == "registered" and not tool["advertised_to_model"]:
                    tool["code"] = "tool_not_advertised"
                    tool["reason"] = "registered tool is not advertised to the model"
            status = "healthy" if not registration_errors and all(
                tool["registration"] == "registered" and tool["advertised_to_model"]
                for tool in tools
            ) else "degraded"
            module = {
                "id": spec.module_id,
                "description": spec.description,
                "status": status,
                "tools": tools,
            }
            if registration_errors:
                module.update({
                    "code": "tool_registration_failed",
                    "reason": "one or more tools could not be registered",
                    "registration_errors": [dict(item) for item in registration_errors],
                })
            modules.append(module)
        for unavailable in self._unavailable_modules.values():
            tools = [
                {
                    "name": f"{unavailable['id']}__{local_name}",
                    "declared": True,
                    "registration": "unregistered",
                    "advertised_to_model": False,
                    "code": unavailable["code"],
                    "reason": unavailable["reason"],
                }
                for local_name in unavailable["declared_tools"]
            ]
            modules.append({
                "id": unavailable["id"],
                "description": unavailable["description"],
                "status": "degraded",
                "tools": sorted(tools, key=lambda item: str(item["name"])),
            })
        return {"modules": sorted(modules, key=lambda item: str(item["id"]))}

    async def capability_snapshot_async(self, registry: ToolRegistry, *, force: bool = False) -> dict[str, list[dict[str, Any]]]:
        """Attach safe runtime dependency results to the registration snapshot.

        在静态注册快照之上再叠一层**运行时依赖探测**（MCP 通不通、播放器在不在）。
        三层并发细节值得注意：
        1. `asyncio.gather` 并发跑所有 probe，每个 probe 自己带 2.5 秒超时，
           所以总耗时约等于最慢的那个 probe，而不是相加；单个 probe 卡死也不会拖住别人。
        2. 探测失败只把模块标成 degraded，**不抛异常**——能力探测的目的是报告健康，
           它自己出问题不该把 /api/health 打成 500。
        3. 对声明了 external_tool_names 的模块（目前是网易云），还要把探测结果
           投影到**每个工具**的执行状态上（execution 字段），因为"模块通了"不等于
           "这个具体工具在远端目录里存在"。三种取值：
             catalog_available —— 远端目录里有这个能力（只证明存在，没验证过能跑）
             verified          —— 真的验证过（目前只有 player_status 走这条路）
             unavailable       —— MCP 不通，或这个能力不在远端目录里
        前端就是靠 catalog_available / verified 的区别来避免"显示可用但一调就失败"。
        """
        snapshot = self.capability_snapshot(registry)
        by_id = {str(module["id"]): module for module in snapshot["modules"]}
        probes = [(spec.module_id, spec.probe) for spec in self.specs() if spec.probe is not None]

        async def run_probe(probe: CapabilityProbe) -> list[dict[str, Any]]:
            try:
                result = await asyncio.wait_for(
                    probe(force=force), timeout=CAPABILITY_PROBE_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                logger.error("capability probe timed out")
                return [{
                    "name": "runtime",
                    "status": "unhealthy",
                    "code": "probe_timeout",
                    "reason": "capability probe timed out",
                }]
            except Exception:
                logger.exception("capability probe failed")
                return [{
                    "name": "runtime",
                    "status": "unhealthy",
                    "code": "probe_failed",
                    "reason": "capability probe failed",
                }]
            return [dict(item) for item in result if isinstance(item, dict)]

        if probes:
            results = await asyncio.gather(*(run_probe(probe) for _, probe in probes))
            for (module_id, _), dependencies in zip(probes, results):
                module = by_id[module_id]
                module["dependencies"] = dependencies
                if any(item.get("status") != "healthy" for item in dependencies):
                    module["status"] = "degraded"
                spec = self.resolve(module_id)
                if spec is None or not spec.external_tool_names:
                    continue
                mcp = next((item for item in dependencies if item.get("name") == "mcp"), {})
                player = next((item for item in dependencies if item.get("name") == "player"), {})
                catalog = {str(name) for name in mcp.get("tools", [])}
                mcp_healthy = mcp.get("status") == "healthy"
                player_healthy = player.get("status") == "healthy"
                for tool in module["tools"]:
                    local_name = str(tool["name"]).split("__", 1)[-1]
                    external_name = spec.external_tool_names.get(local_name)
                    if external_name is None:
                        continue
                    if not mcp_healthy or external_name not in catalog:
                        tool["execution"] = "unavailable"
                    elif local_name == "player_status":
                        tool["execution"] = "verified" if player_healthy else "unavailable"
                    else:
                        tool["execution"] = "catalog_available"
        return snapshot

    def resolve(self, module_id: str) -> ModuleSpec | None:
        """取模块；找不到返回 None（调用方自己决定报错还是降级）。"""
        return self._modules.get(module_id)

    def required_for_readiness(self, module_id: str) -> bool:
        """Return deployment criticality for registered and failed modules.

        要同时查两个地方：注册成功的模块看 spec，**注册失败的模块看 _unavailable_modules**。
        否则一个"部署硬要求但没起来"的模块会被当成不存在，/api/ready 就会误报 ready。
        """
        spec = self.resolve(module_id)
        if spec is not None:
            return spec.required_for_readiness
        unavailable = self._unavailable_modules.get(module_id)
        return bool(unavailable and unavailable["required_for_readiness"])

    def specs(self) -> list[ModuleSpec]:
        """Return registered module metadata in stable module-id order.

        按 module_id 排序而不是按注册顺序：注册顺序取决于 import 顺序，
        不稳定；能力快照要前后两次比对时，顺序一变就等于整个列表都"变了"。
        """
        return [self._modules[key] for key in sorted(self._modules)]

    def tools_for(self, module_ids: set[str]) -> ToolRegistry:
        """按模块子集构建一个**全新的** ToolRegistry。

        这是"这一次请求允许用哪些工具"的实现点：调用方给出模块名单，
        这里逐模块构建工具、拼上命名空间、注册进去。

        为什么每个工具都要包 try：模块里的**一个**工具构建失败（比如某个工具
        依赖的库没装），不该让整个模块的其他工具一起消失。失败的那个记成
        registration_errors + 工具级诊断，其余照常注册。

        注意它**每次都重新 build_tools()**，不缓存——所以工具构建函数必须便宜、
        无副作用（这也是为什么各模块的 build_tools 都是纯拼 ToolSpec）。
        """
        registry = ToolRegistry()
        for module_id in sorted(module_ids):
            spec = self.resolve(module_id)
            if spec is None:
                raise ValueError(f"unknown module: {module_id}")
            # 先把"声明过的工具"全部预置成 unregistered，再逐个覆盖成 registered。
            # 这样构建中途失败时，剩下那些没跑到的工具仍有诊断行，不会凭空消失。
            states: dict[str, dict[str, Any]] = {
                f"{module_id}__{local_name}": {
                    "name": f"{module_id}__{local_name}",
                    "registration": "unregistered",
                    "advertised_to_model": False,
                    "code": "tool_unregistered",
                    "reason": "tool was declared but not registered",
                }
                for local_name in spec.declared_tools
            }
            registration_errors: list[dict[str, str]] = []
            try:
                built = spec.build_tools()
            except Exception as exc:
                logger.exception("capability tool builder failed (%s)", module_id)
                self.set_diagnostic(module_id, status="error", reason=f"{type(exc).__name__}: {exc}")
                for state in states.values():
                    state.update({
                        "registration": "build_failed",
                        "code": "tool_build_failed",
                        "reason": "tool builder failed before registration",
                    })
                self._tool_diagnostics[module_id] = states
                self._tool_registration_errors[module_id] = registration_errors
                continue
            tools = []
            for tool in built:
                try:
                    namespaced = self._namespaced_tool(module_id, tool, spec.description)
                    registry.register(namespaced)
                    tools.append(namespaced.name)
                    states[namespaced.name] = {
                        "name": namespaced.name,
                        "registration": "registered",
                        "advertised_to_model": True,
                        "code": "",
                        "reason": "",
                    }
                except Exception as exc:
                    logger.exception(
                        "capability tool registration failed (%s, %s)",
                        module_id,
                        getattr(tool, "name", ""),
                    )
                    self.set_diagnostic(module_id, status="tool_build_failed", reason=f"{type(exc).__name__}: {exc}", tool=getattr(tool, "name", ""))
                    raw_name = str(getattr(tool, "name", "") or "unknown")
                    name = f"{module_id}__{raw_name.rsplit('.', 1)[-1]}"
                    registration_errors.append({
                        "name": name,
                        "code": "tool_registration_failed",
                        "reason": "tool could not be registered",
                    })
                    if states.get(name, {}).get("registration") == "registered":
                        continue
                    states[name] = {
                        "name": name,
                        "registration": "invalid",
                        "advertised_to_model": False,
                        "code": "tool_registration_failed",
                        "reason": "tool could not be registered",
                    }
            self._tool_diagnostics[module_id] = states
            self._tool_registration_errors[module_id] = registration_errors
            # 模块级状态只在两种情况下改：
            # - 一个工具都没注册成功 → tool_unregistered；
            # - 原本是 registered 且这次确实产出了工具 → 刷新 tools 列表。
            # 中间态（已经 error / tool_build_failed 且这次也只成功了一部分）**故意不动**，
            # 免得用一个"部分成功"覆盖掉更严重的失败原因。
            if not tools:
                self.set_diagnostic(module_id, status="tool_unregistered", reason="no tools registered", tools=[])
            elif self._diagnostics.get(module_id, {}).get("status") == "registered":
                self.set_diagnostic(module_id, status="registered", reason="", tools=tools)
        return registry

    async def start_all(self) -> None:
        """按注册顺序启动所有模块的生命周期钩子（幂等：已启动则直接返回）。

        钩子可以是 async（如 media 的 service.start、work 的 WorkHost.start）。
        任一模块启动失败会**反向回滚已启动的模块**再抛异常 —— 半启动状态比彻底失败更难查。
        """
        if self._started:
            return
        started: list[ModuleSpec] = []
        try:
            for module_id in self._module_order:
                spec = self._modules[module_id]
                if spec.start is not None:
                    await spec.start()
                started.append(spec)
        except Exception:
            for spec in reversed(started):
                if spec.stop is not None:
                    await spec.stop()
            raise
        self._started = True

    async def stop_all(self) -> None:
        """按**启动的逆序**停止模块。

        逆序是有意的：后启动的模块可能依赖先启动的（如 navigation 依赖 location_store），
        正序停止会让依赖方先消失。
        """
        if not self._started:
            return
        try:
            for module_id in reversed(self._module_order):
                spec = self._modules[module_id]
                if spec.stop is not None:
                    await spec.stop()
        finally:
            # 即使某个 stop 抛异常也要把标记清掉，否则进程再也无法重新 start_all。
            self._started = False

    @staticmethod
    def _namespaced_tool(module_id: str, tool: ToolSpec, module_description: str = "") -> ToolSpec:
        """把一个模块内工具包装成对模型可见的"命名空间工具"。

        做三件事，每件都有具体原因：
        1. **拼名字** `模块__工具`。用双下划线而不是点号，是因为 OpenAI 兼容接口的
           函数名不允许点号；而单下划线会与工具名里自带的 `_` 混淆，双下划线不会。
           同时校验长度 ≤ 64（接口的硬限制），超了在这里就报错，不留到调用时。
        2. **改 schema 里的 name**（模型看到的是函数名，不是 registry 里的键）。
        3. **给 description 加`【模块中文名】`前缀**。模型是靠 description 选工具的，
           前缀让它在长工具列表里一眼看出"这个能力属于哪一块"。
           已经以【开头的描述不重复加前缀（避免多次包装时越叠越长）。
        注意：raw_name 用 rsplit(".") 取最后一段，是为了兼容老代码里
        `__init__.py` 冒号写法（如 `a.b` → `b`）的历史命名。
        """
        raw_name = tool.name.rsplit(".", 1)[-1]
        if not _IDENTIFIER.fullmatch(raw_name):
            raise ValueError(f"tool name must be an ASCII identifier: {tool.name}")
        # OpenAI-compatible function names do not allow dots. A double underscore
        # preserves a stable module namespace while remaining provider-valid.
        name = f"{module_id}__{raw_name}"
        if len(name) > 64:
            raise ValueError(f"namespaced tool name exceeds 64 characters: {name}")
        schema = deepcopy(tool.schema)
        function = schema.get("function")
        if isinstance(function, dict):
            function["name"] = name
            owner = module_description.strip() or module_id
            description = str(function.get("description") or "").strip()
            if not description.startswith("【"):
                function["description"] = (
                    f"【{owner}】{description}"
                    if description
                    else f"【{owner}】工具 {tool.name}。"
                )
        # 重建 ToolSpec 时把**授权、路由与校验策略字段**一起搬过去。这里漏一个字段，
        # 对应的路由/闸门能力就在生产注册表（tools_for → 包装 → register）里静默失效，
        # 而"按名字 resolve"的路由（音乐/事实查询）不受影响——失效是静默的，
        # 只有走策略字段的路径（意图闸门、只读强路由、hard_routable 硬拦、
        # argument_validator 结构校验）会死。
        # 历史教训：2026-09-13 实测，漏搬 intent_policies 让意图闸门整个空转、
        # 漏搬 effect+deterministic_route_allowed 让四条只读命令回落给模型。
        # 新增 ToolSpec 策略字段时必须同步加到这里，并靠
        # tests/test_module_architecture.py::test_namespacing_preserves_policy_fields 守门。
        return ToolSpec(
            name=name,
            schema=schema,
            handler=tool.handler,
            risk=tool.risk,
            timeout_seconds=tool.timeout_seconds,
            confirmation_title=tool.confirmation_title,
            effect=tool.effect,
            hard_routable=tool.hard_routable,
            deterministic_route_allowed=tool.deterministic_route_allowed,
            query_extractor=tool.query_extractor,
            intent_policies=tool.intent_policies,
            argument_validator=tool.argument_validator,
        )
