"""云端处理端的可靠性与来源保护：和本地那套用同一批判定。

云端不是另一套规则：更新建议先落库、执行失败按失败重试、证据不足才算拒绝。
以前这里是「应用失败只计一个拒绝数，然后照常把这轮标成完成」——
用户那次纠正没写成，队列里也看不出任何异常。
"""
from __future__ import annotations

from support.memory_review import FixedReviewProvider, supported_row
from agent_core.memory_processing import MemoryReviewProcessor
from agent_core.memory_turn import turn_from_messages
import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryUpdateSuggestion
from agent_core.memory_writes import MemoryWriter
from cloud_memory.store import CloudMemoryStore
from cloud_memory.worker import CloudMemoryWorker


class _Embed:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _SuggestionExtractor:
    """只产出更新建议、不产出新候选：走的正是「改旧记忆」这条路。"""

    def __init__(self, suggestions) -> None:
        self.suggestions = list(suggestions)
        self.calls = 0
        rows = []
        for index, suggestion in enumerate(self.suggestions):
            if suggestion.content == '预算三千':
                rows.append(supported_row('预算三千', [(suggestion.source_refs[0], '不是五千'), (suggestion.source_refs[0], '是三千')], index=index))
            elif suggestion.content == '设备采购预算三千':
                rows.append(dict(index=index, verdict='unsupported', reason='设备采购预算没有变化',
                                 subject='设备采购预算', same_subject=True, preserves_negation=True,
                                 preserves_conditions=True, change_authorized=False))
            else:
                raise AssertionError('未指定该测试内容的模型判定')
        self.provider = FixedReviewProvider(rows)

    async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
        self.calls += 1
        return [], list(self.suggestions)


def _turn(turn_id: str, user_text: str) -> dict:
    return {
        "turn_id": turn_id,
        "owner_id": "user",
        "channel": "web",
        "conversation_id": "c1",
        "module_id": "companion",
        "user_text": user_text,
        "assistant_reply": "好",
        "occurred_at": "2026-10-11T12:00:00+00:00",
    }


class CloudMaintenanceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        self.store = CloudMemoryStore(root / "cloud.db")
        self.store.initialize()
        self.repositories = initialize_memory_repositories(
            root / "records.db", root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.writer = MemoryWriter(self.repositories, _Embed())
        self.service = MemoryMaintenanceService(self.writer, self.repositories)
        self.scope = Scope("user", "web", "c1", "companion")
        env = mock.patch.dict(
            "os.environ",
            {"MEMORY_SEMANTIC_ENABLED": "true", "MEMORY_PROCESSING_ENDPOINT": "cloud"},
        )
        env.start()
        self.addCleanup(env.stop)

    def _seed(self, content: str) -> dict:
        assert content in ('预算五千', '设备采购预算五千')
        packet = turn_from_messages(turn_id='turn-0', occurred_at='2026-10-11T10:00:00+00:00',
                                    scope=self.scope, user_text=content, published_reply='')
        item = MemoryCandidate(content, 'fact', 'reported', ('user_statement',), source_refs=('turn-0',))
        provider = FixedReviewProvider([supported_row(content, [('turn-0', content)], change=False)])
        candidates, _, result = asyncio.run(MemoryReviewProcessor(self.writer, provider).prepare(
            packet, [item], persist=lambda _: None))
        assert candidates and not result.pending_review
        return self.writer.save_semantic_candidates(self.scope, candidates, packet=packet, turn_id='turn-0',
                                                   occurred_at=packet.occurred_at)[0]

    def _worker(self, extractor) -> CloudMemoryWorker:
        return CloudMemoryWorker(
            self.repositories,
            self.store,
            self.writer,
            None,
            semantic_extractor=extractor,
            maintenance=self.service,
            interval_seconds=0,
            batch_size=10,
        )

    def _row(self, turn_id: str) -> dict:
        with self.store.connect() as db:
            return dict(
                db.execute(
                    "SELECT * FROM memory_ingest_queue WHERE turn_id=?", (turn_id,)
                ).fetchone()
            )

    def _active(self) -> list[str]:
        return [
            row["content"]
            for row in self.writer.list_records(self.scope)
            if row["status"] == "active"
        ]

    def _suggestion(self, target: dict, content: str, turn_id: str) -> MemoryUpdateSuggestion:
        return MemoryUpdateSuggestion(
            action="correct",
            target_id=target["id"],
            expected_version=int(target["record_version"]),
            content=content,
            kind="fact",
            content_status="reported",
            source_types=("user_statement",),
            source_refs=(turn_id,),
            reason="用户纠正了这个数字",
        )

    def test_a_maintenance_write_failure_keeps_the_turn_retryable(self) -> None:
        old = self._seed("预算五千")
        extractor = _SuggestionExtractor([self._suggestion(old, "预算三千", "t1")])
        worker = self._worker(extractor)
        self.store.enqueue_turn(_turn("t1", "不是五千，是三千"))

        with mock.patch.object(
            self.writer,
            "apply_semantic_update",
            side_effect=sqlite3.OperationalError("simulated storage outage"),
        ):
            asyncio.run(worker.run_once())

        row = self._row("t1")
        assert worker.stats["failed"] == 1, worker.stats
        assert row["status"] == "failed" and row["attempts"] == 1
        assert "OperationalError" in (row["last_error"] or "")
        # 提炼产物已经落库：重试不用再调模型，也不会换一套措辞。
        stored = self.store.stored_extraction(row)
        assert stored["done"] is True
        assert [item.target_id for item in stored["suggestions"]] == [old["id"]]
        assert extractor.calls == 1
        assert self._active() == ["预算五千"]        # 没写成就是没写成

        later = (datetime.now(UTC) + timedelta(seconds=60)).isoformat(timespec="milliseconds")
        turns = self.store.claim_due(now=later)
        assert len(turns) == 1
        asyncio.run(worker._process(turns[0]))

        assert extractor.calls == 1                  # 重试没有再调模型
        assert worker.stats["maintenance_applied"] == 1
        assert worker.stats["maintained"] == 1
        assert self._active() == ["预算三千"]        # 恢复后只生效一次
        # 只改旧记忆、没有新候选：记成 maintained，不记成「什么都没有」。
        row = self._row("t1")
        assert row["status"] == "maintained" and row["maintenance_applied"] == 1

    def test_a_refused_suggestion_is_not_a_failed_turn(self) -> None:
        """证据不足是拒绝：这轮照常结束，但绝不改动记录。"""
        old = self._seed("设备采购预算五千")
        extractor = _SuggestionExtractor([self._suggestion(old, "设备采购预算三千", "t2")])
        worker = self._worker(extractor)
        self.store.enqueue_turn(_turn("t2", "不是周一，是周二开会；设备采购预算还没变化"))

        asyncio.run(worker.run_once())

        row = self._row("t2")
        assert row["status"] == 'refused'
        assert worker.stats["failed"] == 0
        assert worker.stats["maintenance_refused"] == 1
        assert self._active() == ["设备采购预算五千"]

    def test_maintenance_writes_do_not_feed_themselves_back_in(self) -> None:
        """更新写回只进变更流水，不会变成新的待提炼回合 —— 否则就是自我循环。"""
        from cloud_memory.worker import ChangeRecorder

        old = self._seed("预算五千")
        # 生产装配里写入端带着变更记录器：更新过的东西必须进流水，才能同步给另一端。
        writer = MemoryWriter(
            self.repositories, _Embed(), on_change=ChangeRecorder(self.repositories, self.store)
        )
        extractor = _SuggestionExtractor([self._suggestion(old, "预算三千", "t1")])
        worker = CloudMemoryWorker(
            self.repositories,
            self.store,
            writer,
            None,
            semantic_extractor=extractor,
            maintenance=MemoryMaintenanceService(writer, self.repositories),
            interval_seconds=0,
            batch_size=10,
        )
        self.store.enqueue_turn(_turn("t1", "不是五千，是三千"))

        asyncio.run(worker.run_once())

        assert worker.stats["maintenance_applied"] == 1, worker.stats
        assert extractor.calls == 1                     # 没有被自己的写入再触发一次提炼
        assert self.store.queue_counts() == {"maintained": 1}
        with self.store.connect() as db:
            changes = db.execute("SELECT COUNT(*) AS n FROM memory_changes").fetchone()["n"]
        assert changes >= 2                             # 新版本 + 旧版本归档，都要同步给另一端
        assert self._active() == ["预算三千"]

    def test_the_old_turn_payload_still_carries_the_suggestion_after_a_restart(self) -> None:
        """重启用的是库里那份提炼产物，不是内存里的那一份。"""
        old = self._seed("预算五千")
        extractor = _SuggestionExtractor([self._suggestion(old, "预算三千", "t3")])
        worker = self._worker(extractor)
        self.store.enqueue_turn(_turn("t3", "不是五千，是三千"))
        with mock.patch.object(
            self.writer,
            "apply_semantic_update",
            side_effect=sqlite3.OperationalError("boom"),
        ):
            asyncio.run(worker.run_once())

        # 新进程：同一份存储，新的工人实例，提炼器换成「一调就炸」的替身。
        class _Exploding:
            provider = extractor.provider
            async def extract_maintenance(self, packet, **kwargs):
                raise AssertionError("重启后不该再调模型")

        restarted = self._worker(_Exploding())
        later = (datetime.now(UTC) + timedelta(seconds=60)).isoformat(timespec="milliseconds")
        turns = self.store.claim_due(now=later)
        asyncio.run(restarted._process(turns[0]))

        assert restarted.stats["maintenance_applied"] == 1
        assert self._active() == ["预算三千"]


if __name__ == "__main__":
    unittest.main()
