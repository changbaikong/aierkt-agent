from __future__ import annotations

import unittest

from agent_core.context import ContextAssembler
from agent_core.contracts import Scope
from agent_core.memory import InMemoryMemoryStore, MemoryAssembler


class AgentCoreMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMemoryStore()
        self.assembler = MemoryAssembler(self.store)
        self.scope = Scope("alice", "desktop", "chat-a")

    def test_hot_is_conversation_scoped_and_confirmed_warm_is_injected(self) -> None:
        self.store.append_message(self.scope, "user", "当前对话", "user_chat")
        self.store.append_message(Scope("alice", "desktop", "chat-b"), "user", "另一对话", "user_chat")
        self.store.save_fact(self.scope, "用户喜欢茶", confidence=0.95, source_type="user_explicit")

        context = self.assembler.assemble(self.scope, "")

        self.assertEqual([item["content"] for item in context.hot], ["当前对话"])
        self.assertIn("用户喜欢茶", context.injected_text)
        self.assertNotIn("另一对话", context.injected_text)

    def test_hot_memory_contract_does_not_drop_messages_after_eight(self) -> None:
        for index in range(10):
            self.store.append_message(self.scope, "user", f"message {index}")

        context = self.assembler.assemble(self.scope)

        self.assertEqual(len(context.hot), 10)
        self.assertIn("message 9", context.injected_text)

    def test_cold_history_is_only_injected_for_explicit_history_question(self) -> None:
        self.store.append_message(self.scope, "user", "很久以前说过的话", "user_chat")
        normal = self.assembler.assemble(self.scope, "你好")
        historical = self.assembler.assemble(self.scope, "你还记得以前聊过什么吗")

        self.assertNotIn("[RETRIEVED_EVIDENCE]", normal.injected_text)
        self.assertIn("[RETRIEVED_EVIDENCE]", historical.injected_text)

    def test_context_keeps_ephemeral_instructions_out_of_history(self) -> None:
        context = ContextAssembler(self.assembler).build(
            self.scope,
            history=[{"role": "user", "content": "a"}] * 20,
            channel_rules="desktop rules",
            ephemeral={"turn_style": "short"},
            user_message="hello",
            system_prompt="base",
        )

        self.assertEqual(len(context.history), 15)
        self.assertIn("[turn_style]", context.ephemeral_instructions)
        self.assertNotIn("turn_style", " ".join(item.get("content", "") for item in context.history))

    def test_context_history_excludes_tool_protocol_messages(self) -> None:
        context = ContextAssembler(self.assembler).build(
            self.scope,
            history=[
                {"role": "user", "content": "request"},
                {"role": "assistant", "tool_calls": [{"id": "c1"}], "content": ""},
                {"role": "tool", "tool_call_id": "c1", "content": "internal"},
                {"role": "assistant", "content": "public"},
            ],
        )
        self.assertEqual([item["content"] for item in context.history], ["request", "public"])

    def test_candidate_is_not_injected_until_promoted(self) -> None:
        candidate_id = self.store.save_candidate(self.scope, "候选偏好", confidence=0.7)

        pending = self.assembler.assemble(self.scope)
        self.assertNotIn("候选偏好", pending.injected_text)

        self.assertTrue(self.store.promote_candidate(self.scope, candidate_id))
        promoted = self.assembler.assemble(self.scope)
        self.assertIn("候选偏好", promoted.injected_text)

    def test_expired_fact_is_dropped_and_conflicting_latest_fact_is_preferred(self) -> None:
        self.store.save_fact(self.scope, "用户喜欢咖啡", confidence=1.0, source_type="user_explicit", conflict_key="drink")
        self.store.save_fact(self.scope, "用户不喜欢咖啡", confidence=1.0, source_type="user_explicit", conflict_key="drink", expires_at="2000-01-01T00:00:00")

        context = self.assembler.assemble(self.scope)

        self.assertIn("用户喜欢咖啡", context.injected_text)
        self.assertNotIn("用户不喜欢咖啡", context.injected_text)
        self.assertTrue(context.dropped_items)

        self.store.save_fact(self.scope, "用户不喝咖啡", confidence=1.0, source_type="user_explicit", conflict_key="drink")
        conflicted = self.assembler.assemble(self.scope)
        self.assertTrue(conflicted.conflicts)
        self.assertIn("[MEMORY_CONFLICT]", conflicted.injected_text)

    def test_build_reassembles_decoupled_prompt_parts(self) -> None:
        """build 支持解耦模式：人格/工具构件在组装器内重新拼装，记忆与渠道规则作注入段。"""
        from agent_core.prompts import PromptComposer, PromptContext
        from agent_core.tools import ToolRegistry

        parts = PromptComposer().parts(
            PromptContext(scope=self.scope, profile="primary", registry=ToolRegistry())
        )

        context = ContextAssembler(self.assembler).build(
            self.scope,
            channel_rules="web rules",
            user_message="你好",
            prompt_parts=parts,
        )

        prompt = context.system_prompt
        self.assertIn("[身份与语气]", prompt)
        self.assertIn("[当前工具能力]", prompt)
        self.assertIn("web rules", prompt)
        # 硬约束：安全段先于注入段（渠道规则），人格锁最后
        self.assertLess(prompt.index("[工具与事实边界]"), prompt.index("web rules"))
        self.assertTrue(prompt.rstrip().endswith("不得泄露工具调用、工具观察结果或内部 JSON。"))

    def test_fast_context_is_memory_free_and_keeps_four_messages(self) -> None:
        context = ContextAssembler(self.assembler).build_fast(
            self.scope,
            history=[{"role": "user", "content": str(index)} for index in range(8)],
            channel_rules="fast rules",
        )

        self.assertEqual(len(context.history), 4)
        self.assertIsNone(context.memory_context)
        self.assertEqual(context.metadata["context_mode"], "fast_chat")


    def test_summary_is_available_through_memory_contract(self) -> None:
        self.store.save_summary(self.scope, "recent summary", confidence=0.8)
        context = self.assembler.assemble(self.scope)
        self.assertIn("recent summary", context.injected_text)

    def test_memory_is_channel_scoped(self) -> None:
        other_channel = Scope("alice", "wechat", "chat-a")
        self.store.append_message(other_channel, "user", "wechat-only", "user_chat")
        self.store.save_fact(other_channel, "wechat fact", confidence=1.0, source_type="user_explicit")

        context = self.assembler.assemble(self.scope, "history")

        self.assertNotIn("wechat-only", context.injected_text)
        self.assertNotIn("wechat fact", context.injected_text)


if __name__ == "__main__":
    unittest.main()
