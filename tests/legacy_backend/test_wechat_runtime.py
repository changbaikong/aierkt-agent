from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.channel_events import channel_event_hub
from backend.wechat_adapter import CompanionReply, WeChatInboundImage, WeChatInboundMessage
from backend.wechat_runtime import (
    CompanionReplyService,
    ERROR_REPLY,
    publish_delivered_reply,
    publish_first_bubble,
)
from backend.memory import MemoryScope
from backend.wechat_routing import route_wechat_turn, validate_fast_chat_reply


class WeChatRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_route_only_whitelists_self_contained_smalltalk(self) -> None:
        scope = MemoryScope("wechat:u1", "wechat", "u1")
        with patch("backend.wechat_routing.AgentLoop.pending_for_scope", return_value=[]):
            for text in ("\u4f60\u597d", "\u8c22\u8c22", "\u4eca\u5929\u597d\u7d2f"):
                decision = route_wechat_turn(
                    WeChatInboundMessage(message_id="m", sender_id="u1", text=text),
                    history=[], scope=scope,
                )
                self.assertEqual(decision.route, "fast_chat", text)
            for text in (
                "\u64ad\u653e Gareth.T \u7684\u73bb\u7483", "\u653e", "\u90a3\u4ee5\u524d\u5462",
                "\u4f60\u8fd8\u8bb0\u5f97\u5417", "\u4eca\u5929\u5929\u6c14", "\u5e2e\u6211\u641c\u7d22",
                "\u63d0\u9192\u6211\u559d\u6c34", "\u8fd9\u662f\u8c01",
            ):
                decision = route_wechat_turn(
                    WeChatInboundMessage(message_id="m", sender_id="u1", text=text),
                    history=[], scope=scope,
                )
                self.assertEqual(decision.route, "main_agent", text)

    def test_pending_action_forces_acknowledgement_to_main_agent(self) -> None:
        message = WeChatInboundMessage(message_id="m", sender_id="u1", text="\u597d")
        scope = MemoryScope("wechat:u1", "wechat", "u1")
        with patch("backend.wechat_routing.AgentLoop.pending_for_scope", return_value=[{"action_id": "a"}]):
            decision = route_wechat_turn(message, history=[], scope=scope)
        self.assertEqual(decision.route, "main_agent")
        self.assertEqual(decision.reason, "pending_action")

    def test_fast_reply_guard_rejects_capability_claims(self) -> None:
        self.assertEqual(validate_fast_chat_reply("\u5df2\u7ecf\u64ad\u653e\u4e86\u3002"), "action_claim")
        self.assertEqual(validate_fast_chat_reply("\u6211\u8bb0\u5f97\u4f60\u4e0a\u6b21\u8bf4\u8fc7\u3002"), "memory_claim")
        self.assertEqual(validate_fast_chat_reply("\u4eca\u5929\u8f9b\u82e6\u4e86\u3002"), "")

    async def test_successful_reply_keeps_bounded_history(self) -> None:
        service = CompanionReplyService("prompt", timeout_seconds=1)
        message = WeChatInboundMessage(message_id="m1", sender_id="u1", text="hello")

        with patch(
            "backend.wechat_runtime.create_channel_reply",
            new=AsyncMock(return_value=CompanionReply(text="reply", sticker_path=None)),
        ):
            reply = await service.reply(message)

        self.assertEqual(reply.text, "reply")
        self.assertEqual(len(service.histories["u1"]), 2)

    async def test_timeout_returns_one_visible_fallback(self) -> None:
        service = CompanionReplyService("prompt", timeout_seconds=0.01)
        message = WeChatInboundMessage(message_id="m1", sender_id="u1", text="hello")

        async def never_finishes(*_args, **_kwargs) -> CompanionReply:
            await asyncio.sleep(1)
            return CompanionReply(text="late")

        with patch("backend.wechat_runtime.create_channel_reply", new=never_finishes):
            reply = await service.reply(message)

        self.assertEqual(reply.text, ERROR_REPLY)
        self.assertEqual(service.histories["u1"], [])

    async def test_reply_publishes_thinking_but_waits_to_publish_reply_events(self) -> None:
        service = CompanionReplyService("prompt", timeout_seconds=1)
        message = WeChatInboundMessage(message_id="m1", sender_id="u1", text="hello")
        queue = channel_event_hub.subscribe()
        try:
            with patch(
                "backend.wechat_runtime.create_channel_reply",
                new=AsyncMock(return_value=CompanionReply(text="reply", expression="开心")),
            ):
                await service.reply(message)

            events = [queue.get_nowait() for _ in range(queue.qsize())]
        finally:
            channel_event_hub.unsubscribe(queue)

        self.assertEqual(
            [(event["type"], event.get("active")) for event in events],
            [
                ("activity", None),
                ("thinking", True),
                ("thinking", False),
                ("speech_prepare", None),
                ("speech_cancel", None),
            ],
        )

    def test_prepared_speech_plays_with_first_bubble(self) -> None:
        queue = channel_event_hub.subscribe()
        message = WeChatInboundMessage(message_id="m1", sender_id="u1", text="hello")
        reply = CompanionReply(
            text="中文正文",
            expression="开心",
            speak_text="ご主人、よくやったな。",
            speech_event_id="wechat:m1",
            speech_prepared=True,
        )
        try:
            with patch("backend.wechat_runtime.time.time", return_value=123):
                publish_first_bubble(message, reply, "中文正文", 0)
                publish_delivered_reply(message, reply)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        finally:
            channel_event_hub.unsubscribe(queue)

        self.assertEqual([event["type"] for event in events], ["expression", "speech_play", "activity"])
        self.assertEqual(events[1], {
            "version": 1,
            "type": "speech_play",
            "channel": "wechat",
            "event_id": "wechat:m1",
            "emotion": "开心",
            "created_at": 123,
        })

    async def test_reply_waits_for_desktop_preparation_ack(self) -> None:
        service = CompanionReplyService("prompt", timeout_seconds=1)
        message = WeChatInboundMessage(message_id="prepared", sender_id="u1", text="hello")
        generated = CompanionReply(text="reply", speak_text="返事じゃ。")

        with (
            patch("backend.wechat_runtime.create_channel_reply", new=AsyncMock(return_value=generated)),
            patch(
                "backend.wechat_runtime.speech_preparation_broker.prepare",
                new=AsyncMock(return_value=True),
            ) as prepare,
        ):
            reply = await service.reply(message)

        self.assertTrue(reply.speech_prepared)
        self.assertEqual(reply.speech_event_id, "wechat:prepared")
        prepare.assert_awaited_once()

    async def test_timeout_reply_does_not_contain_speech(self) -> None:
        service = CompanionReplyService("prompt", timeout_seconds=0.01)
        message = WeChatInboundMessage(message_id="m1", sender_id="u1", text="hello")

        async def never_finishes(*_args, **_kwargs) -> CompanionReply:
            await asyncio.sleep(1)
            return CompanionReply(text="late", speak_text="遅い。")

        with patch("backend.wechat_runtime.create_channel_reply", new=never_finishes):
            reply = await service.reply(message)

        self.assertIsNone(reply.speak_text)

    async def test_inbound_sticker_is_described_before_reply(self) -> None:
        image_loader = AsyncMock(return_value=b"RIFF\x00\x00\x00\x00WEBPpayload")
        service = CompanionReplyService("prompt", timeout_seconds=1, image_loader=image_loader)
        message = WeChatInboundMessage(
            message_id="image-1",
            sender_id="u1",
            text="",
            image=WeChatInboundImage(full_url="https://cdn.example/image"),
        )

        with (
            patch(
                "backend.wechat_runtime.describe_channel_image",
                new=AsyncMock(return_value="角色正在开心地挥手"),
            ),
            patch(
                "backend.wechat_runtime.create_channel_reply",
                new=AsyncMock(return_value=CompanionReply(text="看到了")),
            ) as create_reply,
        ):
            reply = await service.reply(message)

        self.assertEqual(reply.text, "看到了")
        self.assertIn("角色正在开心地挥手", create_reply.await_args.args[0])
        image_loader.assert_awaited_once_with(message.image)


if __name__ == "__main__":
    unittest.main()
