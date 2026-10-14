"""坐标 → 行政区的解析器：独立超时与短时缓存。"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from agent_core.contracts import Scope
from agent_tools.location import LocationStore, make_location_tool
from agent_tools.navigation import AmapPlaceResolver


class _CountingClient:
    """只记账不联网的假 client，用来数"到底打了几次逆地理"。"""

    def __init__(self, *, delay: float = 0.0, city: str = "上海市") -> None:
        self.calls = 0
        self.delay = delay
        self.city = city

    async def regeo(self, latitude: float, longitude: float) -> dict:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {
            "province": "上海市", "city": self.city, "district": "浦东新区",
            "township": "陆家嘴街道", "area_code": "310115",
            "address": "上海市浦东新区陆家嘴街道",
        }


@pytest.mark.asyncio
async def test_resolver_returns_place_and_caches_within_the_same_grid() -> None:
    client = _CountingClient()
    resolver = AmapPlaceResolver(client, timeout_seconds=1.0)

    first = await resolver({"latitude": 31.2304, "longitude": 121.4737})
    second = await resolver({"latitude": 31.23042, "longitude": 121.47372})

    assert first["city"] == "上海市"
    assert second == first
    assert client.calls == 1, "同一网格内的第二次解析必须走缓存，不打外部接口"


@pytest.mark.asyncio
async def test_resolver_re_resolves_after_cache_expiry() -> None:
    now = [datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)]
    client = _CountingClient()
    resolver = AmapPlaceResolver(
        client, timeout_seconds=1.0, cache_ttl_seconds=600, now=lambda: now[0]
    )

    await resolver({"latitude": 31.2304, "longitude": 121.4737})
    now[0] = now[0] + timedelta(seconds=601)
    await resolver({"latitude": 31.2304, "longitude": 121.4737})

    assert client.calls == 2


@pytest.mark.asyncio
async def test_resolver_uses_a_different_grid_for_a_far_away_point() -> None:
    client = _CountingClient()
    resolver = AmapPlaceResolver(client, timeout_seconds=1.0)

    await resolver({"latitude": 31.2304, "longitude": 121.4737})
    await resolver({"latitude": 39.9042, "longitude": 116.4074})

    assert client.calls == 2, "跨城市不能被缓存吞掉"


@pytest.mark.asyncio
async def test_resolver_times_out_on_its_own_budget() -> None:
    """逆地理有独立超时：不能把整轮预算拖在一条外部调用上。"""
    client = _CountingClient(delay=0.5)
    resolver = AmapPlaceResolver(client, timeout_seconds=0.05)

    with pytest.raises(asyncio.TimeoutError):
        await resolver({"latitude": 31.2304, "longitude": 121.4737})


@pytest.mark.asyncio
async def test_location_tool_maps_resolver_timeout_to_a_stable_reason() -> None:
    """超时到用户那儿要变成一句可操作的话，不是异常原文。"""
    client = _CountingClient(delay=0.5)
    resolver = AmapPlaceResolver(client, timeout_seconds=0.05)
    store = LocationStore()
    scope = Scope()
    store.update(scope, 31.2304, 121.4737, 18.5, datetime.now(timezone.utc))

    result = await make_location_tool(store, resolver).handler({}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "location_resolution_unavailable"
    assert result["error_kind"] == "transient", "超时属于临时故障，允许按重试规则处理"
    assert "Timeout" not in str(result)
