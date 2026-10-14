"""Behavioral acceptance tests for the unified native AgentLoop."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from backend.agent_loop import AgentLoop, ToolDefinition
from backend.llm.client import LLMClient
from backend.memory import MemoryScope


def _response(*, calls=None, content=None):
    message = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}], "usage": {"total_tokens": 10}}


def _call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_schema_dependent_chain_and_clean_history(self):
        responses = iter([
            _response(calls=[_call("u1", "find_user", {"name": "小明"})]),
            _response(calls=[_call("o1", "find_order", {"user_id": "U42"})]),
            _response(content="订单 O99 已发货。"),
        ])
        requests = []
        executed = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                self.assertIn("tools", body)
                self.assertEqual(body["tool_choice"], "auto")
            if len(requests) == 2:
                self.assertIn("U42", body["messages"][-1]["content"])
            return httpx.Response(200, json=next(responses))

        async def execute(arguments):
            executed.append(arguments)
            return {"user_id": "U42"} if len(executed) == 1 else {"order_id": "O99"}

        llm = LLMClient("system")
        llm._client = lambda timeout=30.0: httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        schema = {"type": "function", "function": {"name": "find_user", "parameters": {"type": "object"}}}
        order_schema = {"type": "function", "function": {"name": "find_order", "parameters": {"type": "object"}}}
        with tempfile.TemporaryDirectory() as directory:
            loop = AgentLoop(
                llm,
                [
                    ToolDefinition("find_user", schema, execute),
                    ToolDefinition("find_order", order_schema, execute),
                ],
                MemoryScope("test-user", "desktop", "test-conversation"),
                db_path=Path(directory) / "agent.db",
            )
            outcome = await loop.run("查小明的订单")

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(executed[1], {"user_id": "U42"})
        self.assertEqual([message["role"] for message in llm.messages], ["system", "user", "assistant"])

    async def test_error_is_observation_and_model_can_retry(self):
        responses = iter([
            _response(calls=[_call("s1", "search", {"query": "坏关键词"})]),
            _response(calls=[_call("s2", "search", {"query": "有效关键词"})]),
            _response(content="找到结果了。"),
        ])
        queries = []

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if len(queries) == 1:
                self.assertIn("ValueError", body["messages"][-1]["content"])
            return httpx.Response(200, json=next(responses))

        async def execute(arguments):
            queries.append(arguments["query"])
            if len(queries) == 1:
                raise ValueError("空结果")
            return {"result": "found"}

        llm = LLMClient("system")
        llm._client = lambda timeout=30.0: httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        schema = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
        with tempfile.TemporaryDirectory() as directory:
            loop = AgentLoop(
                llm,
                [ToolDefinition("search", schema, execute)],
                MemoryScope("test-user", "desktop", "test-retry"),
                db_path=Path(directory) / "agent.db",
            )
            outcome = await loop.run("搜索")

        self.assertEqual(outcome.reply, "找到结果了。")
        self.assertEqual(queries, ["坏关键词", "有效关键词"])

    async def test_internal_protocol_is_rewritten_before_delivery(self):
        responses = iter([
            _response(content='{"tool_calls": [{"function": {"name": "search"}}]}'),
            _response(content="这是给用户看的结果。"),
        ])

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=next(responses))

        llm = LLMClient("system")
        llm._client = lambda timeout=30.0: httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        schema = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
        with tempfile.TemporaryDirectory() as directory:
            loop = AgentLoop(
                llm,
                [ToolDefinition("search", schema, lambda arguments: {"result": "ok"})],
                MemoryScope("test-user", "desktop", "test-clean"),
                db_path=Path(directory) / "agent.db",
            )
            outcome = await loop.run("搜索")

        self.assertEqual(outcome.reply, "这是给用户看的结果。")
        self.assertNotIn("tool_calls", outcome.reply)

    async def test_high_risk_action_is_durable_pause_and_resume(self):
        first_response = iter([_response(calls=[_call("save-1", "save", {"fact": "x"})])])
        second_response = iter([_response(content="已保存。")])
        executions = []

        async def save(arguments):
            executions.append(arguments)
            return {"saved": True}

        async def first_handler(request):
            return httpx.Response(200, json=next(first_response))

        async def second_handler(request):
            return httpx.Response(200, json=next(second_response))

        schema = {"type": "function", "function": {"name": "save", "parameters": {"type": "object"}}}
        definition = ToolDefinition("save", schema, save, risk="high")
        scope = MemoryScope("test-user", "desktop", "test-hitl")
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"
            first_llm = LLMClient("system")
            first_llm._client = lambda timeout=30.0: httpx.AsyncClient(
                base_url="http://test", transport=httpx.MockTransport(first_handler)
            )
            first = AgentLoop(first_llm, [definition], scope, db_path=db_path)
            paused = await first.run("记住这个")
            self.assertEqual(paused.status, "paused")
            self.assertFalse(executions)

            resumed_llm = LLMClient("system")
            resumed_llm._client = lambda timeout=30.0: httpx.AsyncClient(
                base_url="http://test", transport=httpx.MockTransport(second_handler)
            )
            resumed = AgentLoop(
                resumed_llm, [definition], scope, db_path=db_path, run_id=paused.run_id
            )
            completed = await resumed.resume(paused.action_id, "confirmed")

        self.assertEqual(completed.reply, "已保存。")
        self.assertEqual(executions, [{"fact": "x"}])

    async def test_resume_completes_all_tool_call_observations_after_pause(self):
        first_response = iter([_response(calls=[
            _call("low-1", "lookup", {"query": "before"}),
            _call("high-1", "save", {"fact": "x"}),
            _call("low-2", "lookup", {"query": "after"}),
        ])])
        second_response = iter([_response(content="done")])
        executions = []

        async def lookup(arguments):
            executions.append(("lookup", arguments["query"]))
            return {"result": arguments["query"]}

        async def save(arguments):
            executions.append(("save", arguments["fact"]))
            return {"saved": True}

        async def first_handler(request):
            return httpx.Response(200, json=next(first_response))

        async def second_handler(request):
            body = json.loads(request.content)
            tool_ids = [item["tool_call_id"] for item in body["messages"] if item.get("role") == "tool"]
            self.assertEqual(tool_ids, ["low-1", "high-1", "low-2"])
            return httpx.Response(200, json=next(second_response))

        scope = MemoryScope("test-user", "desktop", "test-multi-pause")
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "agent.db"
            definitions = [
                ToolDefinition("lookup", {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}, lookup),
                ToolDefinition("save", {"type": "function", "function": {"name": "save", "parameters": {"type": "object"}}}, save, risk="high"),
            ]
            first_llm = LLMClient("system")
            first_llm._client = lambda timeout=30.0: httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(first_handler))
            paused = await AgentLoop(first_llm, definitions, scope, db_path=db_path).run("run tools")
            self.assertEqual(paused.status, "paused")
            self.assertEqual(executions, [("lookup", "before")])

            resumed_llm = LLMClient("system")
            resumed_llm._client = lambda timeout=30.0: httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(second_handler))
            completed = await AgentLoop(resumed_llm, definitions, scope, db_path=db_path, run_id=paused.run_id).resume(paused.action_id, "confirmed")

        self.assertEqual(completed.status, "completed")
        self.assertEqual(executions, [("lookup", "before"), ("save", "x")])


    async def test_music_command_cannot_finish_without_successful_play_tool(self):
        responses = iter([
            _response(content="Would you like me to play it?"),
            _response(calls=[_call("search-1", "music_search", {"query": "Gareth.T glass"})]),
            _response(calls=[_call("play-1", "music_play", {"track_ref": "track-1"})]),
            _response(content="A poetic line that must not replace the playback result."),
        ])
        requests = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=next(responses))

        async def search(_arguments):
            return {"status": "ok", "output": {"songs": [{"track_ref": "track-1"}]}}

        async def play(_arguments):
            return {
                "status": "ok",
                "output": {"played": True, "name": "\u73bb\u7483", "artist": "Gareth.T"},
            }

        llm = LLMClient("system")
        llm._client = lambda timeout=30.0: httpx.AsyncClient(
            base_url="http://test", transport=httpx.MockTransport(handler)
        )
        definitions = [
            ToolDefinition("music_search", {"type": "function", "function": {"name": "music_search", "parameters": {"type": "object"}}}, search),
            ToolDefinition("music_play", {"type": "function", "function": {"name": "music_play", "parameters": {"type": "object"}}}, play),
        ]
        with tempfile.TemporaryDirectory() as directory:
            outcome = await AgentLoop(
                llm, definitions, MemoryScope("music-user", "wechat", "music-chat"),
                db_path=Path(directory) / "agent.db",
            ).run("\u64ad\u653e Gareth.T \u7684\u73bb\u7483")

        self.assertEqual(outcome.reply, "\u6b63\u5728\u64ad\u653e Gareth.T \u7684\u300a\u73bb\u7483\u300b\u3002")
        self.assertEqual(len(requests), 4)
        self.assertIn("SYSTEM CONSTRAINT", requests[1]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
