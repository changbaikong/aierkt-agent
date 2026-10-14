"""Short-lived, scope-bound image attachments for web chat."""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .contracts import Scope


ALLOWED_MEDIA_TYPES = {
    "image/png": b"png",
    "image/jpeg": b"jpeg",
    "image/webp": b"webp",
    "image/gif": b"gif",
}
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_TTL_SECONDS = 15 * 60
CLAIM_LEASE_SECONDS = 2 * 60


class InvalidImageError(ValueError):
    """The upload is not a supported image."""


class AttachmentUnavailable(ValueError):
    """The attachment is expired, missing, or already claimed."""


@dataclass(frozen=True)
class ChatAttachment:
    attachment_id: str
    scope: Scope
    original_name: str
    media_type: str
    size_bytes: int
    path: Path
    created_at: float
    expires_at: float
    status: str = "ready"


def _signature_matches(media_type: str, content: bytes) -> bool:
    if media_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return content.startswith((b"GIF87a", b"GIF89a"))
    if media_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


class ChatAttachmentStore:
    """Persist metadata in SQLite and bytes in a private temporary directory."""

    def __init__(
        self,
        database_path: str | Path,
        storage_dir: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.database_path = Path(database_path)
        self.storage_dir = Path(storage_dir)
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = int(ttl_seconds)
        if self.max_bytes < 1 or self.ttl_seconds < 0:
            raise ValueError("max_bytes must be positive and ttl_seconds must not be negative")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_chat_attachments (
                    attachment_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ready',
                    claimed_until REAL
                )"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_chat_attachments_expiry
                ON agent_chat_attachments(expires_at)"""
            )

    @staticmethod
    def _scope_values(scope: Scope) -> tuple[str, str, str, str]:
        return scope.owner_id, scope.channel, scope.conversation_id, scope.module_id

    @staticmethod
    def _record(row: sqlite3.Row) -> ChatAttachment:
        return ChatAttachment(
            attachment_id=row["attachment_id"],
            scope=Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
            original_name=row["original_name"],
            media_type=row["media_type"],
            size_bytes=int(row["size_bytes"]),
            path=Path(row["file_path"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            status=row["status"],
        )

    def upload(self, scope: Scope, filename: str, media_type: str, content: bytes) -> ChatAttachment:
        media_type = str(media_type or "").lower().strip()
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise InvalidImageError("unsupported image media type")
        if not content:
            raise InvalidImageError("image file is empty")
        if len(content) > self.max_bytes:
            raise InvalidImageError(f"image file exceeds {self.max_bytes} bytes")
        if not _signature_matches(media_type, content):
            raise InvalidImageError("image file signature does not match media type")

        now = time.time()
        attachment_id = f"att-{uuid.uuid4().hex}"
        safe_name = Path(str(filename or "image")).name[:255] or "image"
        target = self.storage_dir / f"{attachment_id}.{ALLOWED_MEDIA_TYPES[media_type].decode()}"
        target.write_bytes(content)
        try:
            with self._connect() as db:
                db.execute(
                    """INSERT INTO agent_chat_attachments
                    (attachment_id, owner_id, channel, conversation_id, module_id,
                     original_name, media_type, size_bytes, file_path, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        attachment_id,
                        *self._scope_values(scope),
                        safe_name,
                        media_type,
                        len(content),
                        str(target),
                        now,
                        now + self.ttl_seconds,
                    ),
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return ChatAttachment(attachment_id, scope, safe_name, media_type, len(content), target, now, now + self.ttl_seconds)

    def get_for_scope(self, scope: Scope, attachment_id: str) -> ChatAttachment | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM agent_chat_attachments
                WHERE attachment_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (attachment_id, *self._scope_values(scope)),
            ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            return None
        if row["status"] == "claimed" and float(row["claimed_until"] or 0) <= time.time():
            self.release(scope, attachment_id)
            row = dict(row)
            row["status"] = "ready"
        return self._record(row)

    def claim(self, scope: Scope, attachment_id: str) -> ChatAttachment:
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM agent_chat_attachments
                WHERE attachment_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (attachment_id, *self._scope_values(scope)),
            ).fetchone()
            if row is None or float(row["expires_at"]) <= now:
                raise AttachmentUnavailable("attachment not found or expired")
            if row["status"] == "claimed" and float(row["claimed_until"] or 0) > now:
                raise AttachmentUnavailable("attachment is already in use")
            claimed_until = now + CLAIM_LEASE_SECONDS
            db.execute(
                "UPDATE agent_chat_attachments SET status='claimed', claimed_until=? WHERE attachment_id=?",
                (claimed_until, attachment_id),
            )
            row = db.execute("SELECT * FROM agent_chat_attachments WHERE attachment_id=?", (attachment_id,)).fetchone()
        return self._record(row)

    def release(self, scope: Scope, attachment_id: str) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE agent_chat_attachments SET status='ready', claimed_until=NULL
                WHERE attachment_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (attachment_id, *self._scope_values(scope)),
            )

    def delete(self, scope: Scope, attachment_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                """SELECT file_path FROM agent_chat_attachments
                WHERE attachment_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (attachment_id, *self._scope_values(scope)),
            ).fetchone()
            if row is None:
                return False
            db.execute("DELETE FROM agent_chat_attachments WHERE attachment_id=?", (attachment_id,))
        Path(row["file_path"]).unlink(missing_ok=True)
        return True

    def cleanup_expired(self) -> int:
        now = time.time()
        with self._connect() as db:
            rows = db.execute("SELECT attachment_id, file_path FROM agent_chat_attachments WHERE expires_at<=?", (now,)).fetchall()
            db.execute("DELETE FROM agent_chat_attachments WHERE expires_at<=?", (now,))
            db.execute("UPDATE agent_chat_attachments SET status='ready', claimed_until=NULL WHERE status='claimed' AND claimed_until<=?", (now,))
        for row in rows:
            Path(row["file_path"]).unlink(missing_ok=True)
        return len(rows)
