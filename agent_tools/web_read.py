"""网页原文读取工具（一个文件 = 一个完整工具）。

阅读服务与 Agent-Reach 同款：Jina Reader（r.jina.ai），免费、无需 API Key；
若配置了 JINA_READER_API_KEY 则带上鉴权（配额更稳）。

职责边界：
- 只做"给定 URL → 标题 + 正文文本"的读取，不做任何检索；
- 内容截断到上限（默认 6000 字符），防止一次读取吃光回合上下文预算；
- 失败时如实报告获取状态与失败原因，不返回编造内容。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.project_env import PROJECT_ENV_PATH
from agent_core.tools import classify_tool_error

DEFAULT_READER_BASE = "https://r.jina.ai"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_CHARS = 6000

_ALLOWED_SCHEMES = ("http://", "https://")


def _load_reader_config(env_path: str | Path = PROJECT_ENV_PATH) -> tuple[str, str, int]:
    """读 (base_url, api_key, max_chars)。缺省值都可用，零配置即工作。"""
    load_dotenv(dotenv_path=Path(env_path), override=False)
    base_url = os.environ.get("WEB_READ_BASE_URL", DEFAULT_READER_BASE).strip().rstrip("/") or DEFAULT_READER_BASE
    api_key = os.environ.get("JINA_READER_API_KEY", "").strip()
    try:
        max_chars = int(os.environ.get("WEB_READ_MAX_CHARS", str(DEFAULT_MAX_CHARS)))
    except ValueError:
        max_chars = DEFAULT_MAX_CHARS
    return base_url, api_key, max(1000, min(20000, max_chars))


WEB_READ_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read",
        "description": (
            "读取网页原文：给定搜索结果里的 URL，返回页面标题和正文文本（已截断）。"
            "只有读到原文才算真正读过原文；读取失败时要说明，不能假装读过。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要读取的网页地址（http/https）。"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


def _validated_url(arguments: dict[str, Any]) -> str:
    url = str(arguments.get("url") or "").strip()
    if not url.lower().startswith(_ALLOWED_SCHEMES):
        raise ValueError("url is required and must start with http:// or https://")
    if len(url) > 2000:
        raise ValueError("url is too long")
    return url


def _detect_system_proxy() -> str:
    """返回读取服务应使用的代理地址（没有则空串）。

    阅读服务（r.jina.ai）在部分网络下必须走系统代理才可达，而 httpx 只认
    环境变量、不读 Windows 注册表代理。这里的优先级：
    1. WEB_READ_PROXY 显式指定（设为 direct/off/none 表示强制直连）；
    2. Windows 注册表里的系统代理（ProxyEnable + ProxyServer）；
    3. 都没有 → 直连。
    """
    override = os.environ.get("WEB_READ_PROXY", "").strip()
    if override:
        if override.lower() in {"direct", "off", "none"}:
            return ""
        return override if "://" in override else f"http://{override}"
    try:
        import winreg

        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        )
        enable, _ = winreg.QueryValueEx(key, "ProxyEnable")
        if not enable:
            return ""
        server, _ = winreg.QueryValueEx(key, "ProxyServer")
        server = str(server or "").strip()
        if not server:
            return ""
        if "=" in server or ";" in server:
            # 按协议分流的写法（http=...;https=...）：取 https 或 http 项。
            entries: dict[str, str] = {}
            for part in server.split(";"):
                if "=" in part:
                    scheme, address = part.split("=", 1)
                    entries[scheme.strip().lower()] = address.strip()
            server = entries.get("https") or entries.get("http") or ""
            if not server:
                return ""
        return server if "://" in server else f"http://{server}"
    except Exception:
        return ""


def _make_handler(
    base_url: str,
    api_key: str,
    max_chars: int,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
    proxy: str | None = None,
):
    def handle(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        started = time.monotonic()
        try:
            url = _validated_url(arguments)
        except ValueError as exc:
            return {"status": "failed", "error": f"invalid arguments: {exc}", "error_kind": "permanent"}
        headers = {"Accept": "text/plain"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # 显式注入 transport（测试/自定义通道）时不叠加代理，避免两者抢占优先级。
        proxy_address = proxy if proxy is not None else ("" if transport is not None else _detect_system_proxy())
        try:
            with httpx.Client(
                timeout=timeout, trust_env=False, transport=transport,
                follow_redirects=True, proxy=proxy_address or None,
            ) as client:
                response = client.get(f"{base_url}/{url}", headers=headers)
        except Exception as exc:
            return {
                "status": "failed",
                "error": f"web read provider error: {type(exc).__name__}: {exc}",
                "error_kind": classify_tool_error(exc),
                "output": {
                    "url": url,
                    "fetch_status": "failed",
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            }
        duration_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            reason = f"HTTP {response.status_code}"
            return {
                "status": "failed",
                "error": f"web read failed: {reason}",
                "error_kind": "transient" if response.status_code in (429, 500, 502, 503) else "permanent",
                "output": {
                    "url": url,
                    "fetch_status": "failed",
                    "failure_reason": reason,
                    "duration_ms": duration_ms,
                },
            }
        text = response.text or ""
        title = ""
        # Jina 返回的首行通常是 "Title: ..." 元信息，顺手取出来当标题。
        for line in text.splitlines()[:5]:
            stripped = line.strip()
            if stripped.lower().startswith("title:"):
                title = stripped[6:].strip()
                break
        content = text[:max_chars]
        truncated = len(text) > max_chars
        return {
            "status": "ok",
            "output": {
                "url": url,
                "fetch_status": "truncated" if truncated else "ok",
                "title": title,
                "content": content,
                "truncated": truncated,
                "duration_ms": duration_ms,
            },
        }

    return handle


def make_web_read_tool(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    max_chars: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    transport: httpx.BaseTransport | None = None,
    proxy: str | None = None,
    risk: str = "low",
) -> ToolSpec:
    if base_url is None or api_key is None or max_chars is None:
        env_base, env_key, env_max = _load_reader_config()
        base_url = base_url if base_url is not None else env_base
        api_key = api_key if api_key is not None else env_key
        max_chars = max_chars if max_chars is not None else env_max
    return ToolSpec(
        name="read",
        schema=WEB_READ_SCHEMA,
        handler=_make_handler(base_url, api_key, max_chars, timeout, transport, proxy),
        risk=risk,
        timeout_seconds=timeout + 10.0,
        effect=EFFECT_READ,
        deterministic_route_allowed=True,
    )
