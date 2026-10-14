"""天气链路：三种输入 + 五种固定失败 + 不泄漏。

改造前这里断言的是"「当前位置」要被拒绝"（`current_location_must_be_resolved`）。
那个前提已经被推翻 —— 只说一声"天气"就该用当前位置，不再先追问城市。
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agent_core.contracts import Scope
from agent_core.loop import AgentLoop
from agent_tools.location import LocationStore, make_location_tool
from agent_tools.weather import AmapWeatherClient, make_weather_tool, normalize_area

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_location_tool_returns_place_without_coordinates():
    # 时钟必须注入：用真实时钟的话，写死的 _NOW 一过 TTL 就变成 stale，
    # 这条断言会在某个时间点自己开始失败。
    store = LocationStore(ttl_seconds=300, now=lambda: _NOW)
    store.update(Scope(), 31.2304, 121.4737, 80, _NOW)
    tool = make_location_tool(store, resolver=lambda snapshot: {
        "province": "上海市", "city": "上海市", "district": "浦东新区",
        "township": "陆家嘴街道", "area_code": "310115",
    })
    result = await tool.handler({}, Scope())
    assert result["status"] == "ok"
    assert result["output"]["place_label"] == "上海市浦东新区陆家嘴街道"
    assert result["output"]["precision"] == "street"
    assert "latitude" not in result["output"]
    assert "longitude" not in result["output"]


# ── 天气：假 client ────────────────────────────────────────────────────
def _live_payload(*, city: str = "上海市", weather: str = "晴", temperature: str = "28") -> dict:
    return {
        "kind": "live", "area_code": "310115", "province": "上海市", "city": city,
        "weather": weather, "temperature_c": int(temperature),
        "wind_direction": "东风", "wind_power": "3", "humidity": "55",
        "report_time": "2026-09-20 12:00:00",
    }


class _StubClient:
    """只记账不联网。记下"查了哪个区码""解析了哪个地名"，用来断言走了哪条路。"""

    def __init__(self, *, payload=None, area_code="310115", area_error=None, query_error=None):
        self.payload = payload or _live_payload()
        self.area_code = area_code
        self.area_error = area_error
        self.query_error = query_error
        self.queried: list[str] = []
        self.resolved: list[str] = []

    async def query(self, area_code, kind="live"):
        self.queried.append(str(area_code))
        if self.query_error:
            raise self.query_error
        return dict(self.payload)

    async def resolve_area(self, name):
        self.resolved.append(str(name))
        if self.area_error:
            raise self.area_error
        return self.area_code


def _store(*, age_seconds: int | None = 0):
    """一个装了位置快照的存储。``age_seconds=None`` 表示从没上报过。"""
    clock = [_NOW]
    store = LocationStore(ttl_seconds=300, now=lambda: clock[0])
    scope = Scope()
    if age_seconds is not None:
        store.update(scope, 31.2304, 121.4737, 18.5, _NOW - timedelta(seconds=age_seconds))
    return store, scope


def _current_place(snapshot: dict) -> dict:
    return {"city": "上海市", "province": "上海市", "area_code": "310115"}


# ── 三种输入 ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_bare_weather_uses_current_location_without_asking_for_a_city():
    """只说"天气"就用当前位置 —— 不追问城市，这是本轮要修的那条。"""
    store, scope = _store()
    client = _StubClient()
    tool = make_weather_tool(client, location_store=store, place_resolver=_current_place)

    result = await tool.handler({"area": "当前位置"}, scope)

    assert result["status"] == "ok"
    assert client.queried == ["310115"]
    assert result["output"]["city"] == "上海市"


@pytest.mark.asyncio
async def test_bare_weather_word_also_means_current_location():
    """用户只敲了"天气"两个字 —— 上游把地名槽位留空或填了噪声词时的兜底。"""
    store, scope = _store()
    client = _StubClient()
    tool = make_weather_tool(client, location_store=store, place_resolver=_current_place)

    for area in ("天气", "怎么样", ""):
        result = await tool.handler({"area": area}, scope)
        assert result["status"] == "ok", f"area={area!r} 应当落到当前位置"


@pytest.mark.asyncio
async def test_named_city_is_resolved_and_location_is_not_read():
    """点名城市时直接解析那个城市，绝不用当前位置覆盖它。"""
    store, scope = _store()
    client = _StubClient(area_code="110101", payload=_live_payload(city="北京市"))
    tool = make_weather_tool(client, location_store=store, place_resolver=_current_place)

    result = await tool.handler({"area": "北京天气"}, scope)

    assert result["status"] == "ok"
    assert client.resolved == ["北京"], "「北京天气」里的噪声词要剥掉，剩下地名"
    assert client.queried == ["110101"], "点名了城市就该查那个城市，不能被当前位置顶掉"


@pytest.mark.asyncio
async def test_six_digit_area_code_is_used_directly():
    client = _StubClient()
    tool = make_weather_tool(client)

    result = await tool.handler({"area": "310115"}, Scope())

    assert result["status"] == "ok"
    assert client.queried == ["310115"]
    assert client.resolved == [], "六位代码不需要再过地理编码"


def test_normalize_area_strips_weather_words_only():
    assert normalize_area("北京天气") == "北京"
    assert normalize_area(" 浦东新区 ") == "浦东新区"
    assert normalize_area("天气") == ""
    assert normalize_area("怎么样") == ""


# ── 五种固定失败 ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_location_asks_to_share_or_name_a_city():
    client = _StubClient()
    tool = make_weather_tool(client, location_store=LocationStore(), place_resolver=_current_place)

    result = await tool.handler({"area": "当前位置"}, Scope())

    assert result["status"] == "unavailable"
    assert result["output"]["reason"] == "location_unavailable"
    assert client.queried == [], "没位置就不该去打天气接口"


@pytest.mark.asyncio
async def test_expired_location_asks_to_reshare():
    store, scope = _store(age_seconds=900)
    tool = make_weather_tool(_StubClient(), location_store=store, place_resolver=_current_place)

    result = await tool.handler({"area": "当前位置"}, scope)

    assert result["status"] == "stale"
    assert result["output"]["reason"] == "location_stale"


@pytest.mark.asyncio
async def test_missing_resolver_is_reported_as_its_own_reason():
    """有能力缺口时说"定位服务没接上"，不说"你没开共享" —— 后者会误导用户去点开关。"""
    store, scope = _store()
    tool = make_weather_tool(_StubClient(), location_store=store, place_resolver=None)

    result = await tool.handler({"area": "当前位置"}, scope)

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "location_resolution_unavailable"


@pytest.mark.asyncio
async def test_unknown_city_name_is_permanent():
    client = _StubClient(area_error=LookupError("weather_area_unknown"))
    tool = make_weather_tool(client)

    result = await tool.handler({"area": "那个什么地方"}, Scope())

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "weather_area_unknown"
    assert result["error_kind"] == "permanent", "地名认不出属于参数问题，不该被重试"


@pytest.mark.asyncio
async def test_weather_service_failure_is_transient():
    client = _StubClient(query_error=RuntimeError("Amap request failed with HTTP 500"))
    tool = make_weather_tool(client)

    result = await tool.handler({"area": "310115"}, Scope())

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "weather_unavailable"
    assert result["error_kind"] == "transient", "服务没响应属于临时故障，可以按规则重试一次"


@pytest.mark.asyncio
async def test_whole_sentence_used_as_area_is_rejected():
    """用户的整句话被当成地名传进来 —— 取参错误，明确拒绝而不是当成"没这个城市"。"""
    tool = make_weather_tool(_StubClient())

    result = await tool.handler({"area": "查一下天气然后接着看"}, Scope())

    assert result["status"] == "rejected"
    assert result["output"]["reason"] == "invalid_weather_area"


# ── 不泄漏 ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_weather_never_leaks_third_party_text_or_coordinates():
    client = _StubClient(query_error=RuntimeError("Amap request failed with HTTP 500 at lat 31.2304"))
    tool = make_weather_tool(client)

    result = await tool.handler({"area": "310115"}, Scope())
    text = str(result)

    assert "HTTP" not in text
    assert "Amap" not in text
    assert "31.2304" not in text
    assert result["error"] == "weather_unavailable"


# ── 语义闸门（loop 层）────────────────────────────────────────────────
def test_semantic_gate_only_blocks_misparsed_arguments():
    """闸门只挡"整句被当地名"，不再按"必须是 6 位数字"卡 —— 那会误伤城市名与当前位置。"""
    assert AgentLoop._semantic_tool_reason("weather__query", {"area": "后查询当前位置"}) == "compound_text_as_area"
    assert AgentLoop._semantic_tool_reason("weather__query", {"area": "当前位置"}) == ""
    assert AgentLoop._semantic_tool_reason("weather__query", {"area": "北京"}) == ""
    assert AgentLoop._semantic_tool_reason("weather__query", {"area": "310115"}) == ""
    assert AgentLoop._semantic_tool_reason("weather__query", {"area": ""}) == "invalid_weather_area"


# ── 真实 client 的解析（MockTransport）────────────────────────────────
@pytest.mark.asyncio
async def test_weather_client_normalizes_live_response():
    def handler(request):
        assert request.url.path == "/v3/weather/weatherInfo"
        assert request.url.params["city"] == "310115"
        assert request.url.params["extensions"] == "base"
        return httpx.Response(200, json={
            "status": "1", "lives": [{
                "province": "上海市", "city": "上海市", "adcode": "310115",
                "weather": "晴", "temperature": "28", "winddirection": "东风",
                "windpower": "3", "humidity": "55", "reporttime": "2026-09-20 12:00:00",
            }],
        })

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))
    result = await client.query("310115", "live")
    assert result["area_code"] == "310115"
    assert result["weather"] == "晴"
    assert result["temperature_c"] == 28


@pytest.mark.asyncio
async def test_weather_client_resolves_city_name_to_area_code():
    def handler(request):
        assert request.url.path == "/v3/geocode/geo"
        assert request.url.params["address"] == "北京"
        return httpx.Response(200, json={
            "status": "1",
            "geocodes": [{"adcode": "110101", "city": "北京市", "province": "北京市"}],
        })

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))
    assert await client.resolve_area("北京") == "110101"


@pytest.mark.asyncio
async def test_weather_client_treats_empty_geocode_as_unknown_area():
    def handler(request):
        return httpx.Response(200, json={"status": "1", "geocodes": []})

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))
    with pytest.raises(LookupError):
        await client.resolve_area("那个什么地方")


# ── 短缓存：命中就不该再打一次接口 ──────────────────────────────────
# 方案任务 6。缓存只按"地点 / 规范化地名"区分，带去重与过期。


@pytest.mark.asyncio
async def test_repeated_city_name_resolution_hits_the_cache():
    """同一个地名问两次，只该打一次地理编码。

    "上海天气" 与 "上海" 视为同一个键 —— `normalize_area` 会剥掉"天气"
    这类噪声词。但 "北京" 与 "北京市" 是**两个键**：归一化只去噪声词，
    不去行政后缀（去掉后缀会让"北京市"和"北京"之外更细的地名也开始串味）。
    这条测试把两种情形都钉住。
    """
    calls = []

    def handler(request):
        calls.append(request.url.params["address"])
        return httpx.Response(200, json={
            "status": "1",
            "geocodes": [{"adcode": "110101", "city": "北京市", "province": "北京市"}],
        })

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))

    assert await client.resolve_area("北京") == "110101"
    assert await client.resolve_area("北京") == "110101"
    assert await client.resolve_area("北京天气") == "110101"  # 噪声词被剥掉，同一个键

    assert calls == ["北京"], f"同一地名不该重复请求，实际请求了 {calls}"

    # 带行政后缀是另一个键：各自缓存，各自只查一次
    assert await client.resolve_area("北京市") == "110101"
    assert await client.resolve_area("北京市") == "110101"
    assert calls == ["北京", "北京市"]


@pytest.mark.asyncio
async def test_repeated_weather_query_hits_the_cache():
    """同一个行政区代码的实况，短时间内只该查一次。"""
    calls = []

    def handler(request):
        calls.append(request.url.params["city"])
        return httpx.Response(200, json={
            "status": "1", "lives": [{
                "province": "上海市", "city": "上海市", "adcode": "310115",
                "weather": "晴", "temperature": "28", "winddirection": "东风",
                "windpower": "3", "humidity": "55", "reporttime": "2026-09-20 12:00:00",
            }],
        })

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))

    first = await client.query("310115", "live")
    second = await client.query("310115", "live")

    assert calls == ["310115"], f"重复查询不该重复请求，实际请求了 {len(calls)} 次"
    assert first["temperature_c"] == second["temperature_c"] == 28


@pytest.mark.asyncio
async def test_different_areas_and_kinds_do_not_share_a_cache_entry():
    """缓存按地点和类型分开：实况与预报、两个城市，都不能互相顶掉。"""
    calls = []

    def handler(request):
        city = request.url.params["city"]
        kind = request.url.params["extensions"]
        calls.append((city, kind))
        if kind == "base":
            return httpx.Response(200, json={
                "status": "1", "lives": [{"adcode": city, "city": "x", "weather": "晴", "temperature": "20"}],
            })
        return httpx.Response(200, json={
            "status": "1", "forecasts": [{"adcode": city, "city": "x", "casts": [{"date": "2026-09-21"}]}],
        })

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))

    await client.query("310115", "live")
    await client.query("110101", "live")
    await client.query("310115", "forecast")

    assert len(calls) == 3, f"三个不同的键应各请求一次，实际 {calls}"


def test_ttl_cache_expires_and_evicts():
    """纯函数级验证：过期即失效，容量满即清空。"""
    from agent_tools.weather import _TtlCache

    clock = {"now": 100.0}
    cache = _TtlCache(ttl_seconds=10.0, max_entries=2, now=lambda: clock["now"])

    cache.set("a", "A")
    assert cache.get("a") == "A"

    clock["now"] = 111.0  # 超过 ttl
    assert cache.get("a") is None, "过期条目必须失效"

    cache.set("b", "B")
    cache.set("c", "C")
    cache.set("d", "D")  # 超过 max_entries=2 → 整体清空
    assert cache.get("b") is None and cache.get("c") is None
    assert cache.get("d") == "D"


def test_ttl_cache_returns_copies_so_callers_cannot_corrupt_it():
    """取出来的是副本：调用方改它不该污染缓存。"""
    from agent_tools.weather import _TtlCache

    cache = _TtlCache(ttl_seconds=10.0, max_entries=4)
    cache.set("k", {"casts": [{"date": "2026-09-21"}]})

    got = cache.get("k")
    got["casts"].append({"date": "篡改"})

    assert cache.get("k")["casts"] == [{"date": "2026-09-21"}]


@pytest.mark.asyncio
async def test_weather_failure_is_not_cached():
    """失败不能进缓存：否则一次网络抖动会让后面几次查询都直接报错。"""
    calls = []

    def handler(request):
        calls.append(request.url.params["city"])
        return httpx.Response(200, json={"status": "0", "info": "DAILY_QUERY_OVER_LIMIT"})

    client = AmapWeatherClient(api_key="key", transport=httpx.MockTransport(handler))

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await client.query("310115", "live")

    assert len(calls) == 2, "失败的查询不该被缓存"
