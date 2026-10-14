from __future__ import annotations

import asyncio
import os
import unittest
from unittest import mock

from agent_core.contracts import EFFECT_READ, EFFECT_TRANSIENT, Scope
from agent_tools.modules import build_netease_module
from agent_tools.netease_cloud import CloudMusicProvider, build_cloud_music_provider


class FakeCloudProvider:
    async def status(self):
        return {"logged_in": True, "nickname": "测试用户"}

    async def search(self, keyword: str, category: str = "song", limit: int = 5):
        return [{"id": "1", "name": keyword, "artist": "测试歌手"}]


class NeteaseCloudIntegrationTests(unittest.TestCase):
    def test_volume_tools_preserve_read_and_write_contracts(self) -> None:
        tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}
        self.assertEqual(tools["get_volume"].effect, EFFECT_READ)
        self.assertEqual(tools["set_volume"].effect, EFFECT_TRANSIENT)
        # 改音量是**改本机状态**：和系统音量同一个口径，必须先确认。
        self.assertEqual(tools["set_volume"].risk, "high")
        self.assertTrue(tools["set_volume"].requires_confirmation)
        self.assertEqual(tools["set_volume"].confirmation_title, "调整网易云音量")

    def test_music_transport_controls_run_directly(self) -> None:
        """播放/暂停/继续/下一首保持直接执行 —— 这是用户点歌时最不想被打断的一串动作。"""
        tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}

        for name in ("play", "pause", "resume", "next"):
            self.assertEqual(tools[name].risk, "low", name)
            self.assertFalse(tools[name].requires_confirmation, name)

    def test_readonly_queries_run_directly(self) -> None:
        tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}

        for name in ("status", "search", "daily_recommend", "my_playlists", "playlist_detail",
                     "album_info", "artist_info", "my_subscriptions", "player_status", "get_volume"):
            self.assertEqual(tools[name].risk, "low", name)
            self.assertEqual(tools[name].effect, EFFECT_READ, name)

    def test_playlist_writes_still_require_confirmation(self) -> None:
        tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}

        for name in ("create_playlist", "add_to_playlist"):
            self.assertEqual(tools[name].risk, "high", name)
            self.assertTrue(tools[name].requires_confirmation, name)

    def test_risk_policy_matrix(self) -> None:
        """把权限矩阵钉在**策略**层：查询/播放放行，改音量与歌单写入进确认。"""
        from agent_core.tools import DefaultRiskPolicy

        tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}
        policy = DefaultRiskPolicy()
        scope = Scope("user123", "web", "conv", "companion")

        allow = ["player_status", "get_volume", "search", "play", "pause", "resume", "next"]
        confirm = ["set_volume", "create_playlist", "add_to_playlist"]

        for name in allow:
            self.assertEqual(policy.authorize(tools[name], scope, {}), "allow", name)
        for name in confirm:
            self.assertEqual(policy.authorize(tools[name], scope, {}), "confirm", name)

    def test_anonymous_scope_cannot_change_volume(self) -> None:
        from agent_core.tools import DefaultRiskPolicy

        tools = {tool.name: tool for tool in build_netease_module(FakeCloudProvider()).build_tools()}
        policy = DefaultRiskPolicy()
        anonymous = Scope("anonymous:xyz", "web", "conv", "companion")

        self.assertEqual(policy.authorize(tools["set_volume"], anonymous, {}), "deny")
        self.assertEqual(policy.authorize(tools["get_volume"], anonymous, {}), "allow")

    def test_real_provider_uses_external_project_defaults_without_importing_pyncm(self) -> None:
        # 默认值改成「由环境变量给出」：仓库里不能再写死某个人的本机路径，
        # 所以这里验证「环境变量能配进去」，而不是验证某个固定的机器路径。
        with mock.patch.dict(
            os.environ,
            {
                "NETEASE_MCP_UV": r"C:\tools\uv.exe",
                "NETEASE_MCP_DIRECTORY": r"C:\tools\cloud-music-mcp-main",
            },
        ):
            provider = build_cloud_music_provider()

        self.assertIsInstance(provider, CloudMusicProvider)
        self.assertTrue(provider.command.endswith("uv.exe"))
        self.assertIn("cloud-music-mcp-main", provider.directory)

    def test_netease_module_exposes_real_cloud_music_tools(self) -> None:
        module = build_netease_module(FakeCloudProvider())
        names = {tool.name for tool in module.build_tools()}

        self.assertEqual(
            names,
            {
                "status",
                "login",
                "search",
                "daily_recommend",
                "my_playlists",
                "playlist_detail",
                "create_playlist",
                "add_to_playlist",
                "album_info",
                "artist_info",
                "my_subscriptions",
                "play",
                "player_status",
                "next",
                "pause",
                "resume",
                "get_volume",
                "set_volume",
            },
        )

    def test_cloud_music_provider_forwards_playback_and_playlist_operations(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")
        calls: list[tuple[str, dict]] = []

        async def fake_call(name: str, arguments: dict):
            calls.append((name, arguments))
            return {"called": name}

        provider._call = fake_call  # type: ignore[method-assign]

        async def exercise() -> None:
            await provider.create_playlist("Favorites", privacy=True)
            await provider.add_to_playlist("42", [1, "2"])
            await provider.play(99, type="playlist")
            await provider.player_status()
            await provider.next()
            await provider.pause()
            await provider.resume()
            await provider.get_volume()
            await provider.set_volume(30)

        asyncio.run(exercise())

        self.assertEqual(
            calls,
            [
                ("cloud_music_create_playlist", {"name": "Favorites", "privacy": True}),
                ("cloud_music_add_to_playlist", {"playlist_id": "42", "track_ids": [1, "2"]}),
                ("cloud_music_play", {"id": 99, "type": "playlist"}),
                ("cloud_music_player_status", {}),
                ("cloud_music_next", {}),
                ("cloud_music_pause", {}),
                ("cloud_music_resume", {}),
                ("cloud_music_get_volume", {}),
                ("cloud_music_set_volume", {"volume_percent": 30}),
            ],
        )

    def test_side_effecting_cloud_music_tools_require_confirmation(self) -> None:
        module = build_netease_module(FakeCloudProvider())
        tools = {tool.name: tool for tool in module.build_tools()}

        # 播放控制整套（play/next/pause/resume）无需确认，后台播放全程无感；
        # 只有“确认是哪首歌”由模型在对话里向用户确认，不走工具风险门。
        for name in ("play", "next", "pause", "resume"):
            self.assertEqual(tools[name].risk, "low")
            self.assertFalse(tools[name].requires_confirmation)
        # 会改动账号歌单的仍保持高风险、需确认
        for name in ("create_playlist", "add_to_playlist"):
            self.assertEqual(tools[name].risk, "high")

    def test_cloud_music_status_tool_returns_structured_result(self) -> None:
        module = build_netease_module(FakeCloudProvider())
        tool = next(tool for tool in module.build_tools() if tool.name == "status")

        result = asyncio.run(tool.handler({}, Scope("owner", "web", "chat", "netease")))

        self.assertEqual(result, {"status": "ok", "output": {"logged_in": True, "nickname": "测试用户"}})

    def test_existing_catalog_provider_remains_supported(self) -> None:
        from examples.netease_mock import MockNeteaseCatalog

        module = build_netease_module(MockNeteaseCatalog())
        names = {tool.name for tool in module.build_tools()}

        self.assertIn("search_tracks", names)

    def test_capability_probe_checks_mcp_catalog_and_player_rpc(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")
        forced: list[bool] = []

        async def fake_probe(*, force: bool = False):
            forced.append(force)
            return {
                "status": "healthy",
                "tools": ["cloud_music_search", "cloud_music_player_status"],
                "cached": False,
            }

        async def fake_player_status():
            return {"status": "playing", "title": "Moon"}

        provider.probe = fake_probe  # type: ignore[method-assign]
        provider.player_status = fake_player_status  # type: ignore[method-assign]

        result = asyncio.run(provider.probe_capabilities(force=True))

        self.assertEqual(forced, [True])
        self.assertEqual(result, [
            {
                "name": "mcp", "status": "healthy", "code": "", "reason": "",
                "tools": ["cloud_music_search", "cloud_music_player_status"],
            },
            {"name": "player", "status": "healthy", "code": "", "reason": ""},
        ])

    def test_capability_probe_distinguishes_missing_mcp_tool_from_player_failure(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")
        player_called = False

        async def fake_probe(*, force: bool = False):
            return {
                "status": "degraded",
                "reason": "tool_missing",
                "missing": ["cloud_music_player_status"],
                "tools": ["cloud_music_search"],
            }

        async def fake_player_status():
            nonlocal player_called
            player_called = True
            return {"status": "playing"}

        provider.probe = fake_probe  # type: ignore[method-assign]
        provider.player_status = fake_player_status  # type: ignore[method-assign]

        result = asyncio.run(provider.probe_capabilities())

        self.assertFalse(player_called)
        self.assertEqual(result, [
            {
                "name": "mcp", "status": "degraded", "code": "mcp_tool_missing",
                "reason": "MCP is missing required tools",
                "tools": ["cloud_music_search"],
            },
            {
                "name": "player", "status": "unavailable", "code": "mcp_dependency_unavailable",
                "reason": "player probe requires a healthy MCP",
            },
        ])

    def test_forced_mcp_probe_refreshes_the_live_tool_catalog(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")
        provider._process = type("LiveProcess", (), {"returncode": None})()
        provider._available_tools = {"cloud_music_search", "cloud_music_player_status"}
        requests: list[str] = []

        async def fake_request(method: str, _params: dict):
            requests.append(method)
            return {"result": {"tools": [{"name": "cloud_music_search"}]}}

        provider._request = fake_request  # type: ignore[method-assign]

        result = asyncio.run(provider.probe(force=True))

        self.assertEqual(requests, ["tools/list"])
        self.assertEqual(result["status"], "degraded")
        self.assertIn("cloud_music_player_status", result["missing"])
        self.assertIn("cloud_music_play", result["missing"])

    def test_forced_mcp_probe_waits_for_active_tool_io(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")
        started = asyncio.Event()

        async def fake_ensure_started(*, force: bool = False):
            started.set()
            provider._available_tools = {
                "cloud_music_status", "cloud_music_login", "cloud_music_search",
                "cloud_music_get_daily_recommend", "cloud_music_my_playlists",
                "cloud_music_playlist_detail", "cloud_music_create_playlist",
                "cloud_music_add_to_playlist", "cloud_music_album_info",
                "cloud_music_artist_info", "cloud_music_my_subscriptions",
                "cloud_music_play", "cloud_music_player_status", "cloud_music_next",
                "cloud_music_pause", "cloud_music_resume",
            }

        provider._ensure_started = fake_ensure_started  # type: ignore[method-assign]

        async def exercise() -> None:
            await provider._lock.acquire()
            try:
                task = asyncio.create_task(provider.probe(force=True))
                await asyncio.sleep(0)
                self.assertFalse(started.is_set())
            finally:
                provider._lock.release()
            result = await task
            self.assertEqual(result["status"], "healthy")

        asyncio.run(exercise())

    def test_capability_probe_reports_player_rpc_failure(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")

        async def fake_probe(*, force: bool = False):
            return {"status": "healthy", "tools": ["cloud_music_search", "cloud_music_player_status"]}

        async def failing_player_status():
            raise CloudMusicError("player process not found")

        provider.probe = fake_probe  # type: ignore[method-assign]
        provider.player_status = failing_player_status  # type: ignore[method-assign]

        result = asyncio.run(provider.probe_capabilities())

        self.assertEqual(result[1], {
            "name": "player", "status": "unavailable", "code": "player_unavailable",
            "reason": "player status could not be read",
        })

    def test_capability_probe_reports_disconnected_mcp_without_calling_player(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")
        player_called = False

        async def disconnected_probe(*, force: bool = False):
            return {"status": "unhealthy", "reason": "mcp_disconnected"}

        async def fake_player_status():
            nonlocal player_called
            player_called = True
            return {"status": "playing"}

        provider.probe = disconnected_probe  # type: ignore[method-assign]
        provider.player_status = fake_player_status  # type: ignore[method-assign]

        result = asyncio.run(provider.probe_capabilities())

        self.assertFalse(player_called)
        self.assertEqual(result[0], {
            "name": "mcp", "status": "unhealthy", "code": "mcp_disconnected",
            "reason": "MCP is disconnected", "tools": [],
        })
        self.assertEqual(result[1]["code"], "mcp_dependency_unavailable")

    def test_capability_probe_bounds_player_status_rpc(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music", timeout_seconds=0.01)

        async def healthy_probe(*, force: bool = False):
            return {"status": "healthy", "tools": ["cloud_music_search", "cloud_music_player_status"]}

        async def stalled_player_status():
            await asyncio.sleep(0.1)
            return {"status": "playing"}

        provider.probe = healthy_probe  # type: ignore[method-assign]
        provider.player_status = stalled_player_status  # type: ignore[method-assign]

        result = asyncio.run(provider.probe_capabilities())

        self.assertEqual(result[1]["code"], "player_unavailable")

    def test_capability_probe_logs_player_rpc_failure_without_exposing_it(self) -> None:
        provider = CloudMusicProvider(command="uv.exe", directory="music")

        async def fake_probe(*, force: bool = False):
            return {"status": "healthy", "tools": ["cloud_music_search", "cloud_music_player_status"]}

        async def failing_player_status():
            raise CloudMusicError("player process not found")

        provider.probe = fake_probe  # type: ignore[method-assign]
        provider.player_status = failing_player_status  # type: ignore[method-assign]

        with mock.patch("agent_tools.netease_cloud.logger.exception") as log_exception:
            result = asyncio.run(provider.probe_capabilities())

        self.assertEqual(result[1]["code"], "player_unavailable")
        log_exception.assert_called_once_with("cloud music player capability probe failed")


if __name__ == "__main__":
    unittest.main()
