from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_writes import MemoryWriter
from qdrant_client import models


class Embed:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0] if "coffee" in text.lower() else [0.0, 1.0, 0.0]


class MemoryRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repo = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repo.close)
        self.writer = MemoryWriter(self.repo, Embed())
        self.scope = Scope("user", "desktop", "current")
        self.retriever = MemoryRetriever(self.repo, Embed())

    def test_hot_is_current_conversation_only(self) -> None:
        self.writer.append_message(self.scope, "user", "current only")
        self.writer.append_message(Scope("user", "desktop", "other"), "user", "other conversation")
        result = self.retriever.retrieve(self.scope, "hello")
        self.assertEqual([item["content"] for item in result.hot], ["current only"])

    def test_hot_storage_is_unbounded_and_context_window_uses_latest_messages(self) -> None:
        for index in range(20):
            self.writer.append_message(self.scope, "user", f"hot message {index:02d}")

        result = self.retriever.retrieve(self.scope, "unrelated", token_budget=12)
        records = self.writer.list_records(self.scope)

        self.assertEqual(len([row for row in records if row["layer"] == "hot"]), 20)
        self.assertEqual(
            [item["content"] for item in result.hot],
            ["hot message 17", "hot message 18", "hot message 19"],
        )

    def test_history_intent_retrieves_cold_evidence_even_when_warm_matches(self) -> None:
        old = self.writer.append_message(self.scope, "user", "coffee discussion from last month")
        self.writer.archive_records(self.scope, [old["id"]])
        self.writer.save_fact(self.scope, "user prefers coffee", confidence=.9)

        result = self.retriever.retrieve(self.scope, "那时候 coffee preference")

        self.assertIn("coffee discussion from last month", [item["content"] for item in result.cold_evidence])
        self.assertTrue(result.metadata["cold_enabled"])

    def test_history_intent_prioritizes_cold_evidence_with_small_budget(self) -> None:
        old = self.writer.append_message(self.scope, "user", "coffee history evidence")
        self.writer.archive_records(self.scope, [old["id"]])
        self.writer.save_fact(self.scope, "user prefers coffee", confidence=.9)
        self.writer.append_message(self.scope, "assistant", "a long recent response that consumes context")

        result = self.retriever.retrieve(self.scope, "那时候 coffee", token_budget=6)

        self.assertEqual([item["content"] for item in result.cold_evidence], ["coffee history evidence"])

    def test_non_history_query_still_searches_cold_but_ranks_it_after_warm(self) -> None:
        """冷区不因为温区有结果就整层跳过：温区全是近期闲聊时，
        真正有效的架构约束往往只在冷区。顺序上冷区仍然排在温区之后。"""
        old = self.writer.append_message(self.scope, "user", "coffee history evidence")
        self.writer.archive_records(self.scope, [old["id"]])
        self.writer.save_fact(self.scope, "user prefers coffee", confidence=.9)

        result = self.retriever.retrieve(self.scope, "coffee preference")

        self.assertTrue(result.metadata["cold_enabled"])
        self.assertEqual([item["content"] for item in result.warm], ["user prefers coffee"])
        self.assertEqual([item["content"] for item in result.cold_evidence], ["coffee history evidence"])

    def test_archived_fact_is_not_warm_but_is_history_evidence(self) -> None:
        self.writer.save_fact(self.scope, "likes coffee", confidence=.9, conflict_key="drink")
        self.writer.save_fact(self.scope, "does not like coffee", confidence=.9, conflict_key="drink")
        normal = self.retriever.retrieve(self.scope, "coffee")
        history = self.retriever.retrieve(self.scope, "以前喜欢什么 coffee")
        self.assertEqual([item["content"] for item in normal.warm], ["does not like coffee"])
        self.assertIn("likes coffee", [item["content"] for item in history.cold_evidence])

    def test_vector_candidate_is_filtered_by_sql_scope_and_status(self) -> None:
        foreign = self.writer.save_fact(Scope("other", "desktop", "elsewhere"), "likes coffee", confidence=.9)
        result = self.retriever.retrieve(self.scope, "coffee")
        self.assertNotIn(foreign["id"], [item["id"] for item in result.items])

    def test_rrf_prefers_record_found_by_fts_and_vector(self) -> None:
        dual = self.writer.save_fact(self.scope, "coffee preference", confidence=.9)
        failed = MemoryWriter(self.repo, FailingEmbed()).save_candidate(
            self.scope, "coffee note", confidence=.5
        )

        result = self.retriever.retrieve(self.scope, "coffee")

        self.assertEqual(result.warm[0]["id"], dual["id"])
        by_id = {item["id"]: item for item in result.warm}
        self.assertEqual(by_id[dual["id"]]["hit_sources"], ("fts", "vector"))
        self.assertEqual(by_id[failed["id"]]["hit_sources"], ("fts",))

    def test_cold_fallback_returns_history_when_warm_has_no_match(self) -> None:
        self.writer.append_message(self.scope, "user", "project launch happened in May")
        self.writer.archive_records(self.scope, [self.writer.list_records(self.scope)[0]["id"]])

        result = self.retriever.retrieve(self.scope, "launch")

        self.assertEqual([item["content"] for item in result.cold_evidence], ["project launch happened in May"])
        self.assertTrue(result.cold_evidence[0]["evidence_only"])

    def test_candidate_is_separate_from_confirmed_fact_and_citations_are_on_demand(self) -> None:
        self.writer.save_candidate(self.scope, "might like coffee", confidence=.5)
        self.writer.save_fact(self.scope, "likes coffee", confidence=.9)

        context = self.retriever.build_context(self.scope, "coffee, show source")

        self.assertIn("[CONFIRMED_FACT]", context.injected_text)
        self.assertIn("[UNCONFIRMED_CANDIDATE]", context.injected_text)
        self.assertTrue(context.citations)

    def test_token_budget_truncates_low_priority_results(self) -> None:
        self.writer.append_message(self.scope, "user", "one two three four five six seven eight")
        self.writer.append_message(self.scope, "assistant", "nine ten eleven twelve thirteen fourteen")

        result = self.retriever.retrieve(self.scope, "unrelated", token_budget=3)

        self.assertLessEqual(result.metadata["estimated_tokens"], 3)
        self.assertLessEqual(len(result.hot), 1)

    def test_vector_only_candidate_is_retrieved_with_its_vector_source(self) -> None:
        candidate = self.writer.save_candidate(self.scope, "vector memo", confidence=.5)

        result = self.retriever.retrieve(self.scope, "semantic")

        item = next(item for item in result.warm if item["id"] == candidate["id"])
        self.assertEqual(item["hit_sources"], ("vector",))

    def test_wrong_collection_point_cannot_resurrect_a_warm_record_as_cold_evidence(self) -> None:
        fact = self.writer.save_fact(self.scope, "likes coffee", confidence=.9)
        self.repo.vector_client.upsert(
            "cold_memories_vectors",
            [models.PointStruct(id=fact["id"], vector=[1.0, 0.0, 0.0], payload={"memory_id": fact["id"]})],
        )

        result = self.retriever.retrieve(self.scope, "history coffee")

        self.assertNotIn(fact["id"], [item["id"] for item in result.cold_evidence])


class FailingEmbed:
    def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedding unavailable")


class ExpiredMemoryIsUnusableTests(unittest.TestCase):
    """到期就该不能用 —— 这是那条记忆自己的属性，不该等某台机器跑完维护才生效。

    云端模式下本地不再跑轮转维护，而轮转正是把过期记录翻成 'expired' 的那一步。
    只认状态字段的话，一条已经到期、状态却还是 confirmed 的记忆会继续被当成
    有效事实用（同步断掉时尤其明显）。
    """

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repo = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repo.close)
        self.writer = MemoryWriter(self.repo, Embed())
        self.scope = Scope("user", "desktop", "current")
        self.retriever = MemoryRetriever(self.repo, Embed())

    def test_warm_record_past_its_expiry_is_not_returned(self) -> None:
        from datetime import UTC, datetime, timedelta

        record = self.writer.save_memory(
            self.scope, "coffee 偏好已经过期了", conflict_key="user.preference.likes", confidence=0.9,
        )
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
        with self.repo.connect() as db:
            db.execute("UPDATE memory_records SET expires_at=? WHERE id=?", (past, record["id"]))
            status = db.execute("SELECT status FROM memory_records WHERE id=?", (record["id"],)).fetchone()[0]
        self.assertEqual(status, "confirmed", "这条用例要在状态还没翻成 expired 时就成立")

        result = self.retriever.retrieve(self.scope, "coffee")

        self.assertNotIn(record["id"], [item["id"] for item in result.warm])

    def test_record_without_expiry_is_still_returned(self) -> None:
        record = self.writer.save_memory(
            self.scope, "coffee 偏好没有到期时间", conflict_key="user.preference.likes", confidence=0.9,
        )
        with self.repo.connect() as db:
            db.execute("UPDATE memory_records SET expires_at=NULL WHERE id=?", (record["id"],))

        result = self.retriever.retrieve(self.scope, "coffee")

        self.assertIn(record["id"], [item["id"] for item in result.warm])


def _decision(content: str, **overrides):
    from agent_core.memory_turn import MemoryCandidate

    base = dict(
        content=content,
        kind="decision",
        content_status="proposed",
        source_types=("assistant_suggestion",),
    )
    base.update(overrides)
    return MemoryCandidate(**base)


class TaskDrivenRetrievalTests(unittest.TestCase):
    """第四步：预算按中文实算、长记忆切片、一跳关联、任务价值排序、来源化注入。"""

    def setUp(self) -> None:
        # 新版语义记录默认对召回不可见（开关默认关闭），这一组要显式打开。
        env = mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repo = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repo.close)
        self.writer = MemoryWriter(self.repo, Embed())
        self.scope = Scope("user", "desktop", "current")
        self.retriever = MemoryRetriever(self.repo, Embed())

    def _store(self, candidate, turn_id="t1", **kwargs):
        return seed_semantic(self.writer, self.scope, [candidate], turn_id=turn_id, **kwargs)[0]

    def test_chinese_budget_counts_one_unit_per_character(self) -> None:
        content = "决定" * 50  # 100 个汉字
        record = self._store(_decision(content))
        fits = self.retriever.retrieve(self.scope, "决定", token_budget=220)
        self.assertIn(record["id"], [item["id"] for item in fits.warm])
        tight = self.retriever.retrieve(self.scope, "决定", token_budget=50)
        self.assertNotIn(record["id"], [item["id"] for item in tight.warm])

    def test_long_memory_keeps_the_relevant_fragment(self) -> None:
        head = "早期讨论：先把候选提炼放队列。" * 40
        tail = "失败条件：向量库不可写时必须停在待处理，不要当成已完成。"
        record = self._store(_decision(head + tail))
        result = self.retriever.retrieve(self.scope, "失败条件")
        kept = [item for item in result.warm if item["id"] == record["id"]]
        self.assertEqual(len(kept), 1)
        self.assertIn("失败条件", kept[0]["content"])
        self.assertTrue(kept[0].get("content_sliced"))
        self.assertLess(len(kept[0]["content"]), len(head + tail))

    def test_long_memory_with_no_relevant_fragment_is_not_selected(self) -> None:
        record = self._store(_decision("与检索词无关的长篇内容。" * 60))
        result = self.retriever.retrieve(self.scope, "队列")
        self.assertNotIn(record["id"], [item["id"] for item in result.warm])
        result_ids = [entry["id"] for entry in result.metadata["dropped_unrelated_long"]]
        self.assertIn(record["id"], result_ids)

    def test_one_hop_relation_is_expanded_and_reauthorized(self) -> None:
        related = self._store(_decision("把召回改成按当前任务选", topics=("记忆",)), turn_id="t2")
        seed = self._store(
            _decision("队列先落库再提炼", related_ids=(related["id"],)), turn_id="t3"
        )
        # 另一会话里的记录不能被关联带进来。
        other_scope_record = seed_semantic(self.writer,
            Scope("user", "desktop", "other"), [_decision("别的会话的私密决定")], turn_id="t4"
        )[0]

        result = self.retriever.retrieve(self.scope, "队列")
        warm_ids = [item["id"] for item in result.warm]
        self.assertIn(seed["id"], warm_ids)
        self.assertIn(related["id"], warm_ids)
        self.assertNotIn(other_scope_record["id"], warm_ids)

    def test_recent_history_duplicate_is_not_injected_twice(self) -> None:
        self.writer.append_message(self.scope, "user", "先把候选提炼放进队列")
        record = self.writer.save_memory(
            self.scope, "先把候选提炼放进队列", conflict_key="user.task.current", confidence=0.9,
        )
        result = self.retriever.retrieve(self.scope, "候选提炼")
        self.assertNotIn(record["id"], [item["id"] for item in result.warm])
        elided = [entry["id"] for entry in result.metadata["elided_duplicates"]]
        self.assertIn(record["id"], elided)

    def test_pronoun_question_uses_the_previous_user_message(self) -> None:
        record = self._store(_decision("记忆和搜索用同一份处理包"))
        result = self.retriever.retrieve(
            self.scope,
            "这个怎么和搜索结合",
            history=[{"role": "user", "content": "记忆提炼和召回要怎么接起来"}],
        )
        self.assertTrue(result.metadata["reference_resolved"])
        self.assertIn(record["id"], [item["id"] for item in result.warm])

    def test_query_rewriter_runs_at_most_once(self) -> None:
        calls: list[str] = []

        def rewriter(query: str) -> str:
            calls.append(query)
            return query + " 记忆"

        self.retriever.retrieve(self.scope, "方案怎么落地", query_rewriter=rewriter)
        self.assertEqual(len(calls), 1)
        result = self.retriever.retrieve(self.scope, "方案怎么落地")
        self.assertEqual(result.metadata["model_rewrites"], 0)

    def test_accepted_decision_outranks_a_recent_conversational_candidate(self) -> None:
        recent = self.writer.save_memory(
            self.scope, "队列要先落库再提炼", conflict_key="user.task.current", confidence=0.5,
        )
        decision = self._store(
            _decision("队列要先落库再提炼", content_status="accepted", source_types=("user_statement",)),
            turn_id="t5",
        )
        result = self.retriever.retrieve(self.scope, "队列")
        ids = [item["id"] for item in result.warm]
        self.assertIn(decision["id"], ids)
        self.assertIn(recent["id"], ids)
        self.assertLess(ids.index(decision["id"]), ids.index(recent["id"]))

    def test_injected_lines_carry_type_status_reason_and_id(self) -> None:
        record = self._store(
            _decision("队列要先落库再提炼", reason="不想重复造记忆"),
        )
        context = self.retriever.build_context(self.scope, "队列")
        self.assertIn("decision/proposed", context.injected_text)
        self.assertIn("原因：不想重复造记忆", context.injected_text)
        self.assertIn(str(record["id"])[:8], context.injected_text)

    def test_hypothesis_is_marked_unverified_in_the_injection(self) -> None:
        self._store(_decision("也许该把召回改成两阶段", kind="hypothesis", content_status="untested"))
        context = self.retriever.build_context(self.scope, "召回")
        self.assertIn("未验证", context.injected_text)

    def test_new_record_is_invisible_when_recall_switch_is_off(self) -> None:
        """关掉召回开关就回到旧行为：新记录不参与检索，也不半途露出来。"""
        record = self._store(_decision("队列要先落库再提炼"))
        off = mock.patch.dict(os.environ, {"MEMORY_SEMANTIC_ENABLED": "false"})
        off.start()
        try:
            result = self.retriever.retrieve(self.scope, "队列")
        finally:
            off.stop()
        self.assertNotIn(record["id"], [item["id"] for item in result.warm])

    def test_superseded_decision_only_appears_as_history(self) -> None:
        old = self._store(_decision("旧方案：先做全量重提", content_status="accepted", source_types=("user_statement",)))
        import asyncio
        from datetime import UTC, datetime
        from agent_core.memory_processing import MemoryReviewProcessor
        from agent_core.memory_turn import turn_from_messages
        from support.memory_review import FixedReviewProvider, supported_row
        packet = turn_from_messages(turn_id='t6', occurred_at=datetime.now(UTC).isoformat(), scope=self.scope,
            user_text='新方案：只做增量候选，不再做全量重提', published_reply='')
        candidate = _decision(
                "新方案：只做增量候选",
                content_status="accepted",
                source_types=("user_statement",),
                supersedes=(old["id"],),
                source_refs=('t6',),
            )
        provider = FixedReviewProvider([supported_row('新方案：只做增量候选',
            [('t6', '新方案：只做增量候选'), ('t6', '不再做全量重提')], subject='候选提炼方案')])
        candidates, _, result = asyncio.run(MemoryReviewProcessor(self.writer, provider).prepare(
            packet, [candidate], persist=lambda _: None))
        self.assertTrue(candidates, result.as_dict())
        self.writer.save_semantic_candidates(self.scope, candidates, turn_id='t6', packet=packet, known_ids=(old['id'],))
        context = self.retriever.build_context(self.scope, "方案")
        warm_section = context.injected_text.split("[HISTORICAL_EVIDENCE]")[0]
        self.assertNotIn("旧方案：先做全量重提", warm_section)
        self.assertIn("旧方案：先做全量重提", context.injected_text)
