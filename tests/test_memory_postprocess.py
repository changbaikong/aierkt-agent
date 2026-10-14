from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_extract import ExtractedMemory
from agent_core.memory_postprocess import extract_and_store_memory_turn
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_writes import MemoryWriter


class Embed:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class FakeExtractor:
    def __init__(self, memories: list[ExtractedMemory]) -> None:
        self.memories = memories
        self.calls: list[tuple[str, str, list[dict] | None]] = []

    async def extract(self, user_text: str, *, assistant_reply: str = "", history: list[dict] | None = None):
        self.calls.append((user_text, assistant_reply, history))
        return self.memories


class MemoryPostprocessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repo = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repo.close)
        self.writer = MemoryWriter(self.repo, Embed())
        self.scope = Scope("user", "web", "conversation")

    async def test_extract_and_store_memory_turn_persists_real_rows(self) -> None:
        extractor = FakeExtractor([
            ExtractedMemory(
                content="用户最近在赶开题报告",
                conflict_key="user.task.current",
                confidence=0.9,
                importance=0.8,
                duration="session",
            )
        ])

        result = await extract_and_store_memory_turn(
            extractor,
            self.writer,
            self.scope,
            "我最近在赶开题报告",
            "我记住了。",
            history=[{"role": "user", "content": "我最近在赶开题报告"}],
            turn_id="turn-123",
            run_id="run-456",
        )

        self.assertEqual(result["status"], "stored")
        self.assertEqual(result["extracted"], 1)
        self.assertEqual(result["persisted"], 1)
        record_id = result["persisted_ids"][0]
        row = self.repo.records_by_ids([record_id])[record_id]
        metadata = json.loads(row["metadata"])
        self.assertEqual(row["content"], "用户最近在赶开题报告")
        self.assertEqual(metadata["source"], "web.async_memory_extraction")
        self.assertEqual(metadata["turn_id"], "turn-123")
        self.assertEqual(metadata["run_id"], "run-456")
        self.assertEqual(metadata["source_type"], "llm_extracted")
        self.assertEqual(metadata["user_text"], "我最近在赶开题报告")
        self.assertEqual(metadata["assistant_reply"], "我记住了。")
        self.assertEqual(extractor.calls[0][0], "我最近在赶开题报告")
        self.assertEqual(extractor.calls[0][1], "我记住了。")

    async def test_extract_and_store_memory_turn_skips_when_unconfigured(self) -> None:
        result = await extract_and_store_memory_turn(
            None,
            None,
            self.scope,
            "hello",
        )
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["persisted"], 0)
