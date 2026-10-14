"""候选提炼的持久作业队列（B 计划第二步）。

【为什么必须是队列，不是一个后台协程】
网页请求里起的临时协程会随请求结束消失：进程一重启，没提炼完的回合就永远
丢了，而且丢得无声无息。这里把「待提炼的回合」写进 SQLite：重启能接着跑，
重试有次数上限，失败数量能查出来。

【两条硬规矩】
1. **先存候选，再写记忆库**。提炼结果落库前先留在作业行里，重试时直接复用
   已保存的候选，不再调一次模型 —— 否则同一回合会被提炼出两套略微不同的记录。
2. **同一个 turn_id 只入队一次**。网页入口和记忆会话都提交同一个回合时，
   后到的只算幂等确认，不新开一条作业。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .memory_logging import configure_memory_logging
from .memory_turn import (
    MemoryTurnPacket,
    apply_packet_budget,
    decode_candidates,
    decode_suggestions,
    scope_from_packet,
    serialize_candidates,
    serialize_suggestions,
)
from .memory_processing import REVIEW_TIMEOUT_SECONDS
from .memory_job_runtime import AgentJobStore, initialize_agent_columns, run_agent_job

LOGGER = logging.getLogger("agent_core.memory.queue")

#: 单条作业最多尝试次数，超过就停在 failed 上等人查（不再自动重试）。
MAX_ATTEMPTS = 3
#: 递增重试间隔（秒）。第 n 次失败后用第 n 个值，用完取最后一个。
RETRY_BACKOFF_SECONDS = (30, 120)
#: 认领后的租约时长：进程中途死掉，超过它就允许别的处理端重新认领。
DEFAULT_LEASE_SECONDS = 120
#: 单次提炼调用的超时（秒）。超时不阻塞正文，只把作业标成失败待重试。
DEFAULT_EXTRACT_TIMEOUT = 30.0

JOB_STATUSES = ("pending", "processing", "completed", "failed", "skipped", "refused", "pending_review", "unresolved")

#: 作业类别。候选作业的产物是「待新建的候选」，维护作业的产物是「对已有记录的
#: 更新建议」。放同一张表、用类别区分：认领时各取各的，谁也不会把对方的作业领走。
#: 不分成两张表是因为重试、租约、重启恢复这些机制完全一样，抄一遍只会慢慢分叉。
JOB_KINDS = ("candidates", "maintenance")
#: 维护作业的行键后缀。表上 turn_id 是 UNIQUE 的（同一个回合只入队一次），
#: 同一个回合的候选作业和维护作业要各占一行，就得是两个不同的键。
MAINTENANCE_TURN_SUFFIX = "#maintenance"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


#: 判断「是不是同一个作业」要比的载荷字段。**不含** occurred_at 之类时间字段：
#: 同一份内容重新提交时这些会自然变化，拿它们比会把正常重试判成冲突。
_IDENTITY_FIELDS = ("user_text", "published_reply", "outcome_status", "task_goal", "completeness")


def _payload_identity(payload: Any) -> tuple[str, ...]:
    """处理包的内容身份：谁在哪个会话里说了什么、回答发布了没有。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return ("unparsable", str(payload)[:200])
    if not isinstance(payload, dict):
        return ("unparsable", str(payload)[:200])
    scope = dict(payload.get("scope") or {})
    # 澄清关联也是内容身份的一部分：同一个回合带了不同的关联，就不是同一份作业。
    # 缺这个字段的旧包得到空串，与旧行为一致（不改变既有幂等判断）。
    clarification = payload.get("clarification_context")
    if isinstance(clarification, dict) and clarification:
        links = clarification.get("possible_links") or []
        association = json.dumps(
            [
                [str(item.get("candidate_id") or ""), str(item.get("item_path") or ""),
                 int(item.get("target_version") or 0)]
                for item in links if isinstance(item, dict)
            ],
            ensure_ascii=False, sort_keys=True,
        )
    else:
        association = ""
    return (
        str(scope.get("owner_id") or ""),
        str(scope.get("channel") or ""),
        str(scope.get("conversation_id") or ""),
        str(scope.get("module_id") or ""),
        *(str(payload.get(field) or "") for field in _IDENTITY_FIELDS),
        association,
    )


class MemoryCandidateQueue(AgentJobStore):
    """候选作业的持久存储。每个方法自己开连接，连不上就抛，不吞异常。"""

    agent_job_table = 'memory_candidate_jobs'
    agent_job_key = 'job_id'
    agent_retry_column = 'next_attempt_at'
    agent_success_status = 'completed'

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        configure_memory_logging()

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
                CREATE TABLE IF NOT EXISTS memory_candidate_jobs (
                  job_id TEXT PRIMARY KEY,
                  turn_id TEXT NOT NULL UNIQUE,
                  scope_key TEXT NOT NULL,
                  packet TEXT NOT NULL,
                  candidates TEXT,
                  status TEXT NOT NULL,
                  attempts INTEGER NOT NULL DEFAULT 0,
                  next_attempt_at TEXT,
                  lease_until TEXT,
                  last_error TEXT,
                  saved_ids TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS memory_candidate_jobs_status_idx
                  ON memory_candidate_jobs(status, next_attempt_at);
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(memory_candidate_jobs)")}
            if "kind" not in columns:
                # 老库补列：已有的行按「候选作业」算，行为不变。
                db.execute(
                    "ALTER TABLE memory_candidate_jobs ADD COLUMN kind TEXT NOT NULL DEFAULT 'candidates'"
                )
            if "suggestions" not in columns:
                # 候选作业的提炼产物里也有「对已有记录的更新建议」。放在自己的列里，
                # 不和 candidates 混用 —— 混用时重试不知道读到的是候选还是建议。
                db.execute("ALTER TABLE memory_candidate_jobs ADD COLUMN suggestions TEXT")
            if "extracted" not in columns:
                # 0/1：这条作业的**提炼结果已经落库**。有了它，重试才不会因为
                # 「候选是空的」而再调一次模型、再得出另一套措辞。
                db.execute(
                    "ALTER TABLE memory_candidate_jobs ADD COLUMN extracted INTEGER NOT NULL DEFAULT 0"
                )
            if "receipts" not in columns:
                # 核对回执：规则版本、内容与来源摘要、目标版本、逐项判断与依据引用。
                # 和候选／建议存在同一张作业表里（不新增记忆库）；重试时内容与目标
                # 版本没变就直接复用这份判断，变了则作废重算。
                db.execute("ALTER TABLE memory_candidate_jobs ADD COLUMN receipts TEXT")
            for column in ('review_results', 'processing_result'):
                if column not in columns:
                    db.execute(f'ALTER TABLE memory_candidate_jobs ADD COLUMN {column} TEXT')
            initialize_agent_columns(db, self.agent_job_table)
            db.execute("UPDATE memory_candidate_jobs SET pipeline='legacy' WHERE extracted=1 AND pipeline IS NULL")
            db.execute(
                "CREATE INDEX IF NOT EXISTS memory_candidate_jobs_kind_idx "
                "ON memory_candidate_jobs(kind, status, next_attempt_at)"
            )

    # ------------------------------------------------------------------
    # 入队 / 查询
    # ------------------------------------------------------------------
    def enqueue(self, packet: MemoryTurnPacket) -> tuple[str, bool]:
        """把回合处理包写进队列；返回 (作业号, 是否新建)。

        幂等只对**同一个回合的同一份内容**成立。同一个 ``turn_id`` 又送来不同
        正文时抛 ``ValueError``，而不是把新内容当成旧作业的重复提交悄悄丢掉：
        以前只按 turn_id 找旧记录，于是「同回合改了正文」的第二次提交返回旧作业，
        新内容永远不落库，而且一声不响。
        """
        payload_dict = packet.as_payload()
        payload = json.dumps(payload_dict, ensure_ascii=False)
        scope_key = "|".join(
            [packet.scope_owner, packet.scope_channel, packet.scope_conversation, packet.scope_module]
        )
        identity = _payload_identity(payload_dict)
        now = _now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT job_id,packet FROM memory_candidate_jobs WHERE turn_id=?", (packet.turn_id,)
            ).fetchone()
            if existing:
                if _payload_identity(existing["packet"]) != identity:
                    raise ValueError(
                        f"同一回合号已有不同内容的作业，拒绝静默复用：turn_id={packet.turn_id}"
                    )
                return str(existing["job_id"]), False
            job_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO memory_candidate_jobs"
                "(job_id,turn_id,scope_key,packet,status,attempts,kind,created_at,updated_at) "
                "VALUES(?,?,?,?,'pending',0,'candidates',?,?)",
                (job_id, packet.turn_id, scope_key, payload, now, now),
            )
        return job_id, True

    def enqueue_maintenance(self, packet: MemoryTurnPacket, suggestions, *, run_at: str | None = None) -> tuple[str, bool]:
        """把「对已有记录的更新建议」入队；返回 (作业号, 是否新建)。

        建议**先落库再应用**：应用过程中进程被杀，重启后直接复用这条建议重试，
        不重新调模型 —— 重跑一次模型会得出措辞略有不同的一版，那条建议的幂等键
        就跟着变了，「同一动作不重复应用」这条保证也就没了。
        """
        payload_dict = packet.as_payload()
        payload = json.dumps(payload_dict, ensure_ascii=False)
        job_turn_id = f"{packet.turn_id}{MAINTENANCE_TURN_SUFFIX}"
        scope_key = "|".join(
            [packet.scope_owner, packet.scope_channel, packet.scope_conversation, packet.scope_module]
        )
        identity = _payload_identity(payload_dict)
        now = _now()
        with self.connect() as db:
            existing = db.execute(
                "SELECT job_id,packet FROM memory_candidate_jobs WHERE turn_id=?", (job_turn_id,)
            ).fetchone()
            if existing:
                if _payload_identity(existing["packet"]) != identity:
                    raise ValueError(f"同一回合号已有不同内容的维护作业：turn_id={packet.turn_id}")
                return str(existing["job_id"]), False
            job_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO memory_candidate_jobs"
                "(job_id,turn_id,scope_key,packet,candidates,status,attempts,kind,next_attempt_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'pending',0,'maintenance',?,?,?)",
                (job_id, job_turn_id, scope_key, payload, serialize_suggestions(suggestions), run_at, now, now),
            )
        return job_id, True

    def get(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM memory_candidate_jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, int]:
        """各状态条数；失败数量供维护排查用（不静默失败）。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) AS n FROM memory_candidate_jobs GROUP BY status"
            ).fetchall()
        result = {status: 0 for status in JOB_STATUSES}
        for row in rows:
            result[str(row["status"])] = int(row["n"])
        result["total"] = sum(result[status] for status in JOB_STATUSES)
        return result

    def backlog(self) -> int:
        """待处理 + 处理中的条数（失败后还会重试的也算待处理）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM memory_candidate_jobs WHERE status IN ('pending','processing')"
            ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 认领 / 完成 / 失败
    # ------------------------------------------------------------------
    def claim(self, *, limit: int = 5, now: datetime | None = None, lease_seconds: int = DEFAULT_LEASE_SECONDS, kind: str | None = None) -> list[dict]:
        return self._claim_jobs(limit=limit, now=now, kind=kind, lease_seconds=lease_seconds)

    def save_candidates(self, job_id: str, candidates) -> None:
        """候选先落在这里；写记忆库失败时重试直接复用，不再调模型。"""
        with self.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET candidates=?,updated_at=? WHERE job_id=?",
                (serialize_candidates(candidates), _now(), job_id),
            )

    def stored_candidates(self, job: dict) -> list:
        return decode_candidates(job.get("candidates"))

    def save_suggestions(self, job_id: str, suggestions) -> None:
        """更新建议先落在这里；应用失败重试时复用原建议，不再调模型。"""
        with self.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET candidates=?,suggestions=?,updated_at=? WHERE job_id=?",
                (
                    serialize_suggestions(suggestions),
                    serialize_suggestions(suggestions),
                    _now(),
                    job_id,
                ),
            )

    def save_extraction(self, job_id: str, candidates, suggestions) -> None:
        """把一次提炼的**全部产物**先落库，再去交接和写记忆库。

        这是「交接失败可恢复」的前提：入队更新建议失败时作业按失败重试，
        重试直接读这里存着的候选与建议，不再调模型 —— 否则每次重试都会得出
        一版措辞略有不同的建议，同一动作的幂等键随之改变，「只生效一次」就没了。
        """
        with self.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET candidates=?,suggestions=?,extracted=1,updated_at=? "
                "WHERE job_id=?",
                (
                    serialize_candidates(candidates),
                    serialize_suggestions(suggestions),
                    _now(),
                    job_id,
                ),
            )

    def save_receipts(self, job_id: str, receipts) -> None:
        """把这一轮已经做过的核对回执随作业一起存下来（重试时复用）。"""
        with self.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET receipts=?,updated_at=? WHERE job_id=?",
                (json.dumps(list(receipts or ()), ensure_ascii=False), _now(), job_id),
            )

    def stored_receipts(self, job: dict) -> list:
        raw = job.get("receipts")
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return payload if isinstance(payload, list) else []

    def extraction_done(self, job: dict) -> bool:
        """这条作业的提炼结果是否已经落库（落库过就不再调模型）。"""
        return bool(job.get("extracted"))

    def stored_candidates(self, job: dict) -> list:
        return decode_candidates(job.get("candidates"))

    def stored_suggestions(self, job: dict) -> list:
        # 维护作业的建议历史上存在 candidates 列里；候选作业用独立的 suggestions 列。
        raw = job.get("suggestions")
        if raw is None:
            raw = job.get("candidates")
        return decode_suggestions(raw)

    def kind_stats(self) -> dict[str, dict[str, int]]:
        """按类别分开计数：内容维护的开销要能和候提炼分开看。

        混成一个总数时，「候选积压」和「维护积压」长得一模一样，
        排查时分不清是该查模型还是该查库。
        """
        result: dict[str, dict[str, int]] = {
            kind: {status: 0 for status in JOB_STATUSES} for kind in JOB_KINDS
        }
        with self.connect() as db:
            rows = db.execute(
                "SELECT kind,status,COUNT(*) AS n FROM memory_candidate_jobs GROUP BY kind,status"
            ).fetchall()
        for row in rows:
            bucket = result.setdefault(
                str(row["kind"]), {status: 0 for status in JOB_STATUSES}
            )
            bucket[str(row["status"])] = int(row["n"])
        return result

    def complete(self, job_id: str, *, saved_ids=()) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET status='completed',saved_ids=?,lease_until=NULL,"
                "next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE job_id=?",
                (json.dumps(list(saved_ids), ensure_ascii=False), _now(), job_id),
            )

    def save_review_results(self, job_id, result):
        with self.connect() as db:
            db.execute('UPDATE memory_candidate_jobs SET review_results=?,updated_at=? WHERE job_id=?',
                       (json.dumps(result, ensure_ascii=False), _now(), job_id))

    def resume_review_configuration(self, *, kind=None):
        with self.connect() as db:
            cursor = db.execute("UPDATE memory_candidate_jobs SET status='pending',updated_at=? "
                                "WHERE status='pending_review' AND last_error LIKE '%review_not_configured%' "
                                "AND (? IS NULL OR kind=?)", (_now(), kind, kind))
            return cursor.rowcount

    def finish_processing(self, job_id, result, *, saved_ids=()):
        status = 'pending_review' if result.pending_review else ('refused' if result.rejected else 'completed')
        reasons = [r['evidence_receipt'].get('review_error', 'evidence_not_supported') for r in result.pending_review + result.rejected]
        with self.connect() as db:
            db.execute('UPDATE memory_candidate_jobs SET status=?,processing_result=?,saved_ids=?,lease_until=NULL,next_attempt_at=NULL,last_error=?,updated_at=? WHERE job_id=?',
                       (status, json.dumps(result.as_dict(), ensure_ascii=False), json.dumps(list(saved_ids)), ';'.join(reasons) or None, _now(), job_id))

    def skip(self, job_id: str, *, reason: str) -> None:
        """没有可用提炼器这类「不是错误、但干不了」的情况。"""
        with self.connect() as db:
            db.execute(
                "UPDATE memory_candidate_jobs SET status='skipped',lease_until=NULL,last_error=?,updated_at=? "
                "WHERE job_id=?",
                (reason[:500], _now(), job_id),
            )

    def fail(self, job_id: str, *, error: str, now: datetime | None = None) -> str:
        """记一次失败；返回落定后的状态（failed 表示还会重试，dead 表示不再重试）。"""
        current = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM memory_candidate_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            retryable = attempts < MAX_ATTEMPTS
            delay = RETRY_BACKOFF_SECONDS[min(attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
            db.execute(
                "UPDATE memory_candidate_jobs SET status='failed',attempts=?,next_attempt_at=?,"
                "lease_until=NULL,last_error=?,updated_at=? WHERE job_id=?",
                (
                    attempts,
                    _stamp(current + timedelta(seconds=delay)) if retryable else None,
                    str(error)[:1000],
                    _stamp(current),
                    job_id,
                ),
            )
        return "failed" if retryable else "dead"


class MemoryCandidateWorker:
    """从队列取一个回合 → 提炼候选 → 校验 → 写入记忆库。

    提炼失败或超时**不影响已经发布的回答**：这里只是事后补记忆。
    """

    def __init__(
        self,
        queue: MemoryCandidateQueue,
        extractor: Any,
        writer: Any,
        *,
        timeout_seconds: float = DEFAULT_EXTRACT_TIMEOUT,
        batch_size: int = 5,
        maintenance_queue: MemoryCandidateQueue | None = None,
        review_provider=None,
        review_timeout_seconds=REVIEW_TIMEOUT_SECONDS,
        model_agent=None,
    ) -> None:
        self.queue = queue
        self.extractor = extractor
        self.writer = writer
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        # 内容维护作业的出口：一次提炼同时产出「待新建的候选」和「对已有记录的
        # 更新建议」，后者从这里入队，由维护工人逐个应用。不给出口时只在日志里
        # 报数 —— 不许静默丢掉。
        self.maintenance_queue = maintenance_queue
        self.review_provider = review_provider if review_provider is not None else getattr(extractor, 'provider', None)
        self.review_timeout_seconds = review_timeout_seconds
        self.model_agent = model_agent

    async def process_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        if self.model_agent is not None and self.model_agent.provider is not None:
            self.queue.resume_agent_configuration()
        if self.review_provider is not None:
            self.queue.resume_review_configuration(kind='candidates')
        jobs = self.queue.claim(limit=1 if self.model_agent is not None else self.batch_size, now=now, kind='candidates')
        result = {"claimed": len(jobs), "stored": 0, "empty": 0, "failed": 0, "skipped": 0}
        for job in jobs:
            outcome = await self._process_job(job)
            result[outcome] = result.get(outcome, 0) + 1
        return result

    async def _process_job(self, job: dict) -> str:
        job_id = str(job["job_id"])
        if self.model_agent is not None and job.get('pipeline') != 'legacy' and not self.queue.extraction_done(job):
            packet = MemoryTurnPacket.from_payload(json.loads(job['packet']))
            return await run_agent_job(self.queue, job, packet, self.model_agent)
        self.queue.select_legacy_pipeline(job_id, job['lease_owner'])
        # 处理包先按预算裁一次再交给提炼。作业里存的那份原始包不动 ——
        # 重试时还要能重新裁，改了原始包就没法复核了。
        packet, budget_report = apply_packet_budget(
            MemoryTurnPacket.from_payload(json.loads(job["packet"]))
        )
        if budget_report.get("needs_retry"):
            LOGGER.warning("处理包超出预算，本轮标记为缺背景：turn=%s", packet.turn_id)
        extracted = self.queue.extraction_done(job)
        candidates = self.queue.stored_candidates(job) if extracted else []
        suggestions = self.queue.stored_suggestions(job) if extracted else []
        if not extracted:
            if self.extractor is None:
                # 缺提炼器是配置问题，不是错误：记账后跳过，积压条数看得见。
                self.queue.skip(job_id, reason="semantic_extractor_not_configured")
                LOGGER.warning("候选提炼未配置，作业跳过：turn=%s", packet.turn_id)
                return "skipped"
            try:
                candidates, suggestions = await asyncio.wait_for(
                    self._extract(packet), timeout=self.timeout_seconds
                )
            except asyncio.TimeoutError:
                self.queue.fail(job_id, error="extract_timeout")
                LOGGER.warning("候选提炼超时：turn=%s", packet.turn_id)
                return "failed"
            except Exception as exc:  # noqa: BLE001 - 提炼失败不影响已发布回答
                self.queue.fail(job_id, error=f"{type(exc).__name__}: {exc}")
                LOGGER.warning("候选提炼失败：turn=%s 错误类型=%s", packet.turn_id, type(exc).__name__)
                return "failed"
            # **提炼结果先落库**：候选和更新建议都在这里存下来。后面交接或写记忆库
            # 失败时重试直接复用这一份，不再调模型 —— 重跑一次模型会得出另一版措辞，
            # 那条建议的幂等键就变了，「同一动作只生效一次」也就没了。
            try:
                self.queue.save_extraction(job_id, candidates, suggestions)
            except Exception as exc:  # noqa: BLE001 - 存不下就按失败重试
                self.queue.fail(job_id, error=f"{type(exc).__name__}: {exc}")
                LOGGER.warning("提炼结果落库失败：turn=%s 错误类型=%s", packet.turn_id, type(exc).__name__)
                return "failed"

        from .memory_processing import MemoryReviewProcessor
        try:
            candidates, suggestions, processing = await MemoryReviewProcessor(self.writer, self.review_provider, timeout_seconds=self.review_timeout_seconds).prepare(
                packet, candidates, suggestions, cached=json.loads(job.get('review_results') or 'null'),
                persist=lambda payload: self.queue.save_review_results(job_id, payload))
        except Exception as exc:
            self.queue.fail(job_id, error=f'review_failed:{type(exc).__name__}: {exc}')
            return 'failed'

        if suggestions:
            handoff_error = await self._hand_off_suggestions(packet, suggestions)
            if handoff_error:
                # 交接失败**不能**把原作业标成完成：建议还没进维护队列，
                # 这次纠正会永久丢失。按失败重试，重试时复用已落库的建议。
                self.queue.fail(job_id, error=handoff_error)
                LOGGER.warning(
                    "内容维护作业入队失败，原作业保留重试：turn=%s 错误=%s",
                    packet.turn_id,
                    handoff_error,
                )
                return "failed"
        if not candidates:
            self.queue.finish_processing(job_id, processing)
            return 'pending_review' if processing.pending_review else ('refused' if processing.rejected else 'empty')

        try:
            committed = await asyncio.to_thread(
                self.writer.process_semantic_candidates,
                scope_from_packet(packet),
                candidates,
                turn_id=packet.turn_id,
                occurred_at=packet.occurred_at,
                known_ids=[ref.memory_id for ref in packet.recalled_memory_refs],
                # 可信项目绑定从**包**里传下去：重新读本端环境变量会让两端
                # 环境不同的部署存出不同的范围，同项目的新会话就接不上。
                # 包里确实没有绑定时（or None）才退回本端运行配置。
                project_binding=packet.project_binding or None,
                # 处理包一起传下去：寿命规则（用户明说的期限）与替代依据核对
                # 都要看用户原话，不传包这两件事在真实链路上都不会发生。
                packet=packet,
                # 上一次已经做过的核对回执：内容与目标版本没变时直接复用。
                receipts=self.queue.stored_receipts(job),
            )
            records = committed.saved
            processing.checks.extend(committed.checks)
            processing.rejected.extend(committed.rejected)
            processing.pending_review.extend(committed.pending_review)
        except Exception as exc:  # noqa: BLE001 - 写入失败留给重试
            self.queue.fail(job_id, error=f"{type(exc).__name__}: {exc}")
            LOGGER.warning("候选写入失败：turn=%s 错误类型=%s", packet.turn_id, type(exc).__name__)
            return "failed"
        processing.saved = records
        self.queue.finish_processing(job_id, processing, saved_ids=[record['id'] for record in records])
        LOGGER.info("候选写入完成：turn=%s 条数=%d", packet.turn_id, len(records))
        return "stored"

    async def _extract(self, packet: MemoryTurnPacket) -> tuple[list, list]:
        """一次模型调用同时取「待新建的候选」和「对已有记录的更新建议」。

        提炼器没有新版入口时（测试替身、旧实现）退回只取候选：一个可选能力
        不该把整条候选链路带崩。
        """
        extractor = self.extractor
        known_ids = [ref.memory_id for ref in packet.recalled_memory_refs]
        known_versions = {ref.memory_id: int(ref.version) for ref in packet.recalled_memory_refs}
        if hasattr(extractor, "extract_maintenance"):
            candidates, suggestions = await extractor.extract_maintenance(
                packet, known_ids=known_ids, known_versions=known_versions
            )
            return list(candidates), list(suggestions)
        return list(await extractor.extract(packet, known_ids=known_ids)), []

    async def _hand_off_suggestions(self, packet: MemoryTurnPacket, suggestions) -> str:
        """把更新建议交给维护队列；返回空串表示交接成功。

        失败时把错误**返回给调用方**，由它把原作业记成可重试的失败 ——
        只写一行日志、然后照常把作业标成完成，等于这次纠正永久消失。
        """
        if self.maintenance_queue is None:
            LOGGER.warning(
                "本次提炼含 %d 条内容更新建议，未接入内容维护，先记录不落库：turn=%s",
                len(suggestions),
                packet.turn_id,
            )
            return ""
        try:
            await asyncio.to_thread(
                self.maintenance_queue.enqueue_maintenance, packet, suggestions
            )
            LOGGER.info("内容维护作业已入队：turn=%s 建议=%d", packet.turn_id, len(suggestions))
            return ""
        except Exception as exc:  # noqa: BLE001 - 入队失败交给原作业重试
            return f"maintenance_handoff_failed:{type(exc).__name__}: {exc}"

    async def run_forever(self, *, interval_seconds: float = 30.0, stop: asyncio.Event | None = None) -> None:
        """按固定间隔排空队列；``stop`` 被 set 后退出（测试与关停用）。"""
        while stop is None or not stop.is_set():
            await self.process_once()
            if stop is None:
                await asyncio.sleep(interval_seconds)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                except asyncio.TimeoutError:
                    continue


class MemoryMaintenanceWorker:
    """把队列里的更新建议逐个应用到库里。

    与候选工人共用一张作业表，只认 ``kind='maintenance'`` 的行：认领时各取各的，
    谁也领不走对方的作业。

    应用是**幂等**的（同一条建议算同一个幂等键），所以超时或进程被杀之后重试
    不会造出第二条版本 —— 这是「重启可恢复」能成立的前提。
    """

    def __init__(self, queue: MemoryCandidateQueue, service: Any, *, batch_size: int = 5, review_provider=None) -> None:
        self.queue = queue
        self.service = service
        self.batch_size = batch_size
        self.review_provider = review_provider

    async def process_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        if self.review_provider is not None:
            self.queue.resume_review_configuration(kind='maintenance')
        jobs = self.queue.claim(limit=self.batch_size, now=now, kind="maintenance")
        result = {"claimed": len(jobs), "applied": 0, "refused": 0, "empty": 0, "failed": 0}
        for job in jobs:
            outcome = await self._process_job(job)
            result[outcome] = result.get(outcome, 0) + 1
        return result

    async def _process_job(self, job: dict) -> str:
        job_id = str(job["job_id"])
        packet = MemoryTurnPacket.from_payload(json.loads(job["packet"]))
        suggestions = self.queue.stored_suggestions(job)
        if not suggestions:
            self.queue.complete(job_id, saved_ids=[])
            return "empty"
        scope = scope_from_packet(packet)
        from .memory_processing import MemoryReviewProcessor
        try:
            _, suggestions, processing = await MemoryReviewProcessor(self.service.writer, self.review_provider).prepare(
                packet, [], suggestions, cached=json.loads(job.get('review_results') or 'null'),
                persist=lambda payload: self.queue.save_review_results(job_id, payload))
        except Exception as exc:
            self.queue.fail(job_id, error=f'review_failed:{type(exc).__name__}: {exc}')
            return 'failed'
        applied_ids: list[str] = []
        failures: list[str] = []
        prior_receipts = self.queue.stored_receipts(job)
        collected_receipts: list = []
        try:
            for index, suggestion in enumerate(suggestions):
                outcome = await asyncio.to_thread(
                    self.service.apply,
                    suggestion,
                    scope=scope,
                    packet=packet,
                    turn_id=packet.turn_id,
                    project_id=packet.project_binding,
                    # 上一次已经做过的核对回执：内容与目标版本没变时直接复用。
                    prior_receipt=(
                        prior_receipts[index] if index < len(prior_receipts) else None
                    ),
                )
                collected_receipts.append(outcome.get("evidence_receipt"))
                if outcome.get("failure"):
                    # **执行失败不是业务拒绝**：数据库写不进去、向量挂了这类问题
                    # 重试之后可能就好了。错误留着，作业按次数重试；
                    # 当成拒绝并把作业标完成，用户那次纠正就再也没人管了。
                    failures.append(f"{suggestion.action}: {outcome.get('reason')}")
                    continue
                record = outcome.get("record")
                if outcome.get("applied") and record:
                    applied_ids.append(str(record["id"]))
                    processing.saved.append(record)
                else:
                    entry = {'content':suggestion.content, 'reason':outcome.get('reason'),
                             'evidence_receipt':outcome.get('evidence_receipt') or {}}
                    (processing.pending_review if outcome.get('pending_review') else processing.rejected).append(entry)
                    LOGGER.info(
                        "更新建议未应用：action=%s 原因=%s",
                        suggestion.action,
                        outcome.get("reason"),
                    )
                verify = outcome.get("verify") or {}
                if verify.get("partial"):
                    # 正文已保存、检索待修复：**分开记账**，由现有补偿机制重试，
                    # 不能因为记录写进去了就回执成整件事成功。
                    LOGGER.warning(
                        "写入部分完成：记录=%s 检索状态=%s",
                        verify.get("record_id"),
                        verify.get("retrieval"),
                    )
        except Exception as exc:  # noqa: BLE001 - 交给队列按次数重试
            if collected_receipts:
                # 已经核对过的那几条随作业留下：重试时内容没变就直接复用，
                # 不会因为数据库故障就换一套判据重算一遍。
                self.queue.save_receipts(job_id, collected_receipts)
            self.queue.fail(job_id, error=f"{type(exc).__name__}: {exc}")
            LOGGER.warning("内容维护应用失败：turn=%s 错误类型=%s", packet.turn_id, type(exc).__name__)
            return "failed"
        if failures:
            self.queue.save_receipts(job_id, collected_receipts)
            self.queue.fail(job_id, error="; ".join(failures)[:1000])
            LOGGER.warning(
                "内容维护执行失败，保留建议等待重试：turn=%s 失败=%d",
                packet.turn_id,
                len(failures),
            )
            return "failed"
        self.queue.finish_processing(job_id, processing, saved_ids=applied_ids)
        LOGGER.info('内容维护处理回执：turn=%s 保存=%d 拒绝=%d 待复核=%d', packet.turn_id,
                    len(applied_ids), len(processing.rejected), len(processing.pending_review))
        return 'pending_review' if processing.pending_review else ('applied' if applied_ids else 'refused')

    async def run_forever(self, *, interval_seconds: float = 30.0, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            await self.process_once()
            if stop is None:
                await asyncio.sleep(interval_seconds)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
                except asyncio.TimeoutError:
                    continue
