"""确定性快路径的管家表达（agent_core/fastpath.py + loop 接入）。

这一组用例钉的是**契约**，不是某几句话的具体措辞：
  1. 快路径零模型调用，也不产生过程气泡；
  2. 用户看到的永远是中文，JSON / 字段名 / 英文错误串进不来；
  3. 措辞层拿不到原始敏感字段（经纬度、完整地址、内部错误）；
  4. 状态分档正确（过期 ≠ 没开 ≠ 能力挂了）；
  5. 不同 Scope 不串线；长任务的表达完全不受影响。

第 3 条是整组里最要紧的：它测的不是"措辞函数恰好没写坐标"，
而是"措辞函数**在签名上**就收不到坐标"。所以这里既测行为，
也用探针对象直接证明 `narrate_fast_result` 全文规里没碰任何原始字段。
"""
import asyncio
import re

import pytest

from agent_core.contracts import AgentRequest, EFFECT_READ, ProviderResponse, Scope, ToolCall, ToolResult, ToolSpec
from agent_core import fastpath as fast
from agent_core.loop import AgentLoop
from agent_core.modules import ModuleRegistry
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.steward import PHASE_SEARCHING, StewardNarrator, StewardReporter
from agent_core.steward_progress import ProgressBoard, ProgressPolicy
from agent_core.tools import ToolRegistry
from agent_tools.modules import build_condition_task_module, build_search_module, build_task_module
from support.stage_phrases import StagePhraseProvider, stage_line


class ExplodingProvider(LLMProvider):
    """任何一次调用都视为失败 —— 用来证明快路径确实没碰模型。"""

    def __init__(self):
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append(messages)
        raise AssertionError("快路径不允许调用模型")


class RecordingSink:
    def __init__(self):
        self.messages = []

    def __call__(self, scope, message, stable_key):
        self.messages.append({"scope": scope, **message})
        return True


def _registry(name="location__current", handler=None):
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name=name,
        schema={"name": name, "description": name, "input_schema": {"type": "object", "properties": {}}},
        handler=handler or (lambda _arguments, _scope: {"status": "ok", "output": {}}),
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    ))
    return registry


def _loop(provider, registry, tmp_path, sink=None, scope=None, wording_provider=None):
    """``wording_provider`` 只负责过程气泡那句话的措辞，不给时沿用任务模型。"""
    return AgentLoop(
        provider,
        registry,
        scope or Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(str(tmp_path / "core.db")),
        confirmation_store=SQLiteConfirmationStore(str(tmp_path / "core.db")),
        steward=StewardReporter(
            StewardNarrator(wording_provider or provider), sink=sink or RecordingSink()
        ),
    )


def _tool_result(status="ok", output=None, error=""):
    return ToolResult(status=status, output=output if output is not None else {}, error=error)


# ----------------------------------------------------------------------
# 1. 位置：降维、状态分档
# ----------------------------------------------------------------------
def test_location_summary_drops_raw_coordinates():
    """摘要里不许有经纬度；只有区域级描述与区间精度。"""
    summary = fast.location_summary(_tool_result(output={
        "latitude": 31.2304,
        "longitude": 121.4737,
        "accuracy_m": 18.5,
        "age_seconds": 12.0,
        "source": "browser",
        "captured_at": "2026-09-14T08:59:03+00:00",
    }))

    dumped = repr(summary)
    assert "31.2304" not in dumped
    assert "121.4737" not in dumped
    assert "browser" not in dumped
    assert summary.area_label == "上海附近"
    assert summary.accuracy_text == "大约 20 米"
    assert summary.freshness_text == "刚刚更新"


def test_location_reply_is_plain_chinese_without_leaks():
    summary = fast.location_summary(_tool_result(output={
        "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 18.5, "age_seconds": 5.0,
    }))
    reply = fast.narrate(summary)

    assert reply == "你现在共享的位置在上海附近，定位精度大约 20 米，刚刚更新。"
    for leak in ("latitude", "longitude", "age_seconds", "{", "}", "31.2304", "121.4737"):
        assert leak not in reply


def test_location_reply_without_freshness_omits_the_clause_cleanly():
    """没有 age_seconds 时，新鲜度整段消失 —— 不许留下孤零零的逗号。"""
    summary = fast.location_summary(_tool_result(output={
        "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 18.5,
    }))
    reply = fast.narrate(summary)

    assert summary.freshness_text == ""
    assert reply == "你现在共享的位置在上海附近，定位精度大约 20 米。"
    assert "，。" not in reply
    assert "，，" not in reply


def test_location_reply_without_accuracy_still_reads_naturally():
    """浏览器不给精度是常态，不能因此把整句说成半截话。"""
    summary = fast.location_summary(_tool_result(output={"latitude": 31.2304, "longitude": 121.4737}))
    reply = fast.narrate(summary)

    assert "上海附近" in reply
    assert "精度" not in reply
    assert reply.endswith("。")
    assert "，。" not in reply


def test_location_reply_keeps_accuarcy_when_freshness_missing():
    """只有新鲜度缺失：精度照说，且不出现空逗号。"""
    summary = fast.location_summary(_tool_result(output={
        "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 18.5,
    }))
    reply = fast.narrate(summary)

    assert "定位精度大约 20 米" in reply
    assert "，。" not in reply


@pytest.mark.parametrize("meters,expected", [
    (5.0, "大约 10 米"),
    (15.0, "大约 10 米"),
    (16.0, "大约 20 米"),
    (30.0, "大约 20 米"),
    (45.0, "大约 50 米"),
    (120.0, "大约 100 米"),
    (300.0, "大约几百米"),
    (850.0, "大约一公里范围内"),
    (1000.0, "大约一公里范围内"),
    (1000.1, "范围比较大"),
    (5000.0, "范围比较大"),
])
def test_accuracy_text_tiers(meters, expected):
    assert fast.accuracy_text(meters) == expected


@pytest.mark.parametrize("meters", [5.0, 16.0, 45.0, 120.0, 300.0, 850.0, 5000.0])
def test_accuracy_prefix_is_not_duplicated_in_the_sentence(meters):
    """精度档位是名词短语，被模板接在"定位精度"后面；不许拼出"定位精度定位范围比较大"。"""
    summary = fast.location_summary(_tool_result(output={
        "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": meters, "age_seconds": 5.0,
    }))
    reply = fast.narrate(summary)

    assert reply.count("定位") == 1
    assert "定位精度定位" not in reply
    assert reply == f"你现在共享的位置在上海附近，定位精度{fast.accuracy_text(meters)}，刚刚更新。"
    assert "，，" not in reply and "，。" not in reply


def test_city_scale_accuracy_is_not_described_as_unusable():
    """850 米在城市里是能用的位置，不能说成"范围比较大"。"""
    summary = fast.location_summary(_tool_result(output={
        "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 850.0, "age_seconds": 10.0,
    }))
    reply = fast.narrate(summary)

    assert reply == "你现在共享的位置在上海附近，定位精度大约一公里范围内，刚刚更新。"
    assert "范围比较大" not in reply


@pytest.mark.parametrize("age,expected", [
    (0.0, "刚刚更新"),
    (60.0, "刚刚更新"),
    (61.0, "几分钟前更新"),
    (300.0, "几分钟前更新"),
    (301.0, "有一会儿没更新"),
])
def test_freshness_text_tiers(age, expected):
    assert fast.freshness_text(age) == expected


@pytest.mark.parametrize("bad", [None, "", "abc", -1.0, float("nan")])
def test_freshness_text_empty_for_unknown(bad):
    assert fast.freshness_text(bad) == ""


def test_unknown_area_does_not_guess_city():
    """认不出城市时宁可含糊，也不能猜错。"""
    summary = fast.location_summary(_tool_result(output={"latitude": 0.0, "longitude": 0.0, "accuracy_m": 30}))
    reply = fast.narrate(summary)

    assert "上海" not in reply and "北京" not in reply
    assert "你那边" in reply


@pytest.mark.parametrize("status,error,needle", [
    # 没开共享：明确指向"开位置共享"。
    ("unavailable", "No recent browser location is available.", "还没开位置共享"),
    # 过期：指向"重新共享"，不能说成没开过。
    ("stale", "The shared browser location is stale and needs to be refreshed.", "有一会儿没更新"),
    ("failed", "socket timeout", "位置服务没有响应"),
    ("failed", "Traceback (most recent call last):\n  File \"x.py\"", "位置服务没有响应"),
])
def test_location_failure_statuses_use_chinese_templates(status, error, needle):
    summary = fast.location_summary(_tool_result(status=status, output={"reason": error}, error=error))
    reply = fast.narrate(summary)

    assert needle in reply
    # 原始英文串、内部字段名、异常栈一个都不能漏。
    for leak in ("No recent", "timeout", "Traceback", "File \"", "location_unavailable",
                 "location_stale", "stale"):
        assert leak not in reply


def test_location_stale_reason_code_maps_to_stale_status():
    """真实链路：工具返回 status=stale + reason=location_stale，必须落进 stale 分档。"""
    summary = fast.location_summary(_tool_result(
        status="stale",
        output={"reason": "location_stale", "age_seconds": 900.0},
    ))

    assert summary.status == fast.STATUS_STALE
    assert summary.confirmed is False


def test_location_unavailable_reason_never_maps_to_stale():
    """没开共享不能因为字符串里带别的词就误判成过期。"""
    summary = fast.location_summary(_tool_result(
        status="unavailable", output={"reason": "location_unavailable"},
    ))

    assert summary.status == fast.STATUS_UNAVAILABLE
    assert summary.status != fast.STATUS_STALE


def test_stale_location_never_claims_current_position():
    """位置过期时不能说"你现在就在某处" —— 那是刚才的事。"""
    summary = fast.location_summary(_tool_result(
        status="stale",
        output={"reason": "location_stale", "age_seconds": 900.0},
    ))
    reply = fast.narrate(summary)

    assert "你现在" not in reply
    assert "上海" not in reply
    assert "重新共享" in reply
    assert summary.confirmed is False


def test_stale_location_never_leaks_raw_coordinates():
    """过期分支也不能漏坐标（工具层刻意不返回坐标，这里再兜一道）。"""
    summary = fast.location_summary(_tool_result(
        status="stale",
        output={"reason": "location_stale", "age_seconds": 900.0,
                "latitude": 31.2304, "longitude": 121.4737},
    ))
    reply = fast.narrate(summary)

    for leak in ("latitude", "longitude", "31.2304", "121.4737", "age_seconds", "900"):
        assert leak not in reply


# ----------------------------------------------------------------------
# 2. 措辞层的输入面：签名上就够不着原始字段
# ----------------------------------------------------------------------
def test_narrate_never_touches_raw_result_fields():
    """用探针对象证明：措辞函数只读 SafeReadonlySummary 的白名单字段。

    `SafeReadonlySummary` 一旦被加了 `raw` / `payload` 这类后门字段，
    这个用例会失败 —— 那是刻意的，它就是那道防盗门。
    """
    touched: list[str] = []

    class Probe(dict):
        def get(self, key, default=None):
            touched.append(str(key))
            return super().get(key, default)

        def __getitem__(self, key):
            touched.append(str(key))
            raise KeyError(key)

    # 探针按位置工具**现在的**输出形状造：地名 + 原始数字。
    # 工具改成返回地名之后，这里不再需要 latitude / longitude。
    raw = Probe({"place_label": "上海市浦东新区", "accuracy_m": 20, "age_seconds": 5})
    summary = fast.location_summary(_tool_result(output=raw))
    fast.narrate(summary)

    # 构造摘要是唯一被允许读原始字段的地方，而它只读白名单里这几个。
    # 白名单收紧而不是放宽：多读一个原始字段，这道门就少守一格。
    assert set(touched) <= {"place_label", "accuracy_m", "age_seconds", "reason"}

    allowed = set(fast.SafeReadonlySummary.__dataclass_fields__)
    for forbidden in ("raw", "payload", "output", "result", "data", "source"):
        assert forbidden not in allowed


def test_safe_readonly_summary_has_no_backdoor_field():
    fields = set(fast.SafeReadonlySummary.__dataclass_fields__)
    assert fields == {
        "intent", "status", "area_label", "accuracy_text", "freshness_text",
        "items", "total", "moment_text", "reason_code", "extra",
    }


# ----------------------------------------------------------------------
# 3. 其他只读快路径：同一套规则
# ----------------------------------------------------------------------
def test_time_reply_is_plain_chinese():
    summary = fast.time_summary({
        "local_time": "2026-09-14T18:30:00+08:00",
        "timezone": "Asia/Shanghai",
    })
    reply = fast.narrate(summary)

    assert reply == "现在是2026年9月14日 星期一 18:30（Asia/Shanghai）。"
    assert "T18:30" not in reply
    assert "{" not in reply


def test_time_reply_falls_back_when_timestamp_unreadable():
    reply = fast.narrate(fast.time_summary({"local_time": "not-a-timestamp"}))

    assert reply == "我这会儿读不到本机时间。稍后再试一次会更合适。"


def test_system_info_reply_stays_in_chinese():
    summary = fast.system_info_summary({
        "system": "Windows", "release": "11", "machine": "AMD64",
        "hostname": "desk", "path": r"C:\work",
    })
    reply = fast.narrate(summary)

    assert "系统是 Windows 11（AMD64）" in reply
    assert "主机名是 desk" in reply
    assert "工作目录在" in reply
    assert "{" not in reply


@pytest.mark.parametrize("intent,items,total,needle", [
    (fast.INTENT_TASKS, ("写周报", "查机票"), 2, "你手上现在有2件事"),
    (fast.INTENT_CONDITIONS, ("下雨提醒",), 1, "你现在挂着1个条件提醒"),
])
def test_list_replies_use_whitelisted_titles_only(intent, items, total, needle):
    summary = fast.SafeReadonlySummary(intent=intent, status=fast.STATUS_OK, items=items, total=total)
    reply = fast.narrate(summary)

    assert needle in reply
    assert "写周报" in reply or "下雨提醒" in reply
    assert "{" not in reply


def test_task_list_summary_drops_internal_fields():
    """任务对象里的 request_text / allowed_tools / last_error 不该进摘要。"""
    summary = fast.task_list_summary(_tool_result(output={"tasks": [{
        "task_id": "t1",
        "title": "每晚总结",
        "request_text": "把今天的对话整理一下",
        "allowed_tools": ["search__web", "file__list"],
        "status": "scheduled",
        "last_error": "Traceback: boom",
    }]}))

    assert summary.total == 1
    assert "每晚总结" in summary.items[0]
    dumped = repr(summary)
    for leak in ("allowed_tools", "search__web", "Traceback", "request_text"):
        assert leak not in dumped


@pytest.mark.parametrize("intent,needle", [
    (fast.INTENT_TASKS, "没有挂着的定时任务"),
    (fast.INTENT_CONDITIONS, "没有挂着的条件提醒"),
])
def test_empty_list_replies_are_chinese(intent, needle):
    summary = fast.task_list_summary(_tool_result(output={"tasks": []}), intent=intent)
    reply = fast.narrate(summary)

    assert summary.status == fast.STATUS_EMPTY
    assert needle in reply


def test_playback_reply_distinguishes_playing_and_paused():
    playing = fast.playback_summary(_tool_result(output={"status": "playing", "title": "夜曲", "artist": "周杰伦"}))
    paused = fast.playback_summary(_tool_result(output={"status": "paused", "title": "夜曲", "artist": "周杰伦"}))

    assert fast.narrate(playing) == "现在正在放《夜曲》 - 周杰伦。"
    assert fast.narrate(paused) == "现在停在一半：《夜曲》 - 周杰伦。"


def test_playback_empty_and_failed_are_chinese():
    idle = fast.playback_summary(_tool_result(output={"status": "stopped"}))
    failed = fast.playback_summary(_tool_result(status="failed", error="connection refused"))

    assert fast.narrate(idle) == "现在没有在放歌。"
    assert fast.narrate(failed) == "读播放器状态的时候卡住了。稍后再试一次会更合适。"
    assert "connection" not in fast.narrate(failed)


def test_fallback_templates_never_leak_fields():
    """所有兜底模板都要能过洁净度检查 —— 它们是最容易被改坏的一层。"""
    for status in (fast.STATUS_OK, fast.STATUS_EMPTY, fast.STATUS_STALE,
                   fast.STATUS_UNAVAILABLE, fast.STATUS_FAILED):
        text = fast._FALLBACK_TEMPLATES[status]
        assert fast.is_safe_public_text(text), status


def test_is_safe_public_text_rejects_raw_errors_and_internal_terms():
    assert fast.is_safe_public_text("你现在共享的位置在上海附近。")
    assert not fast.is_safe_public_text("No recent browser location is available.")
    assert not fast.is_safe_public_text("latitude: 31.23")
    assert not fast.is_safe_public_text("Traceback (most recent call last)")
    assert not fast.is_safe_public_text('{"status": "ok"}')


# ----------------------------------------------------------------------
# 4. 端到端：loop 里真的零模型、零气泡、中文回复
# ----------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "我在哪里", "我的位置", "我在哪？", "我现在在哪", "我在什么位置？",
])
async def test_location_fast_path_calls_no_model_and_writes_no_bubble(question, tmp_path):
    provider = ExplodingProvider()
    sink = RecordingSink()
    registry = _registry(handler=lambda _a, _s: {"status": "ok", "output": {
        "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 20.0, "age_seconds": 3.0,
    }})
    loop = _loop(provider, registry, tmp_path, sink=sink)

    outcome = await loop.run(AgentRequest(question, scope=loop.scope))

    assert provider.calls == []
    assert sink.messages == []
    assert outcome.reply == "你现在共享的位置在上海附近，定位精度大约 20 米，刚刚更新。"
    assert outcome.reply.startswith("你现在共享的位置")


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "你在哪", "你现在在哪", "你在哪里", "你在哪儿", "你住哪", "你住在哪里",
    "你在什么地方", "你的位置",
])
async def test_second_person_question_answers_about_the_agent_itself(question, tmp_path):
    """「你在哪」问的是 Agent 自己 —— 它住在这台机器里。

    这是产品语义，不是技术细节：用户问"你在哪"得到"你现在共享的位置在上海附近"
    是答非所问。正确回答是第一人称的"我住在你这台电脑里"，且**不调模型**。
    """
    provider = ExplodingProvider()
    sink = RecordingSink()
    registry = _registry(name="system__local_info", handler=lambda _a, _s: {"status": "ok", "output": {
        "system": "Windows", "hostname": "DESKTOP-ABC", "source": "local",
    }})
    loop = _loop(provider, registry, tmp_path, sink=sink)

    outcome = await loop.run(AgentRequest(question, scope=loop.scope))

    assert provider.calls == []
    assert sink.messages == []
    assert outcome.reply == "我就住在你这台电脑里，跑在 Windows 上。"
    # 不能把用户的位置当答案，也不能泄漏主机名
    assert "共享的位置" not in outcome.reply
    assert "DESKTOP-ABC" not in outcome.reply


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["你在哪", "你住在哪里", "你的位置"])
async def test_second_person_question_never_returns_user_location(question, tmp_path):
    """即使这台机器同时有用户的位置，也不能拿它答"你在哪"。"""
    provider = ExplodingProvider()
    calls: list[str] = []

    def handler(_a, _s):
        calls.append("called")
        return {"status": "ok", "output": {
            "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 18.5,
            "age_seconds": 3.0, "system": "Windows",
        }}

    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="system__local_info",
        schema={"name": "system__local_info", "description": "info",
                "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk="low", effect=EFFECT_READ, deterministic_route_allowed=True,
    ))
    registry.register(ToolSpec(
        name="location__current",
        schema={"name": "location__current", "description": "loc",
                "input_schema": {"type": "object", "properties": {}}},
        handler=handler,
        risk="low", effect=EFFECT_READ, deterministic_route_allowed=True,
    ))
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest(question, scope=loop.scope))

    assert provider.calls == []
    assert "31.2304" not in outcome.reply
    assert "上海" not in outcome.reply
    assert "电脑" in outcome.reply


@pytest.mark.asyncio
async def test_host_question_degrades_gracefully_without_system(tmp_path):
    """拿不到系统名时也不能说出"我住在  里"这种半截话。"""
    provider = ExplodingProvider()
    registry = _registry(name="system__local_info",
                         handler=lambda _a, _s: {"status": "ok", "output": {"hostname": "x"}})
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("你在哪", scope=loop.scope))

    assert provider.calls == []
    assert outcome.reply == "我住在这台机器里，具体环境这会儿说不太准。"
    assert "  " not in outcome.reply


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["你在干嘛", "你在干什么", "你为什么在这", "你好"])
async def test_questions_about_the_agent_that_are_not_about_dwelling(question, tmp_path):
    """问行为、问原因、打招呼都不是问"住哪"，不能被 host 快路径吃掉。"""
    provider = ExplodingProvider()
    registry = _registry()
    loop = _loop(provider, registry, tmp_path)

    await loop.run(AgentRequest(question, scope=loop.scope))

    assert provider.calls != [], "非「住哪」的问句应交给模型"


@pytest.mark.parametrize("raw,expected", [
    ("Windows", "Windows"),
    ("Linux", "Linux"),
    ("Darwin", "macOS"),
    ("windows", "Windows"),
    ("Plan9", "Plan9"),
])
def test_friendly_system_name(raw, expected):
    """Darwin 是 macOS 的内核名，直接说出来没人懂。"""
    assert fast._friendly_system(raw) == expected


def test_host_summary_keeps_only_the_platform():
    """host 摘要只保留平台名，主机名/路径不进措辞层。"""
    summary = fast.host_summary({"system": "Windows", "hostname": "SECRET-PC",
                                 "path": "C:/Users/secret"})

    dumped = repr(summary)
    assert "SECRET-PC" not in dumped
    assert "secret" not in dumped
    assert "Windows" in dumped


def test_host_summary_is_empty_without_system():
    summary = fast.host_summary({"hostname": "x"})
    assert summary.status == fast.STATUS_EMPTY


@pytest.mark.asyncio
async def test_stale_location_goes_through_stale_branch_without_model(tmp_path):
    """过期位置：真链路走 stale 分支，说"重新共享"，不说"还没开共享"。"""
    provider = ExplodingProvider()
    sink = RecordingSink()
    registry = _registry(handler=lambda _a, _s: {
        "status": "stale",
        "output": {"reason": "location_stale", "age_seconds": 900.0},
        "error": "The shared browser location is stale and needs to be refreshed.",
    })
    loop = _loop(provider, registry, tmp_path, sink=sink)

    outcome = await loop.run(AgentRequest("我在哪里", scope=loop.scope))

    assert provider.calls == []
    assert sink.messages == []
    assert "有一会儿没更新" in outcome.reply
    assert "重新共享" in outcome.reply
    assert "还没开位置共享" not in outcome.reply
    assert "stale" not in outcome.reply
    assert "The shared" not in outcome.reply


@pytest.mark.asyncio
async def test_never_shared_location_says_not_shared(tmp_path):
    provider = ExplodingProvider()
    registry = _registry(handler=lambda _a, _s: {
        "status": "unavailable",
        "output": {"reason": "location_unavailable"},
        "error": "No recent browser location is available.",
    })
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("我在哪里", scope=loop.scope))

    assert provider.calls == []
    assert "还没开位置共享" in outcome.reply
    assert "有一会儿没更新" not in outcome.reply
    assert "No recent" not in outcome.reply


@pytest.mark.asyncio
async def test_stale_then_refreshed_location_recovers_to_ok(tmp_path):
    """过期 → 重新更新 → 恢复 ok，措辞也要跟着变回来。"""
    provider = ExplodingProvider()
    state = {"status": "stale"}

    def handler(_a, _s):
        if state["status"] == "stale":
            return {"status": "stale", "output": {"reason": "location_stale", "age_seconds": 900.0}}
        return {"status": "ok", "output": {
            "latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 18.5, "age_seconds": 2.0,
        }}

    registry = _registry(handler=handler)
    loop = _loop(provider, registry, tmp_path)

    stale_reply = (await loop.run(AgentRequest("我在哪里", scope=loop.scope))).reply
    state["status"] = "ok"
    fresh_reply = (await loop.run(AgentRequest("我在哪里", scope=loop.scope))).reply

    assert "有一会儿没更新" in stale_reply
    assert fresh_reply == "你现在共享的位置在上海附近，定位精度大约 20 米，刚刚更新。"
    assert provider.calls == []


@pytest.mark.asyncio
async def test_longer_sentence_is_not_a_location_query(tmp_path):
    """「我在哪儿吃饭好」不是位置查询，不能被路由吃掉。"""
    provider = ExplodingProvider()
    registry = _registry()
    loop = _loop(provider, registry, tmp_path)

    await loop.run(AgentRequest("我在哪儿吃饭好", scope=loop.scope))

    assert provider.calls != [], "长问句应交给模型，不该被位置快路径截胡"


@pytest.mark.asyncio
async def test_readonly_failure_does_not_leak_english_error(tmp_path):
    provider = ExplodingProvider()
    registry = _registry(handler=lambda _a, _s: {
        "status": "failed",
        "output": {},
        "error": "Connection refused while reading /api/local-info",
    })
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("我在哪里", scope=loop.scope))

    assert provider.calls == []
    assert "Connection" not in outcome.reply
    assert "/api/" not in outcome.reply
    assert "位置服务没有响应" in outcome.reply


@pytest.mark.asyncio
async def test_time_fast_path_is_plain_chinese(tmp_path):
    provider = ExplodingProvider()
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="system__local_info",
        schema={"name": "system__local_info", "description": "info", "input_schema": {"type": "object", "properties": {}}},
        handler=lambda _a, _s: {"status": "ok", "output": {
            "local_time": "2026-09-14T18:30:00+08:00", "timezone": "Asia/Shanghai",
        }},
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    ))
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("现在几点", scope=loop.scope))

    assert provider.calls == []
    assert "2026年9月14日" in outcome.reply
    assert "T18:30" not in outcome.reply
    assert "{" not in outcome.reply


@pytest.mark.asyncio
async def test_task_list_fast_path_is_plain_chinese(tmp_path):
    provider = ExplodingProvider()
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="tasks__list",
        schema={"name": "tasks__list", "description": "list", "input_schema": {"type": "object", "properties": {}}},
        handler=lambda _a, _s: {"status": "ok", "output": {"tasks": [
            {"task_id": "t1", "title": "每晚总结", "request_text": "内部细节", "allowed_tools": ["search__web"]},
        ]}},
        risk="low",
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    ))
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("我的任务列表", scope=loop.scope))

    assert provider.calls == []
    assert "每晚总结" in outcome.reply
    assert "search__web" not in outcome.reply
    assert "内部细节" not in outcome.reply


@pytest.mark.asyncio
async def test_fast_path_reply_never_contains_json_braces(tmp_path):
    """回归：以前未识别的只读工具会把 payload 原样 json.dumps 出去。"""
    provider = ExplodingProvider()
    registry = _registry(
        name="weather__now",
        handler=lambda _a, _s: {"status": "ok", "output": {"temp": 21, "city": "上海"}},
    )
    # 未识别的工具名不会命中只读正则，这里直接调处理函数验证降级行为。
    loop = _loop(provider, registry, tmp_path)
    summary = loop._readonly_summary("weather__now", {}, _tool_result(output={"temp": 21, "city": "上海"}))
    reply = loop._fast_reply(summary)

    assert "{" not in reply
    assert "temp" not in reply


@pytest.mark.asyncio
async def test_unknown_question_still_goes_to_model(tmp_path):
    """快路径不吞掉普通问题 —— 措辞层不参与路由决策。

    ExplodingProvider 一被调用就抛异常，而 AgentLoop 会把 provider 异常
    收成 failed 结果（这是有意的降级）。所以这里断言的是"模型确实被叫过"，
    而不是"异常冒到调用方"。
    """
    provider = ExplodingProvider()
    registry = _registry()
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("陪我说说话", scope=loop.scope))

    assert provider.calls != []          # 走了模型
    assert outcome.status == "failed"    # 异常被降级成失败，没有崩出去


# ----------------------------------------------------------------------
# 5. Scope 隔离
# ----------------------------------------------------------------------
def test_location_summaries_do_not_cross_scopes():
    """两个 Scope 各自的结果互不影响（摘要本身不带 Scope，靠调用方隔离）。"""
    store_a = fast.location_summary(_tool_result(output={"latitude": 31.2304, "longitude": 121.4737}))
    store_b = fast.location_summary(_tool_result(output={"latitude": 39.9042, "longitude": 116.4074}))

    assert store_a.area_label == "上海附近"
    assert store_b.area_label == "北京附近"
    assert store_a.area_label != store_b.area_label


@pytest.mark.asyncio
async def test_loop_passes_its_own_scope_result_only(tmp_path):
    """同一个工具、两个 Scope 并发跑，各自只拿到自己那份。"""
    provider = ExplodingProvider()
    seen: list[str] = []

    def handler(_arguments, scope):
        # 每个 Scope 返回不同的城市，验证结果不串线。
        if scope.conversation_id == "chat-1":
            return {"status": "ok", "output": {"latitude": 31.2304, "longitude": 121.4737, "accuracy_m": 20}}
        return {"status": "ok", "output": {"latitude": 39.9042, "longitude": 116.4074, "accuracy_m": 20}}

    async def run(conversation_id):
        registry = _registry(handler=handler)
        loop = _loop(provider, registry, tmp_path, scope=Scope("alice", "web", conversation_id, "companion"))
        outcome = await loop.run(AgentRequest("我在哪里", scope=loop.scope))
        seen.append(outcome.reply)

    await asyncio.gather(run("chat-1"), run("chat-2"))

    assert any("上海附近" in text for text in seen)
    assert any("北京附近" in text for text in seen)
    # 两条回复各自只提一个城市，没有互相污染。
    for text in seen:
        assert not ("上海附近" in text and "北京附近" in text)


# ----------------------------------------------------------------------
# 6. 长任务表达不受影响
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_long_task_still_gets_progress_messages(tmp_path):
    """快路径改造不能把长任务的过程气泡一起改没：等得够久，仍然会说真实阶段。"""
    class TwoStepProvider(LLMProvider):
        def __init__(self):
            self.calls = []
            self.index = 0

        async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
            self.calls.append(messages)
            if tools is None:
                return ProviderResponse(
                    message={"role": "assistant", "content": "嗯，交给我吧。我先把这篇论文找出来看看。"},
                    usage={"prompt_tokens": 1, "completion_tokens": 1},
                )
            self.index += 1
            if self.index == 1:
                return ProviderResponse(message={
                    "role": "assistant", "content": "",
                    "tool_calls": [{"id": "c1", "type": "function",
                                    "function": {"name": "search__web", "arguments": "{}"}}],
                }, usage={"prompt_tokens": 1, "completion_tokens": 1})
            return ProviderResponse(
                message={"role": "assistant", "content": "看完了，结论是……"},
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )

    provider = TwoStepProvider()
    registry = _registry(name="search__web", handler=lambda _a, _s: {"status": "ok", "output": {"title": "论文 A"}})
    sink = RecordingSink()
    # 任务本身照旧走 TwoStepProvider，只把过程措辞换成测试替身（模型现场生成那句话）。
    loop = _loop(
        provider, registry, tmp_path, sink=sink, wording_provider=StagePhraseProvider(),
    )
    # 等待门槛压成 0：这里验的是"长任务仍然有过程句"，不是密度参数本身。
    loop._progress_board = ProgressBoard(policy=ProgressPolicy(first_wait_seconds=0.0, min_gap_seconds=0.0))

    await loop.run(AgentRequest("你去看一下这篇论文", scope=loop.scope))

    phases = [item["phase"] for item in sink.messages]
    assert phases == [PHASE_SEARCHING]
    assert sink.messages[0]["content"] == stage_line(PHASE_SEARCHING)  # 文案来自（替身）模型
    assert "论文" not in sink.messages[0]["content"]   # 阶段句不照抄用户的问题


@pytest.mark.asyncio
async def test_narrate_fast_result_never_calls_provider():
    """直接对 narrate_fast_result 断言：模型一次都不许被叫到。"""
    provider = ExplodingProvider()
    narrator = StewardNarrator(provider, persona="爱尔奎特")

    reply = narrator.narrate_fast_result(fast.SafeReadonlySummary(
        intent=fast.INTENT_LOCATION, status=fast.STATUS_OK,
        area_label="上海附近", accuracy_text="大约 20 米",
    ))

    assert provider.calls == []
    assert reply == "你现在共享的位置在上海附近，定位精度大约 20 米。"


def test_narrate_fast_result_returns_chinese_on_broken_summary():
    """摘要被构造坏时也必须退回中文模板，不能抛异常、不能漏字段。"""
    narrator = StewardNarrator(None)
    reply = narrator.narrate_fast_result(fast.SafeReadonlySummary(
        intent=fast.INTENT_LOCATION, status=fast.STATUS_OK, area_label="",
    ))

    assert reply
    assert "{" not in reply
    assert re.search(r"[\u4e00-\u9fff]", reply)


# ----------------------------------------------------------------------
# 7. 正式模块生成的 list 工具必须真的进快路径
# ----------------------------------------------------------------------
# 上一轮的回归形态很隐蔽：测试里手工构造的 ToolSpec 带了
# effect=read + deterministic_route_allowed，于是快路径用例全绿；
# 而**正式模块**（make_task_tools / make_condition_task_tools）产出的 list
# 工具没带这两个标记，真实链路上照样回落给模型。
# 所以这一组只用模块工厂 + 命名空间注册，一个手工 spec 都不许有。
def _stub_search_tool() -> ToolSpec:
    """条件提醒模块要求的授权工具 search__web（仅作为依赖，不是被测对象）。"""
    return ToolSpec(
        name="web",
        schema={"type": "function", "function": {"name": "web", "description": "search",
                                                  "parameters": {"type": "object", "properties": {}}}},
        handler=lambda _arguments, _scope: {"status": "ok", "output": {"results": []}},
        risk="low",
    )


def _official_registry(tmp_path, *, with_conditions: bool):
    """用正式模块工厂搭注册表，返回 (注册表, 任务服务, 条件提醒服务)。"""
    modules = ModuleRegistry()
    ids = {"tasks"}
    if with_conditions:
        ids |= {"conditions", "search"}
        modules.register(build_search_module(_stub_search_tool))

    def registry_provider():
        return modules.tools_for(set(ids))

    task_spec, task_service = build_task_module(
        db_path=tmp_path / "tasks.db", registry_provider=registry_provider
    )
    modules.register(task_spec)
    condition_service = None
    if with_conditions:
        condition_spec, condition_service = build_condition_task_module(
            db_path=tmp_path / "conditions.db", registry_provider=registry_provider
        )
        modules.register(condition_spec)
    return modules.tools_for(set(ids)), task_service, condition_service


@pytest.mark.asyncio
async def test_official_task_list_tool_stays_on_the_fast_path(tmp_path):
    """「我的任务列表」用**正式** tasks 模块也要零模型调用。"""
    provider = ExplodingProvider()
    registry, service, _ = _official_registry(tmp_path, with_conditions=False)
    await service.create_task(
        scope=Scope("alice", "web", "chat-1", "companion"),
        title="每晚总结", request_text="内部细节", delivery_mode="notify",
        schedule={"run_at": "2030-01-01T09:00:00+00:00"}, allowed_tools=[],
    )
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("我的任务列表", scope=loop.scope))

    assert provider.calls == [], "正式 list 工具没进快路径，回落给模型了"
    assert "每晚总结" in outcome.reply
    assert "内部细节" not in outcome.reply
    assert "search__web" not in outcome.reply


@pytest.mark.asyncio
async def test_official_condition_list_tool_stays_on_the_fast_path(tmp_path):
    """「有哪些条件提醒」同样零模型调用，且只报标题与数量。"""
    provider = ExplodingProvider()
    registry, _, condition_service = _official_registry(tmp_path, with_conditions=True)
    await condition_service.create_weather_task(
        scope=Scope("alice", "web", "chat-1", "companion"),
        title="晴天跑步", request_text="天气好时提醒我去跑步", city="上海",
    )
    loop = _loop(provider, registry, tmp_path)

    outcome = await loop.run(AgentRequest("有哪些条件提醒", scope=loop.scope))

    assert provider.calls == [], "正式条件提醒 list 工具没进快路径"
    assert "晴天跑步" in outcome.reply
    assert "天气好时提醒我去跑步" not in outcome.reply
    assert "search__web" not in outcome.reply


def test_task_list_summary_keeps_only_titles_and_total():
    """摘要边界：正式 list 输出里的内部字段一个都不许进措辞层。"""
    summary = fast.task_list_summary(_tool_result(output={"tasks": [
        {"task_id": "t1", "title": "每晚总结", "request_text": "内部细节",
         "allowed_tools": ["search__web"], "last_error": "RuntimeError: boom",
         "schedule": {"kind": "cron"}, "delivery_mode": "execute"},
    ]}))

    dumped = repr(summary)
    for leak in ("内部细节", "search__web", "RuntimeError", "schedule", "execute"):
        assert leak not in dumped
    assert summary.items == ("每晚总结",)
    assert summary.total == 1
