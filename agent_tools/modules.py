"""Module factories for optional Agent tools."""

from __future__ import annotations

from dataclasses import replace
import logging
from typing import Callable

from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, EFFECT_TRANSIENT, IntentPolicy, ToolSpec
from agent_core.events import EventBus
from agent_core.modules import ModuleSpec
from agent_core.task_persistence import TaskStore
from agent_core.task_service import TaskService
from agent_core.tools import classify_tool_error

from .netease import NeteaseCatalogProvider, make_netease_search_tool
from .background_player import hide_netease_window
from .condition_tasks import ConditionTaskService, ConditionTaskStore, make_condition_task_tools
from .location import LocationStore, make_location_tool
from .navigation import AmapNavigationClient, AmapPlaceResolver, make_navigation_tool
from .weather import AmapWeatherClient, make_weather_tool
from .system_info import make_system_info_tool
from .tasks import make_task_tools


logger = logging.getLogger(__name__)


_CLOUD_MUSIC_TOOL_NAMES = (
    "status", "login", "search", "daily_recommend", "my_playlists",
    "playlist_detail", "create_playlist", "add_to_playlist", "album_info",
    "artist_info", "my_subscriptions", "play", "player_status", "next",
    "pause", "resume", "get_volume", "set_volume",
)

_CLOUD_MUSIC_EXTERNAL_TOOL_NAMES = {
    "status": "cloud_music_status",
    "login": "cloud_music_login",
    "search": "cloud_music_search",
    "daily_recommend": "cloud_music_get_daily_recommend",
    "my_playlists": "cloud_music_my_playlists",
    "playlist_detail": "cloud_music_playlist_detail",
    "create_playlist": "cloud_music_create_playlist",
    "add_to_playlist": "cloud_music_add_to_playlist",
    "album_info": "cloud_music_album_info",
    "artist_info": "cloud_music_artist_info",
    "my_subscriptions": "cloud_music_my_subscriptions",
    "play": "cloud_music_play",
    "player_status": "cloud_music_player_status",
    "next": "cloud_music_next",
    "pause": "cloud_music_pause",
    "resume": "cloud_music_resume",
    "get_volume": "cloud_music_get_volume",
    "set_volume": "cloud_music_set_volume",
    "search_tracks": "cloud_music_search",
}


def build_search_module(tool_builder: Callable[[], ToolSpec | None] | None = None) -> ModuleSpec:
    """Build the optional web-search module without coupling it to the CLI.

    模块产出四个工具：网页搜索（博查）、B站搜索、B站视频详情、网页原文读取
    （Jina Reader，与 Agent-Reach 同款阅读服务）。只有网页搜索依赖
    BOCHA_API_KEY；B站与网页读取零配置可用。整体降级口径：
    - BOCHA 未配置 → 网页搜索缺席，B站/读取仍注册（不互相拖垮）；
    - 工具构建抛异常 → 只记 diagnostics，不让其他模块跟着失败。
    """

    if tool_builder is None:
        # Keep module registration independent from search-only dependencies
        # such as httpx. This also lets other modules start when search is off.
        def builder() -> list[ToolSpec]:
            from .web_search import build_web_search_tool
            from .bilibili import make_bilibili_search_tool, make_bilibili_video_tool
            from .web_read import make_web_read_tool
            from .agent_reach_router import AgentReachRouter

            tools: list[ToolSpec] = []
            web_tool = build_web_search_tool()
            if web_tool is not None:
                # 模块命名约定：模块内本地名必须是 "web"（命名空间后即 search__web，
                # 确定性路由与提示词都按这个名字解析），不能沿用工具自带的名字。
                tools.append(replace(web_tool, name="web"))
            # 渠道检查只做覆盖说明：available 与否不影响工具注册，更不代表搜索会成功。
            router = AgentReachRouter()

            def channel_available() -> bool | None:
                try:
                    # 非阻塞缓存查询：doctor 体检很慢，未知时返回 None（不代表不可用）。
                    cached = router.available_platforms_cached()
                    return None if cached is None else "bilibili" in cached
                except Exception:
                    return None

            tools.append(make_bilibili_search_tool(channel_available=channel_available))
            tools.append(make_bilibili_video_tool(channel_available=channel_available))
            tools.append(make_web_read_tool())
            return tools
    else:
        builder = tool_builder

    def build_tools() -> list[ToolSpec]:
        try:
            built = builder()
        except Exception as exc:
            logger.warning("optional search capability unavailable: %s: %s", type(exc).__name__, exc)
            return []
        if built is None:
            logger.info("optional search capability unavailable: BOCHA_API_KEY is not configured")
            return []
        if isinstance(built, ToolSpec):
            # 兼容旧注入契约：builder 只产一个网页搜索工具（测试常用）。
            return [built]
        return [tool for tool in built if isinstance(tool, ToolSpec)]

    return ModuleSpec(
        "search", build_tools, memory_policy="shared_facts", description="联网核实：网页搜索、B站搜索与视频详情、网页原文读取。日常问答可直接检索，范围默认自行决定",
        declared_tools=("web", "bilibili", "bilibili_video", "read"),
    )


# 音乐搜索的意图门控配置。
# "我在歌厅上班"和"搜周杰伦的歌"都含"歌"，靠 action_terms 区分：
# 只有出现明确的点播/搜索动作词，才算"要搜歌"。
MUSIC_QUERY_POLICY = IntentPolicy(
    intent_name="music_query",
    object_terms=("歌", "歌曲", "音乐", "专辑", "歌单", "歌手"),
    action_terms=("播放", "放首", "来首", "听首", "点歌", "搜索", "搜一下", "搜", "查"),
    required_slots=("query",),
)


#: 每个网易云工具会在世界上留下什么。
#:
#: **缺席的名字一律按 persistent 处理**（最严的一档）。这是刻意的：新加一个工具的人
#: 必须显式把它写进这张表，忘了就会掉进"会留下东西"那一档，被超时对账与后续的
#: 低风险复核盯上 —— 宁可多管一个，也不能悄悄漏放一个。
_CLOUD_MUSIC_EFFECTS: dict[str, str] = {
    # 查询：读多少次都一样
    "status": EFFECT_READ,
    "search": EFFECT_READ,
    "search_tracks": EFFECT_READ,
    "daily_recommend": EFFECT_READ,
    "my_playlists": EFFECT_READ,
    "playlist_detail": EFFECT_READ,
    "album_info": EFFECT_READ,
    "artist_info": EFFECT_READ,
    "my_subscriptions": EFFECT_READ,
    "player_status": EFFECT_READ,
    "get_volume": EFFECT_READ,
    # 播放控制：改了但重新读一次 player_status 就知道现在在哪，不会留下东西
    "play": EFFECT_TRANSIENT,
    "pause": EFFECT_TRANSIENT,
    "resume": EFFECT_TRANSIENT,
    "next": EFFECT_TRANSIENT,
    "set_volume": EFFECT_TRANSIENT,
    # 会留下东西：歌单与登录态
    "create_playlist": EFFECT_PERSISTENT,
    "add_to_playlist": EFFECT_PERSISTENT,
    "login": EFFECT_PERSISTENT,
}


def _cloud_music_tool(
    name: str,
    description: str,
    handler,
    properties: dict,
    required: list[str] | None = None,
    *,
    risk: str = "low",
    confirmation_title: str = "Confirmation required",
) -> ToolSpec:
    schema = {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
                "additionalProperties": False,
            },
        },
    }
    deterministic = name in {"search", "player_status"}
    return ToolSpec(
        name, schema, handler, risk=risk, timeout_seconds=60.0,
        effect=_CLOUD_MUSIC_EFFECTS.get(name, EFFECT_PERSISTENT),
        deterministic_route_allowed=deterministic,
        confirmation_title=confirmation_title,
        query_extractor="music" if name == "search" else ("playback" if name == "player_status" else None),
        intent_policies=(MUSIC_QUERY_POLICY,) if name == "search" else (),
    )


def _cloud_handler(provider: object, method_name: str, argument_builder=lambda args: args):
    async def handler(arguments: dict, _scope) -> dict:
        try:
            method = getattr(provider, method_name)
            output = await method(**argument_builder(arguments))
            return {"status": "ok", "output": output if isinstance(output, dict) else {"data": output}}
        except Exception as exc:
            return {"status": "failed", "error": f"netease cloud provider error: {type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
    return handler


def _cloud_play_handler(provider: object):
    """播放工具处理器：播放成功后顺手把网易云主窗口隐藏（后台播放）。

    播放本身失败时不做后台化；后台化失败只告警、不阻塞播放成功的结果。
    """
    async def handler(arguments: dict, _scope) -> dict:
        try:
            method = getattr(provider, "play")
            output = await method(
                id=arguments.get("id"),
                type=str(arguments.get("type") or "song"),
            )
        except Exception as exc:
            return {"status": "failed", "error": f"netease cloud provider error: {type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
        try:
            await hide_netease_window()
        except Exception as exc:
            logger.warning("play succeeded but background hide failed: %s: %s", type(exc).__name__, exc)
        return {"status": "ok", "output": output if isinstance(output, dict) else {"data": output}}
    return handler


def _build_cloud_music_tools(provider: object) -> list[ToolSpec]:
    tools = [
        _cloud_music_tool("status", "检查网易云登录状态。", _cloud_handler(provider, "status", lambda _: {}), {}),
        _cloud_music_tool("login", "发起二维码登录或轮询登录状态。", _cloud_handler(provider, "login"), {"unikey": {"type": ["string", "null"]}}),
        _cloud_music_tool("search", "搜索歌曲、专辑、歌手或歌单。", _cloud_handler(provider, "search"), {"keyword": {"type": "string"}, "category": {"type": "string", "enum": ["song", "album", "artist", "playlist"], "default": "song"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}}, ["keyword"]),
        _cloud_music_tool("daily_recommend", "获取每日推荐歌曲。", _cloud_handler(provider, "daily_recommend", lambda _: {}), {}),
        _cloud_music_tool("my_playlists", "获取我的歌单。", _cloud_handler(provider, "my_playlists", lambda _: {}), {}),
        _cloud_music_tool("playlist_detail", "获取歌单详情。", _cloud_handler(provider, "playlist_detail"), {"playlist_id": {"type": ["string", "integer"]}}, ["playlist_id"]),
        _cloud_music_tool("create_playlist", "创建歌单。", _cloud_handler(provider, "create_playlist"), {"name": {"type": "string"}, "privacy": {"type": "boolean", "default": False}}, ["name"], risk="high", confirmation_title="创建歌单"),
        _cloud_music_tool("add_to_playlist", "向歌单添加歌曲。", _cloud_handler(provider, "add_to_playlist"), {"playlist_id": {"type": ["string", "integer"]}, "track_ids": {"type": "array", "items": {"type": ["string", "integer"]}, "minItems": 1}}, ["playlist_id", "track_ids"], risk="high", confirmation_title="向歌单添加歌曲"),
        _cloud_music_tool("album_info", "获取专辑详情。", _cloud_handler(provider, "album_info"), {"album_id": {"type": ["string", "integer"]}}, ["album_id"]),
        _cloud_music_tool("artist_info", "获取歌手详情。", _cloud_handler(provider, "artist_info"), {"artist_id": {"type": ["string", "integer"]}}, ["artist_id"]),
        _cloud_music_tool("my_subscriptions", "获取收藏的歌手或专辑。", _cloud_handler(provider, "my_subscriptions"), {"category": {"type": "string", "enum": ["artists", "albums"], "default": "artists"}}),
        _cloud_music_tool("play", "播放歌曲或歌单。", _cloud_play_handler(provider), {"id": {"type": ["string", "integer"]}, "type": {"type": "string", "enum": ["song", "playlist"], "default": "song"}}, ["id"], risk="low"),
        _cloud_music_tool("player_status", "获取本机网易云播放器状态。", _cloud_handler(provider, "player_status"), {}),
        _cloud_music_tool("next", "切换到下一首。", _cloud_handler(provider, "next", lambda _: {}), {}, risk="low"),
        _cloud_music_tool("pause", "暂停播放。", _cloud_handler(provider, "pause", lambda _: {}), {}, risk="low"),
        _cloud_music_tool("resume", "继续播放。", _cloud_handler(provider, "resume", lambda _: {}), {}, risk="low"),
        _cloud_music_tool("get_volume", "读取网易云的 Windows 应用音量（0-100）及静音状态。", _cloud_handler(provider, "get_volume", lambda _: {}), {}),
        # 音量是**改本机状态**的操作，而且用户往往只是随口一说；
        # 和系统音量一个口径：必须先确认再改。查询（get_volume）保持只读直答。
        _cloud_music_tool("set_volume", "设置网易云的 Windows 应用音量（0-100），保持静音开关及其他应用不变。调大或调小时先查询当前音量再计算目标值。", _cloud_handler(provider, "set_volume"), {"volume_percent": {"type": "integer", "minimum": 0, "maximum": 100}}, ["volume_percent"], risk="high", confirmation_title="调整网易云音量"),
    ]
    # Keep the original catalog contract available for callers that still use
    # the pre-MCP names, while the Web Agent exposes the richer cloud tools.
    from .netease_cloud import CloudMusicProvider
    if isinstance(provider, CloudMusicProvider):
        async def legacy_search(arguments: dict, scope) -> dict:
            try:
                query = str(arguments.get("query", "")).strip()
                limit = int(arguments.get("limit", 5))
                rows = await provider.search(query, category="song", limit=limit)
                return {"status": "ok", "output": {"query": query, "tracks": [
                    {"id": str(row.get("id", "")), "title": str(row.get("name", "")),
                     "artist": str(row.get("artist", "")), "album": ""}
                    for row in rows
                ]}}
            except Exception as exc:
                return {"status": "failed", "error": f"netease cloud provider error: {type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
        tools.append(_cloud_music_tool(
            "search_tracks", "兼容旧版网易云目录搜索。", legacy_search,
            {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
            ["query"],
        ))
    return tools


def build_netease_module(provider: object = None) -> ModuleSpec:
    """Build NetEase tools, using the external MCP provider by default."""
    if provider is None:
        from .netease_cloud import build_cloud_music_provider

        provider = build_cloud_music_provider()
    external_tool_names: dict[str, str] = {}
    if hasattr(provider, "search_tracks") and not hasattr(provider, "status"):
        tools = lambda: [make_netease_search_tool(provider)]
        declared_tools = ("search_tracks",)
    else:
        tools = lambda: _build_cloud_music_tools(provider)
        from .netease_cloud import CloudMusicProvider

        declared_tools = _CLOUD_MUSIC_TOOL_NAMES + (("search_tracks",) if isinstance(provider, CloudMusicProvider) else ())
        if isinstance(provider, CloudMusicProvider):
            external_tool_names = dict(_CLOUD_MUSIC_EXTERNAL_TOOL_NAMES)
    stop = getattr(provider, "close", None)
    probe = getattr(provider, "probe_capabilities", None)
    return ModuleSpec(
        "netease",
        tools,
        memory_policy="private_state",
        description="网易云音乐",
        stop=stop if callable(stop) else None,
        runtime=provider,
        probe=probe if callable(probe) else None,
        declared_tools=declared_tools,
        external_tool_names=external_tool_names,
    )


def build_system_module() -> ModuleSpec:
    return ModuleSpec(
        "system", lambda: [make_system_info_tool()], memory_policy="private_state",
        description="读取本机只读信息", declared_tools=("local_info",),
    )


def build_navigation_module(
    *, api_key: str | None = None, location_store: LocationStore | None = None,
    base_url: str = "https://restapi.amap.com",
) -> tuple[ModuleSpec, LocationStore]:
    """Build the optional browser-location and Amap navigation module."""
    import os
    from agent_core.project_env import load_project_env

    load_project_env()
    key = str(api_key if api_key is not None else os.getenv("AMAP_WEB_SERVICE_KEY", "")).strip()
    if not key:
        raise ValueError("AMAP_WEB_SERVICE_KEY is not configured")
    store = location_store or LocationStore()
    client = AmapNavigationClient(api_key=key, base_url=base_url)
    return ModuleSpec(
        "navigation",
        lambda: [make_navigation_tool(store, client)],
        memory_policy="private_state",
        description="浏览器位置与高德导航",
        declared_tools=("route",),
    ), store


def build_place_resolver(
    *, api_key: str | None = None, base_url: str = "https://restapi.amap.com",
    timeout_seconds: float = 3.5,
) -> AmapPlaceResolver:
    """单独构造"坐标 → 行政区"解析器。

    位置**存储**不需要任何密钥，只有"解析成哪个城市"才需要。拆成独立工厂，
    是为了让位置模块在没有 key 的机器上照样注册 —— 旧实现把两者绑在同一次
    try 里，key 一缺，"我在哪"就跟着一起消失了。
    """
    import os
    from agent_core.project_env import load_project_env

    load_project_env()
    key = str(api_key if api_key is not None else os.getenv("AMAP_WEB_SERVICE_KEY", "")).strip()
    if not key:
        raise ValueError("AMAP_WEB_SERVICE_KEY is not configured")
    return AmapPlaceResolver(
        AmapNavigationClient(api_key=key, base_url=base_url),
        timeout_seconds=timeout_seconds,
    )


def build_location_module(
    location_store: LocationStore,
    resolver: AmapPlaceResolver | None = None,
) -> ModuleSpec:
    """位置模块。

    ``resolver`` 缺省为 None 时模块照样注册，工具会返回
    ``location_resolution_unavailable``。"解析能力没配好"是一句可以对用户
    明说的状态，比整个模块消失（用户看到的是"这功能不存在"）要诚实。

    传进来的解析器会被真正接上 —— 这是方案第二部分要修的那个断点：
    解析能力早就写在导航模块里，只是从没交给过位置工具。
    """
    return ModuleSpec(
        "location",
        lambda: [make_location_tool(location_store, resolver)],
        memory_policy="private_state",
        description="浏览器当前位置",
        declared_tools=("current",),
    )


def build_weather_module(
    *, api_key: str | None = None, base_url: str = "https://restapi.amap.com",
    location_store: LocationStore | None = None, resolver: AmapPlaceResolver | None = None,
) -> ModuleSpec:
    """天气模块。

    ``location_store`` 与 ``resolver`` 都可选：缺了之后天气照样能用，
    只是"当前位置"这一条输入会返回 ``location_resolution_unavailable``；
    显式给城市名或行政区代码的查询完全不受影响。

    以前天气模块和位置存储之间没有任何连线，导致"天气"只能靠人来报城市名 ——
    这是方案第三部分要修的那条断链。
    """
    import os
    from agent_core.project_env import load_project_env
    load_project_env()
    key = str(api_key if api_key is not None else os.getenv("AMAP_WEB_SERVICE_KEY", "")).strip()
    if not key:
        raise ValueError("AMAP_WEB_SERVICE_KEY is not configured")
    client = AmapWeatherClient(api_key=key, base_url=base_url)
    return ModuleSpec(
        "weather",
        lambda: [make_weather_tool(client, location_store=location_store, place_resolver=resolver)],
        memory_policy="private_state",
        description="高德天气实况与预报",
        declared_tools=("query",),
    )


def build_task_module(
    *, provider: object = None, registry_provider: Callable[[], object] | None = None,
    event_bus: EventBus | None = None, db_path: object = "agent_tasks.db",
) -> tuple[ModuleSpec, TaskService]:
    from pathlib import Path
    from agent_core.tools import ToolRegistry

    get_registry = registry_provider or (lambda: ToolRegistry())
    service = TaskService(
        provider=provider,
        registry_provider=get_registry,  # type: ignore[arg-type]
        store=TaskStore(Path(db_path)),
        event_bus=event_bus,
    )
    return ModuleSpec(
        "tasks", lambda: make_task_tools(service), memory_policy="private_state",
        description="创建和管理定时任务", declared_tools=("create", "list", "get", "cancel", "run_now"),
    ), service


def build_condition_task_module(
    *, registry_provider: Callable[[], object] | None = None,
    event_bus: EventBus | None = None, db_path: object = "agent_condition_tasks.db",
    gateway: Any = None,
) -> tuple[ModuleSpec, ConditionTaskService]:
    """条件任务模块。

    `gateway` 是**必传的生产依赖**：条件任务在后台定时触发，旁边没人点确认，
    所以它反而是最必须过网关的一条执行路径。缺省为 None 只为了让
    只用工具、不跑 `_check` 的场景（若干旧测试）不必改签名。
    """
    from pathlib import Path
    from agent_core.tools import ToolRegistry

    get_registry = registry_provider or (lambda: ToolRegistry())
    service = ConditionTaskService(
        registry_provider=get_registry,  # type: ignore[arg-type]
        store=ConditionTaskStore(Path(db_path)),
        event_bus=event_bus,
        gateway=gateway,
    )
    return ModuleSpec(
        "conditions", lambda: make_condition_task_tools(service), memory_policy="private_state",
        description="创建条件触发提醒", declared_tools=("create_weather", "list", "cancel", "run_now"),
    ), service
