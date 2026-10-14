from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from backend.wechat_adapter import CompanionReply, ProcessedMessageStore, WeChatChannelAdapter, WeChatInboundMessage
from backend.wechat_runtime import ILinkTextTransport, WeChatTurnManager


class FakeClient:
    def __init__(self) -> None:
        self.send_typing = AsyncMock()
        self.sent: list[tuple[str, str, str]] = []
        self.first_send_started = asyncio.Event()
        self.release_first_send = asyncio.Event()

    async def send_text(self, recipient_id: str, text: str, context_token: str) -> None:
        if text == "旧轮第一句。":
            self.first_send_started.set()
            await self.release_first_send.wait()
        self.sent.append((recipient_id, text, context_token))

    async def send_image(self, recipient_id: str, image_path: Path, context_token: str) -> None:
        raise AssertionError("unexpected image")


class WeChatTurnManagerTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skip("superseded by non-cancelling queued-turn behavior")
    async def test_new_message_finishes_current_bubble_then_interrupts_old_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            transport = ILinkTextTransport(client)  # type: ignore[arg-type]

            async def reply_handler(message: WeChatInboundMessage) -> CompanionReply:
                if message.text == "旧问题":
                    return CompanionReply(
                        text="旧轮第一句。\n旧轮第二句。",
                        bubbles=("旧轮第一句。", "旧轮第二句。"),
                    )
                return CompanionReply(text="新轮回复。", bubbles=("新轮回复。",))

            adapter = WeChatChannelAdapter(
                transport=transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(Path(directory) / "processed.json"),
                sleep=AsyncMock(),
            )
            manager = WeChatTurnManager(client, transport, adapter, debounce_seconds=0)
            old = WeChatInboundMessage("old", "user", "旧问题", context_token="ctx-old")
            new = WeChatInboundMessage("new", "user", "新问题", context_token="ctx-new")

            manager.submit(old, "ctx-old")
            await asyncio.wait_for(client.first_send_started.wait(), timeout=1)
            manager.submit(new, "ctx-new")
            client.release_first_send.set()
            await asyncio.wait_for(manager.wait_idle(), timeout=2)

            self.assertEqual(client.sent, [
                ("user", "旧轮第一句。", "ctx-old"),
                ("user", "新轮回复。", "ctx-new"),
            ])
            self.assertTrue(adapter.processed_store.contains("old"))
            self.assertTrue(adapter.processed_store.contains("new"))

    @unittest.skip("superseded by deterministic queued-turn behavior")
    async def test_messages_arriving_during_thinking_are_merged_into_one_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.release_first_send.set()
            transport = ILinkTextTransport(client)  # type: ignore[arg-type]
            first_generation_started = asyncio.Event()

            async def reply_handler(message: WeChatInboundMessage) -> CompanionReply:
                if message.text == "第一条":
                    first_generation_started.set()
                    await asyncio.Event().wait()
                return CompanionReply(text=message.text, bubbles=(message.text,))

            adapter = WeChatChannelAdapter(
                transport=transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(Path(directory) / "processed.json"),
                sleep=AsyncMock(),
            )
            manager = WeChatTurnManager(client, transport, adapter, debounce_seconds=0)
            manager.submit(WeChatInboundMessage("m1", "user", "第一条"), "ctx1")
            await asyncio.wait_for(first_generation_started.wait(), timeout=1)
            manager.submit(WeChatInboundMessage("m2", "user", "第二条"), "ctx2")
            await asyncio.wait_for(manager.wait_idle(), timeout=2)

            self.assertEqual(client.sent, [("user", "第一条\n第二条", "ctx2")])
            self.assertTrue(adapter.processed_store.contains("m1"))
            self.assertTrue(adapter.processed_store.contains("m2"))

    async def test_message_during_generation_waits_for_a_second_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            client.release_first_send.set()
            transport = ILinkTextTransport(client)  # type: ignore[arg-type]
            started = asyncio.Event()
            release = asyncio.Event()

            async def reply_handler(message: WeChatInboundMessage) -> CompanionReply:
                if message.text == "first":
                    started.set()
                    await release.wait()
                return CompanionReply(text=message.text, bubbles=(message.text,))

            adapter = WeChatChannelAdapter(
                transport=transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(Path(directory) / "processed.json"),
                sleep=AsyncMock(),
            )
            manager = WeChatTurnManager(client, transport, adapter, debounce_seconds=0)
            manager.submit(WeChatInboundMessage("m1", "user", "first"), "ctx1")
            await asyncio.wait_for(started.wait(), timeout=1)
            manager.submit(WeChatInboundMessage("m2", "user", "second"), "ctx2")
            release.set()
            await asyncio.wait_for(manager.wait_idle(), timeout=2)

            self.assertEqual(client.sent, [("user", "first", "ctx1"), ("user", "second", "ctx2")])


if __name__ == "__main__":
    unittest.main()
