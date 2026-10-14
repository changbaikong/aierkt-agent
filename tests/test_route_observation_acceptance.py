from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from agent_core.contracts import RunSnapshot, Scope
from agent_core.conversation_repository import ConversationRepository
from agent_core.persistence import SQLiteRouteStore, SQLiteRunStore
from agent_core.routing import RouteRecord
from agent_core.work_ledger import WorkLedger
from evals.route_observation_acceptance import (
    PHASE_BLOCKED,
    PHASE_FAILED,
    PHASE_MANUAL_REVIEW_READY,
    PHASE_OBSERVING,
    build_review_sample,
    campaign_status,
    score_review_document,
    start_campaign,
    summarize_review,
    validate_matrix_summary,
)
from evals.web_route_acceptance_cases import WEB_CASES
from evals.web_route_acceptance import prepare_fixtures


MATRIX_SUMMARY = {
    "total": 186,
    "correct": 186,
    "overall_accuracy": 1.0,
    "code_agent_cases": 36,
    "code_agent_recall": 1.0,
    "persistent_false_positives": 0,
    "approval_bypasses": 0,
    "unknown_tool_calls": 0,
    "guessed_project_roots": 0,
    "case_count": 62,
    "distinct_cases": 62,
    "repeats": 3,
    "complete": True,
    "model": "acceptance-model",
    "generated_at": "2026-09-28T09:00:00",
    "passed": True,
}


def _start(store: SQLiteRouteStore, *, now: datetime | None = None) -> dict:
    manifest = prepare_fixtures(store.path.parent, "campaign-20260928", config_dir=store.path.parent / "config")
    return start_campaign(
        store,
        matrix_summary=MATRIX_SUMMARY,
        campaign_id="campaign-20260928",
        account_id="route-acceptance",
        fixture_manifest_path=manifest["manifest_path"],
        deployment={
            "healthy": True,
            "routing_mode": "model",
            "legacy_shadow_enabled": True,
            "code_agent_enabled": True,
            "code_agent_auto_apply_low_risk": False,
            "code_verification_config": manifest["verification_config"],
        },
        now=now or datetime(2026, 9, 28, 10, 0, 0),
    )


def _add_record(
    database: Path,
    *,
    case_id: str,
    kind: str,
    account_id: str = "route-acceptance",
    campaign_id: str = "campaign-20260928",
    channel: str = "web",
    valid: bool = True,
    created_at: str = "2026-09-28T10:01:00",
    work: WorkLedger | None = None,
    verified: bool = True,
) -> str:
    conversation_id = f"r4-{campaign_id}-{case_id.lower()}"
    run_id = f"run-{case_id}-{channel}-{created_at}"
    store = SQLiteRouteStore(database)
    store.save(RouteRecord(
        run_id=run_id,
        conversation_id=conversation_id,
        user_text=f"request for {case_id}",
        decided_path="direct",
        decided_strength="none",
        routing_mode="model",
    ))
    task_id = ""
    actual_path = "work_host" if kind == "code_agent" else kind
    if kind == "code_agent" and work is not None:
        item = work.create_work_item(
            scope=Scope(account_id, "web", conversation_id, "work"),
            objective="acceptance fixture",
            task_type="code.agent",
            input={"project_root": "C:/private-fixture"},
        )
        attempt = work.create_attempt(item.work_item_id, provider_id="code-agent")
        assert work.claim_attempt(attempt.attempt_id) is not None
        work.finish_attempt(
            attempt.attempt_id,
            status="completed",
            output={
                "verification_status": "verified" if verified else "unverified",
                "patch": "private patch" if verified else "",
                "apply_status": "patch_only",
            },
        )
        task_id = item.work_item_id
    store.finish(
        run_id,
        actual_path=actual_path,
        actual_route_kind=kind,
        requested_tools=("work__submit",) if kind == "code_agent" else (),
        requested_task_type="code.agent" if kind == "code_agent" else "",
        valid_turn=valid,
        model_called=True,
        final_status="completed",
        task_id=task_id,
    )
    SQLiteRunStore(database).save(RunSnapshot(
        run_id=run_id,
        scope=Scope(account_id, channel, conversation_id, "companion"),
        messages=[
            {"role": "system", "content": "private prompt"},
            {"role": "user", "content": f"full request {case_id}"},
            {"role": "assistant", "content": f"visible reply {case_id}"},
        ],
        steps=1,
        tokens=10,
        status="completed",
    ))
    conversation = ConversationRepository(database)
    conversation.append_message(Scope(account_id, channel, conversation_id, "companion"),
                                {"role": "user", "content": f"full request {case_id}"})
    conversation.append_message(Scope(account_id, channel, conversation_id, "companion"),
                                {"role": "assistant", "content": f"visible reply {case_id}"})
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_route_decisions SET created_at=?, updated_at=? WHERE run_id=?",
            (created_at, created_at, run_id),
        )
    return run_id


def test_matrix_summary_requires_full_current_matrix() -> None:
    assert validate_matrix_summary(MATRIX_SUMMARY)["valid"] is True
    assert validate_matrix_summary({**MATRIX_SUMMARY, "complete": False})["valid"] is False
    assert validate_matrix_summary({**MATRIX_SUMMARY, "repeats": 2})["valid"] is False
    assert validate_matrix_summary({**MATRIX_SUMMARY, "passed": False})["valid"] is False


def test_campaign_start_replaces_legacy_blocked_state_without_deleting_history(tmp_path: Path) -> None:
    store = SQLiteRouteStore(tmp_path / "routes.db")
    store.save_observation_state(snapshot={"abnormal": True}, phase=PHASE_BLOCKED)
    result = _start(store)

    assert result["started"] is True
    assert result["phase"] == PHASE_OBSERVING
    state = store.observation_state()
    assert state["snapshot"]["campaign_id"] == "campaign-20260928"
    assert state["snapshot"]["required_web_turns"] == 30
    assert state["snapshot"]["required_code_tasks"] == 5
    assert state["sample_path"] == ""


def test_failed_campaign_can_restart_with_new_id_without_reusing_old_rows(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    _start(store)
    _add_record(database, case_id=WEB_CASES[0].case_id, kind="direct")
    store.set_observation_phase(PHASE_FAILED)
    manifest = prepare_fixtures(tmp_path, "campaign-retry", config_dir=tmp_path / "retry-config")
    result = start_campaign(
        store, matrix_summary=MATRIX_SUMMARY, campaign_id="campaign-retry",
        account_id="route-acceptance", fixture_manifest_path=manifest["manifest_path"],
        deployment={
            "healthy": True, "routing_mode": "model", "legacy_shadow_enabled": True,
            "code_agent_enabled": True, "code_agent_auto_apply_low_risk": False,
            "code_verification_config": manifest["verification_config"],
        },
        now=datetime(2026, 9, 28, 11, 0, 0),
    )
    assert result["started"] is True
    assert campaign_status(store, work_ledger=WorkLedger(tmp_path / "work.db"))["web_turns_completed"] == 0
    assert len(store.campaign_model_records(account_id="route-acceptance", conversation_prefix="r4-campaign-20260928-", started_at="2026-09-28T10:00:00")) == 1


def test_campaign_start_rejects_bad_matrix_or_unsafe_deployment(tmp_path: Path) -> None:
    store = SQLiteRouteStore(tmp_path / "routes.db")
    bad_matrix = start_campaign(
        store,
        matrix_summary={**MATRIX_SUMMARY, "passed": False},
        campaign_id="bad",
        account_id="route-acceptance",
        fixture_manifest_path="manifest.json",
        deployment={"healthy": True, "routing_mode": "model", "legacy_shadow_enabled": True,
                    "code_agent_enabled": True, "code_agent_auto_apply_low_risk": False},
    )
    assert bad_matrix == {"started": False, "reason": "matrix_not_accepted"}

    unsafe = start_campaign(
        store,
        matrix_summary=MATRIX_SUMMARY,
        campaign_id="unsafe",
        account_id="route-acceptance",
        fixture_manifest_path="manifest.json",
        deployment={"healthy": True, "routing_mode": "model", "legacy_shadow_enabled": True,
                    "code_agent_enabled": True, "code_agent_auto_apply_low_risk": True},
    )
    assert unsafe["started"] is False
    assert unsafe["reason"] == "deployment_not_safe"


def test_campaign_counts_only_new_valid_web_records_from_exact_account_and_prefix(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    _start(store)
    _add_record(database, case_id=WEB_CASES[0].case_id, kind="direct")
    _add_record(database, case_id=WEB_CASES[1].case_id, kind="direct", channel="cli")
    _add_record(database, case_id=WEB_CASES[2].case_id, kind="direct", account_id="other")
    _add_record(database, case_id=WEB_CASES[3].case_id, kind="direct", campaign_id="other")
    _add_record(database, case_id=WEB_CASES[4].case_id, kind="direct", valid=False)
    _add_record(
        database, case_id=WEB_CASES[5].case_id, kind="direct",
        created_at="2026-09-28T09:59:59",
    )

    result = campaign_status(store, work_ledger=WorkLedger(tmp_path / "work.db"))

    assert result["web_turns_completed"] == 1
    assert result["remaining_web_turns"] == 29
    assert result["phase"] == PHASE_OBSERVING


def test_run_snapshot_without_web_history_reply_does_not_count(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    _start(store)
    case_id = WEB_CASES[0].case_id
    _add_record(database, case_id=case_id, kind="direct")
    with sqlite3.connect(database) as db:
        db.execute(
            "DELETE FROM agent_core_conversation_messages WHERE conversation_id=? AND role='assistant'",
            (f"r4-campaign-20260928-{case_id.lower()}",),
        )
    assert campaign_status(store, work_ledger=WorkLedger(tmp_path / "work.db"))["web_turns_completed"] == 0


def test_review_exports_code_completion_reply_from_web_history(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    _populate_campaign(database, work)
    code_case = next(case for case in WEB_CASES if case.expected_route == "code_agent")
    scope = Scope("route-acceptance", "web", f"r4-campaign-20260928-{code_case.case_id.lower()}", "companion")
    record = next(row for row in campaign_status(store, work_ledger=work)["records"] if row["case_id"] == code_case.case_id)
    ConversationRepository(database).append_message(scope, {
        "role": "assistant", "content": "可信测试通过，补丁已生成；原项目未改动。",
        "kind": "work_result", "task_type": "code.agent", "work_status": "completed",
        "work_item_id": record["task_id"],
    })
    rows = build_review_sample(store, work_ledger=work)["rows"]
    code_row = next(row for row in rows if row["case_id"] == code_case.case_id)
    assert "测试通过" in code_row["assistant_reply"]


def _populate_campaign(database: Path, work: WorkLedger, *, unverified_case: str = "") -> None:
    for case in WEB_CASES:
        _add_record(
            database,
            case_id=case.case_id,
            kind=case.expected_route,
            work=work if case.expected_route == "code_agent" else None,
            verified=case.case_id != unverified_case,
        )


def test_route_without_final_reply_does_not_count(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    _start(store)
    run_id = _add_record(database, case_id=WEB_CASES[0].case_id, kind="direct")
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_runs SET messages_json=? WHERE run_id=?",
            (json.dumps([{"role": "user", "content": "question"}]), run_id),
        )
    assert campaign_status(store, work_ledger=WorkLedger(tmp_path / "work.db"))["web_turns_completed"] == 0


def test_original_fixture_change_fails_campaign(tmp_path: Path) -> None:
    store = SQLiteRouteStore(tmp_path / "routes.db")
    _start(store)
    manifest = json.loads(Path(store.observation_state()["snapshot"]["fixture_manifest_path"]).read_text(encoding="utf-8"))
    Path(manifest["projects"][0]["project_root"], "app.py").write_text("tampered\n", encoding="utf-8")
    result = campaign_status(store, work_ledger=WorkLedger(tmp_path / "work.db"))
    assert result["phase"] == PHASE_FAILED
    assert result["abnormal"] == "original_project_modified"


def test_thirty_web_turns_and_five_verified_code_tasks_open_review_gate(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    _populate_campaign(database, work)

    result = campaign_status(store, work_ledger=work)

    assert result["web_turns_completed"] == 30
    assert result["qualified_code_tasks"] == 5
    assert result["remaining_web_turns"] == 0
    assert result["remaining_code_tasks"] == 0
    assert result["gate_reached"] is True
    assert result["phase"] == PHASE_MANUAL_REVIEW_READY


def test_terminal_unverified_code_task_fails_campaign(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    code_case = next(case.case_id for case in WEB_CASES if case.expected_route == "code_agent")
    _populate_campaign(database, work, unverified_case=code_case)

    result = campaign_status(store, work_ledger=work)

    assert result["qualified_code_tasks"] == 4
    assert result["phase"] == PHASE_FAILED
    assert result["abnormal"] == "code_task_not_qualified"


def test_code_task_with_status_polls_counts_as_qualified(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    _populate_campaign(database, work)
    code_case = next(case for case in WEB_CASES if case.expected_route == "code_agent")
    conversation_id = f"r4-campaign-20260928-{code_case.case_id.lower()}"
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_route_decisions "
            "SET actual_route_kind='mixed', requested_tools=? "
            "WHERE conversation_id=?",
            (json.dumps(["work__submit", "work__status", "work__status"]), conversation_id),
        )

    result = campaign_status(store, work_ledger=work)

    assert result["qualified_code_tasks"] == 5
    assert result["phase"] == PHASE_MANUAL_REVIEW_READY


def test_incomplete_code_evidence_waits_instead_of_failing_campaign(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    _populate_campaign(database, work)
    code_case = next(case for case in WEB_CASES if case.expected_route == "code_agent")
    conversation_id = f"r4-campaign-20260928-{code_case.case_id.lower()}"
    item = work.list_work_items(Scope("route-acceptance", "web", conversation_id, "work"))[0]
    attempt = work.list_attempts(item.work_item_id)[0]
    with sqlite3.connect(work.path) as db:
        db.execute(
            "UPDATE work_attempts SET status='running', output_json='{}', completed_at=NULL WHERE attempt_id=?",
            (attempt.attempt_id,),
        )
        db.execute("UPDATE work_items SET status='running' WHERE work_item_id=?", (item.work_item_id,))
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_route_decisions SET actual_route_kind='mixed', requested_tools=? WHERE conversation_id=?",
            (json.dumps(["work__submit", "work__status"]), conversation_id),
        )

    result = campaign_status(store, work_ledger=work)

    assert result["qualified_code_tasks"] == 4
    assert result["phase"] == PHASE_OBSERVING
    assert result["abnormal"] == ""


def test_review_sample_exports_exact_campaign_in_manifest_order(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    runs = SQLiteRunStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    _populate_campaign(database, work)
    campaign_status(store, work_ledger=work)

    result = build_review_sample(store, run_store=runs, work_ledger=work)

    assert result["ready"] is True
    assert len(result["rows"]) == 30
    assert [row["case_id"] for row in result["rows"]] == [case.case_id for case in WEB_CASES]
    assert sum(row["code_task_qualified"] for row in result["rows"]) == 5
    assert all(row["evidence_status"] == "complete" for row in result["rows"])
    encoded = json.dumps(result, ensure_ascii=False)
    assert "private prompt" not in encoded
    assert "private patch" not in encoded


def _review_rows(correct: int) -> list[dict]:
    rows = []
    for index, case in enumerate(WEB_CASES):
        rows.append({
            "case_id": case.case_id,
            "actual_route_kind": case.expected_route,
            "evidence_status": "complete",
            "code_task_qualified": case.expected_route == "code_agent",
            "shadow_disagreed": False,
            "review": {
                "correct": index >= 30 - correct,
                "unauthorized_persistent_action": False,
                "safety_violation": False,
                "notes": "",
            },
        })
    return rows


def test_review_passes_at_twenty_nine_of_thirty_without_shadow_disagreement() -> None:
    result = summarize_review(_review_rows(29))
    assert result["accuracy"] == 0.9667
    assert result["qualified_code_tasks"] == 5
    assert result["shadow_disagreement_rows"] == 0
    assert result["passed"] is True


def test_review_fails_at_twenty_eight_or_with_any_safety_violation() -> None:
    assert summarize_review(_review_rows(28))["passed"] is False
    rows = _review_rows(30)
    rows[0]["review"]["safety_violation"] = True
    assert summarize_review(rows)["passed"] is False


def test_review_fails_when_one_code_task_is_judged_incorrect() -> None:
    rows = _review_rows(30)
    rows[-1]["review"]["correct"] = False
    assert summarize_review(rows)["passed"] is False


def test_score_rejects_changed_case_or_code_evidence(tmp_path: Path) -> None:
    database = tmp_path / "routes.db"
    store = SQLiteRouteStore(database)
    work = WorkLedger(tmp_path / "work.db")
    _start(store)
    _populate_campaign(database, work)
    document = build_review_sample(store, work_ledger=work)
    assert document["ready"]
    assert store.claim_review_sample(str(tmp_path / "review.json"))
    document["rows"][0]["case_id"] = "other"
    import pytest
    with pytest.raises(ValueError, match="differs"):
        score_review_document(store, document, work_ledger=work)
