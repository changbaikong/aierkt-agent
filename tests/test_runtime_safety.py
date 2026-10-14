from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_core.contracts import AccountTaskScope, AgentOutcome, AgentRequest, ProviderResponse, Scope, ToolSpec
from agent_core.conversation_manager import ConversationManager
from agent_core.loop import AgentLoop
from agent_core.outbox import OutboxStore
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.runner import AgentRunner, RECOVERY_TEMPLATES
from agent_core.tools import ToolRegistry


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class QueueProvider:
    max_tokens = 512

    def __init__(self, responses: list[dict]) -> None:
        self.responses = iter(responses)

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        return ProviderResponse(next(self.responses), {"completion_tokens": 1})


def _registry(handler) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "danger",
            {"type": "function", "function": {"name": "danger", "parameters": {"type": "object"}}},
            handler,
            risk="high",
        )
    )
    return registry


@pytest.mark.asyncio
async def test_pause_atomically_persists_run_confirmation_and_account_lock(tmp_path: Path) -> None:
    executed: list[dict] = []

    async def danger(arguments, _scope):
        executed.append(arguments)
        return {"done": True}

    scope = Scope("owner", "desktop", "chat", "companion")
    db = tmp_path / "core.db"
    loop = AgentLoop(
        QueueProvider([{"role": "assistant", "tool_calls": [_call("c1", "danger", {})]}]),
        _registry(danger),
        scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
    )

    outcome = await loop.run(AgentRequest("do it", scope=scope))

    assert outcome.status == "paused"
    assert executed == []
    confirmation_store = SQLiteConfirmationStore(db)
    pending = confirmation_store.get_for_account(outcome.action_id, AccountTaskScope("owner", "companion"))
    assert pending is not None
    assert pending.status == "pending"
    lock = confirmation_store.active_lock(AccountTaskScope("owner", "companion"))
    assert lock is not None
    assert lock.action_id == outcome.action_id
    snapshot = SQLiteRunStore(db).load(outcome.run_id, scope)
    assert snapshot is not None
    assert snapshot.status == "paused"


@pytest.mark.asyncio
async def test_pending_confirmations_are_isolated_by_full_scope(tmp_path: Path) -> None:
    async def danger(_arguments, _scope):
        return {"done": True}

    db = tmp_path / "core.db"
    registry = _registry(danger)
    first_scope = Scope("owner", "desktop", "chat-a", "companion")
    second_scope = Scope("owner", "wechat", "chat-b", "companion")

    first = AgentLoop(
        QueueProvider([{"role": "assistant", "tool_calls": [_call("c1", "danger", {})]}]),
        registry,
        first_scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
    )
    assert (await first.run("first")).status == "paused"

    second = AgentLoop(
        QueueProvider([{"role": "assistant", "tool_calls": [_call("c2", "danger", {})]}]),
        registry,
        second_scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
    )
    outcome = await second.run("second")

    assert outcome.status == "paused"
    store = SQLiteConfirmationStore(db)
    assert len(store.pending_for_scope(first_scope)) == 1
    assert len(store.pending_for_scope(second_scope)) == 1


@pytest.mark.asyncio
async def test_persistent_lock_allows_new_turn_from_other_scope(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat-a", "companion")
    other_scope = Scope("owner", "wechat", "chat-b", "companion")
    db = tmp_path / "core.db"
    store = SQLiteConfirmationStore(db)
    request = store.create(
        run_id="run-1",
        scope=scope,
        tool_call_id="call-1",
        tool="danger",
        arguments={},
        title="Danger",
    )
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)

    class Session:
        async def run(self, request):
            return type("Outcome", (), {"status": "completed", "run_id": "run", "reply": "ok"})()

    manager = ConversationManager(lambda _scope: Session(), confirmation_store=store)
    try:
        handle = await manager.submit(AgentRequest("hello", scope=other_scope))
        assert handle.accepted
        assert (await handle.result).status == "completed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_resume_from_other_scope_is_rejected(tmp_path: Path) -> None:
    original_scope = Scope("owner", "desktop", "chat-a", "companion")
    resume_scope = Scope("owner", "wechat", "chat-b", "companion")
    db = tmp_path / "core.db"
    store = SQLiteConfirmationStore(db)
    request = store.create(
        run_id="run-1",
        scope=original_scope,
        tool_call_id="call-1",
        tool="danger",
        arguments={},
        title="Danger",
    )
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, original_scope)
    calls: list[tuple[str, str]] = []

    class Session:
        def __init__(self, scope):
            self.scope = scope

        async def resume(self, action_id, decision):
            calls.append((self.scope.channel, decision))
            store.complete(action_id, original_scope)
            return type("Outcome", (), {"status": "completed", "run_id": "run-1", "reply": "ok"})()

    manager = ConversationManager(lambda scope: Session(scope), confirmation_store=store)
    try:
        outcome = await manager.resume(request.action_id, resume_scope, "confirmed")
        assert outcome.status == "failed"
        assert calls == []
        assert store.active_lock(original_scope) is not None
    finally:
        await manager.shutdown()


def test_recovery_restores_persistent_locks(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    db = tmp_path / "core.db"
    store = SQLiteConfirmationStore(db)
    request = store.create(
        run_id="run-1",
        scope=scope,
        tool_call_id="call-1",
        tool="danger",
        arguments={},
        title="Danger",
    )
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)

    restarted = SQLiteConfirmationStore(db)
    locks = restarted.recover_locks()

    assert len(locks) == 1
    assert locks[0].action_id == request.action_id


def test_runner_recovery_notifies_without_executing_or_releasing_lock(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(
        run_id="run-1",
        scope=scope,
        tool_call_id="call-1",
        tool="danger",
        arguments={"secret": "must not be delivered"},
        title="Danger",
    )
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)
    outbox = OutboxStore(tmp_path / "outbox.db")

    runner = AgentRunner(None, store, outbox=outbox, delivery_target="desktop")
    notices = runner.recover(scope)

    assert len(notices) == 1
    assert notices[0].action_id == request.action_id
    assert "尚未执行" in notices[0].message
    assert store.active_lock(AccountTaskScope("owner", "companion")) is not None
    assert store.get_for_account(request.action_id, AccountTaskScope("owner", "companion")).status == "pending"
    messages = outbox.pending()
    assert len(messages) == 1
    assert messages[0].payload == {"action_id": request.action_id, "title": "Danger", "tool": "danger"}


@pytest.mark.asyncio
async def test_runner_recovery_falls_back_when_language_formatter_fails(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(run_id="run-1", scope=scope, tool_call_id="call-1", tool="danger", arguments={}, title="Danger")
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)

    async def unavailable(_request):
        raise RuntimeError("model unavailable")

    notices = await AgentRunner(None, store).recover_with_language(scope, unavailable)

    assert len(notices) == 1
    assert "尚未执行" in notices[0].message
    assert store.active_lock(AccountTaskScope("owner", "companion")) is not None


@pytest.mark.asyncio
async def test_runner_recovery_falls_back_when_language_formatter_times_out(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(run_id="run-1", scope=scope, tool_call_id="call-1", tool="danger", arguments={}, title="Danger")
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)

    async def slow_formatter(_request, _base_template):
        await asyncio.sleep(0.05)
        return "unsafe"

    notices = await AgentRunner(None, store).recover_with_language(
        scope, slow_formatter, formatter_timeout_seconds=0.001
    )

    assert len(notices) == 1
    assert "尚未执行" in notices[0].message


def test_recovery_template_rotation_is_persistent_and_scoped(tmp_path: Path) -> None:
    db = tmp_path / "core.db"
    first = SQLiteConfirmationStore(db)
    account = AccountTaskScope("owner", "companion")
    other = AccountTaskScope("other", "companion")

    indexes = [first.next_recovery_template_index(account, len(RECOVERY_TEMPLATES)) for _ in range(30)]
    assert indexes == list(range(30))
    assert first.next_recovery_template_index(account, len(RECOVERY_TEMPLATES)) == 0
    assert first.next_recovery_template_index(other, len(RECOVERY_TEMPLATES)) == 0

    restarted = SQLiteConfirmationStore(db)
    assert restarted.next_recovery_template_index(account, len(RECOVERY_TEMPLATES)) == 1


@pytest.mark.asyncio
async def test_recovery_language_formatter_receives_rotating_safe_template(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(run_id="run-1", scope=scope, tool_call_id="call-1", tool="danger", arguments={"secret": "hidden"}, title="Danger")
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)
    received: list[str] = []

    async def formatter(_request, base_template):
        received.append(base_template)
        return base_template

    runner = AgentRunner(None, store)
    first = await runner.recover_with_language(scope, formatter)
    second = await runner.recover_with_language(scope, formatter)

    assert len(received) == 2
    assert received[0] != received[1]
    assert first[0].message == received[0]
    assert second[0].message == received[1]
    assert "hidden" not in received[0]
    assert "尚未执行" in received[0]


def test_all_recovery_templates_preserve_safety_facts() -> None:
    request = type("Request", (), {"title": "创建定时任务", "tool": "tasks__create"})()

    rendered = [template.format(title=request.title, tool=request.tool) for template in RECOVERY_TEMPLATES]

    assert len(RECOVERY_TEMPLATES) == 30
    assert len(set(RECOVERY_TEMPLATES)) == 30
    assert all("尚未执行" in text for text in rendered)
    assert all(any(word in text for word in ("确认", "确定", "同意")) for text in rendered)
    assert all(any(word in text for word in ("取消", "重置待确认")) for text in rendered)


@pytest.mark.asyncio
async def test_completed_confirmation_releases_once_and_records_its_run(tmp_path: Path) -> None:
    executions: list[dict] = []

    async def danger(arguments, _scope):
        executions.append(arguments)
        return {"done": True}

    scope = Scope("owner", "desktop", "chat", "companion")
    db = tmp_path / "core.db"
    loop = AgentLoop(
        QueueProvider([
            {"role": "assistant", "tool_calls": [_call("c1", "danger", {"value": 1})]},
            {"role": "assistant", "content": "done"},
        ]),
        _registry(danger),
        scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
    )
    paused = await loop.run("do it")

    completed = await loop.resume(paused.action_id, "confirmed")
    repeated = await loop.resume(paused.action_id, "confirmed")

    assert completed.status == "completed"
    assert repeated.status == "failed"
    assert executions == [{"value": 1}]
    store = SQLiteConfirmationStore(db)
    assert store.active_lock(AccountTaskScope("owner", "companion")) is None
    events = store.events_for_run(paused.run_id)
    assert any(event["event_type"] == "confirmation.completed" for event in events)


@pytest.mark.asyncio
async def test_other_scope_cannot_clear_original_pending_confirmation(tmp_path: Path) -> None:
    original_scope = Scope("owner", "desktop", "chat-a", "companion")
    other_scope = Scope("owner", "wechat", "chat-b", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(
        run_id="run-1", scope=original_scope, tool_call_id="call-1", tool="danger", arguments={}, title="Danger",
    )
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, original_scope)

    class Session:
        async def run(self, _request):
            return type("Outcome", (), {"status": "completed", "run_id": "run", "reply": "ok"})()

    manager = ConversationManager(lambda _scope: Session(), confirmation_store=store)
    try:
        manager.lock_conversation(original_scope.conversation_id, "waiting_confirmation", request.action_id, scope=original_scope)
        assert manager.is_locked(original_scope.conversation_id)

        count = await manager.reset_pending(other_scope)

        assert count == 0
        assert manager.is_locked(original_scope)
        handle = await manager.submit(AgentRequest("new message", scope=other_scope))
        assert handle.accepted
        assert (await handle.result).status == "completed"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_runner_internal_interface_delegates_without_owning_execution() -> None:
    calls: list[tuple[str, str]] = []

    class Session:
        async def run(self, request):
            calls.append(("run", request.user_text))
            return AgentOutcome("completed", "run-1", reply="ok")

        async def resume(self, action_id, decision):
            calls.append((action_id, decision))
            return AgentOutcome("completed", "run-1", reply="ok")

    runner = AgentRunner(None, None)
    request = AgentRequest("background", scope=Scope())

    assert (await runner.run_internal(Session(), request)).status == "completed"
    assert (await runner.resume_internal(Session(), "action-1", "confirmed")).status == "completed"
    assert calls == [("run", "background"), ("action-1", "confirmed")]


def test_recovery_rejects_legacy_competing_pending_actions(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    first = store.create(run_id="run-1", scope=scope, tool_call_id="call-1", tool="danger", arguments={}, title="First")
    second = store.create(run_id="run-2", scope=scope, tool_call_id="call-2", tool="danger", arguments={}, title="Second")

    locks = store.recover_locks()

    assert len(locks) == 1
    pending = store.pending_for_account(AccountTaskScope("owner", "companion"))
    assert len(pending) == 1
    rejected = store.get_for_account(second.action_id, AccountTaskScope("owner", "companion"))
    assert rejected is not None
    assert rejected.status == "rejected"


@pytest.mark.asyncio
async def test_rejected_confirmation_never_resumes_the_model(tmp_path: Path) -> None:
    scope = Scope("owner", "desktop", "chat", "companion")
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(run_id="run-1", scope=scope, tool_call_id="call-1", tool="danger", arguments={}, title="Danger")
    store.create_lock(AccountTaskScope("owner", "companion"), request.action_id, scope)
    calls: list[str] = []

    class Session:
        async def resume(self, _action_id, _decision):
            calls.append("resume")
            return AgentOutcome("completed", "run-1", reply="unexpected")

    manager = ConversationManager(lambda _scope: Session(), confirmation_store=store)
    try:
        outcome = await manager.resume(request.action_id, scope, "rejected")
        assert outcome.status == "cancelled"
        assert calls == []
        assert store.active_lock(AccountTaskScope("owner", "companion")) is None
    finally:
        await manager.shutdown()
