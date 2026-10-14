"""Adapter for the existing OpenAI-compatible backend LLM client."""

from __future__ import annotations

from typing import Any

from agent_core.contracts import ProviderResponse


class LLMClientProvider:
    """Expose only ``complete`` from the legacy client to the core loop."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    @property
    def max_tokens(self) -> int:
        try:
            return int(self.llm.max_tokens)
        except (AttributeError, TypeError, ValueError):
            return 2048

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        response = await self.llm.complete(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return ProviderResponse(
            message=dict(response.get("message") or {}),
            usage=dict(response.get("usage") or {}),
        )
