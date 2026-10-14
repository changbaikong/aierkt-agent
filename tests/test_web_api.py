from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace
import pytest


def test_web_max_steps_can_be_overridden(monkeypatch):
    import web_api

    monkeypatch.setenv("AGENT_WEB_MAX_STEPS", "8")

    assert web_api._web_max_steps() == 8


def test_web_persona_is_fixed_and_scope_isolated():
    from web_api import ARcueid_PERSONA, build_web_scope, clean_character_reply

    scope = build_web_scope("acct", "conv-1")
    assert scope.owner_id == "acct"
    assert scope.channel == "web"
    assert scope.conversation_id == "conv-1"
    assert scope.module_id == "companion"
    assert "爱尔奎特" in ARcueid_PERSONA
    assert "不要替对方做决定" in ARcueid_PERSONA
    assert "回复不要使用括号动作" in ARcueid_PERSONA
    assert clean_character_reply("（眨了眨眼）你好！(认真地说)") == "你好！"


def test_chat_endpoint_returns_accepted_turn_immediately(monkeypatch):
    from fastapi.testclient import TestClient
    import web_api

    captured = {}

    class FakeHandle:
        accepted = True
        turn_id = "turn-1"
        position = 1

        class Result:
            async def _wait(self):
                return None

            def __await__(self):
                async def done():
                    return type("Outcome", (), {"status": "completed", "reply": "你好呀！", "run_id": "run-1", "action_id": ""})()
                return done().__await__()

        result = Result()

    class FakeManager:
        async def submit(self, request):
            assert request.scope.channel == "web"
            assert request.system_prompt
            captured["request"] = request
            return FakeHandle()

        def history(self, scope):
            return []

    monkeypatch.setattr(web_api, "manager", FakeManager())
    monkeypatch.setattr(web_api, "_memory_extractor", object())
    monkeypatch.setattr(web_api, "_memory_writer", object())
    monkeypatch.setattr(web_api, "_start_task_services", lambda: asyncio.sleep(0))
    monkeypatch.setattr(web_api, "_start_memory_workers", lambda: asyncio.sleep(0))
    scheduled = {}

    class FakeTask:
        def add_done_callback(self, callback):
            callback(self)

    def fake_extract_and_store_memory_turn(*args, **kwargs):
        scheduled["args"] = args
        scheduled["kwargs"] = kwargs
        async def _noop():
            return {"status": "stored", "extracted": 1, "persisted": 1}

        return _noop()

    def fake_create_task(coro):
        scheduled["coro"] = coro
        coro.close()
        return FakeTask()

    monkeypatch.setattr(web_api, "extract_and_store_memory_turn", fake_extract_and_store_memory_turn)
    monkeypatch.setattr(web_api.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(
        web_api,
        "_runtime_time_context",
        lambda: "[运行时本机时间]\n当前本机时间：2030-01-01T20:00:00+08:00\n时区：Asia/Shanghai\n来源：local\n仅用该时间理解相对时间表达，例如今晚、明天和半小时后。每轮以此处最新值为准；创建任务时交由工具校验并转换为绝对时间。",
    )
    client = TestClient(web_api.app)
    response = client.post("/api/chat", json={"message": "你好", "account_id": "acct", "conversation_id": "conv"})
    assert response.status_code == 202
    assert response.json() == {"status": "accepted", "turn_id": "turn-1", "position": 1}
    assert "2030-01-01T20:00:00+08:00" in captured["request"].persona_override
    assert "仅用该时间理解相对时间表达" in captured["request"].persona_override
    assert "coro" in scheduled


def test_chat_endpoint_rejects_blank_message():
    from fastapi.testclient import TestClient
    import web_api

    client = TestClient(web_api.app)
    response = client.post("/api/chat", json={"message": "   "})
    assert response.status_code == 422


def test_chat_endpoint_reports_queue_rejection(monkeypatch):
    from fastapi.testclient import TestClient
    import web_api

    class RejectingManager:
        async def submit(self, _request):
            return SimpleNamespace(accepted=False, turn_id="rejected", position=0, reason="queue full")

    monkeypatch.setattr(web_api, "manager", RejectingManager())
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "_start_task_services", lambda: asyncio.sleep(0))
    monkeypatch.setattr(web_api, "_start_memory_workers", lambda: asyncio.sleep(0))
    response = TestClient(web_api.app).post("/api/chat", json={"message": "hello"})
    assert response.status_code == 409
    assert response.json()["detail"] == "queue full"


def test_chat_does_not_use_legacy_worker_shortcut(monkeypatch):
    from fastapi.testclient import TestClient
    import web_api

    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    client = TestClient(web_api.app)
    response = client.post("/api/chat", json={"message": "查看现在D盘里面最大的那个文件是哪个"})
    assert response.status_code in {202, 503}


def test_work_task_routes_expose_retry_and_approval_endpoints(monkeypatch, tmp_path):
    import web_api
    from agent_core.work_host import WorkHost
    from agent_core.local_worker_provider import LocalWorkerProvider
    web_api.work_host = WorkHost(tmp_path / "work.db", provider=LocalWorkerProvider(allowed_roots=[tmp_path]), max_workers=1)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    from fastapi.testclient import TestClient
    with TestClient(web_api.app) as client:
        response = client.post("/api/work/tasks", json={"task_type": "file.list", "objective": "按名称列出文件", "input": {"directory": str(tmp_path)}})
        assert response.status_code == 202
        work_id = response.json()["work_item_id"]
        assert client.get(f"/api/work/tasks/{work_id}").status_code == 200
        assert client.post(f"/api/work/tasks/{work_id}/retry").status_code in {202, 409}


def test_work_task_event_history_filters_by_attempt_and_sequence(monkeypatch, tmp_path):
    import web_api
    from agent_core.contracts import Scope
    from agent_core.work_host import WorkHost
    from agent_core.work_provider import WorkEvent

    host = WorkHost(tmp_path / "events.db", provider=type("Provider", (), {
        "capabilities": type("Caps", (), {"provider_id": "test"})(),
    })(), max_workers=1)
    web_api.work_host = host
    scope = Scope("local-user", "web", "web", "work")
    work = host.ledger.create_work_item(scope=scope, objective="查看文件")
    attempt = host.ledger.create_attempt(work.work_item_id, provider_id="test")
    host.ledger.append_event(attempt.attempt_id, "started", {"step": 1})
    host.ledger.append_event(attempt.attempt_id, "completed", {"step": 2})
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    with __import__("fastapi.testclient", fromlist=["TestClient"]).TestClient(web_api.app) as client:
        response = client.get(f"/api/work/tasks/{work.work_item_id}/events?attempt_id={attempt.attempt_id}&after_sequence=1")
        assert response.status_code == 200
        assert [event["sequence"] for event in response.json()["events"]] == [2]
        assert client.get(f"/api/work/tasks/{work.work_item_id}/events?attempt_id=missing").status_code == 404


def test_conversation_lifecycle_routes_use_persistent_repository(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import web_api
    from agent_core.conversation_repository import ConversationRepository

    database = tmp_path / "conversations.db"
    repository = ConversationRepository(database)

    class FakeManager:
        def __init__(self):
            self.busy = False
            self.forgotten = []

        def is_busy(self, _scope):
            return self.busy

        def clear_history(self, scope):
            repository.clear(scope)

        async def forget(self, scope):
            self.forgotten.append(scope)

    manager = FakeManager()
    monkeypatch.setattr(web_api, "manager", manager)
    monkeypatch.setattr(web_api, "conversation_repository", repository, raising=False)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    client = TestClient(web_api.app)

    created = client.post("/api/conversations", json={"account_id": "acct", "title": "测试会话"})
    assert created.status_code == 201
    conversation_id = created.json()["conversation"]["conversation_id"]
    assert created.json()["conversation"]["title"] == "测试会话"

    listed = client.get("/api/conversations?account_id=acct&status=active")
    assert listed.status_code == 200
    assert [item["conversation_id"] for item in listed.json()["conversations"]] == [conversation_id]

    scope = web_api.build_web_scope("acct", conversation_id)
    repository.append_message(scope, {"role": "user", "content": "会在清空后消失"})
    cleared = client.post(f"/api/conversations/{conversation_id}/clear?account_id=acct")
    assert cleared.status_code == 200
    reopened = ConversationRepository(database)
    assert reopened.history(scope) == []
    assert reopened.get(scope).title == ""

    archived = client.post(f"/api/conversations/{conversation_id}/archive?account_id=acct")
    assert archived.status_code == 200
    assert archived.json()["conversation"]["status"] == "archived"

    archived_list = client.get("/api/conversations?account_id=acct&status=archived")
    assert [item["conversation_id"] for item in archived_list.json()["conversations"]] == [conversation_id]

    restored = client.post(f"/api/conversations/{conversation_id}/restore?account_id=acct")
    assert restored.status_code == 200

    manager.busy = True
    blocked = client.delete(f"/api/conversations/{conversation_id}?account_id=acct")
    assert blocked.status_code == 409
    assert manager.forgotten == [], "会话还在用就不能 forget，否则正在等确认的动作会失去承载"
    manager.busy = False
    deleted = client.delete(f"/api/conversations/{conversation_id}?account_id=acct")
    assert deleted.status_code == 200
    assert repository.get(web_api.build_web_scope("acct", conversation_id)) is None
    assert manager.forgotten == [web_api.build_web_scope("acct", conversation_id)]


def test_delete_endpoint_forgets_cached_intent_state(monkeypatch, tmp_path):
    """删会话必须连意图状态一起忘掉 —— 包括本进程的读缓存。

    只清数据库不够：``SQLiteIntentStateStore`` 有 ``_cache``，同一个服务进程里
    下一次读到的是缓存里那份旧"待确认意图"，用户新建同名会话就会继承上一个
    会话的追问。所以断言分两半：老 store 实例读不到、新建 store 实例也读不到。
    """
    from datetime import datetime

    from fastapi.testclient import TestClient
    import web_api
    from agent_core.contracts import ConversationIntentState, Scope
    from agent_core.conversation_manager import ConversationManager
    from agent_core.conversation_repository import ConversationRepository
    from agent_core.intent import SQLiteIntentStateStore

    database = tmp_path / "conversations.db"
    repository = ConversationRepository(database)
    store = SQLiteIntentStateStore(database)
    scope = Scope("acct", "web", "conv", "companion")
    repository.ensure(scope)
    store.save(scope, ConversationIntentState(
        active_intent="weather_query",
        pending_intent="weather_query",
        slots={"location": "佛山"},
        updated_at=datetime.now().isoformat(timespec="seconds"),
    ))
    assert store.load(scope) is not None  # 预热读缓存

    manager = ConversationManager(
        lambda _scope: None,
        intent_state_store=store,
        conversation_repository=repository,
    )
    monkeypatch.setattr(web_api, "manager", manager)
    monkeypatch.setattr(web_api, "conversation_repository", repository, raising=False)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    client = TestClient(web_api.app)

    assert client.delete("/api/conversations/conv?account_id=acct").status_code == 200

    assert store.load(scope) is None
    assert SQLiteIntentStateStore(database).load(scope) is None


@pytest.mark.asyncio
async def test_decision_requires_matching_action_and_url_scope(monkeypatch):
    """审批编号与 URL 里的会话必须对得上，否则连"存在"都不承认。

    这条现在打在**统一审批接口**上（`/api/approvals/{id}`）。旧的
    `/api/conversations/{cid}/decision` 只做代理，同一个语义由协调器保证 ——
    两条路各测一遍会让"哪个才是真的"变得含糊。
    """
    import web_api
    from agent_core.approval_coordinator import ApprovalCoordinator
    from agent_core.contracts import Scope as _Scope

    calls = []

    class FakeStore:
        def get(self, action_id, scope):
            calls.append((action_id, scope))
            if action_id == "action-a" and scope.conversation_id == "conv-a":
                return SimpleNamespace(
                    action_id="action-a", run_id="run-a", status="pending",
                    tool="netease__set_volume", arguments={"percent": 30},
                    title="调整音量", code="volume", expires_at="2099-01-01T00:00:00",
                    module_id="companion",
                )
            return None

        def pending_for_scope(self, scope):
            return []

        def decide(self, action_id, scope, decision):
            calls.append(("decide", action_id, decision))
            record = self.get(action_id, scope)
            if record is None or record.status != "pending":
                return None
            return record

    class FakeManager:
        async def resume(self, action_id, scope, decision):
            calls.append(("resume", action_id, scope, decision))
            return SimpleNamespace(status="completed", reply="完成", run_id="run-a", action_id=action_id)

    coordinator = ApprovalCoordinator(
        confirmation_store=FakeStore(),
        # 复用 Web 那条真实的续跑适配，别在测试里另写一份 —— 那样测的是测试。
        resume_chat=web_api._resume_chat_confirmation,
    )
    monkeypatch.setattr(web_api, "manager", FakeManager())
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)

    async def _coordinator():
        return coordinator

    monkeypatch.setattr(web_api, "_approval_coordinator", _coordinator)

    from fastapi import HTTPException

    request = web_api.ApprovalDecisionRequest(decision="confirmed")

    with pytest.raises(HTTPException) as missing:
        await web_api.approval_decide("action-b", request, account_id="acct", conversation_id="conv-a")
    assert missing.value.status_code == 404
    # 同一账号、另一个会话：不许认。
    with pytest.raises(HTTPException) as wrong_scope:
        await web_api.approval_decide("action-a", request, account_id="acct", conversation_id="conv-b")
    assert wrong_scope.value.status_code == 404

    result = await web_api.approval_decide("action-a", request, account_id="acct", conversation_id="conv-a")
    assert result["status"] == "completed"
    assert calls[-1][0:2] == ("resume", "action-a")
    assert calls[-1][2].conversation_id == "conv-a"
    assert _Scope("acct", "web", "conv-a", "companion").conversation_id == "conv-a"


def test_registered_tools_endpoint_groups_namespaced_tools(monkeypatch):
    from fastapi.testclient import TestClient
    import web_api
    from agent_core.contracts import ToolSpec
    from agent_core.modules import ModuleRegistry, ModuleSpec

    def build_search_tools():
        return [ToolSpec(
            "web",
            {"type": "function", "function": {"name": "web", "description": "搜索网页"}},
            lambda _args, _scope: {},
        )]

    def build_task_tools():
        return [ToolSpec(
            "create",
            {"type": "function", "function": {"name": "create", "description": "创建任务"}},
            lambda _args, _scope: {},
            risk="high",
        )]

    registry = ModuleRegistry()
    registry.register(ModuleSpec("search", build_search_tools, description="联网搜索"))
    registry.register(ModuleSpec("tasks", build_task_tools, description="定时任务"))
    tools = registry.tools_for({"search", "tasks"})
    monkeypatch.setattr(web_api, "_module_registry", registry)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", registry)
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)

    response = TestClient(web_api.app).get("/api/tools")

    assert response.status_code == 200
    assert response.json()["modules"] == [
        {
            "id": "search",
            "description": "联网搜索",
            "status": "healthy",
            "diagnostic": {"status": "healthy", "code": "", "reason": ""},
            "tools": [{
                "name": "search__web",
                    "description": "【联网搜索】搜索网页",
                    "risk": "low",
                    "confirmation_required": False,
                    "declared": False,
                    "registration": "registered",
                "advertised_to_model": True,
                "code": "",
                "reason": "",
            }],
        },
        {
            "id": "tasks",
            "description": "定时任务",
            "status": "healthy",
            "diagnostic": {"status": "healthy", "code": "", "reason": ""},
            "tools": [{
                "name": "tasks__create",
                    "description": "【定时任务】创建任务",
                    "risk": "high",
                    "confirmation_required": True,
                    "declared": False,
                    "registration": "registered",
                "advertised_to_model": True,
                "code": "",
                "reason": "",
            }],
        },
    ]


@pytest.mark.asyncio
async def test_capabilities_reports_unregistered_optional_tool_without_failing_readiness(monkeypatch, tmp_path):
    import web_api
    from agent_core.modules import ModuleRegistry, ModuleSpec
    from agent_core.tools import ToolRegistry

    modules = ModuleRegistry()
    modules.register(ModuleSpec(
        "music", lambda: [], description="Music controls", declared_tools=("player_status",),
    ))
    tools = modules.tools_for({"music"})
    monkeypatch.setattr(web_api, "_module_registry", modules)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", modules)
    monkeypatch.setattr(web_api, "_registration_failures", {})
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://example.test")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    database_path = tmp_path / "capabilities.db"
    sqlite3.connect(database_path).close()
    monkeypatch.setenv("AGENT_CORE_DB", str(database_path))

    snapshot = await web_api.capabilities()

    assert snapshot["status"] == "degraded"
    assert snapshot["ready"] is True
    assert snapshot["checks"]["database"] == {
        "status": "healthy", "writable": True, "code": "", "reason": "",
    }
    assert "path" not in snapshot["checks"]["database"]
    assert snapshot["modules"][0]["checked_at"] == snapshot["timestamp"]
    del snapshot["modules"][0]["checked_at"]
    assert snapshot["modules"] == [{
        "id": "music",
        "description": "Music controls",
        "status": "degraded",
        "tools": [{
            "name": "music__player_status",
            "declared": True,
            "registration": "unregistered",
            "advertised_to_model": False,
            "code": "tool_unregistered",
            "reason": "tool was declared but not registered",
        }],
    }]


def test_capability_database_probe_does_not_create_or_write_database(monkeypatch, tmp_path):
    import web_api

    database_path = tmp_path / "missing.db"
    monkeypatch.setenv("AGENT_CORE_DB", str(database_path))

    result = web_api._database_health()

    assert result == {
        "status": "unhealthy",
        "writable": False,
        "code": "database_unavailable",
        "reason": "database is not writable",
    }
    assert not database_path.exists()


@pytest.mark.asyncio
async def test_capability_snapshot_preserves_sanitized_legacy_health_fields(monkeypatch, tmp_path):
    import web_api
    from agent_core.modules import ModuleRegistry, ModuleSpec

    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [], declared_tools=("player_status",)))
    tools = modules.tools_for({"music"})
    monkeypatch.setattr(web_api, "_module_registry", modules)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", modules)
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://example.test")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setenv("AGENT_CORE_DB", str(tmp_path / "capabilities.db"))

    snapshot = await web_api.health()

    assert snapshot["checks"]["modules"]["failures"] == {"music": "tool_unregistered"}
    assert snapshot["checks"]["mcp"] == {
        "status": "unavailable", "code": "mcp_not_configured", "reason": "MCP is not configured",
    }
    assert snapshot["checks"]["player"] == {
        "status": "unavailable", "code": "player_unavailable", "reason": "player is unavailable",
    }


def test_registered_tools_endpoint_keeps_unregistered_declared_tool(monkeypatch):
    from fastapi.testclient import TestClient
    import web_api
    from agent_core.modules import ModuleRegistry, ModuleSpec

    modules = ModuleRegistry()
    modules.register(ModuleSpec(
        "music", lambda: [], description="Music controls", declared_tools=("player_status",),
    ))
    tools = modules.tools_for({"music"})
    monkeypatch.setattr(web_api, "_module_registry", modules)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", modules)
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)

    response = TestClient(web_api.app).get("/api/tools")

    assert response.status_code == 200
    assert response.json()["diagnostics"] == {
        "music": {"status": "degraded", "code": "tool_unregistered", "reason": "tool was declared but not registered"},
    }
    assert response.json()["registration_failures"] == {"music": "tool_unregistered"}
    assert response.json()["modules"] == [{
        "id": "music",
        "description": "Music controls",
        "status": "degraded",
        "diagnostic": {"status": "degraded", "code": "tool_unregistered", "reason": "tool was declared but not registered"},
        "tools": [{
            "name": "music__player_status",
            "description": "",
            "risk": "low",
            "confirmation_required": False,
            "declared": True,
            "registration": "unregistered",
            "advertised_to_model": False,
            "code": "tool_unregistered",
            "reason": "tool was declared but not registered",
        }],
    }]


@pytest.mark.asyncio
async def test_capability_refresh_coalesces_concurrent_runtime_probes(monkeypatch, tmp_path):
    import web_api
    from agent_core.modules import ModuleRegistry, ModuleSpec

    calls: list[bool] = []

    async def probe(*, force: bool = False):
        calls.append(force)
        await asyncio.sleep(0.01)
        return [{"name": "mcp", "status": "healthy", "code": "", "reason": ""}]

    modules = ModuleRegistry()
    modules.register(ModuleSpec("music", lambda: [], declared_tools=("status",), probe=probe))
    tools = modules.tools_for({"music"})
    monkeypatch.setattr(web_api, "_module_registry", modules)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", modules)
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_capability_refresh_task", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://example.test")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setenv("AGENT_CORE_DB", str(tmp_path / "capabilities.db"))

    await asyncio.gather(web_api.refresh_capabilities(), web_api.refresh_capabilities())

    assert calls == [True]


@pytest.mark.asyncio
async def test_chat_reports_runtime_initialization_failure_without_raw_exception(monkeypatch):
    import web_api

    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "manager", None)

    result = await web_api.chat(web_api.ChatRequest(message="hello"))

    assert result["status"] == "failed"
    assert result["error"] == "runtime_initialization_failed"
    assert result["reply"] == "对话核心尚未就绪，请检查系统能力诊断。"


@pytest.mark.asyncio
async def test_readiness_fails_when_a_required_module_is_degraded(monkeypatch, tmp_path):
    import web_api
    from agent_core.modules import ModuleRegistry, ModuleSpec

    modules = ModuleRegistry()
    modules.register(ModuleSpec(
        "required_music", lambda: [], declared_tools=("status",), required_for_readiness=True,
    ))
    tools = modules.tools_for({"required_music"})
    monkeypatch.setattr(web_api, "_module_registry", modules)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", modules)
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://example.test")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setenv("AGENT_CORE_DB", str(tmp_path / "capabilities.db"))

    response = await web_api.readiness()

    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload["status"] == "not_ready"
    assert payload["checks"]["modules"]["registered"]["required_music"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_readiness_fails_when_core_provider_or_database_is_unavailable(monkeypatch, tmp_path):
    import web_api
    from agent_core.modules import ModuleRegistry
    from agent_core.tools import ToolRegistry

    modules = ModuleRegistry()
    tools = ToolRegistry()
    monkeypatch.setattr(web_api, "_module_registry", modules)
    monkeypatch.setattr(web_api, "_tool_registry", tools)
    monkeypatch.setattr(web_api, "_tool_registry_owner", modules)
    monkeypatch.setattr(web_api, "_capability_snapshot_cache", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_CORE_DB", str(tmp_path))

    response = await web_api.readiness()
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["checks"]["provider"]["code"] == "provider_not_configured"
    assert payload["checks"]["database"]["code"] == "database_unavailable"


@pytest.mark.asyncio
async def test_task_event_stream_filters_scope_and_unsubscribes(monkeypatch):
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)

    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    first = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    # 12 个既有事件（三个任务终态已不再订阅，只剩 task.started）
    # + TASK_RESULT（回执桥写库后的刷新通知）
    # + tool.started / tool.completed / tool.failed（等待区的过程状态）
    assert len(bus._subscriptions) == 15

    await bus.emit(Event(EventType.TASK_RESULT, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "other", "module_id": "companion"
    }, {"task_id": "wrong", "status": "failed"}))
    await bus.emit(Event(EventType.TASK_RESULT, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion"
    }, {"task_id": "right", "run_id": "r1", "status": "completed", "history_written": True}))
    payload = await first
    assert "event: task.result" in payload
    assert '"task_id": "right"' in payload

    await stream.aclose()
    assert bus._subscriptions == {}


@pytest.mark.asyncio
async def test_raw_task_terminal_events_never_reach_the_browser(monkeypatch):
    """原始终态是内部业务事件：SSE 一律不下发，前端只看 task.result。

    过去桥会把终态重发一次给前端，于是同一个终态在浏览器里出现两帧
    （一帧业务终态 + 一帧重发），刷两次历史。现在订阅表里根本没有它们。
    """
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)

    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    for event_type in (EventType.TASK_COMPLETED, EventType.TASK_FAILED, EventType.TASK_CANCELLED):
        await bus.emit(Event(event_type, datetime.now(timezone.utc), {
            "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion"
        }, {"task_id": "t1", "title": "每晚总结", "error": "RuntimeError: secret",
            "result": "C:\\Users\\secret\\out.txt"}))
    # 只有 task.result 会到达浏览器
    await bus.emit(Event(EventType.TASK_RESULT, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion"
    }, {"task_id": "t1", "run_id": "r1", "status": "completed", "history_written": True}))

    frame = await next_frame
    assert "event: task.result" in frame
    await stream.aclose()


def test_portrait_expression_is_derived_locally_without_provider():
    from agent_core.presentation_policy import expression_for_event

    assert expression_for_event("chat.started", {}) == ("thinking", "thinking")
    assert expression_for_event("tool.started", {"tool_name": "search__web"}) == ("thinking", "retrieval")
    assert expression_for_event("chat.failed", {}) == ("concern", "failure")
    assert expression_for_event("chat.completed", {"reply": "居然是这样，没想到。"}) == ("surprise", "reply")
    assert expression_for_event("chat.completed", {"reply": "这么说有点害羞。"}) == ("shy", "reply")
    assert expression_for_event("chat.completed", {"reply": "今天有点累，先休息吧。"}) == ("tired", "reply")


def test_today_user_events_are_limited_to_other_conversations(tmp_path):
    from datetime import datetime, timezone
    from agent_core.conversation_repository import ConversationRepository
    from agent_core.contracts import Scope

    repo = ConversationRepository(tmp_path / "conversation.db")
    current = Scope("acct", "web", "current", "companion")
    other = Scope("acct", "web", "other", "companion")
    repo.ensure(current)
    repo.ensure(other)
    repo.append_message(other, {"role": "user", "content": "16:30 我今天喝了酒"}, now=datetime.now(timezone.utc))

    rows = repo.today_user_events(current, now=datetime.now(timezone.utc))
    assert len(rows) == 1
    assert rows[0]["content"] == "16:30 我今天喝了酒"
    assert rows[0]["conversation_id"] == "other"


def test_settings_reject_removed_emotion_provider_keys():
    from pydantic import ValidationError
    import web_api

    with pytest.raises(ValidationError, match="Invalid config key"):
        web_api.SettingsUpdateRequest(settings={"EMOTION_TAGGER_MODEL": "legacy-model"})


@pytest.mark.asyncio
async def test_event_stream_forwards_turn_state_for_the_exact_scope(monkeypatch):
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)
    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await bus.emit(Event(EventType.TURN_COMPLETED, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion"
    }, {"turn_id": "turn-1", "status": "completed"}))

    payload = await next_frame
    assert "event: turn.completed" in payload
    assert '"turn_id": "turn-1"' in payload
    await stream.aclose()


@pytest.mark.asyncio
async def test_event_stream_redacts_internal_failure_details(monkeypatch):
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)
    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await bus.emit(Event(EventType.CHAT_FAILED, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion",
    }, {
        "turn_id": "turn-1", "error_code": "RuntimeError", "error": "secret provider traceback",
        "message": "回复生成失败，请稍后重试。", "retryable": True,
    }))
    frame = await next_frame
    assert "secret provider traceback" not in frame
    assert "RuntimeError" not in frame
    assert "回复生成失败，请稍后重试。" in frame
    await stream.aclose()


@pytest.mark.asyncio
async def test_task_result_bridge_is_wired_and_writes_public_history(monkeypatch):
    """Web 侧只保留"接入回执桥"这一个出口，写进历史的是公开句子。

    旧实现（_task_event_message）会把 event.data["error"] 直接拼进气泡，
    异常类名和本机绝对路径会原样出现在聊天记录里；而且它从没被 lifespan
    调用过。现在终态回执由 agent_core 的 TaskResultBridge 单点负责。
    """
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    dirty = (
        "RuntimeError: [Errno 2] No such file: 'C:\\Users\\secret.txt'\n"
        "Traceback (most recent call last): provider error"
    )

    class FakeManager:
        def __init__(self):
            self.messages = []

        def append_message(self, scope, role, content, *, stable_key=None,
                           touch_activity=True, **extra):
            if any(item["stable_key"] == stable_key for item in self.messages):
                return False
            self.messages.append({
                "scope": scope, "role": role, "content": content,
                "stable_key": stable_key, "touch_activity": touch_activity, **extra,
            })
            return True

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)
    monkeypatch.setattr(web_api, "manager", FakeManager())
    monkeypatch.setattr(web_api, "_task_result_bridge", None)

    web_api._ensure_task_result_bridge()
    web_api._ensure_task_result_bridge()
    assert len(bus._subscriptions) == 3  # completed / failed / cancelled，不重复订阅

    from datetime import datetime, timezone
    await bus.emit(Event(EventType.TASK_FAILED, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion",
    }, {"task_id": "t1", "run_id": "r1", "title": "每晚总结", "error": dirty, "error_code": "permanent"}))

    assert len(web_api.manager.messages) == 1
    written = web_api.manager.messages[0]
    assert written["scope"].owner_id == "acct"
    assert written["scope"].conversation_id == "conv"
    assert written["role"] == "assistant"
    assert written["content"] == "「每晚总结」这次没办成。已经记录了情况，稍后可以重试。"
    assert written["kind"] == "task_result"
    assert written["touch_activity"] is False
    assert written["stable_key"] == "task-result:t1:r1:failed"
    for leak in ("RuntimeError", "Traceback", "C:\\Users", "secret.txt", "provider error", "\n"):
        assert leak not in written["content"]


@pytest.mark.asyncio
async def test_task_result_sse_frame_is_a_bare_refresh_signal(monkeypatch):
    """task.result 帧里只有稳定字段：拿它唯一要做的事就是重新拉一次历史。"""
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    dirty = "RuntimeError: [Errno 2] No such file: 'C:\\Users\\secret.txt'"
    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)

    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    # 故意在事件里塞满内部诊断：出口必须把它们全挡下
    await bus.emit(Event(EventType.TASK_RESULT, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion",
    }, {"task_id": "t1", "run_id": "r1", "status": "failed", "result_code": "permanent",
        "history_written": True, "error": dirty, "result": dirty, "title": "每晚总结"}))
    frame = await next_frame

    for leak in ("RuntimeError", "Errno", "C:\\Users", "secret.txt", "每晚总结"):
        assert leak not in frame
    assert '"task_id": "t1"' in frame
    assert '"run_id": "r1"' in frame
    assert '"history_written": true' in frame
    await stream.aclose()


@pytest.mark.asyncio
async def test_task_result_does_not_carry_the_reply_text(monkeypatch):
    """回执正文只存在于历史里；刷新通知不再顺带一句"渲染用"的话。

    以前 SSE 帧里带 message，和历史各渲染一次就有两套措辞来源，
    改一句要改两处。现在只有历史一处。
    """
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)

    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await bus.emit(Event(EventType.TASK_RESULT, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion",
    }, {"task_id": "t1", "run_id": "r1", "status": "completed", "history_written": True}))
    frame = await next_frame

    assert "已经办好了" not in frame
    assert "message" not in frame
    await stream.aclose()


def test_public_task_error_projection_hides_the_raw_text():
    """/api/tasks 的 last_error 曾经是异常原文，现在只给稳定码 + 一句中文。"""
    import web_api

    task = SimpleNamespace(
        title="每晚总结",
        last_error="KeyError: 'directory'\nTraceback (most recent call last): C:\\Users\\secret.txt",
    )
    projected = web_api._public_task_error(task)

    assert "last_error" not in projected
    assert projected["result_code"] == "internal"
    assert projected["last_result"] == "「每晚总结」这次没办成。已经记录了情况，稍后可以重试。"
    for leak in ("KeyError", "Traceback", "C:\\Users", "secret.txt"):
        assert leak not in json.dumps(projected, ensure_ascii=False)

    assert web_api._public_task_error(SimpleNamespace(title="x", last_error="")) == {
        "result_code": "", "last_result": "",
    }


@pytest.mark.asyncio
async def test_event_stream_forwards_steward_progress(monkeypatch):
    """Web 收到 steward.progress 后能拿到内容与阶段（前端据此更新当前聊天）。"""
    from datetime import datetime, timezone
    import web_api
    from agent_core.events import Event, EventType, InMemoryEventBus

    bus = InMemoryEventBus()
    monkeypatch.setattr(web_api, "event_bus", bus)
    stream = web_api._task_event_stream("acct", "conv", heartbeat_seconds=60)
    next_frame = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await bus.emit(Event(EventType.STEWARD_PROGRESS, datetime.now(timezone.utc), {
        "owner_id": "acct", "channel": "web", "conversation_id": "conv", "module_id": "companion"
    }, {
        "turn_id": "turn-1", "run_id": "run-1", "phase": "evidence_found",
        "content": "找到了。它主要在讨论 A 和 B。", "dedupe_key": "conv|acct|run-1||evidence_found",
    }))

    payload = await next_frame
    assert "event: steward.progress" in payload
    assert '"phase": "evidence_found"' in payload
    assert "找到了" in payload
    await stream.aclose()


def test_steward_sink_persists_without_touching_activity(monkeypatch):
    """管家过程句写进对话，但**不刷新用户活跃时间**。"""
    import web_api

    class Manager:
        def __init__(self):
            self.calls = []

        def append_message(self, scope, role, content, **extra):
            self.calls.append((scope, role, content, extra))
            return True

    manager = Manager()
    monkeypatch.setattr(web_api, "manager", manager)
    accepted = web_api._steward_sink(
        web_api.build_web_scope("acct", "conv"),
        {"role": "assistant", "content": "嗯，交给我吧。我先把论文找出来。", "phase": "accepted", "run_id": "run-1"},
        "conv|acct|run-1||accepted",
    )
    assert accepted is True
    scope, role, content, extra = manager.calls[0]
    assert role == "assistant"
    assert content == "嗯，交给我吧。我先把论文找出来。"
    assert extra["stable_key"] == "conv|acct|run-1||accepted"
    assert extra["touch_activity"] is False
    assert extra["kind"] == "steward_progress"
    assert extra["phase"] == "accepted"


def test_steward_sink_rejects_empty_content(monkeypatch):
    import web_api

    class Manager:
        def append_message(self, *args, **kwargs):
            raise AssertionError("空话术不该落库")

    monkeypatch.setattr(web_api, "manager", Manager())
    assert web_api._steward_sink(web_api.build_web_scope(), {"content": "  "}, "k") is False


@pytest.mark.asyncio
async def test_task_services_are_started_once(monkeypatch):
    import web_api

    class Service:
        def __init__(self):
            self.starts = 0

        async def start(self):
            self.starts += 1

    service = Service()
    monkeypatch.setattr(web_api, "_task_services", [service])
    monkeypatch.setattr(web_api, "_task_services_started", False)

    await web_api._start_task_services()
    await web_api._start_task_services()

    assert service.starts == 1


@pytest.mark.asyncio
async def test_health_reports_memory_cycle_status(monkeypatch, tmp_path):
    import web_api

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://example.com")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test")
    database_path = tmp_path / "health.db"
    sqlite3.connect(database_path).close()
    monkeypatch.setenv("AGENT_CORE_DB", str(database_path))
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "_repositories", object())
    monkeypatch.setattr(web_api, "_memory_extractor", object())
    monkeypatch.setattr(web_api, "_memory_workers_started", True)

    result = await web_api.health()

    assert result["status"] == "degraded"
    assert result["checks"]["memory_extraction"]["configured"] is True
    assert result["checks"]["memory_cycle"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_location_api_updates_and_clears_ephemeral_store(monkeypatch):
    import web_api
    from agent_tools.location import LocationStore
    from web_api import LocationRequest
    from datetime import datetime, timezone

    store = LocationStore()
    monkeypatch.setattr(web_api, "_location_store", store)
    monkeypatch.setattr(web_api, "_location_last_updates", {})
    request = LocationRequest(
        latitude=31.2304, longitude=121.4737, accuracy_m=10,
        captured_at=datetime.now(timezone.utc), account_id="acct", conversation_id="conv",
    )
    updated = await web_api.update_location(request)
    assert updated["status"] == "ok"
    assert store.get(web_api.build_web_scope("acct", "conv")) is not None
    cleared = await web_api.clear_location("acct", "conv")
    assert cleared["output"]["cleared"] is True


@pytest.mark.asyncio
async def test_location_api_rate_limits_same_scope(monkeypatch):
    import web_api
    from agent_tools.location import LocationStore
    from web_api import LocationRequest
    from datetime import datetime, timezone
    from fastapi import HTTPException

    monkeypatch.setattr(web_api, "_location_store", LocationStore())
    monkeypatch.setattr(web_api, "_location_last_updates", {})
    request = LocationRequest(latitude=1, longitude=2, captured_at=datetime.now(timezone.utc))
    await web_api.update_location(request)
    with pytest.raises(HTTPException) as caught:
        await web_api.update_location(request)
    assert caught.value.status_code == 429


@pytest.mark.asyncio
async def test_web_location_store_distinguishes_stale_from_never_shared(monkeypatch):
    """Web 层也要能区分"开过但过期"和"从没开过" —— 这是 stale 分档的入口。"""
    import web_api
    from agent_tools.location import LocationStore, make_location_tool
    from datetime import datetime, timedelta, timezone

    clock = {"now": datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)}
    store = LocationStore(ttl_seconds=300, now=lambda: clock["now"])
    monkeypatch.setattr(web_api, "_location_store", store)
    # 刻意不接解析器：这个测试只关心位置快照的分档，
    # "解析能力没配好"是另一回事（见下面那条断言）。
    tool = make_location_tool(store)
    scope = web_api.build_web_scope("acct", "conv")

    # 从没上报过
    assert (await tool.handler({}, scope))["status"] == "unavailable"

    # 上报后过期
    store.update(scope, 31.2304, 121.4737, 18.5, clock["now"])
    clock["now"] = clock["now"] + timedelta(hours=3)
    stale = await tool.handler({}, scope)
    assert stale["status"] == "stale"
    assert stale["output"]["reason"] == "location_stale"
    assert "latitude" not in stale["output"]

    # 重新上报：位置有了，但没接解析器 —— 答案是"解析不了"，
    # 不是 stale、也不是 unavailable，更不是一串裸坐标。
    store.update(scope, 31.2304, 121.4737, 18.5, clock["now"])
    assert (await tool.handler({}, scope))["output"]["reason"] == "location_resolution_unavailable"

    # 清理后回到"从没上报过"
    assert store.clear(scope) is True
    assert (await tool.handler({}, scope))["status"] == "unavailable"


@pytest.mark.asyncio
async def test_web_location_is_account_scoped_not_conversation_scoped(monkeypatch):
    """位置按账号 + 渠道共享：换会话、刷新页面都不该丢掉已授权的共享。

    旧版本名为 ``test_web_location_scope_isolation_survives_expiry``，
    断言的是"一个会话的过期位置不影响另一个会话"。那个前提和
    "刷新页面 / 新开标签页不用重新授权"直接冲突，已随保存范围一起改掉 ——
    隔离维度现在是 (owner_id, channel, module_id)。
    """
    import web_api
    from agent_tools.location import LocationStore, make_location_tool
    from datetime import datetime, timedelta, timezone

    clock = {"now": datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)}
    store = LocationStore(ttl_seconds=300, now=lambda: clock["now"])
    tool = make_location_tool(store)
    one = web_api.build_web_scope("acct", "one")
    two = web_api.build_web_scope("acct", "two")

    store.update(one, 31.2304, 121.4737, 18.5, clock["now"])

    # 另一个会话读到的是同一条位置 —— 这正是"新开标签页不用再授权一次"
    assert store.get(two)["latitude"] == 31.2304

    # 过期对两个会话同样生效，不会出现"一个过期、另一个还新鲜"
    clock["now"] = clock["now"] + timedelta(hours=3)
    assert (await tool.handler({}, one))["status"] == "stale"
    assert (await tool.handler({}, two))["status"] == "stale"

    # 另一个账号看不到这个位置
    assert store.get(web_api.build_web_scope("other", "one")) is None


@pytest.mark.asyncio
async def test_memory_workers_are_started_once(monkeypatch):
    import web_api

    class Worker:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self):
            self.starts += 1

        def stop(self, timeout=None):
            self.stops += 1

    maintenance = Worker()
    embedding = Worker()
    monkeypatch.setattr(web_api, "_memory_maintenance_worker", maintenance)
    monkeypatch.setattr(web_api, "_memory_embedding_worker", embedding)
    monkeypatch.setattr(web_api, "_memory_workers_started", False)

    await web_api._start_memory_workers()
    await web_api._start_memory_workers()
    await web_api._stop_memory_workers()

    assert maintenance.starts == 1
    assert embedding.starts == 1
    assert maintenance.stops == 1
    assert embedding.stops == 1


@pytest.mark.asyncio
async def test_memory_console_reports_recent_records_and_cycle_state(monkeypatch, tmp_path):
    import web_api
    from agent_core.memory_repositories import initialize_memory_repositories
    from agent_core.memory_writes import MemoryWriter

    class Embed:
        def embed(self, text: str) -> list[float]:
            return [0.1, 0.2, 0.3]

    repositories = initialize_memory_repositories(tmp_path / "memory.db", tmp_path / "vectors", vector_size=3)
    writer = MemoryWriter(repositories, Embed())
    scope = web_api.build_web_scope("acct", "web")
    writer.save_memory(
        scope,
        "有点累",
        conflict_key="user.state.mood",
        confidence=0.9,
        importance=0.2,
        duration="instant",
        metadata={"source": "seed", "turn_id": "turn-1", "run_id": "run-1"},
    )
    writer.save_memory(
        scope,
        "在赶开题报告",
        conflict_key="user.task.current",
        confidence=0.9,
        importance=0.8,
        duration="session",
        metadata={"source": "seed", "turn_id": "turn-2", "run_id": "run-2"},
    )

    monkeypatch.setattr(web_api, "_repositories", repositories)
    monkeypatch.setattr(web_api, "_memory_writer", writer)
    monkeypatch.setattr(web_api, "_memory_extractor", object())
    monkeypatch.setattr(web_api, "_memory_workers_started", True)

    result = await web_api.memory_console(account_id="acct")

    assert result["status"] == "ok"
    assert result["scope"] == {"owner_id": "acct", "channel": "web", "module_id": "companion"}
    assert result["summary"]["total"] == 2
    assert result["summary"]["layers"] == {"hot": 0, "warm": 2, "cold": 0}
    assert result["checks"]["memory_extraction"]["configured"] is True
    assert result["checks"]["memory_cycle"]["status"] == "ok"
    assert result["recent_records"][0]["content"] == "在赶开题报告"
    assert result["recent_records"][0]["metadata"]["turn_id"] == "turn-2"
    assert result["recent_records"][1]["content"] == "有点累"
    repositories.close()


@pytest.mark.asyncio
async def test_memory_console_handles_unconfigured_state(monkeypatch):
    import web_api

    monkeypatch.setattr(web_api, "_repositories", None)
    monkeypatch.setattr(web_api, "_memory_writer", None)
    monkeypatch.setattr(web_api, "_memory_extractor", None)
    monkeypatch.setattr(web_api, "_memory_workers_started", False)

    result = await web_api.memory_console()

    assert result["status"] == "degraded"
    assert result["summary"]["total"] == 0
    assert result["checks"]["memory_store"]["status"] == "unavailable"


# ── 设置接口的密钥遮蔽 ──────────────────────────────────────────────
# 方案任务 7。原来只在 `len(value) > 12` 时遮蔽，短密钥会明文返回 ——
# 而"短"和"不是密钥"是两件事。


def test_settings_never_return_a_full_api_key(monkeypatch):
    """接口层验证：无论 .env 里配了什么，响应里都不出现密钥原文。

    这里**不能**用 `monkeypatch.setenv` 造密钥 —— `get_settings` 开头会
    `load_dotenv(override=True)` 把 .env 重新读进环境，临时值会被真实值盖掉。
    所以改成"读一遍真实配置，断言里面的密钥都不在响应里"。
    """
    import os

    import web_api

    settings = asyncio.run(web_api.get_settings())["settings"]

    for key, value in settings.items():
        if web_api.is_secret_setting_key(key) and value:
            assert value == web_api._SECRET_PLACEHOLDER, f"{key} 返回了非遮蔽值"
    # 真实环境里的密钥都不该以原文出现在响应里
    for name in ("AMAP_WEB_SERVICE_KEY", "GEMINI_API_KEY", "ZHIPUAI_API_KEY", "OPENAI_API_KEY"):
        raw = os.getenv(name, "")
        if len(raw) > 3:
            assert raw not in json.dumps(settings, ensure_ascii=False), f"{name} 原文泄漏"


def test_short_api_key_is_masked_too(monkeypatch):
    """短密钥以前会**明文返回**：12 字符以内的密钥比长密钥更危险，
    因为它一眼看过去不像密钥，容易被人直接复制走。"""
    import web_api

    for short in ("abc", "123456789", "k12345678901"):
        assert web_api._mask_setting_value("GEMINI_API_KEY", short) == web_api._SECRET_PLACEHOLDER
        # 短到 1 个字符也照样遮蔽
        assert web_api._mask_setting_value("OPENAI_API_KEY", "x") == web_api._SECRET_PLACEHOLDER


def test_empty_and_missing_secret_report_unconfigured(monkeypatch):
    """未配置要回空串，不能回占位符 —— 前端能力面板靠"空不空"判断有没有配好。"""
    import web_api

    assert web_api._mask_setting_value("GEMINI_API_KEY", "") == ""
    assert web_api._mask_setting_value("OPENAI_API_KEY", None) == ""


def test_masking_does_not_touch_ordinary_settings(monkeypatch):
    """非密钥字段保持原样：遮蔽是给密钥的，不是给所有配置项的。"""
    import web_api

    assert web_api._mask_setting_value("GOOGLE_GEMINI_BASE_URL", "https://example.invalid/v1") == "https://example.invalid/v1"
    assert web_api._mask_setting_value("GEMINI_MODEL", "gemini-3.1-flash-lite") == "gemini-3.1-flash-lite"
    assert web_api._mask_setting_value("BOCHA_BASE_URL", "https://api.bochaai.com") == "https://api.bochaai.com"


def test_mask_setting_value_covers_secret_name_markers():
    """变量名里写着 SECRET / TOKEN / PASSWORD 的同样算密钥。"""
    import web_api

    for key in ("MY_API_KEY", "SOME_SECRET", "ACCESS_TOKEN", "DB_PASSWORD"):
        assert web_api._mask_setting_value(key, "x") == web_api._SECRET_PLACEHOLDER, key
    assert web_api._mask_setting_value("GEMINI_MODEL", "gemini-3.1") == "gemini-3.1"


def test_key_suffix_without_api_key_prefix_is_still_a_secret():
    """`AMAP_WEB_SERVICE_KEY` 名字里没有「API_KEY」三个字，但它是密钥。

    按 `"API_KEY" in key` 判会把它漏掉 —— 这条就是为那个漏网之鱼立的。
    """
    import web_api

    assert web_api.is_secret_setting_key("AMAP_WEB_SERVICE_KEY") is True
    assert web_api._mask_setting_value("AMAP_WEB_SERVICE_KEY", "b3337e9d84d8a3d7ddb58e13739d8657") == web_api._SECRET_PLACEHOLDER
    # 非密钥的名字不能被误伤
    assert web_api.is_secret_setting_key("GEMINI_MODEL") is False
    assert web_api.is_secret_setting_key("ZHIPUAI_EMBEDDING_BASE_URL") is False
    assert web_api.is_secret_setting_key("BOCHA_BASE_URL") is False


# ── 统一审批接口（§4） ──────────────────────────────────────────────


def _approval_client(monkeypatch, *, chat_records, work_records):
    """装一个只有审批相关依赖的 Web 客户端。

    刻意**不**启动整个运行时：这里要测的是"统一接口把两边的记录摆对没有"，
    真跑 `_initialize_runtime` 会把大模型、向量库、Work 队列全拉起来，
    测的东西就被淹没了。
    """
    from fastapi.testclient import TestClient
    import web_api
    from agent_core.approval_coordinator import ApprovalCoordinator

    class FakeChatStore:
        def __init__(self, records):
            self.records = records

        def _item(self, record):
            return SimpleNamespace(
                action_id=record["action_id"], run_id="run-1", status=record.get("status", "pending"),
                tool="netease__set_volume", arguments={"percent": 30},
                title="调整音量", code="volume", expires_at="2099-01-01T00:00:00",
                module_id="companion",
            )

        def get(self, action_id, scope):
            record = self.records.get(action_id)
            if record is None or record["conversation_id"] != scope.conversation_id:
                return None
            return self._item(record)

        def pending_for_scope(self, scope):
            return [
                self._item(record) for record in self.records.values()
                if record["conversation_id"] == scope.conversation_id
            ]

        def decide(self, action_id, scope, decision):
            record = self.records.get(action_id)
            if record is None or record["conversation_id"] != scope.conversation_id:
                return None
            if record.get("status", "pending") != "pending":
                return None
            record["status"] = decision
            return self._item(record)

    class FakeWorkHost:
        def __init__(self, records):
            self.records = records
            self.decisions = []

        def get_approval(self, approval_id, scope):
            record = self.records.get(approval_id)
            if record is None or record["conversation_id"] != scope.conversation_id:
                return None
            return record

        def pending_approvals(self, scope):
            return [
                record for record in self.records.values()
                if record["conversation_id"] == scope.conversation_id
                and record.get("status") == "pending"
            ]

        async def decide_approval(self, approval_id, scope, decision):
            self.decisions.append((approval_id, decision))
            self.records[approval_id]["status"] = decision
            return {"status": "queued" if decision == "approved" else decision, "approval_id": approval_id}

    chat_store = FakeChatStore(chat_records)
    work_host = FakeWorkHost(work_records)

    async def fake_coordinator():
        return ApprovalCoordinator(
            confirmation_store=chat_store,
            work_host=work_host,
            resume_chat=lambda action_id, scope, decision: _fake_resume(action_id, decision),
        )

    async def _fake_resume(action_id, decision):
        return {"status": "completed", "reply": "好了", "run_id": "run-1", "action_id": action_id}

    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "_approval_coordinator", fake_coordinator)
    return TestClient(web_api.app), work_host


def test_unified_approval_list_merges_both_surfaces(monkeypatch):
    client, _ = _approval_client(
        monkeypatch,
        chat_records={"chat-1": {"action_id": "chat-1", "conversation_id": "conv-a"}},
        work_records={"work-1": {"approval_id": "work-1", "status": "pending", "conversation_id": "conv-a",
                                 "title": "整理文件", "task_type": "file.organize"}},
    )

    payload = client.get("/api/approvals?account_id=acct&conversation_id=conv-a").json()

    kinds = {item["approval_id"]: item["kind"] for item in payload["approvals"]}
    assert kinds == {"chat-1": "chat", "work-1": "work"}
    # 迁移期别名：老前端读的是 action_id，代理过去时不能比原来少东西。
    chat_item = next(item for item in payload["approvals"] if item["kind"] == "chat")
    assert chat_item["action_id"] == "chat-1"
    assert chat_item["arguments"] == {"percent": 30}


def test_unified_approval_decide_speaks_one_vocabulary(monkeypatch):
    """统一接口只认 confirmed / rejected；Work 那套 approved 由代理翻译。"""
    client, work_host = _approval_client(
        monkeypatch,
        chat_records={},
        work_records={"work-1": {"approval_id": "work-1", "status": "pending", "conversation_id": "conv-a",
                                 "title": "整理文件", "task_type": "file.organize"}},
    )
    scope = "account_id=acct&conversation_id=conv-a"

    # 统一接口：confirmed
    decided = client.post(f"/api/approvals/work-1?{scope}", json={"decision": "confirmed"}).json()
    assert decided["kind"] == "work" and decided["decision"] == "confirmed"
    assert work_host.decisions == [("work-1", "approved")]

    # 统一接口不接受老词
    rejected = client.post(f"/api/approvals/work-1?{scope}", json={"decision": "approved"})
    assert rejected.status_code == 422, rejected.text


def test_the_old_work_approval_route_still_works_as_a_proxy(monkeypatch):
    """迁移期代理：老前端发 `approved`，落到账本上还是 `approved`。"""
    client, work_host = _approval_client(
        monkeypatch,
        chat_records={},
        work_records={"work-2": {"approval_id": "work-2", "status": "pending", "conversation_id": "conv-a",
                                 "title": "截图", "task_type": "screen.capture"}},
    )
    scope = "account_id=acct&conversation_id=conv-a"

    ok = client.post(f"/api/work/approvals/work-2?{scope}", json={"decision": "approved"})
    assert ok.status_code == 200, ok.text
    assert work_host.decisions == [("work-2", "approved")]

    # `expired` 不接受：让外界能把审批标成过期，等于给了一条绕过拒绝的路。
    expired = client.post(f"/api/work/approvals/work-2?{scope}", json={"decision": "expired"})
    assert expired.status_code == 400, expired.text


def test_the_confirmed_submit_route_is_gone():
    """`/api/work/tasks/confirmed` 已删。

    它的存在理由是"Web 那一跳自己已经问过用户了，所以补一张免审通行证" ——
    这正是网关要收掉的那类旁路：审批必须落在一条**能被再次查询到**的记录上，
    而不是调用方一句"我已经问过了"。前端改走"先提交，再按返回的 approval_id 审批"。
    """
    import web_api

    paths = {route.path for route in web_api.app.routes if hasattr(route, "path")}
    for included in web_api.app.routes:
        router = getattr(included, "original_router", None)
        if router is not None:
            prefix = included.include_context.prefix
            paths.update(prefix + route.path for route in router.routes if hasattr(route, "path"))
    assert "/api/work/tasks/confirmed" not in paths
