"""Amap weather service and safe weather tool.

天气接受三种输入（方案第三部分），三条路最后都收敛到"6 位行政区代码"再查：

* **6 位行政区代码** —— 直接用；
* **城市或区县名称**（"北京"、"浦东新区"）—— 先过地理编码换到行政区代码；
* **"当前位置"** —— 从位置快照解析出行政区。

为什么"只说天气"默认用当前位置、而不是先追问城市：追问一次要几秒钟，
而多数情况下用户要的就是他所在的那个城市。真猜错了，用户补一句城市名的成本
远低于每次都要先报一次城市。

这个模块刻意**不 import** 位置与导航模块：它只依赖两个注入进来的东西
（一个有 ``get(scope)`` 的位置存储、一个可调用的坐标解析器）。天气是天气，
位置怎么存的属于别人的事。
"""

from __future__ import annotations

import inspect
import logging
import time
from copy import deepcopy
from typing import Any, Callable

import httpx

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_AMAP_BASE_URL = "https://restapi.amap.com"

#: 表示"用当前位置"的说法。用户说"天气"而没给城市时，上游会传"当前位置"进来。
_CURRENT = {"当前位置", "当前所在位置", "这里", "我这里", "本地", "这边"}

#: 用户整句被塞进地名里时会带上的连接词。命中说明**参数取错了**，
#: 跟"这个城市我不认识"是两回事：前者要重新取参，后者要用户换个说法。
_COMPOUND_TOKENS = ("后查询", "然后", "接着", "顺便", "再查", "帮我查")

#: 地名里跟地名无关的尾巴。剥掉它们，"上海天气"就能当成"上海"查 ——
#: 那是最自然的说法，不该因为多了两个字就报"认不出这个地名"。
_AREA_NOISE = ("天气", "怎么样", "如何", "怎样", "查询", "查一下", "查查", "的")


class _WeatherAreaError(Exception):
    """地点解析失败。带上"对外该说什么"的三件套，异常原文只进日志。"""

    def __init__(self, status: str, reason: str, error_kind: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.error_kind = error_kind


def _failure(status: str, reason: str, error_kind: str) -> dict[str, Any]:
    """失败结果的统一形状。``error`` 填的也是稳定码，不是异常原文 ——
    这份结果会进模型的 observation，再被复述进气泡。原文只走 logger。
    """
    return {"status": status, "output": {"reason": reason}, "error": reason, "error_kind": error_kind}


def normalize_area(area: str) -> str:
    """剥掉地名里的噪声词与空格。返回空串表示"没给出可用地名"。"""
    text = str(area or "").strip()
    for token in _AREA_NOISE:
        text = text.replace(token, "")
    return text.strip()


#: 地名 → 行政区代码的缓存时长。这个映射几乎不变，"上海"永远是 310000，
#: 所以可以缓存得久一点。
AREA_CACHE_TTL_SECONDS = 3600.0

#: 天气结果缓存时长。实况几分钟就会变，缓存必须短 ——
#: 用"省一次请求"换"用户看到一小时前的温度"是亏的。
WEATHER_CACHE_TTL_SECONDS = 300.0


class _TtlCache:
    """带过期时间与容量上限的极简缓存。

    只被事件循环线程读写（调用方都是 async 且读写在 await 之外），
    所以不需要锁。容量满了就整体清空重来 —— 条目数本来就少，
    做 LRU 的复杂度换不来任何东西。
    """

    def __init__(self, *, ttl_seconds: float, max_entries: int, now: Any = None) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._now = now or (lambda: time.monotonic())
        self._entries: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self._now() >= expires_at:
            self._entries.pop(key, None)
            return None
        return deepcopy(value)

    def set(self, key: str, value: Any) -> None:
        if len(self._entries) >= self.max_entries:
            self._entries.clear()
        self._entries[key] = (self._now() + self.ttl_seconds, deepcopy(value))


class AmapWeatherClient:
    def __init__(self, *, api_key: str, base_url: str = DEFAULT_AMAP_BASE_URL,
                 timeout: float = 5.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not api_key.strip():
            raise ValueError("api_key is required")
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport
        # 地名 → 行政区代码的短缓存。同一句话里连着问两个城市、
        # 或用户隔几句再问一次同一个城市，都不该再打一次地理编码。
        # 地名和行政区代码的对应关系基本不变，缓存久一点没有风险。
        self._area_cache = _TtlCache(ttl_seconds=AREA_CACHE_TTL_SECONDS, max_entries=64)
        # 天气结果缓存。实况几分钟内就会变，TTL 必须短 ——
        # 用缓存换掉"用户看到一小时前的温度"是亏的。
        self._weather_cache = _TtlCache(ttl_seconds=WEATHER_CACHE_TTL_SECONDS, max_entries=32)

    async def _get(self, path: str, *, failure_reason: str = "weather_unavailable", **params: Any) -> dict[str, Any]:
        params = {"key": self.api_key, "output": "json", **params}
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False, transport=self.transport) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or str(payload.get("status")) != "1":
            # 失败码由调用方指定：地理编码失败要说"认不出这个地名"，
            # 而不是笼统的"天气服务不可用"—— 两者对用户的下一步动作不同。
            raise RuntimeError(failure_reason)
        return payload

    async def resolve_area(self, name: str) -> str:
        """城市/区县名 → 6 位行政区代码。

        高德的天气接口只吃行政区代码；给它一个中文城市名能不能work属于未文档化行为，
        不能依赖。所以这里显式换一次 —— 多一次调用，换一个确定的结果。

        带短缓存：这个映射是稳定的，"上海"永远是 310000。重复问同一个地名
        却每次重打一次地理编码，是纯粹的浪费。
        """
        key = normalize_area(name)
        cached = self._area_cache.get(key)
        if cached is not None:
            return cached
        payload = await self._get(
            "/v3/geocode/geo", failure_reason="weather_area_unknown", address=name
        )
        geocodes = payload.get("geocodes") or []
        if not geocodes or not isinstance(geocodes[0], dict):
            raise LookupError("weather_area_unknown")
        code = str(geocodes[0].get("adcode") or "").strip()
        if not code.isdigit() or len(code) != 6:
            raise LookupError("weather_area_unknown")
        self._area_cache.set(key, code)
        return code

    async def query(self, area_code: str, kind: str = "live") -> dict[str, Any]:
        if not str(area_code).isdigit() or len(str(area_code)) != 6:
            raise ValueError("invalid_weather_area")
        if kind not in {"live", "forecast"}:
            raise ValueError("invalid_weather_kind")
        cache_key = f"{area_code}:{kind}"
        cached = self._weather_cache.get(cache_key)
        if cached is not None:
            return cached
        payload = await self._get(
            "/v3/weather/weatherInfo",
            city=str(area_code),
            extensions="base" if kind == "live" else "all",
        )
        if kind == "live":
            item = (payload.get("lives") or [{}])[0]
            result = {
                "kind": "live", "area_code": str(item.get("adcode") or area_code),
                "province": str(item.get("province") or ""), "city": str(item.get("city") or ""),
                "weather": str(item.get("weather") or ""),
                "temperature_c": _number(item.get("temperature")),
                "wind_direction": str(item.get("winddirection") or ""),
                "wind_power": str(item.get("windpower") or ""),
                "humidity": str(item.get("humidity") or ""),
                "report_time": str(item.get("reporttime") or ""),
            }
        else:
            item = (payload.get("forecasts") or [{}])[0]
            result = {"kind": "forecast", "area_code": str(item.get("adcode") or area_code),
                      "province": str(item.get("province") or ""), "city": str(item.get("city") or ""),
                      "report_time": str(item.get("reporttime") or ""),
                      "casts": list(item.get("casts") or [])[:4]}
        self._weather_cache.set(cache_key, result)
        return result


def _number(value: Any) -> int | float | None:
    try:
        number = float(value)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return None


async def _resolve_area_code(
    area: str,
    scope: Scope,
    client: AmapWeatherClient,
    location_store: Any,
    place_resolver: Callable[[dict], Any] | None,
) -> str:
    """三种输入 → 6 位行政区代码。失败抛 ``_WeatherAreaError``。"""
    normalized = normalize_area(area)

    if not normalized or normalized in _CURRENT:
        # "当前位置"（或者用户只说了"天气"）。不追问城市，直接用他所在的地方。
        if location_store is None or place_resolver is None:
            raise _WeatherAreaError("failed", "location_resolution_unavailable", "permission")
        snapshot = location_store.get(scope)
        if snapshot is None:
            # 从没开过共享 —— 用户要先去开，或者直接说城市名。
            raise _WeatherAreaError("unavailable", "location_unavailable", "permission")
        if snapshot.get("expired"):
            # 开过但超出有效期 —— 重新共享一次就行。这两件事对用户是不同的动作。
            raise _WeatherAreaError("stale", "location_stale", "permission")
        try:
            place = place_resolver(snapshot)
            if inspect.isawaitable(place):
                place = await place
            place = dict(place or {})
        except Exception as exc:
            # 原文只进日志：里面全是高德的 HTTP 码与 API 片段。
            logger.warning("weather location resolution failed: %s: %s", type(exc).__name__, exc)
            raise _WeatherAreaError("failed", "location_resolution_unavailable", "transient") from exc
        code = str(place.get("area_code") or "").strip()
        if not code.isdigit() or len(code) != 6:
            # 定位到了、但这个点落在没有行政区代码的地方。
            raise _WeatherAreaError("failed", "weather_area_unknown", "permanent")
        return code

    if any(token in normalized for token in _COMPOUND_TOKENS):
        # 用户的整句话被当成地名传进来了。这是取参错误，不是"没这个城市"。
        raise _WeatherAreaError("rejected", "invalid_weather_area", "permanent")

    if normalized.isdigit() and len(normalized) == 6:
        return normalized

    try:
        return await client.resolve_area(normalized)
    except LookupError:
        raise _WeatherAreaError("failed", "weather_area_unknown", "permanent")
    except Exception as exc:
        logger.warning("weather area resolution failed: %s: %s", type(exc).__name__, exc)
        raise _WeatherAreaError("failed", "weather_unavailable", "transient") from exc


def make_weather_tool(
    client: AmapWeatherClient,
    *,
    location_store: Any = None,
    place_resolver: Callable[[dict], Any] | None = None,
) -> ToolSpec:
    """天气工具。

    ``location_store`` 与 ``place_resolver`` 都可以缺省：缺了之后"当前位置"
    这条输入会返回 ``location_resolution_unavailable``（一句能对用户明说的话），
    而不是整个天气功能消失。显式给城市名的查询完全不依赖这两者。

    ``deterministic_route_allowed=True``：明确的天气请求该由规则直接接住，
    不该先让模型挑一次工具 —— 那一趟往返就是几百毫秒，且模型可能挑到网页搜索上去。
    """

    async def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        area = str(arguments.get("area") or "").strip()
        kind = str(arguments.get("kind") or "live")
        if kind not in {"live", "forecast"}:
            return _failure("rejected", "invalid_weather_kind", "permanent")
        try:
            code = await _resolve_area_code(area, scope, client, location_store, place_resolver)
        except _WeatherAreaError as exc:
            return _failure(exc.status, exc.reason, exc.error_kind)
        try:
            return {"status": "ok", "output": await client.query(code, kind)}
        except ValueError as exc:
            # 自家参数校验（代码不是 6 位 / kind 不认识）。
            return _failure("rejected", str(exc), "permanent")
        except Exception as exc:
            logger.warning("weather query failed: %s: %s", type(exc).__name__, exc)
            return _failure("failed", "weather_unavailable", "transient")

    return ToolSpec(
        name="query", risk="low", effect=EFFECT_READ, deterministic_route_allowed=True,
        # 最坏情况是"地理编码 + 天气"两次外部调用，各自有 5 秒的 httpx 超时。
        # 10 秒是外层保险：正常路径（缓存命中或直接给代码）远低于它。
        timeout_seconds=10.0, handler=handler,
        schema={"type": "function", "function": {"name": "query",
            "description": "Query Amap live weather or forecast. Accepts a 6-digit administrative area code, a city/district name, or '当前位置' for the user's current place.",
            "parameters": {"type": "object", "properties": {
                "area": {"type": "string"}, "kind": {"type": "string", "enum": ["live", "forecast"], "default": "live"}},
                "required": ["area"], "additionalProperties": False}}},
    )
