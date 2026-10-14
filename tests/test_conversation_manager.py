from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core.contracts import AgentOutcome, AgentRequest, ProviderResponse, Scope
from agent_core.conversation_manager import ConversationManager
from agent_core.conversation_repository import ConversationRepository
from agent_core.events import EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteRunStore
from agent_core.tools import ToolRegistry


class FakeSession:
    def __init__(self, scope: Scope, calls: list[tuple[str, str]], gates: dict[str, asyncio.Event]) -> None:
        self.scope = scope
        self.calls = calls
        self.gates = gates
        self.loop = SimpleNamespace(public_messages=[])

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self.calls.append((self.scope.conversation_id, request.user_text))
        gate = self.gates.get(self.scope.conversation_id)
        if gate is not None and request.user_text == "first":
            await gate.wait()
        self.loop.public_messages = [*request.history, {"role": "user", "content": request.user_text}, {"role": "assistant", "content": request.user_text}]
        return AgentOutcome("completed", request.user_text, reply=request.user_text)


class ConversationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_timeout_writes_terminal_failure_reply(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        scope = Scope("owner", "web", "turn-timeout", "companion")
        repository = ConversationRepository(root / "core.db")

        class SlowSession:
            def __init__(self) -> None:
                self.loop = SimpleNamespace(public_messages=[])

            async def run(self, request: AgentRequest) -> AgentOutcome:
                await asyncio.sleep(1)
                return AgentOutcome("completed", request.turn_id or "", reply="late")

        manager = ConversationManager(
            lambda _scope: SlowSession(),
            conversation_repository=repository,
            turn_timeout_seconds=0.01,
        )
        self.addAsyncCleanup(manager.shutdown)

        handle = await manager.submit(AgentRequest("stuck", scope=scope))
        outcome = await asyncio.wait_for(handle.result, timeout=0.5)

        self.assertEqual(outcome.status, "failed")
        history = repository.history(scope)
        self.assertEqual(history[-1]["role"], "assistant")
        self.assertIn("失败", history[-1]["content"])

    async def test_model_expression_is_not_overwritten_by_keyword_policy(self):
        scope = Scope('owner', 'web', 'expression')
        bus = InMemoryEventBus()
        events = []
        bus.subscribe(EventType.PORTRAIT_CHANGED, events.append)

        class Session:
            def __init__(self):
                self.loop = SimpleNamespace(public_messages=[], _model_reply_seen=True)

            async def run(self, request):
                return AgentOutcome('completed', 'expression', reply='太好了，但是有点害羞。')

        manager = ConversationManager(lambda _: Session(), event_bus=bus)
        self.addAsyncCleanup(manager.shutdown)
        handle = await manager.submit(AgentRequest('你好', scope=scope))
        await handle.result
        self.assertFalse(any(event.data['phase'] == 'reply' for event in events))

    async def test_full_scope_lock_does_not_block_same_id_in_another_scope(self) -> None:
        calls: list[tuple[str, str]] = []
        first = Scope("first-owner", "web", "same-id", "companion")
        second = Scope("second-owner", "web", "same-id", "companion")
        manager = ConversationManager(lambda current: FakeSession(current, calls, {}))
        self.addAsyncCleanup(manager.shutdown)

        manager.lock_conversation(first, "manual_pause", "first-action")

        rejected = await manager.submit(AgentRequest("blocked", scope=first))
        accepted = await manager.submit(AgentRequest("independent", scope=second))

        self.assertFalse(rejected.accepted)
        self.assertTrue(accepted.accepted)
        self.assertEqual((await accepted.result).status, "completed")

    async def test_idle_cache_is_evicted_and_hydrated_from_repository(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        scope = Scope("owner", "web", "cached", "companion")
        seen_histories: list[list[dict]] = []

        class Session:
            def __init__(self) -> None:
                self.loop = SimpleNamespace(public_messages=[])

            async def run(self, request: AgentRequest) -> AgentOutcome:
                seen_histories.append(list(request.history))
                self.loop.public_messages = [
                    *request.history,
                    {"role": "user", "content": request.user_text},
                    {"role": "assistant", "content": f"reply:{request.user_text}"},
                ]
                return AgentOutcome("completed", request.user_text, reply=f"reply:{request.user_text}")

        manager = ConversationManager(
            lambda _scope: Session(),
            conversation_repository=ConversationRepository(root / "core.db"),
            idle_ttl_seconds=0.01,
        )
        self.addAsyncCleanup(manager.shutdown)

        first = await manager.submit(AgentRequest("first", scope=scope))
        self.assertEqual((await first.result).status, "completed")
        await asyncio.sleep(0.05)
        self.assertFalse(manager.is_resident(scope))

        second = await manager.submit(AgentRequest("second", scope=scope))
        self.assertEqual((await second.result).status, "completed")
        self.assertEqual(
            [(item["role"], item["content"]) for item in seen_histories[-1]],
            [("user", "first"), ("assistant", "reply:first")],
        )
        self.assertTrue(all("created_at" in item for item in seen_histories[-1]))

    async def test_progress_written_during_a_turn_stays_after_the_user_message(self) -> None:
        """过程气泡可以即时入库，但刷新后的顺序必须仍是用户提问在前。"""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        scope = Scope("owner", "web", "progress-order", "companion")
        repository = ConversationRepository(root / "core.db")
        manager = None

        class Session:
            def __init__(self) -> None:
                self.loop = SimpleNamespace(public_messages=[])

            async def run(self, request: AgentRequest) -> AgentOutcome:
                manager.append_message(
                    scope,
                    "assistant",
                    "嗯，交给我吧。我先去看看。",
                    stable_key="steward:accepted",
                    touch_activity=False,
                    kind="steward_progress",
                )
                self.loop.public_messages = [
                    *request.history,
                    {"role": "user", "content": request.user_text},
                    {"role": "assistant", "content": "我看完了。"},
                ]
                return AgentOutcome("completed", "run", reply="我看完了。")

        manager = ConversationManager(lambda _scope: Session(), conversation_repository=repository)
        self.addAsyncCleanup(manager.shutdown)

        handle = await manager.submit(AgentRequest("你去看一下这篇论文", scope=scope))
        self.assertEqual((await handle.result).status, "completed")

        self.assertEqual(
            [(item["role"], item["content"]) for item in repository.history(scope)],
            [
                ("user", "你去看一下这篇论文"),
                ("assistant", "嗯，交给我吧。我先去看看。"),
                ("assistant", "我看完了。"),
            ],
        )

    async def test_submit_emits_scope_for_a_scope_dataclass_without_to_dict(self) -> None:
        calls: list[tuple[str, str]] = []
        event_bus = InMemoryEventBus()
        events = []
        event_bus.subscribe(EventType.TURN_STARTED, events.append)
        scope = Scope("owner", "desktop", "chat", "companion")
        manager = ConversationManager(
            lambda current: FakeSession(current, calls, {}),
            event_bus=event_bus,
        )
        self.addAsyncCleanup(manager.shutdown)

        handle = await manager.submit(AgentRequest("hello", scope=scope))

        self.assertTrue(handle.accepted)
        self.assertEqual((await handle.result).status, "completed")
        event = events[0]
        self.assertEqual(event.scope, {
            "owner_id": "owner",
            "channel": "desktop",
            "conversation_id": "chat",
            "module_id": "companion",
        })

    async def test_same_conversation_queues_turns_in_order(self) -> None:
        calls: list[tuple[str, str]] = []
        gate = asyncio.Event()
        scope = Scope("owner", "desktop", "chat")
        manager = ConversationManager(lambda current: FakeSession(current, calls, {"chat": gate}))
        self.addAsyncCleanup(manager.shutdown)

        first = await manager.submit(AgentRequest("first", scope=scope))
        second = await manager.submit(AgentRequest("second", scope=scope))
        third = await manager.submit(AgentRequest("third", scope=scope))
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertTrue(third.accepted)
        await asyncio.sleep(0)
        self.assertEqual(calls, [("chat", "first")])

        gate.set()
        self.assertEqual((await first.result).reply, "first")
        self.assertEqual((await second.result).reply, "second")
        self.assertEqual((await third.result).reply, "third")
        self.assertEqual(calls, [("chat", "first"), ("chat", "second"), ("chat", "third")])

    async def test_different_conversations_run_independently(self) -> None:
        calls: list[tuple[str, str]] = []
        gate = asyncio.Event()
        manager = ConversationManager(lambda scope: FakeSession(scope, calls, {"chat-a": gate}))
        self.addAsyncCleanup(manager.shutdown)
        first = await manager.submit(AgentRequest("first", scope=Scope("owner", "desktop", "chat-a")))
        second = await manager.submit(AgentRequest("other", scope=Scope("owner", "desktop", "chat-b")))

        self.assertEqual((await second.result).reply, "other")
        self.assertFalse(first.result.done())
        gate.set()
        self.assertEqual((await first.result).reply, "first")

    async def test_queue_rejects_the_21st_pending_turn(self) -> None:
        calls: list[tuple[str, str]] = []
        gate = asyncio.Event()
        scope = Scope("owner", "desktop", "chat")
        manager = ConversationManager(lambda current: FakeSession(current, calls, {"chat": gate}), max_queue_size=20)
        self.addAsyncCleanup(manager.shutdown)
        await manager.submit(AgentRequest("first", scope=scope))
        handles = [await manager.submit(AgentRequest(f"queued-{i}", scope=scope)) for i in range(21)]

        self.assertTrue(all(handle.accepted for handle in handles[:20]))
        self.assertFalse(handles[-1].accepted)
        self.assertIn("queue", handles[-1].reason.lower())
        gate.set()

    async def test_waiting_confirmation_allows_new_turns(self) -> None:
        scope = Scope("owner", "desktop", "chat")
        manager = ConversationManager(lambda current: FakeSession(current, [], {}))
        self.addAsyncCleanup(manager.shutdown)
        manager.lock_conversation(scope.conversation_id, "waiting_confirmation", "sensitive-action-id")

        handle = await manager.submit(AgentRequest("设置了吗", scope=scope))

        self.assertTrue(handle.accepted)
        self.assertEqual((await handle.result).status, "completed")

    async def test_shutdown_cancels_active_worker_and_resolves_pending_results(self) -> None:
        calls: list[tuple[str, str]] = []
        gate = asyncio.Event()
        scope = Scope("owner", "desktop", "chat")
        manager = ConversationManager(lambda current: FakeSession(current, calls, {"chat": gate}))
        first = await manager.submit(AgentRequest("first", scope=scope))
        second = await manager.submit(AgentRequest("second", scope=scope))

        await manager.shutdown()

        self.assertTrue(first.result.done())
        self.assertTrue(second.result.done())
        self.assertEqual((await first.result).status, "cancelled")
        self.assertEqual((await second.result).status, "cancelled")

    async def test_clear_history_also_clears_intent_state(self) -> None:
        """清空会话不能只删气泡：派生出来的意图状态也要一起失效。"""
        from datetime import datetime

        from agent_core.contracts import ConversationIntentState
        from agent_core.intent import SQLiteIntentStateStore

        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        scope = Scope("owner", "web", "clear-intent", "companion")
        repository = ConversationRepository(root / "core.db")
        store = SQLiteIntentStateStore(root / "core.db")
        repository.ensure(scope)
        repository.append_message(scope, {"role": "user", "content": "会被清空"})
        store.save(scope, ConversationIntentState(
            active_intent="weather_query",
            pending_intent="weather_query",
            slots={"location": "佛山"},
            updated_at=datetime.now().isoformat(timespec="seconds"),
            turns=1,
        ))
        manager = ConversationManager(
            lambda current: FakeSession(current, [], {}),
            conversation_repository=repository,
            intent_state_store=store,
        )
        self.addAsyncCleanup(manager.shutdown)

        manager.clear_history(scope)

        self.assertIsNone(store.load(scope), "清空后不应再读到旧意图")
        self.assertIsNone(
            SQLiteIntentStateStore(root / "core.db").load(scope),
            "清空必须真的删掉库里的行，换一个实例也读不回来",
        )
        self.assertEqual(repository.history(scope), [])


class ConversationManagerForgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_forget_removes_runtime_lock_and_intent_state(self) -> None:
        from datetime import datetime

        from agent_core.contracts import ConversationIntentState
        from agent_core.intent import InMemoryIntentStateStore

        scope = Scope("owner", "web", "forget-me", "companion")
        store = InMemoryIntentStateStore()
        store.save(scope, ConversationIntentState(
            pending_intent="weather_query",
            updated_at=datetime.now().isoformat(timespec="seconds"),
            turns=1,
        ))
        manager = ConversationManager(lambda current: FakeSession(current, [], {}), intent_state_store=store)
        self.addAsyncCleanup(manager.shutdown)
        # 先制造一个 resident 运行态与进程内锁，forget 必须一并收掉
        handle = await manager.submit(AgentRequest("first", scope=scope))
        await handle.result
        manager.lock_conversation(scope, "waiting_confirmation", "action-x")

        await manager.forget(scope)

        self.assertIsNone(store.load(scope))
        self.assertFalse(manager.is_resident(scope))
        self.assertFalse(manager.is_locked(scope))


class BlockingProvider:
    max_tokens = 512

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None) -> ProviderResponse:
        self.started.set()
        await self.release.wait()
        return ProviderResponse({"role": "assistant", "content": "completed"})


class AgentLoopCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_provider_run_is_saved_as_cancelled(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        provider = BlockingProvider()
        scope = Scope("owner", "desktop", "cancelled")
        run_store = SQLiteRunStore(root / "runs.db")
        loop = AgentLoop(provider, ToolRegistry(), scope, run_store=run_store)
        task = asyncio.create_task(loop.run(AgentRequest("hello", scope=scope)))
        await provider.started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        snapshot = run_store.load(loop.run_id, scope)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.status, "cancelled")
        self.assertEqual(loop.public_messages[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
