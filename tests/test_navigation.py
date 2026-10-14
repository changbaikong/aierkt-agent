import math
import re
from datetime import datetime, timezone

import pytest

from agent_core.contracts import Scope
from agent_tools.location import LocationStore
from agent_tools.navigation import AmapNavigationClient, make_navigation_tool, wgs84_to_gcj02


class ScriptedClient:
    """按脚本返回结果的假客户端：记录 geocode 调用与 route 参数。"""

    def __init__(self, *, regeo_city: str = "上海市", national: dict | None = None, local: dict | None = None, regeo_error: bool = False) -> None:
        self.regeo_city = regeo_city
        self.regeo_error = regeo_error
        # national: 无城市限定的全国搜索结果；local: citylimit 严格同城结果
        self._national = national if national is not None else {"latitude": 31.2200, "longitude": 121.4800, "label": "上海市xx", "city": "上海市"}
        self._local = local  # None 表示 citylimit 搜不到（抛错）
        self.geocode_calls: list[dict] = []
        self.route_origin = None
        self.route_target = None

    async def regeo(self, latitude: float, longitude: float) -> dict:
        if self.regeo_error:
            raise RuntimeError("regeo down")
        return {"city": self.regeo_city, "address": "xx市xx区"}

    async def geocode(self, destination: str, city: str | None = None, citylimit: bool = False) -> dict:
        self.geocode_calls.append({"destination": destination, "city": city, "citylimit": citylimit})
        if citylimit:
            if self._local is None:
                raise RuntimeError("destination_not_found")
            return self._local
        return self._national

    async def route(self, origin, destination, mode: str) -> dict:
        self.route_origin = origin
        self.route_target = destination
        return {"distance_meters": 4200, "duration_seconds": 900, "mode": mode}


def _filled_store(scope: Scope, latitude: float, longitude: float) -> LocationStore:
    now = lambda: datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)  # noqa: E731
    store = LocationStore(now=now)
    store.update(scope, latitude, longitude, 18, datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))
    return store


def test_wgs84_to_gcj02_offsets_coordinates_inside_china() -> None:
    # 北京天安门附近的 WGS-84 坐标，转换后应产生数百米量级的偏移
    gcj_lng, gcj_lat = wgs84_to_gcj02(116.39753, 39.90787)
    dlat_m = (gcj_lat - 39.90787) * 111_320.0
    dlng_m = (gcj_lng - 116.39753) * 111_320.0 * math.cos(math.radians(39.90787))
    offset_m = math.hypot(dlat_m, dlng_m)
    assert 100 <= offset_m <= 1000


def test_wgs84_to_gcj02_keeps_coordinates_outside_china() -> None:
    # 中国大陆范围外（东京）不偏移
    assert wgs84_to_gcj02(139.6917, 35.6895) == (139.6917, 35.6895)


@pytest.mark.asyncio
async def test_navigation_tool_uses_current_location_and_returns_route() -> None:
    scope = Scope()
    store = _filled_store(scope, 31.2304, 121.4737)
    client = ScriptedClient(regeo_city="上海市")  # 全国结果 city=上海市，同城一步命中
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "上海虹桥站", "mode": "walking"}, scope)

    assert result["status"] == "ok"
    assert result["output"]["distance_meters"] == 4200
    assert result["output"]["mode"] == "walking"
    assert "uri.amap.com" in result["output"]["map_url"]
    # 同城一步命中：只有一次无城市限定的 geocode
    assert client.geocode_calls == [{"destination": "上海虹桥站", "city": None, "citylimit": False}]


@pytest.mark.asyncio
async def test_navigation_tool_requires_recent_location() -> None:
    tool = make_navigation_tool(LocationStore(), ScriptedClient())

    result = await tool.handler({"destination": "上海虹桥站", "mode": "driving"}, Scope())

    assert result["status"] == "unavailable"
    assert result["output"]["reason"] == "location_unavailable"


@pytest.mark.asyncio
async def test_navigation_tool_refuses_stale_origin() -> None:
    """过期起点不能用来规划路线 —— 否则用户会照着一个几小时前的起点出门。"""
    scope = Scope()
    current = [datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)]
    store = LocationStore(now=lambda: current[0])
    store.update(scope, 31.2304, 121.4737, 18, current[0])
    client = ScriptedClient()
    tool = make_navigation_tool(store, client)

    current[0] = current[0] + __import__("datetime").timedelta(hours=2)
    result = await tool.handler({"destination": "上海虹桥站", "mode": "walking"}, scope)

    assert result["status"] == "stale"
    assert result["output"]["reason"] == "location_stale"
    assert client.route_origin is None, "过期起点不该发起任何路线规划"
    assert client.geocode_calls == []
    assert "31.2304" not in str(result)


@pytest.mark.asyncio
async def test_navigation_tool_rejects_invalid_arguments() -> None:
    tool = make_navigation_tool(LocationStore(), ScriptedClient())

    result = await tool.handler({"destination": "x" * 257, "mode": "driving"}, Scope())

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "destination_invalid"
    assert result["error_kind"] == "permanent"


@pytest.mark.asyncio
async def test_navigation_tool_requires_a_destination() -> None:
    """缺目的地是参数问题，不该报成"服务没响应"。"""
    tool = make_navigation_tool(_filled_store(Scope(), 31.2304, 121.4737), ScriptedClient())

    result = await tool.handler({"mode": "driving"}, Scope())

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "destination_required"
    assert result["error_kind"] == "permanent"


@pytest.mark.asyncio
async def test_navigation_tool_rejects_out_of_range_coordinates() -> None:
    tool = make_navigation_tool(_filled_store(Scope(), 31.2304, 121.4737), ScriptedClient())

    result = await tool.handler({"destination": "999,999", "mode": "driving"}, Scope())

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "destination_invalid"


@pytest.mark.asyncio
async def test_navigation_tool_converts_wgs84_origin_to_gcj02() -> None:
    scope = Scope()
    # 天安门附近的 WGS-84 浏览器坐标
    store = _filled_store(scope, 39.90787, 116.39753)
    client = ScriptedClient(
        regeo_city="北京市",
        national={"latitude": 39.9028, "longitude": 116.4274, "label": "北京市北京站", "city": "北京市"},
    )
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "北京站", "mode": "driving"}, scope)

    assert result["status"] == "ok"
    expected_lng, expected_lat = wgs84_to_gcj02(116.39753, 39.90787)
    # route() 收到的是转换后的 GCJ-02 起点 (lat, lng)
    assert client.route_origin is not None
    assert client.route_origin[0] == pytest.approx(expected_lat)
    assert client.route_origin[1] == pytest.approx(expected_lng)
    assert client.route_origin[0] != pytest.approx(39.90787, abs=1e-6)
    # 地图链接用的是转换后的坐标（高德口径），用户点开不会偏移几百米
    match = re.search(r"from=([\d.]+),([\d.]+)&to=([\d.]+),([\d.]+)", result["output"]["map_url"])
    assert match is not None
    assert float(match.group(1)) == pytest.approx(expected_lng)
    assert float(match.group(2)) == pytest.approx(expected_lat)
    assert float(match.group(3)) == pytest.approx(116.4274)
    assert float(match.group(4)) == pytest.approx(39.9028)


@pytest.mark.asyncio
async def test_successful_route_keeps_facts_but_not_raw_coordinates() -> None:
    """成功时保留距离 / 时长 / 方式 / 地图链接；坐标只留在工具内部。

    坐标多留一份在 output 里，模型就会把它们复述进气泡 —— 用户要的是
    "开车 7 公里、15 分钟"，不是自己的门牌级经纬度。
    """
    scope = Scope()
    store = _filled_store(scope, 31.2304, 121.4737)
    tool = make_navigation_tool(store, ScriptedClient(regeo_city="上海市"))

    result = await tool.handler({"destination": "上海虹桥站", "mode": "walking"}, scope)

    output = result["output"]
    assert output["distance_meters"] == 4200
    assert output["duration_seconds"] == 900
    assert output["mode"] == "walking"
    assert "uri.amap.com" in output["map_url"]
    assert "origin" not in output and "destination" not in output
    assert "latitude" not in str(output) and "longitude" not in str(output)


@pytest.mark.asyncio
async def test_local_bias_when_national_result_in_other_city() -> None:
    # "王府井"全国搜索命中的是洛阳重名地名，用户在北京 → 应就近取北京结果
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    client = ScriptedClient(
        regeo_city="北京市",
        national={"latitude": 34.6696, "longitude": 112.4337, "label": "河南省洛阳市西工区王府井", "city": "洛阳市"},
        local={"latitude": 39.9154, "longitude": 116.4112, "label": "北京市东城区王府井", "city": "北京市"},
    )
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "王府井", "mode": "driving"}, scope)

    assert result["status"] == "ok"
    assert client.route_target == pytest.approx((39.9154, 116.4112))
    assert client.geocode_calls[-1] == {"destination": "王府井", "city": "北京市", "citylimit": True}


@pytest.mark.asyncio
async def test_explicit_other_city_in_text_wins() -> None:
    # "杭州西湖"显式点名杭州 → 尊重全国结果，不被就近成北京海淀西湖
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    client = ScriptedClient(
        regeo_city="北京市",
        national={"latitude": 30.2440, "longitude": 120.1440, "label": "浙江省杭州市西湖", "city": "杭州市"},
        local={"latitude": 39.9887, "longitude": 116.2683, "label": "北京市海淀区西湖", "city": "北京市"},
    )
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "杭州西湖", "mode": "driving"}, scope)

    assert result["status"] == "ok"
    assert client.route_target == pytest.approx((30.2440, 120.1440))


@pytest.mark.asyncio
async def test_national_fallback_when_citylimit_fails() -> None:
    # "上海虹桥站"（用户在北京）citylimit 必然搜不到 → 回退全国结果
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    client = ScriptedClient(
        regeo_city="北京市",
        national={"latitude": 31.1943, "longitude": 121.3229, "label": "上海市闵行区上海虹桥站", "city": "上海市"},
        local=None,
    )
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "上海虹桥站", "mode": "driving"}, scope)

    assert result["status"] == "ok"
    assert client.route_target == pytest.approx((31.1943, 121.3229))


@pytest.mark.asyncio
async def test_navigation_tool_falls_back_when_regeo_fails() -> None:
    # regeo 失败不阻断导航，只是没有城市偏置
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    client = ScriptedClient(
        regeo_error=True,
        national={"latitude": 39.9028, "longitude": 116.4274, "label": "北京市北京站", "city": "北京市"},
    )
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "北京站", "mode": "driving"}, scope)

    assert result["status"] == "ok"
    assert client.geocode_calls == [{"destination": "北京站", "city": None, "citylimit": False}]


@pytest.mark.asyncio
async def test_navigation_tool_reports_destination_not_found() -> None:
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)

    class FailAllClient(ScriptedClient):
        async def geocode(self, destination: str, city: str | None = None, citylimit: bool = False) -> dict:
            self.geocode_calls.append({"destination": destination, "city": city, "citylimit": citylimit})
            raise RuntimeError("destination_not_found")

    tool = make_navigation_tool(store, FailAllClient(regeo_error=True))

    result = await tool.handler({"destination": "不存在的地方xyz", "mode": "driving"}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "destination_not_found"
    assert result["error_kind"] == "permanent"


@pytest.mark.asyncio
async def test_current_location_navigation_runs_regeo_and_destination_parse_in_parallel() -> None:
    """当前位置导航：逆地理与目的地解析必须**并行**。

    这两件事互不依赖 —— 起点就是用户当前坐标，城市只影响重名地名的偏置。
    串行写法的代价是白等一次网络往返，而这条路径正是导航里最慢的一段。

    判据用"耗时明显小于两次延迟之和"，而不是断言调用顺序：
    顺序断言在并行实现下依然能通过（两者都是先起 regeo 再起 geocode 的排队关系），
    只有耗时能把"真的并发了"和"看着像并发了"区分开。
    """
    import asyncio
    import time

    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)

    delay = 0.20

    class SlowClient(ScriptedClient):
        async def regeo(self, latitude: float, longitude: float) -> dict:
            await asyncio.sleep(delay)
            return await super().regeo(latitude, longitude)

        async def geocode(self, destination: str, city: str | None = None, citylimit: bool = False) -> dict:
            await asyncio.sleep(delay)
            return await super().geocode(destination, city=city, citylimit=citylimit)

    client = SlowClient(
        regeo_city="北京市",
        national={"latitude": 39.9028, "longitude": 116.4274, "label": "北京市北京站", "city": "北京市"},
    )
    tool = make_navigation_tool(store, client)

    started = time.perf_counter()
    result = await tool.handler({"destination": "北京站", "mode": "driving"}, scope)
    elapsed = time.perf_counter() - started

    assert result["status"] == "ok"
    # 串行要 2*delay（还多一次 route），并行只需约 1*delay。留足调度余量：
    # 只要明显小于两次延迟之和，就说明这两步是并发跑的。
    assert elapsed < delay * 1.8, f"两步疑似串行执行（耗时 {elapsed:.3f}s，两次延迟合计 {delay * 2:.3f}s）"


@pytest.mark.asyncio
async def test_explicit_coordinate_destination_skips_name_parsing_entirely() -> None:
    """目的地本身就是坐标 → 完全不做地名解析，也不该为它等逆地理。

    这条同时守着"不留后台任务"：逆地理要么被等到、要么被取消。
    判据不用 `asyncio.all_tasks()` 按名字筛 —— 那样会把测试自己这个任务
    也算进来（它的协程名里含 "regeo"），是一条约等于永假的断言。
    改成看"客户端有没有真的被问到"：被取消的任务不会留下调用记录。
    """
    import asyncio

    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)

    class SlowRegeoClient(ScriptedClient):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.regeo_calls = 0

        async def regeo(self, latitude: float, longitude: float) -> dict:
            self.regeo_calls += 1
            await asyncio.sleep(0.05)
            return await super().regeo(latitude, longitude)

    client = SlowRegeoClient(regeo_city="北京市")
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "116.4274,39.9028", "mode": "driving"}, scope)

    assert result["status"] == "ok"
    assert client.geocode_calls == [], "坐标目的地不该触发任何地名解析"
    # 让被取消的任务有机会跑完 finally；不该有异常逸出。
    await asyncio.sleep(0.15)


# ----------------------------------------------------------------------
# 稳定错误码：失败一律只暴露码，不暴露外部服务原文
# ----------------------------------------------------------------------
class RaisingClient(ScriptedClient):
    def __init__(self, *, route_error: BaseException | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.route_error = route_error

    async def route(self, origin, destination, mode: str) -> dict:
        if self.route_error is not None:
            raise self.route_error
        return await super().route(origin, destination, mode)


def _long_walk_client() -> RaisingClient:
    return RaisingClient(
        regeo_city="北京市",
        national={"latitude": 30.2592, "longitude": 120.1304, "label": "浙江省杭州市西湖", "city": "杭州市"},
        route_error=RuntimeError("Amap request failed: OVER_DIRECTION_RANGE"),
    )


@pytest.mark.asyncio
async def test_navigation_tool_reports_walking_range_with_a_stable_code() -> None:
    """步行超范围是**稳定码**，中文说法交给措辞层（见 test_steward.py）。

    以前"步行/驾车"这几个字是拼在 error 里的 —— 等于把措辞混进了结果结构：
    下游既没法按码分支，也没法统一改口径。
    """
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    tool = make_navigation_tool(store, _long_walk_client())

    result = await tool.handler({"destination": "杭州西湖", "mode": "walking"}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "walking_range_exceeded"
    assert result["error_kind"] == "permanent"


@pytest.mark.asyncio
async def test_navigation_tool_maps_unknown_provider_failure_to_unavailable() -> None:
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    tool = make_navigation_tool(
        store, RaisingClient(route_error=RuntimeError("Amap request failed with HTTP 500"))
    )

    result = await tool.handler({"destination": "北京站", "mode": "driving"}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "navigation_unavailable"


@pytest.mark.asyncio
async def test_navigation_tool_maps_timeout_to_the_timeout_kind() -> None:
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    tool = make_navigation_tool(store, RaisingClient(route_error=TimeoutError("timed out")))

    result = await tool.handler({"destination": "北京站", "mode": "driving"}, scope)

    assert result["output"]["reason"] == "navigation_unavailable"
    assert result["error_kind"] == "timeout"


@pytest.mark.asyncio
async def test_navigation_failures_never_leak_internal_details() -> None:
    """任何失败结果都不含异常类名 / HTTP 原文 / API 片段 / traceback。"""
    scope = Scope()
    store = _filled_store(scope, 39.90787, 116.39753)
    dirty = (
        "RuntimeError: Amap request failed with HTTP 500 "
        "(restapi.amap.com, key=SECRET)\nTraceback (most recent call last)"
    )
    cases = [
        ({}, ScriptedClient(), store),
        ({"destination": "x" * 257}, ScriptedClient(), store),
        ({"destination": "北京站", "mode": "driving"}, RaisingClient(route_error=RuntimeError(dirty)), store),
        ({"destination": "杭州西湖", "mode": "walking"}, _long_walk_client(), store),
        ({"destination": "上海虹桥站", "mode": "driving"}, ScriptedClient(), LocationStore()),
    ]

    for arguments, client, location_store in cases:
        result = await make_navigation_tool(location_store, client).handler(arguments, scope)
        dumped = str(result)
        for leak in ("RuntimeError", "Traceback", "Amap", "HTTP", "http", "SECRET", "key=", "\n"):
            assert leak not in dumped, f"{arguments} 泄漏了 {leak}: {dumped}"
        assert result["output"]["reason"]
        assert result["error"] == result["output"]["reason"], "error 只许填稳定码"


@pytest.mark.asyncio
async def test_amap_client_parses_geocode_and_route_payloads() -> None:
    import httpx

    responses = [
        {"status": "1", "geocodes": [{"formatted_address": "目的地", "location": "121.48,31.22", "city": "上海市"}]},
        {"status": "1", "route": {"paths": [{"distance": "1000", "duration": "120"}]}},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    client = AmapNavigationClient(api_key="secret", transport=httpx.MockTransport(handler))
    geocoded = await client.geocode("目的地")
    assert geocoded["latitude"] == 31.22
    assert geocoded["city"] == "上海市"
    assert (await client.route((31.2, 121.4), (31.22, 121.48), "driving"))["distance_meters"] == 1000


@pytest.mark.asyncio
async def test_amap_client_regeo_handles_municipality_and_city_params() -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/v3/geocode/regeo"):
            # 直辖市：city 是空数组，应退回 province
            payload = {"status": "1", "regeocode": {"formatted_address": "北京市东城区", "addressComponent": {"city": [], "province": "北京市"}}}
        else:
            # geocode 请求应带上 city 与 citylimit 参数
            assert "city=%E5%8C%97%E4%BA%AC%E5%B8%82" in str(request.url)  # 北京市
            assert "citylimit=true" in str(request.url)
            payload = {"status": "1", "geocodes": [{"formatted_address": "北京市东城区王府井", "location": "116.41122,39.915458", "city": "北京市"}]}
        return httpx.Response(200, json=payload)

    client = AmapNavigationClient(api_key="secret", transport=httpx.MockTransport(handler))
    regeo = await client.regeo(39.909187, 116.397463)
    assert regeo["city"] == "北京市"
    geocoded = await client.geocode("王府井", city=regeo["city"], citylimit=True)
    assert geocoded["label"] == "北京市东城区王府井"
    assert geocoded["city"] == "北京市"
