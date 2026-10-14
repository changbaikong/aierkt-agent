from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.channel_events import channel_event_hub
from backend.memory import MemoryScope
from backend.music_api import CatalogTrack, _CircuitBreaker, _MusicCatalog
from backend.music_tools import (
    MUSIC_TOOL_SCHEMAS,
    create_music_tool_executor,
    resolve_music_command_result,
)
from backend.tool_authorization import authorize_owner, deauthorize_owner


LOCAL = MemoryScope(owner_id="local-user", channel="desktop", conversation_id="music-test")


def _songs() -> dict:
    return {
        "code": 200,
        "songs": [
            {"id": "100", "name": "晴天", "artist": "周杰伦"},
            {"id": "101", "name": "七里香", "artist": "周杰伦"},
        ],
    }


class MusicCatalogTests(unittest.TestCase):
    def test_title_artist_and_combined_queries_share_catalog(self) -> None:
        catalog = _MusicCatalog()
        tracks = [
            CatalogTrack("1", "晴天", "周杰伦", "netease"),
            CatalogTrack("2", "晴天", "其他歌手", "netease"),
        ]
        catalog.store("周杰伦晴天", tracks)
        self.assertEqual(catalog.lookup("晴天")[0].song_id, "1")
        self.assertEqual(catalog.lookup("周杰伦")[0].song_id, "1")
        self.assertEqual(catalog.lookup("周杰伦晴天")[0].song_id, "1")

    def test_negative_result_is_cached(self) -> None:
        catalog = _MusicCatalog()
        catalog.store("不存在", [])
        self.assertEqual(catalog.lookup("不存在"), [])

    def test_breaker_opens_after_two_failures(self) -> None:
        breaker = _CircuitBreaker(threshold=2, cooldown_seconds=60)
        self.assertTrue(breaker.allow())
        breaker.failure()
        breaker.failure()
        self.assertFalse(breaker.allow())
        breaker.success()
        self.assertTrue(breaker.allow())


class MusicToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        import backend.music_api as music_api

        music_api._catalog = _MusicCatalog()
        music_api._search_inflight.clear()

    async def test_search_and_semantic_cache(self) -> None:
        import backend.music_api as music_api

        calls = 0

        async def fetch(_query: str):
            nonlocal calls
            calls += 1
            return [
                CatalogTrack("100", "晴天", "周杰伦", "netease"),
                CatalogTrack("101", "七里香", "周杰伦", "netease"),
            ]

        executor = create_music_tool_executor(LOCAL)
        with patch.object(music_api, "_fetch_remote_tracks", new=fetch):
            first = await executor("music_search", {"query": "周杰伦 晴天"})
            second = await executor("music_search", {"query": "晴天"})
            third = await executor("music_search", {"query": "周杰伦"})

        self.assertEqual(calls, 1)
        self.assertEqual(first["output"]["songs"][0]["name"], "晴天")
        self.assertEqual(second["output"]["songs"][0]["artist"], "周杰伦")
        self.assertNotIn("id", first["output"]["songs"][0])
        self.assertNotIn("url", first["output"]["songs"][0])
        self.assertEqual(third["output"]["count"], 2)

    async def test_singleflight_deduplicates_same_query(self) -> None:
        import backend.music_api as music_api

        gate = asyncio.Event()
        calls = 0

        async def fetch(_query: str):
            nonlocal calls
            calls += 1
            await gate.wait()
            return [CatalogTrack("100", "晴天", "周杰伦", "netease")]

        executor = create_music_tool_executor(LOCAL)
        with patch.object(music_api, "_fetch_remote_tracks", new=fetch):
            first = asyncio.create_task(executor("music_search", {"query": "晴天"}))
            second = asyncio.create_task(executor("music_search", {"query": "晴天"}))
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(first, second)

        self.assertEqual(calls, 1)

    async def test_play_uses_id_only_and_resolves_desktop_result(self) -> None:
        import backend.music_api as music_api

        queue = channel_event_hub.subscribe()
        try:
            executor = create_music_tool_executor(LOCAL)
            with patch.object(music_api, "_fetch_remote_tracks", new=AsyncMock(return_value=[
                CatalogTrack("100", "晴天", "周杰伦", "netease"),
            ])):
                search = await executor("music_search", {"query": "晴天"})
            ref = search["output"]["songs"][0]["track_ref"]

            task = asyncio.create_task(executor("music_play", {"track_ref": ref}))
            event = await asyncio.wait_for(queue.get(), timeout=1)
            self.assertEqual(event["action"], "play_track")
            self.assertEqual(event["track"], {"id": "100", "name": "晴天", "artist": "周杰伦"})
            self.assertNotIn("url", event)

            resolve_music_command_result({
                "command_id": event["command_id"],
                "ok": True,
                "state": {"status": "playing", "title": "晴天", "artist": "周杰伦"},
            })
            result = await task
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["output"]["name"], "晴天")
        finally:
            channel_event_hub.unsubscribe(queue)

    async def test_play_fails_without_desktop(self) -> None:
        executor = create_music_tool_executor(LOCAL)
        result = await executor("music_play", {"track_ref": "missing"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("重新搜索", result["error"])

    async def test_pause_next_and_resume_are_commands(self) -> None:
        queue = channel_event_hub.subscribe()
        try:
            executor = create_music_tool_executor(LOCAL)
            for tool, action in (
                ("music_pause", "pause"),
                ("music_resume", "resume"),
                ("music_next", "next"),
            ):
                task = asyncio.create_task(executor(tool, {}))
                event = await asyncio.wait_for(queue.get(), timeout=1)
                self.assertEqual(event["action"], action)
                resolve_music_command_result({
                    "command_id": event["command_id"],
                    "ok": True,
                    "state": {"status": "playing"},
                })
                self.assertEqual((await task)["status"], "ok")
        finally:
            channel_event_hub.unsubscribe(queue)

    async def test_random_and_playlist_commands(self) -> None:
        import backend.music_api as music_api

        queue = channel_event_hub.subscribe()
        try:
            executor = create_music_tool_executor(LOCAL)
            with patch.object(music_api, "random_liked_track", new=AsyncMock(return_value={
                "id": "200", "name": "随机曲", "artist": "歌手",
            })):
                random_task = asyncio.create_task(executor("music_play_random", {}))
                event = await asyncio.wait_for(queue.get(), timeout=1)
                self.assertEqual(event["action"], "play_track")
                resolve_music_command_result({"command_id": event["command_id"], "ok": True})
                self.assertEqual((await random_task)["output"]["random"], True)

            with patch.object(music_api, "my_playlists", new=AsyncMock(return_value={
                "code": 200,
                "playlists": [{"id": "300", "name": "我的歌单", "count": 12, "liked": False}],
            })):
                playlist_task = asyncio.create_task(executor("music_play_playlist", {"name": "歌单"}))
                event = await asyncio.wait_for(queue.get(), timeout=1)
                self.assertEqual(event["action"], "play_playlist")
                self.assertEqual(event["playlist"]["id"], "300")
                resolve_music_command_result({"command_id": event["command_id"], "ok": True})
                self.assertEqual((await playlist_task)["status"], "ok")
        finally:
            channel_event_hub.unsubscribe(queue)

    async def test_playlists_are_readable_for_unbound_channels(self) -> None:
        import backend.music_api as music_api

        stranger = MemoryScope(owner_id="wechat:stranger", channel="wechat", conversation_id="stranger")
        executor = create_music_tool_executor(stranger)
        with patch.object(music_api, "my_playlists", new=AsyncMock(return_value={
            "code": 200, "playlists": [{"id": "1", "name": "歌单", "count": 1, "liked": False}],
        })):
            result = await executor("music_playlists", {})
        self.assertEqual(result["status"], "ok")

        denied = await executor("music_pause", {})
        self.assertEqual(denied["status"], "failed")
        self.assertIn("权限", denied["error"])

    async def test_authorized_wechat_owner_can_control(self) -> None:
        owner = "wechat:owner"
        authorize_owner(owner)
        queue = channel_event_hub.subscribe()
        try:
            executor = create_music_tool_executor(MemoryScope(owner, "wechat", "owner"))
            task = asyncio.create_task(executor("music_next", {}))
            event = await asyncio.wait_for(queue.get(), timeout=1)
            resolve_music_command_result({"command_id": event["command_id"], "ok": True})
            self.assertEqual((await task)["status"], "ok")
        finally:
            deauthorize_owner(owner)
            channel_event_hub.unsubscribe(queue)


class MusicSchemaTests(unittest.TestCase):
    def test_expected_tools_are_registered(self) -> None:
        names = [schema["function"]["name"] for schema in MUSIC_TOOL_SCHEMAS]
        self.assertEqual(names, [
            "music_search", "music_play", "music_next", "music_pause",
            "music_resume", "music_status", "music_play_random",
            "music_playlists", "music_play_playlist",
        ])


if __name__ == "__main__":
    unittest.main()

