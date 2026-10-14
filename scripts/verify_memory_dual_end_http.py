"""双端走**真实 HTTP** 的闭环验收：本地上传 → 云端 API 落库 → 云端工人处理 → 本端拉回。

【为什么单独一条】
先前的双端对照把同一个包分别送进两端的工人，但没有走 API 那一跳：上传要经鉴权
落进云端队列，变更要经 API 回传、由本端同步器合并。这一跳不通，两端的版本关系
就传不过去 —— 本端会一直停在旧版本上。

【覆盖的四段】
1. 上传：本端把回合放进待上传队列，经 HTTP 上传，云端确认「已持久保存」；
2. 处理：云端工人从接收队列取走，落成新语义记录，并写变更流水；
3. 回传：本端同步器拉变更、合并进本端记录库；
4. 纠正：第二轮把「不是五千，是三千」走同一条路 —— 本端最终现行版本是三千，
   旧的五千归档为已替代并指向新版本，本端检索注入的也是三千。

【边界】
- 云端 API 起在临时工作目录，`MEMORY_DB_PATH` 指向临时库；本端同样是临时库；
- 不做真实模型调用：云端工人在本进程里跑，提炼结果由固定替身给出；
- 生产数据、生产服务、正式配置一个不碰。

退出码 0 = 四段都拿到证据；1 = 有检查没过；3 = 云端 API 没起来。
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_sync import CloudMemoryClient, MemorySyncRunner
from agent_core.memory_sync_store import MemorySyncStore
from agent_core.memory_turn import (
    MemoryCandidate,
    MemoryUpdateSuggestion,
    turn_from_messages,
)
from agent_core.memory_writes import MemoryWriter
from cloud_memory.store import CloudMemoryStore
from cloud_memory.worker import ChangeRecorder, CloudMemoryWorker

TOKEN = "http-accept-token"
OWNER = "http-accept-user"
CONVERSATION = "http-conv"


class FixedReviewProvider:
    """Independently authored answers; unknown packets never gain approval."""
    model = "http-independent-review-fixtures"

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        turn = payload["packet"]["turn_id"]
        fixtures = {
            "http-turn-1": ("项目预算上限五千", ["记住：项目预算上限五千"]),
            "http-turn-2": ("项目预算上限三千", ["不是五千", "是三千"]),
        }
        rows = []
        for entry in payload["items"]:
            if turn not in fixtures:
                rows.append({"index":entry["index"],"verdict":"uncertain","reason":"独立核验未提供此回合预期"})
                continue
            content, quotes = fixtures[turn]
            rows.append(dict(index=entry["index"], verdict="supported", content=content,
                subject="项目预算上限", reason="原话明确预算数值，第二轮引用唯一旧预算纠正数值",
                same_subject=True, preserves_negation=True, preserves_conditions=True,
                change_authorized=turn == "http-turn-2", lifetime={"mode":"long_term","basis":"预算事实没有期限"},
                quotes=[{"message_id":turn,"quote":quote} for quote in quotes]))
        return SimpleNamespace(message={"content":json.dumps({"items":rows},ensure_ascii=False)},usage={})


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class ScriptedExtractor:
    """确定性提炼替身：第一轮出新候选，第二轮出针对该记录的纠正建议。"""

    def __init__(self) -> None:
        self.calls = 0
        self.provider = FixedReviewProvider()

    async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
        self.calls += 1
        if "三千" in packet.user_text:
            refs = list(packet.recalled_memory_refs)
            if not refs:
                return [], []
            ref = refs[0]
            return [], [
                MemoryUpdateSuggestion(
                    action="correct",
                    target_id=ref.memory_id,
                    expected_version=int(ref.version),
                    content="项目预算上限三千",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=(packet.turn_id,),
                    reason="用户纠正了预算数字",
                )
            ]
        return [
            MemoryCandidate(
                content="项目预算上限五千",
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(packet.turn_id,),
                scope_kind="conversation",
                occurred_at=packet.occurred_at,
            )
        ], []


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def build_api_env(work: Path, port: int, cloud_db: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "PYTHONIOENCODING": "utf-8",
            "MEMORY_DB_PATH": str(cloud_db),
            "CLOUD_MEMORY_TOKENS": f"{TOKEN}:{OWNER}",
            "WEB_PORT": str(port),
            "TEMP": str(work),
            "TMP": str(work),
            "TMPDIR": str(work),
        }
    )
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        env.pop(name, None)
    return env


def wait_ready(port: int, *, seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=4.0) as response:
                if response.status < 500:
                    return True
        except Exception:  # noqa: BLE001 - 还没起来，继续等
            time.sleep(0.5)
    return False


def stop_api(process: subprocess.Popen, log) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    log.flush()


def records_in(repositories, scope: Scope) -> list[dict]:
    with repositories.connect() as db:
        rows = db.execute(
            "SELECT id,content,status,content_status,record_version,effective_record_id,metadata "
            "FROM memory_records WHERE owner_id=? ORDER BY created_at",
            (scope.owner_id,),
        ).fetchall()
    return [dict(row) for row in rows]


async def run(*, output_path=None) -> int:
    watched = ("MEMORY_SEMANTIC_ENABLED", "MEMORY_PROCESSING_ENDPOINT")
    previous = {name: os.environ.get(name) for name in watched}
    try:
        return await _run(output_path=output_path)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


async def _run(*, output_path=None) -> int:
    checks: list[dict] = []

    def check(name: str, passed: bool, actual) -> None:
        checks.append({"check": name, "passed": bool(passed), "actual": actual})

    # 这两项只影响本进程里的云端工人：语义路径打开、处理端指定云端。
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    os.environ["MEMORY_PROCESSING_ENDPOINT"] = "cloud"

    with tempfile.TemporaryDirectory(prefix="dual-http-", ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        cloud_db = base / "cloud" / "memory_records.db"
        cloud_db.parent.mkdir(parents=True, exist_ok=True)
        local_db = base / "local" / "agent_core.db"
        local_db.parent.mkdir(parents=True, exist_ok=True)
        port = free_port()
        log = (base / "api.log").open("w", encoding="utf-8")
        api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "cloud_memory.api:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(base),
            env=build_api_env(base, port, cloud_db),
            stdout=log,
            stderr=subprocess.STDOUT,
        )

        cloud_store = CloudMemoryStore(cloud_db)
        cloud_store.initialize()
        cloud_repos = initialize_memory_repositories(
            cloud_db, base / "cloud" / "vectors", vector_size=3
        )
        extractor = ScriptedExtractor()
        cloud_writer = MemoryWriter(
            cloud_repos, Embeddings(), on_change=ChangeRecorder(cloud_repos, cloud_store)
        )
        cloud_worker = CloudMemoryWorker(
            cloud_repos,
            cloud_store,
            cloud_writer,
            None,
            semantic_extractor=extractor,
            maintenance=MemoryMaintenanceService(cloud_writer, cloud_repos),
            interval_seconds=0,
            batch_size=10,
        )

        local_repos = initialize_memory_repositories(
            local_db, base / "local" / "vectors", vector_size=3
        )
        sync_store = MemorySyncStore(local_db)
        sync_store.initialize()
        client = CloudMemoryClient(f"http://127.0.0.1:{port}", TOKEN)
        runner = MemorySyncRunner(local_repos, sync_store, client)
        retriever = MemoryRetriever(local_repos, Embeddings())
        scope = Scope(OWNER, "web", CONVERSATION, "companion")
        now = datetime.now(UTC).isoformat(timespec="milliseconds")

        try:
            if not wait_ready(port):
                print(json.dumps({"ok": False, "reason": "云端 API 没起来", "work": str(base)}, ensure_ascii=False))
                return 3

            # ---- 第 1 轮：本地上传 → 云端处理 → 本端拉回 -----------------
            packet1 = turn_from_messages(
                turn_id="http-turn-1",
                occurred_at=now,
                scope=scope,
                user_text="记住：项目预算上限五千",
                published_reply="好，记下了。",
            )
            sync_store.enqueue_turn(
                scope,
                turn_id=packet1.turn_id,
                user_text=packet1.user_text,
                assistant_reply=packet1.published_reply,
                occurred_at=packet1.occurred_at,
                packet=packet1.as_payload(),
            )
            pushed = await runner.sync_once()
            cloud_queue = cloud_store.queue_counts()
            check(
                "上传：回合经 HTTP 被云端接收并落库",
                pushed["pushed"]["turns_sent"] == 1
                and pushed["pushed"]["turns_failed"] == 0
                and cloud_queue.get("stored", 0) + cloud_queue.get("pending", 0) == 1,
                {"pushed": pushed, "cloud_queue": cloud_queue},
            )

            await cloud_worker.run_once()
            cloud_rows = records_in(cloud_repos, scope)
            check(
                "处理：云端工人把这一轮落成新语义记录",
                len(cloud_rows) == 1 and cloud_rows[0]["content_status"] == "reported",
                {"records": [(row["content"], row["content_status"], row["record_version"]) for row in cloud_rows]},
            )
            with cloud_store.connect() as db:
                first_review = db.execute("SELECT review_results FROM memory_ingest_queue WHERE turn_id=?", (packet1.turn_id,)).fetchone()[0]
            check("审查：第一轮已持久保存正式批量核验结果", bool(first_review) and json.loads(first_review).get("completed") is True,
                  {"persisted": bool(first_review)})

            pulled = await runner.sync_once()
            local_rows = records_in(local_repos, scope)
            check(
                "回传：本端拉到云端记录并写进本端库",
                pulled["pulled"] >= 1 and len(local_rows) == 1 and local_rows[0]["content"] == "项目预算上限五千",
                {"pulled": pulled, "local": [(row["content"], row["status"], row["record_version"]) for row in local_rows]},
            )

            # ---- 第 2 轮：纠正走同一条路 ---------------------------------
            target = local_rows[0]
            packet2 = turn_from_messages(
                turn_id="http-turn-2",
                occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                scope=scope,
                user_text="不是五千，是三千",
                published_reply="好，改成三千。",
                # 本端把自己的现行版本作为引用传给云端：目标版本由程序带，不让模型猜。
                recalled_memory_refs=[
                    {
                        "memory_id": target["id"],
                        "kind": "fact",
                        "content_status": "reported",
                        "excerpt": target["content"],
                        "version": int(target["record_version"]),
                    }
                ],
            )
            sync_store.enqueue_turn(
                scope,
                turn_id=packet2.turn_id,
                user_text=packet2.user_text,
                assistant_reply=packet2.published_reply,
                occurred_at=packet2.occurred_at,
                packet=packet2.as_payload(),
                schema_version=1,
            )
            await runner.sync_once()
            second_push = await runner.sync_once()
            cloud_queue_after = cloud_store.queue_counts()
            # The review is persisted before a simulated commit outage; recovery
            # must reuse it, not silently re-extract or obtain a new verdict.
            with patch.object(cloud_writer, "apply_semantic_update", side_effect=sqlite3.OperationalError("isolated commit outage")):
                await cloud_worker.run_once()
            with cloud_store.connect() as db:
                failed = dict(db.execute("SELECT * FROM memory_ingest_queue WHERE turn_id=?", (packet2.turn_id,)).fetchone())
            check("恢复：存储失败保留建议和已持久核验结果", failed["status"] == "failed" and bool(failed["review_results"])
                  and len(cloud_store.stored_extraction(failed)["suggestions"]) == 1,
                  {"status":failed["status"], "review_persisted":bool(failed["review_results"])})
            before_retry = (extractor.calls, extractor.provider.calls)
            with cloud_store.connect() as db:
                db.execute("UPDATE memory_ingest_queue SET next_retry_at=NULL WHERE turn_id=?", (packet2.turn_id,))
            await cloud_worker.run_once()
            check("恢复：重试复用提炼和核验结果", (extractor.calls, extractor.provider.calls) == before_retry,
                  {"before":before_retry,"after":(extractor.calls,extractor.provider.calls)})
            cloud_after = records_in(cloud_repos, scope)
            cloud_current = [row for row in cloud_after if row["status"] == "active"]
            check(
                "纠正：云端把三千落成新的现行版本",
                len(cloud_current) == 1
                and cloud_current[0]["content"] == "项目预算上限三千"
                and int(cloud_current[0]["record_version"]) == 2,
                {
                    "cloud": [
                        (row["content"], row["status"], row["content_status"], row["record_version"])
                        for row in cloud_after
                    ],
                    "push": second_push,
                    "cloud_queue": cloud_queue_after,
                    "worker": {key: value for key, value in cloud_worker.stats.items() if key != "duration_ms"},
                },
            )

            final_pull = await runner.sync_once()
            final_rows = records_in(local_repos, scope)
            by_content = {row["content"]: row for row in final_rows}
            new_row = by_content.get("项目预算上限三千")
            old_row = by_content.get("项目预算上限五千")
            check(
                "回传：本端现行版本变成三千",
                final_pull["pulled"] >= 1
                and new_row is not None
                and new_row["status"] == "active"
                and int(new_row["record_version"]) == 2,
                {"pulled": final_pull, "local": [(row["content"], row["status"], row["record_version"]) for row in final_rows]},
            )
            check(
                "关系：旧的五千在本端归档为已替代并指向新版本",
                old_row is not None
                and old_row["status"] == "archived"
                and old_row["content_status"] == "superseded"
                and str(old_row["effective_record_id"] or "") == str(new_row["id"]),
                {"old": dict(old_row or {}).get("content_status"), "points_to": str((old_row or {}).get("effective_record_id") or "")},
            )

            context = retriever.build_context(scope, "项目预算上限")
            injected = context.injected_text
            recalled = retriever.retrieve(scope, "项目预算上限")
            current_recalled = [row["content"] for row in recalled.warm if row.get("status") == "active"]
            check(
                "召回：本端拿到的现行结论是三千，不再把五千当现行",
                current_recalled == ["项目预算上限三千"] and "[fact/reported] 项目预算上限三千" in injected,
                {"current_contents": current_recalled, "injected": injected[:200],
                 "note": "核验实际现行内容；原话依据中的不是五千属于纠正证据"},
            )

            # ---- 鉴权：口令不对必须被拒 ---------------------------------
            try:
                CloudMemoryClient(f"http://127.0.0.1:{port}", "wrong-token").pull_changes(0)
                rejected = False
            except Exception as exc:  # noqa: BLE001 - 拒了才对
                rejected = "401" in str(exc) or "Unauthorized" in str(exc) or "HTTP Error" in str(exc)
            check("鉴权：口令不对的拉取被拒", rejected, {"rejected": rejected})

            before_replay = records_in(local_repos, scope)
            await runner.sync_once()
            await cloud_worker.run_once()
            await runner.sync_once()
            check("防重复：再次同步和处理不新增版本", records_in(local_repos, scope) == before_replay
                  and records_in(cloud_repos, scope) == cloud_after,
                  {"local_records":len(records_in(local_repos,scope)),"cloud_records":len(records_in(cloud_repos,scope))})

            report = {
                "ok": all(item["passed"] for item in checks),
                "passed": sum(item["passed"] for item in checks),
                "total": len(checks),
                "checks": checks,
                "extractor_calls": extractor.calls,
                "review_fixture_calls": extractor.provider.calls,
                "production_memory_touched": False,
                "live_model_calls": 0,
                "limits": [
                    "云端 API 与两端库都在临时目录；不碰生产服务与生产数据",
                    "提炼用确定性替身，不调真实模型：验的是两端通路与版本关系，不是模型质量",
                ],
            }
            target_path = Path(output_path) if output_path else ROOT / ".pytmp" / "evidence" / "memory-dual-end-http-evidence.json"
            payload = json.dumps(report, ensure_ascii=False, indent=2)
            # 目录可能还不存在（新机器上第一次跑）：先建，别在全部检查通过之后
            # 因为写不进证据文件而报失败 —— 那会把「脚本跑通了」变成「验收没过」。
            # 系统权限拒绝写这个路径时退回可写目录，并在报告里如实记一笔。
            try:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(payload, encoding="utf-8")
            except OSError as exc:
                fallback = ROOT / ".pytmp" / "evidence" / target_path.name
                fallback.parent.mkdir(parents=True, exist_ok=True)
                fallback.write_text(payload, encoding="utf-8")
                report["evidence_write_fallback"] = {
                    "requested": str(target_path),
                    "written_to": str(fallback),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                payload = json.dumps(report, ensure_ascii=False, indent=2)
            print(payload)
            return 0 if report["ok"] else 1
        finally:
            stop_api(api, log)
            log.close()
            cloud_repos.close()
            local_repos.close()
            gc.collect()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(run()))
