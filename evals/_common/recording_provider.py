"""RecordingProvider：包 OpenAICompatibleProvider，记录所有请求用于重放和断言。

从 evals/prompt_matrix/run_matrix.py 和 evals/web_workflow_matrix.py 抽出共用。
每条 LLM case 必须新建实例，不跨 case 复用（多轮内跨轮累积是预期，跨 case 不行）。
"""

from __future__ import annotations

from typing import Any


class RecordingProvider:
    """记录每次 complete 调用的 messages/tools/max_tokens/temperature，用于重放证据。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.requests: list[dict[str, Any]] = []

    @property
    def max_tokens(self) -> int:
        return self.inner.max_tokens

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.requests.append(
            {
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return await self.inner.complete(
            messages, tools=tools, max_tokens=max_tokens, temperature=temperature
        )
