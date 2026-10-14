"""确定性快路径的管家表达（本地模板，零模型调用）。

【它解决什么】
有几类请求有确定答案、瞬间就能完成，走的是 `AgentLoop` 的确定性分岔
（`_readonly_request` / 当前播放 / 确定性搜索）。这些路径不出话术，
用户看到的要么是机器拼的句子，要么直接是一坨 JSON：

    用户：我现在在哪？
    （改造前）{"latitude":31.2304,"longitude":121.4737,"age_seconds":0.58}

本模块把这类结果也过一遍主 Chat 的说话方式：

    用户：我现在在哪？
    （改造后）你现在共享的位置在上海附近，定位精度大约 20 米。

【和 StewardNarrator 的分工】
- 长任务：`StewardNarrator.narrate()` —— 调模型写过程气泡（接手 / 证据 / 审批）。
- 快路径：本模块 —— **纯本地模板**，一次模型都不调，也不产生过程气泡。
两者共用人物设定、公开文本过滤（:mod:`agent_core.reply_guard`）、中文错误模板。

【为什么先做"安全摘要"】
快路径的工具结果里混着原始敏感字段（经纬度、地址、内部错误串）。
措辞层一旦能拿到这些，早晚会有一句把它们说出来。所以这里先把结果**降维**成
:class:`SafeReadonlySummary`：只有区域级描述、精度区间、新鲜度这类粗粒度信息。
措辞函数签名上就收不到 latitude / longitude，写过界也写不出来。

    原始工具结果 ──to_safe_summary──> SafeReadonlySummary ──narrate_fast_result──> 一句中文

【生命周期】
只有 :func:`to_safe_summary` 与 :func:`narrate` 两个入口；两者都是纯函数，
不访问工具、不访问数据库、不调模型。失败永远退回固定中文模板。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .reply_guard import mentions_internal_terms, reply_issue

# ----------------------------------------------------------------------
# 意图常量
# ----------------------------------------------------------------------
#: 这些是"快路径能认出来的意图"。写死在常量里而不是让调用方随手传字符串，
#: 是为了让模板表能对着它做完整性检查（见 tests/test_fastpath.py）。
INTENT_LOCATION = "location.current"
INTENT_TIME = "system.time"
INTENT_SYSTEM_INFO = "system.info"
#: "我住在哪"这类问 **Agent 自己** 的问题。刻意与 INTENT_SYSTEM_INFO 分开：
#: 前者是身份问题（它住在这台机器里），要第一人称回答；后者是设备巡检，
#: 要一份客观清单。混在一起会让"你在哪"答成一串主机名。
INTENT_HOST = "system.host"
INTENT_PLAYBACK = "playback.current"
INTENT_TASKS = "tasks.list"
INTENT_CONDITIONS = "conditions.list"
INTENT_SEARCH = "search.web"
#: 导航失败。刻意**不在** `_readonly_request` 的命中表里 —— 路由成功要模型
#: 从用户话里取目的地，只有**失败**才走这条确定性措辞。
INTENT_NAVIGATION = "navigation.route"
#: 天气。与位置共用同一份位置快照，但对用户是两件事（"我在哪" vs "今天热不热"），
#: 所以意图与模板都分开 —— 合成一条会把"天气"答成一句坐标说明。
INTENT_WEATHER = "weather.query"

# ----------------------------------------------------------------------
# 结果状态
# ----------------------------------------------------------------------
STATUS_OK = "ok"                       # 拿到了内容
STATUS_EMPTY = "empty"                 # 查通了，但没有任何内容（没有任务、没在放歌）
STATUS_STALE = "stale"                 # 有过，但过期了（位置共享断太久）
STATUS_UNAVAILABLE = "unavailable"     # 用户侧没开 / 没授权（位置共享没打开）
STATUS_FAILED = "failed"               # 工具或能力本身出错

#: 这些状态说出的话里**不能**出现"你现在…"这类"已确认"的断言。
#: 位置过期时说"你现在在上海"就是错的 —— 那是几分钟前的事。
NOT_CONFIRMED_STATUSES = frozenset({STATUS_STALE, STATUS_UNAVAILABLE, STATUS_FAILED})

#: 只有这两个状态才算"真的拿到了内容"，才允许出现事实性描述。
CONTENT_STATUSES = frozenset({STATUS_OK, STATUS_EMPTY})

#: 原始经纬度一旦出现在摘要里就算泄漏（措辞层拿不到，但摘要构造可能写错）。
LEAKY_FIELDS = (
    "latitude",
    "longitude",
    "lat",
    "lng",
    "lon",
    "accuracy_m",
    "captured_at",
    "age_seconds",
    "source",
)


@dataclass(frozen=True)
class SafeReadonlySummary:
    """交给措辞层的**唯一**输入：已经处理过的粗粒度摘要。

    刻意不提供 raw / payload 这类后门字段 —— 有了它，前面所有的"降维"
    就形同虚设。要加新意图就加新字段，别加逃生舱。
    """

    intent: str
    status: str = STATUS_OK
    #: 区域级描述，例如"上海附近"。**不是**完整地址，更不是坐标。
    area_label: str = ""
    #: 精度描述，名词短语，例如"大约 20 米""范围比较大"。已离散化成区间，不是原始数字。
    #: 由模板接在"定位精度"后面，所以这里不能写成完整判断句。
    accuracy_text: str = ""
    #: 新鲜度分句，例如"刚刚更新""有一会儿没更新"。空串表示未知，模板会整段省略。
    freshness_text: str = ""
    #: 已经过白名单裁剪的展示条目（歌曲名、任务标题…），上限由构造方保证。
    items: tuple[str, ...] = ()
    #: 条目总数（可能大于 len(items)，因为只展示前几条）。
    total: int | None = None
    #: 时间类结果：已经格式化好的人类可读时间。
    moment_text: str = ""
    #: 失败/不可用时的**内部**原因码。只用于日志，不进用户可见文本。
    reason_code: str = ""
    extra: tuple[tuple[str, str], ...] = field(default=())

    @property
    def has_content(self) -> bool:
        """是否确认拿到了内容。"""
        return self.status == STATUS_OK

    @property
    def confirmed(self) -> bool:
        """这个摘要允许措辞层做"你现在…"这种确定性断言吗。"""
        return self.status not in NOT_CONFIRMED_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "status": self.status,
            "area_label": self.area_label,
            "accuracy_text": self.accuracy_text,
            "freshness_text": self.freshness_text,
            "items": list(self.items),
            "total": self.total,
            "moment_text": self.moment_text,
            "reason_code": self.reason_code,
            "extra": dict(self.extra),
        }


# ----------------------------------------------------------------------
# 原始字段 / 文本的清洗
# ----------------------------------------------------------------------
#: 内部英文错误里常见的形状。命中就一律换成中文模板，不给用户看原文。
_RAW_ERROR_SHAPES = (
    "traceback",
    "no such",
    "not available",
    "unavailable",
    "timed out",
    "timeout",
    "connection",
    "exception",
    "error:",
    "errno",
    "http ",
    "socket",
    "misconfigured",
    "not configured",
    "refused",
    "permission denied",
    "failed",
)

#: 拉丁字母占比超过这个阈值就当成"英文串"。中文回复是汉字为主，
#: 所以这条能兜住 _RAW_ERROR_SHAPES 没穷举到的英文错误
#: （"No recent browser location is available." 就是漏网的那个）。
#: 阈值留了余量：主机名、路径、歌手的英文名会让占比升到 0.4 左右，
#: 正常回复不该被误判，纯英文错误串（0.8+）则必定命中。
_LATIN_RATIO_LIMIT = 0.6


def _latin_ratio(text: str) -> float:
    letters = len(re.findall(r"[A-Za-z]", text))
    # 统计"实义字符"：中文字、拉丁字母、数字。空格标点不参与，否则短句会被带偏。
    meaningful = len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", text))
    if meaningful == 0:
        return 1.0
    return letters / meaningful


def _plain(value: Any) -> str:
    """折成一行纯文本。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


#: `platform.system()` 的原始值 → 用户看得懂的说法。
#: 尤其 Darwin：直接说"跑在 Darwin 上"没人知道那是 macOS 的内核名。
_SYSTEM_NAMES = {
    "windows": "Windows",
    "linux": "Linux",
    "darwin": "macOS",
    "java": "Java",
}


def _friendly_system(system: str) -> str:
    """平台名 → 人话。认不出来的原样返回，不编。"""
    value = _plain(system)
    return _SYSTEM_NAMES.get(value.lower(), value)


def is_safe_public_text(text: str) -> bool:
    """一段文本能不能直接进用户可见回复。

    与最终回复共用 :mod:`agent_core.reply_guard` 的判定，另外挡掉
    原始英文错误串和经纬度字样 —— 那些是快路径最容易漏出去的两种东西。
    """
    value = str(text or "").strip()
    if not value:
        return False
    lowered = value.lower()
    if any(shape in lowered for shape in _RAW_ERROR_SHAPES):
        return False
    if _latin_ratio(value) > _LATIN_RATIO_LIMIT:
        return False
    if any(field in lowered for field in LEAKY_FIELDS):
        return False
    if reply_issue(value) is not None:
        return False
    if mentions_internal_terms(value):
        return False
    return True


def _short(text: Any, limit: int = 32) -> str:
    value = _plain(text)
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


# ----------------------------------------------------------------------
# 位置：坐标 → 区域级描述
# ----------------------------------------------------------------------
#: 粗略的地理分区框，每项是 (名称, 最小经度, 最小纬度, 最大经度, 最大纬度)。
#: 只用于挑一个"XX附近"的说法，精度到市就够 —— 用户不需要在气泡里看到自己的门牌号。
#: 注意纬度是**下界在前**（中国境内纬度越大越靠北），写反会一路掉到"国内"。
_REGION_BOXES: tuple[tuple[str, float, float, float, float], ...] = (
    ("北京附近", 115.4, 39.4, 117.5, 41.1),
    ("上海附近", 120.8, 30.6, 122.2, 31.9),
    ("广州附近", 112.9, 22.5, 114.1, 23.9),
    ("深圳附近", 113.7, 22.4, 114.6, 22.9),
    ("杭州附近", 119.5, 29.8, 120.8, 30.6),
    ("成都附近", 103.5, 30.2, 104.7, 31.0),
    ("重庆附近", 105.8, 29.0, 107.2, 30.3),
    ("武汉附近", 113.7, 30.0, 115.1, 31.0),
    ("西安附近", 108.5, 33.8, 109.7, 34.7),
    ("南京附近", 118.3, 31.6, 119.3, 32.6),
    ("天津附近", 116.7, 38.5, 118.1, 39.4),
    ("苏州附近", 120.2, 30.8, 121.4, 31.9),
    ("香港附近", 113.8, 22.1, 114.5, 22.6),
    ("澳门附近", 113.4, 22.0, 113.7, 22.3),
    ("台北附近", 121.3, 24.8, 121.8, 25.3),
)


def coarse_area_label(latitude: Any, longitude: Any) -> str:
    """把坐标降维成区域级描述。**永远不会**把坐标回传出去。

    认不出具体城市时返回"你那边"这种不含地理位置的说法 ——
    宁可含糊，也不能猜错（猜错比不说更糟）。
    """
    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        return "你那边"
    if not math.isfinite(lat) or not math.isfinite(lng):
        return "你那边"
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return "你那边"
    for name, min_lng, min_lat, max_lng, max_lat in _REGION_BOXES:
        if min_lng <= lng <= max_lng and min_lat <= lat <= max_lat:
            return name
    if 72.0 <= lng <= 137.9 and 0.8 <= lat <= 55.9:
        return "国内"
    return "你那边"


def accuracy_text(accuracy_m: Any) -> str:
    """精度米数 → 人话区间。离散化的好处是原始数字不进气泡。

    分档到"一公里"为止：城市里问"我在哪"，几百米和一公里对用户的决策
    没有区别（都能走到），但"范围比较大"会让人以为定位失效了 ——
    850 米在城里是能用的位置，不该被说成不可信。

    每档都只是**名词短语**，会被 :func:`_fill` 接在"定位精度"后面（"定位精度大约 20 米" /
    "定位精度范围比较大"）。所以这里不放完整判断句 —— 若某档写成"定位范围比较大"，
    就会拼出"定位精度定位范围比较大"这种重复词。改措辞前先看 `_fill` 与前缀约定。
    """
    try:
        value = float(accuracy_m)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value < 0:
        return ""
    if value <= 15:
        return "大约 10 米"
    if value <= 30:
        return "大约 20 米"
    if value <= 60:
        return "大约 50 米"
    if value <= 150:
        return "大约 100 米"
    if value <= 500:
        return "大约几百米"
    if value <= 1000:
        return "大约一公里范围内"
    return "范围比较大"


def freshness_text(age_seconds: Any) -> str:
    """位置快照的新鲜度 → 能直接塞进句子里的分句。

    返回的是"刚刚更新"这种完整分句而不是"刚刚"，因为模板会把它整体填进
    ``…，{freshness}。``。给空值返回空串，模板据此整段省略 ——
    绝不拼出「定位精度大约 20 米，。」这种半截话。
    """
    try:
        age = float(age_seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(age) or age < 0:
        return ""
    if age <= 60:
        return "刚刚更新"
    if age <= 300:
        return "几分钟前更新"
    return "有一会儿没更新"


# ----------------------------------------------------------------------
# 摘要构造：各意图一个入口
# ----------------------------------------------------------------------
def location_summary(result: Any) -> SafeReadonlySummary:
    """位置工具结果 → 安全摘要。

    三种非成功情况要分开，因为用户能做的事完全不同：
    - ``unavailable``：用户没开共享 → 让他去开；
    - ``stale``：开过但太久没更新 → 让他重新共享一次；
    - ``failed``：能力本身没响应 → 让他稍后再试。
    合成一句"读不到位置"是一种偷懒，用户不知道该做什么。
    """
    status = str(getattr(result, "status", "") or "")
    output = getattr(result, "output", None) or {}
    if not isinstance(output, dict):
        output = {}
    reason = _plain(getattr(result, "error", "")) or str(output.get("reason") or "")

    if status != "ok":
        code = _location_failure_code(reason)
        return SafeReadonlySummary(intent=INTENT_LOCATION, status=code, reason_code=reason)

    # 区域优先用工具解析出来的地名（"上海市浦东新区"）。原先这里读 latitude /
    # longitude 再粗判成"国内 / 你那边" —— 那是因为老的位置工具只交得出裸坐标。
    # 现在工具直接给地名，比"你那边"有用得多；退回坐标判断只作兜底。
    # （不改这一处，"我在哪"会退化成"你现在共享的位置在你那边。"，精度和新鲜度全丢。）
    area = str(output.get("place_label") or "").strip() or coarse_area_label(
        output.get("latitude"), output.get("longitude")
    )
    return SafeReadonlySummary(
        intent=INTENT_LOCATION,
        status=STATUS_OK,
        area_label=area,
        accuracy_text=accuracy_text(output.get("accuracy_m")),
        freshness_text=freshness_text(output.get("age_seconds")),
    )


def _distance_text(meters: Any) -> str:
    """米数 → 人话。一公里以下按米说（"800 米"比"0.8 公里"直观）。"""
    try:
        value = float(meters)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value < 0:
        return ""
    if value < 1000:
        return f"{int(round(value))} 米"
    return f"{value / 1000:.1f} 公里"


def _duration_text(seconds: Any, mode: Any) -> str:
    """秒数 → 人话。最少说"1 分钟"，不说"0 分钟" —— 那看着像出了故障。"""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value) or value < 0:
        return ""
    minutes = max(1, int(round(value / 60)))
    if minutes < 60:
        body = f"{minutes} 分钟"
    elif minutes % 60:
        body = f"{minutes // 60} 小时 {minutes % 60} 分钟"
    else:
        body = f"{minutes // 60} 小时"
    return f"{'步行约' if str(mode or '') == 'walking' else '驾车约'}{body}"


def navigation_summary(result: Any) -> SafeReadonlySummary:
    """导航结果（或裸错误码）→ 安全摘要。

    成功时只搬四样：距离、用时、主要路线的那一句、地图链接。
    **起点与终点的坐标一个都不进来** —— 它们在 map_url 里已经够了，
    多留一份就多一次被复述进气泡的机会。

    失败时不在这里写句子，只把 ``reason_code`` 交给 `NAVIGATION_REASON_LINES`。

    ``result`` 也接受一个裸原因码字符串：`_navigation_failure_reply` 那条路径
    手里只有码、没有完整结果。两种输入合并到这一个函数里，
    就是为了不留两个同名实现（同名会静默覆盖，见下面那段注释）。
    """
    if result is None or isinstance(result, str):
        code = _plain(result)
        return SafeReadonlySummary(
            intent=INTENT_NAVIGATION,
            status=NAVIGATION_STATUSES.get(code, STATUS_FAILED),
            reason_code=code,
        )
    status = str(getattr(result, "status", "") or "")
    output = getattr(result, "output", None) or {}
    if not isinstance(output, dict):
        output = {}
    reason = _plain(getattr(result, "error", "")) or str(output.get("reason") or "")

    if status != "ok":
        code = {"unavailable": STATUS_UNAVAILABLE, "stale": STATUS_STALE}.get(status, STATUS_FAILED)
        return SafeReadonlySummary(intent=INTENT_NAVIGATION, status=code, reason_code=reason)

    extra: list[tuple[str, str]] = [
        ("distance", _distance_text(output.get("distance_meters"))),
        ("duration", _duration_text(output.get("duration_seconds"), output.get("mode"))),
    ]
    instruction = _plain(output.get("instruction"))
    if instruction:
        extra.append(("route", instruction))
    map_url = _plain(output.get("map_url"))
    if map_url:
        extra.append(("map_url", map_url))
    return SafeReadonlySummary(intent=INTENT_NAVIGATION, status=STATUS_OK, extra=tuple(extra))


def weather_summary(result: Any) -> SafeReadonlySummary:
    """天气工具结果 → 安全摘要。

    成功时只搬四样东西过来：城市名、天气现象、气温、风级。
    坐标、行政区代码、接口原文一律**不进来** —— 它们对"今天热不热"这个问题
    没有用，而一旦进来就可能在气泡里被复述。

    失败时不在这里写句子，只把 ``reason_code`` 交给模板层；
    `WEATHER_REASON_LINES` 才是决定"怎么说"的地方。职责分开，
    改措辞就不用碰摘要构造。
    """
    status = str(getattr(result, "status", "") or "")
    output = getattr(result, "output", None) or {}
    if not isinstance(output, dict):
        output = {}
    reason = _plain(getattr(result, "error", "")) or str(output.get("reason") or "")

    if status != "ok":
        code = {"unavailable": STATUS_UNAVAILABLE, "stale": STATUS_STALE}.get(status, STATUS_FAILED)
        return SafeReadonlySummary(intent=INTENT_WEATHER, status=code, reason_code=reason)

    city = _plain(output.get("city")) or _plain(output.get("province")) or "你那边"
    extra: list[tuple[str, str]] = [
        ("condition", _plain(output.get("weather"))),
        ("temperature", _plain(output.get("temperature_c"))),
    ]
    # 风向与风力是两个字段，拼起来才是一句"东风 3 级"。缺任何一个都不拼，
    # 留下半句"东"比不说更糟。
    wind = " ".join(
        part for part in (_plain(output.get("wind_direction")), _plain(output.get("wind_power"))) if part
    )
    if wind:
        extra.append(("wind", wind))
    return SafeReadonlySummary(
        intent=INTENT_WEATHER,
        status=STATUS_OK,
        area_label=city,
        extra=tuple(extra),
    )


def _location_failure_code(reason: str) -> str:
    lowered = reason.lower()
    if "location_stale" in lowered or "stale" in lowered:
        # 上报过但超出 TTL：用户只要重新共享一次。
        return STATUS_STALE
    if "location_unavailable" in lowered or "no recent browser location" in lowered:
        # 这个 scope 从没上报过位置：用户需要先去开共享。
        return STATUS_UNAVAILABLE
    if not reason:
        return STATUS_UNAVAILABLE
    return STATUS_FAILED


def time_summary(output: dict[str, Any]) -> SafeReadonlySummary:
    """本机时间 → 安全摘要。时间文本在这里就格式化好，措辞层只负责说。"""
    raw = _plain(output.get("local_time"))
    moment = ""
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
            weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[parsed.weekday()]
            moment = f"{parsed.year}年{parsed.month}月{parsed.day}日 {weekday} {parsed.hour:02d}:{parsed.minute:02d}"
        except ValueError:
            moment = ""
    timezone_name = _plain(output.get("timezone"))
    extra = (("timezone", timezone_name),) if timezone_name else ()
    if not moment:
        return SafeReadonlySummary(intent=INTENT_TIME, status=STATUS_FAILED, reason_code="time_unreadable")
    return SafeReadonlySummary(intent=INTENT_TIME, status=STATUS_OK, moment_text=moment, extra=extra)


def system_info_summary(output: dict[str, Any]) -> SafeReadonlySummary:
    """本机信息 → 安全摘要。字段逐个白名单取，其余一律不进摘要。"""
    items: list[str] = []
    release = _plain(output.get("release"))
    if output.get("system"):
        text = _plain(output["system"])
        if release:
            text = f"{text} {release}"
        if output.get("machine"):
            text = f"{text}（{_plain(output['machine'])}）"
        items.append(f"系统是 {text}")
    if output.get("hostname"):
        items.append(f"主机名是 {_plain(output['hostname'])}")
    if output.get("path"):
        items.append(f"工作目录在 {_plain(output['path'])}")
    if not items:
        return SafeReadonlySummary(intent=INTENT_SYSTEM_INFO, status=STATUS_EMPTY)
    return SafeReadonlySummary(intent=INTENT_SYSTEM_INFO, status=STATUS_OK, items=tuple(items[:3]))


def host_summary(output: dict[str, Any]) -> SafeReadonlySummary:
    """"我住在哪" → 安全摘要。

    这不是设备巡检，是身份问题。回答要第一人称、说清"我住在这台机器里"，
    但**不报**完整主机名/路径 —— 那些是这台机器的内部标识，不是"住处"。
    只说操作系统名（Linux / Windows / macOS）和它跑在本机这一事实。
    """
    system = _plain(output.get("system"))
    if not system:
        return SafeReadonlySummary(intent=INTENT_HOST, status=STATUS_EMPTY)
    return SafeReadonlySummary(
        intent=INTENT_HOST,
        status=STATUS_OK,
        reason_code=system,
    )


def playback_summary(result: Any) -> SafeReadonlySummary:
    """播放器状态 → 安全摘要。"""
    status = str(getattr(result, "status", "") or "")
    if status != "ok":
        reason = _plain(getattr(result, "error", ""))
        return SafeReadonlySummary(
            intent=INTENT_PLAYBACK,
            status=STATUS_FAILED if reason else STATUS_UNAVAILABLE,
            reason_code=reason,
        )
    state = getattr(result, "output", None) or {}
    if not isinstance(state, dict):
        state = {}
    playing = _plain(state.get("status")).lower()
    title = _plain(state.get("title") or state.get("name"))
    artist = _plain(state.get("artist") or state.get("artists"))
    if playing in {"stopped", "stop", "idle", "none", "not_playing"} or not title:
        return SafeReadonlySummary(intent=INTENT_PLAYBACK, status=STATUS_EMPTY)
    track = f"《{_short(title, 28)}》"
    if artist:
        track += f" - {_short(artist, 20)}"
    return SafeReadonlySummary(
        intent=INTENT_PLAYBACK,
        status=STATUS_OK,
        items=(track,),
        extra=(("playing", "yes" if playing in {"playing", "play"} else "no"),),
    )


def task_list_summary(result: Any, *, intent: str = INTENT_TASKS) -> SafeReadonlySummary:
    """任务/条件提醒列表 → 安全摘要。

    只留标题与状态，**不留** request_text、allowed_tools、last_error 这些
    可能带内部细节的字段。
    """
    status = str(getattr(result, "status", "") or "")
    if status != "ok":
        reason = _plain(getattr(result, "error", ""))
        return SafeReadonlySummary(intent=intent, status=STATUS_FAILED, reason_code=reason)
    output = getattr(result, "output", None) or {}
    rows = output.get("tasks") if isinstance(output, dict) else None
    rows = [row for row in (rows or []) if isinstance(row, dict)]
    if not rows:
        return SafeReadonlySummary(intent=intent, status=STATUS_EMPTY, total=0)
    titles = tuple(f"{_short(row.get('title') or row.get('task_id') or '未命名', 28)}" for row in rows[:5])
    return SafeReadonlySummary(intent=intent, status=STATUS_OK, items=titles, total=len(rows))


def search_summary(result: Any) -> SafeReadonlySummary:
    """确定性搜索结果 → 安全摘要。"""
    status = str(getattr(result, "status", "") or "")
    if status != "ok":
        reason = _plain(getattr(result, "error", ""))
        return SafeReadonlySummary(intent=INTENT_SEARCH, status=STATUS_FAILED, reason_code=reason)
    output = getattr(result, "output", None) or {}
    rows = []
    if isinstance(output, dict):
        rows = [row for row in (output.get("results") or output.get("tracks") or []) if isinstance(row, dict)]
    if not rows:
        return SafeReadonlySummary(intent=INTENT_SEARCH, status=STATUS_EMPTY, total=0)
    items: list[str] = []
    for row in rows[:5]:
        title = _short(row.get("title") or row.get("name") or "未知", 32)
        artist = _short(row.get("artist"), 20)
        items.append(f"《{title}》" + (f" - {artist}" if artist else ""))
    return SafeReadonlySummary(intent=INTENT_SEARCH, status=STATUS_OK, items=tuple(items), total=len(rows))


def failure_summary(intent: str, reason: str = "") -> SafeReadonlySummary:
    """给"能力不可用 / 工具没注册"这条分支用。"""
    return SafeReadonlySummary(intent=intent, status=STATUS_FAILED, reason_code=_plain(reason))


#: 导航失败原因码 → 该状态。
#: 只有这两个不是"失败"：它们不是能力坏了，而是"还差用户做一件事"
#: （去开共享 / 重新共享），用户能做的事和真正的失败完全不同。
NAVIGATION_STATUSES: dict[str, str] = {
    "location_unavailable": STATUS_UNAVAILABLE,
    "location_stale": STATUS_STALE,
}


# 导航摘要**只有一个实现** —— 见上面那个 `navigation_summary`。
# 它同时接受"完整结果对象"和"裸错误码"，所以不要再在这里写一个同名的：
# Python 里后定义的同名函数会**静默覆盖**先定义的那个。上一版就是这么坏的 ——
# 带完整结果的那版定义在前面，被这里这个只认错误码的版本盖掉，
# 于是导航成功也走进"按错误码找话术"的路，全句掉进兜底（"这次没查到…"）。


# ----------------------------------------------------------------------
# 本地模板措辞
# ----------------------------------------------------------------------
#: 暂停态是播放独有的分支（同一个 STATUS_OK，说法不同），
#: 用这个伪状态键避免给 SafeReadonlySummary 加一个只有播放用得到的字段。
STATUS_OK_PAUSED = "ok_paused"

#: 兜底模板：意图没覆盖到 / 模板把内容筛空了时用。
#: 刻意不承诺任何事情 —— 说不出内容时，含糊比编造好。
_FALLBACK_TEMPLATES: dict[str, str] = {
    STATUS_OK: "我看到了，不过这会儿能说清楚的部分不多。你把想问的说细一点，我再帮你看。",
    STATUS_EMPTY: "我查过了，这会儿没有可说的内容。",
    STATUS_STALE: "我这里的信息有点旧了。你重新来一次，我再帮你确认。",
    STATUS_UNAVAILABLE: "这会儿我还读不到。你那边是不是还没开这个功能？",
    STATUS_FAILED: "这次没查到。稍后再试一次会更合适。",
}

_TEMPLATES: dict[tuple[str, str], str] = {
    # ── 位置 ──
    # 成功：区域级 + 精度 + 新鲜度。**没有**经纬度，用户在气泡里看到的是"上海附近"。
    # {accuracy} / {freshness} 都自带前导逗号，缺值就整段消失（见 _fill）。
    (INTENT_LOCATION, STATUS_OK): "你现在共享的位置在{area}{accuracy}{freshness}。",
    (INTENT_LOCATION, STATUS_STALE): "我这里的位置有一会儿没更新了。你重新共享一下，我再帮你确认。",
    (INTENT_LOCATION, STATUS_UNAVAILABLE): "这会儿我还读不到你的位置。你那边是不是还没开位置共享？",
    (INTENT_LOCATION, STATUS_FAILED): "这会儿位置服务没有响应，我暂时没法确认。稍后再试一次会更合适。",
    # ── 时间 ──
    (INTENT_TIME, STATUS_OK): "现在是{moment}{timezone}。",
    (INTENT_TIME, STATUS_FAILED): "我这会儿读不到本机时间。稍后再试一次会更合适。",
    # ── Agent 自己住在哪（第一人称：它在这台机器里）──
    (INTENT_HOST, STATUS_OK): "我就住在你这台电脑里，跑在 {system} 上。",
    (INTENT_HOST, STATUS_EMPTY): "我住在这台机器里，具体环境这会儿说不太准。",
    (INTENT_HOST, STATUS_FAILED): "我住在这台机器里，不过现在读不到它的环境信息。",
    # ── 本机信息 ──
    (INTENT_SYSTEM_INFO, STATUS_OK): "我这边的机器情况：{items}。",
    (INTENT_SYSTEM_INFO, STATUS_EMPTY): "本机信息这会儿读不出来。稍后再试一次会更合适。",
    (INTENT_SYSTEM_INFO, STATUS_FAILED): "本机信息这会儿读不出来。稍后再试一次会更合适。",
    # ── 播放 ──
    (INTENT_PLAYBACK, STATUS_OK): "现在正在放{items}。",
    (INTENT_PLAYBACK, STATUS_OK_PAUSED): "现在停在一半：{items}。",
    (INTENT_PLAYBACK, STATUS_EMPTY): "现在没有在放歌。",
    (INTENT_PLAYBACK, STATUS_UNAVAILABLE): "我这会儿连不上播放器，不知道在放什么。",
    (INTENT_PLAYBACK, STATUS_FAILED): "读播放器状态的时候卡住了。稍后再试一次会更合适。",
    # ── 任务列表 ──
    (INTENT_TASKS, STATUS_OK): "你手上现在有{total}件事：{items}。想细看哪一件，说一声。",
    (INTENT_TASKS, STATUS_EMPTY): "你现在没有挂着的定时任务。",
    (INTENT_TASKS, STATUS_FAILED): "任务列表这会儿读不出来。稍后再试一次会更合适。",
    # ── 条件提醒 ──
    (INTENT_CONDITIONS, STATUS_OK): "你现在挂着{total}个条件提醒：{items}。",
    (INTENT_CONDITIONS, STATUS_EMPTY): "你现在没有挂着的条件提醒。",
    (INTENT_CONDITIONS, STATUS_FAILED): "条件提醒列表这会儿读不出来。稍后再试一次会更合适。",
    # ── 确定性搜索（有词的几句）──
    (INTENT_SEARCH, STATUS_OK): "我找到了这些：{items}。要看哪一个，说一声。",
    (INTENT_SEARCH, STATUS_EMPTY): "我查了一圈，没有找到对得上的东西。换个说法再试一次？",
    (INTENT_SEARCH, STATUS_FAILED): "这次没查成。稍后再试一次会更合适。",
    # ── 导航（只有成功档；失败档按原因码走 NAVIGATION_REASON_LINES）──
    # {route} 与 {map_line} 都自带前导标点，缺了整段消失 —— 没有主要路线描述
    # 或没有地图链接时，句子仍然完整。
    (INTENT_NAVIGATION, STATUS_OK): "全程{distance}，{duration}{route}{map_line}。",
    # ── 天气 ──
    # 只有成功档在这里；失败档按原因码走 WEATHER_REASON_LINES ——
    # "没开定位""定位过期""认不出这个地名""服务没响应"这四种对用户下一步
    # 该做什么是不同的（去开 / 重新共享 / 换个说法 / 稍后再试），
    # (意图, 状态) 两维分不开。
    (INTENT_WEATHER, STATUS_OK): "{area}现在{condition}，气温{temperature}℃{wind}。",
}

#: 导航失败的原因码 → 固定中文。**按码说话**，不是按异常原文说话。
#: 每句话都要能让用户知道"下一步做什么"：开共享 / 换目的地 / 换出行方式 / 稍后再试。
#: 这五条是产品口径，改之前先想清楚用户的动作是否会因此改变。
NAVIGATION_REASON_LINES: dict[str, str] = {
    "location_unavailable": "我还没有拿到你的当前位置。打开位置共享后，我再给你规划。",
    "location_stale": "我这里的起点有一会儿没更新了。重新共享位置后，我再继续。",
    "destination_not_found": "这个目的地我没定位准。换一个更完整的名称或地址，我再找。",
    "origin_not_found": "你说的那个出发点我没定位准。换个更完整的名称，或者只说目的地，我从你当前位置出发。",
    "walking_range_exceeded": "这段路超出了步行规划范围。我可以改成驾车路线再看。",
    "navigation_unavailable": "这次路线服务没有响应。我保留了目的地，稍后可以接着查。",
    # 参数类问题：属于"模型没把话听全"，措辞要直接把话头递回给用户。
    "destination_required": "你还没说要去哪儿。告诉我目的地，我马上给你规划。",
    "destination_invalid": "这个目的地我读不出来。换个更完整的写法，我再找。",
}

#: 天气失败的原因码 → 固定中文。同样按码说话，不给用户看英文异常与接口原文。
#: 每种情况都要给**用户能做的下一步**：去开定位 / 重新共享 / 换个说法报城市 / 稍后再试。
WEATHER_REASON_LINES: dict[str, str] = {
    "location_unavailable": "我还不知道你在哪个城市。打开位置共享，或者直接告诉我城市名，都行。",
    "location_stale": "我这里的定位有一会儿没更新了。你重新共享一次，我马上再看。",
    "location_resolution_unavailable": "我这边的定位服务没接上，暂时不知道你在哪个城市。你直接说城市名也可以。",
    "weather_area_unknown": "这个地名我没认出来。换个更完整的写法，或者直接说城市名？",
    "invalid_weather_area": "这个地点我读不出来。用城市名或者六位行政区代码再试一次？",
    "invalid_weather_kind": "这个查询类型我不认识。跟我说「天气」就行。",
    "weather_unavailable": "天气服务这次没有响应。稍后再试一次会更合适。",
}


def template_line(summary: SafeReadonlySummary) -> str:
    """按 (意图, 状态) 挑一句固定中文。纯函数。

    模板里填的都是摘要里**已经清洗过**的字段；拿不到内容就退回兜底话术，
    绝不把空字符串拼进句子（"你现在共享的位置在。"这种）。
    """
    intent = str(summary.intent or "")
    status = str(summary.status or "")
    key_status = status
    if intent == INTENT_PLAYBACK and status == STATUS_OK:
        playing = dict(summary.extra).get("playing")
        if playing != "yes":
            key_status = STATUS_OK_PAUSED

    if intent == INTENT_NAVIGATION and status != STATUS_OK:
        # 导航的每一档**失败**原因各有一句话，模板表按 (意图, 状态) 分不细 ——
        # 认不出的码就退回兜底，宁可含糊也不猜。
        # 成功档必须往下走模板表：这个分支以前不区分成功与失败，于是"路线查到了"
        # 也会去找原因码（成功时本来就没有原因码），直接掉进兜底。
        line = NAVIGATION_REASON_LINES.get(_plain(summary.reason_code))
        if line is None:
            return _FALLBACK_TEMPLATES.get(status, _FALLBACK_TEMPLATES[STATUS_FAILED])
        return line

    if intent == INTENT_WEATHER and status != STATUS_OK:
        # 天气与导航同一套做法：失败档按原因码各说各的（用户接下来要做的事不同），
        # 成功档才落到下面的模板表。认不出的码同样退回兜底。
        line = WEATHER_REASON_LINES.get(_plain(summary.reason_code))
        if line is None:
            return _FALLBACK_TEMPLATES.get(status, _FALLBACK_TEMPLATES[STATUS_FAILED])
        return line

    template = _TEMPLATES.get((intent, key_status))
    if template is None:
        return _FALLBACK_TEMPLATES.get(status, _FALLBACK_TEMPLATES[STATUS_FAILED])

    filled = _fill(template, summary)
    if filled is None:
        # 模板需要的内容缺了 —— 说一句不含事实的兜底，比说半句话强。
        return _FALLBACK_TEMPLATES.get(status, _FALLBACK_TEMPLATES[STATUS_FAILED])
    return filled


def _fill(template: str, summary: SafeReadonlySummary) -> str | None:
    """把摘要填进模板；缺任何必需内容就返回 None（调用方走兜底）。"""
    values: dict[str, str] = {}
    if "{area}" in template:
        area = _plain(summary.area_label)
        if not area:
            return None
        values["area"] = area
    if "{accuracy}" in template:
        # 精度缺失是常态（浏览器不给），不能因此放弃整句。
        # 值里**自带**"定位精度"和**前导逗号**，这样精度缺了就整段消失 ——
        # 前缀若放在模板里，缺值时会把模板自身的逗号留成空壳
        # （"…在上海附近，。"）。
        # 各档位只负责一个名词短语（"大约 20 米""范围比较大"），所以不会拼出
        # "定位精度定位范围比较大"这种重复词。
        accuracy = _plain(summary.accuracy_text)
        values["accuracy"] = f"，定位精度{accuracy}" if accuracy else ""
    if "{freshness}" in template:
        # 和 accuracy 同样的约定：整段带上前导逗号，缺了就整段消失。
        freshness = _plain(summary.freshness_text)
        values["freshness"] = f"，{freshness}" if freshness else ""
    # ── 天气三件套 ──
    # 天气现象和气温是这句话的主干，缺了就不成句 → 返回 None 走兜底。
    # 风级是附加信息，缺了整段消失，不能因此把"晴，28 度"也砍掉。
    if "{condition}" in template:
        condition = _plain(dict(summary.extra).get("condition"))
        if not condition:
            return None
        values["condition"] = condition
    if "{temperature}" in template:
        temperature = _plain(dict(summary.extra).get("temperature"))
        if not temperature:
            return None
        values["temperature"] = temperature
    if "{wind}" in template:
        wind = _plain(dict(summary.extra).get("wind"))
        values["wind"] = f"，{wind}" if wind else ""
    # ── 导航四件套 ──
    # 距离和用时是主干，缺了不成句 → 走兜底。主要路线与地图链接是附加信息，
    # 缺了整段消失，不能因为它们把"全程 12 公里，驾车 25 分钟"也一起砍掉。
    if "{distance}" in template:
        distance = _plain(dict(summary.extra).get("distance"))
        if not distance:
            return None
        values["distance"] = distance
    if "{duration}" in template:
        duration = _plain(dict(summary.extra).get("duration"))
        if not duration:
            return None
        values["duration"] = duration
    if "{route}" in template:
        route = _plain(dict(summary.extra).get("route"))
        values["route"] = f"，主要走{route}" if route else ""
    if "{map_line}" in template:
        map_url = _plain(dict(summary.extra).get("map_url"))
        values["map_line"] = f"，地图链接：{map_url}" if map_url else ""
    if "{moment}" in template:
        moment = _plain(summary.moment_text)
        if not moment:
            return None
        values["moment"] = moment
    if "{system}" in template:
        # INTENT_HOST 用 reason_code 携带平台名（"Windows"/"Linux"/"Darwin"）。
        # 缺了就说"这台机器"—— "我住在  上"这种半截话不能出现。
        system = _plain(summary.reason_code)
        values["system"] = _friendly_system(system) if system else "这台机器"
    if "{timezone}" in template:
        timezone_name = _plain(dict(summary.extra).get("timezone"))
        values["timezone"] = f"（{timezone_name}）" if timezone_name else ""
    if "{items}" in template:
        items = [_plain(item) for item in summary.items if _plain(item)]
        if not items:
            return None
        values["items"] = "、".join(items)
    if "{total}" in template:
        total = summary.total if summary.total is not None else len(summary.items)
        values["total"] = str(int(total))
    out = template
    for name, value in values.items():
        out = out.replace("{" + name + "}", value)
    if "{" in out or "}" in out:
        return None
    return out


# ----------------------------------------------------------------------
# 公开入口
# ----------------------------------------------------------------------
def narrate(summary: SafeReadonlySummary) -> str:
    """安全摘要 → 一条用户可见的中文回复。

    契约（对调用方的承诺）：
    - 不调用模型、不访问工具、不访问数据库；
    - 返回普通字符串，永远非空；
    - 失败也只返回固定中文模板，绝不回传原始字段或英文错误；
    - 最后再兜一层洁净度检查：万一模板被人改坏，也不会把内部东西放出去。
    """
    text = _plain(template_line(summary))
    if not text:
        text = _FALLBACK_TEMPLATES[STATUS_FAILED]
    # 双保险：模板是本地写死的，但"写死"不等于"写对"。
    if not is_safe_public_text(text):
        return _FALLBACK_TEMPLATES.get(summary.status, _FALLBACK_TEMPLATES[STATUS_FAILED])
    return text


def now_text(moment: datetime | None = None) -> str:
    """本机时间 → 模板里那种"2026年9月14日 星期一 18:30"的写法。

    放在这里而不是 loop 里，是因为它属于"措辞"而不是"路由"。
    """
    value = moment or datetime.now(timezone.utc).astimezone()
    weekday = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")[value.weekday()]
    return f"{value.year}年{value.month}月{value.day}日 {weekday} {value.hour:02d}:{value.minute:02d}"
