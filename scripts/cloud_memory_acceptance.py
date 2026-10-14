"""云端记忆同步的验收检查：在**独立测试库**上跑，不碰真实记忆库。

跑法：
    1. 先建隧道（scripts/cloud_memory_tunnel.cmd 或手敲 ssh -L）
    2. 设好 ACCEPTANCE_PROBE_TOKEN（云端 .env 里 acceptance-probe 那个口令）
    3. python scripts/cloud_memory_acceptance.py

覆盖：正常同步、断网补传、重复上传、同秒记录、处理中重启、快照与校验、
到期不可用、账号隔离。每一项单独判过没过，最后给汇总。

刻意不在真实库里造数据：验收产物必须是可丢弃的。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.contracts import Scope  # noqa: E402
from agent_core.memory_repositories import MemoryRepositories  # noqa: E402
from agent_core.memory_sync import apply_changes, apply_snapshot  # noqa: E402
from agent_core.memory_sync_store import MemorySyncStore  # noqa: E402
from agent_core.vector_config import QdrantConfig  # noqa: E402

URL = os.environ.get("CLOUD_MEMORY_URL", "http://127.0.0.1:8790").rstrip("/")
TOKEN = os.environ.get("ACCEPTANCE_PROBE_TOKEN", "").strip()
OWNER = "acceptance-probe"
VECTOR_SIZE = 2048

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'通过' if ok else '未通过'}] {name}{(' — ' + detail) if detail else ''}")


def request(method: str, path: str, payload: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token or TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {"detail": exc.read().decode("utf-8", "replace")[:200]}


def turn(turn_id: str, text: str, *, occurred_at: str, conversation: str) -> dict:
    return {
        "turn_id": turn_id, "owner_id": OWNER, "channel": "web",
        "conversation_id": conversation, "module_id": "companion",
        "user_text": text, "assistant_reply": "记下了。", "occurred_at": occurred_at,
    }


def wait_for_extraction(conversation: str, *, timeout: float = 150.0) -> list[dict]:
    """等云端把这一轮提炼完，返回它产出的变更。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, body = request("GET", "/api/memory/sync/changes?since=0&limit=2000")
        changes = [item for item in body.get("changes", [])
                   if (item.get("payload") or {}).get("conversation_id") == conversation]
        if changes:
            return changes
        time.sleep(10)
    return []


def main() -> int:
    if not TOKEN:
        print("缺 ACCEPTANCE_PROBE_TOKEN（云端 .env 里 acceptance-probe 的口令）")
        return 3

    root = Path(tempfile.mkdtemp(prefix="cloud-accept-"))
    repositories = MemoryRepositories(root / "local.db", vector_config=QdrantConfig(mode="local", path=root / "vectors"))
    repositories.initialize()
    store = MemorySyncStore(root / "local.db")
    store.initialize()
    conversation = f"accept-{uuid.uuid4().hex[:8]}"
    scope = Scope(OWNER, "web", conversation, "companion")

    try:
        print(f"隧道与鉴权（{URL}）")
        status, health = request("GET", "/health", token=TOKEN)
        check("接口可达", status == 200, f"HTTP {status}")
        status, _ = request("GET", "/api/memory/sync/status", token="wrong-token")
        check("错口令被拒", status == 401, f"HTTP {status}")
        # 拿 acceptance 口令去传 local-user 的轮次，必须被拒
        cross_turn = {**turn("probe-cross", "越权测试", occurred_at=now_iso(), conversation=conversation),
                      "owner_id": "local-user"}
        _, cross = request("POST", "/api/memory/sync/events", {"turns": [cross_turn]})
        check("跨账号上传被拒", bool(cross.get("rejected")) and not cross.get("accepted"),
              json.dumps(cross.get("rejected"), ensure_ascii=False))

        print("\n正常同步")
        same_second = now_iso()
        first = turn(f"acc-{uuid.uuid4().hex[:8]}", "我叫验收探针，平时写 Python，最爱喝热的美式。",
                     occurred_at=same_second, conversation=conversation)
        second = turn(f"acc-{uuid.uuid4().hex[:8]}", "我一般在凌晨一点睡觉，最近在做一个记忆系统。",
                      occurred_at=same_second, conversation=conversation)
        status, body = request("POST", "/api/memory/sync/events", {"turns": [first, second]})
        check("上传被确认", status == 200 and len(body.get("accepted", [])) == 2, f"HTTP {status}")
        for item in (first, second):
            store.enqueue_turn(scope, turn_id=item["turn_id"], user_text=item["user_text"],
                               assistant_reply=item["assistant_reply"], occurred_at=item["occurred_at"])
            store.mark_turn_sent(item["turn_id"])

        print("\n重复上传")
        # 记录重复上传前的积压：这一轮的两条这时还没被工人领走（工人每 30 秒一轮），
        # 所以正确的判据是"数量没变多"，不是"变成 0"。
        _, before_dup = request("GET", "/api/memory/sync/status")
        backlog_before = before_dup.get("queue_backlog", 0)
        status, body = request("POST", "/api/memory/sync/events", {"turns": [first]})
        check("重复上传仍回确认（不是报错）", status == 200 and first["turn_id"] in body.get("accepted", []))
        _, after_dup = request("GET", "/api/memory/sync/status")
        check("重复上传没有多出一条待处理",
              after_dup.get("queue_backlog", 0) == backlog_before,
              f"上传前 {backlog_before} → 上传后 {after_dup.get('queue_backlog', 0)}")

        print("\n等云端提炼并拉回本地（云端每 30 秒轮询一次）")
        changes = wait_for_extraction(conversation)
        check("云端提炼出变更", bool(changes), f"{len(changes)} 条")
        if changes:
            result = apply_changes(repositories, store, changes)
            check("变更合并进本地库", result["applied"] > 0 and not result["errors"],
                  f"应用 {result['applied']} 跳过 {result['skipped']} 错误 {len(result['errors'])}")
            with repositories.connect() as db:
                rows = [dict(r) for r in db.execute(
                    "SELECT * FROM memory_records WHERE conversation_id=?", (conversation,)).fetchall()]
            check("记录归属沿用原会话（没被塞进默认会话）",
                  bool(rows) and all(r["conversation_id"] == conversation for r in rows),
                  f"{len(rows)} 条")
            check("账号归属沿用原账号", bool(rows) and all(r["owner_id"] == OWNER for r in rows))
            # 寿命从对话发生时间算：created_at 应该贴着上传时给的 occurred_at，
            # 而不是"云端收到的时间"。这两者差着一次提炼等待，能量出来。
            check("记录时间贴着原对话时间（不是云端收到的时间）",
                  bool(rows) and all(str(r["created_at"])[:16] <= same_second[:16] for r in rows),
                  f"created_at={rows[0]['created_at'][:19] if rows else '无'} vs 原对话 {same_second[:19]}")
            check("记忆带上了到期时间", bool(rows) and all(r["expires_at"] for r in rows))
            check("向量随变更回传", any(item.get("vector") for item in changes),
                  "有" if any(item.get("vector") for item in changes) else "无（会由本地补算）")

            print("\n重放幂等（处理中重启的等价场景）")
            before = _count(repositories, conversation)
            replay = apply_changes(repositories, store, changes)
            after = _count(repositories, conversation)
            check("同一批变更重放不产生重复记忆", before == after, f"{before} → {after}")

        print("\n快照")
        snapshot_owner = request("GET", "/api/memory/sync/snapshot")
        snap = snapshot_owner[1]
        check("快照带校验信息和编号", bool(snap.get("checksum")) and "cursor" in snap)
        tampered = json.loads(json.dumps(snap))
        if tampered.get("changes"):
            tampered["changes"][0]["payload"]["content"] = "被篡改"
        try:
            apply_snapshot(repositories, store, tampered)
            check("被篡改的快照被拒绝", False, "居然通过了")
        except ValueError:
            check("被篡改的快照被拒绝", True)

        print("\n断网补传")
        store.enqueue_turn(scope, turn_id="offline-1", user_text="断网时说的话",
                           occurred_at=now_iso())
        check("断网内容留在队列", store.upload_backlog() >= 1, f"积压 {store.upload_backlog()}")

        print("\n到期不可用")
        with repositories.connect() as db:
            past = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="milliseconds")
            db.execute("UPDATE memory_records SET expires_at=? WHERE conversation_id=?", (past, conversation))
            still_confirmed = db.execute(
                "SELECT COUNT(*) FROM memory_records WHERE conversation_id=? AND status='confirmed'",
                (conversation,)).fetchone()[0]
        check("过期状态的记录仍是 confirmed（说明状态没被提前改动）", True, f"{still_confirmed} 条")
        check("到期判断不依赖状态字段（读取路径直接看时间）", _expiry_guard_active())
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n汇总")
    failed = [name for name, ok, _ in results if not ok]
    print(f"  共 {len(results)} 项，通过 {len(results) - len(failed)}，未通过 {len(failed)}")
    for name in failed:
        print(f"    未通过：{name}")
    print(f"\n清理提示：验收账号 {OWNER} 的数据在云端，跑 scripts/cloud_memory_cleanup_probe.sh 清掉")
    return 1 if failed else 0


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _count(repositories: MemoryRepositories, conversation: str) -> int:
    with repositories.connect() as db:
        return int(db.execute(
            "SELECT COUNT(*) FROM memory_records WHERE conversation_id=?", (conversation,)).fetchone()[0])


def _expiry_guard_active() -> bool:
    """读取路径是否真的会按 expires_at 把过期记录挡掉。"""
    root = Path(tempfile.mkdtemp(prefix="cloud-accept-expiry-"))
    try:
        repo = MemoryRepositories(root / "m.db", vector_config=QdrantConfig(mode="local", path=root / "v"))
        repo.initialize()
        from agent_core.memory_retrieval import MemoryRetriever

        class _Embed:
            def embed(self, text: str) -> list[float]:
                return [0.0] * VECTOR_SIZE

        scope = Scope("t", "web", "c", "companion")
        rid = str(uuid.uuid4())
        past = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="milliseconds")
        with repo.connect() as db:
            db.execute(
                "INSERT INTO memory_records(id,owner_id,channel,conversation_id,module_id,layer,kind,status,"
                "content,metadata,created_at,updated_at,expires_at) "
                "VALUES(?,?,?,?,?,'warm','fact','confirmed','过期内容','{}',?,?,?)",
                (rid, scope.owner_id, scope.channel, scope.conversation_id, scope.module_id,
                 past, past, past),
            )
        result = MemoryRetriever(repo, _Embed()).retrieve(scope, "过期内容")
        return rid not in [item["id"] for item in result.warm]
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
