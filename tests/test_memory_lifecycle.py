from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_writes import MemoryMaintenanceWorker, MemoryWriter


class Embed:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repo = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repo.close)
        self.writer = MemoryWriter(self.repo, Embed())
        self.retriever = MemoryRetriever(self.repo, Embed())
        self.scope = Scope("user", "desktop", "conversation")

    def test_records_have_lifecycle_metadata(self) -> None:
        record = self.writer.append_message(self.scope, "user", "hello")
        self.assertEqual(record["access_count"], 0)
        self.assertEqual(record["importance"], 1.0)
        self.assertIsNotNone(record["last_accessed_at"])
        self.assertIsNone(record["expires_at"])

    def test_retrieval_updates_access_metadata(self) -> None:
        record = self.writer.save_fact(self.scope, "likes coffee", confidence=.9)
        self.retriever.retrieve(self.scope, "coffee")
        row = self.repo.records_by_ids([record["id"]])[record["id"]]
        self.assertEqual(row["access_count"], 1)
        self.assertIsNotNone(row["last_accessed_at"])

    def test_maintenance_migrates_old_hot_record(self) -> None:
        record = self.writer.append_message(self.scope, "user", "old message")
        old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="milliseconds")
        with self.repo.connect() as db:
            db.execute("UPDATE memory_records SET created_at=?, updated_at=? WHERE id=?", (old, old, record["id"]))
        result = self.writer.run_maintenance()
        row = self.repo.records_by_ids([record["id"]])[record["id"]]
        self.assertEqual(result["migrated"], 1)
        self.assertEqual(row["layer"], "warm")
        self.assertEqual(len(self.repo.vector_client.retrieve("warm_memories_vectors", [record["id"]])), 1)
        self.assertEqual(self.repo.vector_client.retrieve("hot_memories_vectors", [record["id"]]), [])

    def test_old_candidate_expires_but_confirmed_fact_does_not(self) -> None:
        candidate = self.writer.save_candidate(self.scope, "might like tea", confidence=.4)
        fact = self.writer.save_fact(self.scope, "likes coffee", confidence=.9)
        old = (datetime.now(UTC) - timedelta(days=15)).isoformat(timespec="milliseconds")
        with self.repo.connect() as db:
            db.execute("UPDATE memory_records SET last_accessed_at=? WHERE id=?", (old, candidate["id"]))
            db.execute("UPDATE memory_records SET last_accessed_at=? WHERE id=?", (old, fact["id"]))
        self.writer.run_maintenance()
        rows = self.repo.records_by_ids([candidate["id"], fact["id"]])
        self.assertEqual(rows[candidate["id"]]["status"], "expired")
        self.assertEqual(rows[fact["id"]]["status"], "confirmed")
        result = self.retriever.retrieve(self.scope, "tea")
        self.assertNotIn(candidate["id"], [item["id"] for item in result.items])

    def test_expired_record_is_purged_after_audit_period(self) -> None:
        record = self.writer.save_candidate(self.scope, "old candidate", confidence=.4)
        now = datetime.now(UTC)
        with self.repo.connect() as db:
            db.execute(
                "UPDATE memory_records SET status='expired', expires_at=?, purge_after=?, deletion_reason=? WHERE id=?",
                ((now - timedelta(days=31)).isoformat(timespec="milliseconds"),
                 (now - timedelta(days=1)).isoformat(timespec="milliseconds"),
                 "decay", record["id"]),
            )
        result = self.writer.run_maintenance(now=now)
        self.assertEqual(result["purged"], 1)
        self.assertNotIn(record["id"], self.repo.records_by_ids([record["id"]]))
        self.assertEqual(self.repo.vector_client.retrieve("warm_memories_vectors", [record["id"]]), [])

    def test_failed_expiry_vector_delete_is_retried_on_next_maintenance(self) -> None:
        record = self.writer.save_candidate(self.scope, "delete me", confidence=.4)
        now = datetime.now(UTC)
        with self.repo.connect() as db:
            db.execute(
                "UPDATE memory_records SET status='expired', expires_at=?, purge_after=? WHERE id=?",
                (now.isoformat(timespec="milliseconds"),
                 (now + timedelta(days=10)).isoformat(timespec="milliseconds"), record["id"]),
            )
        original_delete = self.repo.vector_client.delete
        attempts = {"count": 0}

        def flaky_delete(*args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("vector store unavailable")
            return original_delete(*args, **kwargs)

        self.repo.vector_client.delete = flaky_delete
        first = self.writer.run_maintenance(now=now)
        self.assertEqual(first["deferred"], 1)
        second = self.writer.run_maintenance(now=now + timedelta(minutes=1))
        self.assertEqual(second["deferred"], 0)
        row = self.repo.records_by_ids([record["id"]])[record["id"]]
        self.assertIsNone(row["embedding_layer"])
        self.assertEqual(row["embedding_status"], "deleted")

    def test_maintenance_worker_requires_explicit_start(self) -> None:
        calls: list[int] = []
        original = self.writer.run_maintenance
        self.writer.run_maintenance = lambda: calls.append(1) or {"migrated": 0, "expired": 0, "purged": 0, "deferred": 0}
        worker = MemoryMaintenanceWorker(self.writer, interval_seconds=.01)
        time.sleep(.03)
        self.assertEqual(calls, [])
        worker.start()
        time.sleep(.05)
        worker.stop(timeout=1)
        self.assertGreaterEqual(len(calls), 1)
        count = len(calls)
        time.sleep(.03)
        self.assertEqual(len(calls), count)
        self.writer.run_maintenance = original


if __name__ == "__main__":
    unittest.main()
