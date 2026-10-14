"""Amap geocoding and walking/driving navigation tool."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import httpx

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.tools import classify_tool_error

from .location import LocationStore

logger = logging.getLogger(__name__)

DEFAULT_AMAP_BASE_URL = "https://restapi.amap.com"
MAX_DESTINATION_LENGTH = 256
_COORDINATES = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# ----------------------------------------------------------------------
# 稳定错误码
# ----------------------------------------------------------------------
# 导航的失败原因**一律**以这些码对外。为什么不让调用方看原文：
# 原文里全是外部服务的内部细节 —— 高德的 HTTP 状态码、API 返回片段、
# 异常类名，有时还带 traceback 和本机路径。模型看着这些写解释，
# 迟早会有一句把 "Amap request failed with HTTP 500" 复述给用户。
# 码是稳定契约：措辞层按码说话，日志层留原文，两边各拿各的。
REASON_DESTINATION_REQUIRED = "destination_required"
REASON_DESTINATION_INVALID = "destination_invalid"
REASON_LOCATION_UNAVAILABLE = "location_unavailable"
REASON_LOCATION_STALE = "location_stale"
REASON_DESTINATION_NOT_FOUND = "destination_not_found"
#: 起点是用户**明确给**的（"从北京南站到故宫"），而这个名字定位不出来。
#: 与 destination_not_found 分开：用户要改的是另一头，说错一句会让他在错误的地方找问题。
REASON_ORIGIN_NOT_FOUND = "origin_not_found"
REASON_WALKING_RANGE_EXCEEDED = "walking_range_exceeded"
REASON_NAVIGATION_UNAVAILABLE = "navigation_unavailable"

# WGS-84 -> GCJ-02 转换参数（社区广泛使用的近似算法，误差约 1-2 米）
_GCJ02_A = 6378245.0
_GCJ02_EE = 0.00669342162296594323


def _out_of_china(longitude: float, latitude: float) -> bool:
    # GCJ-02 偏移仅在中国大陆范围内生效，范围外原样返回
    return not (72.004 <= longitude <= 137.8347 and 0.8293 <= latitude <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(longitude: float, latitude: float) -> tuple[float, float]:
    """把浏览器 GPS 坐标(WGS-84)转换为高德使用的 GCJ-02 坐标。

    navigator.geolocation 返回 WGS-84，而高德全部 API 与 uri.amap.com 链接
    均使用 GCJ-02；在中国大陆直接混用会产生约 300-700 米的起点偏移。
    """
    longitude = float(longitude)
    latitude = float(latitude)
    if _out_of_china(longitude, latitude):
        return longitude, latitude
    dlat = _transform_lat(longitude - 105.0, latitude - 35.0)
    dlng = _transform_lng(longitude - 105.0, latitude - 35.0)
    radlat = latitude / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _GCJ02_EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_GCJ02_A * (1 - _GCJ02_EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (_GCJ02_A / sqrtmagic * math.cos(radlat) * math.pi)
    return longitude + dlng, latitude + dlat


class AmapNavigationClient:
    def __init__(self, *, api_key: str, base_url: str = DEFAULT_AMAP_BASE_URL, timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        params = {"key": self.api_key, **params}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, transport=self.transport) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
        if response.status_code >= 400:
            raise RuntimeError(f"Amap request failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Amap returned invalid JSON") from exc
        if not isinstance(payload, dict) or str(payload.get("status")) != "1":
            info = payload.get("info") if isinstance(payload, dict) else ""
            raise RuntimeError(f"Amap request failed: {str(info)[:120]}")
        return payload

    async def regeo(self, latitude: float, longitude: float) -> dict[str, Any]:
        """逆地理编码：把坐标转成城市名，用于给目的地搜索加城市偏置。"""
        payload = await self._get("/v3/geocode/regeo", location=f"{longitude:.6f},{latitude:.6f}", extensions="base")
        regeocode = payload.get("regeocode") or {}
        component = regeocode.get("addressComponent") or {}
        city = component.get("city")
        # 直辖市（如北京）的 city 字段会是空数组，此时退回 province
        if not isinstance(city, str) or not city.strip():
            city = ""
        province = component.get("province")
        if not city and isinstance(province, str) and province.strip():
            city = province.strip()
        return {
            "province": str(component.get("province") or ""),
            "city": city,
            "district": str(component.get("district") or ""),
            "township": str(component.get("township") or ""),
            "area_code": str(component.get("adcode") or ""),
            "address": str(regeocode.get("formatted_address") or ""),
        }

    async def geocode(self, destination: str, city: str | None = None, citylimit: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"address": destination}
        if city and city.strip():
            params["city"] = city.strip()
            if citylimit:
                params["citylimit"] = "true"
        payload = await self._get("/v3/geocode/geo", **params)
        geocodes = payload.get("geocodes") or []
        if not geocodes or not isinstance(geocodes[0], dict):
            raise RuntimeError("destination_not_found")
        location = str(geocodes[0].get("location") or "")
        match = _COORDINATES.match(location)
        if not match:
            raise RuntimeError("Amap returned an invalid destination coordinate")
        longitude, latitude = map(float, match.groups())
        return {"latitude": latitude, "longitude": longitude, "label": str(geocodes[0].get("formatted_address") or destination), "city": str(geocodes[0].get("city") or "")}

    async def route(self, origin: tuple[float, float], destination: tuple[float, float], mode: str) -> dict[str, Any]:
        if mode not in {"driving", "walking"}:
            raise ValueError("mode must be driving or walking")
        path = "/v3/direction/driving" if mode == "driving" else "/v3/direction/walking"
        origin_text = f"{origin[1]},{origin[0]}"
        destination_text = f"{destination[1]},{destination[0]}"
        payload = await self._get(path, origin=origin_text, destination=destination_text)
        paths = ((payload.get("route") or {}).get("paths") or [])
        if not paths or not isinstance(paths[0], dict):
            raise RuntimeError("route_not_found")
        first = paths[0]
        try:
            distance = int(float(first.get("distance", 0)))
            duration = int(float(first.get("duration", 0)))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Amap returned an invalid route") from exc
        # "主要路线"取第一个导航段的那句话。这是高德给的中文描述
        # （"沿世纪大道向西行驶"），属于用户要的信息，不是要藏起来的接口原文；
        # 但要截断 —— 气泡是一眼扫过的，一整套分步指引没人读。
        steps = first.get("steps") or []
        instruction = ""
        if steps and isinstance(steps[0], dict):
            instruction = str(steps[0].get("instruction") or "").strip()[:60]
        return {
            "distance_meters": distance,
            "duration_seconds": duration,
            "mode": mode,
            "instruction": instruction,
        }


class AmapPlaceResolver:
    """把坐标解析成行政区，供位置工具（以及后面的天气链路）复用。

    两件事在这里收口：

    1. **独立超时。** 逆地理是外部调用，方案要求它有自己的时间限制，
       不该把整轮预算拖进一条链路上。默认 3.5 秒 —— 比 client 自己的
       httpx 超时（5 秒）短，这样超时由我们主动判定并给出稳定码，
       而不是等 httpx 抛出一堆带 HTTP 状态码的原文。

    2. **短时缓存。** 同一台设备的位置十几秒内不会变（位置上报本身就有
       10 秒限流），每次查询都打一次逆地理纯属浪费。缓存按坐标取整到
       约 100 米网格，TTL 默认 10 分钟 —— 正好对上方案里
       "城市解析成功后短时间缓存行政区代码"。

    缓存只活在进程内，和位置快照一样属于临时状态，不落库。
    """

    def __init__(
        self,
        client: AmapNavigationClient,
        *,
        timeout_seconds: float = 3.5,
        cache_ttl_seconds: float = 600.0,
        now: Any = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must not be negative")
        self.client = client
        self.timeout_seconds = float(timeout_seconds)
        self.cache_ttl_seconds = float(cache_ttl_seconds)
        self.now = now or (lambda: datetime.now(timezone.utc))
        # 只被事件循环线程读写（调用方是 async handler，且读写在 await 之外），
        # 所以这里不需要锁 —— 加锁反而会让人以为存在并发写入。
        self._cache: dict[tuple[float, float], tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def _grid(latitude: float, longitude: float) -> tuple[float, float]:
        """约 100 米网格：3 位小数在纬度上约 111 米。

        取整而不是用原始坐标，是为了让"同一个地点反复上报"命中同一条缓存；
        格距再放大会把相邻街区混成一个城市，反而错。
        """
        return (round(float(latitude), 3), round(float(longitude), 3))

    def _cached(self, key: tuple[float, float]) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        expires_at, place = entry
        if self.now().timestamp() >= expires_at:
            self._cache.pop(key, None)
            return None
        return deepcopy(place)

    def _remember(self, key: tuple[float, float], place: dict[str, Any]) -> None:
        # 容量上限用最简单的方式：设备数量本来就少，超过就整体丢弃重来。
        if len(self._cache) > 64:
            self._cache.clear()
        self._cache[key] = (self.now().timestamp() + self.cache_ttl_seconds, dict(place))

    async def __call__(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        latitude = float(snapshot["latitude"])
        longitude = float(snapshot["longitude"])
        key = self._grid(latitude, longitude)
        cached = self._cached(key)
        if cached is not None:
            return cached
        place = await asyncio.wait_for(
            self.client.regeo(latitude, longitude), timeout=self.timeout_seconds
        )
        self._remember(key, place)
        return place


def _parse_coordinates(value: str) -> tuple[float, float] | None:
    match = _COORDINATES.match(value)
    if not match:
        return None
    longitude, latitude = map(float, match.groups())
    if not math.isfinite(latitude) or not math.isfinite(longitude) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("destination coordinates are out of range")
    return latitude, longitude


def _city_core(name: Any) -> str:
    """提取城市名核心（去掉行政后缀），用于判断文本是否显式点名了该城市。"""
    if not isinstance(name, str):
        return ""
    name = name.strip()
    if not name:
        return ""
    return name[:-1] if name.endswith("市") else name


async def _resolve_destination(
    client: AmapNavigationClient,
    destination: str,
    city: str | None = None,
    *,
    city_task: "asyncio.Future[str | None] | None" = None,
) -> tuple[float, float]:
    """把目的地文本解析为坐标 (lat, lng)，策略（city 为用户所在城市）：

    1. 全国搜索结果就在用户同城 → 直接用（最常见，1 次调用）。
    2. 全国结果在外地时：
       - 文本显式点名了外地城市（如"杭州西湖"）→ 尊重全国结果（跨城意图）；
       - 否则（如"王府井"这类重名地名）→ 按用户城市 citylimit 严格就近；
       - citylimit 也没搜到（如"上海虹桥站"）→ 回退全国结果。
    3. 全国搜索失败时也尝试 citylimit，再不行才报 destination_not_found。

    ``city_task`` 给的是"当前位置导航"那条路的用法：城市要等逆地理才有，
    但那一步可以和全国搜索**并行**。传进来的 future 只有在真的需要城市偏置时
    才去 `await` 它 —— 全国结果就在本地时，逆地理慢不慢都影响不到这次导航。
    """
    national = None
    try:
        national = await client.geocode(destination)
    except Exception:
        national = None

    # 到这里才需要城市。此前（全国结果已经命中本地 / 压根没有全国结果）
    # 都不必等逆地理返回。
    if city is None and city_task is not None:
        city = await city_task
    if national is not None and city and national.get("city") == city:
        return float(national["latitude"]), float(national["longitude"])
    local = None
    if city:
        try:
            local = await client.geocode(destination, city=city, citylimit=True)
        except Exception:
            local = None
    if national is not None:
        national_core = _city_core(national.get("city"))
        if national_core and national_core in destination:
            # 用户点名了外地城市，尊重全国结果
            return float(national["latitude"]), float(national["longitude"])
    if local is not None:
        return float(local["latitude"]), float(local["longitude"])
    if national is not None:
        return float(national["latitude"]), float(national["longitude"])
    raise RuntimeError("destination_not_found")


async def _soft_city(client: AmapNavigationClient, origin: tuple[float, float]) -> str | None:
    """逆地理取城市；失败返回 None（不阻断导航，只是退回无城市偏置）。"""
    try:
        return str((await client.regeo(*origin)).get("city") or "") or None
    except Exception:
        return None


def _failure(status: str, reason: str, error_kind: str) -> dict[str, Any]:
    """失败结果的统一形状：``{status, output: {reason}, error, error_kind}``。

    ``error`` 填的也是**稳定码本身**，不是异常原文 —— 这份结果会直接进模型的
    observation，再被模型复述进气泡。原文只走 logger。
    """
    return {"status": status, "output": {"reason": reason}, "error": reason, "error_kind": error_kind}


def classify_navigation_error(exc: BaseException, *, mode: str) -> str:
    """已知的 provider 异常 → 稳定错误码；认不出的一律 navigation_unavailable。

    只认高德这几种明确的形状，其余不猜：猜错会说出与事实相反的话
    （比如把"服务挂了"说成"这个目的地不存在"）。
    """
    text = str(exc)
    if REASON_DESTINATION_NOT_FOUND in text:
        return REASON_DESTINATION_NOT_FOUND
    # 步行超出规划范围在高德有两条表现：驾车接口报 OVER_DIRECTION_RANGE，
    # 步行接口则直接不返回任何 path（route_not_found）。两者对用户是同一件事。
    if mode == "walking" and ("OVER_DIRECTION_RANGE" in text or "route_not_found" in text):
        return REASON_WALKING_RANGE_EXCEEDED
    return REASON_NAVIGATION_UNAVAILABLE


def make_navigation_tool(location_store: LocationStore, client: AmapNavigationClient) -> ToolSpec:
    async def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        destination = arguments.get("destination")
        mode = arguments.get("mode", "driving")
        origin_text = str(arguments.get("origin") or "").strip()
        if not isinstance(destination, str) or not destination.strip():
            return _failure("failed", REASON_DESTINATION_REQUIRED, "permanent")
        destination = destination.strip()
        if len(destination) > MAX_DESTINATION_LENGTH:
            return _failure("failed", REASON_DESTINATION_INVALID, "permanent")
        if len(origin_text) > MAX_DESTINATION_LENGTH:
            return _failure("failed", REASON_DESTINATION_INVALID, "permanent")
        if mode not in {"driving", "walking"}:
            return _failure("failed", REASON_DESTINATION_INVALID, "permanent")
        try:
            city = None
            # 只有"当前位置导航"这条路会起逆地理任务；显式初始化成 None，
            # 后面就不必靠 locals() 猜它存不存在（那种写法会随改名静默失效）。
            city_task: asyncio.Future[str | None] | None = None
            if origin_text:
                # 用户**明确给了起点**（"从北京南站到故宫"）。这条路上完全不碰位置共享 ——
                # 起点和终点都是地名，跟"我现在在哪"没关系。
                parsed_origin = _parse_coordinates(origin_text)
                if parsed_origin is not None:
                    origin = parsed_origin
                else:
                    try:
                        found = await client.geocode(origin_text)
                    except Exception as exc:
                        logger.warning("navigation origin not resolved: %s: %s", type(exc).__name__, exc)
                        return _failure("failed", REASON_ORIGIN_NOT_FOUND, "permanent")
                    origin = (float(found["latitude"]), float(found["longitude"]))
                    city = str(found.get("city") or "") or None
            else:
                current = location_store.get(scope)
                if current is None:
                    return _failure("unavailable", REASON_LOCATION_UNAVAILABLE, "permission")
                if current.get("expired"):
                    # 过期起点会规划出一条从"几小时前你所在的地方"出发的路线，
                    # 比不给路线更糟 —— 用户会照着一个错的起点出门。
                    return _failure("stale", REASON_LOCATION_STALE, "permission")
                # 浏览器位置是 WGS-84，高德 API 与地图链接均为 GCJ-02，
                # 不转换起点会偏移约 300-700 米。用户手给 / geocode 得到的坐标
                # 则默认 GCJ-02，不再转换。
                gcj_lng, gcj_lat = wgs84_to_gcj02(
                    float(current["longitude"]), float(current["latitude"])
                )
                origin = (gcj_lat, gcj_lng)

                # 当前位置这条路上，城市要先逆地理才有，而这可以和目的地解析
                # **并行** —— 起点就是用户当前坐标，城市只影响重名地名的偏置。
                # 串行等于白等一次网络往返，而这条路径实测正是导航里最慢的一段。
                city_task = asyncio.ensure_future(_soft_city(client, origin))
            parsed = _parse_coordinates(destination)
            if parsed is not None:
                # 目的地本身就是坐标 → 不需要解析，也就不需要城市偏置。
                target = parsed
            else:
                # 需要城市时才 await 逆地理；全国结果命中本地就完全不用等它。
                target = await _resolve_destination(client, destination, city, city_task=city_task)
            if city_task is not None and not city_task.done():
                # 目的地已经定局但逆地理还在跑（坐标目的地 / 解析提前返回）。
                # 不能把后台任务留在事件循环里。
                city_task.cancel()
                await asyncio.gather(city_task, return_exceptions=True)
            route = await client.route(origin, target, mode)
            map_mode = "car" if mode == "driving" else "walk"
            map_url = f"https://uri.amap.com/navigation?from={origin[1]},{origin[0]}&to={target[1]},{target[0]}&mode={map_mode}"
            # 坐标刻意**不出现在结果里**：它们在 map_url 里已经够了，
            # 而多留在 output 里，模型就会把它们复述进气泡。
            # 高德调用所需的坐标只活在工具内部。
            return {"status": "ok", "output": {**route, "map_url": map_url}}
        except ValueError:
            # 用户手贴的坐标超出经纬度范围 → 参数问题，不是服务问题。
            return _failure("failed", REASON_DESTINATION_INVALID, "permanent")
        except Exception as exc:
            reason = classify_navigation_error(exc, mode=mode)
            # 原文只进日志：这是看到异常类名 / HTTP 码 / API 片段的唯一地方。
            logger.warning("navigation failed (%s): %s: %s", reason, type(exc).__name__, exc)
            if reason == REASON_NAVIGATION_UNAVAILABLE:
                return _failure("failed", reason, classify_tool_error(exc))
            return _failure("failed", reason, "permanent")

    return ToolSpec(
        name="route",
        schema={
            "type": "function",
            "function": {
                "name": "route",
                "description": "Plan a driving or walking route. Origin defaults to the user's current browser location; pass origin only when the user named a starting point themselves.",
                "parameters": {"type": "object", "properties": {
                    "destination": {"type": "string", "maxLength": MAX_DESTINATION_LENGTH},
                    "origin": {"type": "string", "maxLength": MAX_DESTINATION_LENGTH},
                    "mode": {"type": "string", "enum": ["driving", "walking"], "default": "driving"}},
                    "required": ["destination"], "additionalProperties": False},
            },
        },
        handler=handler,
        risk="low",
        # effect=read：导航不改变任何状态，只是查一条路线。
        # 这一条不是装饰 —— 它是"明确的导航请求走确定性分岔"的硬条件
        # （deterministic_route_allowed 需要 effect=read 或 hard_routable，且 risk != "high"）。
        # 旧实现没标它，于是导航必须等模型挑一次工具才能跑。
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
        timeout_seconds=10.0,
    )
