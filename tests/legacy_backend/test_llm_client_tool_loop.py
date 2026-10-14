"""LLMClient.chat_with_tools 真实循环测试（httpx.MockTransport 拦截）。

覆盖之前没有单测的真实方法体：
- 工具轮次耗尽后（模型连续多轮都调工具），进入最终整理，不丢已查到的工具结果。
"""

from __future__ import annotations

import json
import unittest
import httpx

from backend.llm.client import LLMClient


def _tool_response(tool_calls: list[dict] | None, content: str | None) -> dict:
    """构造一个 OpenAI /chat/completions 响应。"""
    message: dict = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message, "finish_reason": "stop"}],
    }


def _weather_call() -> dict:
    return {
        "id": "call_weather_1",
        "type": "function",
        "function": {"name": "weather_lookup", "arguments": '{"city": "广州"}'},
    }


def _web_search_call() -> dict:
    return {
        "id": "call_search_1",
        "type": "function",
        "function": {"name": "web_search", "arguments": '{"query": "广州天气"}'},
    }


class ChatWithToolsLoopTests(unittest.TestCase):
    def _make_client(self, responses: list[dict]) -> LLMClient:
        """构造一个每次请求返回预设响应的 LLMClient。"""
        responses_iter = iter(responses)

        async def handler(request: httpx.Request) -> httpx.Response:
            try:
                resp = next(responses_iter)
            except StopIteration:
                return httpx.Response(500, json={"error": "unexpected extra request"})
            return httpx.Response(200, json=resp)

        client = LLMClient(system_prompt="你是测试用的人设")
        # 替换 _client：让每次请求都走 MockTransport
        client._client = lambda timeout=30.0: httpx.AsyncClient(
            base_url="http://test.local/v1",
            trust_env=False,
            headers={"Authorization": "Bearer test", "Content-Type": "application/json"},
            transport=httpx.MockTransport(handler),
            timeout=timeout,
        )
        return client

    async def _run(self, client: LLMClient, responses: list[dict], max_rounds: int = 3) -> str:
        client._client = client._client  # keep type checkers happy (no-op)
        # 构造新的 client，把 responses 传进去
        c = self._make_client(responses)
        return await c.chat_with_tools(
            "广州今天天气怎么样",
            [{"type": "function", "function": {"name": "weather_lookup", "parameters": {"type": "object"}}}],
            self._executor,
            max_rounds=max_rounds,
        )

    @staticmethod
    async def _executor(name: str, arguments: dict) -> dict:
        if name == "weather_lookup":
            return {"weather": {"temperature": "25°C", "condition": "多云"}}
        if name == "web_search":
            return {"result": "广州今天多云转晴。"}
        return {"status": "ok"}

    # ---------- 关键测试：工具轮次耗尽后进入最终整理 ----------

    def test_exhausted_rounds_does_final_summary(self) -> None:
        """模型连续 3 轮都调工具 → 第 4 次请求强制总结，不返回"想得太久"兜底。"""
        import asyncio

        # 前 3 轮：一直调工具（没有 content）
        responses = [
            _tool_response([_weather_call()], None),
            _tool_response([_web_search_call()], None),
            _tool_response([_weather_call()], None),
            # 第 4 次：不带 tools 的最终总结，模型必须给内容
            _tool_response([], "广州今天多云转晴，25°C。"),
        ]
        client = self._make_client(responses)
        reply = asyncio.run(
            client.chat_with_tools(
                "广州今天天气怎么样",
                [
                    {"type": "function", "function": {"name": "weather_lookup", "parameters": {"type": "object"}}},
                    {"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}},
                ],
                self._executor,
                max_rounds=3,
            )
        )
        self.assertEqual(reply, "广州今天多云转晴，25°C。")
        # 历史里存的是最终整理结果，不是兜底话
        self.assertEqual(client.messages[-1]["content"], "广州今天多云转晴，25°C。")

    def test_no_tool_calls_returns_directly(self) -> None:
        """模型第一轮就直接回复（不调工具）→ 直接返回，不多发请求。"""
        import asyncio

        responses = [
            _tool_response([], "你好呀！"),
        ]
        client = self._make_client(responses)
        reply = asyncio.run(
            client.chat_with_tools(
                "你好",
                [{"type": "function", "function": {"name": "weather_lookup", "parameters": {"type": "object"}}}],
                self._executor,
                max_rounds=3,
            )
        )
        self.assertEqual(reply, "你好呀！")

    def test_final_summary_failure_falls_back(self) -> None:
        """最终整理请求失败 → 回落到兜底文案，不抛异常。"""
        import asyncio

        responses = [
            _tool_response([_weather_call()], None),
        ]
        client = self._make_client(responses)
        # max_rounds=1：只允许 1 轮工具，第 2 次请求（最终整理）会超出预设响应 → 500
        reply = asyncio.run(
            client.chat_with_tools(
                "广州天气",
                [{"type": "function", "function": {"name": "weather_lookup", "parameters": {"type": "object"}}}],
                self._executor,
                max_rounds=1,
            )
        )
        self.assertIn("想得太久", reply)

    def test_no_rounds_no_crash(self) -> None:
        """max_rounds=0：循环不执行，走兜底。"""
        import asyncio

        client = self._make_client([])
        reply = asyncio.run(
            client.chat_with_tools(
                "你好",
                [{"type": "function", "function": {"name": "weather_lookup", "parameters": {"type": "object"}}}],
                self._executor,
                max_rounds=0,
            )
        )
        self.assertIn("想得太久", reply)


if __name__ == "__main__":
    unittest.main()
