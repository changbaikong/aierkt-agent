from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_sync import (
    ACTION_DELETE,
    ACTION_UPSERT,
    MemorySyncRunner,
    apply_changes,
    apply_snapshot,
    build_runner_from_env,
    collect_access,
)
from agent_core.memory_sync_store import MemorySyncStore


def _rid() -> str:
    """云端记录编号是真 UUID（Qdrant 的点编号只认 UUID 或整数）。"""
    return str(uuid.uuid4())


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def _payload(**overrides) -> dict:
    base = {
        "content": "用户最爱喝热的拿铁",
        "metadata": "{}",
        "confidence": 0.9,
        "conflict_key": "user.preference.likes",
        "importance": 0.7,
        "layer": "warm",
        "kind": "fact",
        "status": "confirmed",
        "role": None,
        "visibility": "module",
        "expires_at": None,
        "purge_after": None,
        "retention_locked": 0,
        "deletion_reason": None,
        "archived_at": None,
        "owner_id": "user",
        "channel": "web",
        "conversation_id": "c1",
        "module_id": "companion",
        "created_at": "2026-10-01T00:00:00.000+00:00",
        "updated_at": "2026-10-01T00:00:00.000+00:00",
    }
    base.update(overrides)
    return base


def _change(seq: int, record_id: str, *, action: str = ACTION_UPSERT, payload: dict | None = None, **extra) -> dict:
    body = {
        "seq": seq,
        "record_id": record_id,
        "action": action,
        "updated_at": (payload or _payload()).get("updated_at"),
        "payload": payload or _payload(),
    }
    body.update(extra)
    return body


class SyncStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()
        self.scope = Scope("user", "web", "c1", "companion")

    def test_enqueue_turn_keeps_only_the_first_copy(self) -> None:
        self.assertTrue(self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好"))
        self.assertFalse(self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好"))
        self.assertEqual(len(self.store.pending_turns()), 1)

    def test_failed_upload_stays_pending_with_error_recorded(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好")
        self.store.mark_turn_failed("t1", "connection refused")
        pending = self.store.pending_turns()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertIn("connection refused", pending[0]["last_error"])

    def test_sent_turn_leaves_the_queue(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好")
        self.store.mark_turn_sent("t1")
        self.assertEqual(self.store.pending_turns(), [])
        self.assertEqual(self.store.upload_backlog(), 0)

    def test_access_records_dedupe_within_the_same_minute(self) -> None:
        first = self.store.record_access(self.scope, ["r1"], accessed_at="2026-10-09T10:00:05.000+00:00")
        second = self.store.record_access(self.scope, ["r1"], accessed_at="2026-10-09T10:00:55.000+00:00")
        third = self.store.record_access(self.scope, ["r1"], accessed_at="2026-10-09T10:01:00.000+00:00")
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(third, 1)

    def test_cursor_round_trips(self) -> None:
        self.assertEqual(self.store.change_cursor(), 0)
        self.store.set_state("change_cursor", "42")
        self.assertEqual(self.store.change_cursor(), 42)


class ApplyChangesTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()

    def _row(self, record_id: str) -> dict | None:
        with self.repositories.connect() as db:
            row = db.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone()
        return dict(row) if row else None

    def test_new_record_from_cloud_is_inserted(self) -> None:
        rid = _rid()
        result = apply_changes(self.repositories, self.store, [_change(1, rid)])
        self.assertEqual(result["applied"], 1)
        row = self._row(rid)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["content"], "用户最爱喝热的拿铁")
        self.assertEqual(row["owner_id"], "user")

    def test_local_newer_record_survives_cloud_overwrite(self) -> None:
        rid = _rid()
        apply_changes(self.repositories, self.store, [_change(1, rid)])
        newer = _iso(datetime.now(UTC) + timedelta(minutes=5))
        with self.repositories.connect() as db:
            db.execute(
                "UPDATE memory_records SET content='本地刚改的内容',updated_at=? WHERE id=?",
                (newer, rid),
            )
        stale_cloud = _change(2, rid, payload=_payload(content="云端十分钟前的旧内容"))
        result = apply_changes(self.repositories, self.store, [stale_cloud])
        self.assertEqual(result["skipped"], 1)
        row = self._row(rid)
        assert row is not None
        self.assertEqual(row["content"], "本地刚改的内容")

    def test_newer_cloud_record_does_overwrite(self) -> None:
        rid = _rid()
        apply_changes(self.repositories, self.store, [_change(1, rid)])
        future = _iso(datetime.now(UTC) + timedelta(minutes=5))
        payload = _payload(content="云端改过的内容", updated_at=future)
        result = apply_changes(self.repositories, self.store, [_change(2, rid, payload=payload)])
        self.assertEqual(result["applied"], 1)
        row = self._row(rid)
        assert row is not None
        self.assertEqual(row["content"], "云端改过的内容")

    def test_delete_archives_instead_of_removing_the_row(self) -> None:
        rid = _rid()
        apply_changes(self.repositories, self.store, [_change(1, rid)])
        future = _iso(datetime.now(UTC) + timedelta(minutes=5))
        result = apply_changes(
            self.repositories,
            self.store,
            [{"seq": 2, "record_id": rid, "action": ACTION_DELETE, "updated_at": future}],
        )
        self.assertEqual(result["applied"], 1)
        row = self._row(rid)
        self.assertIsNotNone(row, "删除必须走归档，行不能消失")
        assert row is not None
        self.assertEqual(row["status"], "archived")
        self.assertEqual(row["deletion_reason"], "cloud_deleted")

    def test_replaying_the_same_changes_is_idempotent(self) -> None:
        first, second = _rid(), _rid()
        changes = [_change(1, first), _change(2, second)]
        apply_changes(self.repositories, self.store, changes)
        with self.repositories.connect() as db:
            before = db.execute("SELECT COUNT(*) AS n FROM memory_records").fetchone()["n"]
        replayed = apply_changes(self.repositories, self.store, changes)
        with self.repositories.connect() as db:
            after = db.execute("SELECT COUNT(*) AS n FROM memory_records").fetchone()["n"]
        self.assertEqual(before, after)
        self.assertEqual(replayed["applied"] + replayed["skipped"], 2)

    def test_one_bad_change_does_not_block_the_rest(self) -> None:
        good = _rid()
        broken = {"seq": 1, "record_id": _rid(), "action": ACTION_UPSERT, "payload": {}}
        result = apply_changes(self.repositories, self.store, [broken, _change(2, good)])
        self.assertEqual(result["applied"], 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIsNotNone(self._row(good))

    def test_cloud_vector_is_installed_and_marked_ready(self) -> None:
        rid = _rid()
        change = _change(1, rid, vector=[0.5, 0.25, 0.125], vector_layer="warm")
        result = apply_changes(self.repositories, self.store, [change])
        self.assertEqual(result["errors"], [])
        row = self._row(rid)
        assert row is not None
        self.assertEqual(row["embedding_status"], "ready")
        self.assertEqual(row["embedding_layer"], "warm")

    def test_record_without_vector_stays_pending_for_local_backfill(self) -> None:
        rid = _rid()
        apply_changes(self.repositories, self.store, [_change(1, rid)])
        row = self._row(rid)
        assert row is not None
        self.assertEqual(row["embedding_status"], "pending")

    def test_bad_point_id_costs_the_vector_but_never_the_record(self) -> None:
        """点编号不是 UUID 时 Qdrant 会拒收向量，但记录本身必须留下。

        这条钉住的是「向量装不上也要能用」：记录留在 pending，
        由本地向量补偿补算，检索退回文字匹配，不会因为向量层出问题就整条丢。
        """
        apply_changes(
            self.repositories,
            self.store,
            [_change(1, "not-a-uuid", vector=[0.5, 0.25, 0.125], vector_layer="warm")],
        )
        row = self._row("not-a-uuid")
        self.assertIsNotNone(row, "向量失败不能把记录一起回滚掉")
        assert row is not None
        self.assertEqual(row["embedding_status"], "pending")

    def test_applied_changes_are_recorded(self) -> None:
        apply_changes(self.repositories, self.store, [_change(1, _rid()), _change(2, _rid())])
        self.assertEqual(self.store.applied_seqs(), {1, 2})


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()

    def test_snapshot_with_bad_checksum_is_refused(self) -> None:
        snapshot = {"cursor": 2, "changes": [_change(1, _rid())], "checksum": "deadbeef"}
        with self.assertRaises(ValueError):
            apply_snapshot(self.repositories, self.store, snapshot)

    def test_snapshot_with_matching_checksum_is_applied_and_moves_cursor(self) -> None:
        changes = [_change(1, _rid()), _change(2, _rid())]
        body = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        checksum = hashlib.sha256(body.encode("utf-8")).hexdigest()
        result = apply_snapshot(
            self.repositories,
            self.store,
            {"cursor": 2, "changes": changes, "checksum": checksum},
        )
        self.assertEqual(result["applied"], 2)
        self.assertEqual(self.store.change_cursor(), 2)


class AccessCollectionTests(unittest.TestCase):
    """「多久没用过」靠本地把访问记录报上去，而不是云端猜。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()
        self.scope = Scope("user", "web", "c1", "companion")

    def _insert(self, record_id: str, *, accessed_at: str | None) -> None:
        with self.repositories.connect() as db:
            db.execute(
                "INSERT INTO memory_records(id,owner_id,channel,conversation_id,module_id,layer,kind,status,"
                "content,metadata,created_at,updated_at,last_accessed_at) "
                "VALUES(?,?,?,?,?,'warm','fact','confirmed','内容','{}',?,?,?)",
                (record_id, self.scope.owner_id, self.scope.channel, self.scope.conversation_id,
                 self.scope.module_id, "2026-10-01T00:00:00.000+00:00", "2026-10-01T00:00:00.000+00:00",
                 accessed_at),
            )

    def test_touched_records_are_queued_for_upload(self) -> None:
        self._insert("a", accessed_at="2026-10-09T10:00:00.000+00:00")
        self._insert("b", accessed_at=None)
        written = collect_access(self.repositories, self.store)
        self.assertEqual(written, 1)
        self.assertEqual([row["record_id"] for row in self.store.pending_access()], ["a"])

    def test_repeated_collection_does_not_duplicate_the_same_minute(self) -> None:
        self._insert("a", accessed_at="2026-10-09T10:00:30.000+00:00")
        self.assertEqual(collect_access(self.repositories, self.store), 1)
        self.assertEqual(collect_access(self.repositories, self.store), 0)
        self.assertEqual(len(self.store.pending_access()), 1)

    def test_a_later_minute_is_reported_as_a_new_access(self) -> None:
        self._insert("a", accessed_at="2026-10-09T10:00:30.000+00:00")
        collect_access(self.repositories, self.store)
        with self.repositories.connect() as db:
            db.execute("UPDATE memory_records SET last_accessed_at=? WHERE id=?",
                       ("2026-10-09T10:05:00.000+00:00", "a"))
        self.assertEqual(collect_access(self.repositories, self.store), 1)
        self.assertEqual(len(self.store.pending_access()), 2)


class FakeCloudClient:
    """假云端。记录收到的请求，可以按需要扮演宕机/拒收。"""

    def __init__(
        self,
        *,
        fail: bool = False,
        reject: bool = False,
        changes: list[dict] | None = None,
        snapshot: dict | None = None,
    ) -> None:
        self.fail = fail
        self.reject = reject
        self.changes = changes or []
        self.snapshot = snapshot
        self.pushed: list[dict] = []
        self.pushed_access: list[dict] = []
        self.pull_calls: list[int] = []
        self.snapshot_calls = 0

    def push_turns(self, turns: list[dict]) -> dict:
        if self.fail:
            raise ConnectionError("云端连不上")
        self.pushed.extend(turns)
        if self.reject:
            return {"accepted": [], "rejected": [{"turn_id": item["turn_id"], "reason": "远端拒绝"} for item in turns]}
        return {"accepted": [item["turn_id"] for item in turns], "rejected": []}

    def push_access(self, events: list[dict]) -> dict:
        if self.fail:
            raise ConnectionError("云端连不上")
        self.pushed_access.extend(events)
        return {"access_recorded": len(events)}

    def fetch_snapshot(self) -> dict:
        if self.fail:
            raise ConnectionError("云端连不上")
        self.snapshot_calls += 1
        return self.snapshot or {"cursor": 0, "changes": [], "checksum": "", "count": 0}

    def pull_changes(self, since: int, *, limit: int = 500) -> dict:
        if self.fail:
            raise ConnectionError("云端连不上")
        self.pull_calls.append(since)
        pending = [item for item in self.changes if int(item["seq"]) > int(since)]
        return {"cursor": since, "changes": pending}


class SyncRunnerTests(unittest.TestCase):
    """验收要求：正常同步、断网补传、重复上传、同步不重叠、退出补传有上限。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()
        self.scope = Scope("user", "web", "c1", "companion")

    def _runner(self, client) -> MemorySyncRunner:
        return MemorySyncRunner(self.repositories, self.store, client, interval_seconds=600, shutdown_grace_seconds=1.0)

    def test_successful_sync_uploads_and_clears_the_queue(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好")
        client = FakeCloudClient()
        result = asyncio.run(self._runner(client).sync_once())
        self.assertEqual(result["pushed"]["turns_sent"], 1)
        self.assertEqual(self.store.upload_backlog(), 0)
        self.assertEqual(client.pushed[0]["owner_id"], "user")

    def test_offline_keeps_everything_for_the_next_round(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="断网时说的话")
        client = FakeCloudClient(fail=True)
        asyncio.run(self._runner(client).sync_once())
        pending = self.store.pending_turns()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertIn("连不上", pending[0]["last_error"])

    def test_recovered_network_uploads_the_backlog(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="断网时说的话")
        asyncio.run(self._runner(FakeCloudClient(fail=True)).sync_once())
        client = FakeCloudClient()
        asyncio.run(self._runner(client).sync_once())
        self.assertEqual(self.store.upload_backlog(), 0)
        self.assertEqual([item["turn_id"] for item in client.pushed], ["t1"])

    def test_turn_the_cloud_refused_stays_pending(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好")
        asyncio.run(self._runner(FakeCloudClient(reject=True)).sync_once())
        self.assertEqual(self.store.upload_backlog(), 1, "没被确认的一律留在队列，不能当成传过了")

    def test_same_second_turns_are_both_uploaded(self) -> None:
        moment = "2026-10-09T10:00:00.000+00:00"
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="第一句", occurred_at=moment)
        self.store.enqueue_turn(self.scope, turn_id="t2", user_text="第二句", occurred_at=moment)
        client = FakeCloudClient()
        asyncio.run(self._runner(client).sync_once())
        self.assertEqual(sorted(item["turn_id"] for item in client.pushed), ["t1", "t2"])

    def test_a_running_sync_blocks_the_next_one_from_starting(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好")
        client = FakeCloudClient()
        runner = self._runner(client)

        async def scenario() -> None:
            async with runner._lock:
                await runner._guarded_sync()

        asyncio.run(scenario())
        self.assertEqual(client.pushed, [], "上一轮还在跑时，这一轮应该直接跳过而不是叠上去")

    def test_cloud_changes_are_pulled_and_applied(self) -> None:
        rid = str(uuid.uuid4())
        # 已经有基线了才走增量：首次接入是另一条路（下面单独验）。
        self.store.set_state("baseline_at", "2026-10-09T00:00:00.000+00:00")
        client = FakeCloudClient(changes=[_change(1, rid)])
        runner = self._runner(client)
        asyncio.run(runner.sync_once())
        self.assertEqual(self.store.change_cursor(), 1)
        with self.repositories.connect() as db:
            row = db.execute("SELECT content FROM memory_records WHERE id=?", (rid,)).fetchone()
        self.assertIsNotNone(row, "拉回来的变更要真的合并进本地库")

    def test_first_ever_sync_uses_the_snapshot_instead_of_the_change_log(self) -> None:
        """变更流水里只有「装好这套系统之后」的改动。

        之前就存在的记录一条都不会出现在流水里 —— 只看流水，它们永远不会
        到本地，而且不报错。所以第一次必须用完整快照对齐。
        """
        rid = str(uuid.uuid4())
        changes = [_change(1, rid)]
        body = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        snapshot = {
            "cursor": 7,
            "changes": changes,
            "checksum": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "count": 1,
        }
        client = FakeCloudClient(changes=[], snapshot=snapshot)
        runner = self._runner(client)
        asyncio.run(runner.sync_once())
        self.assertEqual(client.snapshot_calls, 1, "首次接入必须取快照")
        self.assertEqual(client.pull_calls, [], "首次接入不该只拉增量")
        with self.repositories.connect() as db:
            row = db.execute("SELECT content FROM memory_records WHERE id=?", (rid,)).fetchone()
        self.assertIsNotNone(row, "快照里的记录要落到本地")
        self.assertEqual(self.store.change_cursor(), 7)

    def test_snapshot_is_only_used_once(self) -> None:
        self.store.set_state("baseline_at", "2026-10-09T00:00:00.000+00:00")
        client = FakeCloudClient()
        asyncio.run(self._runner(client).sync_once())
        self.assertEqual(client.snapshot_calls, 0, "已经有基线了就不该每轮都拉全量快照")
        self.assertEqual(client.pull_calls, [0])

    def test_shutdown_flush_gives_up_within_the_grace_period(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t1", user_text="你好")
        runner = self._runner(FakeCloudClient(fail=True))
        started = datetime.now(UTC)
        asyncio.run(runner.flush_for_shutdown())
        elapsed = (datetime.now(UTC) - started).total_seconds()
        self.assertLess(elapsed, 5.0, "退出补传必须限时，不能把进程拖在退出上")
        self.assertEqual(self.store.upload_backlog(), 1, "没传完的内容留在队列等下次")


class RunnerWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(root / "memory.db", root / "vectors", vector_size=3)
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()

    def test_no_runner_without_url_and_token(self) -> None:
        self.assertIsNone(build_runner_from_env(self.repositories, self.store, env={}))

    def test_runner_uses_the_configured_interval(self) -> None:
        runner = build_runner_from_env(
            self.repositories,
            self.store,
            env={"CLOUD_MEMORY_URL": "http://127.0.0.1:8790", "CLOUD_MEMORY_TOKEN": "x", "MEMORY_SYNC_INTERVAL_SECONDS": "600"},
        )
        self.assertIsNotNone(runner)
        assert runner is not None
        self.assertEqual(runner.interval_seconds, 600)
        self.assertEqual(runner.client.base_url, "http://127.0.0.1:8790")

    def test_interval_has_a_floor_so_a_typo_cannot_hammer_the_cloud(self) -> None:
        runner = build_runner_from_env(
            self.repositories,
            self.store,
            env={"CLOUD_MEMORY_URL": "http://x", "CLOUD_MEMORY_TOKEN": "x", "MEMORY_SYNC_INTERVAL_SECONDS": "1"},
        )
        assert runner is not None
        self.assertEqual(runner.interval_seconds, 30)


if __name__ == "__main__":
    unittest.main()


class SemanticFieldSyncTests(unittest.TestCase):
    """新语义字段的两端对齐：云端改写的范围与状态必须原样落到本地。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(
            root / "memory.db", root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(self.repositories.db_path)
        self.store.initialize()

    def _row(self, record_id: str) -> dict:
        with self.repositories.connect() as db:
            return dict(db.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone())

    def test_semantic_fields_travel_from_cloud_to_local(self) -> None:
        record_id = _rid()
        payload = _payload(
            kind="decision",
            status="active",
            content_status="accepted",
            semantics_version=1,
            semantics_key="decision||abc",
            scope_kind="project",
            project_id="dm-agent",
        )
        apply_changes(self.repositories, self.store, [_change(1, record_id, payload=payload)])
        row = self._row(record_id)
        self.assertEqual(row["content_status"], "accepted")
        self.assertEqual(row["scope_kind"], "project")
        self.assertEqual(row["project_id"], "dm-agent")
        self.assertEqual(row["semantics_version"], 1)
        self.assertEqual(row["semantics_key"], "decision||abc")

    def test_legacy_payload_lands_as_version_zero_instead_of_failing(self) -> None:
        """旧包不带新字段：按「未知」落地，不能写 NULL 把同步卡住。"""
        record_id = _rid()
        apply_changes(self.repositories, self.store, [_change(1, record_id)])
        row = self._row(record_id)
        self.assertEqual(row["semantics_version"], 0)
        self.assertIsNone(row["scope_kind"])
        self.assertIsNone(row["content_status"])

    def test_partial_update_keeps_the_rest_of_the_semantic_fields(self) -> None:
        record_id = _rid()
        apply_changes(
            self.repositories,
            self.store,
            [_change(1, record_id, payload=_payload(
                content_status="proposed", semantics_version=1, scope_kind="conversation",
            ))],
        )
        later = _iso(datetime.now(UTC) + timedelta(minutes=5))
        apply_changes(
            self.repositories,
            self.store,
            [_change(2, record_id, payload=_payload(
                content_status="accepted", semantics_version=1, scope_kind="conversation",
            ), updated_at=later)],
        )
        self.assertEqual(self._row(record_id)["content_status"], "accepted")

class VersionRelationSyncTests(unittest.TestCase):
    """版本关系与复核状态的两端对齐：不跟着同步，两端就各说各话。

    改动过的那条记忆在云端是「第 2 版、旧的已替代」，本地如果还停在
    「第 1 版、无需复核」，同一条记忆在两边会有不同结论。
    """

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.repositories = initialize_memory_repositories(
            root / "memory.db", root / "vectors", vector_size=3
        )
        self.addCleanup(self.repositories.close)
        self.store = MemorySyncStore(self.repositories.db_path)
        self.store.initialize()

    def _row(self, record_id: str) -> dict:
        with self.repositories.connect() as db:
            return dict(db.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone())

    def test_a_correction_relation_travels_to_the_other_end(self) -> None:
        old_id, new_id = _rid(), _rid()
        apply_changes(
            self.repositories,
            self.store,
            [
                _change(
                    1,
                    old_id,
                    payload=_payload(
                        content="项目采购预算五千",
                        kind="fact",
                        status="archived",
                        content_status="superseded",
                        semantics_version=1,
                        semantics_key="fact||budget",
                        scope_kind="conversation",
                        record_version=1,
                        effective_record_id=new_id,
                        metadata=json.dumps({"effective_record_id": new_id}, ensure_ascii=False),
                    ),
                ),
                _change(
                    2,
                    new_id,
                    payload=_payload(
                        content="项目采购预算三千",
                        kind="fact",
                        status="active",
                        content_status="reported",
                        semantics_version=1,
                        semantics_key="fact||budget",
                        scope_kind="conversation",
                        record_version=2,
                        effective_record_id=None,
                    ),
                ),
            ],
        )

        old_row, new_row = self._row(old_id), self._row(new_id)
        self.assertEqual(old_row["status"], "archived")
        self.assertEqual(old_row["content_status"], "superseded")
        self.assertEqual(old_row["effective_record_id"], new_id)
        self.assertEqual(new_row["record_version"], 2)
        self.assertIsNone(new_row["effective_record_id"])

        # 关系不只要存下来，还要能用：现行版本只应命中新的那一条。
        with self.repositories.connect() as db:
            current = db.execute(
                "SELECT id FROM memory_records WHERE semantics_key='fact||budget' AND status='active' "
                "AND (effective_record_id IS NULL OR effective_record_id='')"
            ).fetchall()
        self.assertEqual([row["id"] for row in current], [new_id])

    def test_review_and_conflict_relations_travel(self) -> None:
        first, second = _rid(), _rid()
        due = "2026-10-12T00:00:00.000+00:00"
        apply_changes(
            self.repositories,
            self.store,
            [
                _change(
                    1,
                    first,
                    payload=_payload(
                        kind="fact",
                        status="active",
                        content_status="reported",
                        semantics_version=1,
                        review_state="pending",
                        review_due_at=due,
                        conflict_with=json.dumps([first, second], ensure_ascii=False),
                    ),
                ),
                _change(
                    2,
                    second,
                    payload=_payload(
                        content="项目采购预算四千",
                        kind="fact",
                        status="active",
                        content_status="reported",
                        semantics_version=1,
                        review_state="pending",
                        review_due_at=due,
                        conflict_with=json.dumps([first, second], ensure_ascii=False),
                    ),
                ),
            ],
        )

        for record_id in (first, second):
            row = self._row(record_id)
            self.assertEqual(row["review_state"], "pending")
            self.assertEqual(row["review_due_at"], due)
            self.assertEqual(
                sorted(json.loads(row["conflict_with"])), sorted([first, second])
            )

    def test_a_payload_without_version_fields_lands_as_unknown(self) -> None:
        """旧包不带版本字段：按「第 0 版、无需复核」落地，不能写 NULL 卡住同步。"""
        record_id = _rid()
        apply_changes(self.repositories, self.store, [_change(1, record_id)])
        row = self._row(record_id)
        self.assertEqual(row["record_version"], 0)
        self.assertEqual(row["review_state"], "none")
        self.assertIsNone(row["effective_record_id"])
        self.assertIsNone(row["conflict_with"])


class UploadQueuePacketTests(unittest.TestCase):
    """待上传队列要能连同处理包一起传：少一轮历史，云端就看不见决策过程。"""

    def setUp(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        self.store = MemorySyncStore(root / "memory.db")
        self.store.initialize()
        self.scope = Scope("user", "web", "c1", "companion")

    def _packet(self) -> dict:
        from agent_core.memory_turn import MemoryTurnPacket

        return MemoryTurnPacket(
            turn_id="t1",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope_owner="user",
            scope_channel="web",
            scope_conversation="c1",
            user_text="就按这个做",
            published_reply="好",
            task_goal="把记忆和搜索接起来",
        ).as_payload()

    def test_packet_and_schema_version_survive_the_queue(self) -> None:
        self.store.enqueue_turn(
            self.scope, turn_id="t1", user_text="就按这个做",
            packet=self._packet(), schema_version=1,
        )
        row = self.store.pending_turns()[0]
        self.assertEqual(row["schema_version"], 1)
        self.assertIn("task_goal", json.loads(row["packet"]))

    def test_legacy_upload_stays_version_zero_without_a_packet(self) -> None:
        self.store.enqueue_turn(self.scope, turn_id="t2", user_text="老包")
        row = self.store.pending_turns()[0]
        self.assertEqual(row["schema_version"], 0)
        self.assertIsNone(row["packet"])

    def test_existing_queue_table_gains_the_new_columns(self) -> None:
        """老库里没有这两列：迁移必须补上，否则升级后第一次上传就失败。"""
        import sqlite3

        with sqlite3.connect(self.store.db_path) as db:
            db.execute("ALTER TABLE memory_upload_queue RENAME TO memory_upload_queue_old")
            db.execute(
                "CREATE TABLE memory_upload_queue ("
                "turn_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,"
                "conversation_id TEXT NOT NULL, module_id TEXT NOT NULL, user_text TEXT NOT NULL,"
                "assistant_reply TEXT NOT NULL DEFAULT '', occurred_at TEXT NOT NULL,"
                "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
                "last_error TEXT, sent_at TEXT, created_at TEXT NOT NULL)"
            )
            db.execute("DROP TABLE memory_upload_queue_old")
        self.store.initialize()
        self.store.enqueue_turn(
            self.scope, turn_id="t3", user_text="迁移后", packet=self._packet(), schema_version=1
        )
        self.assertEqual(self.store.pending_turns()[0]["schema_version"], 1)
