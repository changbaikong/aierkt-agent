"""B站搜索与视频详情读取工具（一个文件 = 一组完整工具）。

设计约束（与 web_search.py 同一取向）：
- 自包含：数据契约、HTTP 适配、schema、handler、工厂都在本模块；
- 只依赖 agent_core 的稳定公开契约（ToolSpec、Scope、classify_tool_error）与 httpx；
- 渠道检查（Agent-Reach doctor）只做"覆盖说明"，实际搜索与详情读取一律以
  本工具的真实调用结果为准 —— 渠道检查通过不等于搜索成功。

后端选择（2026-10-05 起）：
- 首选 `bili` 命令行（Agent-Reach 管理的 bilibili-cli，路径用 .env 的
  BILI_COMMAND 固定）——那是 Agent-Reach 官方安装、维护升级的后端；
- `bili` 未安装或调用失败时，降级到内置的 B站公开 Web API 直连
  （BilibiliWebProvider），返回里带 `backend` 字段明确标记用的是哪个后端。

失败三态（fetch_status / failure_reason 可区分）：
- `uninstalled`：bili 命令不存在（工具未安装）；
- `timeout` / `unreachable`：命令在但没跑成（渠道不可达）；
- `ok` 但 0 条结果：搜索无可靠结果。

无登录、无签名接口：第一版只做视频搜索与视频详情，只读标题、简介、
作者、发布时间等公开字段。不下载音视频，不读字幕。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from dotenv import load_dotenv

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.project_env import PROJECT_ENV_PATH
from agent_core.tools import classify_tool_error

DEFAULT_BASE_URL = "https://api.bilibili.com"
DEFAULT_TIMEOUT = 15.0
BILI_COMMAND_TIMEOUT = 30.0
MAX_RESULTS_MIN = 1
MAX_RESULTS_MAX = 20
MAX_RESULTS_DEFAULT = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class BilibiliVideo:
    """一条视频搜索结果或视频详情的最小字段集。"""

    title: str
    url: str
    bvid: str = ""
    description: str = ""
    author: str = ""
    published_at: str = ""
    play_count: int | None = None


def _strip_html(text: str) -> str:
    """搜索接口会在命中词外面包 <em class="keyword"> 高亮标签，剥掉再给模型。"""
    import re

    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


class BilibiliProvider(Protocol):
    """任何实现 search_videos() / video_detail() 的对象都可作为工具后端。

    实现需通过 ``backend`` 属性声明自己（"bili-cli" / "web-api-direct"），
    该值会进工具返回与日志，用于区分证据实际来源。
    """

    backend: str

    async def search_videos(self, query: str, *, max_results: int = MAX_RESULTS_DEFAULT) -> list[BilibiliVideo]: ...

    async def video_detail(self, bvid: str) -> BilibiliVideo | None: ...


class BilibiliCliUnavailable(Exception):
    """bili 命令不可用（未安装 / 配置路径不存在）。

    ``reason`` 取 "uninstalled" 或 "timeout"；适配层据此降级或如实上报。
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class _CliResult:
    """一次 bili 命令执行的结果。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload


def _configured_bili_command(env_path: str | Path = PROJECT_ENV_PATH) -> str:
    load_dotenv(dotenv_path=Path(env_path), override=False)
    value = os.environ.get("BILI_COMMAND", "").strip()
    return value or "bili"


def _bili_argv(command: str, arguments: list[str]) -> list[str]:
    """把配置的命令 + 参数拆成 argv（不用 shell，规避空格与中文用户名问题）。

    带空格的配置按命令行拆（posix=False 保留 Windows 路径反斜杠）；
    纯路径整串当一个可执行文件。
    """
    value = str(command or "").strip()
    if not value:
        raise BilibiliCliUnavailable("uninstalled", "BILI_COMMAND is empty")
    argv = shlex.split(value, posix=False) if " " in value else [value]
    return [arg.strip('"') for arg in argv] + list(arguments)


def _run_bili_command(command: str, arguments: list[str], *, timeout: float) -> dict[str, Any]:
    """同步执行一次 bili 命令并解析 JSON 输出。

    失败分三类（对应验收要求的三态）：
    - FileNotFoundError → BilibiliCliUnavailable("uninstalled")：工具未安装；
    - subprocess.TimeoutExpired → BilibiliCliUnavailable("timeout")：渠道卡死；
    - 退出码非 0 / JSON 解析失败 → RuntimeError：渠道不可达或输出异常。
    中文输出：子进程强制 PYTHONIOENCODING=utf-8（Windows 管道默认用本地
    代码页 cp936，会把中文标题写坏），读取也按 utf-8 容错解码。
    """
    argv = _bili_argv(command, arguments)
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        completed = subprocess.run(
            argv, capture_output=True, timeout=timeout, check=False, env=child_env,
        )
    except FileNotFoundError as exc:
        raise BilibiliCliUnavailable("uninstalled", f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise BilibiliCliUnavailable("timeout", f"bili command timed out after {timeout}s") from exc
    stdout = completed.stdout.decode("utf-8", errors="replace")
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"bili exited with code {completed.returncode}: {stderr[:300] or 'no stderr'}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bili output is not valid JSON: {stdout[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("bili output is not a JSON object")
    return payload


class BilibiliCliProvider:
    """Agent-Reach 管理的 bilibili-cli（`bili` 命令）适配器。

    handler 是同步签名，这里直接提供**同步**的 fetch 方法（search_json /
    video_json），由 handler 在线程池里调用 —— 与异步的直连后端
    （BilibiliWebProvider）通过 BilibiliBackendRouter 统一成一套协议。
    """

    backend = "bili-cli"

    def __init__(
        self,
        *,
        command: str | None = None,
        timeout: float = BILI_COMMAND_TIMEOUT,
        runner=None,
    ) -> None:
        self.command = command or _configured_bili_command()
        self.timeout = timeout
        self._runner = runner or _run_bili_command

    # ── 同步取数 ────────────────────────────────────────────────
    def search_json(self, query: str, *, max_results: int) -> dict[str, Any]:
        return self._runner(self.command, [
            "search", "--type", "video", "-n", str(max_results), "--json", query,
        ], timeout=self.timeout)

    def video_json(self, bvid: str) -> dict[str, Any]:
        return self._runner(self.command, ["video", "--json", bvid], timeout=self.timeout)

    # ── 异步协议适配（在线程池里跑同步命令）────────────────────
    async def search_videos(self, query: str, *, max_results: int = MAX_RESULTS_DEFAULT) -> list[BilibiliVideo]:
        import asyncio

        payload = await asyncio.to_thread(self.search_json, query, max_results=max_results)
        return self.parse_search(payload)[:max_results]

    async def video_detail(self, bvid: str) -> BilibiliVideo | None:
        import asyncio

        payload = await asyncio.to_thread(self.video_json, bvid)
        return self.parse_video(payload)

    # ── 纯解析（单测直接打这里）────────────────────────────────
    @staticmethod
    def _require_ok(payload: dict[str, Any]) -> dict[str, Any]:
        if not bool(payload.get("ok")):
            message = str(payload.get("error") or payload.get("message") or "bili reported failure")
            raise RuntimeError(f"bili reported failure: {message[:200]}")
        return payload

    @classmethod
    def parse_search(cls, payload: dict[str, Any]) -> list[BilibiliVideo]:
        data = cls._require_ok(payload).get("data")
        rows = data if isinstance(data, list) else []
        results: list[BilibiliVideo] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            bvid = str(row.get("bvid") or row.get("id") or "").strip()
            title = str(row.get("title") or "").strip()
            if not bvid:
                # 无 BV 号的结果无法溯源与后续读取，与直连后端同一口径：丢弃。
                continue
            url = f"https://www.bilibili.com/video/{bvid}" if bvid else ""
            results.append(BilibiliVideo(
                title=title,
                url=url,
                bvid=bvid,
                author=str(row.get("author") or "").strip(),
                play_count=int(row["play"]) if isinstance(row.get("play"), int) else None,
            ))
        return results

    @classmethod
    def parse_video(cls, payload: dict[str, Any]) -> BilibiliVideo | None:
        data = cls._require_ok(payload).get("data")
        video = data.get("video") if isinstance(data, dict) else None
        if not isinstance(video, dict):
            return None
        bvid = str(video.get("bvid") or video.get("id") or "").strip()
        owner = video.get("owner") if isinstance(video.get("owner"), dict) else {}
        return BilibiliVideo(
            title=str(video.get("title") or "").strip(),
            url=str(video.get("url") or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")).strip(),
            bvid=bvid,
            description=str(video.get("description") or "").strip(),
            author=str(owner.get("name") or "").strip(),
        )


class BilibiliWebProvider:
    """B站公开网页接口适配器（内置直连后端，作为 bili 命令的降级方案）。

    搜索接口要求请求带 buvid3 Cookie，否则返回 -412 风控错误。
    处理顺序：先访问一次 B站首页拿真实 Cookie；拿不到就自己合成一个
    合法形状的 buvid3（官方前端就是 UUID 形状）。两者都失败时把真实
    错误码如实报给上层，不伪装成"没有结果"。
    """

    backend = "web-api-direct"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
        }

    async def _ensure_cookies(self, client: httpx.AsyncClient) -> None:
        if "buvid3" in client.cookies:
            return
        try:
            await client.get("https://www.bilibili.com/", headers=self._headers())
        except Exception:
            pass
        if "buvid3" not in client.cookies:
            client.cookies.set("buvid3", f"{uuid.uuid4().hex[:8]}- {int(time.time() * 1000)}")

    @staticmethod
    def _video_from_search(item: dict[str, Any]) -> BilibiliVideo | None:
        bvid = str(item.get("bvid") or "").strip()
        url = str(item.get("arcurl") or "").strip() or (f"https://www.bilibili.com/video/{bvid}" if bvid else "")
        title = str(item.get("title") or "").strip()
        if not url:
            # 无链接的结果对 Agent 没用（后续读取与溯源都依赖 url），直接丢。
            return None
        pubdate = item.get("pubdate")
        published_at = ""
        if isinstance(pubdate, (int, float)) and pubdate > 0:
            from datetime import datetime, timezone

            published_at = datetime.fromtimestamp(pubdate, tz=timezone.utc).strftime("%Y-%m-%d")
        return BilibiliVideo(
            title=_strip_html(title),
            url=url,
            bvid=bvid,
            description=_strip_html(str(item.get("description") or "")),
            author=_strip_html(str(item.get("author") or "")),
            published_at=published_at,
            play_count=int(item["play"]) if isinstance(item.get("play"), int) else None,
        )

    async def search_videos(self, query: str, *, max_results: int = MAX_RESULTS_DEFAULT) -> list[BilibiliVideo]:
        async with httpx.AsyncClient(
            timeout=self.timeout, trust_env=False, transport=self.transport, follow_redirects=True,
        ) as client:
            await self._ensure_cookies(client)
            response = await client.get(
                f"{self.base_url}/x/web-interface/search/type",
                params={"search_type": "video", "keyword": query, "page": 1},
                headers=self._headers(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"bilibili search failed with HTTP {response.status_code}")
            data = response.json()
            code = int(data.get("code", -1))
            if code != 0:
                raise RuntimeError(f"bilibili search failed: code={code} message={data.get('message')}")
            body = data.get("data") or {}
            results: list[BilibiliVideo] = []
            for item in body.get("result") or []:
                if not isinstance(item, dict):
                    continue
                video = self._video_from_search(item)
                if video is not None:
                    results.append(video)
                if len(results) >= max_results:
                    break
            return results

    async def video_detail(self, bvid: str) -> BilibiliVideo | None:
        async with httpx.AsyncClient(
            timeout=self.timeout, trust_env=False, transport=self.transport, follow_redirects=True,
        ) as client:
            await self._ensure_cookies(client)
            response = await client.get(
                f"{self.base_url}/x/web-interface/view",
                params={"bvid": bvid},
                headers=self._headers(),
            )
            if response.status_code >= 400:
                raise RuntimeError(f"bilibili view failed with HTTP {response.status_code}")
            data = response.json()
            code = int(data.get("code", -1))
            if code != 0:
                raise RuntimeError(f"bilibili view failed: code={code} message={data.get('message')}")
            body = data.get("data") or {}
            if not isinstance(body, dict) or not str(body.get("bvid") or "").strip():
                return None
            stat = body.get("stat") if isinstance(body.get("stat"), dict) else {}
            from datetime import datetime, timezone

            pubdate = body.get("pubdate")
            published_at = (
                datetime.fromtimestamp(pubdate, tz=timezone.utc).strftime("%Y-%m-%d")
                if isinstance(pubdate, (int, float)) and pubdate > 0
                else ""
            )
            return BilibiliVideo(
                title=_strip_html(str(body.get("title") or "")),
                url=f"https://www.bilibili.com/video/{body.get('bvid')}",
                bvid=str(body.get("bvid") or "").strip(),
                description=_strip_html(str(body.get("desc") or "")),
                author=_strip_html(str((body.get("owner") or {}).get("name") or "")),
                published_at=published_at,
                play_count=int(stat["view"]) if isinstance(stat.get("view"), int) else None,
            )


class BilibiliBackendRouter:
    """后端路由：`bili` 命令（Agent-Reach 管理）优先，直连 Web API 降级。

    返回值统一带 ``meta``：``backend`` 标明证据实际来源（进工具输出与日志，
    不能把 doctor 通过当成搜索已接通），``degraded_reason`` 记录 CLI 被跳过
    的三态原因之一：uninstalled（未安装）/ timeout（命令超时）/
    unreachable（渠道不可达）。
    """

    def __init__(
        self,
        *,
        cli_provider: BilibiliCliProvider | None = None,
        fallback: BilibiliWebProvider | None = None,
        command: str | None = None,
        timeout: float = BILI_COMMAND_TIMEOUT,
    ) -> None:
        self._cli = cli_provider or BilibiliCliProvider(command=command, timeout=timeout)
        self._fallback = fallback or BilibiliWebProvider()

    async def _cli_search(self, query: str, max_results: int) -> list[BilibiliVideo]:
        return await self._cli.search_videos(query, max_results=max_results)

    async def search_meta(self, query: str, *, max_results: int = MAX_RESULTS_DEFAULT):
        try:
            videos = await self._cli_search(query, max_results)
            return videos, {"backend": self._cli.backend, "degraded_reason": None}
        except BilibiliCliUnavailable as exc:
            reason = exc.reason
        except Exception as exc:
            reason = f"unreachable: {type(exc).__name__}: {exc}"
        videos = await self._fallback.search_videos(query, max_results=max_results)
        return videos, {"backend": self._fallback.backend, "degraded_reason": reason}

    async def video_meta(self, bvid: str):
        try:
            video = await self._cli.video_detail(bvid)
            return video, {"backend": self._cli.backend, "degraded_reason": None}
        except BilibiliCliUnavailable as exc:
            reason = exc.reason
        except Exception as exc:
            reason = f"unreachable: {type(exc).__name__}: {exc}"
        video = await self._fallback.video_detail(bvid)
        return video, {"backend": self._fallback.backend, "degraded_reason": reason}


# ---------------------------------------------------------------------------
# schema + handler + 工厂
# ---------------------------------------------------------------------------
BILIBILI_SEARCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bilibili",
        "description": (
            "B站视频搜索：返回视频标题、简介、作者、发布时间和链接。"
            "适合查热梗、网络流行语和用户明确要求查B站的内容；搜索结果里的视频"
            "链接可以用 B站视频详情 进一步读取简介。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "简短关键词，不要把整段话原样放进参数。"},
                "max_results": {
                    "type": "integer",
                    "minimum": MAX_RESULTS_MIN,
                    "maximum": MAX_RESULTS_MAX,
                    "default": MAX_RESULTS_DEFAULT,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

BILIBILI_VIDEO_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "bilibili_video",
        "description": (
            "读取B站视频详情：返回标题、完整简介、作者和发布时间。"
            "参数传搜索结果里的 bvid 或视频链接。简介是作者的一句话介绍，"
            "不能当成视频内容的完整解释。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "video": {"type": "string", "description": "BV 号或视频页链接。"},
            },
            "required": ["video"],
            "additionalProperties": False,
        },
    },
}


def _validated_query(arguments: dict[str, Any]) -> tuple[str, int]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")
    query = query.strip()
    if len(query) > 200:
        raise ValueError("query must be at most 200 characters")
    max_results = arguments.get("max_results", MAX_RESULTS_DEFAULT)
    if not isinstance(max_results, bool) and isinstance(max_results, int):
        max_results = min(MAX_RESULTS_MAX, max(max_results, MAX_RESULTS_MIN))
    else:
        raise ValueError("max_results must be an integer")
    return query, max_results


def _extract_bvid(value: str) -> str:
    import re

    match = re.search(r"(BV[0-9A-Za-z]{8,12})", str(value or ""))
    return match.group(1) if match else ""


def _make_search_handler(provider: Any, channel_available=lambda: None):
    def handle(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        try:
            query, max_results = _validated_query(arguments)
        except ValueError as exc:
            return {"status": "failed", "error": f"invalid arguments: {exc}", "error_kind": "permanent"}

        async def run():
            search_meta = getattr(provider, "search_meta", None)
            if callable(search_meta):
                return await search_meta(query, max_results=max_results)
            return await provider.search_videos(query, max_results=max_results), {"backend": getattr(provider, "backend", "unknown"), "degraded_reason": None}

        started = time.monotonic()
        try:
            videos, meta = _run_async(run())
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"bilibili search provider error: {type(exc).__name__}: {exc}",
                "error_kind": classify_tool_error(exc),
                "output": {
                    "query": query,
                    "platform": "bilibili",
                    "backend": getattr(provider, "backend", "unknown"),
                    "channel_check": channel_available(),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                },
            }
        return {
            "status": "ok",
            "output": {
                "query": query,
                "platform": "bilibili",
                "backend": meta.get("backend"),
                "degraded_reason": meta.get("degraded_reason"),
                "fetch_status": "ok" if videos else "empty",
                "channel_check": channel_available(),
                "result_count": len(videos),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "results": [
                    {
                        "title": video.title,
                        "url": video.url,
                        "bvid": video.bvid,
                        "snippet": video.description,
                        "author": video.author,
                        "platform": "bilibili",
                        "published_at": video.published_at,
                        "play_count": video.play_count,
                    }
                    for video in videos
                ],
            },
        }

    return handle


def _make_video_handler(provider: Any, channel_available=lambda: None):
    def handle(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        video_ref = str(arguments.get("video") or "").strip()
        bvid = _extract_bvid(video_ref)
        if not bvid:
            return {"status": "failed", "error": "video is required and must contain a BV id", "error_kind": "permanent"}

        async def run():
            video_meta = getattr(provider, "video_meta", None)
            if callable(video_meta):
                return await video_meta(bvid)
            return await provider.video_detail(bvid), {"backend": getattr(provider, "backend", "unknown"), "degraded_reason": None}

        started = time.monotonic()
        try:
            video, meta = _run_async(run())
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"bilibili video provider error: {type(exc).__name__}: {exc}",
                "error_kind": classify_tool_error(exc),
                "output": {
                    "bvid": bvid,
                    "platform": "bilibili",
                    "backend": getattr(provider, "backend", "unknown"),
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                },
            }
        if video is None:
            return {
                "status": "ok",
                "output": {
                    "bvid": bvid,
                    "platform": "bilibili",
                    "backend": meta.get("backend"),
                    "degraded_reason": meta.get("degraded_reason"),
                    "fetch_status": "not_found",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            }
        return {
            "status": "ok",
            "output": {
                "bvid": bvid,
                "platform": "bilibili",
                "backend": meta.get("backend"),
                "degraded_reason": meta.get("degraded_reason"),
                "fetch_status": "ok",
                "duration_ms": int((time.monotonic() - started) * 1000),
                "title": video.title,
                "description": video.description,
                "author": video.author,
                "published_at": video.published_at,
                "url": video.url,
            },
        }

    return handle


def _run_async(awaitable):
    """在同步 handler 里跑一次 async 提供方调用。

    ToolSpec 的 handler 是同步签名，而 httpx 的异步客户端需要事件循环。
    这里每次新建一个独立循环（工具调用之间互不共享状态），拿完结果就关。
    """
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(awaitable)
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def build_bilibili_backend(
    *,
    command: str | None = None,
    fallback: BilibiliWebProvider | None = None,
    timeout: float = BILI_COMMAND_TIMEOUT,
) -> BilibiliBackendRouter:
    """装配入口：bili 命令（Agent-Reach 管理）优先，内置直连接口兜底。

    CLI 未安装或调用失败在**调用时**才暴露（BilibiliCliUnavailable /
    RuntimeError），路由器据此降级并在 meta 里如实标注原因；
    不在装配期做网络探测，保持模块构建便宜、无副作用。
    """
    return BilibiliBackendRouter(command=command, fallback=fallback, timeout=timeout)


def make_bilibili_search_tool(
    provider: Any | None = None,
    *,
    channel_available=None,
    risk: str = "low",
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> ToolSpec:
    return ToolSpec(
        name="bilibili",
        schema=BILIBILI_SEARCH_SCHEMA,
        handler=_make_search_handler(provider or build_bilibili_backend(), channel_available or (lambda: None)),
        risk=risk,
        timeout_seconds=max(timeout_seconds, 30.0),
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    )


def make_bilibili_video_tool(
    provider: Any | None = None,
    *,
    channel_available=None,
    risk: str = "low",
    timeout_seconds: float = DEFAULT_TIMEOUT,
) -> ToolSpec:
    return ToolSpec(
        name="bilibili_video",
        schema=BILIBILI_VIDEO_SCHEMA,
        handler=_make_video_handler(provider or build_bilibili_backend(), channel_available or (lambda: None)),
        risk=risk,
        timeout_seconds=max(timeout_seconds, 30.0),
        effect=EFFECT_READ,
    )
