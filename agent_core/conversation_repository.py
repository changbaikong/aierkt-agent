"""Durable public conversation history backed by the agent core SQLite database."""

from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .connection_pool import get_pool
from .contracts import Scope
from .message_time import strip_message_time_prefix
from .opening_recall import delete_orphan_events, delete_scope_events


DEFAULT_HISTORY_MAX_MESSAGES = 1000
DEFAULT_AUTO_ARCHIVE_DAYS = 30
DEFAULT_ARCHIVE_RETENTION_DAYS = 90


@dataclass(frozen=True)
class ConversationRecord:
    scope: Scope
    title: str
    status: str
    created_at: str
    updated_at: str
    last_activity_at: str
    archived_at: str | None
    runtime_state: str
    runtime_updated_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "conversation_id": self.scope.conversation_id,
            "owner_id": self.scope.owner_id,
            "channel": self.scope.channel,
            "module_id": self.scope.module_id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_activity_at": self.last_activity_at,
            "archived_at": self.archived_at,
            "runtime_state": self.runtime_state,
                "runtime_updated_at": self.runtime_updated_at,
            }


#: 推进展示游标的结果状态。
ADVANCE_ADVANCED = "advanced"      # 游标前进了一条
ADVANCE_UNCHANGED = "unchanged"    # 幂等：已确认过、或不是本 Scope 的助手消息
ADVANCE_BLOCKED = "blocked"        # 试图跳过当前那条未读消息


@dataclass(frozen=True)
class PresentationAdvance:
    """一次展示游标推进请求的结果。

    ``blocked`` 是这轮收紧的核心：只允许确认**队首**那条未读助手消息。
    在此之前，任何一条 id 更大的助手消息都能把游标推过去 —— 用户点一下就把
    中间几句一起"读"掉了，播放队列等于没用。``expected_message_id`` 告诉
    调用方"该确认的是哪一条"，便于前端在 409 后自动对齐，而不是让用户自己猜。
    """

    status: str
    acknowledged_message_id: int
    expected_message_id: int | None = None

    @property
    def changed(self) -> bool:
        return self.status == ADVANCE_ADVANCED


class ConversationRepository:
    """Persist public messages using the complete ``Scope`` as the identity."""

    def __init__(
        self,
        path: Path | str,
        *,
        history_max_messages: int = DEFAULT_HISTORY_MAX_MESSAGES,
        auto_archive_days: int = DEFAULT_AUTO_ARCHIVE_DAYS,
        archive_retention_days: int = DEFAULT_ARCHIVE_RETENTION_DAYS,
    ) -> None:
        if history_max_messages < 1:
            raise ValueError("history_max_messages must be at least 1")
        self.path = Path(path)
        self.history_max_messages = history_max_messages
        self.auto_archive_days = auto_archive_days
        self.archive_retention_days = archive_retention_days
        self._initialize()

    @property
    def _pool(self):
        return get_pool(str(self.path))

    def _initialize(self) -> None:
        with self._pool.get_connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_conversations (
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_activity_at TEXT NOT NULL,
                    archived_at TEXT,
                    runtime_state TEXT NOT NULL DEFAULT 'idle',
                    runtime_updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, channel, conversation_id, module_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_conversation_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    stable_key TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (owner_id, channel, conversation_id, module_id)
                        REFERENCES agent_core_conversations(owner_id, channel, conversation_id, module_id)
                        ON DELETE CASCADE
                )"""
            )
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_core_conversation_message_key
                ON agent_core_conversation_messages(owner_id, channel, conversation_id, module_id, stable_key)
                WHERE stable_key IS NOT NULL"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_core_conversation_activity
                ON agent_core_conversations(owner_id, channel, module_id, status, last_activity_at DESC)"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_agent_core_conversation_messages_scope
                ON agent_core_conversation_messages(owner_id, channel, conversation_id, module_id, message_id)"""
            )
            # 舞台播放游标。**和消息账本分开存**：消息是事实（写过就不该改），
            # 游标是"用户看到哪了"（前端播放进度的投影），两者生命周期不同，
            # 混在一张表里会出现"为了记进度而改历史"的荒谬写操作。
            #
            # 外键指向**完整会话 Scope**（四个键一起）并级联删除：会话没了，
            # 游标就没有承载对象。靠"谁删会话谁记得顺手删游标"是迟早会漏的约定。
            self._create_presentation_table(db)
            # 老库的这张表最初没有外键，而 CREATE TABLE IF NOT EXISTS 不会给
            # 已存在的表补约束 —— 只能重建一次。
            self._rebuild_presentation_table_if_needed(db)
            # 一次性升级迁移：老库第一次建这张表时，把每个会话的游标直接推到
            # 当前最后一条消息 —— 否则上线后所有历史对话会在舞台上一句句重播。
            # NOT EXISTS 保证幂等：这张表建好之后，同一批会话不会再次被写入。
            db.execute(
                """INSERT INTO agent_core_conversation_presentation
                (owner_id, channel, conversation_id, module_id, acknowledged_message_id, updated_at)
                SELECT c.owner_id, c.channel, c.conversation_id, c.module_id,
                       COALESCE((
                           SELECT MAX(m.message_id) FROM agent_core_conversation_messages AS m
                           WHERE m.owner_id=c.owner_id AND m.channel=c.channel
                             AND m.conversation_id=c.conversation_id AND m.module_id=c.module_id
                       ), 0),
                       ?
                FROM agent_core_conversations AS c
                WHERE NOT EXISTS (
                    SELECT 1 FROM agent_core_conversation_presentation AS p
                    WHERE p.owner_id=c.owner_id AND p.channel=c.channel
                      AND p.conversation_id=c.conversation_id AND p.module_id=c.module_id
                )""",
                (self._timestamp(),),
            )

    @staticmethod
    def _create_presentation_table(db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS agent_core_conversation_presentation (
                owner_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                module_id TEXT NOT NULL,
                acknowledged_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, channel, conversation_id, module_id),
                FOREIGN KEY (owner_id, channel, conversation_id, module_id)
                    REFERENCES agent_core_conversations(owner_id, channel, conversation_id, module_id)
                    ON DELETE CASCADE
            )"""
        )

    def _rebuild_presentation_table_if_needed(self, db: sqlite3.Connection) -> None:
        """给老库的展示表补上外键约束。

        SQLite 不支持 ``ALTER TABLE ADD CONSTRAINT``，只能"建新表 → 搬数据 → 换名"。
        搬数据时用 EXISTS 滤掉孤儿行：老表里可能留着已删会话的游标，直接搬会被
        新外键拒绝 —— 而且不是跳过那几行，是整条 ``INSERT ... SELECT`` 失败，
        等于一张游标都迁不过去。
        """
        if db.execute("PRAGMA foreign_key_list(agent_core_conversation_presentation)").fetchall():
            return
        legacy = "_agent_core_conversation_presentation_legacy"
        db.execute(f"DROP TABLE IF EXISTS {legacy}")
        db.execute(f"ALTER TABLE agent_core_conversation_presentation RENAME TO {legacy}")
        self._create_presentation_table(db)
        db.execute(
            f"""INSERT OR IGNORE INTO agent_core_conversation_presentation
            (owner_id, channel, conversation_id, module_id, acknowledged_message_id, updated_at)
            SELECT p.owner_id, p.channel, p.conversation_id, p.module_id,
                   p.acknowledged_message_id, p.updated_at
            FROM {legacy} AS p
            WHERE EXISTS (
                SELECT 1 FROM agent_core_conversations AS c
                WHERE c.owner_id=p.owner_id AND c.channel=p.channel
                  AND c.conversation_id=p.conversation_id AND c.module_id=p.module_id
            )"""
        )
        db.execute(f"DROP TABLE {legacy}")

    @staticmethod
    def _timestamp(now: datetime | None = None) -> str:
        value = now or datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @staticmethod
    def _scope_values(scope: Scope) -> tuple[str, str, str, str]:
        return (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id)

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> ConversationRecord | None:
        if row is None:
            return None
        return ConversationRecord(
            Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
            row["title"],
            row["status"],
            row["created_at"],
            row["updated_at"],
            row["last_activity_at"],
            row["archived_at"],
            row["runtime_state"],
            row["runtime_updated_at"],
        )

    @staticmethod
    def _title_for(content: str) -> str:
        return " ".join(content.split())[:40]

    def ensure(self, scope: Scope, *, now: datetime | None = None) -> ConversationRecord:
        timestamp = self._timestamp(now)
        with self._pool.get_connection() as db:
            db.execute(
                """INSERT INTO agent_core_conversations
                (owner_id, channel, conversation_id, module_id, created_at, updated_at,
                 last_activity_at, runtime_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, channel, conversation_id, module_id) DO NOTHING""",
                (*self._scope_values(scope), timestamp, timestamp, timestamp, timestamp),
            )
            row = db.execute(
                "SELECT * FROM agent_core_conversations WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                self._scope_values(scope),
            ).fetchone()
        record = self._row_to_record(row)
        assert record is not None
        return record

    def create(self, scope: Scope, *, title: str = "", now: datetime | None = None) -> ConversationRecord:
        timestamp = self._timestamp(now)
        with self._pool.get_connection() as db:
            db.execute(
                """INSERT INTO agent_core_conversations
                (owner_id, channel, conversation_id, module_id, title, created_at, updated_at,
                 last_activity_at, runtime_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (*self._scope_values(scope), self._title_for(title), timestamp, timestamp, timestamp, timestamp),
            )
        return self.ensure(scope)

    def get(self, scope: Scope) -> ConversationRecord | None:
        with self._pool.get_connection() as db:
            row = db.execute(
                "SELECT * FROM agent_core_conversations WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                self._scope_values(scope),
            ).fetchone()
        return self._row_to_record(row)

    def list(
        self,
        *,
        owner_id: str,
        channel: str,
        module_id: str,
        status: str = "active",
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[ConversationRecord], str | None]:
        limit = max(1, min(int(limit), 100))
        params: list[Any] = [owner_id, channel, module_id, status]
        cursor_clause = ""
        if cursor:
            try:
                last_activity_at, conversation_id = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            except (ValueError, TypeError, UnicodeDecodeError):
                raise ValueError("invalid conversation cursor") from None
            cursor_clause = " AND (last_activity_at < ? OR (last_activity_at = ? AND conversation_id < ?))"
            params.extend([last_activity_at, last_activity_at, conversation_id])
        params.append(limit + 1)
        with self._pool.get_connection() as db:
            rows = db.execute(
                "SELECT * FROM agent_core_conversations WHERE owner_id=? AND channel=? AND module_id=? AND status=?"
                + cursor_clause
                + " ORDER BY last_activity_at DESC, conversation_id DESC LIMIT ?",
                params,
            ).fetchall()
        records = [record for row in rows[:limit] if (record := self._row_to_record(row)) is not None]
        next_cursor = None
        if len(rows) > limit and records:
            last = records[-1]
            raw = json.dumps([last.last_activity_at, last.scope.conversation_id], ensure_ascii=False).encode()
            next_cursor = base64.urlsafe_b64encode(raw).decode()
        return records, next_cursor

    def history(self, scope: Scope) -> list[dict[str, Any]]:
        """按时间顺序返回公开消息。

        ``message_id`` 是**稳定身份**：前端拿它当播放游标（"这一条播过了"），
        所以它必须出现在每一条里，且不能来自 metadata —— 老数据的 metadata
        里可能残留同名键，显式赋值放在展开之后才不会被覆盖。
        """
        with self._pool.get_connection() as db:
            rows = db.execute(
                """SELECT message_id, role, content, metadata_json, created_at FROM agent_core_conversation_messages
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                ORDER BY message_id""",
                self._scope_values(scope),
            ).fetchall()
        messages: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            messages.append({
                "role": row["role"],
                "content": row["content"],
                **metadata,
                "message_id": int(row["message_id"]),
                "created_at": row["created_at"],
            })
        return messages

    def recent_user_messages(
        self,
        scope: Scope,
        *,
        limit: int = 12,
        within_days: int = 14,
        now: datetime | None = None,
        across_conversations: bool = False,
    ) -> list[dict[str, Any]]:
        """最近的用户原话（时间正序），带 ``message_id`` 与 ``created_at``。

        **刻意不复用 :meth:`history`**：那个方法为了还原完整上下文，会把整个会话
        的所有消息（含助手回复、管家进度、任务回执）都读出来。而开场白只需要
        "用户最近说过什么"，全量读进来再在 Python 里筛 —— 在一个几千条的会话里，
        等于每次进页面都把几千行拉一遍，只为了挑出三句。

        过滤分两层：
        - SQL 层：role='user' + 时间窗口 + 限量（这就是"限量读取"的全部含义）；
        - Python 层：带 ``kind`` 的系统旁白与低信息短句（"好的"/"嗯"）由
          :mod:`agent_core.opening_greeting` 在处理线索时剔除 —— 那部分规则与
          提示词的措辞要求绑在一起，放在一处更好维护。

        ``across_conversations=True`` 时**不加 conversation_id 条件**，按
        ``owner_id + channel + module_id`` 取这个账号最近的用户原话。
        开场白要这个模式：重开 Web 版是一个**新会话**（会话 id 落在
        sessionStorage），而新会话里一条消息都没有 —— 只按当前会话读，
        开场白就永远是"哦，来了啊"，她会记得你这个人（账号级已确认事实本来
        就是跨会话的），却不记得上次聊过什么。

        这与联想台账的口径也是一致的（台账同样按 owner + channel + module
        跨会话查）。消息 id 是同一张表里的全局自增值，所以"按 message_id 倒序
        取 N 条"跨会话之后仍然就是"最近说过的 N 句"，不需要另加排序键。
        """
        current = now or datetime.now(timezone.utc)
        params: list[Any]
        if across_conversations:
            params = [scope.owner_id, scope.channel, scope.module_id]
            query = (
                "SELECT message_id, content, metadata_json, created_at "
                "FROM agent_core_conversation_messages "
                "WHERE owner_id=? AND channel=? AND module_id=? AND role='user'"
            )
        else:
            params = [scope.owner_id, scope.channel, scope.conversation_id, scope.module_id]
            query = (
                "SELECT message_id, content, metadata_json, created_at "
                "FROM agent_core_conversation_messages "
                "WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND role='user'"
            )
        if within_days is not None:
            query += " AND created_at>=?"
            params.append(self._timestamp(current - timedelta(days=max(1, int(within_days)))))
        query += " ORDER BY message_id DESC LIMIT ?"
        params.append(max(1, int(limit)))

        with self._pool.get_connection() as db:
            rows = db.execute(query, params).fetchall()
        messages: list[dict[str, Any]] = []
        for row in reversed(rows):
            metadata = json.loads(row["metadata_json"])
            # 与 history() 同一条规矩：metadata 先展开、显式字段后赋值 ——
            # 老数据的 metadata 里可能残留同名字段，顺序反了就会被它盖掉。
            messages.append({
                **metadata,
                "role": "user",
                "content": row["content"],
                "message_id": int(row["message_id"]),
                "created_at": row["created_at"],
            })
        return messages

    def today_user_events(
        self,
        scope: Scope,
        *,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return recent user-authored messages for this account's local day."""
        current = now or datetime.now(timezone.utc)
        local = current.astimezone(timezone(timedelta(hours=8)))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        end = start + timedelta(days=1)
        with self._pool.get_connection() as db:
            rows = db.execute(
                """SELECT conversation_id, content, created_at FROM agent_core_conversation_messages
                WHERE owner_id=? AND channel=? AND module_id=? AND role='user'
                  AND conversation_id<>? AND created_at>=? AND created_at<?
                ORDER BY message_id DESC LIMIT ?""",
                (scope.owner_id, scope.channel, scope.module_id, scope.conversation_id,
                 self._timestamp(start), self._timestamp(end), max(1, min(int(limit), 20))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def append_message(
        self,
        scope: Scope,
        message: dict[str, Any],
        *,
        stable_key: str | None = None,
        now: datetime | None = None,
        touch_activity: bool = True,
    ) -> bool:
        """追加一条公开消息，返回"是否真的写进去了"（``stable_key`` 撞车则 False）。

        只关心写没写进去的调用方（回执桥、管家 sink）继续用它；
        需要拿到新消息 ``message_id``（播放游标、前端定位）的调用方用
        :meth:`insert_message` —— 布尔和身份是两件事，混在一个返回值里
        会让老调用方的 ``is True`` 断言悄悄失效。
        """
        return self.insert_message(
            scope, message, stable_key=stable_key, now=now, touch_activity=touch_activity
        ) is not None

    def insert_message(
        self,
        scope: Scope,
        message: dict[str, Any],
        *,
        stable_key: str | None = None,
        now: datetime | None = None,
        touch_activity: bool = True,
    ) -> int | None:
        """追加一条公开消息，返回新分配的 ``message_id``；已存在则返回 None。

        ``touch_activity=False`` 用于**系统回执**类消息（如 work.result 桥写回的
        后台任务播报）：消息照写、对话里能看到，但不刷新 ``last_activity_at``
        —— 后台任务完成不该把一个没人说话的对话顶到会话列表最前面。

        ``message_id`` 刻意不进 metadata：它是账本分配的身份，不是消息内容；
        写进 metadata 会让 history 拼装时冒出两个同名键，谁赢取决于字典展开
        顺序 —— 那种"看代码顺序才知道对不对"的隐患不值得留。
        """
        role = str(message.get("role", ""))
        content = str(strip_message_time_prefix(message.get("content", "")))
        if not role:
            raise ValueError("message role is required")
        timestamp = self._timestamp(now)
        message["content"] = content
        message["created_at"] = timestamp
        metadata = {
            key: value for key, value in message.items()
            if key not in {"role", "content", "message_id"} and value is not None
        }
        with self._pool.get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO agent_core_conversations
                (owner_id, channel, conversation_id, module_id, created_at, updated_at,
                 last_activity_at, runtime_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, channel, conversation_id, module_id) DO NOTHING""",
                (*self._scope_values(scope), timestamp, timestamp, timestamp, timestamp),
            )
            try:
                cursor = db.execute(
                    """INSERT INTO agent_core_conversation_messages
                    (owner_id, channel, conversation_id, module_id, role, content, metadata_json, stable_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*self._scope_values(scope), role, content, json.dumps(metadata, ensure_ascii=False), stable_key, timestamp),
                )
            except sqlite3.IntegrityError:
                return None
            message_id = int(cursor.lastrowid)
            title = self._title_for(content) if role == "user" else ""
            if title:
                # 标题补全与活动时间戳无关：只看这条 user 消息本身
                db.execute(
                    """UPDATE agent_core_conversations
                    SET title=CASE WHEN title='' THEN ? ELSE title END
                    WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                    (title, *self._scope_values(scope)),
                )
            if touch_activity:
                db.execute(
                    """UPDATE agent_core_conversations
                    SET updated_at=?, last_activity_at=?
                    WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                    (timestamp, timestamp, *self._scope_values(scope)),
                )
            count = db.execute(
                """SELECT COUNT(*) AS count FROM agent_core_conversation_messages
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                self._scope_values(scope),
            ).fetchone()["count"]
            overflow = int(count) - self.history_max_messages
            if overflow > 0:
                db.execute(
                    """DELETE FROM agent_core_conversation_messages WHERE message_id IN (
                        SELECT message_id FROM agent_core_conversation_messages
                        WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                        ORDER BY message_id ASC LIMIT ?
                    )""",
                    (*self._scope_values(scope), overflow),
                )
        return message_id

    def _presentation_row(self, db: sqlite3.Connection, scope: Scope) -> sqlite3.Row | None:
        return db.execute(
            """SELECT acknowledged_message_id FROM agent_core_conversation_presentation
            WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
            self._scope_values(scope),
        ).fetchone()

    def _ensure_presentation_row(self, db: sqlite3.Connection, scope: Scope) -> None:
        """按需建一行游标（值 0）。

        ``WHERE EXISTS`` 不是多余的防御：这张表有指向会话的外键，给一个**不存在
        的会话**插游标会被外键直接拒绝。而前端在"还没发出第一条消息"的新对话上
        就会来查快照 —— 那条路径不该顺手建出一个空会话（它会出现在会话列表里），
        更不该报错。会话不存在时什么都不做，调用方按"游标 0"处理即可。
        """
        values = self._scope_values(scope)
        db.execute(
            """INSERT INTO agent_core_conversation_presentation
            (owner_id, channel, conversation_id, module_id, acknowledged_message_id, updated_at)
            SELECT ?, ?, ?, ?, 0, ?
            WHERE EXISTS (
                SELECT 1 FROM agent_core_conversations
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?
            )
            ON CONFLICT(owner_id, channel, conversation_id, module_id) DO NOTHING""",
            (*values, self._timestamp(), *values),
        )

    def presentation_state(self, scope: Scope) -> int:
        """已读游标：这个对话在舞台上播放到哪一条了。

        记录不存在时按 **0** 建，而不是按当前最大 ``message_id``。理由：
        "老库全部视为已读"是一次性升级语义，由 :meth:`_initialize` 的迁移在
        建表那一刻完成；这里是**新会话**的起点，新会话没有历史，0 才是对的。
        若在这里兜底成最大值，一个正在用的会话只要游标行缺失（误删、换库），
        未读的最终结果就会被整段吞掉 —— 那正是这次维修要修的毛病。
        """
        with self._pool.get_connection() as db:
            row = self._presentation_row(db, scope)
            if row is None:
                self._ensure_presentation_row(db, scope)
                row = self._presentation_row(db, scope)
        return int(row["acknowledged_message_id"]) if row is not None else 0

    def advance_presentation(self, scope: Scope, message_id: int) -> PresentationAdvance:
        """确认**队首**那条未读助手消息已播完（第二次点击）。

        判定顺序，每一步都对应一类真实输入：

        1. 不是本 Scope 的、或者不是助手说的消息 → ``unchanged``。用户消息、
           别的会话的 id、已被裁剪掉的历史都走这里。这不是错误 —— 前端拿不到
           这些 id，出现它们只可能是重复帧或有人在手工发请求。
        2. 目标是已经确认过的（``<= 游标``）→ ``unchanged``。重复点击、乱序
           到达的确认都在这里被吃掉，幂等且不倒退。
        3. 目标是当前未读的**第一条** → ``advanced``。
        4. 目标排在第一条之后 → ``blocked``：想跳过中间那句，拒绝，并告诉
           调用方"该确认的是哪一条"。
        """
        target = int(message_id)
        if target <= 0:
            raise ValueError("message_id must be a positive integer")
        values = self._scope_values(scope)
        with self._pool.get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_presentation_row(db, scope)
            row = self._presentation_row(db, scope)
            current = int(row["acknowledged_message_id"]) if row is not None else 0
            target_row = db.execute(
                """SELECT role FROM agent_core_conversation_messages
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND message_id=?""",
                (*values, target),
            ).fetchone()
            if target_row is None or str(target_row["role"]) != "assistant":
                return PresentationAdvance(ADVANCE_UNCHANGED, current)
            if target <= current:
                return PresentationAdvance(ADVANCE_UNCHANGED, current)
            head_row = db.execute(
                """SELECT MIN(message_id) AS head_id FROM agent_core_conversation_messages
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                  AND role='assistant' AND message_id > ?""",
                (*values, current),
            ).fetchone()
            head = int(head_row["head_id"]) if head_row is not None and head_row["head_id"] is not None else None
            if head is None or target != head:
                return PresentationAdvance(ADVANCE_BLOCKED, current, head)
            db.execute(
                """UPDATE agent_core_conversation_presentation
                SET acknowledged_message_id=?, updated_at=?
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (target, self._timestamp(), *values),
            )
        return PresentationAdvance(ADVANCE_ADVANCED, target)

    def clear(self, scope: Scope, *, now: datetime | None = None) -> bool:
        timestamp = self._timestamp(now)
        with self._pool.get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            deleted = db.execute(
                "DELETE FROM agent_core_conversation_messages WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                self._scope_values(scope),
            )
            # 游标和消息在**同一个事务**里清：清了消息却留着游标，下次打开
            # 会拿一个指向不存在消息的位置去算队列，表现是"未读消息凭空消失"。
            # 同一会话被重新使用时由 presentation_state 重新建 0。
            db.execute(
                "DELETE FROM agent_core_conversation_presentation WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                self._scope_values(scope),
            )
            # 开场联想记录也要一起清。它不是聊天历史，而是**从聊天内容派生出来的
            # 隐私数据**（"上周他说过考试"）；用户说"清空这个对话"，指的是
            # "关于这段对话的一切"，不是"只删气泡、留着系统对它的记忆"。
            # 放在同一事务里：中间状态（气泡没了、派生记录还在）一旦被下一次
            # 开场白读到，就会提起一段用户刚刚清空的对话。
            delete_scope_events(db, scope)
            db.execute(
                """UPDATE agent_core_conversations SET title='', updated_at=?, last_activity_at=?
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (timestamp, timestamp, *self._scope_values(scope)),
            )
        return deleted.rowcount > 0

    def mark_runtime(self, scope: Scope, state: str, *, now: datetime | None = None) -> ConversationRecord:
        timestamp = self._timestamp(now)
        self.ensure(scope, now=now)
        with self._pool.get_connection() as db:
            db.execute(
                """UPDATE agent_core_conversations
                SET runtime_state=?, runtime_updated_at=?, updated_at=?, last_activity_at=?
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
                (state, timestamp, timestamp, timestamp, *self._scope_values(scope)),
            )
        record = self.get(scope)
        assert record is not None
        return record

    def archive(self, scope: Scope, *, now: datetime | None = None) -> bool:
        timestamp = self._timestamp(now)
        with self._pool.get_connection() as db:
            updated = db.execute(
                """UPDATE agent_core_conversations
                SET status='archived', archived_at=?, updated_at=?
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='active'""",
                (timestamp, timestamp, *self._scope_values(scope)),
            )
        return updated.rowcount == 1

    def restore(self, scope: Scope, *, now: datetime | None = None) -> bool:
        timestamp = self._timestamp(now)
        with self._pool.get_connection() as db:
            updated = db.execute(
                """UPDATE agent_core_conversations
                SET status='active', archived_at=NULL, updated_at=?, last_activity_at=?
                WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='archived'""",
                (timestamp, timestamp, *self._scope_values(scope)),
            )
        return updated.rowcount == 1

    def delete(self, scope: Scope) -> bool:
        with self._pool.get_connection() as db:
            deleted = db.execute(
                "DELETE FROM agent_core_conversations WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                self._scope_values(scope),
            )
            # 展示游标跟着会话一起清：没有外键约束时，残留的游标行会让
            # "同一个 conversation_id 重建"继承上一个会话的播放位置。
            db.execute(
                "DELETE FROM agent_core_conversation_presentation WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                self._scope_values(scope),
            )
            # 联想记录同理：会话没了，它对这个会话的"记忆"就不该留在库里。
            # 同一个 conversation_id 之后被复用时，残留记录会让新的开场白
            # 一进门就提起上一个会话的事。
            delete_scope_events(db, scope)
        return deleted.rowcount == 1

    def cleanup(self, *, now: datetime | None = None) -> dict[str, int]:
        current = now or datetime.now()
        archive_before = self._timestamp(current - timedelta(days=self.auto_archive_days))
        delete_before = self._timestamp(current - timedelta(days=self.archive_retention_days))
        timestamp = self._timestamp(current)
        with self._pool.get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            archived = db.execute(
                """UPDATE agent_core_conversations
                SET status='archived', archived_at=?, updated_at=?
                WHERE status='active' AND last_activity_at < ?""",
                (timestamp, timestamp, archive_before),
            )
            deleted = db.execute(
                "DELETE FROM agent_core_conversations WHERE status='archived' AND archived_at < ?",
                (delete_before,),
            )
            # 顺手清掉所有"父会话已经不在了"的游标行。开了外键的连接上级联删除
            # 会自己生效，这里再显式扫一遍是有意的冗余：老库可能还没完成带外键的
            # 重建，而且这一扫能把升级之前删会话时遗留的孤儿一并收掉。
            # 结果不进返回值 —— cleanup 的返回契约已被调用方和用例钉住，
            # 为一个内部收尾动作改形状不划算。
            db.execute(
                """DELETE FROM agent_core_conversation_presentation
                WHERE NOT EXISTS (
                    SELECT 1 FROM agent_core_conversations AS c
                    WHERE c.owner_id=agent_core_conversation_presentation.owner_id
                      AND c.channel=agent_core_conversation_presentation.channel
                      AND c.conversation_id=agent_core_conversation_presentation.conversation_id
                      AND c.module_id=agent_core_conversation_presentation.module_id
                )"""
            )
            # 联想记录同样扫一遍孤儿。这条路径按时间**批量**删会话行，没有逐个
            # Scope 可传，所以只能按"父会话还在不在"判 —— 与上面游标同一个套路。
            # 注意归档的会话**不算**孤儿（行还在，只是 status='archived'），
            # 所以恢复归档会话之后，它的开场白不会突然"失忆"。
            delete_orphan_events(db)
        return {"archived": archived.rowcount, "deleted": deleted.rowcount}

    def mark_running_interrupted(self, *, now: datetime | None = None) -> int:
        timestamp = self._timestamp(now)
        with self._pool.get_connection() as db:
            updated = db.execute(
                """UPDATE agent_core_conversations
                SET runtime_state='interrupted', runtime_updated_at=?, updated_at=?
                WHERE runtime_state='running'""",
                (timestamp, timestamp),
            )
        return updated.rowcount
