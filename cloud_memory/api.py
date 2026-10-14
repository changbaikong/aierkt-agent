"""云端记忆系统的 HTTP 接口。四个端点：上传、拉变更、取快照、看状态。

【鉴权与账号范围】
口令决定「你是谁」，而不只是「你有没有资格进门」：
``CLOUD_MEMORY_TOKENS`` 写成 ``口令:账号`` 的逗号分隔列表，请求里的账号
必须和口令对应的账号一致，否则拒。

只做一个全局口令是不够的 —— 那样任何拿到口令的人都能读走、或者往别人的
账号里灌数据。这个库装的是长期记忆，越权读一次就是长期泄露。

【为什么上传要返回「已持久保存」才算成功】
本地收到确认才把内容移出队列。如果这里只是"收到就回 200"而没落库，
本地会把还没保存的对话当成已上传丢掉 —— 断一次电就永久少几轮记忆。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .store import CloudMemoryStore

LOGGER = logging.getLogger("cloud-memory.api")

BASE = Path(__file__).resolve().parents[1]
load_dotenv(BASE / ".env", override=False)

DB_PATH = os.environ.get("MEMORY_DB_PATH", str(BASE / "data" / "memory_records.db"))

app = FastAPI(title="cloud-memory-sync")
store = CloudMemoryStore(DB_PATH)


class TurnPayload(BaseModel):
    turn_id: str
    user_text: str = ""
    assistant_reply: str = ""
    owner_id: str = "local-user"
    channel: str = "web"
    conversation_id: str = "default"
    module_id: str = "companion"
    occurred_at: str = ""
    # 处理包必须原样收下：本地是**带着包**上传的（多轮里形成的决策过程、
    # 本轮已用过的记忆引用、可信项目绑定都在里面）。模型少这两个字段时
    # pydantic 会静默丢掉它们，云端就只能看到一问一答 —— 记忆更新建议
    # 没有可指向的旧记录，纠正与替代在云端整条失效，而且一声不响。
    packet: dict | None = None
    schema_version: int = 0


class AccessPayload(BaseModel):
    record_id: str
    occurred_at: str = ""
    owner_id: str = "local-user"


class EventsRequest(BaseModel):
    turns: list[TurnPayload] = Field(default_factory=list)
    access: list[AccessPayload] = Field(default_factory=list)


def _token_owners() -> dict[str, str]:
    """口令 → 账号。每次请求读一次环境变量，改配置不必重启。"""
    raw = os.environ.get("CLOUD_MEMORY_TOKENS", "").strip()
    mapping: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        token, _, owner = item.partition(":")
        if token:
            mapping[token] = owner.strip() or "local-user"
    if not mapping:
        # 退回到旧版单口令配置。没有它就等于没配鉴权，全部拒绝 ——
        # 宁可不服务，也不能让一个不设防的记忆库挂在网上。
        legacy = os.environ.get("CLOUD_INGEST_TOKEN", "").strip()
        if legacy:
            mapping[legacy] = "*"
    return mapping


def _authenticate(authorization: str) -> str:
    header = (authorization or "").strip()
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    owners = _token_owners()
    if not token or token not in owners:
        raise HTTPException(status_code=401, detail="口令无效")
    return owners[token]


def _scope_allows(token_owner: str, record_owner: str) -> bool:
    return token_owner == "*" or token_owner == record_owner


def _load_owner_records(owner_id: str) -> list[dict]:
    """读某个账号的全部记录。``*`` 口令（旧版单口令）才允许读全部。"""
    with store.connect() as db:
        if owner_id == "*":
            rows = db.execute("SELECT * FROM memory_records ORDER BY created_at").fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM memory_records WHERE owner_id=? ORDER BY created_at", (owner_id,)
            ).fetchall()
    return [dict(row) for row in rows]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "db": DB_PATH}


@app.post("/api/memory/sync/events")
def sync_events(payload: EventsRequest, authorization: str = Header(default="")) -> dict:
    token_owner = _authenticate(authorization)
    accepted: list[str] = []
    rejected: list[dict] = []
    for turn in payload.turns:
        if not _scope_allows(token_owner, turn.owner_id):
            rejected.append({"turn_id": turn.turn_id, "reason": "账号越权"})
            continue
        try:
            store.enqueue_turn(turn.model_dump())
        except Exception as exc:  # noqa: BLE001 - 单条失败要能指名道姓回给本地
            LOGGER.warning("入队失败：turn=%s 错误=%s", turn.turn_id, type(exc).__name__)
            rejected.append({"turn_id": turn.turn_id, "reason": f"入队失败：{type(exc).__name__}"})
            continue
        # 走到这里说明已经写进库了。重复上传也回 accepted：
        # 本地要的是"这条已经在云端了"，而不是"这次是新写的"。
        accepted.append(turn.turn_id)

    access_events = [item.model_dump() for item in payload.access if _scope_allows(token_owner, item.owner_id)]
    access_count = store.record_access(access_events) if access_events else 0
    return {"accepted": accepted, "rejected": rejected, "access_recorded": access_count}


@app.get("/api/memory/sync/changes")
def sync_changes(
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
    authorization: str = Header(default=""),
) -> dict:
    token_owner = _authenticate(authorization)
    changes = store.changes_since(since, owner_id=token_owner, limit=limit)
    cursor = changes[-1]["seq"] if changes else since
    return {
        "cursor": cursor,
        "max_seq": store.max_seq(owner_id=token_owner),
        "changes": changes,
    }


@app.get("/api/memory/sync/snapshot")
def sync_snapshot(authorization: str = Header(default="")) -> dict:
    token_owner = _authenticate(authorization)
    # 先取水位、再读记录：反过来的话，生成期间新写入的变更会被算进水位，
    # 但内容不在快照里，本地推完水位就永久漏掉它们。
    cursor = store.max_seq(owner_id=token_owner)
    records = _load_owner_records(token_owner)
    snapshot = store.snapshot(owner_id=token_owner, records=records, cursor=cursor)
    return snapshot


@app.get("/api/memory/sync/status")
def sync_status(authorization: str = Header(default="")) -> dict:
    token_owner = _authenticate(authorization)
    return store.status(owner_id=token_owner)
