"""工具失败分类（error_kind）的行为测试。

重点不是"字段存在"，而是三条容易被改坏的规则：
1. 判定顺序：PermissionError 是 OSError 子类，必须先于 is_retryable 判掉，
   否则会被 OSError 规则误判成可重试。
2. handler 自己声明的 error_kind 要走白名单，脏值丢弃而不是原样透传。
3. CancelledError 不能被分类逻辑吞掉——它是 BaseException，必须继续冒泡。
"""

import asyncio
import unittest

from agent_core.contracts import Scope, ToolCall, ToolResult, ToolSpec
from agent_core.exceptions import RateLimitError, is_retryable
from agent_core.tools import ToolExecutor, classify_tool_error

SCOPE = Scope("owner", "wechat", "chat")


def make_spec(name, handler, timeout=0.2):
    return ToolSpec(
        name=name,
        schema={"type": "function", "function": {"name": name, "parameters": {"type": "object"}}},
        handler=handler,
        timeout_seconds=timeout,
    )


def raising(exc):
    def handler(arguments, scope):
        raise exc
    return handler


class ClassifyToolErrorTest(unittest.TestCase):
    """分类本身是纯函数，用表驱动一次钉死每种异常的归属。"""

    CASES = [
        (asyncio.TimeoutError(), "timeout"),
        (TimeoutError(), "timeout"),
        (PermissionError("denied"), "permission"),
        (ConnectionError("connection reset"), "transient"),
        (RateLimitError("429", retry_after=2), "transient"),
        (KeyError("missing_field"), "permanent"),
        (ValueError("bad value"), "permanent"),
        (TypeError("bad type"), "permanent"),
    ]

    def test_table(self):
        for exc, expected in self.CASES:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(classify_tool_error(exc), expected)

    def test_permission_is_not_retryable_even_though_it_is_an_oserror(self):
        """回归保护：PermissionError 继承自 OSError，而 OSError 被 is_retryable 视为可重试。

        若有人把 PermissionError 分支删掉或挪到 is_retryable 之后，这条会先红。
        """
        self.assertIsInstance(PermissionError("x"), OSError)
        self.assertTrue(is_retryable(OSError("generic os error")))
        self.assertFalse(is_retryable(PermissionError("x")))
        self.assertEqual(classify_tool_error(PermissionError("x")), "permission")

    def test_every_kind_is_in_the_contract_vocabulary(self):
        from agent_core.contracts import TOOL_ERROR_KINDS
        for _, expected in self.CASES:
            with self.subTest(kind=expected):
                self.assertIn(expected, TOOL_ERROR_KINDS)


class ToolResultSerializationTest(unittest.TestCase):
    def test_empty_kind_is_omitted(self):
        self.assertEqual(ToolResult("ok", {"v": 1}).as_dict(), {"status": "ok", "output": {"v": 1}})

    def test_kind_is_emitted_when_present(self):
        self.assertEqual(
            ToolResult("failed", error="boom", error_kind="transient").as_dict(),
            {"status": "failed", "output": {}, "error": "boom", "error_kind": "transient"},
        )


class ToolExecutorErrorKindTest(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_kind_timeout(self):
        async def hang(arguments, scope):
            await asyncio.sleep(5)

        result = await ToolExecutor().execute(make_spec("search", hang), ToolCall("c1", "search", {}), SCOPE)
        self.assertEqual((result.status, result.error_kind), ("failed", "timeout"))

    async def test_uncaught_exception_is_classified(self):
        for exc, kind in [
            (ConnectionError("网络抖动"), "transient"),
            (KeyError("missing_field"), "permanent"),
            (PermissionError("denied"), "permission"),
        ]:
            with self.subTest(kind=kind):
                result = await ToolExecutor().execute(
                    make_spec("t", raising(exc), timeout=5), ToolCall("c1", "t", {}), SCOPE
                )
                self.assertEqual((result.status, result.error_kind), ("failed", kind))

    async def test_handler_declared_kind_is_adopted(self):
        async def handler(arguments, scope):
            return {"status": "failed", "error": "upstream 429", "error_kind": "transient"}

        result = await ToolExecutor().execute(make_spec("t", handler, timeout=5), ToolCall("c1", "t", {}), SCOPE)
        self.assertEqual(result.error_kind, "transient")

    async def test_handler_declared_kind_outside_vocabulary_is_dropped(self):
        async def handler(arguments, scope):
            return {"status": "failed", "error": "boom", "error_kind": "temporary"}

        result = await ToolExecutor().execute(make_spec("t", handler, timeout=5), ToolCall("c1", "t", {}), SCOPE)
        self.assertEqual(result.error_kind, "")
        self.assertEqual(result.error, "boom")

    async def test_success_has_no_kind(self):
        async def handler(arguments, scope):
            return {"status": "ok", "output": {"v": 1}}

        result = await ToolExecutor().execute(make_spec("t", handler, timeout=5), ToolCall("c1", "t", {}), SCOPE)
        self.assertEqual((result.status, result.error_kind), ("ok", ""))

    async def test_handler_returning_tool_result_is_passed_through(self):
        """回归保护：返回 ToolResult 的 handler（media 模块）不能被 dict() 归一化打坏。

        ToolResult 不是 mapping，若少了短路分支，这里会得到
        "TypeError: 'ToolResult' object is not iterable"，成功结果被误报成 failed。
        """
        async def handler(arguments, scope):
            return ToolResult("ok", {"job_id": "j1"})

        result = await ToolExecutor().execute(make_spec("media.submit_tts", handler, timeout=5),
                                              ToolCall("c1", "media.submit_tts", {}), SCOPE)
        self.assertEqual((result.status, result.output, result.error_kind), ("ok", {"job_id": "j1"}, ""))

    async def test_cancelled_error_still_escapes(self):
        """CancelledError 是 BaseException，不能变成一条 failed 结果——否则取消会被静默吃掉。"""
        async def handler(arguments, scope):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await ToolExecutor().execute(make_spec("t", handler, timeout=5), ToolCall("c1", "t", {}), SCOPE)


if __name__ == "__main__":
    unittest.main()
