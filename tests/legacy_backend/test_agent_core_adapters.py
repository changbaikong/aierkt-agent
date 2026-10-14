from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from agent_core.contracts import ProviderResponse, Scope, ToolCall
from agent_core.tools import ToolExecutor
from backend.agent_core_adapters.llm_provider import LLMClientProvider
from backend.agent_core_adapters.legacy_tools import registry_from_definitions
from backend.agent_core_adapters.sqlite_memory import SQLiteMemoryStore


class AgentCoreAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_provider_maps_backend_response(self) -> None:
        class FakeLLM:
            max_tokens = 321

            async def complete(self, messages, **kwargs):
                self.kwargs = kwargs
                return {"message": {"role": "assistant", "content": "ok"}, "usage": {"completion_tokens": 4}}

        llm = FakeLLM()
        response = await LLMClientProvider(llm).complete([], tools=[], max_tokens=20, temperature=0.0)

        self.assertIsInstance(response, ProviderResponse)
        self.assertEqual(response.message["content"], "ok")
        self.assertEqual(response.usage["completion_tokens"], 4)
        self.assertEqual(llm.kwargs["max_tokens"], 20)

    async def test_legacy_definitions_are_registered_without_backend_scope_in_core(self) -> None:
        seen: list[dict] = []

        async def handler(arguments):
            seen.append(arguments)
            return {"value": "ok"}

        registry = registry_from_definitions([
            SimpleNamespace(name="echo", schema={"type": "function"}, handler=handler, risk="low", timeout_seconds=3.0, confirmation_title=""),
        ])
        spec = registry.resolve("echo")
        self.assertIsNotNone(spec)
        result = await ToolExecutor().execute(spec, ToolCall("c1", "echo", {"x": 1}), Scope("owner", "wechat", "chat"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(seen, [{"x": 1}])

    def test_sqlite_memory_adapter_binds_every_read_to_scope(self) -> None:
        scope = Scope("alice", "wechat", "chat-a")
        with patch("backend.memory.recall_hot", return_value=[{"content": "hello"}]) as recall_hot:
            store = SQLiteMemoryStore()
            result = store.recall_hot(scope, "hello")

        self.assertEqual(result, [{"content": "hello"}])
        bound = recall_hot.call_args.args[0]
        self.assertEqual((bound.owner_id, bound.channel, bound.conversation_id), ("alice", "wechat", "chat-a"))

    async def test_compat_loop_preserves_legacy_outcome_surface(self) -> None:
        from backend.agent_core_adapters.legacy_tools import CompatAgentLoop
        from backend.memory import MemoryScope

        class FakeLLM:
            max_tokens = 256

            def __init__(self) -> None:
                self.messages = [{"role": "system", "content": "test"}]

            async def complete(self, messages, **kwargs):
                return {"message": {"role": "assistant", "content": "ok"}, "usage": {"completion_tokens": 1}}

        definition = SimpleNamespace(
            name="echo",
            schema={"type": "function"},
            handler=lambda arguments: {"ok": True},
            risk="low",
            timeout_seconds=3.0,
            confirmation_title="",
        )
        fake_httpx = SimpleNamespace(AsyncClient=object)
        with patch.dict(sys.modules, {"httpx": fake_httpx}), tempfile.TemporaryDirectory() as directory:
            outcome = await CompatAgentLoop(
                FakeLLM(),
                [definition],
                MemoryScope("compat", "desktop", "chat"),
                db_path=Path(directory) / "agent.db",
            ).run("hello")

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.reply, "ok")


    def test_sqlite_memory_adapter_preserves_fact_metadata_and_source(self) -> None:
        scope = Scope("alice", "desktop", "chat-a")
        with patch("backend.memory.save_fact") as save_fact, patch("backend.memory.save_raw_message") as save_message:
            store = SQLiteMemoryStore()
            store.save_fact(scope, "fact", confidence=0.7, source_type="candidate_confirmed", conflict_key="pref", expires_at="2030-01-01T00:00:00")
            store.append_message(scope, "user", "hello", source="ephemeral")

        self.assertEqual(save_fact.call_args.kwargs["confidence"], 0.7)
        self.assertEqual(save_fact.call_args.kwargs["conflict_key"], "pref")
        self.assertEqual(save_message.call_args.kwargs["source"], "ephemeral")


if __name__ == "__main__":
    unittest.main()
