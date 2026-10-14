"""子代理的工具视野必须比主 Chat 窄。

【为什么要单独一份注册表】

2026-09-22 之前，`_dispatch` 把**父 registry 那个对象本身**传给了子 loop：

    loop = AgentLoop(..., registry=registry, ...)          # subagent.py:192
    handler 里闭包捕获的也是同一个 registry                  # subagent.py:319

后果是子代理看到的工具和主 Chat 一模一样，**包括 `subagent__dispatch` 自己**。
于是"子代理再派一个子代理"是模型能**看见、并且真的会去调**的，
挡它的只有 `max_depth` 那一刀 —— 而且是在跑起来之后才挡。

看得见但跑不动比看不见更糟：模型会一轮一轮地试着调它，每一轮都撞墙，
把子代理的步数预算（默认 10 步）烧在明知不可能成功的事情上。

【这个文件守两条】

1. 子代理那份注册表里没有 `subagent__dispatch`，且不是父对象本身；
2. 真跑一次委派，交给子模型的工具清单里也不许出现它 ——
   第 1 条测的是数据，第 2 条测的是"这份数据真的被用上了"。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_core.contracts import ProviderResponse, Scope, ToolSpec
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.subagent import child_tool_registry, register_subagent_tool
from agent_core.tools import ToolRegistry


class RecordingProvider(LLMProvider):
    """把每次请求里的工具清单记下来。

    必须继承 `LLMProvider`：子 loop 按协议挑调用方式，一个"看起来有 complete 方法"
    的鸭子类型不会被当成 provider，症状是子代理一次模型请求都不发、
    直接回一句兜底话 —— 看着像"委派没生效"，其实是替身没被认出来。
    """

    def __init__(self) -> None:
        self.tool_names: list[list[str]] = []
        self._replies = iter(
            [
                {"role": "assistant", "tool_calls": [_call("x1", "safe__echo", {"text": "hi"})]},
                {"role": "assistant", "content": "子代理做完了。"},
            ]
        )

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.tool_names.append([item["function"]["name"] for item in (tools or [])])
        return ProviderResponse(message=next(self._replies), usage={"completion_tokens": 1})


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }


def _echo(_arguments: dict, _scope: Scope) -> dict:
    return {"ok": True}


async def test_the_child_registry_hides_dispatch_and_is_not_the_parent_object() -> None:
    """子代理那份表里没有派发工具，而且是**另一张表**，不是父对象本身。"""
    registry = ToolRegistry()
    register_subagent_tool(registry, RecordingProvider())
    registry.register(ToolSpec("safe__echo", _schema("safe__echo"), _echo))

    child = child_tool_registry(registry)

    assert child is not registry
    assert child.resolve("subagent__dispatch") is None
    assert child.resolve("safe__echo") is not None
    # 反过来必须还在：子代理看不见，不代表主 Chat 也看不见。
    assert registry.resolve("subagent__dispatch") is not None


async def test_the_child_model_never_receives_the_dispatch_tool() -> None:
    """真跑一次委派：交给子模型的工具清单里不许出现 `subagent__dispatch`。"""
    child_provider = RecordingProvider()

    class ParentProvider(LLMProvider):
        def __init__(self) -> None:
            self._replies = iter(
                [
                    # 任务文本特意避开"查一下/搜索"这类词：它们会命中子 loop 的
                    # 确定性路由，模型根本不会被叫到 —— 那测的就不是工具清单了。
                    {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "核对这批结论"})]},
                    {"role": "assistant", "content": "好。"},
                ]
            )

        async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
            return ProviderResponse(message=next(self._replies), usage={"completion_tokens": 1})

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "agent.db"
        registry = ToolRegistry()
        register_subagent_tool(
            registry,
            child_provider,
            run_store=SQLiteRunStore(db_path),
            confirmation_store=SQLiteConfirmationStore(db_path),
        )
        registry.register(ToolSpec("safe__echo", _schema("safe__echo"), _echo))

        loop = AgentLoop(
            ParentProvider(),
            registry,
            Scope("owner", "desktop", "chat", "companion"),
            run_store=SQLiteRunStore(db_path),
            confirmation_store=SQLiteConfirmationStore(db_path),
        )
        await loop.run("派一个子代理去查")

    assert child_provider.tool_names, "子代理一次模型请求都没发"
    for names in child_provider.tool_names:
        assert "subagent__dispatch" not in names, names
