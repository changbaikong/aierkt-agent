from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.wechat_adapter import CompanionReply, ProcessedMessageStore, WeChatChannelAdapter
from backend.wechat_ilink import parse_inbound_messages
from backend.wechat_runtime import CompanionReplyService


class RecordingTransport:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.images: list[tuple[str, Path]] = []

    async def send_text(self, recipient_id: str, text: str) -> None:
        self.texts.append((recipient_id, text))

    async def send_image(self, recipient_id: str, image_path: Path) -> None:
        self.images.append((recipient_id, image_path))


class WeChatPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_image_to_reply_and_restart_deduplication(self) -> None:
        updates = {
            "msgs": [{
                "message_id": "image-e2e",
                "message_type": 1,
                "from_user_id": "bound-user",
                "context_token": "context",
                "item_list": [{
                    "type": 2,
                    "image_item": {
                        "media": {
                            "full_url": "https://cdn.example/inbound",
                            "aes_key": "unused-in-this-layer",
                        },
                    },
                }],
            }],
        }
        parsed = parse_inbound_messages(updates)
        self.assertEqual(len(parsed), 1)
        message, _context_token = parsed[0]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sticker_path = root / "reply.webp"
            sticker_path.write_bytes(b"reply-image")
            store_path = root / "processed.json"
            image_loader = AsyncMock(return_value=b"RIFF\x00\x00\x00\x00WEBPpayload")
            service = CompanionReplyService("prompt", timeout_seconds=1, image_loader=image_loader)
            transport = RecordingTransport()
            adapter = WeChatChannelAdapter(
                transport=transport,
                reply_handler=service.reply,
                processed_store=ProcessedMessageStore(store_path),
            )

            with (
                patch(
                    "backend.wechat_runtime.describe_channel_image",
                    new=AsyncMock(return_value="角色笑着挥手"),
                ),
                patch(
                    "backend.wechat_runtime.create_channel_reply",
                    new=AsyncMock(
                        return_value=CompanionReply(text="收到啦", expression="开心", sticker_path=sticker_path)
                    ),
                ) as create_reply,
            ):
                self.assertTrue(await adapter.handle_message(message))

            self.assertIn("角色笑着挥手", create_reply.await_args.args[0])
            self.assertEqual(transport.texts, [("bound-user", "收到啦")])
            self.assertEqual(transport.images, [("bound-user", sticker_path)])

            restarted = WeChatChannelAdapter(
                transport=transport,
                reply_handler=service.reply,
                processed_store=ProcessedMessageStore(store_path),
            )
            self.assertFalse(await restarted.handle_message(message))
            self.assertEqual(len(transport.texts), 1)
            self.assertEqual(len(transport.images), 1)


if __name__ == "__main__":
    unittest.main()
