"""Host-owned durable ledger for task-level work and provider attempts."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import Scope
from .control_plan import (
    PLAN_IN_FLIGHT_STATUSES,
    PLAN_STATUSES,
    PLAN_TERMINAL_STATUSES,
    STEP_ACTIVE_STATUSES,
    STEP_STATUSES,
    STEP_TERMINAL_STATUSES,
    ControlPlan,
    PlanEvent,
    PlanStep,
    PlanStepDefinition,
)
from .provider_activity import ProviderActivity
from .work_provider import WORK_TASK_TYPES

WORK_STATUSES = frozenset({"queued", "running", "waiting_approval", "completed", "failed", "cancelled", "interrupted", "unknown_result"})
ATTEMPT_STATUSES = WORK_STATUSES
APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})
_PLAN_STEP_IN_FLIGHT = ("running", "ready", "waiting_approval")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


_UNSET: Any = object()


@dataclass(frozen=True)
class WorkItemRecord:
    work_item_id: str
    scope: Scope
    objective: str
    task_type: str
    input: dict[str, Any]
    context: dict[str, Any]
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    work_item_id: str
    provider_id: str
    status: str
    lease_id: str | None = None
    lease_expires_at: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class EventRecord:
    event_id: str
    attempt_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: str
    attempt_id: str
    payload: dict[str, Any]
    status: str
    created_at: str
    decided_at: str | None = None

@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    attempt_id: str
    media_type: str
    path: str
    size_bytes: int
    created_at: str
    expires_at: str


def _legacy_task_type(objective: str, context: Mapping[str, Any]) -> str:
    """Infer the old keyword-based task once while reading pre-migration rows."""
    if context.get("approved_moves") or context.get("moves"):
        return "file.organize"
    text = str(objective).casefold()
    if any(word in text for word in ("整理", "分类", "归类", "organize", "sort")):
        return "file.organize"
    if any(word in text for word in ("截图", "screenshot", "screen capture")):
        return "screen.capture"
    if any(word in text for word in ("音量", "volume")):
        return "system.volume.adjust"
    return "file.list"


class WorkLedger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS work_items (
                    work_item_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, module_id TEXT NOT NULL, objective TEXT NOT NULL,
                    task_type TEXT NOT NULL DEFAULT 'file.list', input_json TEXT NOT NULL DEFAULT '{}',
                    context_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_work_items_scope ON work_items(owner_id, channel, conversation_id, module_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS work_attempts (
                    attempt_id TEXT PRIMARY KEY, work_item_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                    status TEXT NOT NULL, lease_id TEXT, lease_expires_at TEXT, output_json TEXT NOT NULL,
                    error TEXT, created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_work_attempts_work ON work_attempts(work_item_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS work_events (
                    event_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(attempt_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS work_checkpoints (
                    attempt_id TEXT PRIMARY KEY, checkpoint_json TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_approvals (
                    approval_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS work_file_operations (
                    operation_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, operation_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_artifacts (
                    artifact_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, media_type TEXT NOT NULL,
                    path TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
                );
                -- Provider activity is a projection, not a second source of truth:
                -- the attempt row still owns "queued/running/...", this table owns
                -- "what the external agent is doing". Indexed columns exist only
                -- for the two questions the Host asks in SQL (recovery lookup and
                -- "is this attempt's result still trustworthy"); everything the UI
                -- needs lives in activity_json, so adding a field later never needs
                -- a migration.
                CREATE TABLE IF NOT EXISTS provider_activity (
                    attempt_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, session_id TEXT,
                    status TEXT NOT NULL, confidence TEXT NOT NULL DEFAULT 'unknown',
                    activity_json TEXT NOT NULL, started_at TEXT, updated_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_provider_activity_status
                    ON provider_activity(status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS control_plans (
                    plan_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, module_id TEXT NOT NULL, objective TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_control_plans_scope
                    ON control_plans(owner_id, channel, conversation_id, module_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS plan_steps (
                    step_id TEXT NOT NULL, plan_id TEXT NOT NULL, task_type TEXT NOT NULL,
                    objective TEXT NOT NULL, input_json TEXT NOT NULL DEFAULT '{}',
                    requires_approval INTEGER NOT NULL DEFAULT 0, failure_policy TEXT NOT NULL DEFAULT 'stop',
                    status TEXT NOT NULL, work_item_id TEXT, output_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT, position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY (plan_id, step_id)
                );
                CREATE INDEX IF NOT EXISTS idx_plan_steps_work ON plan_steps(work_item_id);
                CREATE TABLE IF NOT EXISTS plan_step_dependencies (
                    plan_id TEXT NOT NULL, step_id TEXT NOT NULL, depends_on_step_id TEXT NOT NULL,
                    PRIMARY KEY (plan_id, step_id, depends_on_step_id)
                );
                CREATE TABLE IF NOT EXISTS plan_events (
                    event_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    step_id TEXT, work_item_id TEXT, attempt_id TEXT, event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(plan_id, sequence)
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(work_items)").fetchall()}
            if "task_type" not in columns:
                db.execute("ALTER TABLE work_items ADD COLUMN task_type TEXT NOT NULL DEFAULT 'file.list'")
            if "input_json" not in columns:
                db.execute("ALTER TABLE work_items ADD COLUMN input_json TEXT NOT NULL DEFAULT '{}'")
            step_columns = {row[1] for row in db.execute("PRAGMA table_info(plan_steps)").fetchall()}
            if step_columns and "position" not in step_columns:
                db.execute("ALTER TABLE plan_steps ADD COLUMN position INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _scope_args(scope: Scope) -> tuple[str, str, str, str]:
        return scope.owner_id, scope.channel, scope.conversation_id, scope.module_id

    def create_work_item(self, *, scope: Scope, objective: str, task_type: str = "file.list", input: Mapping[str, Any] | None = None, context: Mapping[str, Any] | None = None) -> WorkItemRecord:
        if not str(objective).strip():
            raise ValueError("objective is required")
        if task_type not in WORK_TASK_TYPES:
            raise ValueError(f"unsupported task type: {task_type}")
        now = _iso(_now())
        record = WorkItemRecord(uuid.uuid4().hex, scope, str(objective).strip(), task_type, dict(input or {}), dict(context or {}), "queued", now, now)
        with self._connect() as db:
            db.execute("INSERT INTO work_items (work_item_id, owner_id, channel, conversation_id, module_id, objective, task_type, input_json, context_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (record.work_item_id, *self._scope_args(scope), record.objective, record.task_type, _json(record.input), _json(record.context), record.status, now, now))
        return record

    def _work_row(self, row: sqlite3.Row | None) -> WorkItemRecord | None:
        if row is None:
            return None
        context = json.loads(row["context_json"] or "{}")
        input_data = json.loads(row["input_json"] or "{}")
        task_type = row["task_type"] or "file.list"
        if task_type == "file.list" and not input_data:
            inferred = _legacy_task_type(row["objective"], context)
            if inferred != "file.list":
                task_type = inferred
            input_data = {
                key: context[key]
                for key in ("directory", "path", "recursive", "sort_by", "descending", "offset", "limit", "percent")
                if key in context
            }
        return WorkItemRecord(
            row["work_item_id"],
            Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
            row["objective"],
            task_type,
            input_data,
            context,
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    def get_work_item(self, work_item_id: str, scope: Scope) -> WorkItemRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM work_items WHERE work_item_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?", (work_item_id, *self._scope_args(scope))).fetchone()
            return self._work_row(row)

    def status_snapshot(self) -> dict[str, int]:
        """Return durable task counts used by quiet observation polling."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS n FROM work_items "
                "GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def list_work_items(self, scope: Scope, *, limit: int = 50) -> list[WorkItemRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM work_items WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? ORDER BY created_at DESC LIMIT ?", (*self._scope_args(scope), max(1, min(100, int(limit))))).fetchall()
        return [self._work_row(row) for row in rows if row is not None]

    def update_work_context(self, work_item_id: str, scope: Scope, context: Mapping[str, Any]) -> bool:
        now = _iso(_now())
        with self._connect() as db:
            cur = db.execute("UPDATE work_items SET context_json=?, updated_at=? WHERE work_item_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?", (_json(context), now, work_item_id, *self._scope_args(scope)))
        return cur.rowcount == 1

    def create_attempt(self, work_item_id: str, *, provider_id: str) -> AttemptRecord:
        now = _iso(_now())
        attempt = AttemptRecord(uuid.uuid4().hex, work_item_id, str(provider_id), "queued", created_at=now)
        with self._connect() as db:
            if db.execute("SELECT 1 FROM work_items WHERE work_item_id=?", (work_item_id,)).fetchone() is None:
                raise ValueError("unknown work item")
            db.execute("INSERT INTO work_attempts (attempt_id, work_item_id, provider_id, status, output_json, created_at) VALUES (?, ?, ?, ?, ?, ?)", (attempt.attempt_id, work_item_id, attempt.provider_id, attempt.status, "{}", now))
            db.execute("UPDATE work_items SET status='queued', updated_at=? WHERE work_item_id=?", (now, work_item_id))
        return attempt

    def _attempt_row(self, row: sqlite3.Row | None) -> AttemptRecord | None:
        if row is None:
            return None
        return AttemptRecord(row["attempt_id"], row["work_item_id"], row["provider_id"], row["status"], row["lease_id"], row["lease_expires_at"], json.loads(row["output_json"]), row["error"], row["created_at"], row["started_at"], row["completed_at"])

    def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        with self._connect() as db:
            return self._attempt_row(db.execute("SELECT * FROM work_attempts WHERE attempt_id=?", (attempt_id,)).fetchone())

    def list_attempts(self, work_item_id: str) -> list[AttemptRecord]:
        with self._connect() as db:
            # ``rowid`` breaks ties: attempts created within the same second are
            # common (an approval creates its successor immediately), and the
            # first element is what the UI shows as "the latest attempt".
            rows = db.execute("SELECT * FROM work_attempts WHERE work_item_id=? ORDER BY created_at DESC, rowid DESC", (work_item_id,)).fetchall()
        return [self._attempt_row(row) for row in rows if row is not None]

    def public_review_evidence(
        self, task_ids: tuple[str, ...]
    ) -> dict[str, dict[str, Any]]:
        """Return a redacted task/attempt summary for route acceptance review.

        Project paths, task inputs, objectives, patches, command output and
        artifacts stay private.  The review needs only enough evidence to tell
        whether the routed task existed, reached a terminal state, was verified,
        produced a patch, or applied anything to the source workspace.
        """
        ids = tuple(dict.fromkeys(str(item) for item in task_ids if str(item).strip()))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT w.work_item_id, w.conversation_id, w.task_type,
                    w.status AS work_status, a.status AS attempt_status,
                    a.output_json, a.error
                FROM work_items w
                LEFT JOIN work_attempts a ON a.attempt_id = (
                    SELECT latest.attempt_id FROM work_attempts latest
                    WHERE latest.work_item_id=w.work_item_id
                    ORDER BY latest.created_at DESC LIMIT 1
                )
                WHERE w.work_item_id IN ({placeholders})""",
                ids,
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                attempt_output = json.loads(row["output_json"] or "{}")
            except (TypeError, ValueError):
                attempt_output = {}
            if not isinstance(attempt_output, dict):
                attempt_output = {}
            output[str(row["work_item_id"])] = {
                "conversation_id": str(row["conversation_id"] or ""),
                "task_type": str(row["task_type"] or ""),
                "status": str(row["work_status"] or ""),
                "attempt_status": str(row["attempt_status"] or ""),
                "verification_status": str(
                    attempt_output.get("verification_status") or ""
                ),
                "has_patch": bool(attempt_output.get("patch")),
                "apply_status": str(attempt_output.get("apply_status") or ""),
                "error_present": bool(row["error"]),
            }
        return output

    def recover_runtime(self) -> list[str]:
        """Mark in-flight attempts interrupted; return queued attempt ids."""
        now = _iso(_now())
        with self._connect() as db:
            rows = db.execute("SELECT attempt_id, work_item_id FROM work_attempts WHERE status='running'").fetchall()
            for row in rows:
                db.execute("UPDATE work_attempts SET status='interrupted', error='host restarted', completed_at=?, lease_id=NULL, lease_expires_at=NULL WHERE attempt_id=?", (now, row["attempt_id"]))
                db.execute("UPDATE work_items SET status='interrupted', updated_at=? WHERE work_item_id=?", (now, row["work_item_id"]))
            queued = db.execute("SELECT attempt_id FROM work_attempts WHERE status='queued'").fetchall()
        return [str(row["attempt_id"]) for row in queued]

    def recover_expired_attempts(
        self,
        *,
        active_attempt_ids: Sequence[str] = (),
        now: datetime | None = None,
    ) -> list[dict[str, str]]:
        """Recover expired leases once, leaving confirmed active processes alone.

        The attempt row itself is the durable idempotency key: after it becomes
        ``interrupted`` later polls cannot process it again.  Callers must pass
        the attempt ids observed in live worker processes before invoking this.
        """
        current = now or _now()
        now_text = _iso(current)
        active = {str(item) for item in active_attempt_ids}
        recovered: list[dict[str, str]] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT attempt_id, work_item_id FROM work_attempts "
                "WHERE status='running' AND lease_expires_at IS NOT NULL "
                "AND lease_expires_at <= ? ORDER BY created_at",
                (now_text,),
            ).fetchall()
            for row in rows:
                attempt_id = str(row["attempt_id"])
                if attempt_id in active:
                    continue
                reason = "lease expired; no active process confirmed"
                updated = db.execute(
                    "UPDATE work_attempts SET status='interrupted', error=?, "
                    "completed_at=?, lease_id=NULL, lease_expires_at=NULL "
                    "WHERE attempt_id=? AND status='running' "
                    "AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?",
                    (reason, now_text, attempt_id, now_text),
                )
                if updated.rowcount != 1:
                    continue
                db.execute(
                    "UPDATE work_items SET status='interrupted', updated_at=? "
                    "WHERE work_item_id=? AND status='running'",
                    (now_text, row["work_item_id"]),
                )
                recovered.append({"attempt_id": attempt_id, "reason": reason})
        return recovered

    def claim_attempt(self, attempt_id: str, *, lease_seconds: int = 300) -> AttemptRecord | None:
        now = _now(); now_text = _iso(now); lease_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cur = db.execute("UPDATE work_attempts SET status='running', lease_id=?, lease_expires_at=?, started_at=? WHERE attempt_id=? AND status='queued'", (lease_id, _iso(now + timedelta(seconds=max(1, lease_seconds))), now_text, attempt_id))
            if cur.rowcount != 1:
                return None
            row = db.execute("SELECT work_item_id FROM work_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            db.execute("UPDATE work_items SET status='running', updated_at=? WHERE work_item_id=?", (now_text, row["work_item_id"]))
            return self._attempt_row(db.execute("SELECT * FROM work_attempts WHERE attempt_id=?", (attempt_id,)).fetchone())

    def append_event(self, attempt_id: str, event_type: str, payload: Mapping[str, Any]) -> EventRecord:
        now = _iso(_now()); event_id = uuid.uuid4().hex
        with self._connect() as db:
            row = db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM work_events WHERE attempt_id=?", (attempt_id,)).fetchone()
            sequence = int(row["sequence"])
            db.execute("INSERT INTO work_events VALUES (?, ?, ?, ?, ?, ?)", (event_id, attempt_id, sequence, event_type, _json(payload), now))
        return EventRecord(event_id, attempt_id, sequence, event_type, dict(payload), now)

    def list_events(self, attempt_id: str, *, after_sequence: int = 0) -> list[EventRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM work_events WHERE attempt_id=? AND sequence>? ORDER BY sequence", (attempt_id, after_sequence)).fetchall()
        return [EventRecord(r["event_id"], r["attempt_id"], r["sequence"], r["event_type"], json.loads(r["payload_json"]), r["created_at"]) for r in rows]

    def save_checkpoint(self, attempt_id: str, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        now = _iso(_now())
        with self._connect() as db:
            db.execute("INSERT INTO work_checkpoints VALUES (?, ?, ?) ON CONFLICT(attempt_id) DO UPDATE SET checkpoint_json=excluded.checkpoint_json, updated_at=excluded.updated_at", (attempt_id, _json(checkpoint), now))
        return dict(checkpoint)

    def get_checkpoint(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT checkpoint_json FROM work_checkpoints WHERE attempt_id=?", (attempt_id,)).fetchone()
        return json.loads(row["checkpoint_json"]) if row else None

    def create_approval(self, attempt_id: str, payload: Mapping[str, Any]) -> ApprovalRecord:
        now = _iso(_now()); approval = ApprovalRecord(uuid.uuid4().hex, attempt_id, dict(payload), "pending", now)
        with self._connect() as db:
            db.execute("INSERT INTO work_approvals VALUES (?, ?, ?, ?, ?, ?)", (approval.approval_id, attempt_id, _json(payload), approval.status, now, None))
            db.execute("UPDATE work_attempts SET status='waiting_approval' WHERE attempt_id=? AND status='running'", (attempt_id,))
            db.execute("UPDATE work_items SET status='waiting_approval', updated_at=? WHERE work_item_id=(SELECT work_item_id FROM work_attempts WHERE attempt_id=?)", (now, attempt_id))
        return approval

    def pause_for_approval(
        self,
        attempt_id: str,
        checkpoint: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> ApprovalRecord:
        """Atomically persist a provider checkpoint and its user-facing approval."""
        now = _iso(_now())
        approval = ApprovalRecord(
            uuid.uuid4().hex, attempt_id, dict(payload), "pending", now
        )
        with self._connect() as db:
            row = db.execute(
                "SELECT work_item_id, status FROM work_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError("attempt is not running")
            db.execute(
                "INSERT INTO work_checkpoints VALUES (?, ?, ?) "
                "ON CONFLICT(attempt_id) DO UPDATE SET "
                "checkpoint_json=excluded.checkpoint_json, updated_at=excluded.updated_at",
                (attempt_id, _json(checkpoint), now),
            )
            db.execute(
                "INSERT INTO work_approvals VALUES (?, ?, ?, ?, ?, ?)",
                (
                    approval.approval_id,
                    attempt_id,
                    _json(payload),
                    approval.status,
                    now,
                    None,
                ),
            )
            db.execute(
                "UPDATE work_attempts SET status='waiting_approval' WHERE attempt_id=?",
                (attempt_id,),
            )
            db.execute(
                "UPDATE work_items SET status='waiting_approval', updated_at=? WHERE work_item_id=?",
                (now, row["work_item_id"]),
            )
        return approval

    def _approval_row(self, row: sqlite3.Row | None) -> ApprovalRecord | None:
        return ApprovalRecord(row["approval_id"], row["attempt_id"], json.loads(row["payload_json"]), row["status"], row["created_at"], row["decided_at"]) if row else None

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._connect() as db:
            return self._approval_row(db.execute("SELECT * FROM work_approvals WHERE approval_id=?", (approval_id,)).fetchone())

    def list_approvals(self, work_item_id: str, scope: Scope) -> list[ApprovalRecord]:
        with self._connect() as db:
            rows = db.execute("SELECT a.* FROM work_approvals a JOIN work_attempts t ON t.attempt_id=a.attempt_id JOIN work_items w ON w.work_item_id=t.work_item_id WHERE w.work_item_id=? AND w.owner_id=? AND w.channel=? AND w.conversation_id=? AND w.module_id=? ORDER BY a.created_at DESC", (work_item_id, *self._scope_args(scope))).fetchall()
        return [self._approval_row(row) for row in rows if row is not None]

    def decide_approval(self, approval_id: str, decision: str) -> bool:
        if decision not in {"approved", "rejected", "expired"}:
            raise ValueError("invalid approval decision")
        now = _iso(_now())
        with self._connect() as db:
            cur = db.execute("UPDATE work_approvals SET status=?, decided_at=? WHERE approval_id=? AND status='pending'", (decision, now, approval_id))
        return cur.rowcount == 1

    def pending_approval_ids_for_owner(self, owner_id: str) -> list[str]:
        """该 owner（跨全部会话、全部 module）所有仍 pending 的 approval id。"""
        with self._connect() as db:
            rows = db.execute(
                """SELECT a.approval_id FROM work_approvals a
                   JOIN work_attempts t ON t.attempt_id = a.attempt_id
                   JOIN work_items w ON w.work_item_id = t.work_item_id
                   WHERE w.owner_id=? AND a.status='pending'""",
                (str(owner_id or "").strip(),),
            ).fetchall()
        return [row["approval_id"] for row in rows]

    def waiting_plan_steps_for_owner(self, owner_id: str) -> list[tuple[str, str]]:
        """该 owner 里停在 ``waiting_approval`` 的 (plan_id, step_id)。"""
        with self._connect() as db:
            rows = db.execute(
                """SELECT DISTINCT s.plan_id, s.step_id FROM plan_steps s
                   JOIN control_plans p ON p.plan_id = s.plan_id
                   WHERE p.owner_id=? AND s.status='waiting_approval'""",
                (str(owner_id or "").strip(),),
            ).fetchall()
        return [(row["plan_id"], row["step_id"]) for row in rows]

    def in_flight_plan_ids_for_owner(self, owner_id: str) -> list[str]:
        """该 owner 里仍处于在飞状态（queued/running/waiting_approval）的 plan_id。"""
        placeholders = ",".join("?" for _ in PLAN_IN_FLIGHT_STATUSES)
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT DISTINCT p.plan_id FROM control_plans p
                    JOIN plan_steps s ON s.plan_id = p.plan_id
                    WHERE p.owner_id=? AND s.status IN ({placeholders})""",
                (str(owner_id or "").strip(), *sorted(PLAN_IN_FLIGHT_STATUSES)),
            ).fetchall()
        return [row["plan_id"] for row in rows]

    def terminate_plan_step(self, plan_id: str, step_id: str, *, reason: str = "permission_mode_changed") -> bool:
        """把单个等待中的步骤推进到终态 ``cancelled``，并留下可追溯的 plan 事件。

        调用方持有的通常是 ``waiting_approval`` 的步骤；这里不做状态预判，
        用 ``WHERE status IN (在飞)`` 兜住并发，避免把已终结的步骤改回去。
        """
        now = _iso(_now())
        active = tuple(sorted(STEP_ACTIVE_STATUSES | {"pending"}))
        placeholders = ",".join("?" for _ in active)
        with self._connect() as db:
            cur = db.execute(
                f"UPDATE plan_steps SET status='cancelled', error=?, updated_at=? "
                f"WHERE plan_id=? AND step_id=? AND status IN ({placeholders})",
                (reason, now, plan_id, step_id, *active),
            )
            if cur.rowcount != 1:
                return False
            row = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM plan_events WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO plan_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    plan_id,
                    int(row["sequence"]),
                    step_id,
                    None,
                    None,
                    "step.cancelled",
                    _json({"reason": reason}),
                    now,
                ),
            )
        return True

    def terminal_plan_if_settled(self, plan_id: str, *, reason: str = "permission_mode_changed") -> bool:
        """步骤都终结后，把仍非终态的 plan 收口成 ``cancelled``。"""
        now = _iso(_now())
        with self._connect() as db:
            row = db.execute("SELECT status FROM control_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if row is None or row["status"] in PLAN_TERMINAL_STATUSES:
                return False
            live = db.execute(
                f"SELECT COUNT(*) AS n FROM plan_steps WHERE plan_id=? AND status NOT IN "
                f"({','.join('?' for _ in STEP_TERMINAL_STATUSES)})",
                (plan_id, *sorted(STEP_TERMINAL_STATUSES)),
            ).fetchone()
            if int(live["n"]) > 0:
                return False
            db.execute("UPDATE control_plans SET status='cancelled', updated_at=? WHERE plan_id=?", (now, plan_id))
            seq_row = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM plan_events WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO plan_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    plan_id,
                    int(seq_row["sequence"]),
                    None,
                    None,
                    None,
                    "plan.cancelled",
                    _json({"reason": reason}),
                    now,
                ),
            )
        return True

    def record_file_operation(self, attempt_id: str, operation: Mapping[str, Any]) -> str:
        operation_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO work_file_operations VALUES (?, ?, ?, ?)", (operation_id, attempt_id, _json(operation), _iso(_now())))
        return operation_id

    def list_file_operations(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT operation_json FROM work_file_operations WHERE attempt_id=? ORDER BY created_at", (attempt_id,)).fetchall()
        return [json.loads(row["operation_json"]) for row in rows]

    def record_artifact(self, attempt_id: str, *, media_type: str, path: str, size_bytes: int, expires_at: str) -> ArtifactRecord:
        record = ArtifactRecord(uuid.uuid4().hex, attempt_id, media_type, path, int(size_bytes), _iso(_now()), expires_at)
        with self._connect() as db:
            db.execute("INSERT INTO work_artifacts VALUES (?, ?, ?, ?, ?, ?, ?)", (record.artifact_id, record.attempt_id, record.media_type, record.path, record.size_bytes, record.created_at, record.expires_at))
        return record

    def create_artifact(
        self,
        *,
        attempt_id: str,
        scope: Scope,
        media_type: str,
        size_bytes: int,
        path: str,
        expires_at: str,
    ) -> ArtifactRecord:
        """Create an artifact after verifying the attempt belongs to ``scope``."""
        with self._connect() as db:
            row = db.execute(
                """
                SELECT 1
                FROM work_attempts t
                JOIN work_items w ON w.work_item_id=t.work_item_id
                WHERE t.attempt_id=? AND w.owner_id=? AND w.channel=?
                  AND w.conversation_id=? AND w.module_id=?
                """,
                (attempt_id, *self._scope_args(scope)),
            ).fetchone()
        if row is None:
            raise ValueError("attempt does not belong to scope")
        return self.record_artifact(
            attempt_id,
            media_type=media_type,
            path=path,
            size_bytes=size_bytes,
            expires_at=expires_at,
        )

    def get_artifact(self, artifact_id: str, scope: Scope) -> ArtifactRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT a.* FROM work_artifacts a JOIN work_attempts t ON t.attempt_id=a.attempt_id JOIN work_items w ON w.work_item_id=t.work_item_id WHERE a.artifact_id=? AND w.owner_id=? AND w.channel=? AND w.conversation_id=? AND w.module_id=?", (artifact_id, *self._scope_args(scope))).fetchone()
        return ArtifactRecord(row["artifact_id"], row["attempt_id"], row["media_type"], row["path"], row["size_bytes"], row["created_at"], row["expires_at"]) if row else None

    def list_artifacts(self, attempt_id: str, scope: Scope) -> list[ArtifactRecord]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT a.*
                FROM work_artifacts a
                JOIN work_attempts t ON t.attempt_id=a.attempt_id
                JOIN work_items w ON w.work_item_id=t.work_item_id
                WHERE a.attempt_id=? AND w.owner_id=? AND w.channel=?
                  AND w.conversation_id=? AND w.module_id=?
                ORDER BY a.created_at
                """,
                (attempt_id, *self._scope_args(scope)),
            ).fetchall()
        return [
            ArtifactRecord(
                row["artifact_id"],
                row["attempt_id"],
                row["media_type"],
                row["path"],
                row["size_bytes"],
                row["created_at"],
                row["expires_at"],
            )
            for row in rows
        ]

    def list_expired_artifacts(self, *, now: str | None = None) -> list[ArtifactRecord]:
        cutoff = now or _iso(_now())
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM work_artifacts WHERE expires_at <= ? ORDER BY expires_at",
                (cutoff,),
            ).fetchall()
        return [
            ArtifactRecord(
                row["artifact_id"],
                row["attempt_id"],
                row["media_type"],
                row["path"],
                row["size_bytes"],
                row["created_at"],
                row["expires_at"],
            )
            for row in rows
        ]

    def delete_artifact(self, artifact_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute("DELETE FROM work_artifacts WHERE artifact_id=?", (artifact_id,))
        return cur.rowcount == 1

    # ------------------------------------------------------------------ #
    # Provider activity projection
    # ------------------------------------------------------------------ #

    def save_provider_activity(self, activity: ProviderActivity) -> dict[str, Any]:
        """Upsert the provider projection for one attempt.

        Last write wins is correct here: the provider always sends a *snapshot*
        (not a delta), so an out-of-order write would at worst show a slightly
        older step, never a corrupted one. The attempt id is the primary key, so
        a replayed event after a restart updates the same row.
        """
        snapshot = activity.as_dict()
        now = _iso(_now())
        with self._connect() as db:
            db.execute(
                """INSERT INTO provider_activity
                       (attempt_id, provider_id, session_id, status, confidence, activity_json,
                        started_at, updated_at, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(attempt_id) DO UPDATE SET
                       provider_id=excluded.provider_id, session_id=excluded.session_id,
                       status=excluded.status, confidence=excluded.confidence,
                       activity_json=excluded.activity_json, started_at=excluded.started_at,
                       updated_at=excluded.updated_at, finished_at=excluded.finished_at""",
                (
                    activity.attempt_id,
                    activity.provider_id,
                    activity.session_id,
                    activity.status,
                    activity.confidence,
                    _json(snapshot),
                    activity.started_at,
                    activity.updated_at or now,
                    activity.finished_at,
                ),
            )
        return snapshot

    def get_provider_activity(self, attempt_id: str) -> dict[str, Any] | None:
        return self.list_provider_activities([attempt_id]).get(str(attempt_id))

    def list_provider_activities(
        self, attempt_ids: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Bulk read used by the task detail endpoint (one query, not N)."""
        ids = tuple(dict.fromkeys(str(item) for item in attempt_ids if str(item).strip()))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as db:
            rows = db.execute(
                f"SELECT activity_json FROM provider_activity WHERE attempt_id IN ({placeholders})",
                ids,
            ).fetchall()
        output: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                payload = json.loads(row["activity_json"] or "{}")
            except (TypeError, ValueError):
                continue
            activity = ProviderActivity.from_dict(payload)
            if activity is None:
                continue
            output[activity.attempt_id] = activity.as_dict()
        return output

    def interrupted_provider_attempts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Interrupted attempts that still own a live external session.

        Recovery rules encoded in the query (each one matters):

        * ``session_id`` must exist — without it there is nothing to resume;
        * the last recorded provider status must not be terminal-unknown — a task
          whose result is unknown is never re-run automatically;
        * the attempt must have **no newer sibling** — that is the idempotency
          key. Recovery creates a successor attempt, and the successor is then
          the only candidate, so restarting twice cannot multiply attempts.
        """
        with self._connect() as db:
            rows = db.execute(
                """SELECT pa.attempt_id AS attempt_id, pa.session_id AS session_id,
                          pa.status AS activity_status, pa.activity_json AS activity_json,
                          a.work_item_id AS work_item_id,
                          COALESCE(NULLIF(pa.provider_id, ''), a.provider_id) AS provider_id,
                          a.created_at AS created_at
                   FROM provider_activity pa
                   JOIN work_attempts a ON a.attempt_id = pa.attempt_id
                   WHERE a.status='interrupted'
                     AND pa.session_id IS NOT NULL AND pa.session_id != ''
                     AND pa.status NOT IN ('completed','failed','disconnected','unknown_result')
                     AND NOT EXISTS (
                         SELECT 1 FROM work_attempts newer
                         WHERE newer.work_item_id = a.work_item_id
                           AND (newer.created_at > a.created_at
                                OR (newer.created_at = a.created_at AND newer.rowid > a.rowid))
                     )
                   ORDER BY a.created_at DESC
                   LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                activity = json.loads(row["activity_json"] or "{}")
            except (TypeError, ValueError):
                activity = {}
            output.append(
                {
                    "attempt_id": str(row["attempt_id"]),
                    "work_item_id": str(row["work_item_id"]),
                    "provider_id": str(row["provider_id"] or ""),
                    "session_id": str(row["session_id"] or ""),
                    "activity_status": str(row["activity_status"] or ""),
                    "activity": dict(activity) if isinstance(activity, dict) else {},
                    "created_at": str(row["created_at"] or ""),
                }
            )
        return output

    def latest_attempt_status(self, work_item_id: str) -> str:
        with self._connect() as db:
            row = db.execute(
                "SELECT status FROM work_attempts WHERE work_item_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (work_item_id,),
            ).fetchone()
        return str(row["status"]) if row is not None else ""

    def finish_attempt(self, attempt_id: str, *, status: str, output: Mapping[str, Any] | None = None, error: str | None = None) -> bool:
        if status not in {"completed", "failed", "cancelled", "interrupted", "unknown_result"}:
            raise ValueError("invalid terminal attempt status")
        now = _iso(_now())
        with self._connect() as db:
            row = db.execute("SELECT work_item_id FROM work_attempts WHERE attempt_id=? AND status IN ('running','waiting_approval','queued')", (attempt_id,)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE work_attempts SET status=?, output_json=?, error=?, completed_at=?, lease_id=NULL, lease_expires_at=NULL WHERE attempt_id=?", (status, _json(output or {}), error, now, attempt_id))
            db.execute("UPDATE work_items SET status=?, updated_at=? WHERE work_item_id=?", (status, now, row["work_item_id"]))
        return True

    # ------------------------------------------------------------------ #
    # Control plan persistence
    # ------------------------------------------------------------------ #

    def create_plan(
        self,
        *,
        scope: Scope,
        objective: str,
        steps: Sequence[PlanStepDefinition],
        status: str = "queued",
    ) -> ControlPlan:
        """Persist a complete validated plan in one transaction.

        The whole DAG is written before the Host creates any WorkItem, so a
        crash can never leave a half-described plan behind.
        """
        if not str(objective or "").strip():
            raise ValueError("plan objective is required")
        if not steps:
            raise ValueError("a plan requires at least one step")
        if status not in PLAN_STATUSES:
            raise ValueError(f"invalid plan status: {status}")
        now = _iso(_now())
        plan = ControlPlan(uuid.uuid4().hex, scope, str(objective).strip(), status, now, now)
        with self._connect() as db:
            db.execute(
                "INSERT INTO control_plans (plan_id, owner_id, channel, conversation_id, module_id, objective, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan.plan_id, *self._scope_args(scope), plan.objective, plan.status, now, now),
            )
            for position, step in enumerate(steps):
                db.execute(
                    "INSERT INTO plan_steps (step_id, plan_id, task_type, objective, input_json, requires_approval, failure_policy, status, work_item_id, output_json, error, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}', NULL, ?, ?, ?)",
                    (
                        step.step_id,
                        plan.plan_id,
                        step.task_type,
                        step.objective,
                        _json(step.input),
                        1 if step.requires_approval else 0,
                        step.failure_policy,
                        "pending",
                        position,
                        now,
                        now,
                    ),
                )
                for parent in step.depends_on:
                    db.execute(
                        "INSERT INTO plan_step_dependencies (plan_id, step_id, depends_on_step_id) VALUES (?, ?, ?)",
                        (plan.plan_id, step.step_id, parent),
                    )
        return plan

    def _plan_row(self, row: sqlite3.Row | None) -> ControlPlan | None:
        if row is None:
            return None
        return ControlPlan(
            row["plan_id"],
            Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
            row["objective"],
            row["status"],
            row["created_at"],
            row["updated_at"],
        )

    def get_plan(self, plan_id: str, scope: Scope) -> ControlPlan | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM control_plans WHERE plan_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (plan_id, *self._scope_args(scope)),
            ).fetchone()
        return self._plan_row(row)

    def get_plan_unscoped(self, plan_id: str) -> ControlPlan | None:
        """Internal read used by the scheduler, which already holds a scoped plan."""
        with self._connect() as db:
            row = db.execute("SELECT * FROM control_plans WHERE plan_id=?", (plan_id,)).fetchone()
        return self._plan_row(row)

    def list_plans(self, scope: Scope, *, limit: int = 50) -> list[ControlPlan]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM control_plans WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? ORDER BY created_at DESC LIMIT ?",
                (*self._scope_args(scope), max(1, min(100, int(limit)))),
            ).fetchall()
        return [plan for plan in (self._plan_row(row) for row in rows) if plan is not None]

    def update_plan_status(self, plan_id: str, status: str) -> bool:
        if status not in PLAN_STATUSES:
            raise ValueError(f"invalid plan status: {status}")
        now = _iso(_now())
        with self._connect() as db:
            cur = db.execute("UPDATE control_plans SET status=?, updated_at=? WHERE plan_id=?", (status, now, plan_id))
        return cur.rowcount == 1

    def _step_row(self, row: sqlite3.Row | None, depends_on: Sequence[str] = ()) -> PlanStep | None:
        if row is None:
            return None
        return PlanStep(
            step_id=row["step_id"],
            plan_id=row["plan_id"],
            task_type=row["task_type"],
            objective=row["objective"],
            input=json.loads(row["input_json"] or "{}"),
            depends_on=tuple(depends_on),
            requires_approval=bool(row["requires_approval"]),
            failure_policy=row["failure_policy"],
            status=row["status"],
            work_item_id=row["work_item_id"],
            output=json.loads(row["output_json"] or "{}"),
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def plan_step_dependencies(self, plan_id: str) -> dict[str, tuple[str, ...]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT step_id, depends_on_step_id FROM plan_step_dependencies WHERE plan_id=? ORDER BY step_id",
                (plan_id,),
            ).fetchall()
        dependencies: dict[str, tuple[str, ...]] = {}
        for row in rows:
            dependencies.setdefault(row["step_id"], ())
            dependencies[row["step_id"]] = (*dependencies[row["step_id"]], row["depends_on_step_id"])
        return dependencies

    def list_plan_steps(self, plan_id: str) -> list[PlanStep]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM plan_steps WHERE plan_id=? ORDER BY position, step_id", (plan_id,)).fetchall()
        dependencies = self.plan_step_dependencies(plan_id)
        steps: list[PlanStep] = []
        for row in rows:
            step = self._step_row(row, dependencies.get(row["step_id"], ()))
            if step is not None:
                steps.append(step)
        return steps

    def get_plan_step(self, plan_id: str, step_id: str) -> PlanStep | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM plan_steps WHERE plan_id=? AND step_id=?", (plan_id, step_id)).fetchone()
        return self._step_row(row, self.plan_step_dependencies(plan_id).get(step_id, ()))

    def get_step_by_work_item(self, work_item_id: str) -> PlanStep | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM plan_steps WHERE work_item_id=?", (work_item_id,)).fetchone()
            if row is None:
                return None
            dependencies = db.execute(
                "SELECT depends_on_step_id FROM plan_step_dependencies WHERE plan_id=? AND step_id=?",
                (row["plan_id"], row["step_id"]),
            ).fetchall()
        return self._step_row(row, [item["depends_on_step_id"] for item in dependencies])

    def update_step_status(
        self,
        plan_id: str,
        step_id: str,
        status: str,
        *,
        work_item_id: Any = _UNSET,
        output: Any = _UNSET,
        error: Any = _UNSET,
    ) -> bool:
        if status not in STEP_STATUSES:
            raise ValueError(f"invalid plan step status: {status}")
        assignments = ["status=?", "updated_at=?"]
        values: list[Any] = [status, _iso(_now())]
        if work_item_id is not _UNSET:
            assignments.append("work_item_id=?")
            values.append(work_item_id)
        if output is not _UNSET:
            assignments.append("output_json=?")
            values.append(_json(output or {}))
        if error is not _UNSET:
            assignments.append("error=?")
            values.append(error)
        values.extend([plan_id, step_id])
        with self._connect() as db:
            cur = db.execute(f"UPDATE plan_steps SET {', '.join(assignments)} WHERE plan_id=? AND step_id=?", tuple(values))
        return cur.rowcount == 1

    def bind_step_work_item(self, plan_id: str, step_id: str, work_item_id: str) -> bool:
        return self.update_step_status(plan_id, step_id, "ready", work_item_id=work_item_id)

    def save_step_output(self, plan_id: str, step_id: str, output: Mapping[str, Any]) -> bool:
        now = _iso(_now())
        with self._connect() as db:
            cur = db.execute(
                "UPDATE plan_steps SET output_json=?, updated_at=? WHERE plan_id=? AND step_id=?",
                (_json(output or {}), now, plan_id, step_id),
            )
        return cur.rowcount == 1

    def ready_plan_steps(self, plan_id: str) -> list[PlanStep]:
        """``pending`` steps whose dependencies have all completed."""
        steps = self.list_plan_steps(plan_id)
        statuses = {step.step_id: step.status for step in steps}
        return [
            step
            for step in steps
            if step.status == "pending"
            and all(statuses.get(parent) == "completed" for parent in step.depends_on)
        ]

    # ------------------------------------------------------------------ #
    # Control plan events
    # ------------------------------------------------------------------ #

    def append_plan_event(
        self,
        plan_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        step_id: str | None = None,
        work_item_id: str | None = None,
        attempt_id: str | None = None,
    ) -> PlanEvent:
        now = _iso(_now())
        event_id = uuid.uuid4().hex
        with self._connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM plan_events WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            db.execute(
                "INSERT INTO plan_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, plan_id, sequence, step_id, work_item_id, attempt_id, event_type, _json(payload), now),
            )
        return PlanEvent(event_id, plan_id, sequence, event_type, dict(payload), now, step_id, work_item_id, attempt_id)

    def list_plan_events(self, plan_id: str, *, after_sequence: int = 0) -> list[PlanEvent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM plan_events WHERE plan_id=? AND sequence>? ORDER BY sequence",
                (plan_id, max(0, int(after_sequence))),
            ).fetchall()
        return [
            PlanEvent(
                row["event_id"],
                row["plan_id"],
                row["sequence"],
                row["event_type"],
                json.loads(row["payload_json"]),
                row["created_at"],
                row["step_id"],
                row["work_item_id"],
                row["attempt_id"],
            )
            for row in rows
        ]

    def recover_plans(self) -> list[str]:
        """Mark in-flight plans interrupted. Nothing is replayed automatically."""
        now = _iso(_now())
        interrupted: list[str] = []
        with self._connect() as db:
            plans = db.execute(
                f"SELECT plan_id FROM control_plans WHERE status IN ({', '.join('?' * len(PLAN_IN_FLIGHT_STATUSES))})",
                tuple(sorted(PLAN_IN_FLIGHT_STATUSES)),
            ).fetchall()
            for plan in plans:
                plan_id = plan["plan_id"]
                db.execute(
                    f"UPDATE plan_steps SET status='interrupted', updated_at=? WHERE plan_id=? AND status IN ({', '.join('?' * len(_PLAN_STEP_IN_FLIGHT))})",
                    (now, plan_id, *_PLAN_STEP_IN_FLIGHT),
                )
                db.execute("UPDATE control_plans SET status='interrupted', updated_at=? WHERE plan_id=?", (now, plan_id))
                interrupted.append(plan_id)
        return interrupted
