"""与云端记忆系统双向同步：上传对话轮次、拉取云端变更、合并回本地库。

【分工】
云端正本 = 提炼、轮转、维护。本正本 = 记录与读取。
所以本地**只上传原始对话**，不上传记忆；记忆由云端提炼后以「变更编号」的形式回流。

【为什么合并要按编号顺序、并且应用成功才推进水位】
云端把每次增/改/过期/删都记成一条带自增编号的变更。本地存一个「已应用到第几号」。
边合并边推进水位，一旦中途崩了，重放的是**没应用成功**的那些 —— 幂等。
反过来先推进水位再应用，崩一次就永久丢一批变更，而且看不出来。

【本地更新的记录不会被云端旧数据覆盖】
同一条记录两边都改过时，比 ``updated_at``，谁新听谁。本地在对话中刚写的东西，
不会因为云端那边还是十分钟前的版本就被改回去。

【删除一律走归档】
云端说删，本地只是标成 archived 并退回 cold 层，不物理删行。
和项目既有惯例一致（归档不删），也留了事后追查的余地。

【表名/列名只在本模块与 memory_sync_store 出现】
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from qdrant_client import models

from .contracts import Scope
from .memory_repositories import VECTOR_PAYLOAD_VERSION, MemoryRepositories
from .memory_sync_store import (
    STATE_ACCESS_WATERMARK,
    STATE_BASELINE_AT,
    STATE_CHANGE_CURSOR,
    STATE_LAST_SYNC_AT,
    MemorySyncStore,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 600
DEFAULT_SHUTDOWN_GRACE_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 30.0

# 云端可以改写的记录列。刻意不含 id/归属四元组/created_at ——
# 那些是身份与排序依据，由上传时的原始信息决定，不能让云端改。
#
# 末尾五个是新语义记录的列：两端必须同步它们，否则云端算出来的
# project_id / content_status 到不了本地，本地读到的范围与状态就各说各话。
# 旧版包不带这几列，落地时按「未知」处理（语义版本 0、其余留空），
# 不猜、也不悄悄降级成已确认事实。
SYNCED_COLUMNS: tuple[str, ...] = (
    "content", "metadata", "confidence", "conflict_key", "importance",
    "layer", "kind", "status", "role", "visibility",
    "expires_at", "purge_after", "retention_locked", "deletion_reason", "archived_at",
    "project_id", "content_status", "semantics_version", "semantics_key", "scope_kind",
    # 记录级版本与复核状态：不跟着同步，两端会各说各话 ——
    # 本端已经改到第 3 版，另一端还停在「第 1 版、无需复核」，
    # 于是同一条记忆在两边有不同结论，谁也说不清哪边为准。
    "record_version", "effective_record_id", "review_state", "review_due_at", "conflict_with",
)

#: 落地时不能为 NULL 的新列（连同缺省值）。旧包不带它们时用缺省值补，
#: 直接写 NULL 会撞 NOT NULL 约束，把整次同步卡住。
_SYNCED_DEFAULTS: dict[str, Any] = {"semantics_version": 0, "record_version": 0, "review_state": "none"}


def synced_value(column: str, payload: dict) -> Any:
    """从云端载荷里取一列的值；缺失或为 None 时用该列的缺省值。"""
    value = payload.get(column)
    if value is None and column in _SYNCED_DEFAULTS:
        return _SYNCED_DEFAULTS[column]
    return value

ACTION_UPSERT = "upsert"
ACTION_DELETE = "delete"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _parse_moment(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


class CloudMemoryClient:
    """云端记忆系统的 HTTP 客户端。URL 指向本地隧道入口，不是公网地址。"""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, payload: dict | None = None, query: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def push_turns(self, turns: list[dict]) -> dict:
        return self._request("POST", "/api/memory/sync/events", {"turns": turns})

    def push_access(self, events: list[dict]) -> dict:
        return self._request("POST", "/api/memory/sync/events", {"access": events})

    def pull_changes(self, since: int, *, limit: int = 500) -> dict:
        return self._request("GET", "/api/memory/sync/changes", query={"since": since, "limit": limit})

    def fetch_snapshot(self) -> dict:
        return self._request("GET", "/api/memory/sync/snapshot")

    def status(self) -> dict:
        return self._request("GET", "/api/memory/sync/status")


def apply_changes(
    repositories: MemoryRepositories,
    store: MemorySyncStore,
    changes: list[dict],
) -> dict:
    """按编号顺序把云端变更合并进本地库，**一条一事务**。

    返回 ``{applied, skipped, errors}``。任何一条失败只影响它自己，不会把
    整批吞掉 —— 否则一条脏数据就能让同步永远卡在同一个位置。

    刻意不把整批包成一个大事务：一批几百条变更里有一条格式不对，
    整批回滚就等于**所有**正确的变更都白做了，而水位又会因为失败而停在原地，
    下一轮重放同一批、再失败一次 —— 变成死循环。
    """
    ordered = sorted(changes, key=lambda item: int(item.get("seq") or 0))
    applied = 0
    skipped = 0
    errors: list[str] = []

    for change in ordered:
        seq = int(change.get("seq") or 0)
        if seq <= 0:
            errors.append("变更缺少有效编号")
            continue
        try:
            # 第一步：把记录写进库并提交。向量安装放在事务之外 ——
            # 装向量失败（比如云端没给向量、或者维度对不上）不该把记录一起回滚掉，
            # 那条记录留在 pending 状态由本地向量补偿补算就行。
            with repositories.connect() as db:
                outcome, vector_job = _apply_one(db, repositories, change)
                store.mark_applied(db, seq)
        except Exception as exc:  # noqa: BLE001 - 单条失败不能拖垮整批
            errors.append(f"seq={seq}: {type(exc).__name__}: {exc}")
            continue

        if vector_job is not None:
            try:
                _install_vector(repositories, *vector_job)
            except Exception as exc:  # noqa: BLE001 - 记录已在库里，向量失败只降级
                errors.append(f"seq={seq} 向量未安装: {type(exc).__name__}: {exc}")

        if outcome == "stale":
            skipped += 1
        else:
            applied += 1

    return {"applied": applied, "skipped": skipped, "errors": errors}


def _apply_one(
    db: sqlite3.Connection,
    repositories: MemoryRepositories,
    change: dict,
) -> tuple[str, tuple | None]:
    """写一条变更。返回 ``(结果, 待安装的向量任务或 None)``。

    向量任务刻意不在这里装：这个函数跑在写事务里，装向量要访问 Qdrant，
    是一个可能失败的**外部**动作。放在事务里意味着它一失败，
    刚写好的记录也跟着回滚 —— 记录本身明明是对的。
    """
    record_id = str(change.get("record_id") or "")
    if not record_id:
        raise ValueError("变更缺少 record_id")
    existing = db.execute("SELECT * FROM memory_records WHERE id=?", (record_id,)).fetchone()
    action = str(change.get("action") or ACTION_UPSERT)
    cloud_updated = _parse_moment(change.get("updated_at"))

    if action == ACTION_DELETE:
        if existing and not _cloud_is_newer(cloud_updated, existing["updated_at"]):
            return "stale", None
        if existing:
            db.execute(
                "UPDATE memory_records SET status='archived',layer='cold',archived_at=?,"
                "deletion_reason='cloud_deleted',updated_at=?,embedding_status='pending' WHERE id=?",
                (_now(), _now(), record_id),
            )
        return "applied", None

    payload = change.get("payload") or {}
    if not payload:
        raise ValueError("变更缺少 payload")

    if existing is None:
        _insert_from_cloud(db, record_id, payload)
        return "applied", _vector_job(record_id, change, payload)

    if not _cloud_is_newer(cloud_updated, existing["updated_at"]):
        return "stale", None

    assignments = [f"{column}=?" for column in SYNCED_COLUMNS]
    values = [synced_value(column, payload) for column in SYNCED_COLUMNS]
    db.execute(
        f"UPDATE memory_records SET {', '.join(assignments)}, updated_at=? WHERE id=?",
        (*values, change.get("updated_at") or _now(), record_id),
    )
    return "applied", _vector_job(record_id, change, payload)


def _cloud_is_newer(cloud_updated: datetime | None, local_updated: Any) -> bool:
    """云端版本是否比本地新。云端没给时间戳时按「不覆盖本地」处理。"""
    if cloud_updated is None:
        return False
    local = _parse_moment(local_updated)
    if local is None:
        return True
    return cloud_updated >= local


def _insert_from_cloud(db: sqlite3.Connection, record_id: str, payload: dict) -> None:
    columns = ["id", *SYNCED_COLUMNS, "owner_id", "channel", "conversation_id", "module_id",
               "created_at", "updated_at", "embedding_status"]
    values = [
        record_id,
        *[synced_value(column, payload) for column in SYNCED_COLUMNS],
        payload.get("owner_id", "local-user"),
        payload.get("channel", "web"),
        payload.get("conversation_id", "default"),
        payload.get("module_id", "companion"),
        payload.get("created_at") or _now(),
        payload.get("updated_at") or _now(),
        "pending",
    ]
    placeholders = ",".join("?" for _ in columns)
    db.execute(
        f"INSERT INTO memory_records({','.join(columns)}) VALUES({placeholders})",
        values,
    )


def _vector_job(record_id: str, change: dict, payload: dict) -> tuple | None:
    """攒出向量安装所需的信息；没有向量就返回 None。

    ``payload_for`` 要的是完整的记录行（含 ``id``），而云端给的 payload 里没有 id
    （id 单独放在变更的 record_id 上），所以这里补进去再交出去。
    """
    vector = change.get("vector")
    layer = str(change.get("vector_layer") or payload.get("layer") or "")
    if not vector or not layer:
        return None
    return record_id, list(vector), layer, {**payload, "id": record_id}


def _install_vector(
    repositories: MemoryRepositories,
    record_id: str,
    vector: list[float],
    layer: str,
    record: dict,
) -> None:
    """装上云端回传的检索向量，装上之后才把状态标成可用。

    没有向量时这条路根本不会走到：记录留在 ``embedding_status='pending'``，
    本地的向量补偿会拿文字重新算一遍。检索功能不因此中断 ——
    文字检索这条路本来就不依赖向量，这也是「向量没装好也能用」的由来。
    """
    repositories.vector_client.upsert(
        f"{layer}_memories_vectors",
        [models.PointStruct(id=record_id, vector=vector, payload=repositories.payload_for(record))],
    )
    with repositories.connect() as db:
        db.execute(
            "UPDATE memory_records SET embedding_status='ready',embedding_layer=?,"
            "vector_payload_version=?,retry_count=0,next_retry_at=NULL WHERE id=?",
            (layer, VECTOR_PAYLOAD_VERSION, record_id),
        )


class MemorySyncRunner:
    """后台同步循环：先上传、再拉取，一轮不重叠。

    跑在 web_api 的事件循环里（不是独立进程）：本地向量库是嵌入式模式、
    单进程独占目录，另起一个进程会直接抢不到 ``data/memory_vectors``。

    所有 SQLite 操作走 ``asyncio.to_thread``，否则一次全表写就能把
    SSE 推送和聊天响应一起卡住。
    """

    def __init__(
        self,
        repositories: MemoryRepositories,
        store: MemorySyncStore,
        client: CloudMemoryClient,
        *,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        shutdown_grace_seconds: float = DEFAULT_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        self.repositories = repositories
        self.store = store
        self.client = client
        self.interval_seconds = interval_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.last_result: dict = {}

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # 启动后立刻同步一次：上次强退留下的积压不等下一个十分钟。
        await self._guarded_sync()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
            await self._guarded_sync()

    async def _guarded_sync(self) -> None:
        if self._lock.locked():
            LOGGER.info("上一轮同步还没跑完，跳过这一轮")
            return
        async with self._lock:
            try:
                self.last_result = await self.sync_once()
            except Exception as exc:  # noqa: BLE001 - 同步失败不能拖垮进程
                LOGGER.warning("同步失败：%s", type(exc).__name__)
                self.last_result = {"error": type(exc).__name__}

    async def flush_for_shutdown(self) -> None:
        """退出前补传一次，最多等 N 秒。超时就放弃，未传内容留在队列里下次补。"""
        try:
            await asyncio.wait_for(self._push_only(), timeout=self.shutdown_grace_seconds)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            LOGGER.info("退出补传未完成（%s），未传内容保留在队列", type(exc).__name__)

    async def sync_once(self) -> dict:
        """一轮同步：先上传，再拉取。

        两段各自兜自己的错。上传成功、拉取失败（或者反过来）是很常见的
        —— 云端重启、网络抖一下 —— 这时候把另一段的成果一起报成失败，
        排查时会看到"明明传上去了却说同步失败"，反而更难查。
        """
        pushed = await self._push_only()
        pulled = 0
        pull_error = ""
        try:
            pulled = await self._pull_changes()
        except Exception as exc:  # noqa: BLE001 - 拉取失败不该抹掉上传的结果
            pull_error = type(exc).__name__
            LOGGER.warning("拉取云端变更失败：%s", pull_error)
        if not pull_error:
            # 只有整轮都成了才记"上次同步时间"。云端挂着的时候这个字段
            # 应该显出"很久没同步了"，而不是每次失败的尝试都把它刷新一遍。
            await asyncio.to_thread(self.store.set_state, STATE_LAST_SYNC_AT, _now())
        cursor = await asyncio.to_thread(self.store.change_cursor)
        result = {"pushed": pushed, "pulled": pulled, "cursor": cursor}
        if pull_error:
            result["pull_error"] = pull_error
        return result

    async def _push_only(self) -> dict:
        turns_ok, turns_failed = await self._push_turns()
        access_ok = await self._push_access()
        return {"turns_sent": turns_ok, "turns_failed": turns_failed, "access_sent": access_ok}

    async def _push_turns(self) -> tuple[int, int]:
        pending = await asyncio.to_thread(self.store.pending_turns)
        if not pending:
            return 0, 0
        payload = [
            {
                "turn_id": row["turn_id"],
                "owner_id": row["owner_id"],
                "channel": row["channel"],
                "conversation_id": row["conversation_id"],
                "module_id": row["module_id"],
                "user_text": row["user_text"],
                "assistant_reply": row["assistant_reply"],
                "occurred_at": row["occurred_at"],
                # 处理包随原始对话一起上传：少了它，云端只能看到一问一答，
                # 多轮里形成的决策过程就到不了那一端。
                "packet": json.loads(row["packet"]) if row.get("packet") else None,
                "schema_version": int(row.get("schema_version") or 0),
            }
            for row in pending
        ]
        try:
            response = await asyncio.to_thread(self.client.push_turns, payload)
        except Exception as exc:  # noqa: BLE001 - 整批失败就整批留在队列
            for row in pending:
                await asyncio.to_thread(self.store.mark_turn_failed, row["turn_id"], str(exc))
            LOGGER.warning("上传对话失败：%s", type(exc).__name__)
            return 0, len(pending)

        accepted = set(response.get("accepted") or [])
        rejected = {item.get("turn_id"): item.get("reason", "") for item in response.get("rejected") or []}
        ok = failed = 0
        for row in pending:
            turn_id = row["turn_id"]
            if turn_id in accepted:
                # 只有云端确认**已经持久保存**了才出队。
                await asyncio.to_thread(self.store.mark_turn_sent, turn_id)
                ok += 1
            else:
                await asyncio.to_thread(
                    self.store.mark_turn_failed, turn_id, rejected.get(turn_id, "云端未确认")
                )
                failed += 1
        return ok, failed

    async def _push_access(self) -> int:
        # 先把本地新的访问收进队列，再一起上传。顺序反了的话这一轮用到的记忆
        # 要等下一个十分钟才报给云端，「多久没用过」永远慢一拍。
        await asyncio.to_thread(collect_access, self.repositories, self.store)
        pending = await asyncio.to_thread(self.store.pending_access)
        if not pending:
            return 0
        payload = [
            {
                "record_id": row["record_id"],
                "occurred_at": row["accessed_at"],
                "owner_id": row["owner_id"],
                "channel": row["channel"],
                "conversation_id": row["conversation_id"],
                "module_id": row["module_id"],
            }
            for row in pending
        ]
        try:
            await asyncio.to_thread(self.client.push_access, payload)
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("上传访问记录失败：%s", type(exc).__name__)
            return 0
        keys = [(row["record_id"], row["minute_bucket"]) for row in pending]
        await asyncio.to_thread(self.store.mark_access_sent, keys)
        return len(keys)

    async def _pull_changes(self) -> int:
        baseline_at = await asyncio.to_thread(self.store.get_state, STATE_BASELINE_AT, "")
        if not baseline_at:
            # 首次接入：变更流水里只有「装好这套系统之后」发生的改动，
            # 之前就存在的记录一条都不会出现在流水里 —— 光看流水，那些记录
            # 永远不会到本地，而且不报错。所以第一次必须用完整快照对齐。
            snapshot = await asyncio.to_thread(self.client.fetch_snapshot)
            result = await asyncio.to_thread(apply_snapshot, self.repositories, self.store, snapshot)
            LOGGER.info(
                "首次接入：用快照建立基线，云端 %d 条，应用 %d 跳过 %d",
                int(snapshot.get("count") or 0),
                result["applied"],
                result["skipped"],
            )
            if result["errors"]:
                LOGGER.warning("快照合并存在失败：%s", result["errors"][:3])
            return result["applied"]
        since = await asyncio.to_thread(self.store.change_cursor)
        response = await asyncio.to_thread(self.client.pull_changes, since)
        changes = response.get("changes") or []
        if not changes:
            return 0
        result = await asyncio.to_thread(apply_changes, self.repositories, self.store, changes)
        highest = max(int(item.get("seq") or 0) for item in changes)
        if result["errors"]:
            # 有失败就不推进到最高编号之外 —— 下次重放同一段，幂等。
            LOGGER.warning("合并变更存在失败：%s", result["errors"][:3])
        await asyncio.to_thread(self.store.set_state, STATE_CHANGE_CURSOR, str(highest))
        return result["applied"]


def apply_snapshot(
    repositories: MemoryRepositories,
    store: MemorySyncStore,
    snapshot: dict,
) -> dict:
    """首次接入/水位对不上时用完整快照对齐。

    快照本身就是一串 upsert 变更（含编号），所以走的还是同一套合并逻辑 ——
    不用为「首次」单独写一条容易和日常路径分叉的代码。
    本地较新的记录不会被覆盖，本地独有的记录也不会被删掉。
    """
    changes = snapshot.get("changes") or []
    expected = str(snapshot.get("checksum") or "")
    if expected and _snapshot_checksum(changes) != expected:
        raise ValueError("快照校验失败，已中止合并")
    result = apply_changes(repositories, store, changes)
    cursor = int(snapshot.get("cursor") or 0)
    store.set_state(STATE_CHANGE_CURSOR, str(cursor))
    store.set_state(STATE_BASELINE_AT, _now())
    return result


def _snapshot_checksum(changes: list[dict]) -> str:
    import hashlib

    body = json.dumps(changes, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_runner_from_env(
    repositories: MemoryRepositories,
    store: MemorySyncStore,
    *,
    env: dict | None = None,
) -> MemorySyncRunner | None:
    """从环境变量装配同步器；没配 URL/口令时返回 None（不启用同步）。"""
    source = env if env is not None else os.environ
    url = str(source.get("CLOUD_MEMORY_URL", "")).strip()
    token = str(source.get("CLOUD_MEMORY_TOKEN", "") or source.get("CLOUD_INGEST_TOKEN", "")).strip()
    if not url or not token:
        return None
    try:
        interval = int(source.get("MEMORY_SYNC_INTERVAL_SECONDS", str(DEFAULT_INTERVAL_SECONDS)) or DEFAULT_INTERVAL_SECONDS)
    except ValueError:
        interval = DEFAULT_INTERVAL_SECONDS
    try:
        grace = float(source.get("MEMORY_SYNC_SHUTDOWN_GRACE_SECONDS", str(DEFAULT_SHUTDOWN_GRACE_SECONDS)))
    except ValueError:
        grace = DEFAULT_SHUTDOWN_GRACE_SECONDS
    return MemorySyncRunner(
        repositories,
        store,
        CloudMemoryClient(url, token),
        interval_seconds=max(30, interval),
        shutdown_grace_seconds=max(0.0, grace),
    )


def rebuild_scope(payload: dict) -> Scope:
    return Scope(
        owner_id=str(payload.get("owner_id") or "local-user"),
        channel=str(payload.get("channel") or "web"),
        conversation_id=str(payload.get("conversation_id") or "default"),
        module_id=str(payload.get("module_id") or "companion"),
    )


def collect_access(repositories: MemoryRepositories, store: MemorySyncStore, *, limit: int = 500) -> int:
    """把本地「最近被用到过」的记录收进待上传队列。

    直接从 ``last_accessed_at`` 读，而不是去检索层插回调：检索那条路本来就
    会写这个字段，另加一个回调等于把同一件事说两遍，而且以后新增任何一条
    读取路径都会漏掉通知 —— 读字段则天然覆盖全部路径。

    水位用 ``>=`` 而不是 ``>``：同一毫秒内被用到的两条记录，用 ``>`` 会让
    后一条永远轮不到。重复扫描是安全的，出队那一侧按「记录 + 分钟」去重。
    """
    watermark = store.get_state(STATE_ACCESS_WATERMARK, "")
    with repositories.connect() as db:
        rows = db.execute(
            "SELECT id,last_accessed_at,owner_id,channel,conversation_id,module_id "
            "FROM memory_records WHERE last_accessed_at IS NOT NULL AND last_accessed_at>=? "
            "ORDER BY last_accessed_at LIMIT ?",
            (watermark, int(limit)),
        ).fetchall()
    if not rows:
        return 0
    # 按「作用域 + 分钟」分组：入队时的去重键是这两者，分组键必须和它一致。
    # 一组里所有记录的实际访问时间才能原样带下去 —— 全用"现在"的话，
    # 同一轮同步到的访问会挤进同一个分钟桶，真实使用时间就被抹掉了，
    # 云端"多久没用过"的判断跟着失真。
    groups: dict[tuple[tuple[str, str, str, str], str], tuple[str, list[str]]] = {}
    highest = watermark
    for row in rows:
        moment = str(row["last_accessed_at"])
        scope_key = (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
        bucket = moment[:16]
        entry = groups.setdefault((scope_key, bucket), (moment, []))
        entry[1].append(str(row["id"]))
        highest = max(highest, moment)
    written = 0
    for (scope_key, _bucket), (moment, record_ids) in groups.items():
        scope = rebuild_scope(dict(zip(("owner_id", "channel", "conversation_id", "module_id"), scope_key)))
        written += store.record_access(scope, record_ids, accessed_at=moment)
    store.set_state(STATE_ACCESS_WATERMARK, highest)
    return written
