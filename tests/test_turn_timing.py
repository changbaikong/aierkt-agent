"""单轮耗时的分阶段日志：只写日志、不泄漏、不崩业务。"""

import logging

from agent_core.loop import AgentLoop
from agent_core.turn_timing import (
    STAGE_INTENT_ROUTE,
    STAGE_LOCATION_READ,
    STAGE_TOOL_FINISHED,
    STAGE_TURN_STARTED,
    TurnTimer,
    safe_token,
)


def test_safe_token_rejects_coordinates_and_free_text() -> None:
    """坐标"恰好"能通过字符集检查，所以纯数字要单独挡掉。"""
    assert safe_token("ok") == "ok"
    assert safe_token("location__current") == "location__current"
    assert safe_token("location__current.ok") == "location__current.ok"
    assert safe_token("invalid_weather_area") == "invalid_weather_area"
    assert safe_token("non_loopback_host") == "non_loopback_host"

    assert safe_token("31.2304") == "invalid"
    assert safe_token("-121.4737") == "invalid"
    assert safe_token("31.2304,121.4737") == "invalid"
    assert safe_token("阿姆斯特丹的天气怎么样") == "invalid"
    assert safe_token("Amap request failed with HTTP 500") == "invalid"


def test_timer_marks_elapsed_since_turn_start() -> None:
    """阶段记的是"从这一轮开始到现在"，不是相邻两点的差值。"""
    clock = [0.0]
    timer = TurnTimer("run-1", clock=lambda: clock[0])
    clock[0] = 0.15
    timer.mark(STAGE_TURN_STARTED)
    clock[0] = 2.0
    timer.mark(STAGE_INTENT_ROUTE, status="model")
    result = timer.finish()

    assert result["turn_id"] == "run-1"
    assert result["total_ms"] == 2000.0
    stages = {item["stage"]: item for item in result["stages"]}
    assert stages[STAGE_TURN_STARTED]["elapsed_ms"] == 150.0
    assert stages[STAGE_INTENT_ROUTE]["elapsed_ms"] == 2000.0
    assert stages[STAGE_INTENT_ROUTE]["status"] == "model"


def test_timer_logs_one_line_and_finishes_only_once(caplog) -> None:
    clock = [0.0]
    timer = TurnTimer("abc", clock=lambda: clock[0])
    clock[0] = 1.0
    with caplog.at_level(logging.INFO, logger="agent_core.turn_timing"):
        timer.finish()
        second = timer.finish()

    assert second == {}, "同一轮只收口一次，重复调用不能写第二条"
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "turn=abc" in message
    assert "total_ms=1000.0" in message


def test_timer_never_writes_coordinates_even_if_caller_passes_them(caplog) -> None:
    """防呆：调用方就算把坐标当 status 传进来，也进不了日志。"""
    clock = [0.0]
    timer = TurnTimer("abc", clock=lambda: clock[0])
    timer.mark(STAGE_LOCATION_READ, status="31.2304,121.4737")
    with caplog.at_level(logging.INFO, logger="agent_core.turn_timing"):
        timer.finish()

    message = caplog.records[0].getMessage()
    assert "31.2304" not in message
    assert "121.4737" not in message
    assert "/invalid" in message


def test_timer_ignores_marks_after_finish() -> None:
    clock = [0.0]
    timer = TurnTimer("abc", clock=lambda: clock[0])
    timer.finish()
    timer.mark(STAGE_TOOL_FINISHED, status="x")

    assert timer.marks() == ()


def test_mark_turn_is_a_no_op_without_a_timer() -> None:
    """计时是观测，没有 timer 时也不能抛 —— 业务不能因为观测缺失而崩。"""
    loop = AgentLoop.__new__(AgentLoop)

    AgentLoop._mark_turn(loop, STAGE_TURN_STARTED)
    AgentLoop._finish_turn_timing(loop)
