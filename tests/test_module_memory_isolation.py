from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_writes import MemoryWriter


class Embed:
    def embed(self, _text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def _decision(content: str, **overrides):
    from agent_core.memory_turn import MemoryCandidate

    base = dict(
        content=content,
        kind="decision",
        content_status="proposed",
        source_types=("assistant_suggestion",),
    )
    base.update(overrides)
    return MemoryCandidate(**base)


class SemanticScopeIsolationTests(unittest.TestCase):
    """新语义记录的三种范围在真实读取链路上的边界。"""

    def setUp(self) -> None:
        env = mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.writer = MemoryWriter(self.repositories, Embed())
        self.retriever = MemoryRetriever(self.repositories, Embed())
        self.here = Scope("owner", "web", "chat-a", "companion")
        self.elsewhere = Scope("owner", "web", "chat-b", "companion")

    def _contents(self, scope, query, **kwargs) -> list[str]:
        return [item["content"] for item in self.retriever.retrieve(scope, query, **kwargs).warm]

    def test_conversation_scope_stays_inside_its_own_conversation(self) -> None:
        seed_semantic(self.writer,
            self.here, [_decision("把候选提炼放进持久队列")], turn_id="t1"
        )
        self.assertEqual(self._contents(self.here, "持久队列"), ["把候选提炼放进持久队列"])
        self.assertEqual(self._contents(self.elsewhere, "持久队列"), [])

    def test_project_scope_is_readable_from_another_conversation_with_the_binding(self) -> None:
        seed_semantic(self.writer,
            self.here,
            [_decision("把候选提炼放进持久队列", scope_kind="project", project_id="dm-agent")],
            turn_id="t2",
            project_binding="dm-agent",
        )
        self.assertEqual(
            self._contents(self.elsewhere, "持久队列", project_id="dm-agent"),
            ["把候选提炼放进持久队列"],
        )
        # 没有可信绑定就退回会话范围，光凭项目名一样读不到。
        self.assertEqual(self._contents(self.elsewhere, "持久队列", project_id=""), [])

    def test_owner_scope_preference_is_readable_across_main_channels(self) -> None:
        seed_semantic(self.writer,
            self.here,
            [
                _decision(
                    "用户偏好先给结论",
                    kind="preference",
                    content_status="explicit",
                    source_types=("user_statement",),
                    scope_kind="owner",
                )
            ],
            turn_id="t3",
        )
        cli = Scope("owner", "cli", "chat-c", "companion")
        self.assertEqual(self._contents(cli, "先给结论"), ["用户偏好先给结论"])

    def test_owner_scope_does_not_open_up_to_scenario_channel(self) -> None:
        from agent_core.memory_scope import authorize

        row = {
            "owner_id": "owner",
            "channel": "web",
            "conversation_id": "chat-a",
            "module_id": "companion",
            "semantics_version": 1,
            "scope_kind": "owner",
        }
        self.assertTrue(authorize(row, self.here))
        self.assertFalse(authorize(row, Scope("owner", "scenario", "scene-1", "companion")))

    def test_other_account_cannot_read_a_project_record(self) -> None:
        seed_semantic(self.writer,
            self.here,
            [_decision("把候选提炼放进持久队列", scope_kind="project", project_id="dm-agent")],
            turn_id="t4",
            project_binding="dm-agent",
        )
        intruder = Scope("someone-else", "web", "chat-a", "companion")
        self.assertEqual(self._contents(intruder, "持久队列", project_id="dm-agent"), [])


class ModuleMemoryIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.writer = MemoryWriter(self.repositories, Embed())
        self.retriever = MemoryRetriever(self.repositories, Embed())
        self.search = Scope("owner", "desktop", "chat", "search")
        self.netease = Scope("owner", "desktop", "chat", "netease")

    def test_private_messages_are_not_visible_to_another_module(self) -> None:
        self.writer.append_message(self.search, "user", "search-only query")

        result = self.retriever.retrieve(self.netease, "query")

        self.assertEqual(result.hot, [])

    def test_confirmed_shared_fact_is_visible_to_another_module(self) -> None:
        record = self.writer.save_fact(self.search, "user likes jazz", confidence=0.9)

        result = self.retriever.retrieve(self.netease, "jazz")

        self.assertEqual(record["visibility"], "shared")
        self.assertEqual([item["content"] for item in result.warm], ["user likes jazz"])

    def test_idempotency_key_is_scoped_by_module(self) -> None:
        search_record = self.writer.append_message(self.search, "user", "search message", idempotency_key="turn-1")
        music_record = self.writer.append_message(self.netease, "user", "music message", idempotency_key="turn-1")

        self.assertNotEqual(search_record["id"], music_record["id"])

    def test_vector_payload_contains_scope_and_visibility_for_filtered_retrieval(self) -> None:
        record = self.writer.append_message(self.search, "user", "private vector message")

        point = self.repositories.vector_client.retrieve(
            "hot_memories_vectors", [record["id"]], with_payload=True
        )[0]

        self.assertEqual(point.payload, {
            "memory_id": record["id"],
            "owner_id": "owner",
            "channel": "desktop",
            "conversation_id": "chat",
            "module_id": "search",
            "visibility": "module",
            "semantics_version": 0,
        })

    def test_legacy_confirmed_facts_are_migrated_to_shared_visibility(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        db_path = root / "legacy.db"
        vector_path = root / "vectors"
        with sqlite3.connect(db_path) as db:
            db.execute(
                "CREATE TABLE memory_records ("
                "id TEXT PRIMARY KEY, owner_id TEXT, channel TEXT, conversation_id TEXT, "
                "layer TEXT, kind TEXT, status TEXT, content TEXT, metadata TEXT, "
                "confidence REAL, conflict_key TEXT, idempotency_key TEXT, "
                "embedding_status TEXT, embedding_layer TEXT, created_at TEXT, updated_at TEXT)"
            )
            db.execute(
                "INSERT INTO memory_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ",
                ("legacy", "owner", "desktop", "chat", "warm", "fact", "confirmed", "likes jazz", "{}", .9, None, None, "pending", None, "2026-01-01", "2026-01-01"),
            )
            db.commit()
        repositories = initialize_memory_repositories(db_path, vector_path, vector_size=3)
        self.addCleanup(repositories.close)

        with repositories.connect() as db:
            row = db.execute("SELECT module_id, visibility FROM memory_records WHERE id='legacy'").fetchone()
        self.assertEqual(tuple(row), ("companion", "shared"))

    def test_reinitializing_does_not_promote_module_private_confirmed_fact(self) -> None:
        record = self.writer.save_memory(
            self.search, "music module state", conflict_key="music.recent",
            confidence=.9, visibility="module",
        )
        self.repositories.initialize()

        with self.repositories.connect() as db:
            row = db.execute("SELECT visibility FROM memory_records WHERE id=?", (record["id"],)).fetchone()
        self.assertEqual(row[0], "module")


if __name__ == "__main__":
    unittest.main()
