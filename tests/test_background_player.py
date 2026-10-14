from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from agent_core.contracts import Scope
from agent_tools.background_player import hide_netease_window
from agent_tools.modules import build_netease_module

SCOPE = Scope("owner", "desktop", "chat", "netease")


def _run(coro):
    return asyncio.run(coro)


class FakeProvider:
    """只实现了 play 的假 provider，避免拉起真实 MCP/网易云。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls: list[tuple] = []
        self.error = error

    async def play(self, id, type: str = "song"):
        self.calls.append((id, type))
        if self.error is not None:
            raise self.error
        return {"ok": True, "status": "playing"}


def _play_tool(provider: object):
    module = build_netease_module(provider)
    return next(tool for tool in module.build_tools() if tool.name == "play")


class HideNeteaseWindowTests(unittest.TestCase):
    def test_runs_requested_attempts_and_returns_executed_count(self) -> None:
        calls: list[str] = []

        async def fake_runner(script: str) -> str:
            calls.append(script)
            return ""

        count = _run(hide_netease_window(attempts=3, interval_seconds=0, runner=fake_runner))

        self.assertEqual(count, 3)
        self.assertEqual(len(calls), 3)
        self.assertIn("ShowWindowAsync", calls[0])
        self.assertIn("cloudmusic", calls[0])
        # 用的是 SW_HIDE=0，而不是最小化
        self.assertIn("ShowWindowAsync($_.MainWindowHandle, 0)", calls[0])

    def test_retries_transient_failures_and_counts_only_successes(self) -> None:
        calls: list[str] = []
        failures_left = 2

        async def fake_runner(script: str) -> str:
            nonlocal failures_left
            calls.append(script)
            if failures_left > 0:
                failures_left -= 1
                raise RuntimeError("powershell unavailable")
            return ""

        count = _run(hide_netease_window(attempts=5, interval_seconds=0, runner=fake_runner))

        self.assertEqual(count, 3)
        self.assertEqual(len(calls), 5)

    def test_clamps_to_at_least_one_attempt(self) -> None:
        calls: list[str] = []

        async def fake_runner(script: str) -> str:
            calls.append(script)
            return ""

        count = _run(hide_netease_window(attempts=0, interval_seconds=0, runner=fake_runner))

        self.assertEqual(count, 1)
        self.assertEqual(len(calls), 1)


class CloudPlayHandlerTests(unittest.TestCase):
    def test_play_then_hides_window(self) -> None:
        provider = FakeProvider()
        tool = _play_tool(provider)

        with mock.patch(
            "agent_tools.modules.hide_netease_window", new=mock.AsyncMock(return_value=4)
        ) as hide:
            result = _run(tool.handler({"id": 66823}, SCOPE))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.calls, [(66823, "song")])
        hide.assert_awaited_once()

    def test_passes_playlist_type_through(self) -> None:
        provider = FakeProvider()
        tool = _play_tool(provider)

        with mock.patch("agent_tools.modules.hide_netease_window", new=mock.AsyncMock()):
            result = _run(tool.handler({"id": "123", "type": "playlist"}, SCOPE))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.calls, [("123", "playlist")])

    def test_play_failure_returns_failed_without_hide(self) -> None:
        provider = FakeProvider(error=RuntimeError("login required"))
        tool = _play_tool(provider)

        with mock.patch("agent_tools.modules.hide_netease_window", new=mock.AsyncMock()) as hide:
            result = _run(tool.handler({"id": 66823}, SCOPE))

        self.assertEqual(result["status"], "failed")
        self.assertIn("netease cloud provider error", result["error"])
        hide.assert_not_awaited()

    def test_hide_failure_does_not_fail_playback(self) -> None:
        provider = FakeProvider()
        tool = _play_tool(provider)

        with mock.patch(
            "agent_tools.modules.hide_netease_window",
            new=mock.AsyncMock(side_effect=RuntimeError("no window")),
        ) as hide:
            result = _run(tool.handler({"id": 66823}, SCOPE))

        self.assertEqual(result["status"], "ok")
        hide.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()