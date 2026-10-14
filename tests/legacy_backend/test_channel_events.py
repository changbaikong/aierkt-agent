from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from backend.channel_events import ChannelEventHub, SpeechPreparationBroker
from backend.main import accept_authenticated_websocket


class FakeWebSocket:
    def __init__(self, protocol: str = "") -> None:
        self.headers = {"sec-websocket-protocol": protocol}
        self.accepted_protocol: str | None = None
        self.close_code: int | None = None
        self.close_reason = ""

    async def accept(self, subprotocol: str | None = None) -> None:
        self.accepted_protocol = subprotocol

    async def close(self, code: int, reason: str) -> None:
        self.close_code = code
        self.close_reason = reason


class WebSocketAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_websocket_rejects_missing_token(self) -> None:
        websocket = FakeWebSocket()
        with (
            patch("backend.main.REMOTE_RUNTIME", True),
            patch("backend.main.REMOTE_WS_TOKEN", "secret"),
        ):
            accepted = await accept_authenticated_websocket(websocket)

        self.assertFalse(accepted)
        self.assertEqual(websocket.close_code, 1008)
        self.assertIsNone(websocket.accepted_protocol)

    async def test_remote_websocket_rejects_wrong_token(self) -> None:
        websocket = FakeWebSocket("companion.wrong")
        with (
            patch("backend.main.REMOTE_RUNTIME", True),
            patch("backend.main.REMOTE_WS_TOKEN", "secret"),
        ):
            accepted = await accept_authenticated_websocket(websocket)

        self.assertFalse(accepted)
        self.assertEqual(websocket.close_code, 1008)

    async def test_remote_websocket_accepts_and_echoes_matching_protocol(self) -> None:
        websocket = FakeWebSocket("other.v1, companion.secret")
        with (
            patch("backend.main.REMOTE_RUNTIME", True),
            patch("backend.main.REMOTE_WS_TOKEN", "secret"),
        ):
            accepted = await accept_authenticated_websocket(websocket)

        self.assertTrue(accepted)
        self.assertEqual(websocket.accepted_protocol, "companion.secret")
        self.assertIsNone(websocket.close_code)


class ChannelEventHubTests(unittest.TestCase):
    def test_events_published_before_subscription_are_not_replayed(self) -> None:
        hub = ChannelEventHub()
        hub.publish({"type": "speech", "event_id": "old"})

        queue = hub.subscribe()
        try:
            self.assertTrue(queue.empty())
        finally:
            hub.unsubscribe(queue)

    def test_active_subscriber_receives_an_independent_event_copy(self) -> None:
        hub = ChannelEventHub()
        queue = hub.subscribe()
        event = {"type": "speech", "event_id": "current"}
        try:
            hub.publish(event)
            event["event_id"] = "mutated"
            self.assertEqual(queue.get_nowait()["event_id"], "current")
        finally:
            hub.unsubscribe(queue)


class SpeechPreparationBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_ack_completes_preparation(self) -> None:
        hub = ChannelEventHub()
        broker = SpeechPreparationBroker(hub)
        queue = hub.subscribe()
        try:
            task = asyncio.create_task(broker.prepare({
                "type": "speech_prepare",
                "event_id": "wechat:ready",
            }, timeout_seconds=1))
            event = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(event["type"], "speech_prepare")
            self.assertTrue(broker.resolve("wechat:ready", "ready"))
            self.assertTrue(await task)
        finally:
            hub.unsubscribe(queue)

    async def test_timeout_cancels_prepared_audio(self) -> None:
        hub = ChannelEventHub()
        broker = SpeechPreparationBroker(hub)
        queue = hub.subscribe()
        try:
            prepared = await broker.prepare({
                "type": "speech_prepare",
                "event_id": "wechat:slow",
            }, timeout_seconds=0.01)
            events = [queue.get_nowait() for _ in range(queue.qsize())]
        finally:
            hub.unsubscribe(queue)

        self.assertFalse(prepared)
        self.assertEqual([event["type"] for event in events], ["speech_prepare", "speech_cancel"])

    async def test_no_desktop_skips_without_waiting(self) -> None:
        hub = ChannelEventHub()
        broker = SpeechPreparationBroker(hub)
        self.assertFalse(await broker.prepare({
            "type": "speech_prepare",
            "event_id": "wechat:offline",
        }, timeout_seconds=1))

if __name__ == "__main__":
    unittest.main()
