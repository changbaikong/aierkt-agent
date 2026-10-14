from __future__ import annotations

import os
import json
from typing import Any

import httpx

from agent_core.contracts import ProviderResponse
from agent_core.project_env import load_project_env


class OpenAICompatibleProvider:
    @staticmethod
    def aggregate_stream_chunks(chunks: list[dict[str, Any]]) -> tuple[ProviderResponse, list[str]]:
        content_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        for chunk in chunks:
            usage.update(dict(chunk.get("usage") or {}))
            choices = chunk.get("choices") or []
            delta = choices[0].get("delta") if choices and isinstance(choices[0], dict) else {}
            delta = delta if isinstance(delta, dict) else {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                content_parts.append(content)
            for raw in delta.get("tool_calls") or []:
                if not isinstance(raw, dict):
                    continue
                index = int(raw.get("index", 0))
                target = calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                target["id"] += str(raw.get("id") or "")
                function = raw.get("function") or {}
                target["function"]["name"] += str(function.get("name") or "")
                target["function"]["arguments"] += str(function.get("arguments") or "")
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        return ProviderResponse(message=message, usage=usage), content_parts

    @staticmethod
    def visible_stream_deltas(response: ProviderResponse, deltas: list[str]) -> list[str]:
        return [] if response.message.get("tool_calls") else deltas

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_token: int = 2048,
        timeout: float = 60.0,
        thinking_enabled: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_token = max_token
        self.timeout = timeout
        self.thinking_enabled = bool(thinking_enabled)

    @classmethod
    def from_env(cls, *, thinking_enabled: bool = False) -> "OpenAICompatibleProvider":
        # 懒加载：构造时不校验配置，真正发请求（complete）时才检查。
        # 这样没配对话模型也能启动（只跑记忆/监视器等其它功能），
        # 缺变量报错由 loop 兜住变成一条 failed 回复，不挡主流程。
        load_project_env()
        return cls(
            base_url=os.environ.get("GOOGLE_GEMINI_BASE_URL", "").strip(),
            api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
            model=os.environ.get("GEMINI_MODEL", "").strip(),
            thinking_enabled=thinking_enabled,
        )

    def _ensure_configured(self) -> None:
        """发请求前校验三项对话配置，缺哪个报哪个。"""
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is missing")
        if not self.base_url:
            raise RuntimeError("GOOGLE_GEMINI_BASE_URL is missing")
        if not self.model:
            raise RuntimeError("GEMINI_MODEL is missing")

    @property
    def max_tokens(self) -> int:
        return self.max_token

    async def complete(
        self,
        message: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        self._ensure_configured()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": message,
            "max_tokens": max_tokens or self.max_token,
            "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )

        if response.status_code >= 400:
            try:
                details = response.json()
            except ValueError:
                details = response.text
            raise RuntimeError(
                f"LLM request failed with HTTP {response.status_code}: {details}"
            )

        data = response.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("LLM response has no choices")

        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError("LLM response has no valid message")

        return ProviderResponse(
            message=message,
            usage=dict(data.get("usage") or {}),
        )

    async def stream_complete(
        self,
        message: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: Any = None,
    ) -> ProviderResponse:
        self._ensure_configured()
        payload: dict[str, Any] = {
            "model": self.model, "messages": message, "max_tokens": max_tokens or self.max_token,
            "thinking": {"type": "enabled" if self.thinking_enabled else "disabled"},
            "stream": True, "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if tools:
            payload["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        chunks: list[dict[str, Any]] = []
        stream_mode = ""
        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            async with client.stream("POST", f"{self.base_url}/chat/completions", json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise RuntimeError(f"LLM request failed with HTTP {response.status_code}: {await response.aread()}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    chunk = json.loads(raw)
                    chunks.append(chunk)
                    choices = chunk.get("choices") or []
                    delta = choices[0].get("delta") if choices else {}
                    if isinstance(delta, dict) and delta.get("tool_calls"):
                        stream_mode = "tool"
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if content and stream_mode != "tool":
                        stream_mode = "content"
                        if on_delta is not None:
                            await on_delta(str(content))
        aggregated, deltas = self.aggregate_stream_chunks(chunks)
        return aggregated
