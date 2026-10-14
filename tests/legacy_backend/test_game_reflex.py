from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from backend.channel_events import ChannelEventHub
from backend.game_reflex import (
    AudioAssetRegistry,
    GameEvent,
    GamePriority,
    GameReflexEngine,
    RouteDecision,
    game_event_from_dict,
    text_checksum,
)


def event(event_id: str, event_type: str, owner: str = "local-user", session: str = "game-1", **payload) -> GameEvent:
    return GameEvent(
        event_id=event_id,
        owner_id=owner,
        session_id=session,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        confidence=payload.pop("confidence", 0.98),
        source="test",
        payload=payload,
    )


class GameReflexTests(unittest.IsolatedAsyncioTestCase):
    async def test_p0_is_local_rule_only_and_emits_interrupt(self) -> None:
        hub = ChannelEventHub()
        queue = hub.subscribe()
        called = False

        async def handler(*args):
            nonlocal called
            called = True
            return {"text": "should not run"}

        engine = GameReflexEngine(hub, reply_handler=handler)
        try:
            result = await engine.ingest(event("danger-1", "danger_behind"))
            messages = [queue.get_nowait(), queue.get_nowait()]
        finally:
            await engine.close()
            hub.unsubscribe(queue)

        self.assertTrue(result.accepted)
        self.assertEqual(result.decision.route, "p0_rule")
        self.assertEqual(result.decision.priority, GamePriority.P0)
        self.assertFalse(called)
        self.assertEqual(messages[0]["type"], "speech_interrupt")
        self.assertEqual(messages[1]["type"], "game_reaction")
        self.assertIsNone(messages[1]["asset_id"])

    async def test_duplicate_and_cooldown_are_dropped(self) -> None:
        engine = GameReflexEngine(ChannelEventHub())
        try:
            first = await engine.ingest(event("kill-1", "kill"))
            duplicate = await engine.ingest(event("kill-1", "kill"))
            cooldown = await engine.ingest(event("kill-2", "kill"))
        finally:
            await engine.close()

        self.assertTrue(first.accepted)
        self.assertEqual(duplicate.decision.reason, "duplicate_event")
        self.assertEqual(cooldown.decision.reason, "cooldown")

    async def test_priority_queue_processes_p1_before_p2_for_one_owner(self) -> None:
        processed: list[str] = []

        async def handler(event_value, decision, context, emotion):
            processed.append(event_value.event_type)
            return {"text": event_value.event_type}

        engine = GameReflexEngine(ChannelEventHub(), reply_handler=handler)
        try:
            await engine.ingest(event("question-1", "player_question", text="怎么处理？"))
            await engine.ingest(event("kill-1", "kill"))
            await engine.drain("local-user")
        finally:
            await engine.close()

        self.assertEqual(processed, ["kill", "player_question"])

    async def test_pad_is_scoped_and_decays_toward_baseline(self) -> None:
        engine = GameReflexEngine(ChannelEventHub())
        try:
            await engine.ingest(event("penta-1", "penta_kill", owner="alice", session="a"))
            alice = engine.pad_state("alice", "a")
            alice_arousal = alice.arousal
            alice_updated_at = alice.updated_at
            bob = engine.pad_state("bob", "b")
            decayed = engine.pad_state("alice", "a", alice_updated_at + 90)
        finally:
            await engine.close()

        self.assertGreater(alice.arousal, bob.arousal)
        self.assertEqual(bob.to_dict(), {"pleasure": 0.1, "arousal": 0.0, "dominance": 0.0})
        self.assertLess(decayed.arousal, alice_arousal)

    def test_audio_registry_requires_verified_p0_asset_and_text_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audio-assets.json"
            text = "小心后面！"
            path.write_text(json.dumps({
                "version": 1,
                "assets": [{
                    "asset_id": "danger-voice",
                    "path": "tts-assets/danger.wav",
                    "transcript": text,
                    "category": "game_reaction",
                    "emotion": "紧张",
                    "status": "verified",
                    "p0_eligible": True,
                    "reaction_id": "danger_behind",
                    "duration_ms": 800,
                    "sha256": "file",
                    "text_sha256": text_checksum(text),
                }],
            }, ensure_ascii=False), encoding="utf-8")
            registry = AudioAssetRegistry(path)

        self.assertEqual(registry.eligible_asset("danger_behind", text_checksum(text)), "danger-voice")
        self.assertIsNone(registry.eligible_asset("danger_behind", text_checksum("别回头！")))

    def test_event_input_rejects_unknown_source_and_large_payload(self) -> None:
        with self.assertRaises(ValueError):
            game_event_from_dict({
                "event_id": "bad-source",
                "owner_id": "alice",
                "session_id": "game",
                "event_type": "kill",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "source": "unknown",
            })
        with self.assertRaises(ValueError):
            game_event_from_dict({
                "event_id": "large-payload",
                "owner_id": "alice",
                "session_id": "game",
                "event_type": "kill",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "payload": {"blob": "x" * 5000},
            })


if __name__ == "__main__":
    unittest.main()
