"""Bridge the Web Agent to the standalone NetEase Music MCP over stdio."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)


# 这两个默认值只是「本机常见位置」的猜测，任何人克隆后都必须改：
# 优先用环境变量 NETEASE_MCP_UV / NETEASE_MCP_DIRECTORY 覆盖（见文件末尾）。
DEFAULT_COMMAND = os.getenv("NETEASE_MCP_UV", "")
DEFAULT_DIRECTORY = os.getenv("NETEASE_MCP_DIRECTORY", "")
REQUIRED_MCP_TOOLS = frozenset({
    "cloud_music_status",
    "cloud_music_login",
    "cloud_music_search",
    "cloud_music_get_daily_recommend",
    "cloud_music_my_playlists",
    "cloud_music_playlist_detail",
    "cloud_music_create_playlist",
    "cloud_music_add_to_playlist",
    "cloud_music_album_info",
    "cloud_music_artist_info",
    "cloud_music_my_subscriptions",
    "cloud_music_play",
    "cloud_music_player_status",
    "cloud_music_next",
    "cloud_music_pause",
    "cloud_music_resume",
})


class CloudMusicError(RuntimeError):
    """A structured error returned by the external MCP server."""


class CloudMusicProvider:
    """Small async JSON-RPC client for the external stdio MCP process."""

    def __init__(
        self,
        *,
        command: str = DEFAULT_COMMAND,
        directory: str = DEFAULT_DIRECTORY,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.command = str(command)
        self.directory = str(directory)
        self.timeout_seconds = float(timeout_seconds)
        self._process: asyncio.subprocess.Process | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._available_tools: set[str] | None = None
        self._probe_at = 0.0
        self._last_error = ""

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
            await process.stdin.wait_closed()
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def status(self) -> dict[str, Any]:
        return await self._call("cloud_music_status", {})

    async def login(self, unikey: str | None = None) -> dict[str, Any]:
        arguments = {} if unikey is None else {"unikey": unikey}
        return await self._call("cloud_music_login", arguments)

    async def search(self, keyword: str, category: str = "song", limit: int = 5) -> list[dict[str, Any]]:
        return await self._call("cloud_music_search", {"keyword": keyword, "category": category, "limit": limit})

    async def daily_recommend(self) -> list[dict[str, Any]]:
        return await self._call("cloud_music_get_daily_recommend", {})

    async def my_playlists(self) -> list[dict[str, Any]]:
        return await self._call("cloud_music_my_playlists", {})

    async def playlist_detail(self, playlist_id: str | int) -> dict[str, Any]:
        return await self._call("cloud_music_playlist_detail", {"playlist_id": playlist_id})

    async def create_playlist(self, name: str, privacy: bool = False) -> dict[str, Any]:
        return await self._call("cloud_music_create_playlist", {"name": name, "privacy": privacy})

    async def add_to_playlist(self, playlist_id: str | int, track_ids: object) -> dict[str, Any]:
        return await self._call(
            "cloud_music_add_to_playlist", {"playlist_id": playlist_id, "track_ids": track_ids}
        )

    async def album_info(self, album_id: str | int) -> dict[str, Any]:
        return await self._call("cloud_music_album_info", {"album_id": album_id})

    async def artist_info(self, artist_id: str | int) -> dict[str, Any]:
        return await self._call("cloud_music_artist_info", {"artist_id": artist_id})

    async def my_subscriptions(self, category: str = "artists") -> list[dict[str, Any]]:
        return await self._call("cloud_music_my_subscriptions", {"category": category})

    async def play(self, id: str | int, type: str = "song") -> dict[str, Any]:
        return await self._call("cloud_music_play", {"id": id, "type": type})

    async def player_status(self) -> dict[str, Any]:
        return await self._call("cloud_music_player_status", {})

    async def next(self) -> dict[str, Any]:
        return await self._call("cloud_music_next", {})

    async def pause(self) -> dict[str, Any]:
        return await self._call("cloud_music_pause", {})

    async def resume(self) -> dict[str, Any]:
        return await self._call("cloud_music_resume", {})

    async def get_volume(self) -> dict[str, Any]:
        return await self._call("cloud_music_get_volume", {})

    async def set_volume(self, volume_percent: int) -> dict[str, Any]:
        return await self._call("cloud_music_set_volume", {"volume_percent": volume_percent})

    async def _call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        async with self._lock:
            await self._ensure_started()
            if self._available_tools is not None and tool_name not in self._available_tools:
                raise CloudMusicError(f"tool_missing: {tool_name}")
            response = await self._request(
                "tools/call", {"name": tool_name, "arguments": arguments}
            )
            result = response.get("result") or {}
            structured = result.get("structuredContent")
            if isinstance(structured, dict):
                envelope = structured
            else:
                envelope = self._content_json(result.get("content"))
            if envelope.get("ok") is False:
                error = envelope.get("error") or {}
                raise CloudMusicError(str(error.get("message") or "external music error"))
            return envelope.get("data")

    async def _refresh_tool_catalog(self) -> None:
        listed = await asyncio.wait_for(self._request("tools/list", {}), timeout=self.timeout_seconds)
        tools = ((listed.get("result") or {}).get("tools") or [])
        self._available_tools = {
            str(item.get("name"))
            for item in tools
            if isinstance(item, dict) and item.get("name")
        }
        self._probe_at = time.monotonic()

    async def _ensure_started(self, *, force: bool = False) -> None:
        if self._process is not None and self._process.returncode is None:
            if force:
                try:
                    await self._refresh_tool_catalog()
                    self._last_error = ""
                except Exception as exc:
                    self._last_error = "mcp_disconnected"
                    await self.close()
                    if isinstance(exc, CloudMusicError):
                        raise
                    raise CloudMusicError(f"mcp_disconnected: {type(exc).__name__}: {exc}") from exc
            return
        if not Path(self.command).exists():
            raise CloudMusicError(f"uv executable not found: {self.command}")
        if not Path(self.directory).is_dir():
            raise CloudMusicError(f"music MCP directory not found: {self.directory}")
        self._process = await asyncio.create_subprocess_exec(
            self.command,
            "run",
            "--locked",
            "--directory",
            self.directory,
            "python",
            "-m",
            "cloud_music_mcp",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(
                self._request(
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "self-built-agent", "version": "0.1"},
                    },
                ),
                timeout=self.timeout_seconds,
            )
            await asyncio.wait_for(self._notify("notifications/initialized", {}), timeout=self.timeout_seconds)
            await self._refresh_tool_catalog()
            self._last_error = ""
        except Exception as exc:
            self._last_error = "mcp_disconnected"
            await self.close()
            if isinstance(exc, CloudMusicError):
                raise
            raise CloudMusicError(f"mcp_disconnected: {type(exc).__name__}: {exc}") from exc

    async def probe(self, *, force: bool = False) -> dict[str, Any]:
        async with self._lock:
            if not force and self._available_tools is not None and time.monotonic() - self._probe_at < 10:
                return {"status": "healthy", "tools": sorted(self._available_tools), "cached": True}
            try:
                await self._ensure_started(force=force)
                missing = sorted(REQUIRED_MCP_TOOLS - (self._available_tools or set()))
                if missing:
                    return {"status": "degraded", "reason": "tool_missing", "missing": missing, "tools": sorted(self._available_tools or set())}
                return {"status": "healthy", "tools": sorted(self._available_tools or set()), "cached": False}
            except Exception as exc:
                logger.exception("cloud music MCP capability probe failed")
                return {"status": "unhealthy", "reason": "mcp_disconnected", "error": str(exc)}

    async def probe_capabilities(self, *, force: bool = False) -> list[dict[str, Any]]:
        """Probe MCP transport and player RPC without changing playback state."""
        mcp = await self.probe(force=force)
        if mcp.get("status") != "healthy":
            code = "mcp_tool_missing" if mcp.get("reason") == "tool_missing" else "mcp_disconnected"
            reason = "MCP is missing required tools" if code == "mcp_tool_missing" else "MCP is disconnected"
            return [
                {
                    "name": "mcp",
                    "status": str(mcp.get("status") or "unhealthy"),
                    "code": code,
                    "reason": reason,
                    "tools": list(mcp.get("tools") or []),
                },
                {
                    "name": "player",
                    "status": "unavailable",
                    "code": "mcp_dependency_unavailable",
                    "reason": "player probe requires a healthy MCP",
                },
            ]

        try:
            player = await asyncio.wait_for(self.player_status(), timeout=min(2.5, self.timeout_seconds))
        except Exception:
            logger.exception("cloud music player capability probe failed")
            return [
                {"name": "mcp", "status": "healthy", "code": "", "reason": "", "tools": list(mcp.get("tools") or [])},
                {
                    "name": "player",
                    "status": "unavailable",
                    "code": "player_unavailable",
                    "reason": "player status could not be read",
                },
            ]

        player_state = str(player.get("status") if isinstance(player, dict) else "").lower()
        if player_state in {"unavailable", "error", "failed"}:
            return [
                {"name": "mcp", "status": "healthy", "code": "", "reason": "", "tools": list(mcp.get("tools") or [])},
                {
                    "name": "player",
                    "status": "unavailable",
                    "code": "player_unavailable",
                    "reason": "player status could not be read",
                },
            ]
        return [
            {"name": "mcp", "status": "healthy", "code": "", "reason": "", "tools": list(mcp.get("tools") or [])},
            {"name": "player", "status": "healthy", "code": "", "reason": ""},
        ]

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise CloudMusicError("music MCP process is not running")
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode())
        await process.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise CloudMusicError("music MCP process is not running")
        self._request_id += 1
        request_id = self._request_id
        process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n").encode())
        await process.stdin.drain()
        while True:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=self.timeout_seconds)
            if not line:
                raise CloudMusicError("music MCP process exited unexpectedly")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if message.get("id") == request_id:
                if "error" in message:
                    raise CloudMusicError(str(message["error"]))
                return message

    @staticmethod
    def _content_json(content: Any) -> dict[str, Any]:
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    try:
                        value = json.loads(str(item.get("text", "")))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        return value
        return {"ok": False, "error": {"message": "invalid MCP response"}}


def build_cloud_music_provider() -> CloudMusicProvider:
    return CloudMusicProvider(
        command=os.getenv("NETEASE_MCP_UV", DEFAULT_COMMAND),
        directory=os.getenv("NETEASE_MCP_DIRECTORY", DEFAULT_DIRECTORY),
        timeout_seconds=float(os.getenv("NETEASE_MCP_TIMEOUT_SECONDS", "60")),
    )


__all__ = ["CloudMusicError", "CloudMusicProvider", "build_cloud_music_provider"]
