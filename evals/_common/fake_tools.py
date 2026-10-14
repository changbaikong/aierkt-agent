"""参数化的假工具注册器：给 LLM evals 用受控假工具，避免真调外部服务。

从 evals/prompt_matrix/run_matrix.py 和 evals/web_workflow_matrix.py 抽出共用。
build_registry(executions, tools=...) 按需注册；executions 列表记录每次调用，供 ToolCallScorer 断言。

现有两套 evals 各自重复定义这套东西，这里统一后，套 5/6/7 直接复用。
"""

from __future__ import annotations

from typing import Any

from agent_core.contracts import Scope, ToolSpec
from agent_core.tools import ToolRegistry


def _make_search(executions: list[dict[str, Any]]):
    async def search(arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        executions.append({"tool": "search__web", "arguments": arguments})
        query = str(arguments.get("query") or "")
        return {
            "results": [
                {
                    "title": f"Controlled result for {query or 'query'}",
                    "url": "https://example.test/result",
                    "snippet": "矩阵测试用的受控结果。",
                }
            ]
        }

    return ToolSpec(
        "search__web",
        {
            "type": "function",
            "function": {
                "name": "search__web",
                "description": "Search the public web.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        },
        search,
    )


def _make_account_delete(executions: list[dict[str, Any]]):
    async def delete(arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        executions.append({"tool": "account__delete", "arguments": arguments})
        return {"deleted": True}

    return ToolSpec(
        "account__delete",
        {
            "type": "function",
            "function": {
                "name": "account__delete",
                "description": "Delete the user's account permanently.",
                "parameters": {
                    "type": "object",
                    "properties": {"account_id": {"type": "string"}},
                    "required": ["account_id"],
                },
            },
        },
        delete,
        risk="high",
    )


# 工具名 -> 工厂。新增工具时在这里加一条，build_registry 自动可用。
_TOOL_FACTORIES = {
    "search__web": _make_search,
    "account__delete": _make_account_delete,
}

DEFAULT_TOOLS = ("search__web", "account__delete")


def build_registry(
    executions: list[dict[str, Any]],
    *,
    tools: tuple[str, ...] = DEFAULT_TOOLS,
) -> ToolRegistry:
    """按 tools 列表注册受控假工具。executions 记录每次调用供 Scorer 断言。"""
    registry = ToolRegistry()
    for name in tools:
        factory = _TOOL_FACTORIES.get(name)
        if factory is None:
            raise ValueError(f"未知假工具：{name}（可用：{list(_TOOL_FACTORIES)}）")
        registry.register(factory(executions))
    return registry
