"""云端提炼工人走新版语义路径（B 计划第五步）。

钉三件事：带处理包的上传按七类候选落地；老包（一问一答）标成缺背景仍能提炼；
开关开着但云端拿不到主回答模型配置时走失败通道留痕，不静默退回旧陪伴模型。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket
from agent_core.memory_writes import MemoryWriter
from cloud_memory.store import CloudMemoryStore
from cloud_memory.worker import CloudMemoryWorker, packet_from_turn
from support.memory_review import FixedReviewProvider, supported_row


class _Embed:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _StubSemanticExtractor:
    def __init__(self) -> None:
        self.packets: list[MemoryTurnPacket] = []

    async def extract(self, packet, *, known_ids=None):
        self.packets.append(packet)
        return [
            MemoryCandidate(
                content="决定让主模型参与候选提炼",
                kind="decision",
                content_status="proposed",
                source_types=("user_statement",),
                reason="不想陪伴记忆和项目记忆分裂",
                source_refs=(packet.turn_id,),
            )
        ]


class _LegacyExtractorRecorder:
    """旧陪伴提炼器的替身：只记录有没有被调用。"""

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, user_text, *, assistant_reply="", history=None):
        self.calls += 1
        return []


def _turn(**overrides) -> dict:
    base = {
        "turn_id": "t1",
        "owner_id": "user",
        "channel": "web",
        "conversation_id": "c1",
        "module_id": "companion",
        "user_text": "就按这个做",
        "assistant_reply": "好，先按方案实施",
        "occurred_at": "2026-10-10T12:00:00+00:00",
    }
    base.update(overrides)
    return base


class PacketFromTurnTests(unittest.TestCase):
    def test_legacy_turn_is_marked_partial_instead_of_pretending_to_be_complete(self) -> None:
        packet = packet_from_turn(_turn())
        self.assertEqual(packet.completeness, "partial")
        self.assertIn("relevant_history", packet.omitted_sections)

    def test_packet_survives_the_round_trip(self) -> None:
        original = MemoryTurnPacket(
            turn_id="t2",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope_owner="user",
            scope_channel="web",
            scope_conversation="c1",
            user_text="问题",
            published_reply="回答",
            task_goal="把记忆和搜索接起来",
        )
        import json

        packet = packet_from_turn(_turn(turn_id="t2", packet=json.dumps(original.as_payload(), ensure_ascii=False)))
        self.assertEqual(packet.completeness, "complete")
        self.assertEqual(packet.task_goal, "把记忆和搜索接起来")

    def test_broken_packet_falls_back_to_partial_instead_of_failing_the_turn(self) -> None:
        packet = packet_from_turn(_turn(packet="{not json"))
        self.assertEqual(packet.completeness, "partial")


class CloudWorkerSemanticPathTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = CloudMemoryStore(root / "cloud.db")
        self.store.initialize()
        self.repositories = initialize_memory_repositories(
            root / "records.db", root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.writer = MemoryWriter(self.repositories, _Embed())
        self.scope = Scope("user", "web", "c1", "companion")
        # 处理端必须显式指定：云端跑新版提炼的前提是「本端被指定为处理端」。
        # 不写这一项时默认是本地 —— 两端同时处理会让同一个包被提炼两遍。
        env = mock.patch.dict(
            os.environ,
            {"MEMORY_SEMANTIC_ENABLED": "true", "MEMORY_PROCESSING_ENDPOINT": "cloud"},
        )
        env.start()
        self.addCleanup(env.stop)

    def _worker(self, *, semantic=None, legacy=None) -> CloudMemoryWorker:
        return CloudMemoryWorker(
            self.repositories,
            self.store,
            self.writer,
            legacy,
            semantic_extractor=semantic,
            interval_seconds=0,
            batch_size=10,
        )

    def test_turn_with_packet_is_stored_as_semantic_candidates(self) -> None:
        extractor = _StubSemanticExtractor()
        extractor.provider = FixedReviewProvider([supported_row('决定让主模型参与候选提炼',
            [('t-ok', '决定让主模型参与候选提炼')], subject='候选提炼方式', change=False)])
        worker = self._worker(semantic=extractor)
        self.store.enqueue_turn(_turn(turn_id="t-ok", user_text='决定让主模型参与候选提炼'))
        asyncio.run(worker.run_once())

        records = self.writer.list_records(self.scope)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "decision")
        self.assertEqual(records[0]["content_status"], "proposed")
        self.assertEqual(self.store.queue_counts().get("stored", 0), 1)
        self.assertEqual(extractor.packets[0].completeness, "partial")
        self.assertEqual(extractor.provider.calls, 1)

    def test_missing_semantic_extractor_is_recorded_as_failure(self) -> None:
        """开关开着但云端没配主回答模型：留痕，不悄悄退回旧陪伴提炼器。"""
        legacy = _LegacyExtractorRecorder()
        worker = self._worker(semantic=None, legacy=legacy)
        self.store.enqueue_turn(_turn(turn_id="t-missing"))
        asyncio.run(worker.run_once())

        self.assertEqual(legacy.calls, 0)
        self.assertEqual(self.writer.list_records(self.scope), [])
        self.assertEqual(worker.stats["failed"], 1)

    def test_legacy_extractor_is_used_when_the_switch_is_off(self) -> None:
        off = mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "false"})
        off.start()
        try:
            legacy = _LegacyExtractorRecorder()
            worker = self._worker(semantic=_StubSemanticExtractor(), legacy=legacy)
        finally:
            off.stop()
        self.store.enqueue_turn(_turn(turn_id="t-legacy"))
        asyncio.run(worker.run_once())
        self.assertEqual(legacy.calls, 1)


if __name__ == "__main__":
    unittest.main()
