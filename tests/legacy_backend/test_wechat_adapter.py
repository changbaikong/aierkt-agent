from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from backend.wechat_adapter import (
    CompanionReply,
    PendingDeliveryStore,
    ProcessedMessageStore,
    WeChatChannelAdapter,
    WeChatInboundImage,
    WeChatInboundMessage,
)
from backend.wechat_ilink import (
    ILinkAccountState,
    ILinkClient,
    load_recipient_contexts,
    parse_inbound_messages,
    save_recipient_contexts,
)


class FakeTransport:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.images: list[tuple[str, Path]] = []

    async def send_text(self, recipient_id: str, text: str) -> None:
        self.texts.append((recipient_id, text))

    async def send_image(self, recipient_id: str, image_path: Path) -> None:
        self.images.append((recipient_id, image_path))


class FailingTransport(FakeTransport):
    async def send_text(self, recipient_id: str, text: str) -> None:
        raise RuntimeError("wechat unavailable")


class FailOnSecondBubbleTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def send_text(self, recipient_id: str, text: str) -> None:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("second bubble failed")
        await super().send_text(recipient_id, text)


class FailingStickerTransport(FakeTransport):
    async def send_image(self, recipient_id: str, image_path: Path) -> None:
        raise RuntimeError("sticker failed")


class WeChatAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_message_is_delivered_once(self) -> None:
        transport = FakeTransport()

        async def reply_handler(_message: WeChatInboundMessage) -> CompanionReply:
            return CompanionReply(text="reply", sticker_path=Path("sticker.png"))

        adapter = WeChatChannelAdapter(transport=transport, reply_handler=reply_handler)
        message = WeChatInboundMessage(message_id="m1", sender_id="u1", text="hello")

        self.assertTrue(await adapter.handle_message(message))
        self.assertFalse(await adapter.handle_message(message))
        self.assertEqual(transport.texts, [("u1", "reply")])
        self.assertEqual(transport.images, [("u1", Path("sticker.png"))])

    async def test_reply_bubbles_are_sent_through_a_two_item_window(self) -> None:
        transport = FakeTransport()
        sleep = AsyncMock()

        async def reply_handler(_message: WeChatInboundMessage) -> CompanionReply:
            return CompanionReply(
                text="第一句。\n第二句。\n第三句。",
                bubbles=("第一句。", "第二句。", "第三句。"),
            )

        adapter = WeChatChannelAdapter(
            transport=transport,
            reply_handler=reply_handler,
            sleep=sleep,
        )
        message = WeChatInboundMessage(message_id="windowed", sender_id="u1", text="hello")

        self.assertTrue(await adapter.handle_message(message))
        self.assertEqual(transport.texts, [
            ("u1", "第一句。"),
            ("u1", "第二句。"),
            ("u1", "第三句。"),
        ])
        self.assertEqual(sleep.await_count, 2)

    async def test_partial_delivery_resumes_without_regeneration_or_duplicate_bubble(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pending_path = Path(directory) / "pending.json"
            processed_path = Path(directory) / "processed.json"
            reply_handler = AsyncMock(return_value=CompanionReply(
                text="第一句。\n第二句。\n第三句。",
                bubbles=("第一句。", "第二句。", "第三句。"),
            ))
            first_transport = FailOnSecondBubbleTransport()
            first_adapter = WeChatChannelAdapter(
                transport=first_transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(processed_path),
                pending_store=PendingDeliveryStore(pending_path),
                sleep=AsyncMock(),
            )
            message = WeChatInboundMessage("partial", "u1", "hello")

            with self.assertRaisesRegex(RuntimeError, "second bubble failed"):
                await first_adapter.handle_message(message)
            self.assertEqual(first_transport.texts, [("u1", "第一句。")])

            resumed_transport = FakeTransport()
            resumed_handler = AsyncMock(side_effect=AssertionError("must not regenerate"))
            resumed_adapter = WeChatChannelAdapter(
                transport=resumed_transport,
                reply_handler=resumed_handler,
                processed_store=ProcessedMessageStore(processed_path),
                pending_store=PendingDeliveryStore(pending_path),
                sleep=AsyncMock(),
            )

            self.assertTrue(await resumed_adapter.handle_message(message))
            self.assertEqual(resumed_transport.texts, [
                ("u1", "第二句。"),
                ("u1", "第三句。"),
            ])
            resumed_handler.assert_not_awaited()
            self.assertFalse(PendingDeliveryStore(pending_path).contains("partial"))

    async def test_sticker_failure_resumes_without_duplicate_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pending_path = Path(directory) / "pending.json"
            processed_path = Path(directory) / "processed.json"
            reply_handler = AsyncMock(return_value=CompanionReply(
                text="先发文字。",
                bubbles=("先发文字。",),
                sticker_path=Path("sticker.webp"),
            ))
            message = WeChatInboundMessage("sticker-partial", "u1", "hello")
            first_transport = FailingStickerTransport()
            first_adapter = WeChatChannelAdapter(
                transport=first_transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(processed_path),
                pending_store=PendingDeliveryStore(pending_path),
                sleep=AsyncMock(),
            )

            with self.assertRaisesRegex(RuntimeError, "sticker failed"):
                await first_adapter.handle_message(message)
            self.assertEqual(first_transport.texts, [("u1", "先发文字。")])

            resumed_transport = FakeTransport()
            resumed_handler = AsyncMock(side_effect=AssertionError("must not regenerate"))
            resumed_adapter = WeChatChannelAdapter(
                transport=resumed_transport,
                reply_handler=resumed_handler,
                processed_store=ProcessedMessageStore(processed_path),
                pending_store=PendingDeliveryStore(pending_path),
                sleep=AsyncMock(),
            )

            self.assertTrue(await resumed_adapter.handle_message(message))
            self.assertEqual(resumed_transport.texts, [])
            self.assertEqual(resumed_transport.images, [("u1", Path("sticker.webp"))])
            resumed_handler.assert_not_awaited()
            self.assertFalse(PendingDeliveryStore(pending_path).contains("sticker-partial"))

    async def test_processed_message_is_not_delivered_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store_path = Path(directory) / "processed.json"
            message = WeChatInboundMessage(message_id="persisted", sender_id="u1", text="hello")

            first_transport = FakeTransport()

            async def reply_handler(_message: WeChatInboundMessage) -> CompanionReply:
                return CompanionReply(text="reply")

            first_adapter = WeChatChannelAdapter(
                transport=first_transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(store_path),
            )
            self.assertTrue(await first_adapter.handle_message(message))

            restarted_transport = FakeTransport()
            restarted_adapter = WeChatChannelAdapter(
                transport=restarted_transport,
                reply_handler=reply_handler,
                processed_store=ProcessedMessageStore(store_path),
            )
            self.assertFalse(await restarted_adapter.handle_message(message))
            self.assertEqual(restarted_transport.texts, [])

    async def test_delivery_handler_runs_only_after_successful_send(self) -> None:
        delivered = MagicMock()

        async def reply_handler(_message: WeChatInboundMessage) -> CompanionReply:
            return CompanionReply(text="reply", speak_text="聞いておるぞ。")

        adapter = WeChatChannelAdapter(
            transport=FailingTransport(),
            reply_handler=reply_handler,
            delivery_handler=delivered,
        )
        message = WeChatInboundMessage(message_id="retryable", sender_id="u1", text="hello")

        with self.assertRaisesRegex(RuntimeError, "wechat unavailable"):
            await adapter.handle_message(message)

        delivered.assert_not_called()
        self.assertNotIn(message.message_id, adapter.seen_message_ids)

    def test_ilink_parser_accepts_only_user_text(self) -> None:
        updates = {
            "msgs": [
                {
                    "message_id": "user-text",
                    "message_type": 1,
                    "from_user_id": "u1",
                    "context_token": "ctx",
                    "item_list": [{"type": 1, "text_item": {"text": " hello "}}],
                },
                {
                    "message_id": "bot-text",
                    "message_type": 2,
                    "from_user_id": "u1",
                    "item_list": [{"type": 1, "text_item": {"text": "ignore"}}],
                },
                {
                    "message_id": "user-image",
                    "message_type": 1,
                    "from_user_id": "u1",
                    "context_token": "image-ctx",
                    "item_list": [{
                        "type": 2,
                        "image_item": {
                            "media": {
                                "encrypt_query_param": "download-param",
                                "aes_key": "a2V5",
                            },
                        },
                    }],
                },
                {
                    "message_id": "user-image-without-reference",
                    "message_type": 1,
                    "from_user_id": "u1",
                    "item_list": [{"type": 2, "image_item": {}}],
                },
            ]
        }

        parsed = parse_inbound_messages(updates)

        self.assertEqual(len(parsed), 3)
        message, context_token = parsed[0]
        self.assertEqual(message.message_id, "user-text")
        self.assertEqual(message.text, "hello")
        self.assertEqual(context_token, "ctx")
        image_message, image_context = parsed[1]
        self.assertEqual(image_message.message_id, "user-image")
        self.assertEqual(image_message.text, "")
        self.assertEqual(image_message.image.encrypted_query_param, "download-param")
        self.assertEqual(image_context, "image-ctx")
        self.assertIsNotNone(parsed[2][0].image)

    async def test_ilink_image_upload_encrypts_and_sends_cdn_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "sticker.webp"
            plaintext = b"test-image-payload"
            image_path.write_bytes(plaintext)
            aes_key = bytes(range(16))
            client = ILinkClient(ILinkAccountState(token="token", account_id="bot", user_id="user"))
            client._post = AsyncMock(side_effect=[
                {"upload_full_url": "https://cdn.example/upload"},
                {},
            ])
            cdn_response = MagicMock(
                status_code=200,
                headers={"x-encrypted-param": "download-param"},
            )
            cdn_response.raise_for_status.return_value = None
            client._client.post = AsyncMock(return_value=cdn_response)

            with patch("backend.wechat_ilink.secrets.token_bytes", return_value=aes_key):
                await client.send_image("recipient", image_path, "context")

            cdn_call = client._client.post.await_args
            ciphertext = cdn_call.kwargs["content"]
            decrypted = unpad(AES.new(aes_key, AES.MODE_ECB).decrypt(ciphertext), AES.block_size)
            self.assertEqual(decrypted, plaintext)

            send_payload = client._post.await_args_list[1].args[1]
            image_item = send_payload["msg"]["item_list"][0]["image_item"]
            self.assertEqual(image_item["media"]["encrypt_query_param"], "download-param")
            self.assertEqual(
                image_item["media"]["aes_key"],
                base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
            )
            self.assertEqual(image_item["mid_size"], len(ciphertext))
            self.assertEqual(send_payload["msg"]["context_token"], "context")

            await client.close()

    async def test_ilink_image_download_decrypts_aes_payload(self) -> None:
        key = bytes(range(16))
        plaintext = b"\x89PNG\r\n\x1a\nimage-data"
        ciphertext = AES.new(key, AES.MODE_ECB).encrypt(pad(plaintext, AES.block_size))
        client = ILinkClient(ILinkAccountState(token="token", account_id="bot", user_id="user"))
        response = MagicMock(content=ciphertext)
        response.raise_for_status.return_value = None
        client._client.get = AsyncMock(return_value=response)
        image = WeChatInboundImage(
            full_url="https://cdn.example/download",
            aes_key=base64.b64encode(key).decode("ascii"),
        )

        downloaded = await client.download_image(image)

        self.assertEqual(downloaded, plaintext)
        await client.close()

    def test_recipient_context_is_persisted_in_direct_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            context_file = state_dir / "contexts.json"
            with (
                patch("backend.wechat_ilink.STATE_DIR", state_dir),
                patch("backend.wechat_ilink.CONTEXT_FILE", context_file),
            ):
                save_recipient_contexts({"recipient": "context-token"})
                reloaded = load_recipient_contexts()

            self.assertEqual(reloaded, {"recipient": "context-token"})

    async def test_ilink_client_rejects_messages_from_unbound_senders(self) -> None:
        client = ILinkClient(
            ILinkAccountState(token="token", account_id="bot", user_id="allowed")
        )
        client._post = AsyncMock(return_value={
            "msgs": [
                {
                    "message_id": "allowed-message",
                    "message_type": 1,
                    "from_user_id": "allowed",
                    "item_list": [{"type": 1, "text_item": {"text": "hello"}}],
                },
                {
                    "message_id": "blocked-message",
                    "message_type": 1,
                    "from_user_id": "not-bound",
                    "item_list": [{"type": 1, "text_item": {"text": "ignore"}}],
                },
            ],
        })

        with (
            patch("backend.wechat_ilink.save_state"),
            patch("backend.wechat_ilink.save_recipient_contexts"),
        ):
            updates = await client.get_updates()

        self.assertEqual([message.message_id for message, _ in updates], ["allowed-message"])
        await client.close()

    async def test_ilink_typing_ticket_is_cached_and_cancelled(self) -> None:
        client = ILinkClient(ILinkAccountState(token="token", account_id="bot", user_id="user"))
        client._post = AsyncMock(side_effect=[{"typing_ticket": "ticket"}, {}, {}])

        await client.send_typing("user", "context", True)
        await client.send_typing("user", "context", False)

        endpoints = [call.args[0] for call in client._post.await_args_list]
        self.assertEqual(endpoints, [
            "ilink/bot/getconfig",
            "ilink/bot/sendtyping",
            "ilink/bot/sendtyping",
        ])
        self.assertEqual(client._post.await_args_list[1].args[1]["status"], 1)
        self.assertEqual(client._post.await_args_list[2].args[1]["status"], 2)
        await client.close()


if __name__ == "__main__":
    unittest.main()
