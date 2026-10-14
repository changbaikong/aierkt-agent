"""
媒体任务持久化

管理媒体任务（TTS/ASR）的 SQLite 存储，包括任务状态跟踪和 artifact 元信息。
"""
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from agent_core.contracts import Scope


@dataclass
class MediaJobRecord:
    """媒体任务记录"""
    job_id: str
    kind: Literal["tts", "asr"]
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    scope: Scope
    request_json: str  # JSON 序列化的请求参数
    artifact_id: str | None = None
    transcript: str | None = None  # ASR 结果
    error: str | None = None
    queued_at: str = ""  # ISO 8601
    started_at: str | None = None
    completed_at: str | None = None
    claim_id: str | None = None
    claim_expires_at: str | None = None


@dataclass
class ArtifactRecord:
    """Artifact 元信息记录"""
    artifact_id: str
    scope: Scope
    media_type: str  # "audio/wav", "audio/mp3"
    size_bytes: int
    file_path: str  # 相对于 artifact_dir 的路径
    created_at: str  # ISO 8601
    expires_at: str  # ISO 8601


class MediaTaskStore:
    """媒体任务持久化存储"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        # 任务表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                request_json TEXT NOT NULL,
                artifact_id TEXT,
                transcript TEXT,
                error TEXT,
                queued_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                claim_id TEXT,
                claim_expires_at TEXT
            )
        """)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(media_jobs)")}
        if "claim_id" not in columns:
            conn.execute("ALTER TABLE media_jobs ADD COLUMN claim_id TEXT")
        if "claim_expires_at" not in columns:
            conn.execute("ALTER TABLE media_jobs ADD COLUMN claim_expires_at TEXT")

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_jobs_status
            ON media_jobs(status)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_jobs_claim
            ON media_jobs(status, claim_expires_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_jobs_scope
            ON media_jobs(owner_id, channel, conversation_id, module_id)
        """)

        # Artifact 表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_artifacts (
                artifact_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                media_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_artifacts_expires
            ON media_artifacts(expires_at)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_media_artifacts_scope
            ON media_artifacts(owner_id, channel, conversation_id, module_id)
        """)

        conn.commit()
        conn.close()

    def save_job(self, job: MediaJobRecord) -> None:
        """保存或更新任务记录"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO media_jobs (
                    job_id, kind, status, owner_id, channel, conversation_id, module_id,
                    request_json, artifact_id, transcript, error,
                    queued_at, started_at, completed_at, claim_id, claim_expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.kind,
                    job.status,
                    job.scope.owner_id,
                    job.scope.channel,
                    job.scope.conversation_id,
                    job.scope.module_id,
                    job.request_json,
                    job.artifact_id,
                    job.transcript,
                    job.error,
                    job.queued_at,
                    job.started_at,
                    job.completed_at,
                    job.claim_id,
                    job.claim_expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _scope_args(scope: Scope) -> tuple[str, str, str, str]:
        return scope.owner_id, scope.channel, scope.conversation_id, scope.module_id

    def get_job(self, job_id: str, scope: Scope | None = None) -> MediaJobRecord | None:
        """获取任务记录；生产调用必须提供 scope。"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            if scope is None:
                cursor = conn.execute("SELECT * FROM media_jobs WHERE job_id = ?", (job_id,))
            else:
                cursor = conn.execute(
                    """SELECT * FROM media_jobs WHERE job_id = ?
                       AND owner_id = ? AND channel = ? AND conversation_id = ? AND module_id = ?""",
                    (job_id, *self._scope_args(scope)),
                )
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        return self._row_to_job(row)

    def cancel_job(self, job_id: str, scope: Scope) -> MediaJobRecord | None:
        """Cancel a non-terminal job only when it belongs to ``scope``."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(
                """UPDATE media_jobs SET status='cancelled', completed_at=?, claim_id=NULL,
                   claim_expires_at=NULL
                   WHERE job_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                     AND status IN ('queued', 'running')""",
                (now, job_id, *self._scope_args(scope)),
            )
            row = conn.execute(
                """SELECT * FROM media_jobs WHERE job_id=? AND owner_id=? AND channel=?
                   AND conversation_id=? AND module_id=?""",
                (job_id, *self._scope_args(scope)),
            ).fetchone()
            conn.commit()
        finally:
            conn.close()
        return self._row_to_job(row) if row is not None else None

    def claim_queued_job(self, job_id: str, scope: Scope, *, lease_seconds: int = 300) -> MediaJobRecord | None:
        """Atomically claim a queued job for one worker and scope."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires_text = (now + timedelta(seconds=max(1, int(lease_seconds)))).isoformat()
        claim_id = uuid.uuid4().hex
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """UPDATE media_jobs
                   SET status='running', started_at=?, claim_id=?, claim_expires_at=?
                   WHERE job_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                     AND status='queued'""",
                (now_text, claim_id, expires_text, job_id, *self._scope_args(scope)),
            )
            if cur.rowcount != 1:
                conn.rollback()
                return None
            row = conn.execute("SELECT * FROM media_jobs WHERE job_id=?", (job_id,)).fetchone()
            conn.commit()
        finally:
            conn.close()
        return self._row_to_job(row) if row is not None else None

    def recover_expired_claims(self) -> int:
        """Requeue jobs whose worker lease expired."""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            cur = conn.execute(
                """UPDATE media_jobs
                   SET status='queued', started_at=NULL, claim_id=NULL, claim_expires_at=NULL
                   WHERE status='running' AND claim_id IS NOT NULL AND claim_expires_at <= ?""",
                (now,),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    def finish_claimed_job(self, job: MediaJobRecord) -> bool:
        """Persist a terminal result only for the worker holding the claim."""
        if not job.claim_id:
            return False
        now = job.completed_at or datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            cur = conn.execute(
                """UPDATE media_jobs SET status=?, artifact_id=?, transcript=?, error=?,
                   completed_at=?, claim_id=NULL, claim_expires_at=NULL
                   WHERE job_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                     AND status='running' AND claim_id=?""",
                (job.status, job.artifact_id, job.transcript, job.error, now, job.job_id,
                 *self._scope_args(job.scope), job.claim_id),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def _row_to_job(self, row: sqlite3.Row) -> MediaJobRecord:
        return MediaJobRecord(
            job_id=row["job_id"], kind=row["kind"], status=row["status"],
            scope=Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
            request_json=row["request_json"], artifact_id=row["artifact_id"],
            transcript=row["transcript"], error=row["error"], queued_at=row["queued_at"],
            started_at=row["started_at"], completed_at=row["completed_at"],
            claim_id=row["claim_id"], claim_expires_at=row["claim_expires_at"],
        )

    def list_jobs_by_status(self, status: str, limit: int = 100) -> list[MediaJobRecord]:
        """按状态列出任务"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM media_jobs WHERE status = ? ORDER BY queued_at DESC LIMIT ?",
                (status, limit),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        return [self._row_to_job(row) for row in rows]

    def save_artifact(self, artifact: ArtifactRecord) -> None:
        """保存 artifact 记录"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO media_artifacts (
                    artifact_id, owner_id, channel, conversation_id, module_id,
                    media_type, size_bytes, file_path, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.scope.owner_id,
                    artifact.scope.channel,
                    artifact.scope.conversation_id,
                    artifact.scope.module_id,
                    artifact.media_type,
                    artifact.size_bytes,
                    artifact.file_path,
                    artifact.created_at,
                    artifact.expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_artifact(self, artifact_id: str, scope: Scope) -> ArtifactRecord | None:
        """获取 artifact 记录（需要 scope 匹配）"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT * FROM media_artifacts
                WHERE artifact_id = ?
                AND owner_id = ? AND channel = ?
                AND conversation_id = ? AND module_id = ?
                """,
                (
                    artifact_id,
                    scope.owner_id,
                    scope.channel,
                    scope.conversation_id,
                    scope.module_id,
                ),
            )
            row = cursor.fetchone()
        finally:
            conn.close()

        if not row:
            return None

        return ArtifactRecord(
            artifact_id=row["artifact_id"],
            scope=Scope(
                owner_id=row["owner_id"],
                channel=row["channel"],
                conversation_id=row["conversation_id"],
                module_id=row["module_id"],
            ),
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            file_path=row["file_path"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def list_expired_artifacts(self) -> list[ArtifactRecord]:
        """列出所有已过期的 artifact"""
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM media_artifacts WHERE expires_at <= ?",
                (now,),
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        return [
            ArtifactRecord(
                artifact_id=row["artifact_id"],
                scope=Scope(
                    owner_id=row["owner_id"],
                    channel=row["channel"],
                    conversation_id=row["conversation_id"],
                    module_id=row["module_id"],
                ),
                media_type=row["media_type"],
                size_bytes=row["size_bytes"],
                file_path=row["file_path"],
                created_at=row["created_at"],
                expires_at=row["expires_at"],
            )
            for row in rows
        ]

    def delete_artifact(self, artifact_id: str) -> None:
        """删除 artifact 记录"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("DELETE FROM media_artifacts WHERE artifact_id = ?", (artifact_id,))
            conn.commit()
        finally:
            conn.close()

    def delete_old_jobs(self, before: str) -> int:
        """删除指定时间之前的已完成/失败/取消任务"""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            cursor = conn.execute(
                """
                DELETE FROM media_jobs
                WHERE status IN ('completed', 'failed', 'cancelled')
                AND completed_at < ?
                """,
                (before,),
            )
            deleted_count = cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return deleted_count
