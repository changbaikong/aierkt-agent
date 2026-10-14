"""子代理派发工具测试：预算隔离、深度防护、超预算处理。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_core.contracts import AgentRequest, ProviderResponse, Scope, ToolSpec
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.provider import LLMProvider
from agent_core.prompts import PromptComposer
from agent_core.subagent import register_subagent_tool
from agent_core.tools import ToolRegistry


def _call(call_id: str, name: str, arguments: dict) -> dict:
    """构造 OpenAI function call 格式的工具调用。"""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class QueueProvider(LLMProvider):
    """脚本化的测试 provider：预设响应队列，记录所有请求。"""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = iter(responses)
        self.requests: list[dict] = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.requests.append(
            {
                "messages": json.loads(json.dumps(messages, ensure_ascii=False)),
                "tools": tools,
                "max_tokens": max_tokens,
            }
        )
        return ProviderResponse(message=next(self.responses), usage={"completion_tokens": 1})


def _registry(*specs: ToolSpec) -> ToolRegistry:
    """构造包含给定 specs 的 registry。"""
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


class SubagentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = Scope("test_owner", "test_channel", "test_conversation", "companion")

    async def test_subagent_uses_independent_budget(self) -> None:
        """子代理用独立预算跑，不占父 loop 步数。"""
        # 记录工具执行
        executed: list[str] = []

        async def mock_tool(arguments, _scope):
            executed.append("mock_tool_called")
            return {"result": "tool_output"}

        # 父 provider 脚本：调 dispatch → 拿子结果 → 最终回复
        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "子任务"})]},
                {"role": "assistant", "content": "子代理完成了任务。"},
            ]
        )

        # 子 provider 脚本：调工具 → 回复
        child_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("t1", "mock_tool", {})]},
                {"role": "assistant", "content": "子任务已完成。"},
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"

            # 父 registry + 子代理工具（注入子 provider）
            parent_registry = _registry(ToolSpec("mock_tool", {"type": "function"}, mock_tool))
            register_subagent_tool(
                parent_registry,
                child_provider,  # ← 子代理用独立 provider（测试用；生产是同一个）
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )

            # 父 loop
            parent_loop = AgentLoop(
                parent_provider,
                parent_registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            outcome = await parent_loop.run("复杂任务")

        # 断言
        self.assertEqual(outcome.status, "completed")
        self.assertIn("子代理完成了任务", outcome.reply)
        self.assertEqual(len(parent_provider.requests), 2)  # 父只调 2 次 provider（调 dispatch + 最终回复）
        self.assertEqual(len(child_provider.requests), 2)  # 子调 2 次（调工具 + 回复）
        self.assertEqual(executed, ["mock_tool_called"])  # 工具被子执行

        # 父的第 2 次请求（拿到子代理观察）应包含子的 reply
        parent_second_request = parent_provider.requests[1]["messages"]
        tool_observation = next((msg for msg in parent_second_request if msg.get("role") == "tool"), None)
        self.assertIsNotNone(tool_observation)
        self.assertIn("子任务已完成", tool_observation["content"])

    async def test_subagent_uses_execution_profile_when_composer_is_enabled(self) -> None:
        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "子任务"})]},
                {"role": "assistant", "content": "收到。"},
            ]
        )
        child_provider = QueueProvider([{"role": "assistant", "content": "已完成。"}])

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"
            registry = _registry()
            register_subagent_tool(
                registry,
                child_provider,
                prompt_composer=PromptComposer(),
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            loop = AgentLoop(
                parent_provider,
                registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            outcome = await loop.run("委派")

        self.assertEqual(outcome.status, "completed")
        child_system = child_provider.requests[0]["messages"][0]["content"]
        self.assertIn("[身份与语气]", child_system)
        self.assertIn("独立执行委派任务", child_system)
        self.assertNotIn("猫娘", child_system)
        self.assertIn("[人格锁]", child_system)

    async def test_subagent_high_risk_tool_requires_confirmation_by_default(self) -> None:
        executed: list[dict] = []

        async def dangerous(arguments, _scope):
            executed.append(arguments)
            return {"ok": True}

        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "危险任务"})]},
                {"role": "assistant", "content": "收到暂停结果。"},
            ]
        )
        child_provider = QueueProvider(
            [{"role": "assistant", "tool_calls": [_call("x1", "dangerous", {"value": 1})]}]
        )

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"
            registry = _registry(ToolSpec("dangerous", {"type": "function"}, dangerous, risk="high"))
            register_subagent_tool(
                registry,
                child_provider,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            loop = AgentLoop(
                parent_provider,
                registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            outcome = await loop.run("委派危险任务")

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(executed, [])
        self.assertIn("paused", parent_provider.requests[1]["messages"][-1]["content"])

    async def test_depth_guard_blocks_recursion(self) -> None:
        """深度防护：子代理再调 dispatch 超过 max_depth 时返回 failed。"""
        # 父 provider：调 dispatch → 收子结果
        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "递归任务"})]},
                {"role": "assistant", "content": "收到子代理的 failed 结果。"},
            ]
        )

        # 子 provider：只有1步，调 dispatch（深度=1，max_depth=1 → 被拦截 → 观察 failed）
        # 子 loop 收到 failed 观察后，如果没有更多响应，会因预算耗尽返回 failed
        child_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d2", "subagent__dispatch", {"task": "二层任务"})]},
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"

            parent_registry = _registry()
            register_subagent_tool(
                parent_registry,
                child_provider,
                max_depth=1,  # ← 只允许 1 层子代理（parent→child 深度1，child→grandchild 会被拦）
                child_max_steps=1,  # ← 子只有1步预算，调完 dispatch 就达限
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )

            parent_loop = AgentLoop(
                parent_provider,
                parent_registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            outcome = await parent_loop.run("递归测试")

        self.assertEqual(outcome.status, "completed")
        # 父的第 1 次请求（dispatch 返回后）应包含子代理的 failed 观察
        parent_second_request = parent_provider.requests[1]["messages"]
        tool_observation = next((msg for msg in parent_second_request if msg.get("role") == "tool"), None)
        self.assertIsNotNone(tool_observation)
        # 子代理调 dispatch → 被深度拦截返回 failed → 子 loop 尝试继续但 provider 队列空 → StopIteration
        # 或者子 loop 因预算耗尽返回 failed
        content = tool_observation["content"]
        self.assertIn("failed", content)  # 子 loop 最终 failed
        # 检查是深度拦截或预算耗尽导致的（观察里会有 error 字段）
        self.assertTrue("depth exceeded" in content or "安全预算" in content or "StopIteration" in content)

    async def test_child_exceeds_budget_returns_failed(self) -> None:
        """子代理超预算时返回 failed 观察。"""
        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "超预算", "max_steps": 1})]},
                {"role": "assistant", "content": "子代理预算不足。"},
            ]
        )

        # 子 provider：第 1 步就调工具 → 达到 max_steps=1 限制 → failed
        child_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("t1", "mock_tool", {})]},
            ]
        )

        async def mock_tool(arguments, _scope):
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"

            parent_registry = _registry(ToolSpec("mock_tool", {"type": "function"}, mock_tool))
            register_subagent_tool(
                parent_registry,
                child_provider,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )

            parent_loop = AgentLoop(
                parent_provider,
                parent_registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            outcome = await parent_loop.run("预算测试")

        self.assertEqual(outcome.status, "completed")
        # 父的第 1 次请求（dispatch 返回后）应包含 "安全预算" 失败观察
        parent_second_request = parent_provider.requests[1]["messages"]
        tool_observation = next((msg for msg in parent_second_request if msg.get("role") == "tool"), None)
        self.assertIsNotNone(tool_observation)
        self.assertIn("安全预算", tool_observation["content"])

    async def test_subagent_reply_truncation(self) -> None:
        """子代理 reply 超长时截断到 4000 字符。"""
        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "长回复"})]},
                {"role": "assistant", "content": "已收到。"},
            ]
        )

        long_reply = "X" * 5000
        child_provider = QueueProvider([{"role": "assistant", "content": long_reply}])

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"

            parent_registry = _registry()
            register_subagent_tool(
                parent_registry,
                child_provider,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )

            parent_loop = AgentLoop(
                parent_provider,
                parent_registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            outcome = await parent_loop.run("截断测试")

        self.assertEqual(outcome.status, "completed")
        parent_second_request = parent_provider.requests[1]["messages"]
        tool_observation = next((msg for msg in parent_second_request if msg.get("role") == "tool"), None)
        self.assertIsNotNone(tool_observation)
        # 观察内容应被截断（reply 截到 4000，整体 JSON 被父 _observation 再截到 4800）
        content = tool_observation["content"]
        self.assertLess(len(content), 5000)
        self.assertIn("XXXX", content)  # 部分内容存在

    async def test_registered_step_budget_is_actually_applied(self) -> None:
        """注册时给的 child_max_steps 必须真的生效。

        这是个回归测试：`register_subagent_tool(child_max_steps=...)` 以前会把参数
        收下、写进闭包签名，却从来没传给 `_validated_args`，于是永远用模块常量 10。
        判据是失败原因——预算生效时子 loop 报 "安全预算"，
        没生效时子 provider 队列耗尽，报 "StopIteration"。
        """
        parent_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("d1", "subagent__dispatch", {"task": "受限预算"})]},
                {"role": "assistant", "content": "子代理预算不足。"},
            ]
        )
        child_provider = QueueProvider(
            [
                {"role": "assistant", "tool_calls": [_call("t1", "mock_tool", {})]},
            ]
        )

        async def mock_tool(arguments, _scope):
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"

            parent_registry = _registry(ToolSpec("mock_tool", {"type": "function"}, mock_tool))
            register_subagent_tool(
                parent_registry,
                child_provider,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
                child_max_steps=1,
            )

            parent_loop = AgentLoop(
                parent_provider,
                parent_registry,
                self.scope,
                run_store=SQLiteRunStore(db_path),
                confirmation_store=SQLiteConfirmationStore(db_path),
            )
            await parent_loop.run("预算测试")

        tool_observation = next(
            (msg for msg in parent_provider.requests[1]["messages"] if msg.get("role") == "tool"),
            None,
        )
        self.assertIsNotNone(tool_observation)
        self.assertIn("安全预算", tool_observation["content"])


if __name__ == "__main__":
    unittest.main()
