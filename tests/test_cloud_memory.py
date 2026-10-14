from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_sync import apply_snapshot
from agent_core.memory_sync_store import MemorySyncStore
from cloud_memory import api
from cloud_memory.store import (
    MAX_ATTEMPTS,
    QUEUE_EMPTY,
    QUEUE_EXHAUSTED,
    QUEUE_FAILED,
    QUEUE_PROCESSING,
    QUEUE_STORED,
    CloudMemoryStore,
)


def _turn(turn_id: str = "t1", **overrides) -> dict:
    body = {
        "turn_id": turn_id,
        "owner_id": "local-user",
        "channel": "web",
        "conversation_id": "c1",
        "module_id": "companion",
        "user_text": "你好",
        "assistant_reply": "你好呀",
        "occurred_at": "2026-10-09T10:00:00.000+00:00",
    }
    body.update(overrides)
    return body


def _record(record_id: str = "rec-1", **overrides) -> dict:
    body = {
        "id": record_id,
        "owner_id": "local-user",
        "channel": "web",
        "conversation_id": "c1",
        "module_id": "companion",
        "visibility": "module",
        "layer": "warm",
        "kind": "fact",
        "status": "confirmed",
        "role": None,
        "content": "用户最爱喝热的拿铁",
        "metadata": "{}",
        "confidence": 0.9,
        "conflict_key": "user.preference.likes",
        "importance": 0.7,
        "expires_at": None,
        "purge_after": None,
        "retention_locked": 0,
        "deletion_reason": None,
        "archived_at": None,
        "created_at": "2026-10-01T00:00:00.000+00:00",
        "updated_at": "2026-10-01T00:00:00.000+00:00",
    }
    body.update(overrides)
    return body


class CloudQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = CloudMemoryStore(root / "cloud.db")
        self.store.initialize()

    def test_duplicate_upload_is_received_once(self) -> None:
        self.assertTrue(self.store.enqueue_turn(_turn("t1")))
        self.assertFalse(self.store.enqueue_turn(_turn("t1", user_text="改过的话")))

    def test_claimed_turn_is_marked_processing(self) -> None:
        self.store.enqueue_turn(_turn("t1"))
        claimed = self.store.claim_due()
        self.assertEqual([item["turn_id"] for item in claimed], ["t1"])
        self.assertEqual(self.store.queue_counts()[QUEUE_PROCESSING], 1)
        # 已经领走的不该被重复领
        self.assertEqual(self.store.claim_due(), [])

    def test_crash_midway_requeues_the_processing_turn(self) -> None:
        self.store.enqueue_turn(_turn("t1"))
        self.store.claim_due()
        self.assertEqual(self.store.requeue_stale(), 1)
        self.assertEqual(len(self.store.claim_due()), 1, "重启后中断的任务必须能被重新捡起")

    def test_failure_schedules_retry_instead_of_being_marked_done(self) -> None:
        self.store.enqueue_turn(_turn("t1"))
        self.store.claim_due()
        state = self.store.mark_failed("t1", "connection refused")
        self.assertEqual(state, QUEUE_FAILED)
        self.assertEqual(self.store.queue_counts().get(QUEUE_STORED, 0), 0)
        # 还没到点，不该被领走
        self.assertEqual(self.store.claim_due(), [])
        later = (datetime.now(UTC) + timedelta(seconds=60)).isoformat(timespec="milliseconds")
        self.assertEqual(len(self.store.claim_due(now=later)), 1)

    def test_retry_backoff_grows_then_gives_up_and_keeps_the_task(self) -> None:
        self.store.enqueue_turn(_turn("t1"))
        delays = []
        for _ in range(MAX_ATTEMPTS):
            self.store.claim_due()
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT next_retry_at FROM memory_ingest_queue WHERE turn_id='t1'"
                ).fetchone()
            self.store.mark_failed("t1", "boom")
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT next_retry_at,status FROM memory_ingest_queue WHERE turn_id='t1'"
                ).fetchone()
            delays.append((row["next_retry_at"], row["status"]))
        self.assertEqual(delays[-1][1], QUEUE_EXHAUSTED)
        self.assertIsNone(delays[-1][0])
        # 耗尽之后任务仍在库里，等人看 —— 丢掉才是最坏的选择
        self.assertEqual(self.store.queue_counts()[QUEUE_EXHAUSTED], 1)

    def test_empty_and_stored_are_different_states(self) -> None:
        self.store.enqueue_turn(_turn("t1"))
        self.store.enqueue_turn(_turn("t2"))
        self.store.claim_due()
        self.store.mark_empty("t1")
        self.store.mark_stored("t2", extracted=3, persisted=3)
        counts = self.store.queue_counts()
        self.assertEqual(counts[QUEUE_EMPTY], 1)
        self.assertEqual(counts[QUEUE_STORED], 1)


class AccessAndImportTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = CloudMemoryStore(root / "cloud.db")
        self.store.initialize()
        self.repositories = initialize_memory_repositories(root / "cloud.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)

    def _insert(self, record_id: str) -> None:
        with self.repositories.connect() as db:
            db.execute(
                "INSERT INTO memory_records(id,owner_id,channel,conversation_id,module_id,layer,kind,status,"
                "content,metadata,created_at,updated_at) VALUES(?,?,?,?,?,'warm','fact','confirmed',"
                "'内容','{}','2026-10-01T00:00:00.000+00:00','2026-10-01T00:00:00.000+00:00')",
                (record_id, "local-user", "web", "c1", "companion"),
            )

    def test_access_events_reach_the_record_so_rotation_can_judge_recency(self) -> None:
        self._insert("a")
        self.store.record_access([{"record_id": "a", "occurred_at": "2026-10-09T10:00:00.000+00:00"}])
        self.assertEqual(self.store.apply_access(), 1)
        with self.repositories.connect() as db:
            row = dict(db.execute("SELECT last_accessed_at,access_count FROM memory_records WHERE id='a'").fetchone())
        self.assertEqual(row["last_accessed_at"], "2026-10-09T10:00:00.000+00:00")
        self.assertEqual(row["access_count"], 1)

    def test_access_never_refreshes_updated_at(self) -> None:
        """访问不是内容变化。刷新 updated_at 会让每次访问都推一条变更给本地，
        本地看到"这条又更新了"，而它其实一个字都没变。"""
        self._insert("a")
        self.store.record_access([{"record_id": "a", "occurred_at": "2026-10-09T10:00:00.000+00:00"}])
        self.store.apply_access()
        with self.repositories.connect() as db:
            updated_at = db.execute("SELECT updated_at FROM memory_records WHERE id='a'").fetchone()[0]
        self.assertEqual(updated_at, "2026-10-01T00:00:00.000+00:00")

    def test_access_is_only_applied_once(self) -> None:
        self._insert("a")
        self.store.record_access([{"record_id": "a", "occurred_at": "2026-10-09T10:00:00.000+00:00"}])
        self.assertEqual(self.store.apply_access(), 1)
        self.assertEqual(self.store.apply_access(), 0, "重复并入会让访问次数凭空翻倍")

    def test_older_access_does_not_move_last_accessed_backwards(self) -> None:
        self._insert("a")
        self.store.record_access([{"record_id": "a", "occurred_at": "2026-10-09T12:00:00.000+00:00"}])
        self.store.apply_access()
        self.store.record_access([{"record_id": "a", "occurred_at": "2026-10-09T09:00:00.000+00:00"}])
        self.store.apply_access()
        with self.repositories.connect() as db:
            value = db.execute("SELECT last_accessed_at FROM memory_records WHERE id='a'").fetchone()[0]
        self.assertEqual(value, "2026-10-09T12:00:00.000+00:00", "记录的是最近一次使用，迟到的旧访问不能把它往回拨")

    def test_import_inserts_a_missing_record_and_leaves_a_change(self) -> None:
        outcome = self.store.import_record(_record("new-1"))
        self.assertEqual(outcome, "inserted")
        self.assertIn("new-1", self.store.existing_records())
        changes = self.store.changes_since(0)
        self.assertEqual([item["record_id"] for item in changes], ["new-1"])

    def test_import_is_a_no_op_when_content_already_matches(self) -> None:
        self.store.import_record(_record("new-1"))
        before = self.store.max_seq()
        self.assertEqual(self.store.import_record(_record("new-1")), "same")
        self.assertEqual(self.store.max_seq(), before, "内容一样就不该再产生一条变更")

    def test_import_updates_when_the_source_is_ahead(self) -> None:
        self.store.import_record(_record("new-1"))
        later = _record("new-1", content="本地改过的内容", updated_at="2026-10-05T00:00:00.000+00:00")
        self.assertEqual(self.store.import_record(later), "updated")
        self.assertEqual(self.store.existing_records()["new-1"]["content"], "本地改过的内容")

    def test_imported_records_are_marked_for_local_recompute_of_vectors(self) -> None:
        self.store.import_record(_record("new-1"))
        self.assertEqual(self.store.existing_records()["new-1"]["embedding_status"], "pending")

    def test_reconcile_separates_local_only_cloud_only_and_differing(self) -> None:
        from scripts.cloud_memory_baseline import reconcile

        local = {
            "both-same": {"id": "both-same", "content": "一样", "status": "confirmed", "importance": 0.5},
            "both-diff": {"id": "both-diff", "content": "本地版", "status": "confirmed", "importance": 0.5},
            "local-only": {"id": "local-only", "content": "只在本地", "status": "confirmed", "importance": 0.5},
        }
        cloud = {
            "both-same": {"id": "both-same", "content": "一样", "status": "confirmed", "importance": 0.5},
            "both-diff": {"id": "both-diff", "content": "云端版", "status": "confirmed", "importance": 0.5},
            "cloud-only": {"id": "cloud-only", "content": "只在云端", "status": "confirmed", "importance": 0.5},
        }
        report = reconcile(local, cloud)
        self.assertEqual(report["local_only"], ["local-only"])
        self.assertEqual(report["cloud_only"], ["cloud-only"], "云端独有必须被列出来，不能静默丢掉")
        self.assertEqual([item["id"] for item in report["differing"]], ["both-diff"])

    def test_reconcile_ignores_numeric_type_noise(self) -> None:
        from scripts.cloud_memory_baseline import reconcile

        local = {"a": {"id": "a", "importance": 0, "content": "x", "retention_locked": 0}}
        cloud = {"a": {"id": "a", "importance": 0.0, "content": "x", "retention_locked": 0.0}}
        self.assertEqual(reconcile(local, cloud)["differing"], [], "0 与 0.0 不该算差异")


class CloudChangesTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = CloudMemoryStore(root / "cloud.db")
        self.store.initialize()

    def test_change_numbers_stay_ordered(self) -> None:
        first = self.store.record_change("upsert", _record("a"))
        second = self.store.record_change("upsert", _record("b"))
        self.assertLess(first, second)
        self.assertEqual(self.store.max_seq(), second)

    def test_changes_since_returns_only_newer_ones(self) -> None:
        first = self.store.record_change("upsert", _record("a"))
        self.store.record_change("upsert", _record("b"))
        changes = self.store.changes_since(first)
        self.assertEqual([item["record_id"] for item in changes], ["b"])

    def test_changes_are_scoped_to_the_owner(self) -> None:
        self.store.record_change("upsert", _record("a", owner_id="alice"))
        self.store.record_change("upsert", _record("b", owner_id="bob"))
        alice = self.store.changes_since(0, owner_id="alice")
        self.assertEqual([item["record_id"] for item in alice], ["a"])

    def test_snapshot_checksum_is_accepted_by_the_local_verifier(self) -> None:
        """两边算校验值的方式必须逐字节一致，否则首次接入永远失败。"""
        records = [_record("rec-a"), _record("rec-b", content="另一条")]
        snapshot = self.store.snapshot(owner_id="local-user", records=records, cursor=self.store.max_seq())

        local_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(local_root, ignore_errors=True))
        repositories = initialize_memory_repositories(local_root / "local.db", local_root / "v", vector_size=3)
        self.addCleanup(repositories.close)
        local_store = MemorySyncStore(local_root / "local.db")
        local_store.initialize()

        result = apply_snapshot(repositories, local_store, snapshot)
        self.assertEqual(len(result["errors"]), 0)
        self.assertEqual(result["applied"], 2)
        self.assertEqual(local_store.change_cursor(), snapshot["cursor"])

    def test_tampered_snapshot_is_refused(self) -> None:
        records = [_record("rec-a")]
        snapshot = self.store.snapshot(owner_id="local-user", records=records, cursor=0)
        snapshot["changes"][0]["payload"]["content"] = "被改过"
        local_root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(local_root, ignore_errors=True))
        repositories = initialize_memory_repositories(local_root / "local.db", local_root / "v", vector_size=3)
        self.addCleanup(repositories.close)
        local_store = MemorySyncStore(local_root / "local.db")
        local_store.initialize()
        with self.assertRaises(ValueError):
            apply_snapshot(repositories, local_store, snapshot)

    def test_snapshot_carries_the_vector_so_local_need_not_recompute(self) -> None:
        records = [_record("rec-a")]
        self.store.record_change("upsert", _record("rec-a"), vector=[0.1, 0.2, 0.3], vector_layer="warm")
        snapshot = self.store.snapshot(owner_id="local-user", records=records, cursor=self.store.max_seq())
        self.assertEqual(snapshot["changes"][0]["vector"], [0.1, 0.2, 0.3])
        self.assertEqual(snapshot["changes"][0]["vector_layer"], "warm")


class CloudApiAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = CloudMemoryStore(root / "cloud.db")
        self.store.initialize()
        # memory_records 表由记忆仓库建立（生产环境里是工人启动时建的），
        # 这里一并建好，接口读的是真实的表结构而不是替身。
        self.repositories = initialize_memory_repositories(root / "cloud.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self._saved_store = api.store
        api.store = self.store
        self.addCleanup(lambda: setattr(api, "store", self._saved_store))
        from fastapi.testclient import TestClient

        self.client = TestClient(api.app)
        env = {"CLOUD_MEMORY_TOKENS": "tok-alice:alice,tok-bob:bob"}
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("CLOUD_INGEST_TOKEN",):
            os.environ.pop(key, None)

    def test_missing_or_wrong_token_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/memory/sync/status").status_code, 401)
        self.assertEqual(
            self.client.get("/api/memory/sync/status", headers={"Authorization": "Bearer nope"}).status_code,
            401,
        )

    def test_upload_from_another_account_is_rejected(self) -> None:
        response = self.client.post(
            "/api/memory/sync/events",
            json={"turns": [_turn("t1", owner_id="bob")]},
            headers={"Authorization": "Bearer tok-alice"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["accepted"], [])
        self.assertEqual(body["rejected"][0]["reason"], "账号越权")
        self.assertEqual(self.store.queue_counts().get("pending", 0), 0)

    def test_upload_confirm_means_persisted(self) -> None:
        response = self.client.post(
            "/api/memory/sync/events",
            json={"turns": [_turn("t1", owner_id="alice")]},
            headers={"Authorization": "Bearer tok-alice"},
        )
        self.assertEqual(response.json()["accepted"], ["t1"])
        self.assertEqual(self.store.queue_counts().get("pending", 0), 1)

    def test_the_processing_packet_survives_the_upload_hop(self) -> None:
        """本地上传是**带着处理包**的：接口少收这两个字段，云端就只剩一问一答。

        静默丢包的后果不是「少一点背景」：云端看不到本轮用过的旧记忆引用，
        更新建议就没有可指向的目标，纠正与替代在云端整条失效，而且不报错。
        """
        import json

        packet = {
            "turn_id": "t1",
            "user_text": "不是五千，是三千",
            "recalled_memory_refs": [{"memory_id": "m-1", "version": 1}],
            "project_binding": "trusted-project",
        }
        response = self.client.post(
            "/api/memory/sync/events",
            json={"turns": [_turn("t1", owner_id="alice", packet=packet, schema_version=1)]},
            headers={"Authorization": "Bearer tok-alice"},
        )
        self.assertEqual(response.json()["accepted"], ["t1"])

        with self.store.connect() as db:
            row = dict(
                db.execute(
                    "SELECT packet,schema_version FROM memory_ingest_queue WHERE turn_id='t1'"
                ).fetchone()
            )
        self.assertEqual(row["schema_version"], 1)
        stored = json.loads(row["packet"])
        self.assertEqual(stored["recalled_memory_refs"][0]["memory_id"], "m-1")
        self.assertEqual(stored["project_binding"], "trusted-project")

    def test_changes_never_leak_across_accounts(self) -> None:
        self.store.record_change("upsert", _record("a", owner_id="alice"))
        self.store.record_change("upsert", _record("b", owner_id="bob"))
        response = self.client.get(
            "/api/memory/sync/changes?since=0", headers={"Authorization": "Bearer tok-alice"}
        )
        record_ids = [item["record_id"] for item in response.json()["changes"]]
        self.assertEqual(record_ids, ["a"])

    def test_snapshot_never_leaks_across_accounts(self) -> None:
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO memory_records(id,owner_id,channel,conversation_id,module_id,layer,kind,status,"
                "content,metadata,created_at,updated_at) VALUES('a','alice','web','c1','companion','warm',"
                "'fact','confirmed','alice 的秘密','{}','2026-10-01T00:00:00.000+00:00','2026-10-01T00:00:00.000+00:00')"
            )
            db.execute(
                "INSERT INTO memory_records(id,owner_id,channel,conversation_id,module_id,layer,kind,status,"
                "content,metadata,created_at,updated_at) VALUES('b','bob','web','c1','companion','warm',"
                "'fact','confirmed','bob 的秘密','{}','2026-10-01T00:00:00.000+00:00','2026-10-01T00:00:00.000+00:00')"
            )
        response = self.client.get(
            "/api/memory/sync/snapshot", headers={"Authorization": "Bearer tok-alice"}
        )
        record_ids = [item["record_id"] for item in response.json()["changes"]]
        self.assertEqual(record_ids, ["a"])

    def test_status_reports_the_backlog(self) -> None:
        self.store.enqueue_turn(_turn("t1", owner_id="alice"))
        response = self.client.get(
            "/api/memory/sync/status", headers={"Authorization": "Bearer tok-alice"}
        )
        self.assertEqual(response.json()["queue_backlog"], 1)


if __name__ == "__main__":
    unittest.main()
