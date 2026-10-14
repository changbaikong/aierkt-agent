"""能力目录：模块与工具声明的**唯一**真相。

【它取代了什么】

在这之前，"系统里有哪些模块、每个模块有哪些工具"有**两份**声明：

    agent_tools/__init__.py::discover_modules()      真实工厂列表（会构建模块）
    agent_tools/__init__.py::BUILTIN_MODULE_MANIFESTS  静态清单（只写名字，不构建）

第二份的存在理由听起来合理："工厂挂了也要能报出它本该有哪些工具"。
问题是它必须跟第一份**手动保持同步**，而没人会记得。2026-09-21 的事故：
静态清单里漏了 `weather`，而报告"某模块不可用"的代码正好去查这份清单 →
`KeyError: 'weather'`。**诊断路径自己炸了**，前端能力面板拿不到任何答案，
用户看到的是"莫名其妙少了一块"。

止血只是给清单补一行；根因是"两份真相"。这个文件杀掉其中一份。

【三件事，按重要性排】

1. **声明即构建**。每个条目带一个工厂函数。声明了这个模块，就一定构建得出来；
   构建不出来就是工厂失败（一个**运行期**事实，有诊断记录），
   而不是"声明与实现不一致"（一个**设计期**事实，只能靠人记得改两处）。
2. **工具名在同一处声明**。`declared_tools` 不再手工维护，
   而是从工厂的产出里取（见 `CapabilityCatalog.resolved_tools`）。
   手工名单与真实产出会漂移，取产出不会。
3. **目录构造完就冻结**。运行期注册表建完之后不许再往里塞东西 ——
   "谁能提供工具"在启动那一刻就定死了，之后再变就是两条真相重新长出来。

【关于"工厂挂了报不出工具名"这个原始诉求】

诉求本身是对的，解法换了个方向：不再维护一份"本该有哪些工具"的**模块侧**手写清单，
而是让**唯一**那份声明（本文件）承担这个职责。工厂挂了的时候，
`resolve_module()` 捕获失败并把原因记进 `failures`，`declared_tools`
仍然来自那份唯一声明 —— 信息没少，同步负担没了。

`declared_tools` 因此是"声明层面的预期"，不依赖工厂跑成功。
这一点对 weather 那个场景是关键：天气工厂失败时，我们仍然要能说出
"本该有 weather__query"，而这句现在不需要第二份清单来提供。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .contracts import ACTION_ORIGINS

logger = logging.getLogger(__name__)

#: 工具可见性。`model` = 出现在给模型的工具列表里；`internal` = 只在本机内部调用
#: （比如 Work 的审批动作——它由审批链接触发，不该由模型自己发起）。
VISIBILITY_MODEL = "model"
VISIBILITY_INTERNAL = "internal"


@dataclass(frozen=True)
class CapabilityEntry:
    """目录里的一条：某个模块能提供什么。

    Attributes:
        module_id: 模块标识。会拼进工具名（`模块__工具`），必须是 ASCII 标识符。
        description: 中文短描述，用于能力快照与诊断。
        declared_tools: 本模块**预期**产出的工具本地名（不含模块前缀）。
            这是声明层面的预期，工厂失败时仍然有效 —— 诊断就是靠它说出
            "本该有哪些工具"。
        visibility: 默认可见性。模块可以逐工具覆盖（见 `tool_visibility`）。
        allowed_origins: 本模块工具默认允许的来源。None 表示不限制。
        build: 构建本模块的工厂。**惰性调用**：只有在真的要装配时才跑，
            所以构造目录本身不会碰环境变量、不会连数据库。
    """

    module_id: str
    description: str
    declared_tools: tuple[str, ...]
    build: Callable[["BuildContext"], Any]
    visibility: str = VISIBILITY_MODEL
    allowed_origins: frozenset[str] | None = None
    required_for_readiness: bool = False
    #: 逐工具覆盖可见性与来源限制（键是工具本地名）。
    tool_visibility: dict[str, str] = field(default_factory=dict)
    tool_allowed_origins: dict[str, frozenset[str]] = field(default_factory=dict)

    def visibility_for(self, local_name: str) -> str:
        """某个工具最终对模型可见吗。"""
        return self.tool_visibility.get(local_name, self.visibility)

    def origins_for(self, local_name: str) -> frozenset[str] | None:
        """某个工具允许哪些来源调用。"""
        return self.tool_allowed_origins.get(local_name, self.allowed_origins)


@dataclass
class BuildContext:
    """装配模块时要传进去的依赖。**显式传参，没有模块级全局槽。**

    它取代了原来的两个东西：
    - `agent_tools._permission_layer` 模块级全局槽（靠 `set_permission_layer`
      在注册前塞进去、work 模块在 discover 期间 get 出来）。全局槽的问题是
      "谁在什么时候设的"只能靠读调用顺序，而调用顺序会被下一个改动打乱。
    - 各入口自己 `os.getenv` 拼路径。默认值写两遍就会漂移，
      表现为"Web 上建的定时任务在 CLI 里看不见"。

    Attributes:
        provider: 模型 provider（tasks / conditions 需要它转派）。
        event_bus: 事件总线。
        permission_layer: 账号级权限层（work 模块构造 WorkHost 要用）。
        work_roots: Work 允许触达的目录（Web 与 CLI 必须同源）。
        task_db / condition_task_db / media_task_db: 各库路径。
        disabled_modules: 被配置禁用的模块集合。
    """

    provider: Any = None
    registry_provider: Callable[[], Any] | None = None
    event_bus: Any = None
    permission_layer: Any = None
    work_roots: tuple[str, ...] = ()
    #: 主库路径（确认单、运行快照、意图状态都在里面）。
    #: **两个入口都从这里取**，不再各自 `os.getenv("AGENT_CORE_DB", ...)`。
    core_db: str = "agent_core.db"
    task_db: str = "agent_tasks.db"
    condition_task_db: str = "agent_condition_tasks.db"
    media_task_db: str = "./data/media_tasks.db"
    code_agent_enabled: bool = True
    code_runtime_root: str = ""
    code_powershell_executable: str = ""
    code_python_executable: str = ""
    code_search_executable: str = ""
    code_snapshot_root: str = "./data/code_snapshots"
    code_sandbox_state_root: str = "./data/code_sandbox_state"
    code_artifact_dir: str = "./data/code_artifacts"
    code_verification_config: str = ""
    code_agent_auto_apply_low_risk: bool = False
    code_apply_state_root: str = "./data/code_apply_state"
    code_workers: int = 1
    #: Harness 是唯一代码执行者；关闭时明确失败，不能回落到旧代码代理。
    harness_enabled: bool = True
    routing_mode: str = "legacy"
    legacy_shadow_enabled: bool = True
    disabled_modules: frozenset[str] = frozenset()
    #: 动作网关。**执行工具的唯一入口**。
    #: 由装配层在工具注册表建好之后填入；tasks / conditions 用它执行动作，
    #: 不再自己 resolve 自己跑。为 None 时这些模块只能记录失败
    #: （测试场景允许，生产不应出现 —— `_check` 会把它当一次调用失败改期）。
    gateway: Any = None
    #: 本次装配内共享的运行时句柄（位置存储、解析器、各 service）。
    #: 刻意用 `field(default_factory=dict)` 而不是模块级全局槽：
    #: 全局槽的问题是"谁在什么时候设的"只能靠读调用顺序，而调用顺序
    #: 会被下一个改动打乱；这里的作用域就是这一次装配，构造完就没人能碰。
    _shared: dict[str, Any] = field(default_factory=dict)


class CapabilityCatalog:
    """模块与工具的声明集合。构造后冻结。"""

    def __init__(self, entries: list[CapabilityEntry]) -> None:
        self._entries: dict[str, CapabilityEntry] = {}
        for entry in entries:
            if entry.module_id in self._entries:
                # 重名必须炸：静默覆盖会让一个模块顶掉另一个，
                # 而且从日志里完全看不出来（与 ModuleRegistry.register 同一取向）。
                raise ValueError(f"duplicate capability entry: {entry.module_id}")
            if entry.visibility not in {VISIBILITY_MODEL, VISIBILITY_INTERNAL}:
                raise ValueError(f"invalid visibility for {entry.module_id}: {entry.visibility}")
            self._entries[entry.module_id] = entry
        self._frozen = True

    # ── 读取 ────────────────────────────────────────────────────────

    def entry(self, module_id: str) -> CapabilityEntry | None:
        return self._entries.get(module_id)

    def entries(self) -> list[CapabilityEntry]:
        """按 module_id 排序返回。排序而不是按声明顺序：声明顺序取决于
        import 顺序，不稳定；能力快照要前后比对时顺序一变就全"变了"。"""
        return [self._entries[key] for key in sorted(self._entries)]

    def module_ids(self) -> list[str]:
        return sorted(self._entries)

    def declared_tools(self, module_id: str) -> tuple[str, ...]:
        """声明的工具清单。工厂失败也拿得到 —— 这正是它取代静态清单的地方。"""
        entry = self._entries.get(module_id)
        return entry.declared_tools if entry is not None else ()

    def disabled(self, ctx: BuildContext) -> set[str]:
        """被配置禁用、或不在目录里的模块，都算不可用。"""
        unknown = set(ctx.disabled_modules) - set(self._entries)
        if unknown:
            # 配错名字（比如 AGENT_DISABLED_MODULES=weathers）不该静默无效：
            # 用户以为关掉了一个模块，实际什么都没关。
            logger.warning("AGENT_DISABLED_MODULES mentions unknown modules: %s", sorted(unknown))
        return set(ctx.disabled_modules)

    # ── 构建 ────────────────────────────────────────────────────────

    def resolve_module(self, module_id: str, ctx: BuildContext) -> tuple[Any | None, str]:
        """构建一个模块，返回 (模块实例, 失败原因)。

        失败不抛出来：模块工厂挂掉是**正常情况**（key 没配、依赖没装），
        核心服务要照样 ready。原因回给调用方，由它记进诊断。
        """
        entry = self._entries.get(module_id)
        if entry is None:
            return None, "module is not declared in the capability catalog"
        if module_id in ctx.disabled_modules:
            return None, "disabled by configuration"
        try:
            return entry.build(ctx), ""
        except Exception as exc:
            logger.exception("capability module factory failed (%s)", module_id)
            return None, f"{type(exc).__name__}: {exc}"


def _work_entry() -> CapabilityEntry:
    """Work 模块的目录条目。

    声明是**四个**工具（2026-09-22 从九个收窄）：单步提交 / 计划提交 /
    统一状态 / 统一取消。收窄的理由见 `agent_tools/work.py` 的模块头 ——
    一句话是"`approve` 让模型能替用户批准高风险操作"。

    已经没有 `tool_visibility` 覆盖了：以前只有 `approve` 需要标 `internal`
    （模型若能自己调 approve，高风险确认就形同虚设）。它整个删掉之后，
    四个入口对模型就是全部可见的，不存在"看得见但不该调"的工具。
    """
    return CapabilityEntry(
        module_id="work",
        description="后台工作计划编排",
        declared_tools=("submit", "plan_submit", "status", "cancel"),
        build=_build_work,
        allowed_origins=frozenset({"chat_model", "chat_fastpath", "subagent", "work_host", "api"}),
    )


def _build_work(ctx: BuildContext) -> Any:
    from agent_core.work_module import build_work_module

    module, _store = build_work_module(
        permission_layer=ctx.permission_layer,
        allowed_roots=list(ctx.work_roots) or None,
        code_agent_enabled=ctx.code_agent_enabled,
        code_runtime_root=ctx.code_runtime_root,
        code_powershell_executable=ctx.code_powershell_executable,
        code_python_executable=ctx.code_python_executable,
        code_search_executable=ctx.code_search_executable,
        code_snapshot_root=ctx.code_snapshot_root,
        code_sandbox_state_root=ctx.code_sandbox_state_root,
        code_artifact_dir=ctx.code_artifact_dir,
        code_verification_config=ctx.code_verification_config,
        code_agent_auto_apply_low_risk=ctx.code_agent_auto_apply_low_risk,
        code_apply_state_root=ctx.code_apply_state_root,
        code_workers=ctx.code_workers,
        harness_enabled=ctx.harness_enabled,
    )
    return module


def _subagent_entry() -> CapabilityEntry:
    """子代理模块的目录条目。

    它被收进目录的理由和 work 一模一样：之前 `web_api.py` 与 `chat_cli.py`
    各注册一遍，Web 那条还包在 try/except 里静默降级，两个入口传的参数也不同
    （一个显式复用 run_store、一个走默认库）。目录是唯一来源之后，
    这些分叉没有地方可以再长出来。
    """
    return CapabilityEntry(
        module_id="subagent",
        description="独立预算的子任务委派",
        declared_tools=("dispatch",),
        build=_build_subagent,
        allowed_origins=frozenset({"chat_model", "chat_fastpath", "subagent", "work_host", "api"}),
    )


def _build_subagent(ctx: BuildContext) -> Any:
    from agent_core.prompts import PromptComposer
    from agent_core.subagent_module import build_subagent_module

    return build_subagent_module(
        provider=ctx.provider,
        registry_provider=ctx.registry_provider,
        event_bus=ctx.event_bus,
        permission_layer=ctx.permission_layer,
        prompt_composer=PromptComposer(),
    )


def build_catalog() -> CapabilityCatalog:
    """构造能力目录。

    这里声明的模块集合与每个模块的工具清单是**唯一**的声明来源；
    `tests/test_capability_catalog.py` 会拿它跟真实工厂对账，两边不许有差。
    """
    from agent_core.media_module import build_media_module
    from agent_tools.modules import (
        build_condition_task_module,
        build_location_module,
        build_navigation_module,
        build_netease_module,
        build_search_module,
        build_system_module,
        build_task_module,
        build_weather_module,
    )

    entries = [
        CapabilityEntry(
            module_id="search",
            description="联网核实：网页搜索、B站搜索与视频详情、网页原文读取。日常问答可直接检索，范围默认自行决定",
            declared_tools=("web", "bilibili", "bilibili_video", "read"),
            build=lambda ctx: build_search_module(),
            allowed_origins=frozenset({"chat_model", "chat_fastpath", "subagent"}),
        ),
        CapabilityEntry(
            module_id="netease",
            description="网易云音乐",
            # 2026-09-21：这份清单由"目录 vs 真实工厂"对账测试逼出来的。
            # 旧的静态清单（已删）漏了 get_volume / set_volume 两个 ——
            # 它在 `play` 与 `resume` 之间少写了两项，而没人发现，
            # 因为漏的后果只是能力面板少显示两个工具。
            # 这份是照着 `make_netease_tools()` 的真实产出核对的。
            declared_tools=(
                "status", "login", "search", "daily_recommend", "my_playlists",
                "playlist_detail", "create_playlist", "add_to_playlist", "album_info",
                "artist_info", "my_subscriptions", "play", "player_status", "next",
                "pause", "resume", "search_tracks", "get_volume", "set_volume",
            ),
            build=lambda ctx: build_netease_module(),
        ),
        CapabilityEntry(
            module_id="system",
            description="读取本机只读信息",
            declared_tools=("local_info",),
            build=lambda ctx: build_system_module(),
        ),
        CapabilityEntry(
            module_id="media",
            description="语音合成",
            # ASR 未实现（media_service.submit_asr 直接 NotImplementedError），暂不声明。
            declared_tools=("submit_tts", "get_status", "cancel", "get_artifact"),
            # 注意要传 Path 不是 str：`MediaTaskStore.__init__` 直接对 `db_path.parent`
            # 调 mkdir，传字符串会 `AttributeError: 'str' object has no attribute 'parent'`。
            # 这个坑在旧的装配代码里被"恰好传入的是 Path"掩盖着，
            # 目录化之后第一次暴露 —— 所以这里显式转换。
            build=lambda ctx: build_media_module(db_path=Path(ctx.media_task_db)),
        ),
        CapabilityEntry(
            module_id="location",
            description="浏览器当前位置",
            declared_tools=("current",),
            build=_build_location,
        ),
        CapabilityEntry(
            module_id="navigation",
            description="浏览器位置与高德导航",
            declared_tools=("route",),
            build=_build_navigation,
        ),
        CapabilityEntry(
            module_id="weather",
            description="高德天气查询",
            declared_tools=("query",),
            build=_build_weather,
        ),
        CapabilityEntry(
            module_id="tasks",
            description="创建和管理定时任务",
            declared_tools=("create", "list", "get", "cancel", "run_now"),
            build=_build_tasks,
        ),
        CapabilityEntry(
            module_id="conditions",
            description="创建条件触发提醒",
            declared_tools=("create_weather", "list", "cancel", "run_now"),
            build=_build_conditions,
        ),
        _work_entry(),
        _subagent_entry(),
    ]
    return CapabilityCatalog(entries)


# ── 位置 / 导航 / 天气：三者共用一份存储与解析器 ──────────────────────
#
# 为什么共享实例由目录来做而不是各模块自己建：LocationStore 是"用户现在在哪"的
# 唯一存储，各建一份就会出现"导航看到的起点与『我在哪』报的不是同一个"。
# 共享实例挂在 BuildContext 上（`_shared`），只在同一次装配内有效。

_SHARED_KEY = "location_store"
_RESOLVER_KEY = "place_resolver"


def _shared(ctx: BuildContext) -> dict[str, Any]:
    return ctx._shared


def _location_store(ctx: BuildContext) -> Any:
    from agent_tools.location import LocationStore

    shared = _shared(ctx)
    if _SHARED_KEY not in shared:
        shared[_SHARED_KEY] = LocationStore()
    return shared[_SHARED_KEY]


def _place_resolver(ctx: BuildContext) -> Any:
    """坐标 → 城市。需要高德 key，所以单独构造、单独失败。

    它失败只影响"不报城市名就问天气"这一条路，不该把位置或天气整个关掉。
    """
    from agent_tools.modules import build_place_resolver

    shared = _shared(ctx)
    if _RESOLVER_KEY not in shared:
        try:
            shared[_RESOLVER_KEY] = build_place_resolver()
        except Exception as exc:
            logger.warning("place resolution unavailable: %s", exc)
            shared[_RESOLVER_KEY] = None
    return shared[_RESOLVER_KEY]


def _build_location(ctx: BuildContext) -> Any:
    from agent_tools.modules import build_location_module

    return build_location_module(_location_store(ctx), _place_resolver(ctx))


def _build_navigation(ctx: BuildContext) -> Any:
    from agent_tools.modules import build_navigation_module

    module, store = build_navigation_module(location_store=_location_store(ctx))
    # 记下这次导航实际用的存储实例：装配层要把它挂在注册表上供 /api 读取，
    # 而"导航用的存储"必须是位置模块同一个，不能是导航自己新建的第二个。
    _shared(ctx)["navigation_store"] = store
    return module


def _build_weather(ctx: BuildContext) -> Any:
    from agent_tools.modules import build_weather_module

    return build_weather_module(
        location_store=_location_store(ctx), resolver=_place_resolver(ctx)
    )


def _build_tasks(ctx: BuildContext) -> Any:
    from agent_tools.modules import build_task_module

    module, service = build_task_module(
        provider=ctx.provider,
        registry_provider=ctx.registry_provider,
        event_bus=ctx.event_bus,
        db_path=ctx.task_db,
    )
    # 服务句柄不回传给目录（目录只管"声明"，不管"运行时对象"），
    # 而是放进本次装配的共享区，由装配层取走挂到注册表上。
    _shared(ctx)["task_service"] = service
    return module


def _build_conditions(ctx: BuildContext) -> Any:
    from agent_tools.modules import build_condition_task_module

    module, service = build_condition_task_module(
        registry_provider=ctx.registry_provider,
        event_bus=ctx.event_bus,
        db_path=ctx.condition_task_db,
        gateway=ctx.gateway,
    )
    _shared(ctx)["condition_task_service"] = service
    return module


__all__ = [
    "VISIBILITY_INTERNAL",
    "VISIBILITY_MODEL",
    "BuildContext",
    "CapabilityCatalog",
    "CapabilityEntry",
    "ACTION_ORIGINS",
    "build_catalog",
]
