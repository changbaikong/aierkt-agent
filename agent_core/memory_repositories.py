"""SQLite source-of-truth and local Qdrant indexes for memory records."""
from __future__ import annotations
import logging
import sqlite3
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from qdrant_client import QdrantClient, models

from .contracts import Scope
from . import memory_scope
from .vector_config import QdrantConfig

MEMORY_LAYERS = ("hot", "warm", "cold")
DEFAULT_VECTOR_SIZE = 2048
LOGGER = logging.getLogger("agent_core.memory.repositories")
# 版本 3：载荷新增 project_id / content_status / semantics_version / scope_kind。
# 读到旧版本的记录会在启动回填时补上 —— 向量载荷是派生数据，重推一次是安全的，
# 但**必须**先把载荷写进向量、再标版本号，否则中途失败会留下"标了却没写"的洞。
VECTOR_PAYLOAD_VERSION = 3
VECTOR_PAYLOAD_BATCH_SIZE = 256
_CLIENTS: dict[str, QdrantClient] = {}


def _expires_at_passed(value: Any, *, now: datetime) -> bool:
    """``expires_at`` 是否已经过去。

    格式不可解析时按"没过期"处理：宁可多提一句，也不能因为一条记录的时间字段
    脏了，就把它当成过期事实悄悄丢掉 —— 那是**静默**的数据丢失，比多提一句难查得多。
    空值（多数记录没有到期时间）自然也算没过期。
    """
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment <= now

class MemoryRepositories:
    def __init__(
        self,
        db_path: str | Path,
        vector_path: str | Path | None = None,
        *,
        vector_size: int = DEFAULT_VECTOR_SIZE,
        vector_config: QdrantConfig | None = None
    ) -> None:
        """初始化记忆仓库。

        Args:
            db_path: SQLite 数据库路径
            vector_path: 本地 Qdrant 存储路径（兼容旧接口）
            vector_size: 向量维度
            vector_config: Qdrant 配置（优先级高于 vector_path）
        """
        self.db_path = Path(db_path)
        self.vector_size = vector_size

        # 配置优先级：vector_config > vector_path
        if vector_config is not None:
            self.vector_config = vector_config
        elif vector_path is not None:
            # 兼容旧接口：自动转换为本地模式配置
            self.vector_config = QdrantConfig(mode="local", path=vector_path)
        else:
            raise ValueError("必须指定 vector_path 或 vector_config")

        # 根据配置模式初始化客户端
        if self.vector_config.mode == "local":
            assert self.vector_config.path is not None
            self.vector_path = Path(self.vector_config.path)
            self.vector_path.mkdir(parents=True, exist_ok=True)
            self._key = f"local:{self.vector_path.resolve()}"
            if self._key in _CLIENTS:
                _CLIENTS[self._key].close()
            self.vector_client = QdrantClient(path=str(self.vector_path))
            _CLIENTS[self._key] = self.vector_client
        else:  # server 模式
            self._key = f"server:{self.vector_config.url}"
            if self._key not in _CLIENTS:
                _CLIENTS[self._key] = QdrantClient(
                    url=self.vector_config.url,
                    api_key=self.vector_config.api_key,
                    timeout=self.vector_config.timeout,
                    prefer_grpc=self.vector_config.prefer_grpc,
                    https=self.vector_config.https,
                )
            self.vector_client = _CLIENTS[self._key]
            self.vector_path = None  # server 模式无本地路径
    def close(self) -> None:
        self.vector_client.close()
        if _CLIENTS.get(self._key) is self.vector_client: del _CLIENTS[self._key]
    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        return db
    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS memory_records (
              id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, channel TEXT NOT NULL, conversation_id TEXT NOT NULL,
              module_id TEXT NOT NULL DEFAULT 'companion', visibility TEXT NOT NULL DEFAULT 'module',
              layer TEXT NOT NULL CHECK(layer IN ('hot','warm','cold')), kind TEXT NOT NULL, status TEXT NOT NULL,
              role TEXT, content TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', confidence REAL,
              conflict_key TEXT, idempotency_key TEXT UNIQUE, embedding_status TEXT NOT NULL DEFAULT 'pending',
              embedding_layer TEXT, retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at TEXT,
              last_embedding_error TEXT, last_embedding_attempt_at TEXT, embedding_lease_until TEXT,
              vector_payload_version INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT,
              last_accessed_at TEXT, access_count INTEGER NOT NULL DEFAULT 0,
              importance REAL NOT NULL DEFAULT 1.0, expires_at TEXT, purge_after TEXT,
              retention_locked INTEGER NOT NULL DEFAULT 0, deletion_reason TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts USING fts5(content, content='memory_records', content_rowid='rowid');
            CREATE TRIGGER IF NOT EXISTS memory_records_ai AFTER INSERT ON memory_records BEGIN
              INSERT INTO memory_records_fts(rowid,content) VALUES(new.rowid,new.content); END;
            CREATE TRIGGER IF NOT EXISTS memory_records_ad AFTER DELETE ON memory_records BEGIN
              INSERT INTO memory_records_fts(memory_records_fts,rowid,content) VALUES('delete',old.rowid,old.content); END;
            CREATE TRIGGER IF NOT EXISTS memory_records_au AFTER UPDATE OF content ON memory_records BEGIN
              INSERT INTO memory_records_fts(memory_records_fts,rowid,content) VALUES('delete',old.rowid,old.content);
              INSERT INTO memory_records_fts(rowid,content) VALUES(new.rowid,new.content); END;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(memory_records)")}
            visibility_added = "visibility" not in columns
            for name, definition in {
                "retry_count": "INTEGER NOT NULL DEFAULT 0", "next_retry_at": "TEXT",
                "last_embedding_error": "TEXT", "last_embedding_attempt_at": "TEXT",
                "embedding_lease_until": "TEXT",
                "vector_payload_version": "INTEGER NOT NULL DEFAULT 0",
                "last_accessed_at": "TEXT", "access_count": "INTEGER NOT NULL DEFAULT 0",
                "importance": "REAL NOT NULL DEFAULT 1.0", "expires_at": "TEXT",
                "purge_after": "TEXT", "retention_locked": "INTEGER NOT NULL DEFAULT 0",
                "deletion_reason": "TEXT",
                "module_id": "TEXT NOT NULL DEFAULT 'companion'",
                "visibility": "TEXT NOT NULL DEFAULT 'module'",
                # —— 新语义记录（semantics_version >= 1）用的列 ——
                # 旧记录这几列保持空/0，读取端据此区分新旧格式，不靠 kind 猜。
                "project_id": "TEXT",
                "content_status": "TEXT",
                "semantics_version": "INTEGER NOT NULL DEFAULT 0",
                "semantics_key": "TEXT",
                "scope_kind": "TEXT",
                # —— 记录级版本与复核（自动记忆维护闭环）——
                # 与 semantics_version（格式版本）、profile_version（提炼规则版本）
                # 是**三个不同的东西**，刻意分成三列：混用会让「换了个提炼规则」
                # 看起来像「这条记忆改过一版」。
                "record_version": "INTEGER NOT NULL DEFAULT 0",
                "effective_record_id": "TEXT",
                "review_state": "TEXT NOT NULL DEFAULT 'none'",
                "review_due_at": "TEXT",
                "conflict_with": "TEXT",
            }.items():
                if name not in columns: db.execute(f"ALTER TABLE memory_records ADD COLUMN {name} {definition}")
            if visibility_added:
                # Before module isolation existed, confirmed facts were implicitly
                # user-level. Preserve that visibility on the one-time upgrade,
                # without turning future module-private facts into shared facts.
                db.execute(
                    "UPDATE memory_records SET visibility='shared' "
                    "WHERE kind='fact' AND status='confirmed' AND visibility='module'"
                )
            db.execute(
                "UPDATE memory_records SET idempotency_key=module_id || ':' || idempotency_key "
                "WHERE idempotency_key IS NOT NULL AND idempotency_key NOT LIKE module_id || ':%'"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_records_scope_idx "
                "ON memory_records(owner_id,channel,conversation_id,module_id,visibility,layer,status,created_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_records_conflict_idx "
                "ON memory_records(owner_id,channel,conversation_id,module_id,visibility,conflict_key,status)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_records_project_idx "
                "ON memory_records(owner_id,project_id,layer,status)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_records_semantics_idx "
                "ON memory_records(owner_id,channel,conversation_id,module_id,semantics_version,semantics_key)"
            )
            # 定期内容维护按「该复核谁」取一批：复核状态 + 到点时间。
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_records_review_idx "
                "ON memory_records(review_state,review_due_at)"
            )
        for layer in MEMORY_LAYERS:
            name = f"{layer}_memories_vectors"
            if not self.vector_client.collection_exists(name): self.vector_client.create_collection(name, models.VectorParams(size=self.vector_size, distance=models.Distance.COSINE))
        self._backfill_vector_payloads()

    @staticmethod
    def payload_for(record: dict) -> dict:
        """向量点载荷。

        载荷必须与 SQL 的读取权限**同一口径**：向量检索先按载荷过滤，
        再回 SQL 取正文；两边字段对不上就会"向量能捞到、SQL 却查不着"，
        或者反过来越权捞到别的范围。
        """
        return {
            key: value
            for key, value in {
                "memory_id": record["id"],
                "owner_id": record["owner_id"],
                "channel": record["channel"],
                "conversation_id": record["conversation_id"],
                "module_id": record["module_id"],
                "visibility": record["visibility"],
                # 旧记录没有这几列（语义版本 0），取不到就**不写这个键**，
                # 而不是写 null：写 null 会让"按项目过滤"这类查询多一层歧义。
                "project_id": record.get("project_id"),
                "content_status": record.get("content_status"),
                "semantics_version": record.get("semantics_version"),
                "scope_kind": record.get("scope_kind"),
            }.items()
            if value is not None
        }

    def _backfill_vector_payloads(self) -> None:
        """把旧版本载荷的记录补成当前载荷。

        **派生数据过期不允许把启动拖死。** 库里可能存在「记录还在、向量点已经不在」
        的行（点被删过，或那次 upsert 当时失败）。一次性批量更新会因为其中一个
        不存在的点整体报 404，异常一路抛穿 ``initialize()``，服务直接起不来 ——
        而真实原因只是一条派生数据过期。

        所以这里逐条容错：批量失败就退成逐条，只跳过「点不存在」的那些，
        并把它们标回 ``pending`` 交给补偿工人重建向量；别的错误照旧抛出去，
        不掩盖真正的故障。
        """
        with self.connect() as db:
            records = [dict(row) for row in db.execute(
                "SELECT * FROM memory_records "
                "WHERE embedding_status='ready' AND embedding_layer IS NOT NULL "
                "AND COALESCE(vector_payload_version, 0) < ?",
                (VECTOR_PAYLOAD_VERSION,),
            )]
        by_layer: dict[str, list[dict]] = {}
        for record in records:
            by_layer.setdefault(str(record["embedding_layer"]), []).append(record)

        missing: list[dict] = []
        for layer, layer_records in by_layer.items():
            collection = f"{layer}_memories_vectors"
            for start in range(0, len(layer_records), VECTOR_PAYLOAD_BATCH_SIZE):
                batch = layer_records[start : start + VECTOR_PAYLOAD_BATCH_SIZE]
                operations = [
                    models.SetPayloadOperation(
                        set_payload=models.SetPayload(
                            payload=self.payload_for(record),
                            points=[record["id"]],
                        )
                    )
                    for record in batch
                ]
                try:
                    self.vector_client.batch_update_points(collection, operations)
                except Exception as exc:  # noqa: BLE001 - 派生数据的问题不该拖死启动
                    missing.extend(self._refresh_payloads_one_by_one(collection, batch, exc))
                    continue
                self._mark_payload_version(batch)
        if missing:
            LOGGER.warning(
                "向量载荷回填：%d 条记录在向量库里找不到对应的点，已标回待重建（例：%s）",
                len(missing),
                str(missing[0].get("id"))[:8],
            )

    def _mark_payload_version(self, batch: list[dict]) -> None:
        with self.connect() as db:
            db.executemany(
                "UPDATE memory_records SET vector_payload_version=? "
                "WHERE id=? AND COALESCE(vector_payload_version, 0) < ?",
                [(VECTOR_PAYLOAD_VERSION, record["id"], VECTOR_PAYLOAD_VERSION) for record in batch],
            )

    def _refresh_payloads_one_by_one(self, collection: str, batch: list[dict], error: Exception) -> list[dict]:
        """批量失败后逐条重试；返回「向量点已经不在了」的记录。"""
        missing: list[dict] = []
        for record in batch:
            try:
                self.vector_client.set_payload(
                    collection_name=collection,
                    payload=self.payload_for(record),
                    points=[record["id"]],
                )
            except Exception as exc:  # noqa: BLE001 - 逐个甄别：是点没了，还是别的故障
                if "No point with id" in str(exc) or "not found" in str(exc).lower():
                    missing.append(record)
                    continue
                raise error
            self._mark_payload_version([record])
        if missing:
            stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
            with self.connect() as db:
                db.executemany(
                    "UPDATE memory_records SET embedding_status='pending',"
                    "next_retry_at=?,updated_at=? WHERE id=?",
                    [(stamp, stamp, record["id"]) for record in missing],
                )
        return missing

    def recent_hot_records(self, scope: Scope, *, limit: int | None = None) -> list[dict]:
        """Return active hot records in chronological order.

        ``limit`` is optional so storage remains unbounded; callers that build a
        prompt can apply a token-based window after loading the records.
        """
        base_query = """SELECT * FROM memory_records
                        WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                          AND layer='hot' AND status='active'"""
        args: tuple[object, ...] = (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id)
        if limit is not None:
            query = f"SELECT * FROM ({base_query} ORDER BY created_at DESC LIMIT ?) ORDER BY created_at ASC"
            args += (limit,)
        else:
            query = base_query + " ORDER BY created_at ASC"
        with self.connect() as db:
            rows = db.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def confirmed_shared_facts(
        self,
        scope: Scope,
        *,
        limit: int = 5,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """账号级已确认事实（warm 层 + shared 可见性 + kind=fact + status=confirmed）。

        边界与 :meth:`MemoryRetriever._warm_allowed` 的 shared 分支**同一口径**：
        ``(owner_id, channel)`` 是隔离边界，``conversation_id`` 不是。
        开场白要在"刚开的、还没有任何历史的会话"里提到同账号以前确认过的事，
        靠的正是这一条 —— 按会话取就永远拿不到。

        排除项：非 confirmed（候选不能当事实用）、expired（生命周期已结束）、
        以及 expires_at 已过的记录（双槽记忆到期后 status 可能还没来得及翻牌）。

        过期判断**在 LIMIT 之前**做。放在 Python 层过滤是错的：几条刚到期的
        新记录会先占满 LIMIT 名额，再把它们自己筛掉，结果是一条较旧但**有效**
        的事实因此拿不到 —— 表现是"明明有可用事实，开场白却什么都不提"。
        SQL 里只排除**能确定为过期**的（``expires_at`` 是标准 ISO 且已过）；
        空值与异构格式（老数据）一律放行，交给下面 Python 层的解析器裁决，
        保持"解析不了就按未过期"的兼容规则。

        ``order by created_at desc``：同一账号下最近形成的先给，开场白提到
        "上个月那句"比提到"三年前那句"更像人话。

        刻意**不用** ``updated_at`` 排序：嵌入流水线每次同步都会写一次 ``updated_at``
        （见 ``memory_writes._sync``），用它排序会让"顺序"随着后台维护而抖动 ——
        同一条事实今天排第 1、明天排第 3，而这只是因为它重新算了向量。
        事实的**形成时间**才是稳定的、有语义的排序依据。
        """
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        # 阈值必须与写入端（memory_writes 的 .isoformat(timespec='milliseconds')）
        # 用同一种格式：带 '+00:00' 的串和带 'Z' 的串在同一天时比较会落到后缀上，
        # '+' 比 'Z' 小，边界那一毫秒就会判错。
        threshold = current.astimezone(UTC).isoformat(timespec="milliseconds")
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM memory_records
                   WHERE owner_id=? AND channel=? AND layer='warm'
                     AND visibility='shared' AND kind='fact' AND status='confirmed'
                     AND (
                       expires_at IS NULL OR expires_at = ''
                       OR expires_at NOT LIKE '____-__-__T%'
                       OR expires_at > ?
                     )
                   ORDER BY created_at DESC LIMIT ?""",
                (scope.owner_id, scope.channel, threshold, max(1, int(limit))),
            ).fetchall()
        # 第二层保护：SQL 只能按字符串判，这里用真正的日期解析再确认一次。
        return [dict(row) for row in rows if not _expires_at_passed(row["expires_at"], now=current)]

    def fts_candidates(
        self,
        scope: Scope,
        query: str,
        *,
        layer: str,
        limit: int = 30,
        project_id: str = "",
        channels: Iterable[str] | None = None,
        include_semantic: bool = True,
    ) -> list[str]:
        """Return ranked UUID candidates only; callers must rehydrate and authorize.

        候选筛选里旧格式与新格式用 OR 拼起来：旧记录按 ``visibility`` 判，
        新记录按 ``scope_kind`` 判。选出来了还要过 ``memory_scope.authorize`` ——
        这一步只是把候选范围收窄，不是最终授权。

        ``include_semantic=False``（召回开关关掉）时只查旧格式记录。
        """
        terms = re.findall(r"[\w\u4e00-\u9fff]+", query, flags=re.UNICODE)
        if not terms:
            return []
        match = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        legacy_sql = (
            "(COALESCE(memory_records.semantics_version,0)<1 AND memory_records.channel=? AND ("
            "  memory_records.visibility='shared' OR ("
            "    memory_records.visibility='module' AND memory_records.conversation_id=? AND memory_records.module_id=?"
            "  )))"
        )
        new_sql, new_args = memory_scope.sql_filter_new(
            scope, project_id=project_id, channels=channels, prefix="memory_records.",
            include_semantic=include_semantic,
        )
        visibility = f"({legacy_sql} OR {new_sql})" if include_semantic else f"({legacy_sql})"
        try:
            with self.connect() as db:
                rows = db.execute(
                    f"""SELECT memory_records.id
                       FROM memory_records_fts
                       JOIN memory_records ON memory_records.rowid=memory_records_fts.rowid
                       WHERE memory_records_fts MATCH ? AND memory_records.layer=?
                         AND memory_records.owner_id=?
                         AND {visibility}
                       ORDER BY bm25(memory_records_fts) LIMIT ?""",
                    (match, layer, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id, *new_args, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [str(row[0]) for row in rows]

    @staticmethod
    def _match(key: str, value: str):
        return models.FieldCondition(key=key, match=models.MatchValue(value=value))

    @staticmethod
    def _match_any(key: str, values: list[str]):
        return models.FieldCondition(key=key, match=models.MatchAny(any=values))

    def _vector_scope_filter(
        self,
        scope: Scope,
        *,
        project_id: str = "",
        channels: Iterable[str] | None = None,
        include_semantic: bool = True,
    ):
        """向量侧的候选筛选：账号边界放外层，各范围分支自带渠道/会话约束。"""
        should = memory_scope.payload_scope_should(
            scope,
            project_id=project_id,
            channels=channels,
            match=self._match,
            match_any=self._match_any,
            must=lambda items: models.Filter(must=list(items)),
            include_semantic=include_semantic,
        )
        return models.Filter(must=[self._match("owner_id", scope.owner_id)], should=should)

    def vector_candidates(
        self,
        scope: Scope,
        layer: str,
        vector: list[float],
        *,
        limit: int = 30,
        project_id: str = "",
        channels: Iterable[str] | None = None,
        include_semantic: bool = True,
    ) -> list[str]:
        response = self.vector_client.query_points(
            f"{layer}_memories_vectors", query=vector, limit=limit,
            query_filter=self._vector_scope_filter(
                scope, project_id=project_id, channels=channels, include_semantic=include_semantic
            ),
            with_payload=False,
        )
        return [str(point.id) for point in response.points]

    def records_by_ids(self, record_ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM memory_records WHERE id IN ({placeholders})", ids
            ).fetchall()
        return {str(row["id"]): dict(row) for row in rows}

    def touch_records(self, record_ids: Iterable[str], *, accessed_at: str) -> int:
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            cursor = db.execute(
                f"UPDATE memory_records SET last_accessed_at=?, access_count=access_count+1, updated_at=? "
                f"WHERE id IN ({placeholders}) AND status != 'expired'",
                (accessed_at, accessed_at, *ids),
            )
            return cursor.rowcount

    def count_by_account(self, owner_id: str, module_id: str = "companion") -> dict[str, int]:
        """Count active records for an account without loading memory content."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT status, layer, COUNT(*) AS count FROM memory_records WHERE owner_id=? AND module_id=? GROUP BY status, layer",
                (owner_id, module_id),
            ).fetchall()
        result = {"total": 0, "hot": 0, "warm": 0, "cold": 0, "non_active": 0}
        for row in rows:
            count = int(row["count"])
            if row["status"] == "active":
                result["total"] += count
                if row["layer"] in MEMORY_LAYERS:
                    result[row["layer"]] += count
            else:
                result["non_active"] += count
        return result

def initialize_memory_repositories(db_path: str | Path = Path('data')/'memory_records.db', vector_path: str | Path = Path('data')/'memory_vectors', *, vector_size: int = DEFAULT_VECTOR_SIZE) -> MemoryRepositories:
    repo = MemoryRepositories(db_path, vector_path, vector_size=vector_size); repo.initialize(); return repo
