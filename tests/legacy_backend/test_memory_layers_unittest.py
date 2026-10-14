from __future__ import annotations

import tempfile
import unittest
import gc
from pathlib import Path
from unittest.mock import patch

from backend import memory


class MemoryLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_dir = patch.object(memory, "DB_DIR", root)
        self.db_path = patch.object(memory, "DB_PATH", root / "memory.db")
        self.embeddings = patch.object(memory, "EMBEDDINGS_DISABLED", True)
        self.db_dir.start()
        self.db_path.start()
        self.embeddings.start()

    def tearDown(self):
        self.embeddings.stop()
        self.db_path.stop()
        self.db_dir.stop()
        gc.collect()
        self.temp_dir.cleanup()

    def test_hot_memory_is_conversation_scoped(self):
        first = memory.MemoryScope("same-user", "desktop", "first")
        second = memory.MemoryScope("same-user", "desktop", "second")
        memory.save_raw_message("user", "first-only", first)
        memory.save_raw_message("user", "second-only", second)

        first_context = memory.assemble_memory_context(first)
        self.assertEqual([item["content"] for item in first_context.hot], ["first-only"])
        self.assertNotIn("second-only", first_context.injected_text)

    def test_candidate_stays_out_of_fact_section_until_promoted(self):
        scope = memory.MemoryScope("candidate-user", "desktop", "desktop")
        candidate_id = memory.save_memory_candidate("likes tea", scope=scope, confidence=0.7)
        pending = memory.assemble_memory_context(scope)
        self.assertNotIn("[USER_CONFIRMED_FACT]", pending.injected_text)
        self.assertEqual(pending.warm, [])
        self.assertNotIn("[USER_CONFIRMED_FACT]", pending.injected_text)

        self.assertTrue(memory.promote_memory_candidate(candidate_id, scope))
        promoted = memory.assemble_memory_context(scope)
        self.assertIn("[USER_CONFIRMED_FACT]", promoted.injected_text)

    def test_new_explicit_fact_supersedes_old_fact_with_conflict(self):
        scope = memory.MemoryScope("fact-user", "desktop", "desktop")
        memory.save_fact("用户喜欢咖啡", scope=scope)
        memory.save_fact("用户现在不喝咖啡", scope=scope)
        context = memory.assemble_memory_context(scope)

        self.assertIn("用户现在不喝咖啡", context.injected_text)
        self.assertNotIn("[USER_CONFIRMED_FACT]\n- 用户喜欢咖啡", context.injected_text)
        self.assertTrue(context.conflicts)
        self.assertIn("用户喜欢咖啡", context.injected_text)

    def test_history_without_records_is_explicitly_unverifiable(self):
        scope = memory.MemoryScope("empty-user", "desktop", "desktop")
        context = memory.assemble_memory_context(scope, "以前聊过什么")
        self.assertEqual(context.cold_evidence, [])
        self.assertIn("未找到可验证的历史记录", context.injected_text)


if __name__ == "__main__":
    unittest.main()
