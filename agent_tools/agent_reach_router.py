"""受限的 Agent-Reach 体检适配器。

这里只读取 doctor 快照并选择零配置渠道；不会替用户登录或执行任意上游命令。

执行路径默认取 PATH 上的 `agent-reach`；本机没装或装在别处时，用 .env 里的
`AGENT_REACH_COMMAND` 指到实际可执行文件（绝对路径或带参数的命令行均可）。
体检失败一律按"没有可用渠道"降级，绝不阻断搜索本身。

【为什么必须有缓存，而且查询渠道绝不能等 doctor】
`agent-reach doctor --json` 要逐个渠道做网络体检，本机实测 30 秒以上；
而工具 handler 每次调用都会想问一次"bilibili 渠道可用吗"。如果内联等
doctor，等于每次搜索都先卡半分钟。所以：

- 默认 runner（生产）走**进程级共享缓存**（TTL 10 分钟）——多请求共用一次体检；
- 搜索路径一律用 `select_cached` / `available_platforms_cached`：有新鲜缓存就返回，
  没有就**后台起线程刷新**并立即返回 None（渠道状态未知），绝不阻塞搜索；
- 注入 runner 的测试用实例级缓存，互不污染。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

from agent_core.project_env import PROJECT_ENV_PATH

SAFE_PLATFORMS = frozenset({"web", "github", "rss", "youtube", "bilibili", "v2ex"})
DEFAULT_COMMAND = "agent-reach"
DOCTOR_TIMEOUT_SECONDS = 60.0
CACHE_TTL_SECONDS = 600.0

# 进程级共享缓存：command → (monotonic 时间戳, doctor 快照)。
# 只在默认 runner（生产路径）下启用；注入 runner 的测试实例走实例级缓存。
_SHARED_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_CACHE_LOCK = threading.Lock()
_REFRESHING: set[str] = set()


def _configured_command(env_path: str | Path = PROJECT_ENV_PATH) -> str:
    load_dotenv(dotenv_path=Path(env_path), override=False)
    value = os.environ.get("AGENT_REACH_COMMAND", "").strip()
    return value or DEFAULT_COMMAND


class AgentReachRouter:
    def __init__(self, runner: Callable[[list[str]], str] | None = None, *, command: str | None = None) -> None:
        self._runner = runner or self._run_doctor
        self._command = command or _configured_command()
        self._instance_cache: dict[str, tuple[float, dict[str, object]]] | None = None
        if runner is not None:
            self._instance_cache = {}
        self._snapshot: dict[str, object] | None = None

    # ── 命令拼装与执行 ──────────────────────────────────────────
    def _argv(self) -> list[str]:
        # 允许配置成带参数的命令行（例如 `python -m agent_reach`），shlex 拆开；
        # Windows 上反斜杠路径由 posix=False 语义保留。含空白才拆，否则整串
        # 当一个可执行名。
        value = self._command.strip()
        if not value:
            return [DEFAULT_COMMAND]
        if " " in value:
            return shlex.split(value, posix=False)
        return [value]

    @staticmethod
    def _run_doctor(command: list[str]) -> str:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCTOR_TIMEOUT_SECONDS, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "agent-reach doctor failed")
        return completed.stdout

    # ── 缓存读取与刷新 ──────────────────────────────────────────
    def _cache(self) -> dict[str, tuple[float, dict[str, object]]]:
        return self._instance_cache if self._instance_cache is not None else _SHARED_CACHE

    def _fresh_snapshot(self) -> dict[str, object] | None:
        entry = self._cache().get(self._command)
        if entry is None:
            return None
        timestamp, snapshot = entry
        if time.monotonic() - timestamp > CACHE_TTL_SECONDS:
            return None
        return snapshot

    def _fetch_and_cache(self) -> dict[str, object]:
        """真正跑一次 doctor 并写缓存；失败时保留旧值、不更新时间戳。"""
        try:
            raw = self._runner([*self._argv(), "doctor", "--json"])
            value = json.loads(raw)
            snapshot = value if isinstance(value, dict) else {}
        except Exception:
            return self._cache().get(self._command, (0.0, {}))[1]
        with _CACHE_LOCK:
            self._cache()[self._command] = (time.monotonic(), snapshot)
        return snapshot

    def _load(self) -> dict[str, object]:
        """阻塞式读取（测试与显式刷新用）：新鲜缓存优先，否则同步跑 doctor。"""
        fresh = self._fresh_snapshot()
        if fresh is not None:
            return fresh
        return self._fetch_and_cache()

    def _kick_background_refresh(self) -> None:
        key = self._command
        with _CACHE_LOCK:
            if key in _REFRESHING:
                return
            _REFRESHING.add(key)

        def refresh() -> None:
            try:
                self._fetch_and_cache()
            finally:
                with _CACHE_LOCK:
                    _REFRESHING.discard(key)

        threading.Thread(target=refresh, name="agent-reach-doctor", daemon=True).start()

    # ── 对外查询 ────────────────────────────────────────────────
    def available_platforms(self) -> tuple[str, ...]:
        """阻塞版：等 doctor 出结果。测试与运维诊断用，别放进搜索热路径。"""
        available: list[str] = []
        for platform, status in self._load().items():
            if platform not in SAFE_PLATFORMS or not isinstance(status, dict):
                continue
            if str(status.get("active_backend") or "").strip():
                available.append(platform)
        return tuple(sorted(available))

    def available_platforms_cached(self) -> tuple[str, ...] | None:
        """非阻塞版：新鲜缓存直接用；没有就后台刷新并返回 None（未知）。"""
        fresh = self._fresh_snapshot()
        if fresh is None:
            self._kick_background_refresh()
            return None
        return self._platforms_from(fresh)

    @staticmethod
    def _platforms_from(snapshot: dict[str, object]) -> tuple[str, ...]:
        available: list[str] = []
        for platform, status in snapshot.items():
            if platform not in SAFE_PLATFORMS or not isinstance(status, dict):
                continue
            if str(status.get("active_backend") or "").strip():
                available.append(platform)
        return tuple(sorted(available))

    def select(self, requested: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        available = set(self.available_platforms())
        return tuple(platform for platform in requested if platform in available and platform in SAFE_PLATFORMS)

    def select_cached(self, requested: tuple[str, ...] | list[str]) -> tuple[str, ...] | None:
        """非阻塞版 select：缓存未知时返回 None（调用方按"渠道状态未知"处理）。"""
        fresh = self._fresh_snapshot()
        if fresh is None:
            self._kick_background_refresh()
            return None
        available = set(self._platforms_from(fresh))
        return tuple(platform for platform in requested if platform in available and platform in SAFE_PLATFORMS)


__all__ = ["AgentReachRouter"]
