"""Durable, idempotent delivery outbox independent from channel SDKs."""

from __future__ import annotations

import inspect
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    account_id: str
    delivery_target: str
    event_type: str
    payload: dict[str, Any]
    status: str
    attempts: int
    next_attempt_at: str
    dedupe_key: str
    last_error: str | None
    created_at: str
    updated_at: str


class OutboxStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS outbox_messages (
                    message_id TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL,
                    delivery_target TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox_messages(status, next_attempt_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), timeout=15.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    @staticmethod
    def _row(row: sqlite3.Row | None) -> OutboxMessage | None:
        if row is None:
            return None
        return OutboxMessage(
            row["message_id"], row["account_id"], row["delivery_target"], row["event_type"],
            json.loads(row["payload_json"]), row["status"], int(row["attempts"]),
            row["next_attempt_at"], row["dedupe_key"], row["last_error"], row["created_at"], row["updated_at"],
        )

    def enqueue(self, *, account_id: str, delivery_target: str, event_type: str, payload: dict[str, Any], dedupe_key: str) -> OutboxMessage:
        now = _iso(_now())
        message_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO outbox_messages (message_id, account_id, delivery_target, event_type, payload_json, status, attempts, next_attempt_at, dedupe_key, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, NULL, ?, ?)",
                (message_id, account_id, delivery_target, event_type, json.dumps(payload, ensure_ascii=False), now, dedupe_key, now, now),
            )
            row = db.execute("SELECT * FROM outbox_messages WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        result = self._row(row)
        if result is None:
            raise RuntimeError("failed to enqueue outbox message")
        return result

    def get(self, message_id: str) -> OutboxMessage | None:
        with self._connect() as db:
            return self._row(db.execute("SELECT * FROM outbox_messages WHERE message_id=?", (message_id,)).fetchone())

    def pending(self, limit: int = 100) -> list[OutboxMessage]:
        now = _iso(_now())
        with self._connect() as db:
            rows = db.execute("SELECT * FROM outbox_messages WHERE status='pending' AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?", (now, max(1, min(int(limit), 500)))).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def recover_stale_sending(self, *, stale_after_seconds: int = 300) -> int:
        """Return messages abandoned by a crashed sender to the retry queue."""
        now_dt = _now()
        now = _iso(now_dt)
        cutoff = _iso(now_dt - timedelta(seconds=max(1, int(stale_after_seconds))))
        with self._connect() as db:
            cur = db.execute(
                "UPDATE outbox_messages SET status='pending', next_attempt_at=?, last_error=COALESCE(last_error, 'sender lease expired'), updated_at=? WHERE status='sending' AND updated_at<=?",
                (now, now, cutoff),
            )
        return cur.rowcount

    def mark_sent(self, message_id: str) -> bool:
        now = _iso(_now())
        with self._connect() as db:
            cur = db.execute("UPDATE outbox_messages SET status='sent', updated_at=? WHERE message_id=? AND status='sending'", (now, message_id))
        return cur.rowcount == 1

    def mark_sending(self, message_id: str) -> bool:
        now = _iso(_now())
        with self._connect() as db:
            cur = db.execute("UPDATE outbox_messages SET status='sending', attempts=attempts+1, updated_at=? WHERE message_id=? AND status='pending' AND next_attempt_at<=?", (now, message_id, now))
        return cur.rowcount == 1

    def mark_failed(self, message_id: str, error: str, retry_base_seconds: int = 30) -> bool:
        now_dt = _now()
        now = _iso(now_dt)
        with self._connect() as db:
            row = db.execute("SELECT attempts FROM outbox_messages WHERE message_id=? AND status='sending'", (message_id,)).fetchone()
            if row is None:
                return False
            delay = retry_base_seconds * (2 ** max(0, int(row["attempts"]) - 1))
            cur = db.execute("UPDATE outbox_messages SET status='pending', next_attempt_at=?, last_error=?, updated_at=? WHERE message_id=? AND status='sending'", (_iso(now_dt + timedelta(seconds=delay)), str(error)[:500], now, message_id))
        return cur.rowcount == 1


SendHandler = Callable[[str, str, dict[str, Any]], Any | Awaitable[Any]]


class OutboxWorker:
    def __init__(self, store: OutboxStore, send: SendHandler, *, retry_base_seconds: int = 30, sending_lease_seconds: int = 300) -> None:
        self.store = store
        self.send = send
        self.retry_base_seconds = max(0, int(retry_base_seconds))
        self.sending_lease_seconds = max(1, int(sending_lease_seconds))

    async def process_once(self) -> int:
        self.store.recover_stale_sending(stale_after_seconds=self.sending_lease_seconds)
        processed = 0
        for message in self.store.pending():
            if not self.store.mark_sending(message.message_id):
                continue
            processed += 1
            try:
                result = self.send(message.account_id, message.delivery_target, {"event_type": message.event_type, **message.payload})
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self.store.mark_failed(message.message_id, f"{type(exc).__name__}: {exc}", self.retry_base_seconds)
            else:
                self.store.mark_sent(message.message_id)
        return processed
