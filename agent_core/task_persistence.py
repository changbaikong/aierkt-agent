"""Durable task definitions and execution leases."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import AccountTaskScope, Scope


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    scope: Scope
    title: str
    request_text: str
    delivery_mode: str
    schedule: dict[str, Any]
    allowed_tools: list[str]
    status: str
    next_run_at: str | None
    last_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskRunRecord:
    run_id: str
    task_id: str
    status: str
    scheduled_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: str | None = None
    error: str | None = None
    lease_id: str | None = None


class TaskStore:
    def __init__(self, path: Path) -> None:
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
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, module_id TEXT NOT NULL,
                    title TEXT NOT NULL, request_text TEXT NOT NULL,
                    delivery_mode TEXT NOT NULL, schedule_json TEXT NOT NULL,
                    allowed_tools_json TEXT NOT NULL, status TEXT NOT NULL,
                    next_run_at TEXT, last_error TEXT,
                    lease_id TEXT, lease_expires_at TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(status, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_scope ON tasks(owner_id, channel, conversation_id, module_id);
                CREATE TABLE IF NOT EXISTS task_runs (
                    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, lease_id TEXT,
                    status TEXT NOT NULL, scheduled_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, result TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, scheduled_at DESC);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(task_runs)")}
            if "lease_id" not in columns:
                db.execute("ALTER TABLE task_runs ADD COLUMN lease_id TEXT")

    @staticmethod
    def _scope_args(scope: Scope) -> tuple[str, str, str, str]:
        return scope.owner_id, scope.channel, scope.conversation_id, scope.module_id

    @staticmethod
    def _account_args(scope: Scope | AccountTaskScope) -> tuple[str, str]:
        if isinstance(scope, AccountTaskScope):
            return scope.account_id, scope.module_id
        return scope.owner_id, scope.module_id

    def create_task(
        self, *, scope: Scope, title: str, request_text: str, delivery_mode: str,
        schedule: dict[str, Any], allowed_tools: list[str], next_run_at: str | None,
    ) -> TaskRecord:
        now = _iso(_now())
        task = TaskRecord(
            uuid.uuid4().hex, scope, title, request_text, delivery_mode,
            dict(schedule), list(allowed_tools), "scheduled", next_run_at, None, now, now,
        )
        with self._connect() as db:
            db.execute(
                """INSERT INTO tasks (task_id, owner_id, channel, conversation_id, module_id,
                title, request_text, delivery_mode, schedule_json, allowed_tools_json, status,
                next_run_at, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (task.task_id, *self._scope_args(scope), title, request_text, delivery_mode,
                 json.dumps(schedule, ensure_ascii=False), json.dumps(allowed_tools),
                 task.status, next_run_at, None, now, now),
            )
        return task

    def _row_to_task(self, row: sqlite3.Row) -> TaskRecord:
        scope = Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
        return TaskRecord(
            row["task_id"], scope, row["title"], row["request_text"], row["delivery_mode"],
            json.loads(row["schedule_json"]), json.loads(row["allowed_tools_json"]),
            row["status"], row["next_run_at"], row["last_error"], row["created_at"], row["updated_at"],
        )

    def get_task(self, task_id: str, scope: Scope | AccountTaskScope) -> TaskRecord | None:
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                row = db.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=? AND module_id=?", (task_id, *self._account_args(scope))).fetchone()
            else:
                row = db.execute("SELECT * FROM tasks WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?", (task_id, *self._scope_args(scope))).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, scope: Scope | AccountTaskScope, status: str | None = None, limit: int = 50) -> list[TaskRecord]:
        if isinstance(scope, AccountTaskScope):
            query = "SELECT * FROM tasks WHERE owner_id=? AND module_id=?"
            args: list[Any] = list(self._account_args(scope))
        else:
            query = "SELECT * FROM tasks WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?"
            args = list(self._scope_args(scope))
        if status:
            query += " AND status=?"
            args.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self._connect() as db:
            rows = db.execute(query, args).fetchall()
        return [self._row_to_task(row) for row in rows]

    def cancel_task(self, task_id: str, scope: Scope | AccountTaskScope) -> bool:
        now = _iso(_now())
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                cur = db.execute("UPDATE tasks SET status='cancelled', next_run_at=NULL, updated_at=? WHERE task_id=? AND owner_id=? AND module_id=? AND status IN ('scheduled','running')", (now, task_id, *self._account_args(scope)))
            else:
                cur = db.execute("UPDATE tasks SET status='cancelled', next_run_at=NULL, updated_at=? WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status IN ('scheduled','running')", (now, task_id, *self._scope_args(scope)))
            if cur.rowcount == 1:
                db.execute(
                    "UPDATE task_runs SET status='cancelled', completed_at=?, error='task cancelled' WHERE task_id=? AND status='running'",
                    (now, task_id),
                )
        return cur.rowcount == 1

    def claim_due_task(self, task_id: str, scope: Scope, lease_seconds: int = 300, now: datetime | None = None) -> TaskRunRecord | None:
        now = now or _now()
        now_text = _iso(now)
        lease_id = uuid.uuid4().hex
        expires = _iso(now + timedelta(seconds=max(1, lease_seconds)))
        run_id = uuid.uuid4().hex
        with self._connect() as db:
            stale = db.execute(
                "SELECT task_id, lease_id FROM tasks WHERE status='running' AND lease_expires_at <= ?",
                (now_text,),
            ).fetchall()
            for row in stale:
                db.execute(
                    """UPDATE task_runs SET status='failed', completed_at=?, error='lease expired'
                    WHERE task_id=? AND lease_id=? AND status='running'""",
                    (now_text, row["task_id"], row["lease_id"]),
                )
                db.execute(
                    """UPDATE tasks SET status='scheduled', lease_id=NULL, lease_expires_at=NULL,
                    updated_at=? WHERE task_id=? AND status='running' AND lease_id=?""",
                    (now_text, row["task_id"], row["lease_id"]),
                )
            cur = db.execute(
                """UPDATE tasks SET status='running', lease_id=?, lease_expires_at=?, updated_at=?
                WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                AND status='scheduled' AND next_run_at IS NOT NULL AND next_run_at <= ?""",
                (lease_id, expires, now_text, task_id, *self._scope_args(scope), now_text),
            )
            if cur.rowcount != 1:
                return None
            row = db.execute("SELECT next_run_at FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            scheduled_at = row["next_run_at"] or now_text
            db.execute(
                "INSERT INTO task_runs (run_id, task_id, lease_id, status, scheduled_at, started_at) VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, task_id, lease_id, scheduled_at, now_text),
            )
        return TaskRunRecord(run_id, task_id, "running", scheduled_at, now_text, lease_id=lease_id)

    def due_tasks(self, now_text: str, limit: int = 100) -> list[TaskRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE status='scheduled' AND next_run_at IS NOT NULL AND next_run_at <= ? ORDER BY next_run_at LIMIT ?",
                (now_text, max(1, min(int(limit), 500))),
            ).fetchall()
        return [self._row_to_task(row) for row in rows]

    def set_due_now(self, task_id: str, scope: Scope | AccountTaskScope, when: str) -> bool:
        with self._connect() as db:
            if isinstance(scope, AccountTaskScope):
                cur = db.execute("UPDATE tasks SET next_run_at=?, status='scheduled', updated_at=? WHERE task_id=? AND owner_id=? AND module_id=? AND status='scheduled'", (when, when, task_id, *self._account_args(scope)))
            else:
                cur = db.execute("UPDATE tasks SET next_run_at=?, status='scheduled', updated_at=? WHERE task_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='scheduled'", (when, when, task_id, *self._scope_args(scope)))
        return cur.rowcount == 1

    def finish_run(
        self, task_id: str, run_id: str, scope: Scope, *, status: str, result: str | None = None,
        error: str | None = None, next_run_at: str | None = None,
        lease_id: str | None = None,
    ) -> bool:
        now = _iso(_now())
        with self._connect() as db:
            run = db.execute("SELECT lease_id FROM task_runs WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
            owner_lease = lease_id or (run["lease_id"] if run else None)
            if owner_lease is None:
                return False
            task_status = "scheduled" if next_run_at else status
            cur = db.execute(
                """UPDATE tasks SET status=?, next_run_at=?, last_error=?, lease_id=NULL,
                lease_expires_at=NULL, updated_at=? WHERE task_id=? AND owner_id=? AND channel=?
                AND conversation_id=? AND module_id=? AND status='running' AND lease_id=?""",
                (task_status, next_run_at, error, now, task_id, *self._scope_args(scope), owner_lease),
            )
            if cur.rowcount != 1:
                return False
            db.execute(
                "UPDATE task_runs SET status=?, completed_at=?, result=?, error=? WHERE run_id=? AND task_id=? AND lease_id=? AND status='running'",
                (status, now, result, error, run_id, task_id, owner_lease),
            )
        return True

    def runs(self, task_id: str, scope: Scope | AccountTaskScope, limit: int = 50) -> list[TaskRunRecord]:
        if self.get_task(task_id, scope) is None:
            return []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM task_runs WHERE task_id=? ORDER BY scheduled_at DESC LIMIT ?",
                (task_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [TaskRunRecord(r["run_id"], r["task_id"], r["status"], r["scheduled_at"], r["started_at"], r["completed_at"], r["result"], r["error"], r["lease_id"]) for r in rows]
