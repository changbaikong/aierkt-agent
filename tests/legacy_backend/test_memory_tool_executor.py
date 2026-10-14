"""Memory tool executor routing and scope-boundary tests."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.channel_actions import create_memory_tool_executor
from backend.memory import MemoryScope


def _make_llm() -> MagicMock:
    llm = MagicMock()
    llm.judge = AsyncMock(return_value="accepted")
    return llm


class MemoryToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = MemoryScope("test-user", "desktop", "test-chat")

    async def test_memory_search_calls_retrieve_memories(self) -> None:
        executor = create_memory_tool_executor(_make_llm(), self.scope)
        with patch("backend.memory.retrieve_memories", new=AsyncMock(return_value="found")) as retrieve:
            result = await executor("memory_search", {"query": "earlier topic"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"]["memory"], "found")
        self.assertEqual(retrieve.call_args.kwargs["scope"], self.scope)

    async def test_memory_search_no_hit_is_honest(self) -> None:
        executor = create_memory_tool_executor(_make_llm(), self.scope)
        with patch("backend.memory.retrieve_memories", new=AsyncMock(return_value="")):
            result = await executor("memory_search", {"query": "missing"})
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["output"]["memory"])

    async def test_diary_and_internet_tools_bind_scope(self) -> None:
        executor = create_memory_tool_executor(_make_llm(), self.scope)
        with patch("backend.memory.retrieve_secret_diary", new=AsyncMock(return_value="diary")) as diary:
            diary_result = await executor("diary_lookup", {"topic": "mood"})
        with patch("backend.memory.retrieve_internet_mind", return_value="news") as internet:
            internet_result = await executor("internet_mind_lookup", {"topic": "tech"})
        self.assertEqual(diary_result["output"]["diary"], "diary")
        self.assertEqual(internet_result["output"]["internet_mind"], "news")
        self.assertEqual(diary.call_args.kwargs["scope"], self.scope)
        self.assertEqual(internet.call_args.kwargs["scope"], self.scope)

    async def test_memory_save_writes_to_bound_scope(self) -> None:
        executor = create_memory_tool_executor(_make_llm(), self.scope)
        with patch("backend.memory.save_fact") as save_fact:
            result = await executor("memory_save", {"fact": "user changed jobs", "category": "work", "importance": 3})
        self.assertEqual(result["status"], "ok")
        save_fact.assert_called_once()
        self.assertEqual(save_fact.call_args.kwargs["scope"], self.scope)
        self.assertEqual(save_fact.call_args.kwargs["importance"], 3)

    async def test_unbound_executor_cannot_write_long_term_memory(self) -> None:
        executor = create_memory_tool_executor(_make_llm())
        with patch("backend.memory.save_fact") as save_fact:
            result = await executor("memory_save", {"fact": "must not leak"})
        self.assertEqual(result["status"], "failed")
        save_fact.assert_not_called()

    async def test_empty_fact_and_unknown_tool_fail(self) -> None:
        executor = create_memory_tool_executor(_make_llm(), self.scope)
        empty = await executor("memory_save", {"fact": ""})
        unknown = await executor("unknown", {})
        self.assertEqual(empty["status"], "failed")
        self.assertEqual(unknown["status"], "failed")


if __name__ == "__main__":
    unittest.main()
