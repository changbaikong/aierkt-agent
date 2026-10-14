from __future__ import annotations

import gc
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import memory
from backend.context_service import build_agent_context
from backend import main


class EngineeringClosureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_dir = patch.object(memory, "DB_DIR", root)
        self.db_path = patch.object(memory, "DB_PATH", root / "memory.db")
        self.embeddings = patch.object(memory, "EMBEDDINGS_DISABLED", True)
        self.db_dir.start()
        self.db_path.start()
        self.embeddings.start()

    def tearDown(self) -> None:
        self.embeddings.stop()
        self.db_path.stop()
        self.db_dir.stop()
        gc.collect()
        self.temp_dir.cleanup()

    def test_empty_and_repeated_database_migration(self) -> None:
        first = memory.verify_database_schema()
        second = memory.verify_database_schema()
        self.assertTrue(first["ok"])
        self.assertEqual(first["tables"], second["tables"])
        with memory._get_db() as db:
            candidate_columns = {row["name"] for row in db.execute("PRAGMA table_info(memory_candidates)")}
        self.assertIn("status", candidate_columns)
        self.assertIn("owner_id", candidate_columns)

    def test_legacy_database_content_survives_migration(self) -> None:
        db_path = memory.DB_PATH
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, importance INTEGER DEFAULT 1)")
        conn.execute("INSERT INTO facts (content, importance) VALUES (?, ?)", ("legacy fact", 2))
        conn.commit()
        conn.close()

        result = memory.verify_database_schema()
        self.assertTrue(result["ok"])
        with memory._get_db() as db:
            row = db.execute("SELECT content, source_type, status FROM facts WHERE content = ?", ("legacy fact",)).fetchone()
        self.assertEqual(row["content"], "legacy fact")
        self.assertEqual(row["source_type"], "legacy")
        self.assertEqual(row["status"], "active")

    def test_expired_internet_mind_cleanup_is_owner_scoped(self) -> None:
        alice = memory.MemoryScope("alice", "wechat", "alice-chat")
        bob = memory.MemoryScope("bob", "wechat", "bob-chat")
        with memory._get_db() as db:
            db.executemany(
                "INSERT INTO internet_mind (summary, owner_id, channel, conversation_id, expires_at) VALUES (?, ?, ?, ?, ?)",
                [
                    ("alice expired", alice.owner_id, alice.channel, alice.conversation_id, "2000-01-01 00:00:00"),
                    ("bob expired", bob.owner_id, bob.channel, bob.conversation_id, "2000-01-01 00:00:00"),
                ],
            )
            db.commit()
        self.assertEqual(memory.cleanup_expired_internet_mind(alice), 1)
        with memory._get_db() as db:
            remaining = db.execute("SELECT summary FROM internet_mind ORDER BY id").fetchall()
        self.assertEqual([row["summary"] for row in remaining], ["bob expired"])

    async def test_context_entry_contains_layers_and_ephemeral_instructions(self) -> None:
        scope = memory.MemoryScope("context-user", "desktop", "context-chat")
        memory.save_fact("confirmed preference", scope=scope)
        context = build_agent_context(
            scope,
            user_message="hello",
            system_prompt="base system",
            channel_rules="desktop rules",
            ephemeral_context={"turn_style": "short"},
        )
        self.assertIn("base system", context.system_prompt)
        self.assertIn("desktop rules", context.system_prompt)
        self.assertIn("confirmed preference", context.memory_text)
        self.assertIn("[turn_style]", context.ephemeral_instructions)
        self.assertEqual(context.metadata["layer_counts"]["warm"], 1)

    async def test_remote_debug_is_disabled_and_candidates_are_redacted(self) -> None:
        scope = memory.MemoryScope("debug-user", "desktop", "debug-chat")
        memory.save_memory_candidate("password=top-secret", scope=scope)
        with patch.object(main, "REMOTE_RUNTIME", True), patch.dict(os.environ, {"COMPANION_ENABLE_REMOTE_DEBUG": "0"}, clear=False):
            with self.assertRaises(HTTPException) as blocked:
                await main.debug_memory_candidates(owner_id=scope.owner_id, channel=scope.channel, conversation_id=scope.conversation_id)
        self.assertEqual(blocked.exception.status_code, 404)

        with patch.object(main, "REMOTE_RUNTIME", False):
            response = await main.debug_memory_candidates(owner_id=scope.owner_id, channel=scope.channel, conversation_id=scope.conversation_id)
        self.assertNotIn("content", response["items"][0])
        self.assertEqual(response["items"][0]["content_preview"], "[REDACTED_SENSITIVE_MEMORY]")


if __name__ == "__main__":
    unittest.main()
