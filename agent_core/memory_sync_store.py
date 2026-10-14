"""本地同步侧的三张表：待上传队列、访问记录、同步水位。

【为什么和 memory_records 放同一个库】
合并云端变更必须和本地记录写在**同一个事务**里：先写记录、再推进水位，
中途崩了要一起回滚。分库就没法保证这一点，只能靠对账去补，那更复杂。

【为什么不复用 memory_records 承载待上传内容】
待上传的是**一轮原始对话**（用户说了什么、助手回了什么），不是一条记忆。
以前把记忆行反推成对话再送去提炼，等于把已经提炼过的东西重炼一遍。
两者是不同生命周期的东西，混在一张表里迟早互相污染。

表名只在本模块出现（项目约定）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .contracts import Scope

UPLOAD_PENDING = "pending"
UPLOAD_SENT = "sent"

STATE_CHANGE_CURSOR = "change_cursor"
STATE_LAST_SYNC_AT = "last_sync_at"
STATE_BASELINE_AT = "baseline_at"
STATE_ACCESS_WATERMARK = "access_watermark"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _scope_tuple(scope: Scope) -> tuple[str, str, str, str]:
    return scope.owner_id, scope.channel, scope.conversation_id, scope.module_id


class MemorySyncStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_upload_queue (
                  turn_id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                  conversation_id TEXT NOT NULL, module_id TEXT NOT NULL,
                  user_text TEXT NOT NULL, assistant_reply TEXT NOT NULL DEFAULT '',
                  occurred_at TEXT NOT NULL,
                  packet TEXT,
                  schema_version INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  last_error TEXT, sent_at TEXT, created_at TEXT NOT NULL);

                CREATE INDEX IF NOT EXISTS memory_upload_queue_status_idx
                  ON memory_upload_queue(status, created_at);

                CREATE TABLE IF NOT EXISTS memory_access_outbox (
                  record_id TEXT NOT NULL, minute_bucket TEXT NOT NULL,
                  owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                  conversation_id TEXT NOT NULL, module_id TEXT NOT NULL,
                  accessed_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                  PRIMARY KEY (record_id, minute_bucket));

                CREATE TABLE IF NOT EXISTS memory_sync_state (
                  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

                CREATE TABLE IF NOT EXISTS memory_applied_changes (
                  seq INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                """
            )
            # 建表语句是后加的列：早期版本的表里没有 packet / schema_version。
            # 不补的话，升级后第一轮上传就会因为「没有这个列」整条失败。
            columns = {row[1] for row in db.execute("PRAGMA table_info(memory_upload_queue)")}
            for name, definition in {
                "packet": "TEXT",
                "schema_version": "INTEGER NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE memory_upload_queue ADD COLUMN {name} {definition}")

    # ---- 待上传队列 -------------------------------------------------

    def enqueue_turn(
        self,
        scope: Scope,
        *,
        turn_id: str,
        user_text: str,
        assistant_reply: str = "",
        occurred_at: str = "",
        packet: dict | None = None,
        schema_version: int = 0,
    ) -> bool:
        """把一轮对话放进待上传队列。同一个 turn_id 重复入队只保留第一条。

        返回 True 表示这次真的写进去了（新的一轮），False 表示早就有了。
        """
        if not turn_id:
            raise ValueError("turn_id 不能为空")
        moment = occurred_at or _now()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO memory_upload_queue"
                "(turn_id,owner_id,channel,conversation_id,module_id,user_text,assistant_reply,"
                "occurred_at,packet,schema_version,status,attempts,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',0,?)",
                (
                    turn_id, *_scope_tuple(scope), user_text, assistant_reply, moment,
                    # 处理包按原样存 JSON：它是两端对齐的**唯一**依据，
                    # 拆成列存反而会在加字段时两边不同步。
                    json.dumps(packet, ensure_ascii=False) if packet else None,
                    int(schema_version or 0),
                    _now(),
                ),
            )
            return cursor.rowcount > 0

    def pending_turns(self, *, limit: int = 50) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM memory_upload_queue WHERE status='pending' "
                "ORDER BY occurred_at, created_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_turn_sent(self, turn_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE memory_upload_queue SET status='sent', sent_at=?, last_error=NULL "
                "WHERE turn_id=?",
                (_now(), turn_id),
            )

    def mark_turn_failed(self, turn_id: str, error: str) -> None:
        """上传失败只记错误、留在 pending —— 下次同步会重试同一条，不跳数据。"""
        with self.connect() as db:
            db.execute(
                "UPDATE memory_upload_queue SET attempts=attempts+1, last_error=? WHERE turn_id=?",
                (str(error)[:500], turn_id),
            )

    def upload_backlog(self) -> int:
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM memory_upload_queue WHERE status='pending'"
            ).fetchone()
        return int(row["n"])

    # ---- 访问记录 ---------------------------------------------------

    def record_access(self, scope: Scope, record_ids: list[str], *, accessed_at: str = "") -> int:
        """记一次「这条记忆被用到了」。同一分钟内同一条只记一次。

        去重放在写入端而不是读取端：一条记忆在组装提示词时可能被引用好几次，
        每次都往云端发是纯噪声 —— 云端只关心"最近有没有被用过"。
        """
        moment = accessed_at or _now()
        bucket = moment[:16]  # 精确到分钟
        rows = [(rid, bucket, *_scope_tuple(scope), moment) for rid in dict.fromkeys(record_ids)]
        if not rows:
            return 0
        with self.connect() as db:
            cursor = db.executemany(
                "INSERT OR IGNORE INTO memory_access_outbox"
                "(record_id,minute_bucket,owner_id,channel,conversation_id,module_id,accessed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                rows,
            )
            return cursor.rowcount

    def pending_access(self, *, limit: int = 500) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM memory_access_outbox WHERE status='pending' "
                "ORDER BY accessed_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_access_sent(self, keys: list[tuple[str, str]]) -> None:
        if not keys:
            return
        with self.connect() as db:
            db.executemany(
                "UPDATE memory_access_outbox SET status='sent' "
                "WHERE record_id=? AND minute_bucket=?",
                keys,
            )

    # ---- 水位 -------------------------------------------------------

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as db:
            row = db.execute("SELECT value FROM memory_sync_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO memory_sync_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), _now()),
            )

    def change_cursor(self) -> int:
        return int(self.get_state(STATE_CHANGE_CURSOR, "0") or 0)

    def applied_seqs(self) -> set[int]:
        with self.connect() as db:
            rows = db.execute("SELECT seq FROM memory_applied_changes").fetchall()
        return {int(row["seq"]) for row in rows}

    def mark_applied(self, db: sqlite3.Connection, seq: int) -> None:
        """在调用方的事务里登记「这条变更应用过了」。

        刻意接收外部连接：它必须和记录写入在同一个事务里提交，
        自己开连接就又变成两步写了。
        """
        db.execute(
            "INSERT OR IGNORE INTO memory_applied_changes(seq,applied_at) VALUES(?,?)",
            (int(seq), _now()),
        )

    def status(self) -> dict:
        with self.connect() as db:
            upload = db.execute(
                "SELECT COUNT(*) AS n FROM memory_upload_queue WHERE status='pending'"
            ).fetchone()["n"]
            access = db.execute(
                "SELECT COUNT(*) AS n FROM memory_access_outbox WHERE status='pending'"
            ).fetchone()["n"]
            applied = db.execute("SELECT COUNT(*) AS n FROM memory_applied_changes").fetchone()["n"]
        return {
            "upload_pending": int(upload),
            "access_pending": int(access),
            "applied_changes": int(applied),
            "change_cursor": self.change_cursor(),
            "last_sync_at": self.get_state(STATE_LAST_SYNC_AT, ""),
        }
