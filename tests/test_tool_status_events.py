"""工具执行状态事件。

方案第七部分的两条判据：

* 每条确定性工具路径都会发出"开始"和"结束"事件；
* 网页事件接口只公开白名单字段。

这个文件守的主要是第二条 —— 因为这条以前是**原样透传**的，
而模型路径的 `tool.started` 里带着 `arguments`（位置类工具的参数就是坐标）。
"""

import pytest

from agent_core.contracts import Scope, ToolCall, ToolResult
from agent_core.events import EventType, InMemoryEventBus
from agent_core.loop import AgentLoop


def _bare_loop(event_bus):
    """只装配发事件用得到的那两个属性，不跑 __init__（那要一大堆依赖）。"""
    loop = AgentLoop.__new__(AgentLoop)
    loop.event_bus = event_bus
    loop.scope = Scope(owner_id="acct", channel="web", conversation_id="conv")
    return loop


@pytest.mark.asyncio
async def test_tool_status_carries_name_and_id_but_never_arguments() -> None:
    bus = InMemoryEventBus()
    seen = []
    for event_type in (EventType.TOOL_STARTED, EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
        bus.subscribe(event_type, lambda event: seen.append(event))
    loop = _bare_loop(bus)
    call = ToolCall("call-1", "location__current", {"latitude": 31.2304, "longitude": 121.4737})

    await AgentLoop._emit_tool_status(loop, call)
    await AgentLoop._emit_tool_status(loop, call, ToolResult("ok", {"city": "上海市"}))

    assert [event.type for event in seen] == [EventType.TOOL_STARTED, EventType.TOOL_COMPLETED]
    for event in seen:
        assert event.data["tool_name"] == "location__current"
        assert event.data["tool_call_id"] == "call-1"
        assert "arguments" not in event.data, "工具参数（位置类就是坐标）不能进事件"
    assert "31.2304" not in str([event.data for event in seen])


@pytest.mark.asyncio
async def test_not_ok_result_is_reported_as_a_failed_event_with_its_kind() -> None:
    bus = InMemoryEventBus()
    seen = []
    bus.subscribe(EventType.TOOL_FAILED, lambda event: seen.append(event))
    loop = _bare_loop(bus)
    call = ToolCall("call-2", "weather__query", {"area": "310115"})

    await AgentLoop._emit_tool_status(
        loop, call,
        ToolResult("failed", {"reason": "weather_unavailable"}, "weather_unavailable", "transient"),
    )

    assert len(seen) == 1, "非 ok 的结果要发失败事件，前端据此把状态清掉"
    assert seen[0].data["status"] == "failed"
    assert seen[0].data["error_kind"] == "transient"


@pytest.mark.asyncio
async def test_tool_status_is_a_no_op_without_an_event_bus() -> None:
    """过程状态是观感，没有总线时不能把这一轮带崩。"""
    loop = _bare_loop(None)

    await AgentLoop._emit_tool_status(loop, ToolCall("c", "n", {}))


def test_public_event_data_keeps_only_the_tool_whitelist() -> None:
    import web_api

    published = web_api._public_event_data(EventType.TOOL_STARTED, {
        "tool_call_id": "call-1",
        "tool_name": "location__current",
        # 下面这些都是必须被挡住的：前两个在老实现里会原样进浏览器。
        "arguments": {"latitude": 31.2304, "longitude": 121.4737},
        "output": {"latitude": 31.2304},
        "scope": {"owner_id": "acct"},
    })

    assert set(published) == {"tool_call_id", "tool_name", "status", "error_kind"}
    assert "31.2304" not in str(published)


@pytest.mark.asyncio
async def test_deterministic_readonly_path_emits_start_and_finish() -> None:
    """只读快路径以前**不发**事件，于是位置 / 天气 / 导航在前面是一片空白。"""
    from agent_core.tools import ToolRegistry

    bus = InMemoryEventBus()
    seen = []
    for event_type in (EventType.TOOL_STARTED, EventType.TOOL_COMPLETED, EventType.TOOL_FAILED):
        bus.subscribe(event_type, lambda event: seen.append(event))

    loop = _bare_loop(bus)
    loop.registry = ToolRegistry()
    loop.public_messages = []
    loop.run_id = "run-1"

    async def fake_execute(spec, call, working):
        return ToolResult("ok", {"city": "上海市"})

    loop._execute_deterministic_tool = fake_execute  # type: ignore[assignment]
    loop._save = lambda *args, **kwargs: None  # type: ignore[assignment]
    loop._readonly_summary = lambda *args, **kwargs: None  # type: ignore[assignment]
    loop._fast_reply = lambda summary: "好的"  # type: ignore[assignment]

    from agent_core.contracts import EFFECT_READ, ToolSpec
    # 名字要带模块前缀 —— 注册表按 `module__tool` 解析，写 "current" 会 resolve 不到，
    # 于是这条路径会退回模型分支（`self.provider` 都没装配，直接报错）。
    spec = ToolSpec(
        name="location__current", risk="low", effect=EFFECT_READ, deterministic_route_allowed=True,
        handler=lambda arguments, scope: {"status": "ok"},
        schema={"type": "function", "function": {"name": "location__current"}},
    )
    loop.registry.register(spec)

    outcome = await AgentLoop._handle_readonly_request(
        loop, "location__current", {}, [], []
    )

    assert outcome.status == "completed"
    assert [event.type for event in seen] == [EventType.TOOL_STARTED, EventType.TOOL_COMPLETED]
    assert seen[0].data["tool_name"] == "location__current"
