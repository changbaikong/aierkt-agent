import asyncio
import unittest
from unittest import mock

from agent_core.loop import AgentLoop
from agent_tools.netease_cloud import CloudMusicProvider


class MusicRoutingRegressions(unittest.TestCase):
    def test_status_phrases_never_become_searches(self):
        for phrase in (
            "现在在放什么歌", "你检测网易云状态", "网易云现在有没有播放歌曲",
            "网易云是不是正在播放音乐？", "检查一下播放器状态", "现在在放什么歌？告诉我一下",
        ):
            with self.subTest(phrase=phrase):
                self.assertTrue(AgentLoop._is_current_playback_request(phrase))
                self.assertIsNone(AgentLoop._music_query(phrase))

    def test_music_mentions_and_pasted_results_are_not_search_commands(self):
        for phrase in (
            "网易云找到候选歌曲：\n1. 晴天 - 周杰伦\n2. 夜曲 - 周杰伦",
            "你为什么一直给我候选歌曲", "网易云登录了吗", "暂停播放", "这首歌很好听",
        ):
            with self.subTest(phrase=phrase):
                self.assertIsNone(AgentLoop._music_query(phrase))

    def test_repository_request_mentioning_impact_is_not_playback_status(self):
        self.assertFalse(AgentLoop._is_current_playback_request(
            "检查 C:/Projects/demo 的依赖升级影响，必要时修改代码和测试。"
        ))

    def test_explicit_search_extracts_only_the_query(self):
        for phrase, query in (
            ("帮我搜索一下周杰伦的歌", "周杰伦的歌"),
            ("在网易云搜索晴天", "晴天"),
            ("帮我在网易云搜一下夜曲", "夜曲"),
            ("搜索歌曲《现在在放什么歌》", "《现在在放什么歌》"),
        ):
            with self.subTest(phrase=phrase):
                self.assertFalse(AgentLoop._is_current_playback_request(phrase))
                self.assertEqual(AgentLoop._music_query(phrase), query)


class MusicStartupRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_first_call_accepts_slow_startup_stages(self):
        for slow_stage in ("initialize", "tools/list"):
            with self.subTest(stage=slow_stage):
                provider = CloudMusicProvider(timeout_seconds=5)
                process = mock.Mock(returncode=None)
                process.stdin.wait_closed = mock.AsyncMock()
                process.wait = mock.AsyncMock()

                async def request(method, params):
                    if method == slow_stage:
                        await asyncio.sleep(2.1)
                    if method == "tools/list":
                        return {"result": {"tools": [{"name": "cloud_music_player_status"}]}}
                    return {"result": {"structuredContent": {"ok": True, "data": {"status": "paused"}}}}

                with mock.patch("agent_tools.netease_cloud.Path.exists", return_value=True), \
                     mock.patch("agent_tools.netease_cloud.Path.is_dir", return_value=True), \
                     mock.patch("agent_tools.netease_cloud.asyncio.create_subprocess_exec", return_value=process), \
                     mock.patch.object(provider, "_request", side_effect=request), \
                     mock.patch.object(provider, "_notify", new_callable=mock.AsyncMock):
                    try:
                        self.assertEqual(await provider.player_status(), {"status": "paused"})
                    finally:
                        await provider.close()
