from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from backend.agent_loop import AgentOutcome
from backend.companion_core import create_channel_reply, infer_expression, parse_channel_reply
from backend.context_service import build_fast_chat_context
from backend.memory import MemoryScope
from backend.speech_text import romanize_marked_chinese


class CompanionCoreTests(unittest.TestCase):
    def test_fast_chat_context_uses_only_two_clean_turns_and_no_memory(self) -> None:
        history = [
            {"role": "user", "content": "u1"}, {"role": "assistant", "content": "a1"},
            {"role": "tool", "content": "secret observation"},
            {"role": "user", "content": "u2"}, {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"}, {"role": "assistant", "content": "a3"},
        ]
        with patch("backend.context_service.assemble_memory_context") as memory_builder:
            context = build_fast_chat_context(
                MemoryScope("wechat:u", "wechat", "u"), history=history, channel_rules="wechat rules"
            )
        memory_builder.assert_not_called()
        self.assertEqual([item["content"] for item in context.history], ["u2", "a2", "u3", "a3"])
        self.assertEqual(context.memory_text, "")
        self.assertNotIn("secret observation", context.system_prompt)

    def test_plain_text_reply_is_preserved(self) -> None:
        reply = parse_channel_reply("  正常回复  ")

        self.assertEqual(reply.text, "正常回复")
        self.assertIsNone(reply.sticker_path)

    def test_valid_sticker_becomes_an_image_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sticker_dir = Path(directory)
            sticker_path = sticker_dir / "开心.webp"
            sticker_path.touch()

            with patch("backend.companion_core.STICKER_DIR", sticker_dir):
                reply = parse_channel_reply("收到啦 [STICKER:开心]")

        self.assertEqual(reply.text, "收到啦")
        self.assertEqual(reply.sticker_path, sticker_path.resolve())

    def test_directives_only_reply_gets_visible_fallback(self) -> None:
        reply = parse_channel_reply("[SPEAK:忽略][MUSIC:不要在微信播放]")

        self.assertEqual(reply.text, "……吾辈刚才走神了。再说一次吧。")
        self.assertIsNone(reply.sticker_path)

    def test_unknown_sticker_label_is_removed_without_file_access(self) -> None:
        reply = parse_channel_reply("不知道 [STICKER:不存在的文件]")

        self.assertEqual(reply.text, "不知道")
        self.assertIsNone(reply.sticker_path)

    def test_expression_is_inferred_without_an_extra_model_call(self) -> None:
        self.assertEqual(infer_expression("哼，笨蛋才不是在等你"), "傲娇")
        self.assertEqual(parse_channel_reply("今天真开心").expression, "开心")

    def test_expression_includes_conversational_behavior_intents(self) -> None:
        self.assertEqual(infer_expression("嗯嗯，确实很烦人。"), "认同")
        self.assertEqual(infer_expression("怎么啦，哪里不舒服？"), "关心")
        self.assertEqual(infer_expression("不可以哦，这样太危险。"), "拒绝")

    def test_marked_chinese_is_romanized_for_local_tts(self) -> None:
        self.assertEqual(romanize_marked_chinese("今日は{重庆}へ行くぞ。"), "今日はchongqingへ行くぞ。")


    def test_stage_direction_is_removed_and_ordinary_reply_is_capped_at_two_bubbles(self) -> None:
        reply = parse_channel_reply(
            "嗯？\n"
            "主人是想说‘在吗’还是‘在不在’呀……\n"
            "（轻轻敲了敲刀鞘）\n"
            "吾辈一直在这儿呢，没载走，也没断连～"
        )

        self.assertEqual(len(reply.bubbles), 2)
        self.assertNotIn("刀鞘", reply.text)
        self.assertNotIn("（", reply.text)

    def test_short_reaction_does_not_become_its_own_bubble(self) -> None:
        reply = parse_channel_reply("嗯？\n你是想问我在不在吗？")

        self.assertEqual(reply.bubbles, ("嗯？你是想问我在不在吗？",))

    def test_legitimate_parenthetical_explanation_is_preserved(self) -> None:
        reply = parse_channel_reply("FastAPI（一个 Python Web 框架）可以处理这个接口。")

        self.assertIn("（一个 Python Web 框架）", reply.text)

    def test_explicit_detailed_reply_can_use_four_bubbles(self) -> None:
        reply = parse_channel_reply(
            "第一步先检查日志。\n第二步检查配置。\n第三步验证网络。\n第四步再重启服务。",
            max_bubbles=4,
        )

        self.assertEqual(len(reply.bubbles), 4)


class CompanionCoreAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsafe_fast_reply_escalates_once_to_main_agent(self) -> None:
        scope = MemoryScope("wechat:route-user", "wechat", "route-user")
        fast_llm = SimpleNamespace(
            messages=[], max_tokens=1024,
            chat=AsyncMock(return_value="\u5df2\u7ecf\u64ad\u653e\u4e86\u3002"),
        )
        main_llm = SimpleNamespace(messages=[], max_tokens=1024)
        fast_context = SimpleNamespace(
            system_prompt="fast", history=[], ephemeral_instructions="", memory_text="", memory_context=None,
        )
        full_context = SimpleNamespace(
            system_prompt="main", history=[], ephemeral_instructions="", memory_text="full", memory_context=None,
        )
        with (
            patch("backend.companion_core.LLMClient", side_effect=[fast_llm, main_llm]) as llm_type,
            patch("backend.companion_core.build_fast_chat_context", return_value=fast_context) as fast_builder,
            patch("backend.companion_core.build_agent_context", return_value=full_context) as full_builder,
            patch("backend.companion_core.AgentLoop") as loop_type,
            patch("backend.companion_core.build_agent_tool_definitions", return_value=[]),
            patch("backend.companion_core.auto_maintain"),
            patch("backend.companion_core.save_raw_message") as save_raw,
        ):
            loop_type.return_value.run = AsyncMock(
                return_value=AgentOutcome("completed", "main-run", "\u64ad\u653e\u6ca1\u6709\u6210\u529f\u3002")
            )
            reply = await create_channel_reply(
                "\u4f60\u597d", "full prompt", history=[], scope=scope,
                fast_path=True, wechat_fast_chat=True,
                route_reason="self_contained_smalltalk",
            )

        self.assertEqual(reply.text, "\u64ad\u653e\u6ca1\u6709\u6210\u529f\u3002")
        self.assertEqual(reply.route, "main_agent")
        self.assertTrue(reply.route_escalated)
        self.assertIn("action_claim", reply.route_reason)
        self.assertEqual(llm_type.call_count, 2)
        fast_builder.assert_called_once()
        full_builder.assert_called_once()
        loop_type.return_value.run.assert_awaited_once()
        self.assertEqual(save_raw.call_count, 2)

    async def test_channel_reply_keeps_speech_out_of_visible_text(self) -> None:
        with (
            patch("backend.companion_core.auto_maintain"),
            patch("backend.companion_core.affection_mood", return_value=""),
            patch("backend.companion_core.build_memory_context", return_value=""),
            patch("backend.companion_core.LLMClient") as llm_type,
            patch("backend.companion_core.AgentLoop") as loop_type,
            patch(
                "backend.companion_core.make_speak_text",
                new=AsyncMock(return_value="ご主人、聞いておるぞ。"),
            ),
            patch("backend.companion_core.remember_speak_text") as remember,
            patch("backend.companion_core.save_raw_message"),
        ):
            loop_type.return_value.run = AsyncMock(return_value=AgentOutcome("completed", "run-1", "中文回复 [SPEAK:不应显示]"))
            reply = await create_channel_reply("hello", "prompt", include_speech=True)

        self.assertEqual(reply.text, "中文回复")
        self.assertEqual(reply.speak_text, "ご主人、聞いておるぞ。")
        remember.assert_called_once_with("ご主人、聞いておるぞ。")
        prompt = llm_type.call_args.kwargs["system_prompt"]
        self.assertIn("这是桌面语音入口", prompt)
        self.assertNotIn("这是微信私聊", prompt)

    async def test_wechat_scope_keeps_wechat_channel_rules(self) -> None:
        scope = MemoryScope("wechat:alice", "wechat", "alice")
        with (
            patch("backend.companion_core.auto_maintain"),
            patch("backend.companion_core.affection_mood", return_value=""),
            patch("backend.companion_core.build_memory_context", return_value=""),
            patch("backend.companion_core.LLMClient") as llm_type,
            patch("backend.companion_core.AgentLoop") as loop_type,
            patch("backend.companion_core.save_raw_message"),
        ):
            loop_type.return_value.run = AsyncMock(return_value=AgentOutcome("completed", "run-2", "收到。"))
            await create_channel_reply("在吗", "prompt", scope=scope)

        prompt = llm_type.call_args.kwargs["system_prompt"]
        self.assertIn("这是微信私聊", prompt)
        self.assertNotIn("这是桌面语音入口", prompt)

    async def test_successful_tool_result_overrides_model_refusal(self) -> None:
        with (
            patch("backend.companion_core.auto_maintain"),
            patch("backend.companion_core.affection_mood", return_value=""),
            patch("backend.companion_core.build_memory_context", return_value=""),
            patch("backend.companion_core.LLMClient") as llm_type,
            patch("backend.companion_core.AgentLoop") as loop_type,
            patch("backend.companion_core.save_raw_message"),
        ):
            loop_type.return_value.run = AsyncMock(return_value=AgentOutcome("completed", "run-3", "根据工具结果回答。"))
            reply = await create_channel_reply("天气怎么样", "prompt")

        self.assertEqual(reply.text, "根据工具结果回答。")

    async def test_direct_tool_question_skips_chat_model(self) -> None:
        with (
            patch("backend.companion_core.auto_maintain"),
            patch("backend.companion_core.affection_mood", return_value=""),
            patch("backend.companion_core.build_memory_context", return_value=""),
            patch("backend.companion_core.LLMClient") as llm_type,
            patch("backend.companion_core.AgentLoop") as loop_type,
            patch("backend.companion_core.save_raw_message"),
        ):
            loop_type.return_value.run = AsyncMock(return_value=AgentOutcome("completed", "run-4", "主人想搜索什么？"))
            reply = await create_channel_reply("帮我搜索一下", "prompt")

        self.assertEqual(reply.text, "主人想搜索什么？")


if __name__ == "__main__":
    unittest.main()
