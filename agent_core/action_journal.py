"""动作审计与重启对账。

【在系统里的位置】

它是 `ActionGateway` 的记账本，只回答三个问题：

    ① 这个动作编号是不是第一次来？（决定要不要真的执行）
    ② 它现在到哪一步了？（running / 终态 / 等审批）
    ③ 上次进程死的时候，有没有动作挂在半路？（重启对账的依据）

【为什么 `outcome_unknown` 必须靠这个表才成立】

`asyncio.wait_for` 超时只是**停止等待**，不是停止执行：同步 handler
跑在线程池里，Python 没有安全的方式把那个线程掐掉。所以一个非只读动作
超时之后，副作用可能已经发生、也可能还没有 —— 我们**无法确认**。

唯一能表达这件事的办法是：执行**之前**先把 `running` 写下来，
进程重启时把这些遗留的 `running` 一律改判 `outcome_unknown`。
这样"上次跑到一半就没了"是一条持久化的、可查证的事实，
而不是一句代码注释里的口头约定。

【为什么不存参数值】

审计表是高频读写的，而且会被 /api 之类的地方读出来给人看。
参数值里可能有本机路径、坐标、用户原话 —— 那些是会话与 Work 账本该管的数据。
这里只留**字段名**（知道这次动了哪些槽位）与授权结果，不留内容。
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .connection_pool import get_pool
from .contracts import ActionRequest

logger = logging.getLogger(__name__)

#: 动作已经登记但还没跑完。重启时遗留的 `running` 会被对账成 outcome_unknown。
STATUS_RUNNING = "running"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ActionJournal:
    """动作审计表。只用 SQLite，跟项目其余存储走同一个连接池。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with get_pool(str(self.db_path)).get_connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS action_journal (
                    action_id     TEXT PRIMARY KEY,
                    name          TEXT NOT NULL,
                    origin        TEXT NOT NULL,
                    owner_id      TEXT NOT NULL,
                    channel       TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id     TEXT NOT NULL,
                    run_id        TEXT NOT NULL DEFAULT '',
                    turn_id       TEXT NOT NULL DEFAULT '',
                    tool_call_id  TEXT NOT NULL DEFAULT '',
                    -- 参数只存**字段名**，不存值（见模块头注释）。
                    argument_keys TEXT NOT NULL DEFAULT '',
                    status        TEXT NOT NULL,
                    error_kind    TEXT NOT NULL DEFAULT '',
                    approval_id   TEXT NOT NULL DEFAULT '',
                    duration_ms   INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_action_journal_status ON action_journal (status)"
            )

    # ── 写入 ────────────────────────────────────────────────────────

    def register(self, request: ActionRequest) -> bool:
        """原子登记一个动作编号。

        返回 ``True`` 表示这是**第一次**登记（调用方应继续执行）；
        ``False`` 表示这个编号已经存在（调用方**不得**重跑）。

        用 `INSERT OR IGNORE` + `rowcount` 而不是"先查后插"：
        后者在两处并发时都会查到"不存在"，然后都插、都执行 ——
        这正是要防的双跑。
        """
        scope = request.scope
        stamp = _now()
        with get_pool(str(self.db_path)).get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO action_journal
                    (action_id, name, origin, owner_id, channel, conversation_id, module_id,
                     run_id, turn_id, tool_call_id, argument_keys, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.action_id,
                    request.name,
                    request.origin,
                    scope.owner_id,
                    scope.channel,
                    scope.conversation_id,
                    scope.module_id,
                    request.run_id,
                    request.turn_id,
                    request.tool_call_id,
                    # 排序后再存，保证同样的参数集合得到同样的字符串（便于比对）。
                    ",".join(sorted(request.arguments or {})),
                    STATUS_RUNNING,
                    stamp,
                    stamp,
                ),
            )
            return cursor.rowcount == 1

    def finish(
        self,
        action_id: str,
        *,
        status: str,
        error_kind: str = "",
        approval_id: str = "",
        duration_ms: int = 0,
    ) -> None:
        """记终态（或等审批态）。

        记录不存在时**静默返回**，不抛异常：收尾路径（异常处理、对账）
        本来就可能拿到一条已经被清理的记录，为此打断流程得不偿失。
        """
        with get_pool(str(self.db_path)).get_connection() as conn:
            conn.execute(
                """
                UPDATE action_journal
                   SET status = ?, error_kind = ?, approval_id = ?, duration_ms = ?, updated_at = ?
                 WHERE action_id = ?
                """,
                (status, error_kind, approval_id, int(duration_ms), _now(), action_id),
            )

    # ── 读取 ────────────────────────────────────────────────────────

    def get(self, action_id: str) -> dict[str, Any] | None:
        with get_pool(str(self.db_path)).get_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM action_journal WHERE action_id = ?", (action_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with get_pool(str(self.db_path)).get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM action_journal ORDER BY created_at DESC, action_id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ── 重启对账 ────────────────────────────────────────────────────

    def reconcile_on_start(self) -> int:
        """把遗留的 `running` 改判 `outcome_unknown`，返回改了几条。

        这是 `outcome_unknown` 能成立的**唯一**依据（见模块头注释）。
        只动 `running`：
        - `waiting_approval` 跨重启本来就该活着，不能碰；
        - 其它终态已经确定，更不能碰。

        幂等：第二次调用返回 0（已经没有 running 了），
        所以启动流程重试也不会重复记账。
        """
        with get_pool(str(self.db_path)).get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE action_journal
                   SET status = 'outcome_unknown',
                       error_kind = 'indeterminate',
                       updated_at = ?
                 WHERE status = ?
                """,
                (_now(), STATUS_RUNNING),
            )
            repaired = cursor.rowcount
        if repaired:
            # 只记条数，不记动作名 —— 审计表本身能查，日志保持精简。
            logger.warning("reconciled %d interrupted action(s) to outcome_unknown", repaired)
        return repaired
