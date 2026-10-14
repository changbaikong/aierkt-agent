"""GENERATE_WITH_TOOLS stage tests against the unified AgentLoop boundary."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agent_loop import AgentOutcome
from backend.dialogue.context import TurnContext
from backend.dialogue.errors import StageResult
from backend.dialogue.stages import stage_generate_with_tools


def _make_ctx(source_message: str, ephemeral_suffix: str = "") -> TurnContext:
    ctx = TurnContext(ws=MagicMock(), llm=MagicMock())
    ctx.source_message = source_message
    ctx.ephemeral_suffix = ephemeral_suffix
    return ctx


class GenerateWithToolsTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, ctx: TurnContext, outcome: AgentOutcome):
        with (
            patch("backend.memory.build_memory_context", return_value=""),
            patch("backend.agent_core_adapters.legacy_tools.CompatAgentLoop") as loop_class,
        ):
            loop_class.return_value.run = AsyncMock(return_value=outcome)
            result = await stage_generate_with_tools(ctx)
        return result, loop_class.return_value.run

    async def test_completed_reply_is_stored(self):
        ctx = _make_ctx("今天广州天气怎么样")
        result, run = await self._run(ctx, AgentOutcome("completed", "run-1", "广州今天中雨。"))
        self.assertEqual(result, StageResult.OK)
        self.assertEqual(ctx.reply, "广州今天中雨。")
        self.assertEqual(ctx.display_text, ctx.reply)
        run.assert_awaited_once()

    async def test_empty_reply_gets_fallback(self):
        ctx = _make_ctx("你好")
        result, _ = await self._run(ctx, AgentOutcome("completed", "run-2", ""))
        self.assertEqual(result, StageResult.OK)
        self.assertIn("卡住了", ctx.reply)

    async def test_ephemeral_suffix_is_passed_to_loop(self):
        ctx = _make_ctx("你好呀", "\n\n[硬性要求：必须输出 SPEAK]")
        _, run = await self._run(ctx, AgentOutcome("completed", "run-3", "你好呀！"))
        _, kwargs = run.call_args
        self.assertIn("必须输出 SPEAK", kwargs["ephemeral_suffix"])

    async def test_confirmation_outcome_is_exposed(self):
        ctx = _make_ctx("记住这件事")
        confirmation = {"action_id": "a1", "code": "ABCD1234", "summary": "保存记忆"}
        result, _ = await self._run(ctx, AgentOutcome("paused", "run-4", action_id="a1", confirmation_code="ABCD1234", confirmation=confirmation))
        self.assertEqual(result, StageResult.OK)
        self.assertEqual(ctx.agent_status, "paused")
        self.assertEqual(ctx.confirmation, confirmation)

    async def test_agent_exception_is_fatal(self):
        ctx = _make_ctx("你好")
        with (
            patch("backend.memory.build_memory_context", return_value=""),
            patch("backend.agent_core_adapters.legacy_tools.CompatAgentLoop") as loop_class,
        ):
            loop_class.return_value.run = AsyncMock(side_effect=RuntimeError("api down"))
            result = await stage_generate_with_tools(ctx)
        self.assertEqual(result, StageResult.FATAL)


if __name__ == "__main__":
    unittest.main()
