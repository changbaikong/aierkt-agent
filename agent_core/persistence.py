"""
SQLite persistence contracts for resumable Agent runs.

这个模块负责持久化存储：
- Agent 运行快照（RunSnapshot）
- 待确认的操作（ConfirmationRequest）
- 运行时锁（RuntimeLock）

已改进：使用连接池管理连接，防止资源泄漏
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
import logging

from .contracts import AccountTaskScope, ConfirmationRequest, RunSnapshot, RuntimeLock, Scope
from .connection_pool import get_pool
from .routing import RouteRecord

logger = logging.getLogger(__name__)


class PendingConfirmationExists(RuntimeError):
    """账户/模块已有活跃的高风险操作等待确认"""


class RunStore(Protocol):
    """运行快照存储协议"""
    def save(self, snapshot: RunSnapshot) -> None: ...
    def load(self, run_id: str, scope: Scope) -> RunSnapshot | None: ...


class ConfirmationStore(Protocol):
    """确认请求存储协议"""
    def create(
        self,
        *,
        run_id: str,
        scope: Scope,
        tool_call_id: str,
        tool: str,
        arguments: dict,
        title: str,
        ttl_hours: int = 24,
        module_spec_json: str = "{}",
    ) -> ConfirmationRequest: ...

    def get(self, action_id: str, scope: Scope) -> ConfirmationRequest | None: ...
    def pending_by_code(self, code: str, scope: Scope) -> ConfirmationRequest | None: ...
    def pending_for_scope(self, scope: Scope) -> list[ConfirmationRequest]: ...
    def reject_pending(self, scope: Scope) -> int: ...
    def reject_pending_for_owner(self, owner_id: str) -> int: ...
    def decide(self, action_id: str, scope: Scope, decision: str) -> ConfirmationRequest | None: ...
    def get_for_account(self, action_id: str, scope: AccountTaskScope) -> ConfirmationRequest | None: ...
    def pending_for_account(self, scope: AccountTaskScope) -> list[ConfirmationRequest]: ...
    def decide_for_account(self, action_id: str, scope: AccountTaskScope, decision: str) -> ConfirmationRequest | None: ...
    def complete(self, action_id: str, scope: Scope) -> None: ...
    def active_lock(self, scope: AccountTaskScope) -> RuntimeLock | None: ...
    def recover_locks(self) -> list[RuntimeLock]: ...


# ============= 已弃用：旧的连接方法（保留用于向后兼容） =============
# 新代码应该使用连接池：get_pool(path).get_connection()

def _connect(path: Path) -> sqlite3.Connection:
    """
    创建数据库连接（已弃用，请使用连接池）

    ⚠️ 警告：此方法每次创建新连接，容易导致资源泄漏
    建议使用 get_pool(path).get_connection() 替代
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=15.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


@contextmanager
def _database(path: Path):
    """
    数据库上下文管理器（已弃用，请使用连接池）

    ⚠️ 警告：每次创建新连接，请改用：
    with get_pool(path).get_connection() as db:
        # 使用连接
    """
    db = _connect(path)
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _pool_for(path: Path):
    return get_pool(str(path))


class SQLiteRunStore:
    """
    Agent 运行快照存储（SQLite 实现）

    已改进：
    - 使用连接池管理连接
    - 添加了日志记录
    - 保留向后兼容的 _database 方法
    """

    def __init__(self, path: Path | str) -> None:
        """
        初始化运行快照存储

        Args:
            path: 数据库文件路径（可以是 Path 对象或字符串）
        """
        self.path = Path(path)

        # 确保表结构存在
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_runs (
                    run_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL DEFAULT 'companion',
                    status TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    steps INTEGER NOT NULL,
                    tokens INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            # 向后兼容：添加 module_id 列（如果不存在）
            columns = {row[1] for row in db.execute("PRAGMA table_info(agent_core_runs)")}
            if "module_id" not in columns:
                db.execute("ALTER TABLE agent_core_runs ADD COLUMN module_id TEXT NOT NULL DEFAULT 'companion'")
                logger.info("Added module_id column to agent_core_runs table")

    def save(self, snapshot: RunSnapshot) -> None:
        """
        保存运行快照到数据库

        Args:
            snapshot: 运行快照对象

        为什么用 UPSERT（INSERT ... ON CONFLICT）？
        就像存档游戏进度：
        - 第一次存档 → INSERT（新建记录）
        - 后续存档 → UPDATE（更新已有记录）
        用 UPSERT 可以一条语句搞定，不用判断是新建还是更新
        """
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                """INSERT INTO agent_core_runs
                (run_id, owner_id, channel, conversation_id, module_id, status, messages_json, steps, tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
                messages_json=excluded.messages_json, steps=excluded.steps,
                tokens=excluded.tokens, updated_at=excluded.updated_at""",
                (
                    snapshot.run_id,
                    snapshot.scope.owner_id,
                    snapshot.scope.channel,
                    snapshot.scope.conversation_id,
                    snapshot.scope.module_id,
                    snapshot.status,
                    json.dumps(snapshot.messages, ensure_ascii=False),
                    snapshot.steps,
                    snapshot.tokens,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def load(self, run_id: str, scope: Scope) -> RunSnapshot | None:
        with _pool_for(self.path).get_connection() as db:
            row = db.execute(
                "SELECT * FROM agent_core_runs WHERE run_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (run_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            ).fetchone()
        if row is None:
            return None
        return RunSnapshot(
            run_id=row["run_id"],
            scope=Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
            messages=json.loads(row["messages_json"]),
            steps=int(row["steps"]),
            tokens=int(row["tokens"]),
            status=row["status"],
        )

    def public_review_evidence(self, run_ids: tuple[str, ...]) -> dict[str, dict[str, str]]:
        """Return only public user/final-assistant text for route review.

        System prompts, tool calls, tool arguments, tool observations and
        intermediate assistant messages are intentionally excluded.  This is a
        review export, not a general run-snapshot bypass.
        """
        ids = tuple(dict.fromkeys(str(item) for item in run_ids if str(item).strip()))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with _pool_for(self.path).get_connection() as db:
            rows = db.execute(
                f"SELECT run_id, messages_json FROM agent_core_runs WHERE run_id IN ({placeholders})",
                ids,
            ).fetchall()
        output: dict[str, dict[str, str]] = {}
        for row in rows:
            try:
                messages = json.loads(row["messages_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(messages, list):
                continue
            user_text = ""
            assistant_reply = ""
            for message in messages:
                if not isinstance(message, dict):
                    continue
                role = message.get("role")
                content = message.get("content")
                if role == "user" and isinstance(content, str) and content.strip():
                    user_text = content.strip()
                elif (
                    role == "assistant"
                    and not message.get("tool_calls")
                    and isinstance(content, str)
                    and content.strip()
                ):
                    assistant_reply = content.strip()
            if user_text and assistant_reply:
                output[str(row["run_id"])] = {
                    "user_request": user_text,
                    "assistant_reply": assistant_reply,
                }
        return output


class SQLiteConfirmationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_confirmations (
                    action_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL DEFAULT 'companion',
                    tool_call_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    code TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT DEFAULT '',
                    module_spec_json TEXT DEFAULT ''
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(agent_core_confirmations)")}
            if "module_id" not in columns:
                db.execute("ALTER TABLE agent_core_confirmations ADD COLUMN module_id TEXT NOT NULL DEFAULT 'companion'")
            if "module_spec_json" not in columns:
                db.execute("ALTER TABLE agent_core_confirmations ADD COLUMN module_spec_json TEXT DEFAULT '{}'")
            db.execute("DROP INDEX IF EXISTS idx_agent_core_confirmation_code")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_core_confirmation_code "
                "ON agent_core_confirmations(owner_id, module_id, code) WHERE status='pending'"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_locks (
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    action_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, channel, conversation_id, module_id)
                )"""
            )
            self._migrate_lock_table(db)
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_runtime_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_recovery_rotation (
                    owner_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    next_index INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, module_id)
                )"""
            )

    @staticmethod
    def _migrate_lock_table(db: sqlite3.Connection) -> None:
        """Atomically upgrade legacy account/module locks to complete Scope locks."""
        columns = db.execute("PRAGMA table_info(agent_core_locks)").fetchall()
        primary_key = [
            row["name"] for row in sorted(columns, key=lambda row: int(row["pk"])) if row["pk"]
        ]
        expected = ["owner_id", "channel", "conversation_id", "module_id"]
        if primary_key == expected:
            return
        required = {"owner_id", "module_id", "channel", "conversation_id", "action_id", "reason", "created_at"}
        if not required.issubset({row["name"] for row in columns}):
            raise RuntimeError("cannot migrate agent_core_locks: legacy schema is missing required columns")
        legacy_table = "agent_core_locks_scope_migration_backup"
        try:
            db.execute("SAVEPOINT agent_core_locks_scope_migration")
            db.execute(f"ALTER TABLE agent_core_locks RENAME TO {legacy_table}")
            db.execute(
                """CREATE TABLE agent_core_locks (
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    action_id TEXT NOT NULL UNIQUE,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, channel, conversation_id, module_id)
                )"""
            )
            db.execute(
                f"""INSERT INTO agent_core_locks
                (owner_id, channel, conversation_id, module_id, action_id, reason, created_at)
                SELECT owner_id, channel, conversation_id, module_id, action_id, reason, created_at
                FROM {legacy_table}"""
            )
            db.execute(f"DROP TABLE {legacy_table}")
            db.execute("RELEASE SAVEPOINT agent_core_locks_scope_migration")
        except Exception as exc:
            try:
                db.execute("ROLLBACK TO SAVEPOINT agent_core_locks_scope_migration")
                db.execute("RELEASE SAVEPOINT agent_core_locks_scope_migration")
            except sqlite3.Error:
                pass
            raise RuntimeError("agent_core_locks migration failed; legacy table was preserved") from exc

    @staticmethod
    def _ensure_run_table(db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS agent_core_runs (
                run_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                module_id TEXT NOT NULL DEFAULT 'companion',
                status TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                steps INTEGER NOT NULL,
                tokens INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )

    @staticmethod
    def _lock_from_row(row: sqlite3.Row | None) -> RuntimeLock | None:
        if row is None:
            return None
        return RuntimeLock(
            row["owner_id"], row["module_id"], row["channel"], row["conversation_id"],
            row["action_id"], row["reason"], row["created_at"],
        )

    @staticmethod
    def _append_event(
        db: sqlite3.Connection,
        *,
        event_type: str,
        run_id: str,
        scope: Scope,
        payload: dict,
    ) -> None:
        db.execute(
            """INSERT INTO agent_core_runtime_events
            (event_id, event_type, run_id, owner_id, channel, conversation_id, module_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                event_type,
                run_id,
                scope.owner_id,
                scope.channel,
                scope.conversation_id,
                scope.module_id,
                json.dumps(payload, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def create_lock(
        self,
        scope: AccountTaskScope | Scope,
        action_id: str,
        source_scope: Scope | None = None,
        *,
        reason: str = "waiting_confirmation",
    ) -> RuntimeLock:
        source_scope = source_scope or (scope if isinstance(scope, Scope) else None)
        if source_scope is None:
            raise ValueError("source_scope is required when creating a legacy account lock")
        created_at = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            try:
                db.execute(
                    """INSERT INTO agent_core_locks
                    (owner_id, module_id, channel, conversation_id, action_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (source_scope.owner_id, source_scope.module_id, source_scope.channel, source_scope.conversation_id, action_id, reason, created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise PendingConfirmationExists(
                    f"conversation {source_scope.conversation_id!r} already has an active action"
                ) from exc
        return RuntimeLock(source_scope.owner_id, source_scope.module_id, source_scope.channel, source_scope.conversation_id, action_id, reason, created_at)

    def active_lock(self, scope: AccountTaskScope | Scope) -> RuntimeLock | None:
        self.recover_locks()
        with _pool_for(self.path).get_connection() as db:
            if isinstance(scope, Scope):
                row = db.execute(
                    "SELECT * FROM agent_core_locks WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                    (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM agent_core_locks WHERE owner_id=? AND module_id=? ORDER BY created_at",
                    (scope.account_id, scope.module_id),
                ).fetchone()
        return self._lock_from_row(row)

    def events_for_run(self, run_id: str) -> list[dict]:
        """Return audit events for one run in their durable creation order."""
        with _pool_for(self.path).get_connection() as db:
            rows = db.execute(
                "SELECT event_type, run_id, payload_json, created_at FROM agent_core_runtime_events "
                "WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "run_id": row["run_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def next_recovery_template_index(self, scope: AccountTaskScope, template_count: int) -> int:
        """Atomically claim the next template slot for an account/module."""
        if int(template_count) < 1:
            raise ValueError("template_count must be positive")
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT next_index FROM agent_core_recovery_rotation WHERE owner_id=? AND module_id=?",
                (scope.account_id, scope.module_id),
            ).fetchone()
            current = int(row["next_index"]) if row is not None else 0
            selected = current % int(template_count)
            next_index = (selected + 1) % int(template_count)
            db.execute(
                "INSERT INTO agent_core_recovery_rotation(owner_id, module_id, next_index, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(owner_id, module_id) DO UPDATE SET next_index=excluded.next_index, updated_at=excluded.updated_at",
                (scope.account_id, scope.module_id, next_index, now),
            )
        return selected

    def recover_locks(self) -> list[RuntimeLock]:
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                """UPDATE agent_core_confirmations SET status='expired', decided_at=?
                WHERE status='pending' AND expires_at <= ?""",
                (now, now),
            )
            db.execute(
                """DELETE FROM agent_core_locks
                WHERE NOT EXISTS (
                    SELECT 1 FROM agent_core_confirmations c
                    WHERE c.action_id=agent_core_locks.action_id
                      AND c.status = 'pending'
                      AND c.expires_at > ?
                )""",
                (now,),
            )
            rows = db.execute("SELECT * FROM agent_core_locks ORDER BY created_at").fetchall()
            existing = {row["action_id"] for row in rows}
            pending = db.execute(
                """SELECT * FROM agent_core_confirmations
                WHERE status='pending' AND expires_at > ?
                ORDER BY rowid""",
                (now,),
            ).fetchall()
            owned_scopes = {
                (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
                for row in rows
            }
            for row in pending:
                if row["action_id"] in existing:
                    continue
                identity = (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
                if identity in owned_scopes:
                    db.execute(
                        "UPDATE agent_core_confirmations SET status='rejected', decided_at=? "
                        "WHERE action_id=? AND status='pending'",
                        (now, row["action_id"]),
                    )
                    self._append_event(
                        db,
                        event_type="confirmation.rejected",
                        run_id=row["run_id"],
                        scope=Scope(row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
                        payload={"action_id": row["action_id"], "tool": row["tool"], "reason": "competing_pending_recovery"},
                    )
                    continue
                try:
                    db.execute(
                        """INSERT INTO agent_core_locks
                        (owner_id, module_id, channel, conversation_id, action_id, reason, created_at)
                        VALUES (?, ?, ?, ?, ?, 'waiting_confirmation', ?)""",
                        (row["owner_id"], row["module_id"], row["channel"], row["conversation_id"], row["action_id"], now),
                    )
                    existing.add(row["action_id"])
                    owned_scopes.add(identity)
                except sqlite3.IntegrityError:
                    # Another worker wrote the exact Scope lock first.
                    continue
            rows = db.execute("SELECT * FROM agent_core_locks ORDER BY created_at").fetchall()
        return [self._lock_from_row(row) for row in rows if self._lock_from_row(row) is not None]

    def release_lock(self, action_id: str, scope: AccountTaskScope | Scope) -> bool:
        with _pool_for(self.path).get_connection() as db:
            if isinstance(scope, Scope):
                updated = db.execute(
                    "DELETE FROM agent_core_locks WHERE action_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                    (action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
                )
            else:
                updated = db.execute(
                    "DELETE FROM agent_core_locks WHERE action_id=? AND owner_id=? AND module_id=?",
                    (action_id, scope.account_id, scope.module_id),
                )
        return updated.rowcount == 1

    def pause(
        self,
        *,
        snapshot: RunSnapshot,
        tool_call_id: str,
        tool: str,
        arguments: dict,
        title: str,
        ttl_hours: int = 24,
        module_spec_json: str = "{}",
    ) -> ConfirmationRequest:
        """Persist a paused run, confirmation, account lock and event atomically."""
        expires_at = datetime.now() + timedelta(hours=ttl_hours)
        now = datetime.now().isoformat(timespec="seconds")
        action_id = uuid.uuid4().hex
        code = uuid.uuid4().hex[:8].upper()
        scope = snapshot.scope
        with _pool_for(self.path).get_connection() as db:
            self._ensure_run_table(db)
            db.execute(
                """INSERT INTO agent_core_runs
                (run_id, owner_id, channel, conversation_id, module_id, status, messages_json, steps, tokens, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
                messages_json=excluded.messages_json, steps=excluded.steps,
                tokens=excluded.tokens, updated_at=excluded.updated_at""",
                (
                    snapshot.run_id, scope.owner_id, scope.channel, scope.conversation_id,
                    scope.module_id, "paused", json.dumps(snapshot.messages, ensure_ascii=False),
                    snapshot.steps, snapshot.tokens, now,
                ),
            )
            try:
                db.execute(
                    """INSERT INTO agent_core_confirmations
                    (action_id, run_id, owner_id, channel, conversation_id, module_id, tool_call_id,
                     tool, arguments_json, code, title, status, expires_at, module_spec_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                    (
                        action_id, snapshot.run_id, scope.owner_id, scope.channel, scope.conversation_id,
                        scope.module_id, tool_call_id, tool, json.dumps(arguments, ensure_ascii=False),
                        code, title, expires_at.isoformat(timespec="seconds"), module_spec_json,
                    ),
                )
                db.execute(
                    """INSERT INTO agent_core_locks
                    (owner_id, module_id, channel, conversation_id, action_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, 'waiting_confirmation', ?)""",
                    (scope.owner_id, scope.module_id, scope.channel, scope.conversation_id, action_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise PendingConfirmationExists(
                    f"conversation {scope.conversation_id!r} already has an active action"
                ) from exc
            self._append_event(
                db,
                event_type="turn.paused",
                run_id=snapshot.run_id,
                scope=scope,
                payload={"action_id": action_id, "tool": tool},
            )
        return ConfirmationRequest(
            action_id, snapshot.run_id, scope.owner_id, scope.channel, scope.conversation_id,
            tool_call_id, tool, dict(arguments), code, title, expires_at.isoformat(timespec="seconds"),
            module_id=scope.module_id, module_spec_json=module_spec_json,
        )

    def create(
        self,
        *,
        run_id: str,
        scope: Scope,
        tool_call_id: str,
        tool: str,
        arguments: dict,
        title: str,
        ttl_hours: int = 24,
        module_spec_json: str = "{}",
    ) -> ConfirmationRequest:
        expires_at = datetime.now() + timedelta(hours=ttl_hours)
        with _pool_for(self.path).get_connection() as db:
            for _ in range(5):
                action_id = uuid.uuid4().hex
                code = uuid.uuid4().hex[:8].upper()
                try:
                    db.execute(
                        """INSERT INTO agent_core_confirmations
                        (action_id, run_id, owner_id, channel, conversation_id, module_id, tool_call_id,
                         tool, arguments_json, code, title, status, expires_at, module_spec_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                        (
                            action_id,
                            run_id,
                            scope.owner_id,
                            scope.channel,
                            scope.conversation_id,
                            scope.module_id,
                            tool_call_id,
                            tool,
                            json.dumps(arguments, ensure_ascii=False),
                            code,
                            title,
                            expires_at.isoformat(timespec="seconds"),
                            module_spec_json,
                        ),
                    )
                    break
                except sqlite3.IntegrityError:
                    continue
            else:
                raise RuntimeError("unable to allocate confirmation code")
        return ConfirmationRequest(
            action_id,
            run_id,
            scope.owner_id,
            scope.channel,
            scope.conversation_id,
            tool_call_id,
            tool,
            dict(arguments),
            code,
            title,
            expires_at.isoformat(timespec="seconds"),
            module_id=scope.module_id,
            module_spec_json=module_spec_json,
        )

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ConfirmationRequest:
        # sqlite3.Row doesn't have .get(), use try/except for optional columns
        try:
            module_spec_json = row["module_spec_json"]
        except (KeyError, IndexError):
            module_spec_json = "{}"

        return ConfirmationRequest(
            row["action_id"], row["run_id"], row["owner_id"], row["channel"],
            row["conversation_id"], row["tool_call_id"], row["tool"],
            json.loads(row["arguments_json"]), row["code"], row["title"],
            row["expires_at"], row["status"], row["module_id"],
            module_spec_json,
        )

    def get(self, action_id: str, scope: Scope) -> ConfirmationRequest | None:
        with _pool_for(self.path).get_connection() as db:
            row = db.execute(
                "SELECT * FROM agent_core_confirmations WHERE action_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            ).fetchone()
            if row and row["status"] == "pending" and datetime.fromisoformat(row["expires_at"]) <= datetime.now():
                db.execute(
                    "UPDATE agent_core_confirmations SET status='expired', decided_at=? WHERE action_id=? AND status='pending'",
                    (datetime.now().isoformat(timespec="seconds"), action_id),
                )
                row = dict(row)
                row["status"] = "expired"
        return self._row_to_request(row) if row else None

    def pending_by_code(self, code: str, scope: Scope) -> ConfirmationRequest | None:
        with _pool_for(self.path).get_connection() as db:
            row = db.execute(
                "SELECT * FROM agent_core_confirmations "
                "WHERE code=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='pending' "
                "AND expires_at > ? "
                "ORDER BY rowid DESC LIMIT 1",
                (code.upper(), scope.owner_id, scope.channel, scope.conversation_id, scope.module_id, datetime.now().isoformat(timespec="seconds")),
            ).fetchone()
        return self._row_to_request(row) if row else None

    def pending_for_scope(self, scope: Scope) -> list[ConfirmationRequest]:
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                "UPDATE agent_core_confirmations SET status='expired', decided_at=? "
                "WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='pending' AND expires_at <= ?",
                (now, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id, now),
            )
            rows = db.execute(
                "SELECT * FROM agent_core_confirmations WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? "
                "AND status='pending' AND expires_at > ? ORDER BY rowid DESC",
                (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id, now),
            ).fetchall()
        return [self._row_to_request(row) for row in rows]

    def get_for_account(self, action_id: str, scope: AccountTaskScope) -> ConfirmationRequest | None:
        with _pool_for(self.path).get_connection() as db:
            row = db.execute(
                "SELECT * FROM agent_core_confirmations WHERE action_id=? AND owner_id=? AND module_id=?",
                (action_id, scope.account_id, scope.module_id),
            ).fetchone()
        return self._row_to_request(row) if row else None

    def pending_for_account(self, scope: AccountTaskScope) -> list[ConfirmationRequest]:
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute("UPDATE agent_core_confirmations SET status='expired', decided_at=? WHERE owner_id=? AND module_id=? AND status='pending' AND expires_at <= ?", (now, scope.account_id, scope.module_id, now))
            rows = db.execute("SELECT * FROM agent_core_confirmations WHERE owner_id=? AND module_id=? AND status='pending' AND expires_at > ? ORDER BY rowid DESC", (scope.account_id, scope.module_id, now)).fetchall()
        return [self._row_to_request(row) for row in rows]

    def reject_pending(self, scope: Scope) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                "UPDATE agent_core_confirmations SET status='expired', decided_at=? "
                "WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? "
                "AND status='pending' AND expires_at <= ?",
                (now, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id, now),
            )
            updated = db.execute(
                "UPDATE agent_core_confirmations SET status='rejected', decided_at=? "
                "WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='pending'",
                (now, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            )
            db.execute(
                "DELETE FROM agent_core_locks WHERE owner_id=? AND module_id=? AND channel=? AND conversation_id=?",
                (scope.owner_id, scope.module_id, scope.channel, scope.conversation_id),
            )
        return updated.rowcount

    def reject_pending_account(self, scope: AccountTaskScope) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute("UPDATE agent_core_confirmations SET status='expired', decided_at=? WHERE owner_id=? AND module_id=? AND status='pending' AND expires_at <= ?", (now, scope.account_id, scope.module_id, now))
            updated = db.execute("UPDATE agent_core_confirmations SET status='rejected', decided_at=? WHERE owner_id=? AND module_id=? AND status='pending'", (now, scope.account_id, scope.module_id))
            db.execute("DELETE FROM agent_core_locks WHERE owner_id=? AND module_id=?", (scope.account_id, scope.module_id))
        return updated.rowcount

    def reject_pending_for_owner(self, owner_id: str) -> int:
        """按 owner 拒绝该账号**所有**会话的待确认，并清掉它们的会话锁。

        与 ``reject_pending`` 的区别只在范围：那个要一个完整 Scope（等于锁定
        单个会话），这个只认 owner_id。切到完全访问时用这个 —— 权限是账号级的，
        清理也必须是账号级的，只清当前 conversation_id 会留下一堆点了就执行的
        旧确认框。

        只改状态，**不执行**任何被挂起的动作：拒绝就是拒绝。
        """
        owner = str(owner_id or "").strip()
        if not owner:
            return 0
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            db.execute(
                "UPDATE agent_core_confirmations SET status='expired', decided_at=? "
                "WHERE owner_id=? AND status='pending' AND expires_at <= ?",
                (now, owner, now),
            )
            updated = db.execute(
                "UPDATE agent_core_confirmations SET status='rejected', decided_at=? "
                "WHERE owner_id=? AND status='pending'",
                (now, owner),
            )
            db.execute("DELETE FROM agent_core_locks WHERE owner_id=?", (owner,))
        return int(updated.rowcount or 0)

    def decide(self, action_id: str, scope: Scope, decision: str) -> ConfirmationRequest | None:
        if decision not in {"confirmed", "rejected", "expired"}:
            raise ValueError("invalid confirmation decision")
        current = self.get(action_id, scope)
        if current is None or current.status != "pending":
            return None
        status = decision
        if datetime.fromisoformat(current.expires_at) <= datetime.now():
            status = "expired"
        with _pool_for(self.path).get_connection() as db:
            updated = db.execute(
                "UPDATE agent_core_confirmations SET status=?, decided_at=? WHERE action_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='pending'",
                (status, datetime.now().isoformat(timespec="seconds"), action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            )
            if updated.rowcount != 1:
                return None
            db.execute(
                "DELETE FROM agent_core_locks WHERE action_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            )
        return ConfirmationRequest(
            current.action_id, current.run_id, current.owner_id, current.channel,
            current.conversation_id, current.tool_call_id, current.tool,
            current.arguments, current.code, current.title, current.expires_at, status,
            current.module_id,
        )

    def decide_for_account(self, action_id: str, scope: AccountTaskScope, decision: str) -> ConfirmationRequest | None:
        if decision not in {"confirmed", "rejected", "expired"}:
            raise ValueError("invalid confirmation decision")
        now = datetime.now()
        with _pool_for(self.path).get_connection() as db:
            self._ensure_run_table(db)
            row = db.execute(
                "SELECT * FROM agent_core_confirmations WHERE action_id=? AND owner_id=? AND module_id=?",
                (action_id, scope.account_id, scope.module_id),
            ).fetchone()
            if row is None or row["status"] != "pending":
                return None
            status = "expired" if datetime.fromisoformat(row["expires_at"]) <= now else decision
            updated = db.execute("UPDATE agent_core_confirmations SET status=?, decided_at=? WHERE action_id=? AND owner_id=? AND module_id=? AND status='pending'", (status, now.isoformat(timespec="seconds"), action_id, scope.account_id, scope.module_id))
            if updated.rowcount != 1:
                return None
            if status in {"rejected", "expired"}:
                db.execute("DELETE FROM agent_core_locks WHERE action_id=? AND owner_id=? AND module_id=?", (action_id, scope.account_id, scope.module_id))
                db.execute(
                    "UPDATE agent_core_runs SET status=?, updated_at=? WHERE run_id=?",
                    (status, now.isoformat(timespec="seconds"), row["run_id"]),
                )
            current = self._row_to_request(row)
            self._append_event(
                db,
                event_type=f"confirmation.{status}",
                run_id=current.run_id,
                scope=Scope(current.owner_id, current.channel, current.conversation_id, current.module_id),
                payload={"action_id": current.action_id, "tool": current.tool},
            )
        return ConfirmationRequest(current.action_id, current.run_id, current.owner_id, current.channel, current.conversation_id, current.tool_call_id, current.tool, current.arguments, current.code, current.title, current.expires_at, status, current.module_id, current.module_spec_json)

    def complete(self, action_id: str, scope: Scope) -> None:
        with _pool_for(self.path).get_connection() as db:
            self._ensure_run_table(db)
            row = db.execute(
                "SELECT * FROM agent_core_confirmations WHERE action_id=? AND owner_id=? "
                "AND channel=? AND conversation_id=? AND module_id=?",
                (action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            ).fetchone()
            updated = db.execute(
                "UPDATE agent_core_confirmations SET status='completed', decided_at=COALESCE(NULLIF(decided_at, ''), ?) "
                "WHERE action_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND status='confirmed'",
                (datetime.now().isoformat(timespec="seconds"), action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            )
            if updated.rowcount:
                db.execute(
                    "DELETE FROM agent_core_locks WHERE action_id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                    (action_id, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
                )
                if row is not None:
                    db.execute(
                        "UPDATE agent_core_runs SET status='completed', updated_at=? WHERE run_id=?",
                        (datetime.now().isoformat(timespec="seconds"), row["run_id"]),
                    )
                self._append_event(
                    db,
                    event_type="confirmation.completed",
                    run_id=str(row["run_id"]),
                    scope=scope,
                    payload={"action_id": action_id},
                )


class RouteStore(Protocol):
    """路由决策存储协议：一次决策先落 decided_*，回合结束再回填 actual_*。"""
    def save(self, record: RouteRecord) -> None: ...
    def finish(
        self,
        run_id: str,
        *,
        actual_path: str,
        final_status: str,
        task_id: str | None = None,
        actual_route_kind: str = "",
        requested_tools: tuple[str, ...] = (),
        requested_task_type: str = "",
        tool_statuses: tuple[dict[str, Any], ...] = (),
        valid_turn: bool = False,
        model_called: bool = False,
    ) -> None: ...
    def stats(self, limit: int = 20, *, now: datetime | None = None) -> dict[str, Any]: ...


class SQLiteRouteStore:
    """主 Chat 路由决策的落库实现。

    为什么要单独一张表：`work_events` 只覆盖走 WorkHost 的那部分，
    而判断"路由有没有误判"需要**三条路径合在一起看**——
    建议走 A、实际走了 B，这条记录只有在统一表里才对得上。

    表是新增的，不动任何现有 schema：删掉它只是失去观测能力，不影响主链路。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)

    @staticmethod
    def _ensure_table(db: sqlite3.Connection) -> None:
        db.execute(
            """CREATE TABLE IF NOT EXISTS agent_core_route_decisions (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL DEFAULT '',
                user_text TEXT NOT NULL DEFAULT '',
                decided_path TEXT NOT NULL,
                decided_strength TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'rule',
                task_type TEXT DEFAULT '',
                task_id TEXT DEFAULT '',
                actual_path TEXT DEFAULT '',
                final_status TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        existing = {
            str(row[1])
            for row in db.execute("PRAGMA table_info(agent_core_route_decisions)").fetchall()
        }
        additions = {
            "routing_mode": "TEXT NOT NULL DEFAULT 'legacy'",
            "actual_route_kind": "TEXT NOT NULL DEFAULT ''",
            "requested_tools": "TEXT NOT NULL DEFAULT '[]'",
            "requested_task_type": "TEXT NOT NULL DEFAULT ''",
            "tool_statuses": "TEXT NOT NULL DEFAULT '[]'",
            "valid_turn": "INTEGER NOT NULL DEFAULT 0",
            "model_called": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in existing:
                db.execute(
                    f"ALTER TABLE agent_core_route_decisions ADD COLUMN {name} {declaration}"
                )
        db.execute(
            """CREATE TABLE IF NOT EXISTS agent_core_route_observation_heartbeats (
                observed_at TEXT PRIMARY KEY,
                routing_mode TEXT NOT NULL,
                legacy_shadow_enabled INTEGER NOT NULL,
                service_status TEXT NOT NULL,
                heartbeat_valid INTEGER NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS agent_core_route_observation_state (
                state_id INTEGER PRIMARY KEY CHECK (state_id = 1),
                phase TEXT NOT NULL DEFAULT 'observing',
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                sample_path TEXT NOT NULL DEFAULT '',
                sample_claimed_at TEXT NOT NULL DEFAULT '',
                review_result_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )"""
        )
        columns = {
            str(row[1])
            for row in db.execute(
                "PRAGMA table_info(agent_core_route_observation_state)"
            ).fetchall()
        }
        if "review_result_json" not in columns:
            db.execute(
                "ALTER TABLE agent_core_route_observation_state "
                "ADD COLUMN review_result_json TEXT NOT NULL DEFAULT '{}'"
            )

    def record_observation_heartbeat(
        self,
        *,
        routing_mode: str,
        legacy_shadow_enabled: bool,
        service_status: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one real deployment-health observation for the seven-day gate."""
        observed = now or datetime.now()
        valid = (
            routing_mode == "model"
            and bool(legacy_shadow_enabled)
            and service_status in {"healthy", "degraded"}
        )
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            db.execute(
                "INSERT OR REPLACE INTO agent_core_route_observation_heartbeats "
                "(observed_at, routing_mode, legacy_shadow_enabled, service_status, heartbeat_valid) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    observed.isoformat(timespec="seconds"),
                    routing_mode,
                    1 if legacy_shadow_enabled else 0,
                    service_status,
                    1 if valid else 0,
                ),
            )
        return {
            "observed_at": observed.isoformat(timespec="seconds"),
            "routing_mode": routing_mode,
            "legacy_shadow_enabled": bool(legacy_shadow_enabled),
            "service_status": service_status,
            "heartbeat_valid": valid,
        }

    def observation_state(self) -> dict[str, Any]:
        """Return the durable phase and last meaningful observation snapshot."""
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            row = db.execute(
                "SELECT phase, snapshot_json, sample_path, sample_claimed_at, "
                "review_result_json, updated_at "
                "FROM agent_core_route_observation_state WHERE state_id=1"
            ).fetchone()
        if row is None:
            return {
                "phase": "observing",
                "snapshot": {},
                "sample_path": "",
                "sample_claimed_at": "",
                "review_result": {},
                "updated_at": "",
            }
        try:
            snapshot = json.loads(row["snapshot_json"] or "{}")
        except (TypeError, ValueError):
            snapshot = {}
        try:
            review_result = json.loads(row["review_result_json"] or "{}")
        except (TypeError, ValueError):
            review_result = {}
        return {
            "phase": str(row["phase"] or "observing"),
            "snapshot": snapshot if isinstance(snapshot, dict) else {},
            "sample_path": str(row["sample_path"] or ""),
            "sample_claimed_at": str(row["sample_claimed_at"] or ""),
            "review_result": review_result if isinstance(review_result, dict) else {},
            "updated_at": str(row["updated_at"] or ""),
        }

    def begin_observation_campaign(
        self,
        snapshot: dict[str, Any],
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Start one explicit acceptance campaign without deleting route history."""
        observed = now or datetime.now()
        updated_at = observed.isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            db.execute(
                """INSERT INTO agent_core_route_observation_state
                (state_id, phase, snapshot_json, sample_path, sample_claimed_at,
                 review_result_json, updated_at)
                VALUES (1, 'observing', ?, '', '', '{}', ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    phase='observing', snapshot_json=excluded.snapshot_json,
                    sample_path='', sample_claimed_at='', review_result_json='{}',
                    updated_at=excluded.updated_at""",
                (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), updated_at),
            )
        return {
            "phase": "observing",
            "snapshot": dict(snapshot),
            "sample_path": "",
            "sample_claimed_at": "",
            "review_result": {},
            "updated_at": updated_at,
        }

    def save_observation_state(
        self,
        *,
        snapshot: dict[str, Any],
        phase: str | None = None,
        review_result: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Persist a meaningful observation snapshot without recording a heartbeat."""
        observed = now or datetime.now()
        current = self.observation_state()
        next_phase = phase or str(current["phase"])
        next_review_result = (
            review_result if review_result is not None else current["review_result"]
        )
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            db.execute(
                """INSERT INTO agent_core_route_observation_state
                (state_id, phase, snapshot_json, sample_path, sample_claimed_at,
                 review_result_json, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    phase=excluded.phase,
                    snapshot_json=excluded.snapshot_json,
                    sample_path=excluded.sample_path,
                    sample_claimed_at=excluded.sample_claimed_at,
                    review_result_json=excluded.review_result_json,
                    updated_at=excluded.updated_at""",
                (
                    next_phase,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    current["sample_path"],
                    current["sample_claimed_at"],
                    json.dumps(next_review_result, ensure_ascii=False, sort_keys=True),
                    observed.isoformat(timespec="seconds"),
                ),
            )
        return {
            "phase": next_phase,
            "snapshot": snapshot,
            "review_result": next_review_result,
            "updated_at": observed.isoformat(timespec="seconds"),
        }

    def set_observation_phase(
        self,
        phase: str,
        *,
        review_result: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        current = self.observation_state()
        return self.save_observation_state(
            snapshot=current["snapshot"], phase=phase,
            review_result=review_result, now=now,
        )

    def claim_review_sample(self, sample_path: str, *, now: datetime | None = None) -> bool:
        """Atomically claim the one allowed manual-review batch."""
        observed = now or datetime.now()
        claimed_at = observed.isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            cursor = db.execute(
                """INSERT INTO agent_core_route_observation_state
                (state_id, phase, snapshot_json, sample_path, sample_claimed_at, updated_at)
                VALUES (1, 'manual_review_ready', '{}', ?, ?, ?)
                ON CONFLICT(state_id) DO UPDATE SET
                    phase='manual_review_ready', sample_path=excluded.sample_path,
                    sample_claimed_at=excluded.sample_claimed_at, updated_at=excluded.updated_at
                WHERE agent_core_route_observation_state.sample_path=''""",
                (str(sample_path), claimed_at, claimed_at),
            )
        return cursor.rowcount == 1

    def save(self, record: RouteRecord) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            db.execute(
                """INSERT OR REPLACE INTO agent_core_route_decisions (
                    run_id, conversation_id, user_text, decided_path, decided_strength,
                    reason, source, task_type, task_id, actual_path, final_status,
                    created_at, updated_at, routing_mode, actual_route_kind,
                    requested_tools, requested_task_type, tool_statuses, valid_turn, model_called
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.run_id,
                    record.conversation_id,
                    record.user_text[:500],
                    record.decided_path,
                    record.decided_strength,
                    record.reason,
                    record.source,
                    record.task_type or "",
                    record.task_id or "",
                    record.actual_path or "",
                    record.final_status or "",
                    now,
                    now,
                    record.routing_mode,
                    "",
                    "[]",
                    "",
                    "[]",
                    0,
                    0,
                ),
            )

    def finish(
        self,
        run_id: str,
        *,
        actual_path: str,
        final_status: str,
        task_id: str | None = None,
        actual_route_kind: str = "",
        requested_tools: tuple[str, ...] = (),
        requested_task_type: str = "",
        tool_statuses: tuple[dict[str, Any], ...] = (),
        valid_turn: bool = False,
        model_called: bool = False,
    ) -> None:
        """回合结束后回填事实。没有对应的决策记录时静默跳过（比如路由被禁用）。"""
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            updates = (
                actual_path,
                final_status,
                actual_route_kind,
                json.dumps(list(requested_tools), ensure_ascii=False),
                requested_task_type,
                json.dumps(list(tool_statuses), ensure_ascii=False),
                1 if valid_turn else 0,
                1 if model_called else 0,
                datetime.now().isoformat(timespec="seconds"),
            )
            if task_id is None:
                db.execute(
                    "UPDATE agent_core_route_decisions SET actual_path=?, final_status=?, "
                    "actual_route_kind=?, requested_tools=?, requested_task_type=?, tool_statuses=?, "
                    "valid_turn=?, model_called=?, updated_at=? WHERE run_id=?",
                    (*updates, run_id),
                )
            else:
                db.execute(
                    "UPDATE agent_core_route_decisions SET actual_path=?, final_status=?, "
                    "actual_route_kind=?, requested_tools=?, requested_task_type=?, tool_statuses=?, "
                    "valid_turn=?, model_called=?, updated_at=?, task_id=? WHERE run_id=?",
                    (*updates, task_id, run_id),
                )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """按时间倒序取最近的决策，用于判断路由是否误判。"""
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            rows = db.execute(
                "SELECT * FROM agent_core_route_decisions ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def valid_model_records(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Return recent valid model-routed turns for phase-four human review.

        The route ledger deliberately contains tool names and statuses but no tool
        arguments.  Keeping this query on the store prevents acceptance scripts
        from depending on SQLite migration details or accidentally widening the
        selected population to legacy/invalid turns.
        """
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            rows = db.execute(
                "SELECT * FROM agent_core_route_decisions "
                "WHERE routing_mode='model' AND valid_turn=1 "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def campaign_model_records(
        self,
        *,
        account_id: str,
        conversation_prefix: str,
        started_at: str,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        """Return only valid model turns produced by the campaign's Web scope."""
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            SQLiteConfirmationStore._ensure_run_table(db)
            rows = db.execute(
                """SELECT d.*, r.owner_id AS run_owner_id, r.channel AS run_channel,
                    r.status AS run_status
                FROM agent_core_route_decisions d
                JOIN agent_core_runs r ON r.run_id=d.run_id
                WHERE d.routing_mode='model' AND d.valid_turn=1 AND d.model_called=1
                  AND d.created_at>=? AND r.owner_id=? AND r.channel='web'
                  AND r.conversation_id LIKE ?
                ORDER BY d.created_at ASC, d.rowid ASC LIMIT ?""",
                (
                    str(started_at),
                    str(account_id),
                    f"{str(conversation_prefix)}%",
                    max(1, int(limit)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def misrouted(self, limit: int = 50) -> list[dict[str, Any]]:
        """建议路径与实际路径不一致的记录（NONE 不算，因为它本就没给建议）。"""
        return [
            row
            for row in self.recent(limit)
            if row["decided_strength"] != "none"
            and row["actual_path"]
            and row["decided_path"] != row["actual_path"]
        ]

    def stats(self, limit: int = 20, *, now: datetime | None = None) -> dict[str, Any]:
        """聚合观测：总量、分布、误判率。

        误判的定义与 ``misrouted()`` 一致：给了建议（strength != none）且
        实际路径已回填，但两者不一致。HARD 的记录实际路径恒等于 work_host，
        所以误判率天然主要衡量 SOFT 建议的质量。
        """
        with _pool_for(self.path).get_connection() as db:
            self._ensure_table(db)
            total = db.execute(
                "SELECT COUNT(*) AS n FROM agent_core_route_decisions"
            ).fetchone()["n"]
            by_path = db.execute(
                "SELECT decided_path AS k, COUNT(*) AS n FROM agent_core_route_decisions GROUP BY decided_path"
            ).fetchall()
            by_strength = db.execute(
                "SELECT decided_strength AS k, COUNT(*) AS n FROM agent_core_route_decisions GROUP BY decided_strength"
            ).fetchall()
            evaluated = db.execute(
                """SELECT COUNT(*) AS n FROM agent_core_route_decisions
                WHERE routing_mode='legacy' AND decided_strength <> 'none' AND actual_path <> ''"""
            ).fetchone()["n"]
            misrouted = db.execute(
                """SELECT COUNT(*) AS n FROM agent_core_route_decisions
                WHERE routing_mode='legacy' AND decided_strength <> 'none' AND actual_path <> ''
                  AND decided_path <> actual_path"""
            ).fetchone()["n"]
            recent_misrouted = db.execute(
                """SELECT * FROM agent_core_route_decisions
                WHERE routing_mode='legacy' AND decided_strength <> 'none' AND actual_path <> ''
                  AND decided_path <> actual_path
                ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (int(limit),),
            ).fetchall()
            by_routing_mode = db.execute(
                "SELECT routing_mode AS k, COUNT(*) AS n FROM agent_core_route_decisions GROUP BY routing_mode"
            ).fetchall()
            valid_model_turns = db.execute(
                "SELECT COUNT(*) AS n FROM agent_core_route_decisions "
                "WHERE routing_mode='model' AND valid_turn=1"
            ).fetchone()["n"]
            model_route_kinds = db.execute(
                "SELECT actual_route_kind AS k, COUNT(*) AS n FROM agent_core_route_decisions "
                "WHERE routing_mode='model' AND valid_turn=1 AND actual_route_kind<>'' "
                "GROUP BY actual_route_kind"
            ).fetchall()
            legacy_shadow_evaluated = db.execute(
                "SELECT COUNT(*) AS n FROM agent_core_route_decisions "
                "WHERE routing_mode='model' AND decided_strength<>'none' AND actual_path<>''"
            ).fetchone()["n"]
            legacy_shadow_disagreed = db.execute(
                "SELECT COUNT(*) AS n FROM agent_core_route_decisions "
                "WHERE routing_mode='model' AND decided_strength<>'none' AND actual_path<>'' "
                "AND decided_path<>actual_path"
            ).fetchone()["n"]
            first_valid_at = db.execute(
                "SELECT MIN(created_at) AS value FROM agent_core_route_decisions "
                "WHERE routing_mode='model' AND valid_turn=1"
            ).fetchone()["value"]
            heartbeat_rows = db.execute(
                "SELECT * FROM agent_core_route_observation_heartbeats ORDER BY observed_at ASC"
            ).fetchall()
        current = now or datetime.now()
        elapsed_days = 0.0
        if first_valid_at:
            try:
                elapsed_days = max(
                    0.0,
                    (current - datetime.fromisoformat(str(first_valid_at))).total_seconds() / 86400,
                )
            except ValueError:
                elapsed_days = 0.0
        heartbeat_gap_hours = 12.0
        continuous_start: datetime | None = None
        previous_at: datetime | None = None
        last_at: datetime | None = None
        last_valid = False
        for raw in heartbeat_rows:
            try:
                observed_at = datetime.fromisoformat(str(raw["observed_at"]))
            except ValueError:
                continuous_start = None
                previous_at = None
                last_valid = False
                continue
            row_valid = bool(raw["heartbeat_valid"])
            gap_too_large = (
                previous_at is not None
                and (observed_at - previous_at).total_seconds() > heartbeat_gap_hours * 3600
            )
            if not row_valid:
                continuous_start = None
            elif continuous_start is None or gap_too_large or not last_valid:
                continuous_start = observed_at
            previous_at = observed_at
            last_at = observed_at
            last_valid = row_valid
        heartbeat_age_hours = None
        current_heartbeat_valid = False
        if last_at is not None:
            heartbeat_age_hours = max(0.0, (current - last_at).total_seconds() / 3600)
            current_heartbeat_valid = last_valid and heartbeat_age_hours <= heartbeat_gap_hours
        continuous_days = 0.0
        if continuous_start is not None and current_heartbeat_valid:
            continuous_days = max(0.0, (current - continuous_start).total_seconds() / 86400)
        time_ready = bool(valid_model_turns) and current_heartbeat_valid and continuous_days >= 7.0
        volume_ready = int(valid_model_turns) >= 1000
        return {
            "total": int(total),
            "by_decided_path": {row["k"]: int(row["n"]) for row in by_path},
            "by_decided_strength": {row["k"]: int(row["n"]) for row in by_strength},
            "evaluated": int(evaluated),
            "misrouted": int(misrouted),
            "misrouted_rate": round(misrouted / evaluated, 4) if evaluated else None,
            "recent_misrouted": [dict(row) for row in recent_misrouted],
            "by_routing_mode": {row["k"]: int(row["n"]) for row in by_routing_mode},
            "valid_model_turns": int(valid_model_turns),
            "model_route_kinds": {row["k"]: int(row["n"]) for row in model_route_kinds},
            "legacy_shadow_evaluated": int(legacy_shadow_evaluated),
            "legacy_shadow_disagreed": int(legacy_shadow_disagreed),
            "legacy_shadow_disagreement_rate": (
                round(legacy_shadow_disagreed / legacy_shadow_evaluated, 4)
                if legacy_shadow_evaluated
                else None
            ),
            "observation_gate": {
                "first_valid_at": first_valid_at,
                "elapsed_days": round(elapsed_days, 4),
                "continuous_start_at": (
                    continuous_start.isoformat(timespec="seconds") if continuous_start else None
                ),
                "continuous_days": round(continuous_days, 4),
                "last_heartbeat_at": last_at.isoformat(timespec="seconds") if last_at else None,
                "heartbeat_age_hours": (
                    round(heartbeat_age_hours, 4) if heartbeat_age_hours is not None else None
                ),
                "heartbeat_gap_hours": heartbeat_gap_hours,
                "current_heartbeat_valid": current_heartbeat_valid,
                "time_ready": time_ready,
                "volume_ready": volume_ready,
                "eligible_for_manual_review": time_ready or volume_ready,
            },
        }
