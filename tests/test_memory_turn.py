"""统一记忆候选与回合处理包的契约测试（B 计划第一步）。

先把契约钉死，后面的提炼/存储/召回才有共同的判断依据。
"""
from __future__ import annotations

import unittest

from agent_core.contracts import Scope
from agent_core.memory_turn import (
    CONTENT_KINDS,
    CONTENT_STATUSES,
    PACKET_SECTION_LIMITS,
    EvidenceRef,
    HistoryItem,
    MemoryCandidate,
    MemoryCandidateError,
    MemoryRef,
    MemoryTurnPacket,
    apply_packet_budget,
    candidate_idempotency_key,
    estimate_units,
    semantic_key,
    serialize_candidates,
    turn_from_messages,
    validate_candidate,
)


def _candidate(**overrides):
    base = dict(
        content="决定让主回答模型参与记忆候选提炼",
        kind="decision",
        content_status="proposed",
        source_types=("assistant_suggestion",),
    )
    base.update(overrides)
    return MemoryCandidate(**base)


class ContentStatusMatrixTests(unittest.TestCase):
    def test_every_kind_accepts_its_own_statuses(self) -> None:
        for kind, statuses in CONTENT_STATUSES.items():
            for status in statuses:
                sources = ("user_statement",)
                origin = "model_candidate"
                if (kind, status) in {("fact", "verified"), ("plan", "completed")}:
                    origin, sources = "program", ("tool_result",)
                with self.subTest(kind=kind, status=status):
                    validate_candidate(
                        _candidate(
                            kind=kind,
                            content_status=status,
                            source_types=sources,
                            origin=origin,
                        )
                    )

    def test_kind_rejects_status_from_another_kind(self) -> None:
        # 「计划已完成」不能挂到决策上：两类的进展词表互不通用。
        with self.assertRaises(MemoryCandidateError) as ctx:
            validate_candidate(_candidate(kind="decision", content_status="completed"))
        self.assertIn("completed", str(ctx.exception))

    def test_all_seven_kinds_are_declared(self) -> None:
        self.assertEqual(set(CONTENT_KINDS), set(CONTENT_STATUSES))

    def test_unknown_kind_and_unknown_source_are_rejected(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(kind="note"))
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(source_types=("webpage",)))

    def test_blank_content_is_rejected(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(content="   "))


class ConfidenceCannotPromoteTests(unittest.TestCase):
    def test_model_candidate_cannot_claim_verified_fact(self) -> None:
        """模型对句意有把握，不等于这件事被工具证实过。"""
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(
                _candidate(
                    kind="fact",
                    content_status="verified",
                    source_types=("tool_result",),
                    confidence=0.99,
                )
            )

    def test_program_candidate_may_claim_verified_fact(self) -> None:
        validate_candidate(
            _candidate(
                kind="fact",
                content_status="verified",
                source_types=("tool_result",),
                origin="program",
            )
        )

    def test_completed_plan_needs_tool_evidence(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(
                _candidate(kind="plan", content_status="completed", origin="program")
            )
        validate_candidate(
            _candidate(
                kind="plan",
                content_status="completed",
                origin="program",
                source_types=("tool_result",),
            )
        )

    def test_assistant_suggestion_cannot_be_stored_as_user_accepted(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(content_status="accepted"))
        validate_candidate(
            _candidate(content_status="accepted", source_types=("user_statement",))
        )

    def test_confidence_out_of_range_is_rejected(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(confidence=1.4))


class ScopeTests(unittest.TestCase):
    def test_project_scope_requires_project_id(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(scope_kind="project"))
        validate_candidate(_candidate(scope_kind="project", project_id="dm-agent"))

    def test_owner_scope_only_for_explicit_user_preference(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(
                _candidate(kind="fact", content_status="reported", scope_kind="owner")
            )
        validate_candidate(
            _candidate(
                kind="preference",
                content_status="explicit",
                source_types=("user_statement",),
                scope_kind="owner",
            )
        )

    def test_unknown_scope_kind_is_rejected(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(scope_kind="global"))

    def test_relations_must_point_at_known_ids(self) -> None:
        candidate = _candidate(related_ids=("mem-1", "mem-2"))
        validate_candidate(candidate, known_ids=("mem-1", "mem-2", "mem-3"))
        with self.assertRaises(MemoryCandidateError) as ctx:
            validate_candidate(candidate, known_ids=("mem-1",))
        self.assertIn("mem-2", str(ctx.exception))

    def test_supersedes_is_checked_as_well(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            validate_candidate(_candidate(supersedes=("mem-9",)), known_ids=("mem-1",))


class StableKeyTests(unittest.TestCase):
    def test_semantic_key_ignores_whitespace_and_case(self) -> None:
        self.assertEqual(
            semantic_key("decision", "让主模型参与记忆"),
            semantic_key("decision", " 让主模型参与记忆 "),
        )

    def test_semantic_key_is_order_independent_for_retries(self) -> None:
        """换一次模型输出顺序，不该重复造记录。"""
        first = MemoryCandidate(
            content="记忆按当前任务召回",
            kind="decision",
            content_status="proposed",
            source_types=("assistant_suggestion",),
            topics=("记忆", "召回"),
        )
        again = MemoryCandidate(
            content="记忆按当前任务召回",
            kind="decision",
            content_status="proposed",
            source_types=("assistant_suggestion",),
            topics=("召回", "记忆"),
        )
        self.assertEqual(first.resolved_stable_key(), again.resolved_stable_key())

    def test_idempotency_key_is_stable_and_requires_parts(self) -> None:
        args = dict(
            scope_kind="conversation",
            scope_id="conv-1",
            turn_or_event_id="turn-9",
            source_locator="m3",
            stable_key="decision||abc",
        )
        self.assertEqual(
            candidate_idempotency_key(**args), candidate_idempotency_key(**args)
        )
        self.assertNotEqual(
            candidate_idempotency_key(**args),
            candidate_idempotency_key(**{**args, "turn_or_event_id": "turn-10"}),
        )
        with self.assertRaises(MemoryCandidateError):
            candidate_idempotency_key(**{**args, "turn_or_event_id": ""})

    def test_metadata_carries_reason_and_status_but_not_confidence_as_status(self) -> None:
        metadata = _candidate(
            content_status="accepted",
            source_types=("user_statement",),
            reason="不想陪伴记忆和项目记忆分裂",
            goal="同一份上下文",
            topics=("记忆",),
            source_refs=("msg-3",),
        ).as_metadata()
        self.assertEqual(metadata["reason"], "不想陪伴记忆和项目记忆分裂")
        self.assertEqual(metadata["source_types"], ["user_statement"])
        self.assertEqual(metadata["semantics_version"], 1)
        # 进展状态走独立列，不塞进 metadata 冒充置信度。
        self.assertNotIn("content_status", metadata)
        self.assertNotIn("confidence", metadata)


class UnitEstimateTests(unittest.TestCase):
    def test_chinese_is_not_underestimated_at_length_over_four(self) -> None:
        text = "决定" * 40  # 80 个汉字
        self.assertEqual(estimate_units(text), 80)

    def test_ascii_falls_back_to_a_quarter_of_length(self) -> None:
        self.assertEqual(estimate_units("abcdefgh"), 2)

    def test_empty_text_counts_zero(self) -> None:
        self.assertEqual(estimate_units(""), 0)
        self.assertEqual(estimate_units(None), 0)


class PacketTests(unittest.TestCase):
    def _packet(self, **overrides) -> MemoryTurnPacket:
        base = dict(
            turn_id="turn-1",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope_owner="local-user",
            scope_channel="web",
            scope_conversation="conv-1",
            user_text="这个怎么和搜索结合",
            published_reply="先把记忆按当前任务召回，再把材料合并",
        )
        base.update(overrides)
        return MemoryTurnPacket(**base)

    def test_packet_round_trips_through_payload(self) -> None:
        packet = self._packet(
            relevant_history=[HistoryItem("m1", "user", "先讨论记忆提炼")],
            recalled_memory_refs=[MemoryRef("mem-1", "decision", "accepted", excerpt="让主模型参与")],
            evidence_refs=[EvidenceRef("ev-1", "网页说明支持该做法", outcome="success")],
            task_goal="把记忆和搜索接起来",
        )
        restored = MemoryTurnPacket.from_payload(packet.as_payload())
        self.assertEqual(restored.turn_id, packet.turn_id)
        self.assertEqual(restored.scope_kind, packet.scope_kind)
        self.assertEqual(restored.relevant_history[0].message_id, "m1")
        self.assertEqual(restored.recalled_memory_refs[0].memory_id, "mem-1")
        self.assertEqual(restored.evidence_refs[0].outcome, "success")

    def test_packet_payload_has_no_secrets_placeholder(self) -> None:
        payload = self._packet().as_payload()
        self.assertNotIn("api_key", payload)
        self.assertNotIn("token", payload)

    def test_project_scope_requires_binding(self) -> None:
        with self.assertRaises(MemoryCandidateError):
            self._packet(scope_kind="project")

    def test_unpublished_reply_is_not_treated_as_assistant_conclusion(self) -> None:
        self.assertTrue(self._packet().is_published())
        self.assertFalse(self._packet(outcome_status="cancelled").is_published())
        self.assertFalse(self._packet(published_reply="").is_published())

    def test_backend_progress_messages_do_not_enter_history(self) -> None:
        packet = turn_from_messages(
            turn_id="turn-2",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope=Scope("local-user", "web", "conv-1"),
            user_text="上次那个方案",
            published_reply="按上次定的做",
            relevant_history=[
                {"id": "m1", "role": "user", "content": "讨论记忆提炼"},
                {"id": "m2", "kind": "work_result", "content": "「每晚总结」已经办好了"},
                {"id": "m3", "kind": "steward_progress", "content": "正在处理"},
            ],
        )
        self.assertEqual([item.message_id for item in packet.relevant_history], ["m1"])

    def test_budget_drops_duplicates_without_truncating_current_turn(self) -> None:
        packet = self._packet(
            relevant_history=[
                HistoryItem("m1", "user", "讨论记忆提炼"),
                HistoryItem("m2", "assistant", "讨论记忆提炼"),
            ],
        )
        trimmed, report = apply_packet_budget(packet)
        self.assertEqual(len(trimmed.relevant_history), 1)
        self.assertEqual(trimmed.completeness, "partial")
        self.assertIn("relevant_history", trimmed.omitted_sections)
        self.assertNotIn("needs_retry", report)
        # 当前问答本身没被裁剪，正文一个字不少。
        self.assertEqual(trimmed.user_text, "这个怎么和搜索结合")

    def test_budget_marks_retry_instead_of_silently_truncating(self) -> None:
        packet = self._packet(user_text="问" * 9000, published_reply="答")
        trimmed, report = apply_packet_budget(packet, unit_limit=100)
        self.assertTrue(report.get("needs_retry"))
        self.assertIn("current_turn", trimmed.omitted_sections)
        self.assertEqual(trimmed.user_text, "问" * 9000)

    def test_section_limits_are_enforced(self) -> None:
        max_items = PACKET_SECTION_LIMITS["relevant_history"][0]
        packet = self._packet(
            relevant_history=[
                HistoryItem(f"m{i}", "user", f"第{i}条背景")
                for i in range(max_items + 3)
            ]
        )
        trimmed, _ = apply_packet_budget(packet)
        self.assertEqual(len(trimmed.relevant_history), max_items)


class SerializeTests(unittest.TestCase):
    def test_candidates_serialize_with_kind_and_status(self) -> None:
        import json

        raw = serialize_candidates([_candidate(topics=("记忆",))])
        items = json.loads(raw)
        self.assertEqual(items[0]["kind"], "decision")
        self.assertEqual(items[0]["content_status"], "proposed")
        self.assertTrue(items[0]["stable_key"])


if __name__ == "__main__":
    unittest.main()

class HandoffIntoPacketTests(unittest.TestCase):
    """回合交接快照要能进处理包：本轮用过什么，提炼那边才看得见。"""

    def _packet(self, handoff):
        return turn_from_messages(
            turn_id="turn-h",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope=Scope("local-user", "web", "conv-1"),
            user_text="这个怎么和搜索结合",
            published_reply="按已采纳的方案接",
            handoff=handoff,
        )

    def test_recalled_memories_and_evidence_come_from_the_handoff(self) -> None:
        packet = self._packet(
            {
                "recalled_memory_refs": [
                    {"memory_id": "mem-1", "kind": "decision", "content_status": "accepted",
                     "excerpt": "让主模型参与候选提炼", "layer": "warm"}
                ],
                "evidence_refs": [
                    {"ref_id": "https://example.invalid/a", "summary": "官方说明",
                     "outcome": "success", "evidence_level": "original"}
                ],
                "tool_outcomes": [
                    {"action_id": "a1", "name": "web__search", "status": "failed"},
                    {"action_id": "a2", "name": "system__local_info", "status": "ok"},
                ],
                "project_binding": "dm-agent",
            }
        )
        self.assertEqual(packet.recalled_memory_refs[0].memory_id, "mem-1")
        self.assertEqual(packet.recalled_memory_refs[0].content_status, "accepted")
        self.assertEqual(packet.project_binding, "dm-agent")
        # 工具终局按原状态记进证据，不美化成"成功"。
        outcomes = {item.ref_id: item.outcome for item in packet.evidence_refs}
        self.assertEqual(outcomes.get("a1"), "failed")
        self.assertEqual(outcomes.get("a2"), "ok")
        self.assertIn("https://example.invalid/a", outcomes)

    def test_missing_handoff_leaves_the_packet_empty_but_valid(self) -> None:
        packet = self._packet(None)
        self.assertEqual(packet.recalled_memory_refs, [])
        self.assertEqual(packet.evidence_refs, [])
