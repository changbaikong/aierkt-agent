"""公开边界的跨层扫描：同一条链路的三个出口一起过同一张禁用词表。

为什么单独有这么一个文件
------------------------
本轮反复踩到同一种形态：**零件测试通过、真实链路仍然出问题**。

- 测试里手工构造的 ToolSpec 带了 `effect=read + deterministic_route_allowed`，
  正式模块没带 → 快路径用例全绿，真实链路上"我的任务列表"照样回落给模型；
- 任务回执的投影函数写得挺干净，但它**从没被 lifespan 调用过** → 干净地不生效；
- `_requires_approval` 写进了 context，而 `WorkRequest.task_input` 取的是
  `input or context` → 调用方只要给一个 input 字段，审批闸门就整个失效。

所以这里不再按层写断言，而是按**出口**扫：聊天历史、SSE 帧、HTTP 任务详情
三个出口用同一张禁用词表，链路上换任何一层实现都要重新过一遍。

词表来自本轮全局约束：异常原文、堆栈、本机绝对路径、Provider 名称、
内部工具名、原始 JSON 字段名。
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_core.contracts import AgentRequest, ProviderResponse, Scope, ToolSpec
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.modules import ModuleRegistry
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.routing import TaskRouter, RouteStrength
from agent_core.tools import ToolRegistry
from agent_tools.location import LocationStore
from agent_tools.modules import build_search_module, build_task_module
from agent_tools.navigation import make_navigation_tool

#: 用户可见的任何文本里都不该出现这些。
FORBIDDEN_IN_PUBLIC_TEXT = (
    "Traceback",
    "RuntimeError",
    "KeyError",
    "Errno",
    "No such file",
    "provider error",
    "C:\\Users",
    "search__web",
    "navigation__route",
    "task_result",
    "_requires_approval",
)

#: 一段"真实形状"的内部诊断：异常类名 + 本机绝对路径 + 换行 + Provider 名。
DIRTY_ERROR = (
    "RuntimeError: [Errno 2] No such file or directory: "
    "'C:\\Users\\someone\\Desktop\\secret.txt'\n"
    "Traceback (most recent call last): provider error: AmapNavigationClient"
)


class ExplodingProvider(LLMProvider):
    """任何一次模型调用都算失败 —— 用来证明这条链路真的零模型。"""

    def __init__(self) -> None:
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append(messages)
        raise AssertionError("这条链路不允许调用模型")


class ScriptedProvider(LLMProvider):
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append({"messages": messages, "tools": tools})
        return ProviderResponse(next(self.responses), {"completion_tokens": 1})


def _assert_public(text: str, *, where: str) -> None:
    for token in FORBIDDEN_IN_PUBLIC_TEXT:
        assert token not in text, f"{where} 泄漏了 {token}: {text!r}"


# ----------------------------------------------------------------------
# 出口一：聊天历史（确定性快路径 + 正式模块）
# ----------------------------------------------------------------------
def _task_registry(tmp_path):
    modules = ModuleRegistry()
    module_ids = {"tasks", "search"}
    modules.register(build_search_module(lambda: ToolSpec(
        name="web",
        schema={"type": "function", "function": {"name": "web", "description": "search",
                                                 "parameters": {"type": "object", "properties": {}}}},
        handler=lambda _args, _scope: {"status": "ok", "output": {"results": []}},
        risk="low",
    )))
    spec, service = build_task_module(
        db_path=tmp_path / "tasks.db",
        registry_provider=lambda: modules.tools_for(set(module_ids)),
    )
    modules.register(spec)
    return modules.tools_for(set(module_ids)), service


@pytest.mark.asyncio
async def test_task_list_history_and_reply_share_one_public_boundary(tmp_path):
    """「我的任务列表」：零模型调用，且**写进历史的那份**同样干净。

    只看 outcome.reply 是不够的 —— 用户下次翻聊天记录看到的是历史。
    """
    provider = ExplodingProvider()
    registry, service = _task_registry(tmp_path)
    await service.create_task(
        scope=Scope("alice", "web", "chat-1", "companion"),
        title="每晚总结", request_text=DIRTY_ERROR, delivery_mode="notify",
        schedule={"run_at": "2030-01-01T09:00:00+00:00"}, allowed_tools=[],
    )
    loop = AgentLoop(
        provider, registry, Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
    )

    outcome = await loop.run(AgentRequest("我的任务列表", scope=loop.scope))

    assert provider.calls == []
    assert "每晚总结" in outcome.reply
    _assert_public(outcome.reply, where="任务列表回复")
    history = "\n".join(
        str(item.get("content") or "") for item in (loop.public_messages or [])
    )
    _assert_public(history, where="任务列表历史")
    assert "每晚总结" in history


# ----------------------------------------------------------------------
# 出口二：SSE 帧
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_sse_task_frames_are_public(monkeypatch):
    """跨过浏览器边界的任务帧同样不许带内部诊断。

    三个业务终态（completed/failed/cancelled）已经不再下发，所以这里喂的
    是它们各自对应的 **task.result** 刷新通知，并且**故意**把内部诊断
    塞进 data —— 出口必须把它们全挡在浏览器之外。
    """
    import web_api

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)

    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    frames = []
    for task_id, status in (("t1", "failed"), ("t2", "completed"), ("t3", "cancelled")):
        pending = asyncio.create_task(stream.__anext__())
        await asyncio.sleep(0)
        await bus.emit(Event(EventType.TASK_RESULT, datetime.now(timezone.utc), {
            "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion",
        }, {"task_id": task_id, "run_id": "r1", "status": status,
            "history_written": True, "error": DIRTY_ERROR, "title": DIRTY_ERROR}))
        frames.append(await pending)
    await stream.aclose()

    assert len(frames) == 3
    for frame in frames:
        _assert_public(frame, where="SSE 帧")
        assert "history_written" in frame


STEWARD_PUBLIC_FIELDS = {
    "turn_id", "run_id", "plan_id", "step_id", "phase", "content", "kind", "dedupe_key",
}

#: SSE 帧自带的信封字段 —— 与事件类型无关，每种事件都有这一组。
FRAME_ENVELOPE_FIELDS = {
    "version", "event_id", "event_key", "status", "turn_id", "run_id",
    "action_id", "task_id", "title", "result", "timestamp",
}


def test_steward_progress_public_data_is_allowlisted() -> None:
    """过程播报只露前端要用的那八个字段，其余（异常原文、Provider 回执）一律不过边界。

    这里还断言**入参没被改动**：出口做白名单时如果顺手 mutate 原 dict，
    同一条事件在内部订阅者（落库、诊断）眼里就会少字段。
    """
    import web_api

    data = {
        "turn_id": "t", "run_id": "r", "phase": "accepted",
        "content": "我先看看。", "kind": "steward_progress", "dedupe_key": "d",
        "traceback": "secret", "error": "internal", "provider_payload": {"x": 1},
    }
    original = dict(data)

    public = web_api._public_event_data(EventType.STEWARD_PROGRESS, data)

    assert public == {
        "turn_id": "t", "run_id": "r", "plan_id": "", "step_id": "",
        "phase": "accepted", "content": "我先看看。",
        "kind": "steward_progress", "dedupe_key": "d",
    }
    assert data == original


@pytest.mark.asyncio
async def test_sse_steward_progress_frame_carries_no_diagnostics(monkeypatch):
    """真实事件流上再走一遍：脏 data 进、干净帧出。"""
    import json

    import web_api

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)

    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await bus.emit(Event(EventType.STEWARD_PROGRESS, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion",
    }, {
        "turn_id": "turn-1", "run_id": "run-1", "plan_id": "p-1", "step_id": "s-1",
        "phase": "running", "content": "我先看看。",
        "kind": "steward_progress", "dedupe_key": "d-1",
        "error": DIRTY_ERROR, "traceback": DIRTY_ERROR,
        "provider_payload": {"api_key": "sk-secret", "raw": DIRTY_ERROR},
    }))
    frame = await next_frame
    await stream.aclose()

    payload = json.loads(frame.split("data:", 1)[1].strip())

    _assert_public(frame, where="steward.progress 帧")
    assert set(payload) <= FRAME_ENVELOPE_FIELDS | STEWARD_PUBLIC_FIELDS
    assert payload["content"] == "我先看看。"
    assert payload["phase"] == "running"
    assert payload["dedupe_key"] == "d-1"


# ----------------------------------------------------------------------
# 出口三：HTTP 任务详情
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_http_task_list_response_is_public(monkeypatch):
    """/api/tasks 的任务详情不许带 last_error 原文。"""
    import json

    import web_api

    class FakeStore:
        def list_tasks(self, scope, limit=200):
            return [SimpleNamespace(
                task_id="t1", title="每晚总结", delivery_mode="execute", status="failed",
                schedule={"kind": "cron", "cron": "0 7 * * *", "timezone": "Asia/Shanghai"},
                next_run_at=None, last_error=DIRTY_ERROR,
                created_at="2026-09-14T00:00:00+00:00", updated_at="2026-09-14T00:00:00+00:00",
            )]

    class FakeConditionStore:
        def list(self, scope):
            return []

    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "_start_task_services", lambda: asyncio.sleep(0))
    monkeypatch.setattr(web_api, "_task_service", lambda: SimpleNamespace(store=FakeStore()))
    monkeypatch.setattr(web_api, "_condition_task_service", lambda: SimpleNamespace(store=FakeConditionStore()))

    payload = await web_api.list_all_tasks("local-user")
    dumped = json.dumps(payload, ensure_ascii=False)

    _assert_public(dumped, where="/api/tasks 响应")
    assert "last_error" not in dumped
    row = payload["tasks"][0]
    # 只给稳定码，不给原文。DIRTY_ERROR 的形状（No such file）会被归到 unavailable；
    # 具体归哪一档不是契约，**不把原文带出来**才是。
    assert row["result_code"] in {"timeout", "permission", "unavailable", "internal"}
    assert row["last_result"] == "「每晚总结」这次没办成。已经记录了情况，稍后可以重试。"


# ----------------------------------------------------------------------
# 跨层：音量口径一致（路由判定 + 工具权限 + Host 审批）
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_netease_volume_request_never_becomes_a_system_volume_task():
    """同一句话在两层必须是同一个口径：网易云音量 ≠ 系统音量。"""
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="work__submit",
        schema={"type": "function", "function": {"name": "work__submit",
                                                 "parameters": {"type": "object"}}},
        handler=lambda _args, _scope: {},
        risk="low",
        hard_routable=True,
        deterministic_route_allowed=True,
    ))
    decision = await TaskRouter(registry=registry).decide("把网易云音量调到 30%")

    assert decision.task_type != "system.volume.adjust"
    assert decision.strength is not RouteStrength.HARD

    from agent_tools.modules import build_netease_module

    class FakeCloudProvider:
        async def status(self):
            return {"logged_in": True}

    tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}
    assert tools["set_volume"].risk == "high"
    assert tools["set_volume"].confirmation_title == "调整网易云音量"


# ----------------------------------------------------------------------
# 跨层：真实导航工具 → AgentLoop → 气泡
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_navigation_failure_end_to_end_stays_public(tmp_path):
    """用**真**导航工具跑一遍：位置没共享时，气泡里只有固定那句中文。"""
    registry = ToolRegistry()
    # 必须带命名空间：循环里认的是 tools_for 包装后的名字（navigation__route），
    # 直接注册裸 "route" 会让 resolve 落空、退化成"未知工具"绕圈子。
    registry.register(replace(make_navigation_tool(LocationStore(), object()), name="navigation__route"))
    provider = ScriptedProvider([{
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "navigation__route",
                                     "arguments": '{"destination": "广州塔", "mode": "driving"}'}}],
    }])
    loop = AgentLoop(
        provider, registry, Scope("alice", "web", "chat-1", "companion"),
        run_store=SQLiteRunStore(tmp_path / "core.db"),
        confirmation_store=SQLiteConfirmationStore(tmp_path / "core.db"),
    )

    outcome = await loop.run(AgentRequest("导航到广州塔", scope=loop.scope))

    assert outcome.reply == "我还没有拿到你的当前位置。打开位置共享后，我再给你规划。"
    _assert_public(outcome.reply, where="导航失败气泡")
    # 明确的导航请求现在走确定性快路径，整轮**零模型调用**。
    # （改造前这里断言的是"有且只有一次带工具的模型调用" —— 那时导航要先让
    # 模型挑一次工具，而那一趟往返正是这次要修掉的东西。）
    assert provider.calls == []
