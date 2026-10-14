"""模型调用边界：核心包不认识 HTTP，也不认识任何模型厂商。

【在系统里的位置】
loop.py 只说"帮我跑一步模型决策"，具体走哪家网关、怎么拼 HTTP、怎么解析流，
全部由 agent_core_adapters/ 里的适配器实现。core 与 adapter 之间只靠这两个 Protocol 通信。

【为什么拆成两个 Protocol】
- LLMProvider：一次性拿完整回复（不需要边打字边看），实现简单。
- StreamingLLMProvider：边生成边回调 on_delta，让前端能逐字显示。
两者是**并列能力**，不是继承关系——一个适配器可以只实现前者，
调用方（loop.py）按能力选择路径。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol

from .contracts import ProviderResponse


class LLMProvider(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        """Run one provider decision step."""
        # 一次调用 = 一次"决策步"：模型要么直接给出回复，要么给出 tool_calls 要工具。
        # 多步循环由 loop.py 负责，provider 自己不循环。


# 流式回调：每收到一段可见文本就调一次，参数是增量片段（不是累积全文）。
StreamDeltaHandler = Callable[[str], Awaitable[None]]


class StreamingLLMProvider(Protocol):
    async def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        on_delta: StreamDeltaHandler | None = None,
    ) -> ProviderResponse:
        """Run one provider decision step and publish visible content chunks."""
        # 注意：返回值仍是完整的 ProviderResponse（含 usage），
        # on_delta 只是"过程中的旁路通知"，不替代最终结果。
