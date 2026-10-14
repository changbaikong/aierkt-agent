from __future__ import annotations

import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_core.contracts import Scope
from agent_core.memory_repositories import (
    MEMORY_LAYERS,
    VECTOR_PAYLOAD_BATCH_SIZE,
    VECTOR_PAYLOAD_VERSION,
    MemoryRepositories,
    initialize_memory_repositories,
)
from agent_core.memory_writes import MemoryWriter


class Embed:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class MemoryRepositoriesTests(unittest.TestCase):
    def _paths(self) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root / "memory-repositories.db", root / "vectors"

    def test_initialization_creates_unified_sql_table_and_vector_collection_per_layer(self) -> None:
        db_path, vector_path = self._paths()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(repositories.close)

        with sqlite3.connect(db_path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        collections = {collection.name for collection in repositories.vector_client.get_collections().collections}

        self.assertIn("memory_records", tables)
        self.assertTrue({f"{layer}_memories_vectors" for layer in MEMORY_LAYERS} <= collections)

    def test_reinitializing_keeps_existing_memory_record(self) -> None:
        db_path, vector_path = self._paths()
        repositories = MemoryRepositories(db_path, vector_path, vector_size=3)
        repositories.initialize()
        self.addCleanup(repositories.close)
        memory = MemoryWriter(repositories, Embed()).save_candidate(
            Scope("user", "desktop", "conversation"), "Keep this memory", confidence=.5
        )
        memory_id = memory["id"]
        repositories.close()

        reinitialized = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(reinitialized.close)

        with sqlite3.connect(db_path) as connection:
            row = connection.execute("SELECT id, content FROM memory_records WHERE id = ?", (memory_id,)).fetchone()
        point = reinitialized.vector_client.retrieve("warm_memories_vectors", [memory_id], with_payload=True)[0]

        self.assertEqual(row, (memory_id, "Keep this memory"))
        self.assertEqual(point.payload, {
            "memory_id": memory_id,
            "owner_id": "user",
            "channel": "desktop",
            "conversation_id": "conversation",
            "module_id": "companion",
            "visibility": "module",
            # 旧格式记录：语义版本 0，且不带 project_id / content_status。
            "semantics_version": 0,
        })

    def _insert_ready_record(self, connection, record_id: str, version: int, layer: str = "warm") -> None:
        connection.execute(
            "INSERT INTO memory_records "
            "(id, owner_id, channel, conversation_id, module_id, visibility, layer, kind, status, content, "
            "embedding_status, embedding_layer, created_at, updated_at, vector_payload_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)",
            (record_id, "user", "desktop", "conversation", "companion", "module", layer, "message", "active", record_id, layer, "now", "now", version),
        )

    def test_backfill_skips_records_already_at_the_current_version(self) -> None:
        """已经标过当前版本的记录不该再被回填一遍 —— 否则每次启动都要重做同样的活。"""
        db_path, vector_path = self._paths()
        repositories = MemoryRepositories(db_path, vector_path, vector_size=3)
        repositories.initialize()
        self.addCleanup(repositories.close)

        with repositories.connect() as connection:
            self._insert_ready_record(connection, "legacy", version=0)
            self._insert_ready_record(connection, "current", version=VECTOR_PAYLOAD_VERSION)

        repositories.vector_client.batch_update_points = Mock()
        repositories._backfill_vector_payloads()

        repositories.vector_client.batch_update_points.assert_called_once()
        collection, operations = repositories.vector_client.batch_update_points.call_args.args[:2]
        self.assertEqual(collection, "warm_memories_vectors")
        self.assertEqual(len(operations), 1, "只应回填版本落后的那一条")
        with repositories.connect() as connection:
            versions = dict(connection.execute("SELECT id, vector_payload_version FROM memory_records"))
        self.assertEqual(versions, {"legacy": VECTOR_PAYLOAD_VERSION, "current": VECTOR_PAYLOAD_VERSION})

    def test_backfill_splits_a_backlog_into_batches(self) -> None:
        """积压超过一个批次时按批切分，而不是一条一个请求、也不是几百条挤一个请求。"""
        db_path, vector_path = self._paths()
        repositories = MemoryRepositories(db_path, vector_path, vector_size=3)
        repositories.initialize()
        self.addCleanup(repositories.close)

        total = VECTOR_PAYLOAD_BATCH_SIZE + 2
        with repositories.connect() as connection:
            for index in range(total):
                self._insert_ready_record(connection, f"r{index}", version=0)

        repositories.vector_client.batch_update_points = Mock()
        repositories._backfill_vector_payloads()

        sizes = [len(call.args[1]) for call in repositories.vector_client.batch_update_points.call_args_list]
        self.assertEqual(sizes, [VECTOR_PAYLOAD_BATCH_SIZE, total - VECTOR_PAYLOAD_BATCH_SIZE])
        with repositories.connect() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) FROM memory_records WHERE COALESCE(vector_payload_version,0) < ?",
                (VECTOR_PAYLOAD_VERSION,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_close_releases_the_local_qdrant_directory_for_reinitialization(self) -> None:
        db_path, vector_path = self._paths()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)

        repositories.close()
        reopened = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(reopened.close)

    def test_writes_use_the_same_uuid_for_sql_and_vector_records_in_each_layer(self) -> None:
        db_path, vector_path = self._paths()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(repositories.close)

        scope = Scope("user", "desktop", "conversation")
        records = MemoryWriter(repositories, Embed()).batch_save([
            {"scope": scope, "layer": "hot", "kind": "message", "status": "active", "role": "user", "content": "A hot memory", "metadata": {"source": "test"}},
            {"scope": scope, "layer": "warm", "kind": "candidate", "status": "candidate", "content": "A warm memory", "confidence": .5, "metadata": {"source": "test"}},
            {"scope": scope, "layer": "cold", "kind": "event", "status": "archived", "content": "A cold memory", "metadata": {"source": "test"}},
        ])
        for record in records:
            layer = record["layer"]
            memory_id = record["id"]
            with sqlite3.connect(db_path) as connection:
                row = connection.execute(
                    "SELECT id, content, metadata FROM memory_records WHERE id = ?",
                    (memory_id,),
                ).fetchone()
            point = repositories.vector_client.retrieve(
                f"{layer}_memories_vectors", [memory_id], with_payload=True
            )[0]

            self.assertEqual(row, (memory_id, f"A {layer} memory", '{"source":"test"}'))
            self.assertEqual(str(point.id), memory_id)
            self.assertEqual(point.payload, {
                "memory_id": memory_id,
                "owner_id": "user",
                "channel": "desktop",
                "conversation_id": "conversation",
                "module_id": "companion",
                "visibility": "module",
                "semantics_version": 0,
            })

    def test_recent_hot_records_has_no_fixed_eight_record_limit(self) -> None:
        db_path, vector_path = self._paths()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(repositories.close)
        writer = MemoryWriter(repositories, Embed())
        scope = Scope("user", "desktop", "conversation")
        for index in range(12):
            writer.append_message(scope, "user", f"message {index}")

        records = repositories.recent_hot_records(scope)
        recent_window = repositories.recent_hot_records(scope, limit=3)

        self.assertEqual(len(records), 12)
        self.assertEqual(records[0]["content"], "message 0")
        self.assertEqual(records[-1]["content"], "message 11")
        self.assertEqual([record["content"] for record in recent_window], ["message 9", "message 10", "message 11"])

    def test_count_by_account_returns_active_layer_breakdown_without_other_accounts(self) -> None:
        db_path, vector_path = self._paths()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(repositories.close)
        with sqlite3.connect(db_path) as connection:
            rows = [
                ("a1", "web", "c1", "companion", "hot", "active"),
                ("a1", "wechat", "c2", "companion", "warm", "active"),
                ("a1", "web", "c1", "companion", "cold", "active"),
                ("a1", "web", "c1", "companion", "warm", "archived"),
                ("a2", "web", "c3", "companion", "hot", "active"),
            ]
            for index, (owner, channel, conversation, module, layer, status) in enumerate(rows):
                connection.execute(
                    "INSERT INTO memory_records (id, owner_id, channel, conversation_id, module_id, visibility, layer, kind, status, content, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'module', ?, 'fact', ?, 'x', '{}', 'now', 'now')",
                    (f"m{index}", owner, channel, conversation, module, layer, status),
                )
        self.assertEqual(
            repositories.count_by_account("a1", "companion"),
            {"total": 3, "hot": 1, "warm": 1, "cold": 1, "non_active": 1},
        )

    def test_count_by_account_empty_returns_zeroes(self) -> None:
        db_path, vector_path = self._paths()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(repositories.close)
        self.assertEqual(
            repositories.count_by_account("missing", "companion"),
            {"total": 0, "hot": 0, "warm": 0, "cold": 0, "non_active": 0},
        )
