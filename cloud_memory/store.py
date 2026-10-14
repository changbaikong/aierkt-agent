"""云端记忆系统的存储层：接收队列 + 变更流水。

【两张表各管什么】
``memory_ingest_queue`` —— 本地上传上来的**原始对话轮次**，等提炼。
``memory_changes`` —— 云端每次增/改/过期/删留下的流水，每条一个自增编号，
本地按编号合并。没有它，本地就只能靠全量比对猜「云端改了什么」。

【为什么编号要用自增整数，不用时间戳】
同一秒里批量写入是很常见的事（一次提炼产出三条记忆），时间戳会撞号，
本地就没法判断先后 —— 而合并的正确性依赖「按顺序应用」。

【表名只在本模块出现】（项目约定）
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

# 变更载荷里带哪些记录列，**唯一来源**在本地那侧（agent_core.memory_sync）。
# 两边各写一份必然会漂移：云端多加一列、本地不认识，或者本地少读一列、
# 合并后悄悄丢字段，而且都不会报错。
from agent_core.memory_sync import SYNCED_COLUMNS, synced_value

QUEUE_PENDING = "pending"
QUEUE_PROCESSING = "processing"
QUEUE_STORED = "stored"
QUEUE_EMPTY = "empty"
#: 提炼成功但**没有新候选**，只更新了已有记忆。以前这轮和「什么都没发生」
#: 都记成 empty：一次真实的内容纠正看起来像空转，排查时分不清。
QUEUE_MAINTAINED = "maintained"
QUEUE_FAILED = "failed"
QUEUE_EXHAUSTED = "exhausted"

# 重试节奏：30 秒 → 2 分钟 → 10 分钟，之后维持 10 分钟，最多 5 次。
# 头两次快是因为绝大多数失败是网络抖动，等十分钟再试纯属浪费；
# 后面慢是因为再失败通常是配置或配额问题，催也没用。
RETRY_DELAYS_SECONDS: tuple[int, ...] = (30, 120, 600, 600, 600)
MAX_ATTEMPTS = 3

ACTION_UPSERT = "upsert"
ACTION_DELETE = "delete"

PAYLOAD_COLUMNS: tuple[str, ...] = (
    *SYNCED_COLUMNS,
    "owner_id", "channel", "conversation_id", "module_id", "created_at", "updated_at",
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def checksum_of(changes: list[dict]) -> str:
    """快照校验值。

    序列化方式必须和本地校验端**逐字节一致**，否则校验永远失败。
    所以顺序固定（sort_keys）、分隔符固定、不转义中文。
    """
    body = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class CloudMemoryStore:
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
                CREATE TABLE IF NOT EXISTS memory_ingest_queue (
                  turn_id TEXT PRIMARY KEY,
                  owner_id TEXT NOT NULL, channel TEXT NOT NULL,
                  conversation_id TEXT NOT NULL, module_id TEXT NOT NULL,
                  user_text TEXT NOT NULL, assistant_reply TEXT NOT NULL DEFAULT '',
                  occurred_at TEXT NOT NULL,
                  packet TEXT,
                  schema_version INTEGER NOT NULL DEFAULT 0,
                  status TEXT NOT NULL DEFAULT 'pending',
                  attempts INTEGER NOT NULL DEFAULT 0,
                  next_retry_at TEXT, last_error TEXT,
                  extracted INTEGER NOT NULL DEFAULT 0,
                  persisted INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);

                CREATE INDEX IF NOT EXISTS memory_ingest_queue_due_idx
                  ON memory_ingest_queue(status, next_retry_at, occurred_at);

                CREATE TABLE IF NOT EXISTS memory_changes (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  record_id TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  action TEXT NOT NULL,
                  updated_at TEXT,
                  payload TEXT NOT NULL DEFAULT '{}',
                  vector TEXT,
                  vector_layer TEXT,
                  created_at TEXT NOT NULL);

                CREATE INDEX IF NOT EXISTS memory_changes_owner_idx
                  ON memory_changes(owner_id, seq);

                CREATE TABLE IF NOT EXISTS memory_access_log (
                  record_id TEXT NOT NULL, minute_bucket TEXT NOT NULL,
                  owner_id TEXT NOT NULL, accessed_at TEXT NOT NULL,
                  applied INTEGER NOT NULL DEFAULT 0,
                  PRIMARY KEY (record_id, minute_bucket));
                """
            )
            # 建表语句是后加的列，早期版本的表里没有 applied。
            # 补上，否则"哪些访问还没并入记录"无从判断，每次都会重复累加。
            columns = {row[1] for row in db.execute("PRAGMA table_info(memory_access_log)")}
            if "applied" not in columns:
                db.execute("ALTER TABLE memory_access_log ADD COLUMN applied INTEGER NOT NULL DEFAULT 0")
            # 同一批补上接收队列的新列：老库里没有它们，
            # 不补的话第一批带处理包的上传会整条失败。
            queue_columns = {row[1] for row in db.execute("PRAGMA table_info(memory_ingest_queue)")}
            for name, definition in {
                "packet": "TEXT",
                "schema_version": "INTEGER NOT NULL DEFAULT 0",
                # 提炼产物先落库：应用更新建议失败时按失败重试，重试复用这一份，
                # 不再调模型 —— 重跑一次模型会得出一版措辞不同的建议，
                # 那条建议的幂等键跟着变，「同一动作只生效一次」也就没了。
                "extraction": "TEXT",
                "suggestions": "TEXT",
                "extraction_done": "INTEGER NOT NULL DEFAULT 0",
                "maintenance_applied": "INTEGER NOT NULL DEFAULT 0",
                "review_results": "TEXT",
                "processing_result": "TEXT",
            }.items():
                if name not in queue_columns:
                    db.execute(f"ALTER TABLE memory_ingest_queue ADD COLUMN {name} {definition}")

    # ---- 接收队列 ---------------------------------------------------

    def enqueue_turn(self, turn: dict) -> bool:
        """入队一轮对话。同一个 turn_id 重复上传只收一次。

        重复上传不是异常情况：本地断网重试、或者上传成功但确认包丢了，
        都会让同一轮被送第二次。按 turn_id 去重是最省事的正确做法。
        """
        turn_id = str(turn.get("turn_id") or "")
        if not turn_id:
            raise ValueError("turn_id 不能为空")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO memory_ingest_queue"
                "(turn_id,owner_id,channel,conversation_id,module_id,user_text,assistant_reply,"
                "occurred_at,packet,schema_version,status,attempts,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'pending',0,?,?)",
                (
                    turn_id,
                    str(turn.get("owner_id") or "local-user"),
                    str(turn.get("channel") or "web"),
                    str(turn.get("conversation_id") or "default"),
                    str(turn.get("module_id") or "companion"),
                    str(turn.get("user_text") or ""),
                    str(turn.get("assistant_reply") or ""),
                    str(turn.get("occurred_at") or _now()),
                    json.dumps(turn.get("packet"), ensure_ascii=False) if turn.get("packet") else None,
                    int(turn.get("schema_version") or 0),
                    _now(),
                    _now(),
                ),
            )
            return cursor.rowcount > 0

    def claim_due(self, *, now: str | None = None, limit: int = 20) -> list[dict]:
        """取一批到期的待处理轮次，并标成 processing。

        标 processing 是为了「处理中重启」这件事：进程在提炼到一半被杀掉时，
        这些轮次的 status 会停在 processing。下一次启动要能把它们捡回来
        （见 :meth:`requeue_stale`），否则它们永远卡住。
        """
        moment = now or _now()
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM memory_ingest_queue "
                "WHERE status IN ('pending','failed') AND (next_retry_at IS NULL OR next_retry_at<=?) "
                "ORDER BY occurred_at, created_at LIMIT ?",
                (moment, limit),
            ).fetchall()
            turn_ids = [row["turn_id"] for row in rows]
            if turn_ids:
                placeholders = ",".join("?" for _ in turn_ids)
                db.execute(
                    f"UPDATE memory_ingest_queue SET status='processing',updated_at=? "
                    f"WHERE turn_id IN ({placeholders})",
                    (moment, *turn_ids),
                )
        return [dict(row) for row in rows]

    def requeue_stale(self) -> int:
        """把上次崩在 processing 的轮次放回队列。

        不做这一步的话，进程重启后这些轮次既不会被 claim（状态不是 pending/failed），
        也没有别的路径碰它们 —— 表现是「有几轮对话永远没提炼，日志里也不报错」。
        """
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE memory_ingest_queue SET status='pending',updated_at=? WHERE status='processing'",
                (_now(),),
            )
            return cursor.rowcount

    def mark_stored(self, turn_id: str, *, extracted: int, persisted: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE memory_ingest_queue SET status='stored',extracted=?,persisted=?,"
                "last_error=NULL,next_retry_at=NULL,updated_at=? WHERE turn_id=?",
                (extracted, persisted, _now(), turn_id),
            )

    def save_review_results(self, turn_id, payload):
        with self.connect() as db:
            db.execute('UPDATE memory_ingest_queue SET review_results=?,updated_at=? WHERE turn_id=?',
                       (json.dumps(payload, ensure_ascii=False), _now(), turn_id))

    def resume_review_configuration(self):
        with self.connect() as db:
            cursor = db.execute("UPDATE memory_ingest_queue SET status='pending',updated_at=? "
                                "WHERE status='pending_review' AND last_error LIKE '%review_not_configured%'", (_now(),))
            return cursor.rowcount

    def save_processing_result(self, turn_id, result):
        with self.connect() as db:
            db.execute('UPDATE memory_ingest_queue SET processing_result=?,updated_at=? WHERE turn_id=?',
                       (json.dumps(result.as_dict(), ensure_ascii=False), _now(), turn_id))
            if result.pending_review or result.rejected:
                status = 'pending_review' if result.pending_review else 'refused'
                db.execute('UPDATE memory_ingest_queue SET status=?,last_error=?,next_retry_at=NULL WHERE turn_id=?',
                           (status, json.dumps(result.pending_review or result.rejected, ensure_ascii=False), turn_id))

    def mark_empty(self, turn_id: str) -> None:
        """提炼成功，但这一轮确实没有值得记住的东西。

        和「失败」分开是必须的：以前两者都记成空结果，于是一次网络超时
        和一次真正的"没内容"看起来一模一样，失败被当成完成、永远不会重试。
        """
        with self.connect() as db:
            db.execute(
                "UPDATE memory_ingest_queue SET status='empty',extracted=0,persisted=0,"
                "last_error=NULL,next_retry_at=NULL,updated_at=? WHERE turn_id=?",
                (_now(), turn_id),
            )

    def mark_maintained(self, turn_id: str, *, applied: int) -> None:
        """这轮没有新候选，但更新了已有记忆：和「什么都没有」分开记。"""
        with self.connect() as db:
            db.execute(
                "UPDATE memory_ingest_queue SET status=?,extracted=0,persisted=0,"
                "maintenance_applied=?,last_error=NULL,next_retry_at=NULL,updated_at=? WHERE turn_id=?",
                (QUEUE_MAINTAINED, int(applied), _now(), turn_id),
            )

    def save_extraction(self, turn_id: str, candidates, suggestions) -> None:
        """把一次提炼的**全部产物**先落库，再去写记忆库和应用更新建议。

        这是「失败可恢复」的前提：应用更新建议失败时这轮按失败重试，
        重试直接复用这里存着的候选与建议，不再调模型。
        """
        from agent_core.memory_turn import serialize_candidates, serialize_suggestions

        with self.connect() as db:
            db.execute(
                "UPDATE memory_ingest_queue SET extraction=?,suggestions=?,extraction_done=1,"
                "updated_at=? WHERE turn_id=?",
                (
                    serialize_candidates(candidates),
                    serialize_suggestions(suggestions),
                    _now(),
                    turn_id,
                ),
            )

    def stored_extraction(self, turn: dict) -> dict:
        """这轮已经落库的提炼产物；没提炼过时 ``done=False``。"""
        from agent_core.memory_turn import decode_candidates, decode_suggestions

        if not int(turn.get("extraction_done") or 0):
            return {"done": False, "candidates": [], "suggestions": []}
        return {
            "done": True,
            "candidates": decode_candidates(turn.get("extraction")),
            "suggestions": decode_suggestions(turn.get("suggestions")),
        }

    def mark_failed(self, turn_id: str, error: str) -> str:
        """记一次失败并排下次重试。返回新状态（``failed`` 或 ``exhausted``）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM memory_ingest_queue WHERE turn_id=?", (turn_id,)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            if attempts >= MAX_ATTEMPTS:
                # 耗尽：保留这条任务、不再自动重试，留给人工看。
                # 丢掉它才是最坏的选择 —— 那轮对话就永久消失了，还没人知道。
                db.execute(
                    "UPDATE memory_ingest_queue SET status='exhausted',attempts=?,last_error=?,"
                    "next_retry_at=NULL,updated_at=? WHERE turn_id=?",
                    (attempts, str(error)[:1000], _now(), turn_id),
                )
                return QUEUE_EXHAUSTED
            delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
            next_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
            db.execute(
                "UPDATE memory_ingest_queue SET status='failed',attempts=?,last_error=?,"
                "next_retry_at=?,updated_at=? WHERE turn_id=?",
                (attempts, str(error)[:1000], next_at, _now(), turn_id),
            )
            return QUEUE_FAILED

    def queue_counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) AS n FROM memory_ingest_queue GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    # ---- 变更流水 ---------------------------------------------------

    def record_change(
        self,
        action: str,
        record: dict,
        *,
        vector: list[float] | None = None,
        vector_layer: str | None = None,
    ) -> int:
        """记一条变更，返回它的编号。"""

        if action == ACTION_DELETE:
            payload: dict = {}
        else:
            payload = {column: record.get(column) for column in PAYLOAD_COLUMNS}
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO memory_changes(record_id,owner_id,action,updated_at,payload,"
                "vector,vector_layer,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(record.get("id") or ""),
                    str(record.get("owner_id") or "local-user"),
                    action,
                    record.get("updated_at") or _now(),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(vector) if vector else None,
                    vector_layer,
                    _now(),
                ),
            )
            return int(cursor.lastrowid or 0)

    def changes_since(self, since: int, *, owner_id: str | None = None, limit: int = 500) -> list[dict]:
        query = "SELECT seq,record_id,owner_id,action,updated_at,payload,vector,vector_layer FROM memory_changes WHERE seq>?"
        args: list = [int(since)]
        if owner_id and owner_id != "*":
            query += " AND owner_id=?"
            args.append(owner_id)
        query += " ORDER BY seq LIMIT ?"
        args.append(int(limit))
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return [_row_to_change(row) for row in rows]

    def max_seq(self, *, owner_id: str | None = None) -> int:
        query = "SELECT COALESCE(MAX(seq),0) AS n FROM memory_changes"
        args: list = []
        if owner_id and owner_id != "*":
            query += " WHERE owner_id=?"
            args.append(owner_id)
        with self.connect() as db:
            return int(db.execute(query, args).fetchone()["n"])

    def snapshot(self, *, owner_id: str, records: list[dict], cursor: int) -> dict:
        """完整快照：把当前所有记录摊成一串 ``upsert`` 变更。

        刻意复用同一套「变更」结构而不是另造一种快照格式 ——
        首次接入和日常增量走同一条合并代码，才不会出现「日常是对的、
        首次接入有 bug」这种最难查的分叉。

        ``cursor`` 由调用方**先取**再读记录。顺序反了的话，快照生成期间
        新写进来的变更会被算进 cursor，但内容不在快照里 ——
        本地把水位推到那个编号之后，这批变更就永久丢了，且毫无痕迹。
        """
        with self.connect() as db:
            vectors = {
                str(row["record_id"]): (row["vector"], row["vector_layer"])
                for row in db.execute(
                    "SELECT record_id,vector,vector_layer FROM memory_changes "
                    "WHERE seq IN (SELECT MAX(seq) FROM memory_changes GROUP BY record_id)"
                )
            }
        changes = []
        for index, record in enumerate(records, start=1):
            vector, layer = vectors.get(str(record["id"]), (None, None))
            changes.append(
                {
                    "seq": index,
                    "record_id": record["id"],
                    "action": ACTION_UPSERT,
                    "updated_at": record.get("updated_at") or _now(),
                    "payload": {column: record.get(column) for column in PAYLOAD_COLUMNS},
                    "vector": json.loads(vector) if vector else None,
                    "vector_layer": layer,
                }
            )
        return {
            "cursor": int(cursor),
            "generated_at": _now(),
            "count": len(changes),
            "checksum": checksum_of(changes),
            "changes": changes,
        }

    def existing_records(self, *, owner_id: str | None = None) -> dict[str, dict]:
        query = "SELECT * FROM memory_records"
        args: list = []
        if owner_id and owner_id != "*":
            query += " WHERE owner_id=?"
            args.append(owner_id)
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return {str(row["id"]): dict(row) for row in rows}

    def import_record(self, record: dict, *, emit: bool = True) -> str:
        """把一条外部记录（本地基线）写进云端库。

        首次接入时用：云端以本地库为准，但**只对齐两边都认识的那些记录**；
        云端独有的记录原样留着，由调用方列进核对结果，不在这里被抹掉。

        返回值：``inserted`` / ``updated`` / ``same``。
        """
        record_id = str(record.get("id") or "")
        if not record_id:
            raise ValueError("记录缺少 id")
        existing = self.existing_records(owner_id="*").get(record_id)
        columns = [*PAYLOAD_COLUMNS]
        if existing is None:
            # 导入的记录统一标成待补向量：本地的向量在另一台机器上，
            # 与其猜，不如让云端自己的向量补偿算一遍。
            names = ["id", *columns, "embedding_status"]
            # 缺列（旧包没有语义字段）时用该列缺省值，不能写 NULL：
            # semantics_version 是 NOT NULL，写 NULL 会让整条导入直接失败。
            values = [record_id, *[synced_value(column, record) for column in columns], "pending"]
            placeholders = ",".join("?" for _ in names)
            with self.connect() as db:
                db.execute(
                    f"INSERT INTO memory_records({','.join(names)}) VALUES({placeholders})", values
                )
            outcome = "inserted"
        else:
            same = all(
                _same(synced_value(column, existing), synced_value(column, record))
                for column in columns
            )
            if same:
                return "same"
            with self.connect() as db:
                db.execute(
                    f"UPDATE memory_records SET {', '.join(f'{c}=?' for c in columns)} WHERE id=?",
                    [*[synced_value(column, record) for column in columns], record_id],
                )
            outcome = "updated"
        if emit:
            fresh = self.existing_records(owner_id="*").get(record_id) or record
            self.record_change(ACTION_UPSERT, fresh)
        return outcome

    def apply_access(self, *, limit: int = 2000) -> int:
        """把收到的访问记录并进记忆记录，让轮转能判断"多久没用过"。

        刻意**不**碰 ``updated_at``、也不发变更：
        访问不是内容变化，把 updated_at 刷新一遍会让云端每次访问都往本地推一条
        变更 —— 本地看到的是"这条记忆又更新了"，而它其实一个字都没变。
        本地自己也在记 last_accessed_at，两边各记各的、不去互相同步这个字段。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT record_id,accessed_at FROM memory_access_log WHERE applied=0 "
                "ORDER BY accessed_at LIMIT ?",
                (int(limit),),
            ).fetchall()
            if not rows:
                return 0
            for row in rows:
                db.execute(
                    "UPDATE memory_records SET "
                    "last_accessed_at=CASE WHEN last_accessed_at IS NULL OR last_accessed_at<? THEN ? "
                    "ELSE last_accessed_at END, "
                    "access_count=access_count+1 WHERE id=?",
                    (row["accessed_at"], row["accessed_at"], row["record_id"]),
                )
            db.executemany(
                "UPDATE memory_access_log SET applied=1 WHERE record_id=? AND minute_bucket=?",
                [(row["record_id"], str(row["accessed_at"])[:16]) for row in rows],
            )
            return len(rows)

    def record_access(self, events: list[dict]) -> int:
        rows = []
        for item in events:
            record_id = str(item.get("record_id") or "")
            if not record_id:
                continue
            moment = str(item.get("occurred_at") or _now())
            rows.append(
                (record_id, moment[:16], str(item.get("owner_id") or "local-user"), moment)
            )
        if not rows:
            return 0
        with self.connect() as db:
            cursor = db.executemany(
                "INSERT OR IGNORE INTO memory_access_log(record_id,minute_bucket,owner_id,accessed_at) "
                "VALUES(?,?,?,?)",
                rows,
            )
            return cursor.rowcount

    def status(self, *, owner_id: str, queue: dict[str, int] | None = None) -> dict:
        counts = queue if queue is not None else self.queue_counts()
        return {
            "owner_id": owner_id,
            "queue": counts,
            "queue_backlog": int(counts.get(QUEUE_PENDING, 0)) + int(counts.get(QUEUE_PROCESSING, 0)),
            "queue_exhausted": int(counts.get(QUEUE_EXHAUSTED, 0)),
            "max_change_seq": self.max_seq(owner_id=owner_id),
        }


def _same(left, right) -> bool:
    """两侧值是否等价。SQLite 会把整数列读成 int、JSON 里可能是 float，
    0 与 0.0 不算差异 —— 否则核对结果会被这种噪声灌满。"""
    if left is None and right is None:
        return True
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return str(left) == str(right)


def _row_to_change(row: sqlite3.Row) -> dict:
    payload = row["payload"]
    vector = row["vector"]
    return {
        "seq": int(row["seq"]),
        "record_id": str(row["record_id"]),
        "action": str(row["action"]),
        "updated_at": row["updated_at"],
        "payload": json.loads(payload) if payload else {},
        "vector": json.loads(vector) if vector else None,
        "vector_layer": row["vector_layer"],
    }
