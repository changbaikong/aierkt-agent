from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.memory import (
    get_wechat_sync_cursor,
    list_pending_wechat_updates,
    mark_wechat_update,
    persist_wechat_updates,
)


class WeChatReliabilityTests(unittest.TestCase):
    def test_cursor_and_inbox_are_committed_together_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.db"
            update = {
                "message_id": "message-1",
                "sender_id": "bound-user",
                "payload_json": json.dumps({"message_id": "message-1", "text": "hello"}),
            }
            with patch("backend.memory.DB_DIR", Path(directory)), patch("backend.memory.DB_PATH", db_path):
                inserted = persist_wechat_updates("wechat:bound-user", [update], "cursor-1")
                duplicate = persist_wechat_updates("wechat:bound-user", [update], "cursor-2")
                pending = list_pending_wechat_updates("wechat:bound-user")

                self.assertEqual(inserted, 1)
                self.assertEqual(duplicate, 0)
                self.assertEqual(get_wechat_sync_cursor("wechat:bound-user"), "cursor-2")
                self.assertEqual([row["message_id"] for row in pending], ["message-1"])

                mark_wechat_update("wechat:bound-user", "message-1", "processing")
                # A process restart sees processing work as recoverable.
                self.assertEqual(len(list_pending_wechat_updates("wechat:bound-user")), 1)
                mark_wechat_update("wechat:bound-user", "message-1", "delivered")
                self.assertEqual(list_pending_wechat_updates("wechat:bound-user"), [])

    def test_owner_scopes_cannot_read_each_others_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.db"
            with patch("backend.memory.DB_DIR", Path(directory)), patch("backend.memory.DB_PATH", db_path):
                persist_wechat_updates("wechat:a", [{
                    "message_id": "same-id",
                    "sender_id": "a",
                    "payload_json": "{}",
                }], "a-cursor")
                persist_wechat_updates("wechat:b", [{
                    "message_id": "same-id",
                    "sender_id": "b",
                    "payload_json": "{}",
                }], "b-cursor")

                self.assertEqual([row["sender_id"] for row in list_pending_wechat_updates("wechat:a")], ["a"])
                self.assertEqual([row["sender_id"] for row in list_pending_wechat_updates("wechat:b")], ["b"])


if __name__ == "__main__":
    unittest.main()
