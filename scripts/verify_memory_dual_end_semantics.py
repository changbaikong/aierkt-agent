"""双端处理语义对照：同一个包分别送进本地与云端**各自真实工人入口**，逐项比结果。

【为什么需要这一条】
「同一份代码在云端机器上跑得通」不等于「两端处理同一个包得出同样的语义」。
两端的装配方式不同（本地是持久队列 + 维护工人，云端是接收队列 + 云端工人），
差的地方正是写错记忆的地方。

【怎么保证公平与隔离】
- 两端各用**临时库**（记录库、向量集合、作业库全在临时目录里），生产数据一个不碰；
- 两端用**同一份处理包、同一条更新建议、同一条目标记录内容**，只是库不是同一个；
- 本地走 `MemoryMaintenanceWorker`（真实维护工人），云端走 `CloudMemoryWorker.run_once`
  （真实云端工人入口），不手工调用辅助模块；
- 不调真实模型：提炼结果由固定替身给出，比对的是**程序侧语义**：
  现行版本、旧版本归档与关系、来源、范围、版本号，以及拒绝与失败回执。

退出码 0 = 三项对照全部一致；1 = 有项目不一致。
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_queue import MemoryCandidateQueue, MemoryMaintenanceWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import (
    MemoryCandidate,
    MemoryUpdateSuggestion,
    turn_from_messages,
)
from agent_core.memory_writes import MemoryWriter
from cloud_memory.store import CloudMemoryStore
from cloud_memory.worker import CloudMemoryWorker

PROJECT_BINDING = "dual-end-project"
SCOPE = Scope("dual-user", "web", "dual-conv", "companion")
CONTENT_MARKERS = ("五千", "三千", "四千")

# Independently authored oracle: no production semantic checker computes these answers.
REVIEW_FIXTURES = {
    "dual-turn-1": ("项目采购预算三千", "项目采购预算", "项目采购预算不是五千，是三千"),
    "dual-turn-3": ("会议预算三千", "会议预算", "会议预算不是五千，是三千"),
}


class FixedReviewProvider:
    model = "independent-offline-review-fixtures"

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        turn = payload["packet"]["turn_id"]
        rows = []
        for entry in payload["items"]:
            if turn == "dual-turn-2":
                rows.append(dict(index=entry["index"], verdict="unsupported", reason="用户明确设备采购预算未变化，不能借会议纠正修改预算"))
                continue
            content, subject, quote = REVIEW_FIXTURES[turn]
            rows.append(dict(index=entry["index"], verdict="supported", content=content,
                subject=subject, reason="该预算原话明确从五千纠正为三千", same_subject=True,
                preserves_negation=True, preserves_conditions=True, change_authorized=True,
                lifetime={"mode":"long_term", "basis":"预算事实，原话没有期限"},
                quotes=[{"message_id":turn,"quote":part} for part in quote.split("，")]))
        return SimpleNamespace(message={"content":json.dumps({"items":rows},ensure_ascii=False)}, usage={})


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class FixedExtractor:
    """云端提炼替身：给出的就是本地队列里放进去的那一条建议。"""

    def __init__(self, suggestions, provider) -> None:
        self.suggestions = list(suggestions)
        self.provider = provider
        self.calls = 0

    async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
        self.calls += 1
        return [], list(self.suggestions)


def packet_for(turn_id: str, text: str):
    return turn_from_messages(
        turn_id=turn_id,
        occurred_at="2026-10-11T12:00:00+00:00",
        scope=SCOPE,
        user_text=text,
        published_reply="好",
        project_binding=PROJECT_BINDING,
    )


def suggestion_for(target: dict, content: str, *, turn_id: str):
    return MemoryUpdateSuggestion(
        action="correct",
        target_id=target["id"],
        expected_version=int(target["record_version"]),
        content=content,
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=(turn_id,),
        reason="用户纠正了这个数字",
    )


class End:
    """一端：自己的记录库、向量集合，以及自己的工人入口。"""

    def __init__(self, base: Path, name: str) -> None:
        self.name = name
        self.repos = initialize_memory_repositories(
            base / f"{name}-records.db", base / f"{name}-vectors", vector_size=3
        )
        self.writer = MemoryWriter(self.repos, Embeddings())
        self.service = MemoryMaintenanceService(self.writer, self.repos)
        self.review_provider = FixedReviewProvider()

    def seed(self, content: str, *, turn_id: str) -> dict:
        return seed_semantic(self.writer,
            SCOPE,
            [
                MemoryCandidate(
                    content=content,
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=(turn_id,),
                    scope_kind="project",
                    project_id=PROJECT_BINDING,
                    occurred_at="2026-10-11T10:00:00+00:00",
                )
            ],
            turn_id=turn_id,
            occurred_at="2026-10-11T10:00:00+00:00",
            project_binding=PROJECT_BINDING,
        )[0]

    def status_of(self, record_id: str) -> dict:
        row = self.repos.records_by_ids([record_id])[record_id]
        return {
            "status": row["status"],
            "content": row["content"],
            "content_status": row["content_status"],
        }

    def snapshot(self) -> dict:
        """只看这次对照关心的字段：现行版本、归档关系、来源、范围、版本号。"""
        rows = [
            row
            for row in self.writer.list_records(SCOPE)
            if any(marker in (row["content"] or "") for marker in CONTENT_MARKERS)
        ]
        current = [row for row in rows if row["status"] == "active"]
        archived = [row for row in rows if row["status"] != "active"]
        return {
            "current_contents": sorted(row["content"] for row in current),
            "current_versions": sorted(int(row["record_version"] or 0) for row in current),
            "archived_contents": sorted(row["content"] for row in archived),
            "archived_content_statuses": sorted(str(row["content_status"]) for row in archived),
            "archived_points_to_current": all(
                bool(row["effective_record_id"]) for row in archived
            ),
            "current_scope_kinds": sorted({str(row["scope_kind"]) for row in current}),
            "current_project_ids": sorted({str(row["project_id"] or "") for row in current}),
            "current_source_refs": sorted(
                {
                    str(ref)
                    for row in current
                    for ref in json.loads(row["metadata"] or "{}").get("source_refs", [])
                }
            ),
        }

    def close(self) -> None:
        self.repos.close()


class LocalEnd(End):
    """本地路径：持久维护队列 → 维护工人应用。"""

    def __init__(self, base: Path) -> None:
        super().__init__(base, "local")
        self.queue = MemoryCandidateQueue(base / "local-jobs.db")
        self.queue.initialize()
        self.worker = MemoryMaintenanceWorker(self.queue, self.service, review_provider=self.review_provider)

    def process(self, packet, suggestions) -> dict:
        job_id, _ = self.queue.enqueue_maintenance(packet, suggestions)
        outcome = asyncio.run(self.worker.process_once())
        job = self.queue.get(job_id)
        return {
            "outcome": outcome,
            "status": job["status"],
            "attempts": int(job["attempts"]),
            "error": job["last_error"],
            "suggestions_kept": len(self.queue.stored_suggestions(job)),
        }


class CloudEnd(End):
    """云端路径：接收队列 → 云端工人（真实入口）。"""

    def __init__(self, base: Path) -> None:
        super().__init__(base, "cloud")
        self.store = CloudMemoryStore(base / "cloud-queue.db")
        self.store.initialize()

    def process(self, packet, suggestions) -> dict:
        self.store.enqueue_turn(
            {
                "turn_id": packet.turn_id,
                "owner_id": packet.scope_owner,
                "channel": packet.scope_channel,
                "conversation_id": packet.scope_conversation,
                "module_id": packet.scope_module,
                "user_text": packet.user_text,
                "assistant_reply": packet.published_reply,
                "occurred_at": packet.occurred_at,
                "packet": packet.as_payload(),
            }
        )
        extractor = FixedExtractor(suggestions, self.review_provider)
        worker = CloudMemoryWorker(
            self.repos,
            self.store,
            self.writer,
            None,
            semantic_extractor=extractor,
            maintenance=self.service,
            interval_seconds=0,
            batch_size=10,
        )
        asyncio.run(worker.run_once())
        with self.store.connect() as db:
            row = dict(
                db.execute(
                    "SELECT * FROM memory_ingest_queue WHERE turn_id=?", (packet.turn_id,)
                ).fetchone()
            )
        stored = self.store.stored_extraction(row)
        return {
            "outcome": {
                key: value for key, value in worker.stats.items() if key != "duration_ms"
            },
            "status": row["status"],
            "attempts": int(row["attempts"]),
            "error": row["last_error"],
            "suggestions_kept": len(stored["suggestions"]),
            "extractor_calls": extractor.calls,
        }


def compare(label: str, left: dict, right: dict, checks: list[dict]) -> None:
    """逐字段比对两端，字段名由左端（本地）决定。"""
    for name in sorted(left):
        checks.append(
            {
                "id": f"{label}:{name}",
                "passed": left[name] == right.get(name),
                "local": left[name],
                "cloud": right.get(name),
            }
        )


def run() -> int:
    checks: list[dict] = []
    # 这些开关是**进程级**的：脚本自己用完必须还原，否则同一个测试进程里
    # 后面的用例会继承「云端处理端」这种只属于本次对照的设置。
    watched = ("MEMORY_SEMANTIC_ENABLED", "MEMORY_PROCESSING_ENDPOINT", "AGENT_MEMORY_PROJECT")
    previous = {name: os.environ.get(name) for name in watched}
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    os.environ["MEMORY_PROCESSING_ENDPOINT"] = "cloud"
    os.environ.pop("AGENT_MEMORY_PROJECT", None)
    try:
        return _run_checks(checks)
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _run_checks(checks: list[dict]) -> int:
    with tempfile.TemporaryDirectory(prefix="dual-end-", ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        local = LocalEnd(base)
        cloud = CloudEnd(base)
        try:
            # ---- 对照一：正常纠正，两端应落成同一种版本关系 ------------
            local_target = local.seed("项目采购预算五千", turn_id="dual-seed-a")
            cloud_target = cloud.seed("项目采购预算五千", turn_id="dual-seed-a")
            local_receipt = local.process(
                packet_for("dual-turn-1", "项目采购预算不是五千，是三千"),
                [suggestion_for(local_target, "项目采购预算三千", turn_id="dual-turn-1")],
            )
            cloud_receipt = cloud.process(
                packet_for("dual-turn-1", "项目采购预算不是五千，是三千"),
                [suggestion_for(cloud_target, "项目采购预算三千", turn_id="dual-turn-1")],
            )
            compare("纠正", local.snapshot(), cloud.snapshot(), checks)
            # Equality alone is insufficient: both ends refusing would also be equal.
            expected = {
                "current_contents": ["项目采购预算三千"], "current_versions": [2],
                "archived_contents": ["项目采购预算五千"],
                "current_scope_kinds": ["project"], "current_project_ids": [PROJECT_BINDING],
                "current_source_refs": ["dual-seed-a", "dual-turn-1"], "archived_points_to_current": True,
            }
            for name, value in expected.items():
                left, right = local.snapshot()[name], cloud.snapshot()[name]
                checks.append({"id": f"独立预期:{name}", "expected": value,
                               "local": left, "cloud": right, "passed": left == right == value})
            checks.append(
                {
                    "id": "纠正:两端都确认落地",
                    "passed": local_receipt["status"] == "completed"
                    and cloud_receipt["status"] in ("maintained", "stored"),
                    "local": local_receipt,
                    "cloud": cloud_receipt,
                }
            )

            # ---- 对照二：证据不足的纠正，两端都不许改状态 --------------
            local_bad = local.seed("设备采购预算五千", turn_id="dual-seed-b")
            cloud_bad = cloud.seed("设备采购预算五千", turn_id="dual-seed-b")
            local_refusal = local.process(
                packet_for("dual-turn-2", "不是周一，是周二开会；设备采购预算还没变化"),
                [suggestion_for(local_bad, "设备采购预算三千", turn_id="dual-turn-2")],
            )
            cloud_refusal = cloud.process(
                packet_for("dual-turn-2", "不是周一，是周二开会；设备采购预算还没变化"),
                [suggestion_for(cloud_bad, "设备采购预算三千", turn_id="dual-turn-2")],
            )
            local_after = local.status_of(local_bad["id"])
            cloud_after = cloud.status_of(cloud_bad["id"])
            checks.append(
                {
                    "id": "拒绝:两端都保持原状",
                    "passed": local_after == cloud_after == {
                        "status": "active",
                        "content": "设备采购预算五千",
                        "content_status": "reported",
                    },
                    "local": local_after,
                    "cloud": cloud_after,
                }
            )
            checks.append(
                {
                    "id": "拒绝:两端都不按失败重试",
                    "passed": local_refusal["status"] == cloud_refusal["status"] == "refused"
                    and local_refusal["attempts"] == cloud_refusal["attempts"] == 0,
                    "local": local_refusal,
                    "cloud": cloud_refusal,
                }
            )

            # ---- 对照三：存储故障，两端都必须留下可恢复的失败 ----------
            local_out = local.seed("会议预算五千", turn_id="dual-seed-c")
            cloud_out = cloud.seed("会议预算五千", turn_id="dual-seed-c")
            outage_packet = packet_for("dual-turn-3", "会议预算不是五千，是三千")
            with patch.object(
                local.writer,
                "apply_semantic_update",
                side_effect=sqlite3.OperationalError("simulated storage outage"),
            ), patch.object(
                cloud.writer,
                "apply_semantic_update",
                side_effect=sqlite3.OperationalError("simulated storage outage"),
            ):
                local_outage = local.process(
                    outage_packet,
                    [suggestion_for(local_out, "会议预算三千", turn_id="dual-turn-3")],
                )
                cloud_outage = cloud.process(
                    outage_packet,
                    [suggestion_for(cloud_out, "会议预算三千", turn_id="dual-turn-3")],
                )

            checks.append(
                {
                    "id": "故障:两端都进入可恢复失败",
                    "passed": local_outage["status"] == "failed"
                    and cloud_outage["status"] == "failed"
                    and local_outage["attempts"] > 0
                    and cloud_outage["attempts"] > 0
                    and "OperationalError" in str(local_outage["error"] or "")
                    and "OperationalError" in str(cloud_outage["error"] or "")
                    and local_outage["suggestions_kept"] == cloud_outage["suggestions_kept"] == 1,
                    "local": local_outage,
                    "cloud": cloud_outage,
                }
            )
            checks.append(
                {
                    "id": "故障:两端都不改动原记录",
                    "passed": local.status_of(local_out["id"])["status"] == "active"
                    and cloud.status_of(cloud_out["id"])["status"] == "active",
                    "local": local.status_of(local_out["id"]),
                    "cloud": cloud.status_of(cloud_out["id"]),
                }
            )
        finally:
            local.close()
            cloud.close()
            gc.collect()

    report = {
        "ok": all(item["passed"] for item in checks),
        "passed": sum(item["passed"] for item in checks),
        "total": len(checks),
        "checks": checks,
        "live_model_calls": 0,
        "review_fixture_calls": {"local": local.review_provider.calls, "cloud": cloud.review_provider.calls},
        "production_memory_touched": False,
        "limits": [
            "两端都用临时库和固定提炼替身：比对的是程序侧语义，不是模型措辞",
            "没有登录或部署生产云端：生产实例的装配不在本脚本覆盖范围内",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(run())
