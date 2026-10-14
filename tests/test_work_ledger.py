from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from agent_core.contracts import Scope
from agent_core.work_ledger import WorkLedger


def test_ledger_creates_work_and_attempt_and_preserves_scope(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")

    work = ledger.create_work_item(scope=scope, objective="整理下载目录", context={"path": "C:/Downloads"})
    attempt = ledger.create_attempt(work.work_item_id, provider_id="local-worker")

    loaded = ledger.get_work_item(work.work_item_id, scope)
    assert loaded is not None
    assert loaded.objective == "整理下载目录"
    assert loaded.context["path"] == "C:/Downloads"
    assert ledger.get_work_item(work.work_item_id, Scope(owner_id="bob")) is None
    assert attempt.status == "queued"


def test_ledger_claims_attempt_once_and_records_events_checkpoint_and_result(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")
    work = ledger.create_work_item(scope=scope, objective="列出文件")
    attempt = ledger.create_attempt(work.work_item_id, provider_id="local-worker")

    claimed = ledger.claim_attempt(attempt.attempt_id, lease_seconds=30)
    assert claimed is not None
    assert claimed.status == "running"
    assert ledger.claim_attempt(attempt.attempt_id) is None

    event = ledger.append_event(attempt.attempt_id, "progress", {"message": "扫描中"})
    checkpoint = ledger.save_checkpoint(attempt.attempt_id, {"cursor": 3})
    ledger.record_file_operation(attempt.attempt_id, {"source": "a.txt", "target": "txt/a.txt"})
    assert event.sequence == 1
    assert checkpoint["cursor"] == 3
    assert ledger.list_events(attempt.attempt_id)[0].payload["message"] == "扫描中"
    assert ledger.list_file_operations(attempt.attempt_id)[0]["source"] == "a.txt"

    assert ledger.finish_attempt(attempt.attempt_id, status="completed", output={"count": 2})
    finished = ledger.get_attempt(attempt.attempt_id)
    assert finished is not None
    assert finished.status == "completed"
    assert finished.output["count"] == 2


def test_ledger_approval_is_bound_to_attempt_and_can_be_decided(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")
    work = ledger.create_work_item(scope=scope, objective="移动文件")
    attempt = ledger.create_attempt(work.work_item_id, provider_id="local-worker")
    approval = ledger.create_approval(attempt.attempt_id, {"moves": [{"source": "a", "target": "b"}]})

    assert ledger.decide_approval(approval.approval_id, "approved")
    decided = ledger.get_approval(approval.approval_id)
    assert decided is not None
    assert decided.status == "approved"
    assert not ledger.decide_approval(approval.approval_id, "approved")


def test_ledger_lists_pending_approvals_for_a_scoped_work_item(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")
    work = ledger.create_work_item(scope=scope, objective="移动文件")
    attempt = ledger.create_attempt(work.work_item_id, provider_id="local-worker")
    ledger.claim_attempt(attempt.attempt_id)
    approval = ledger.create_approval(attempt.attempt_id, {"moves": []})
    assert ledger.list_approvals(work.work_item_id, scope)[0].approval_id == approval.approval_id


def test_new_attempt_requeues_existing_work_item(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")
    work = ledger.create_work_item(scope=scope, objective="列出文件")
    first = ledger.create_attempt(work.work_item_id, provider_id="local-worker")
    ledger.claim_attempt(first.attempt_id)
    ledger.finish_attempt(first.attempt_id, status="failed", error="boom")
    second = ledger.create_attempt(work.work_item_id, provider_id="local-worker")
    assert second.status == "queued"
    assert ledger.get_work_item(work.work_item_id, scope).status == "queued"


def test_ledger_persists_explicit_task_type_and_artifact_scope(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")
    work = ledger.create_work_item(
        scope=scope,
        objective="截图",
        task_type="screen.capture",
        input={"display": 1},
    )
    attempt = ledger.create_attempt(work.work_item_id, provider_id="local-worker")
    artifact = ledger.create_artifact(
        attempt_id=attempt.attempt_id,
        scope=scope,
        media_type="image/png",
        size_bytes=12,
        path="data/work_artifacts/a.png",
        expires_at="2099-01-01T00:00:00+00:00",
    )

    loaded = ledger.get_work_item(work.work_item_id, scope)
    assert loaded is not None
    assert loaded.task_type == "screen.capture"
    assert loaded.input == {"display": 1}
    assert ledger.get_artifact(artifact.artifact_id, scope) == artifact
    assert ledger.get_artifact(artifact.artifact_id, Scope(owner_id="bob")) is None


def test_ledger_migrates_legacy_work_items_without_task_type(tmp_path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE work_items (
                work_item_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL, module_id TEXT NOT NULL, objective TEXT NOT NULL,
                context_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy",
                "alice",
                "web",
                "chat",
                "work",
                "整理下载目录",
                '{"directory":"C:/Downloads"}',
                "completed",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        db.commit()

    ledger = WorkLedger(path)
    loaded = ledger.get_work_item("legacy", Scope("alice", "web", "chat", "work"))
    assert loaded is not None
    assert loaded.task_type == "file.organize"
    assert loaded.input["directory"] == "C:/Downloads"


def test_expired_lease_recovers_once_and_preserves_confirmed_process(tmp_path) -> None:
    ledger = WorkLedger(tmp_path / "work.db")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat-1", module_id="work")
    work = ledger.create_work_item(scope=scope, objective="检查任务")
    attempt = ledger.create_attempt(work.work_item_id, provider_id="worker")
    ledger.claim_attempt(attempt.attempt_id)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    with sqlite3.connect(tmp_path / "work.db") as db:
        db.execute(
            "UPDATE work_attempts SET lease_expires_at=? WHERE attempt_id=?",
            (expired, attempt.attempt_id),
        )
        db.commit()

    assert ledger.recover_expired_attempts(active_attempt_ids=[attempt.attempt_id]) == []
    recovered = ledger.recover_expired_attempts()
    assert recovered == [{
        "attempt_id": attempt.attempt_id,
        "reason": "lease expired; no active process confirmed",
    }]
    assert ledger.recover_expired_attempts() == []
    final = ledger.get_attempt(attempt.attempt_id)
    assert final is not None
    assert final.status == "interrupted"
    assert final.error == "lease expired; no active process confirmed"
