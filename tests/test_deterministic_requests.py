"""确定性分岔：天气与导航的识别、执行与措辞。

方案里点名的几条判据都在这个文件里：

* 「导航到广州塔」直接规划、只用「导航」时只追问目的地；
* 「今天路上很堵」不能误判成导航指令；
* 「天气局是做什么的」保持普通聊天；
* 每条成功路径都不把坐标交出去。
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent_core import fastpath as fast
from agent_core.contracts import Scope
from agent_core.loop import AgentLoop
from agent_tools.location import LocationStore
from agent_tools.navigation import make_navigation_tool

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _route(text: str):
    return AgentLoop._readonly_request(text)


# ── 天气的识别 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected_area", [
    ("天气", "当前位置"),
    ("现在天气怎么样", "当前位置"),
    ("今天天气如何", "当前位置"),
    ("查一下天气", "当前位置"),
    ("上海天气", "上海"),
    ("北京的天气", "北京"),
    ("浦东新区天气", "浦东新区"),
])
def test_weather_request_maps_to_an_area(text, expected_area):
    routed = _route(text)
    assert routed is not None, text
    assert routed[0] == "weather__query"
    assert routed[1]["area"] == expected_area


@pytest.mark.parametrize("text", [
    "天气局是做什么的",
    "天气之子好看吗",
    "今天天气不错然后我们去吃饭",
    "顺便看看天气",
    # 改口句必须交给意图闸门：它认得"不是X，是Y"，快路径只会把
    # "不是北京的"整段当地名送出去。
    "不是北京的天气，是上海的",
])
def test_weather_lookalikes_stay_in_normal_chat(text):
    """「天气」只是某个词的一部分、或者句子其实是两件事 —— 都不能接。"""
    routed = _route(text)
    assert routed is None or routed[0] != "weather__query", text


# ── 导航的识别 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("导航到广州塔", {"destination": "广州塔", "mode": "driving"}),
    ("步行去广州塔", {"destination": "广州塔", "mode": "walking"}),
    ("广州塔怎么走", {"destination": "广州塔", "mode": "driving"}),
    ("从北京南站到故宫", {"origin": "北京南站", "destination": "故宫", "mode": "driving"}),
])
def test_navigation_request_parses_endpoints(text, expected):
    routed = _route(text)
    assert routed is not None, text
    assert routed[0] == "navigation__route"
    for key, value in expected.items():
        assert routed[1][key] == value, (text, key)


def test_bare_qu_form_is_left_to_the_model_on_purpose():
    """「去广州塔」只有一「去」字，跟「我去吃饭」在规则层分不开。

    这是**有意的取舍**：宁可漏给模型（它照样会调导航工具），也不误接 ——
    误接的代价是平白弹一条用户没要的路线。想走快路径，带上
    「导航 / 怎么走 / 步行 / 从…到…」任一个形状就行。
    """
    assert _route("去广州塔") is None
    assert _route("我去吃饭了") is None


def test_bare_navigation_only_asks_for_a_destination():
    """只说「导航」：把空目的地交给工具，由工具回一句「你还没说要去哪儿」。"""
    routed = _route("导航")
    assert routed is not None
    assert routed[0] == "navigation__route"
    assert routed[1]["destination"] == ""


@pytest.mark.parametrize("text", [
    "今天路上很堵",
    "这个路线怎么样",
    "路上堵不堵",
])
def test_traffic_chatter_is_not_a_navigation_command(text):
    """评述句里出现「路线」两个字，不等于用户要一条路线。"""
    routed = _route(text)
    assert routed is None or routed[0] != "navigation__route", text


# ── 导航执行 ───────────────────────────────────────────────────────────
class _NavClient:
    def __init__(self, *, regeo_city="上海市", geocode_error=None, route_error=None):
        self.regeo_city = regeo_city
        self.geocode_error = geocode_error
        self.route_error = route_error
        self.geocoded: list[str] = []
        self.routed: list[tuple] = []

    async def regeo(self, latitude, longitude):
        return {"city": self.regeo_city, "province": "上海市", "district": "浦东新区", "area_code": "310115"}

    async def geocode(self, destination, city=None, citylimit=False):
        self.geocoded.append(str(destination))
        if self.geocode_error:
            raise self.geocode_error
        return {"latitude": 23.1066, "longitude": 113.3245, "label": f"{destination}（示例）", "city": "广州市"}

    async def route(self, origin, destination, mode):
        self.routed.append((origin, destination, mode))
        if self.route_error:
            raise self.route_error
        return {
            "distance_meters": 12300, "duration_seconds": 1500, "mode": mode,
            "instruction": "沿世纪大道向西行驶",
        }


def _store(*, age_seconds: int | None = 0):
    clock = [_NOW]
    store = LocationStore(ttl_seconds=300, now=lambda: clock[0])
    scope = Scope()
    if age_seconds is not None:
        store.update(scope, 31.2304, 121.4737, 18.5, _NOW - timedelta(seconds=age_seconds))
    return store, scope


@pytest.mark.asyncio
async def test_navigation_defaults_to_the_current_location_as_origin():
    store, scope = _store()
    client = _NavClient()
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "广州塔"}, scope)

    assert result["status"] == "ok"
    assert client.routed, "应当真的去规划了路线"
    assert result["output"]["distance_meters"] == 12300
    assert "latitude" not in result["output"], "坐标只在 map_url 里，不进 output"
    assert result["output"]["map_url"].startswith("https://uri.amap.com/navigation")


@pytest.mark.asyncio
async def test_navigation_without_location_asks_to_share():
    tool = make_navigation_tool(LocationStore(), _NavClient())

    result = await tool.handler({"destination": "广州塔"}, Scope())

    assert result["status"] == "unavailable"
    assert result["output"]["reason"] == "location_unavailable"


@pytest.mark.asyncio
async def test_navigation_with_expired_location_asks_to_reshare():
    store, scope = _store(age_seconds=900)
    tool = make_navigation_tool(store, _NavClient())

    result = await tool.handler({"destination": "广州塔"}, scope)

    assert result["status"] == "stale"
    assert result["output"]["reason"] == "location_stale"


@pytest.mark.asyncio
async def test_named_origin_does_not_touch_location_sharing():
    """「从北京南站到故宫」两头都是地名，跟「我在哪」无关 —— 不该要求开共享。"""
    client = _NavClient()
    tool = make_navigation_tool(LocationStore(), client)

    result = await tool.handler({"origin": "北京南站", "destination": "故宫"}, Scope())

    assert result["status"] == "ok"
    assert "北京南站" in client.geocoded


@pytest.mark.asyncio
async def test_unresolved_origin_has_its_own_reason():
    """起点定位不出来时要说起点，不能把用户支到目的地那头去找问题。"""
    client = _NavClient(geocode_error=RuntimeError("destination_not_found"))
    tool = make_navigation_tool(LocationStore(), client)

    result = await tool.handler({"origin": "那个什么地方", "destination": "故宫"}, Scope())

    assert result["status"] == "failed"
    assert result["output"]["reason"] == "origin_not_found"


@pytest.mark.asyncio
async def test_navigation_never_leaks_third_party_text():
    client = _NavClient(route_error=RuntimeError("Amap request failed with HTTP 500"))
    store, scope = _store()
    tool = make_navigation_tool(store, client)

    result = await tool.handler({"destination": "广州塔"}, scope)
    text = str(result)

    assert "HTTP" not in text
    assert "Amap" not in text


# ── 措辞 ───────────────────────────────────────────────────────────────
class _Result:
    def __init__(self, status="ok", output=None, error="", error_kind=""):
        self.status = status
        self.output = output or {}
        self.error = error
        self.error_kind = error_kind


def test_navigation_success_line_carries_distance_duration_and_link():
    line = fast.narrate(fast.navigation_summary(_Result(output={
        "distance_meters": 12300, "duration_seconds": 1500, "mode": "driving",
        "instruction": "沿世纪大道向西行驶", "map_url": "https://uri.amap.com/navigation?x=1",
    })))

    assert "12.3 公里" in line
    assert "25 分钟" in line
    assert "世纪大道" in line
    assert "uri.amap.com" in line


def test_navigation_walking_line_says_walking():
    line = fast.narrate(fast.navigation_summary(_Result(output={
        "distance_meters": 800, "duration_seconds": 600, "mode": "walking",
    })))

    assert "800 米" in line
    assert "步行约" in line


@pytest.mark.parametrize("reason,expected_fragment", [
    ("location_unavailable", "位置共享"),
    ("location_stale", "重新共享"),
    ("destination_not_found", "目的地"),
    ("origin_not_found", "出发点"),
    ("walking_range_exceeded", "步行"),
])
def test_navigation_failure_lines_are_stable_per_reason(reason, expected_fragment):
    line = fast.narrate(fast.navigation_summary(
        _Result(status="failed", output={"reason": reason}, error=reason)
    ))
    assert expected_fragment in line, reason


@pytest.mark.parametrize("reason,expected_fragment", [
    ("location_unavailable", "位置共享"),
    ("location_stale", "重新共享"),
    ("weather_area_unknown", "地名"),
    ("weather_unavailable", "天气服务"),
])
def test_weather_failure_lines_are_stable_per_reason(reason, expected_fragment):
    line = fast.narrate(fast.weather_summary(
        _Result(status="failed", output={"reason": reason}, error=reason)
    ))
    assert expected_fragment in line, reason


def test_weather_success_line_carries_city_condition_temperature_and_wind():
    line = fast.narrate(fast.weather_summary(_Result(output={
        "city": "上海市", "weather": "晴", "temperature_c": 28,
        "wind_direction": "东风", "wind_power": "3",
    })))

    assert "上海市" in line
    assert "晴" in line
    assert "28" in line
    assert "东风 3" in line


def test_weather_summary_keeps_area_code_and_coordinates_out():
    """行政区代码对"今天热不热"没用，不该进摘要 —— 也就没机会被复述。"""
    summary = fast.weather_summary(_Result(output={
        "city": "上海市", "weather": "晴", "temperature_c": 28, "area_code": "310115",
        "province": "上海市", "humidity": "55",
    }))
    dumped = str(summary.as_dict())

    assert "310115" not in dumped
    assert "latitude" not in dumped
