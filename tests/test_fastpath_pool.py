import pytest

from agent_core import fastpath as fast
from agent_core import fastpath_pool
from agent_core.contracts import ToolResult
from agent_core.steward import StewardNarrator


@pytest.mark.parametrize("intent,reason", [
    (fast.INTENT_WEATHER, "weather_unavailable"),
    (fast.INTENT_NAVIGATION, "location_unavailable"),
    (fast.INTENT_SEARCH, "connection_failed"),
])
def test_character_pool_does_not_replace_failure_reasons(intent, reason):
    summary = fast.SafeReadonlySummary(intent=intent, status=fast.STATUS_FAILED, reason_code=reason)
    assert fastpath_pool.get_character_fastpath_reply(summary) is None


def test_playback_pool_keeps_formatted_track_without_extra_brackets(monkeypatch):
    monkeypatch.setattr(fastpath_pool.random, "shuffle", lambda candidates: None)
    summary = fast.playback_summary(ToolResult("ok", {"status": "playing", "title": "说一句", "artist": "连诗雅"}))
    reply = fastpath_pool.get_character_fastpath_reply(summary)
    assert "《说一句》 - 连诗雅" in reply
    assert "《《" not in reply
    assert "电视" not in reply


def test_navigation_pool_leaves_fact_rich_results_to_neutral_templates():
    summary = fast.SafeReadonlySummary(intent=fast.INTENT_NAVIGATION, extra=(
        ("distance", "12 公里"), ("duration", "驾车约 25 分钟"),
    ))
    assert fastpath_pool.get_character_fastpath_reply(summary) is None


def test_system_information_pool_does_not_claim_device_health():
    summary = fast.system_info_summary({"system": "Windows", "hostname": "host"})
    assert fastpath_pool.get_character_fastpath_reply(summary) is None


def test_narrator_uses_neutral_templates_without_character_opt_in():
    summary = fast.playback_summary(ToolResult("ok", {"status": "playing", "title": "说一句"}))
    assert StewardNarrator().narrate_fast_result(summary) == fast.narrate(summary)


def test_freezing_weather_never_uses_hot_weather_template(monkeypatch):
    monkeypatch.setattr(fastpath_pool.random, "shuffle", lambda candidates: None)
    summary = fast.SafeReadonlySummary(intent=fast.INTENT_WEATHER, area_label="上海", extra=(
        ("condition", "阴"), ("temperature", "-5"),
    ))
    reply = fastpath_pool.get_character_fastpath_reply(summary)
    assert "热" not in reply
    assert "冷" in reply


def test_character_pool_rejects_internal_error_in_track(monkeypatch):
    monkeypatch.setattr(fastpath_pool.random, "shuffle", lambda candidates: None)
    summary = fast.SafeReadonlySummary(intent=fast.INTENT_PLAYBACK, items=("HTTP 500 connection failed",), extra=(("playing", "yes"),))
    assert fastpath_pool.get_character_fastpath_reply(summary) is None
