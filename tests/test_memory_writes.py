from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import json
import sqlite3
import tempfile, shutil
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_extract import ExtractedMemory
from agent_core.memory_repositories import VECTOR_PAYLOAD_VERSION, initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryCandidateError
from agent_core.memory_writes import (
    COMPANION_KEEP,
    MemoryWriter,
)


class FakeEmbeddingProvider:
    def __init__(self, fail: bool = False) -> None: self.fail = fail
    def embed(self, text: str) -> list[float]:
        if self.fail: raise RuntimeError("embedding unavailable")
        return [0.1, 0.2, 0.3]


def _expiry_seconds(record, now: datetime) -> float:
    """记录里 expires_at 相对基准 now 的秒数（验证 importance 插值）。"""
    return (datetime.fromisoformat(record["expires_at"]) - now).total_seconds()


class MemoryWritesTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.scope = Scope("user", "desktop", "conversation")

    def test_confirmed_fact_archives_conflicting_fact_in_one_transaction(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        old = writer.save_fact(self.scope, "lives in Beijing", confidence=0.9, conflict_key="home")
        new = writer.save_fact(self.scope, "lives in Shanghai", confidence=0.9, conflict_key="home")
        records = writer.list_records(self.scope)
        by_id = {record["id"]: record for record in records}
        self.assertEqual(by_id[new["id"]]["status"], "confirmed")
        self.assertEqual(by_id[old["id"]]["status"], "archived")
        self.assertEqual(by_id[old["id"]]["layer"], "cold")
        self.assertEqual(self.repositories.vector_client.retrieve("warm_memories_vectors", [old["id"]]), [])
        self.assertEqual(len(self.repositories.vector_client.retrieve("cold_memories_vectors", [old["id"]])), 1)

    def test_user_fact_conflicts_across_conversations(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        old = writer.save_fact(self.scope, "Beijing", confidence=.9, conflict_key="home")
        writer.save_fact(Scope("user", "desktop", "next"), "Shanghai", confidence=.9, conflict_key="home")
        self.assertEqual(writer.list_records(self.scope)[0]["status"], "archived")

    def test_successful_embedding_marks_the_current_payload_version(self) -> None:
        """写入成功时必须带上当前版本号。

        启动时的回填只处理版本号落后的记录。这条写入路径要是漏标，
        每写一条就会让下一次启动多背一次回填，等于把刚省下的时间又还回去。
        """
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        record = writer.save_fact(self.scope, "lives in Beijing", confidence=.9, conflict_key="home")
        with self.repositories.connect() as db:
            row = db.execute(
                "SELECT embedding_status, vector_payload_version FROM memory_records WHERE id=?",
                (record["id"],),
            ).fetchone()
        self.assertEqual(row["embedding_status"], "ready")
        self.assertEqual(row["vector_payload_version"], VECTOR_PAYLOAD_VERSION)

    def test_trigger_keeps_fts_and_idempotency_key_keeps_original_record(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        first = writer.append_message(self.scope, "user", "find the blue notebook", idempotency_key="msg-1")
        repeated = writer.append_message(self.scope, "user", "changed", idempotency_key="msg-1")
        self.assertEqual(first["id"], repeated["id"])
        with sqlite3.connect(self.repositories.db_path) as db:
            match = db.execute("SELECT content FROM memory_records_fts WHERE memory_records_fts MATCH 'notebook'").fetchone()
            self.assertEqual(match, ("find the blue notebook",))

    def test_candidate_confirmation_and_embedding_retry(self) -> None:
        failing = MemoryWriter(self.repositories, FakeEmbeddingProvider(fail=True))
        candidate = failing.save_candidate(self.scope, "likes tea", confidence=0.6, conflict_key="drink")
        self.assertEqual(candidate["embedding_status"], "failed")
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        confirmed = writer.confirm_candidate(self.scope, candidate["id"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(writer.sync_pending_embeddings()["ready"], 0)
        self.assertEqual(writer.get_memory_state_overview(self.scope)["embedding"]["ready"], 1)

    def test_save_extracted_memories_persists_real_rows_with_provenance(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        memories = [
            ExtractedMemory(
                content="用户最近在赶开题报告",
                conflict_key="user.task.current",
                confidence=0.9,
                importance=0.8,
                duration="session",
            )
        ]

        records = writer.save_extracted_memories(
            self.scope,
            memories,
            user_text="我最近在赶开题报告",
            assistant_reply="我记住了。",
            turn_id="turn-123",
            run_id="run-456",
        )

        self.assertEqual(len(records), 1)
        row = self.repositories.records_by_ids([records[0]["id"]])[records[0]["id"]]
        metadata = json.loads(row["metadata"])
        self.assertEqual(row["content"], "用户最近在赶开题报告")
        self.assertEqual(row["layer"], "warm")
        self.assertEqual(metadata["source"], "web.async_memory_extraction")
        self.assertEqual(metadata["turn_id"], "turn-123")
        self.assertEqual(metadata["run_id"], "run-456")
        self.assertEqual(metadata["source_type"], "llm_extracted")
        self.assertEqual(metadata["user_text"], "我最近在赶开题报告")
        self.assertEqual(metadata["assistant_reply"], "我记住了。")
        self.assertEqual(
            self.repositories.vector_client.retrieve("warm_memories_vectors", [records[0]["id"]], with_payload=True)[0].payload["memory_id"],
            records[0]["id"],
        )

    # ---------- 双槽陪伴记忆：slot 映射 / importance 过期 / N-keep / 到期失效 ----------

    def test_save_memory_passes_through_semantic_key_and_confidence(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        task = writer.save_memory(self.scope, "正在赶开题报告", conflict_key="user.task.current", confidence=0.9, importance=0.8, duration="session")
        self.assertEqual(task["conflict_key"], "user.task.current")  # 语义键透传，不被 duration 改写
        self.assertEqual((task["kind"], task["status"]), ("fact", "confirmed"))
        low = writer.save_memory(self.scope, "有点累", conflict_key="user.state.mood", confidence=0.5, importance=0.6, duration="instant")
        self.assertEqual(low["conflict_key"], "user.state.mood")
        self.assertEqual((low["kind"], low["status"]), ("candidate", "candidate"))
        habit = writer.save_memory(self.scope, "习惯熬夜", conflict_key="user.habit.schedule", confidence=0.8, importance=0.7, duration="habit")
        self.assertEqual(habit["conflict_key"], "user.habit.schedule")

    def test_save_memory_importance_drives_expiry(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        now = datetime.now(UTC)
        short0 = writer.save_memory(self.scope, "状态0", conflict_key="user.state.mood", confidence=0.9, importance=0.0, duration="session")
        short1 = writer.save_memory(self.scope, "状态1", conflict_key="user.state.mood", confidence=0.9, importance=1.0, duration="session")
        long0 = writer.save_memory(self.scope, "记忆0", conflict_key="user.preference.likes", confidence=0.9, importance=0.0, duration="long_term")
        long1 = writer.save_memory(self.scope, "记忆1", conflict_key="user.preference.likes", confidence=0.9, importance=1.0, duration="long_term")
        tol = 120
        self.assertAlmostEqual(_expiry_seconds(short0, now), 3 * 3600, delta=tol)
        self.assertAlmostEqual(_expiry_seconds(short1, now), 48 * 3600, delta=tol)
        self.assertAlmostEqual(_expiry_seconds(long0, now), 30 * 86400, delta=tol)
        self.assertAlmostEqual(_expiry_seconds(long1, now), 180 * 86400, delta=tol)
        self.assertLess(short0["expires_at"], short1["expires_at"])
        self.assertLess(long0["expires_at"], long1["expires_at"])

    def test_save_memory_keeps_recent_n_per_semantic_key(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        self.assertEqual(COMPANION_KEEP["user.state.mood"], 1)  # 情绪键只留最新一条
        for i in range(3):
            writer.save_memory(self.scope, f"情绪{i}", conflict_key="user.state.mood", confidence=0.9, importance=0.5, duration="session")
        with sqlite3.connect(self.repositories.db_path) as db:
            db.row_factory = sqlite3.Row
            mood_warm = db.execute(
                "SELECT id, status, layer FROM memory_records WHERE conflict_key=? AND layer='warm'", ("user.state.mood",)
            ).fetchall()
            mood_cold = db.execute(
                "SELECT id, status, layer FROM memory_records WHERE conflict_key=? AND layer='cold'", ("user.state.mood",)
            ).fetchall()
        self.assertEqual(len(mood_warm), 1)
        self.assertEqual(mood_warm[0]["status"], "confirmed")
        self.assertEqual(len(mood_cold), 2)

        self.assertEqual(COMPANION_KEEP["user.task.current"], 3)  # 当前任务可并行多条
        for i in range(5):
            writer.save_memory(self.scope, f"任务{i}", conflict_key="user.task.current", confidence=0.9, importance=0.5, duration="session")
        with sqlite3.connect(self.repositories.db_path) as db:
            db.row_factory = sqlite3.Row
            task_warm = db.execute(
                "SELECT id, status FROM memory_records WHERE conflict_key=? AND layer='warm'", ("user.task.current",)
            ).fetchall()
            task_cold = db.execute(
                "SELECT id, status FROM memory_records WHERE conflict_key=? AND layer='cold'", ("user.task.current",)
            ).fetchall()
        self.assertEqual(len(task_warm), 3)
        self.assertEqual([r["status"] for r in task_warm], ["confirmed"] * 3)
        self.assertEqual(len(task_cold), 2)

        self.assertEqual(COMPANION_KEEP["user.preference.likes"], 5)  # 长期偏好留证据
        for i in range(7):
            writer.save_memory(self.scope, f"喜好{i}", conflict_key="user.preference.likes", confidence=0.9, importance=0.5, duration="long_term")
        with sqlite3.connect(self.repositories.db_path) as db:
            db.row_factory = sqlite3.Row
            likes_warm = db.execute(
                "SELECT id, status FROM memory_records WHERE conflict_key=? AND layer='warm'", ("user.preference.likes",)
            ).fetchall()
            likes_cold = db.execute(
                "SELECT id, status FROM memory_records WHERE conflict_key=? AND layer='cold'", ("user.preference.likes",)
            ).fetchall()
        self.assertEqual(len(likes_warm), 5)
        self.assertEqual([r["status"] for r in likes_warm], ["confirmed"] * 5)
        self.assertEqual(len(likes_cold), 2)

    def test_different_dimensions_do_not_evict_each_other(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        # 同一轮对话里情绪 + 任务两个维度：互不挤兑，都留在 warm
        mood1 = writer.save_memory(self.scope, "现在很烦躁", conflict_key="user.state.mood", confidence=0.9, importance=0.8, duration="instant")
        task1 = writer.save_memory(self.scope, "在赶开题报告", conflict_key="user.task.current", confidence=0.9, importance=0.8, duration="session")
        rows = {r["id"]: r for r in writer.list_records(self.scope)}
        self.assertEqual((rows[mood1["id"]]["status"], rows[mood1["id"]]["layer"]), ("confirmed", "warm"))
        self.assertEqual((rows[task1["id"]]["status"], rows[task1["id"]]["layer"]), ("confirmed", "warm"))

        # 再来一条情绪：旧情绪归档到 cold，任务不受影响
        mood2 = writer.save_memory(self.scope, "现在很平静", conflict_key="user.state.mood", confidence=0.9, importance=0.8, duration="instant")
        rows = {r["id"]: r for r in writer.list_records(self.scope)}
        self.assertEqual((rows[mood1["id"]]["status"], rows[mood1["id"]]["layer"]), ("archived", "cold"))
        self.assertEqual((rows[mood2["id"]]["status"], rows[mood2["id"]]["layer"]), ("confirmed", "warm"))
        self.assertEqual((rows[task1["id"]]["status"], rows[task1["id"]]["layer"]), ("confirmed", "warm"))

    def test_save_memory_expires_via_maintenance_keeping_real_expiry(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        rec = writer.save_memory(self.scope, "临时情绪", conflict_key="user.state.mood", confidence=0.9, importance=0.0, duration="session")
        original_expiry = rec["expires_at"]
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(hours=4))
        row = next(r for r in writer.list_records(self.scope) if r["id"] == rec["id"])
        self.assertEqual(row["status"], "expired")
        self.assertEqual(row["deletion_reason"], "expired")
        self.assertEqual(row["expires_at"], original_expiry)  # 保留真实到期时间，不被维护覆盖

    def test_archived_slot_memory_expires_by_own_expiry_in_cold(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        old = writer.save_memory(self.scope, "现在很烦躁", conflict_key="user.state.mood", confidence=0.9, importance=0.0, duration="session")
        newer = writer.save_memory(self.scope, "现在很平静", conflict_key="user.state.mood", confidence=0.9, importance=0.0, duration="session")
        # 旧短期被替换归档到 cold，保留自己的真实 expires_at（约 3 小时），而不是等 180 天冷保留
        rows = {r["id"]: r for r in writer.list_records(self.scope)}
        self.assertEqual((rows[old["id"]]["layer"], rows[old["id"]]["status"]), ("cold", "archived"))
        # 维护时刻已过旧记录到期时间（但远未到 180 天）：两条都按自身 expires_at 失效
        writer.run_maintenance(now=datetime.fromisoformat(old["expires_at"]) + timedelta(hours=1))
        rows = {r["id"]: r for r in writer.list_records(self.scope)}
        self.assertEqual(rows[old["id"]]["status"], "expired")
        self.assertEqual(rows[old["id"]]["deletion_reason"], "expired")
        self.assertEqual(rows[old["id"]]["expires_at"], old["expires_at"])  # 真实到期时间不被维护覆盖
        self.assertEqual(rows[newer["id"]]["status"], "expired")
        self.assertEqual(rows[newer["id"]]["deletion_reason"], "expired")

    def test_archived_legacy_fact_without_expiry_keeps_cold_retention(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        writer.save_fact(self.scope, "旧事实", confidence=0.9, conflict_key="legacy")
        writer.save_fact(self.scope, "新事实", confidence=0.9, conflict_key="legacy")
        # 旧式 fact 归档到 cold 时不带 expires_at，不应被"自身到期"通道提前清掉（仍走 180 天冷保留）
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(hours=4))
        by_content = {r["content"]: r for r in writer.list_records(self.scope)}
        self.assertEqual(by_content["旧事实"]["status"], "archived")
        self.assertEqual(by_content["新事实"]["status"], "confirmed")


class OccurredAtTests(unittest.TestCase):
    """云端补提炼历史对话时，寿命必须从对话发生时间算，不是从收到时间算。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.scope = Scope("user", "web", "c1")

    def test_created_at_and_expiry_follow_the_conversation_time(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        spoken_at = datetime.now(UTC) - timedelta(days=3)
        record = writer.save_memory(
            self.scope,
            "用户那天很低落",
            conflict_key="user.state.mood",
            confidence=0.6,
            importance=0.4,
            duration="instant",
            occurred_at=spoken_at.isoformat(timespec="milliseconds"),
        )
        self.assertEqual(record["created_at"][:10], spoken_at.isoformat()[:10])
        self.assertLess(
            datetime.fromisoformat(record["expires_at"]),
            datetime.now(UTC),
            "三小时寿命的瞬时情绪，三天前的对话现在就该是过期状态",
        )

    def test_unparsable_occurred_at_falls_back_to_now(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        record = writer.save_memory(
            self.scope, "随便一句", conflict_key="k", confidence=0.9, occurred_at="not-a-time",
        )
        self.assertTrue(record["created_at"])
        self.assertTrue(record["expires_at"])

    def test_late_arriving_old_memory_does_not_evict_the_newer_one(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        fresh = writer.save_memory(
            self.scope, "今天心情不错", conflict_key="user.state.mood", confidence=0.6, importance=0.4,
        )
        late = writer.save_memory(
            self.scope,
            "三天前那会儿很低落",
            conflict_key="user.state.mood",
            confidence=0.6,
            importance=0.4,
            occurred_at=(datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="milliseconds"),
        )
        by_id = {r["id"]: r for r in writer.list_records(self.scope)}
        self.assertEqual(by_id[fresh["id"]]["status"], "candidate", "较新的情绪不能被迟到的旧情绪挤走")
        self.assertEqual(by_id[late["id"]]["status"], "archived", "迟到的旧情绪归档的是它自己")


class ChangeHookTests(unittest.TestCase):
    """变更通知是云端同步的基础；它必须发得全，而且绝不能反过来搞崩写入。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.scope = Scope("user", "web", "c1")

    def test_无回调时行为与以前一致(self) -> None:
        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())
        record = writer.save_memory(self.scope, "一句话", conflict_key="k", confidence=0.9)
        self.assertTrue(record["id"])

    def test_new_memory_and_rotation_both_emit(self) -> None:
        events: list[tuple[str, str]] = []
        writer = MemoryWriter(
            self.repositories, FakeEmbeddingProvider(), on_change=lambda action, rec: events.append((action, rec["id"])),
        )
        # 用只留最新一条的即时情绪槽位，第二次写入必然触发轮转归档
        first = writer.save_memory(self.scope, "第一句", conflict_key="user.state.mood", confidence=0.9)
        second = writer.save_memory(self.scope, "第二句", conflict_key="user.state.mood", confidence=0.9)
        emitted = [rid for action, rid in events if action == "upsert"]
        self.assertIn(first["id"], emitted)
        self.assertIn(second["id"], emitted)
        # 第一条被轮转归档，归档也要发出变更，否则云端不知道它变了
        self.assertEqual(emitted.count(first["id"]), 2)

    def test_hook_exception_never_breaks_the_write(self) -> None:
        def boom(action, record):
            raise RuntimeError("通知通道坏了")

        writer = MemoryWriter(self.repositories, FakeEmbeddingProvider(), on_change=boom)
        record = writer.save_memory(self.scope, "照样要写进去", conflict_key="k", confidence=0.9)
        stored = {r["id"]: r for r in writer.list_records(self.scope)}
        self.assertIn(record["id"], stored)

    def test_maintenance_transitions_emit_and_purge_emits_delete(self) -> None:
        events: list[tuple[str, str]] = []
        writer = MemoryWriter(
            self.repositories, FakeEmbeddingProvider(), on_change=lambda action, rec: events.append((action, rec["id"])),
        )
        record = writer.save_memory(
            self.scope,
            "很久以前的事",
            conflict_key="user.state.mood",
            confidence=0.6,
            importance=0.4,
            duration="instant",
            occurred_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="milliseconds"),
        )
        writer.run_maintenance(now=datetime.now(UTC))
        actions = [action for action, rid in events if rid == record["id"]]
        self.assertIn("upsert", actions, "迁移/到期都要通知云端")

        # 审计期过了之后真正清除，必须发 delete 而不是 upsert
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=400))
        self.assertIn("delete", [action for action, rid in events if rid == record["id"]])


def _decision(**overrides) -> MemoryCandidate:
    base = dict(
        content="让主回答模型在回答发布后参与记忆候选提炼",
        kind="decision",
        content_status="proposed",
        source_types=("assistant_suggestion",),
        reason="不想陪伴记忆和项目记忆分裂",
        scope_kind="project",
        project_id="dm-agent",
    )
    base.update(overrides)
    return MemoryCandidate(**base)


class SemanticMemoryWriteTests(unittest.TestCase):
    """新语义记录（七类 kind + 独立 content_status）的写入行为。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(
            root / "memory.db", root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.scope = Scope("user", "desktop", "conversation")
        self.writer = MemoryWriter(self.repositories, FakeEmbeddingProvider())

    def _rows(self) -> dict[str, dict]:
        return {row["id"]: row for row in self.writer.list_records(self.scope)}

    def test_semantic_record_keeps_kind_status_and_project(self) -> None:
        record = seed_semantic(self.writer,
            self.scope, [_decision()], turn_id="turn-1", project_binding="dm-agent"
        )[0]
        stored = self._rows()[record["id"]]
        self.assertEqual(stored["kind"], "decision")
        self.assertEqual(stored["content_status"], "proposed")
        self.assertEqual(stored["semantics_version"], 1)
        self.assertEqual(stored["scope_kind"], "project")
        self.assertEqual(stored["project_id"], "dm-agent")
        # 记录本身有效 = 生命周期 status；内容进展另有独立字段，两者不再混用。
        self.assertEqual(stored["status"], "active")
        self.assertEqual(stored["layer"], "warm")
        metadata = json.loads(stored["metadata"])
        self.assertEqual(metadata["reason"], "不想陪伴记忆和项目记忆分裂")
        self.assertEqual(metadata["source_types"], ["assistant_suggestion"])
        self.assertTrue(metadata["stable_key"])

    def test_model_candidate_cannot_claim_a_completed_plan(self) -> None:
        before = len(self._rows())
        with self.assertRaises(MemoryCandidateError):
            self.writer.save_semantic_candidates(
                self.scope,
                [_decision(kind="plan", content_status="completed")],
                turn_id="turn-2",
            )
        self.assertEqual(len(self._rows()), before, "不合规候选不得留下半条记录")

    def test_retry_with_reordered_candidates_does_not_duplicate(self) -> None:
        first = seed_semantic(self.writer,
            self.scope,
            [_decision(), _decision(content="记忆按当前任务召回", topics=("记忆",))],
            turn_id="turn-7",
        )
        second = seed_semantic(self.writer,
            self.scope,
            [
                _decision(content="记忆按当前任务召回", topics=("记忆",)),
                _decision(),
            ],
            turn_id="turn-7",
        )
        self.assertEqual(sorted(r["id"] for r in first), sorted(r["id"] for r in second))
        self.assertEqual(len(self._rows()), 2)

    def test_identical_candidate_is_merged_instead_of_stored_twice(self) -> None:
        first = seed_semantic(self.writer, self.scope, [_decision()], turn_id="t-a")[0]
        again = seed_semantic(self.writer, self.scope, [_decision()], turn_id="t-b")[0]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self._rows()), 1)

    def test_late_record_does_not_override_the_newer_decision(self) -> None:
        newer = seed_semantic(self.writer,
            self.scope,
            [
                _decision(
                    content_status="accepted",
                    source_types=("user_statement",),
                    occurred_at="2026-10-02T09:00:00+00:00",
                )
            ],
            turn_id="turn-new",
        )[0]
        seed_semantic(self.writer,
            self.scope,
            [_decision(occurred_at="2026-09-20T09:00:00+00:00")],
            turn_id="turn-late",
        )
        stored = self._rows()[newer["id"]]
        self.assertEqual(stored["content_status"], "accepted")
        self.assertEqual(stored["status"], "active")

    def test_explicit_supersede_archives_the_previous_decision(self) -> None:
        old = seed_semantic(self.writer,
            self.scope,
            [_decision(content_status="accepted", source_types=("user_statement",))],
            turn_id="turn-3",
        )[0]
        import asyncio
        from datetime import UTC, datetime
        from agent_core.memory_processing import MemoryReviewProcessor
        from agent_core.memory_turn import turn_from_messages
        from support.memory_review import FixedReviewProvider, supported_row
        packet = turn_from_messages(turn_id='turn-4', occurred_at=datetime.now(UTC).isoformat(), scope=self.scope,
            user_text='改成记忆只在本地提炼，不再采用旧提炼方案', published_reply='')
        candidate = _decision(
                    content="改成记忆只在本地提炼",
                    content_status="accepted",
                    source_types=("user_statement",),
                    supersedes=(old["id"],),
                    source_refs=('turn-4',),
                )
        provider = FixedReviewProvider([supported_row('改成记忆只在本地提炼',
            [('turn-4', '改成记忆只在本地提炼'), ('turn-4', '不再采用旧提炼方案')], subject='记忆提炼方案')])
        candidates, _, result = asyncio.run(MemoryReviewProcessor(self.writer, provider).prepare(
            packet, [candidate], persist=lambda _: None))
        self.assertTrue(candidates, result.as_dict())
        new = self.writer.save_semantic_candidates(self.scope, candidates,
            turn_id="turn-4",
            known_ids=(old["id"],),
            packet=packet,
        )[0]
        rows = self._rows()
        self.assertEqual(rows[old["id"]]["status"], "archived")
        self.assertEqual(rows[old["id"]]["content_status"], "superseded")
        self.assertEqual(rows[new["id"]]["status"], "active")
        self.assertEqual(rows[new["id"]]["content_status"], "accepted")

    def test_relations_must_point_at_known_ids(self) -> None:
        """关系只能指向输入里已有且有权限的记忆 ID。"""
        with self.assertRaises(MemoryCandidateError):
            seed_semantic(self.writer,
                self.scope,
                [_decision(related_ids=("mem-missing",))],
                turn_id="turn-5",
                known_ids=(),
            )

    def test_old_confirmed_facts_stay_readable_next_to_new_records(self) -> None:
        """升级前后同一张表里两种格式都要能读：旧记录一律保持未知。"""
        old = self.writer.save_fact(self.scope, "用户在上海", confidence=0.9, conflict_key="home")
        new = seed_semantic(self.writer, self.scope, [_decision()], turn_id="turn-6")[0]
        rows = self._rows()
        self.assertEqual(rows[old["id"]]["semantics_version"], 0)
        self.assertEqual(rows[old["id"]]["status"], "confirmed")
        self.assertEqual(rows[old["id"]]["content_status"], None)
        self.assertEqual(rows[new["id"]]["semantics_version"], 1)

    def test_vector_payload_carries_project_and_content_status(self) -> None:
        record = seed_semantic(self.writer,
            self.scope, [_decision()], turn_id="turn-8", project_binding="dm-agent"
        )[0]
        points = self.repositories.vector_client.retrieve(
            "warm_memories_vectors", [record["id"]], with_payload=True
        )
        self.assertEqual(len(points), 1)
        payload = points[0].payload
        self.assertEqual(payload["project_id"], "dm-agent")
        self.assertEqual(payload["content_status"], "proposed")
        self.assertEqual(payload["scope_kind"], "project")
        self.assertEqual(payload["owner_id"], self.scope.owner_id)

    def test_untrusted_project_name_falls_back_to_conversation_scope(self) -> None:
        """项目级读取权限不能靠候选自己填名字拿到：对不上可信绑定就降回会话范围。"""
        record = seed_semantic(self.writer,
            self.scope, [_decision()], turn_id="turn-12", project_binding=""
        )[0]
        stored = self._rows()[record["id"]]
        self.assertEqual(stored["scope_kind"], "conversation")
        self.assertIsNone(stored["project_id"])
        self.assertEqual(json.loads(stored["metadata"])["project_label"], "dm-agent")

    def test_matching_binding_keeps_project_scope(self) -> None:
        record = seed_semantic(self.writer,
            self.scope, [_decision()], turn_id="turn-13", project_binding="dm-agent"
        )[0]
        stored = self._rows()[record["id"]]
        self.assertEqual(stored["scope_kind"], "project")
        self.assertEqual(stored["project_id"], "dm-agent")

    def test_blank_content_is_rejected_before_touching_the_database(self) -> None:
        before = len(self._rows())
        with self.assertRaises(MemoryCandidateError):
            seed_semantic(self.writer,
                self.scope, [_decision(content="   ")], turn_id="turn-9"
            )
        self.assertEqual(len(self._rows()), before)
