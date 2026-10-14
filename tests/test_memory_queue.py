"""候选提炼持久队列与处理端的行为测试（B 计划第二步）。

钉住三件事：入队幂等、提炼失败不阻塞已发布回答、重试复用已保存候选。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_queue import (
    MAX_ATTEMPTS,
    MemoryCandidateQueue,
    MemoryCandidateWorker,
)
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket, scope_from_packet
from agent_core.memory_writes import MemoryWriter
from support.memory_review import FixedReviewProvider, supported_row


class _Embed:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FakeSemanticExtractor:
    """可配置的候选提炼器：返回值、异常、耗时都能摆布，并记录调用次数。"""

    def __init__(self, candidates=None, *, error: Exception | None = None, delay: float = 0.0):
        self.candidates = list(candidates or [])
        self.error = error
        self.delay = delay
        self.calls = 0
        self.known_ids_seen: list[list[str]] = []
        self.provider = None

    async def extract(self, packet, *, known_ids=None):
        self.calls += 1
        self.known_ids_seen.append(list(known_ids or ()))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return list(self.candidates)


def _packet(turn_id: str = "turn-1", **overrides) -> MemoryTurnPacket:
    base = dict(
        turn_id=turn_id,
        occurred_at="2026-10-10T12:00:00+00:00",
        scope_owner="user",
        scope_channel="desktop",
        scope_conversation="conversation",
        user_text="就按这个做，先别部署",
        published_reply="好，已记下：先按方案实施，暂不部署",
    )
    base.update(overrides)
    return MemoryTurnPacket(**base)


def _candidate() -> MemoryCandidate:
    return MemoryCandidate(
        content="先别部署",
        kind="decision",
        content_status="proposed",
        source_types=("user_statement",),
        source_refs=(),
    )


class MemoryCandidateQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(
            self.root / "memory.db", self.root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.writer = MemoryWriter(self.repositories, _Embed())
        self.queue = MemoryCandidateQueue(self.root / "jobs.db")
        self.queue.initialize()
        self.scope = Scope("user", "desktop", "conversation")

    def _records(self) -> list[dict]:
        return self.writer.list_records(self.scope)

    def test_enqueue_is_idempotent_for_the_same_turn(self) -> None:
        first, created = self.queue.enqueue(_packet("turn-9"))
        again, created_again = self.queue.enqueue(_packet("turn-9"))
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first, again)
        self.assertEqual(self.queue.stats()["pending"], 1)

    def test_published_turn_is_not_written_until_the_worker_runs(self) -> None:
        """入队不碰记忆库：正文发布不等提炼。"""
        self.queue.enqueue(_packet("turn-2"))
        self.assertEqual(self._records(), [])
        extractor = _FakeSemanticExtractor([_candidate()])
        extractor.provider = FixedReviewProvider([supported_row('先别部署', [('turn-2', '先别部署')],
            subject='部署安排', change=False, mode='phase')])
        worker = MemoryCandidateWorker(self.queue, extractor, self.writer)
        result = asyncio.run(worker.process_once())
        self.assertEqual(result["stored"], 1)
        self.assertEqual(len(self._records()), 1)

    def test_worker_reads_back_the_stored_packet(self) -> None:
        packet = _packet("turn-3", task_goal="把记忆和搜索接起来", outcome_status="published")
        job_id, _ = self.queue.enqueue(packet)
        job = self.queue.get(job_id)
        restored = MemoryTurnPacket.from_payload(json.loads(job["packet"]))
        self.assertEqual(restored.turn_id, "turn-3")
        self.assertEqual(restored.task_goal, "把记忆和搜索接起来")
        self.assertEqual(scope_from_packet(restored), self.scope)

    def test_cancelled_turn_keeps_its_outcome_status_in_the_packet(self) -> None:
        """取消回合照旧入队（用户说的话仍然算数），但包里必须留着「未发布」。"""
        self.queue.enqueue(_packet("turn-4", published_reply="", outcome_status="cancelled"))
        job = self.queue.claim(limit=1)[0]
        restored = MemoryTurnPacket.from_payload(json.loads(job["packet"]))
        self.assertFalse(restored.is_published())

    def test_timeout_marks_the_job_failed_instead_of_blocking(self) -> None:
        self.queue.enqueue(_packet("turn-5"))
        worker = MemoryCandidateWorker(
            self.queue, _FakeSemanticExtractor([_candidate()], delay=0.2), self.writer, timeout_seconds=0.01
        )
        result = asyncio.run(worker.process_once())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self._records(), [], "超时不得写入任何记忆")
        job = self.queue.claim(limit=5)  # 立刻认领：重试时间没到，应拿不到
        self.assertEqual(job, [])
        self.assertEqual(self.queue.stats()["failed"], 1)

    def test_retry_reuses_stored_candidates_without_calling_the_model_again(self) -> None:
        extractor = _FakeSemanticExtractor([_candidate()])
        extractor.provider = FixedReviewProvider([supported_row('先别部署', [('turn-6', '先别部署')],
            subject='部署安排', change=False, mode='phase')])
        worker = MemoryCandidateWorker(self.queue, extractor, self.writer)
        job_id, _ = self.queue.enqueue(_packet("turn-6"))

        original = self.writer.process_semantic_candidates
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return original(*args, **kwargs)

        self.writer.process_semantic_candidates = flaky  # type: ignore[assignment]
        first = asyncio.run(worker.process_once())
        self.assertEqual(first["failed"], 1)
        self.assertEqual(extractor.calls, 1)
        self.assertTrue(self.queue.get(job_id)["candidates"], "失败前必须先把候选存下来")

        later = datetime.now(UTC) + timedelta(minutes=5)
        second = asyncio.run(worker.process_once(now=later))
        self.assertEqual(second["stored"], 1)
        self.assertEqual(extractor.calls, 1, "重试不得再调一次模型")
        self.assertEqual(extractor.provider.calls, 1, '重试也复用已经持久保存的复核结论')
        self.assertEqual(len(self._records()), 1)

    def test_missing_extractor_skips_and_keeps_the_job_visible(self) -> None:
        self.queue.enqueue(_packet("turn-7"))
        worker = MemoryCandidateWorker(self.queue, None, self.writer)
        result = asyncio.run(worker.process_once())
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.queue.stats()["skipped"], 1)
        self.assertEqual(self.queue.backlog(), 0)

    def test_dead_job_stops_retrying_after_max_attempts(self) -> None:
        self.queue.enqueue(_packet("turn-8"))
        job_id = self.queue.claim(limit=1)[0]["job_id"]
        status = ""
        for _ in range(MAX_ATTEMPTS):
            status = self.queue.fail(job_id, error="boom")
        self.assertEqual(status, "dead")
        self.assertIsNone(self.queue.get(job_id)["next_attempt_at"])
        self.assertEqual(self.queue.claim(limit=5), [])

    def test_pending_jobs_survive_a_restart(self) -> None:
        self.queue.enqueue(_packet("turn-10"))
        reopened = MemoryCandidateQueue(self.root / "jobs.db")
        reopened.initialize()
        self.assertEqual(reopened.stats()["pending"], 1)
        self.assertEqual([job["turn_id"] for job in reopened.claim(limit=5)], ["turn-10"])

    def test_known_ids_come_from_the_packet_recalled_memories(self) -> None:
        from agent_core.memory_turn import MemoryRef

        packet = _packet(
            "turn-11",
            recalled_memory_refs=[MemoryRef("mem-1", "decision", "accepted", excerpt="早前决定")],
        )
        self.queue.enqueue(packet)
        extractor = _FakeSemanticExtractor([_candidate()])
        asyncio.run(MemoryCandidateWorker(self.queue, extractor, self.writer).process_once())
        self.assertEqual(extractor.known_ids_seen, [["mem-1"]])

    def test_default_review_timeout_matches_b_single_call_budget(self) -> None:
        """后台语义复核默认也必须给单次模型调用 60 秒。"""
        worker = MemoryCandidateWorker(self.queue, _FakeSemanticExtractor(), self.writer)
        self.assertEqual(worker.review_timeout_seconds, 60.0)


if __name__ == "__main__":
    unittest.main()
