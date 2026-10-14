"""云端记忆工人：从接收队列取对话、提炼、入库、记录变更。

【和本地那套的关系】
本地不再提炼，只上传原始对话；提炼与轮转都在这里跑。
所以这个进程是「记忆正本」的唯一写入者 —— 它写的每次变更都会进
``memory_changes``，本地按编号合并回去。

【状态必须分三种，不能只分"成功/失败"】
``stored`` 提炼出记忆了；``empty`` 提炼成功但确实没内容；
``failed`` 调用或解析出错。以前后两者都记成 empty，于是一次网络超时
和一次真的"没内容"长得一模一样 —— 失败被当成完成，永远不会重试。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from agent_core.contracts import Scope
from agent_core.memory_extract import build_extractor_from_env, build_semantic_extractor_from_env
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_postprocess import extract_and_store_memory_turn
from agent_core.memory_repositories import MemoryRepositories
from agent_core.memory_switches import (
    cloud_processing_enabled,
    content_maintenance_enabled,
    semantic_memory_enabled,
)
from agent_core.memory_turn import MemoryTurnPacket, apply_packet_budget
from agent_core.memory_writes import EmbeddingRetryWorker, MemoryMaintenanceWorker, MemoryWriter
from agent_core.vector_config import QdrantConfig

from .store import ACTION_UPSERT, CloudMemoryStore

LOGGER = logging.getLogger("cloud-memory.worker")

BASE = Path(__file__).resolve().parents[1]
load_dotenv(BASE / ".env", override=False)

DEFAULT_INTERVAL_SECONDS = 30


class MaintenanceExecutionError(RuntimeError):
    """更新建议**执行失败**（不是业务拒绝）：可恢复，整轮按失败重试。

    和「证据不足被拒绝」分开：拒绝是这条建议本身不该落地，重试多少次都一样；
    执行失败是这次没写成，重试可能就好了。两者都不许悄悄变成「这轮没事」。
    """


def _vector_for(repositories: MemoryRepositories, record: dict) -> tuple[list[float] | None, str | None]:
    """取一条记录已经算好的向量，用来随变更回传本地。

    从 Qdrant 里读而不是重新调一次嵌入接口：向量刚由 ``MemoryWriter`` 算完并写进去，
    再调一次 API 是纯粹多花一次钱，还会因为两次调用的微小差异让两边向量不一致。
    """
    layer = record.get("embedding_layer")
    status = record.get("embedding_status")
    if not layer or status != "ready":
        return None, None
    try:
        points = repositories.vector_client.retrieve(
            f"{layer}_memories_vectors", ids=[str(record["id"])], with_vectors=True
        )
    except Exception as exc:  # noqa: BLE001 - 取不到向量不该让整次提炼失败
        LOGGER.warning("取向量失败：记录=%s 错误类型=%s", str(record.get("id"))[:8], type(exc).__name__)
        return None, None
    if not points:
        return None, None
    return list(points[0].vector or []), str(layer)


def packet_from_turn(turn: dict) -> MemoryTurnPacket:
    """接收队列里的一行 → 回合处理包。

    有处理包就用它（带必要历史、已用记忆、证据）。**没有的话不假装完整**：
    老的一问一答上传只能拼出一个缺背景的包，包上明写 ``partial``，
    让后面的提炼知道"用户可能还有约束没提供"。
    """
    raw = turn.get("packet")
    if raw:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            return MemoryTurnPacket.from_payload(payload)
        except Exception as exc:  # noqa: BLE001 - 包坏了就按缺背景处理，不能整轮丢
            LOGGER.warning("处理包无法解析，按缺背景处理：turn=%s 错误类型=%s", turn.get("turn_id"), type(exc).__name__)
    return MemoryTurnPacket(
        turn_id=str(turn.get("turn_id") or ""),
        occurred_at=str(turn.get("occurred_at") or ""),
        scope_owner=str(turn.get("owner_id") or ""),
        scope_channel=str(turn.get("channel") or "web"),
        scope_conversation=str(turn.get("conversation_id") or "default"),
        scope_module=str(turn.get("module_id") or "companion"),
        user_text=str(turn.get("user_text") or ""),
        published_reply=str(turn.get("assistant_reply") or ""),
        completeness="partial",
        omitted_sections=("relevant_history",),
    )


class ChangeRecorder:
    """把 ``MemoryWriter`` 的状态变化通知落成云端变更流水。"""

    def __init__(self, repositories: MemoryRepositories, store: CloudMemoryStore) -> None:
        self.repositories = repositories
        self.store = store

    def __call__(self, action: str, record: dict) -> None:
        vector, layer = (None, None)
        if action == ACTION_UPSERT:
            vector, layer = _vector_for(self.repositories, record)
        self.store.record_change(action, record, vector=vector, vector_layer=layer)


class CloudMemoryWorker:
    def __init__(
        self,
        repositories: MemoryRepositories,
        store: CloudMemoryStore,
        writer: MemoryWriter,
        extractor,
        *,
        semantic_extractor=None,
        maintenance=None,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        batch_size: int = 20,
    ) -> None:
        self.repositories = repositories
        self.store = store
        self.writer = writer
        self.extractor = extractor
        # 内容维护服务：更新建议的应用口。云端没接上时只在日志里报数，不静默丢。
        self.maintenance = maintenance
        # 新版语义提炼是**另一条路**：开关打开时只跑它，旧陪伴提炼器退回兼容备用。
        # 开关开着但提炼器没配上（云端拿不到主回答模型配置）时，走失败通道留痕，
        # 不静默换回旧模型 —— 那会让两端的结果差异无从解释。
        #
        # 还要认「处理端指定」：指定本地时云端不碰新版提炼，否则同一个包两端
        # 各提炼一遍（B-R4 的另一半）。
        self.use_semantic = cloud_processing_enabled()
        self.semantic_extractor = semantic_extractor
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self._stop = asyncio.Event()
        self.stats = {
            "turns": 0,
            "stored": 0,
            "empty": 0,
            "failed": 0,
            "duration_ms": 0,
            # 内容维护单独记账：变更条数与拒绝原因要能分开看，
            # 混进候选的计数里就说不清「今天到底改了几条旧记忆」。
            "maintenance_applied": 0,
            "maintenance_refused": 0,
            # 只改旧记忆、没有新候选的轮次：和「什么都没有」分开。
            "maintained": 0,
        }

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # 上次崩在「提炼到一半」的轮次先捡回来，否则它们既不在待处理里、
        # 也没人再碰它们 —— 表现是几轮对话永远没被提炼，日志里一声不吭。
        recovered = await asyncio.to_thread(self.store.requeue_stale)
        if recovered:
            LOGGER.warning("把 %d 条中断的处理中任务放回队列", recovered)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - 一轮出错不能让服务退出
                LOGGER.exception("云端记忆工人这一轮失败")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> dict:
        if getattr(self.semantic_extractor, 'provider', None) is not None:
            await asyncio.to_thread(self.store.resume_review_configuration)
        # 先把访问记录并进记忆，再处理新对话：轮转（每 60 秒一次）会按
        # 「多久没用过」决定淘汰谁，访问信息晚一步并入，淘汰判断就晚一步准确。
        touched = await asyncio.to_thread(self.store.apply_access)
        if touched:
            LOGGER.info("并入 %d 条访问记录", touched)
        turns = await asyncio.to_thread(self.store.claim_due, limit=self.batch_size)
        for turn in turns:
            await self._process(turn)
        return dict(self.stats)

    async def _process(self, turn: dict) -> None:
        turn_id = str(turn["turn_id"])
        scope = Scope(
            owner_id=str(turn["owner_id"]),
            channel=str(turn["channel"]),
            conversation_id=str(turn["conversation_id"]),
            module_id=str(turn["module_id"]),
        )
        started = datetime.now(UTC)
        if self.use_semantic:
            await self._process_semantic(turn, scope, turn_id, started)
            return
        try:
            result = await extract_and_store_memory_turn(
                self.extractor,
                self.writer,
                scope,
                str(turn["user_text"] or ""),
                str(turn["assistant_reply"] or ""),
                turn_id=turn_id,
                occurred_at=str(turn["occurred_at"] or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 记失败并等下次重试
            state = await asyncio.to_thread(self.store.mark_failed, turn_id, f"{type(exc).__name__}: {exc}")
            self.stats["failed"] += 1
            LOGGER.warning("提炼失败：turn=%s 状态=%s 错误=%s", turn_id, state, type(exc).__name__)
            return

        elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self.stats["duration_ms"] += elapsed
        self.stats["turns"] += 1
        status = str(result.get("status") or "")
        if status == "stored":
            await asyncio.to_thread(
                self.store.mark_stored,
                turn_id,
                extracted=int(result.get("extracted") or 0),
                persisted=int(result.get("persisted") or 0),
            )
            self.stats["stored"] += 1
        elif status == "empty":
            await asyncio.to_thread(self.store.mark_empty, turn_id)
            self.stats["empty"] += 1
        else:
            # skipped 说明提炼器没配上（缺 key 之类）—— 那是配置问题，
            # 必须走失败通道留痕，不能当成"这轮没内容"悄悄过去。
            state = await asyncio.to_thread(
                self.store.mark_failed, turn_id, f"未预期的提炼结果：{status or '未知'}"
            )
            self.stats["failed"] += 1
            LOGGER.warning("提炼未产生结果：turn=%s 状态=%s 结果=%s", turn_id, state, status)

    async def _process_semantic(self, turn: dict, scope: Scope, turn_id: str, started: datetime) -> None:
        """新版路径：处理包 → 七类候选 → 校验写入。

        提炼器缺失是**配置问题**，不是"这轮没内容"：走失败通道留痕，
        积压条数在队列里看得见，不会悄悄丢。
        """
        if self.semantic_extractor is None:
            await asyncio.to_thread(
                self.store.mark_failed, turn_id, "semantic_extractor_not_configured"
            )
            self.stats["failed"] += 1
            LOGGER.warning("候选提炼器未配置：turn=%s（云端缺主回答模型配置？）", turn_id)
            return
        packet, budget_report = apply_packet_budget(packet_from_turn(turn))
        if budget_report.get("needs_retry"):
            LOGGER.warning("处理包超出预算，本轮标记为缺背景：turn=%s", turn_id)
        if packet.completeness != "complete":
            LOGGER.info("按缺背景处理：turn=%s 缺口=%s", turn_id, ",".join(packet.omitted_sections))
        stored = self.store.stored_extraction(turn)
        records: list = []
        maintained = 0
        try:
            if stored["done"]:
                # 上一次已经提炼过：复用落库的产物重试，不再调模型。
                candidates = stored["candidates"]
                suggestions = stored["suggestions"]
                LOGGER.info(
                    "复用已落库的提炼结果重试：turn=%s 候选=%d 更新建议=%d",
                    turn_id,
                    len(candidates),
                    len(suggestions),
                )
            elif hasattr(self.semantic_extractor, "extract_maintenance"):
                candidates, suggestions = await self.semantic_extractor.extract_maintenance(
                    packet,
                    known_ids=[ref.memory_id for ref in packet.recalled_memory_refs],
                    known_versions={
                        ref.memory_id: int(ref.version) for ref in packet.recalled_memory_refs
                    },
                )
            else:
                candidates = await self.semantic_extractor.extract(
                    packet, known_ids=[ref.memory_id for ref in packet.recalled_memory_refs]
                )
                suggestions = []
            if not stored["done"]:
                # 提炼产物先落库：后面写库或应用更新建议失败时，重试复用这一份，
                # 不重新调模型，也不让同一动作的幂等键换一个样子。
                await asyncio.to_thread(
                    self.store.save_extraction, turn_id, candidates, suggestions
                )
            from agent_core.memory_processing import MemoryReviewProcessor
            original_candidate_count = len(candidates)
            candidates, suggestions, processing = await MemoryReviewProcessor(
                self.writer, getattr(self.semantic_extractor, 'provider', None)).prepare(
                    packet, candidates, suggestions, cached=json.loads(turn.get('review_results') or 'null'),
                    persist=lambda payload: self.store.save_review_results(turn_id, payload))
            records = (
                await asyncio.to_thread(
                    self.writer.process_semantic_candidates,
                    scope,
                    candidates,
                    turn_id=turn_id,
                    occurred_at=packet.occurred_at,
                    known_ids=[ref.memory_id for ref in packet.recalled_memory_refs],
                    # 可信项目绑定从包里传下去，不重新读本端环境变量：两端环境
                    # 不同时，同一个包会存成不同范围，同项目的新会话接不上。
                    # 包里确实没有绑定时才退回本端运行配置。
                    project_binding=packet.project_binding or None,
                    # 处理包一起传下去：寿命规则与替代依据核对都看用户原话，
                    # 本地与云端必须走同一条判断。
                    packet=packet,
                )
                if candidates
                else []
            )
            if candidates:
                committed = records
                records = committed.saved
                processing.checks.extend(committed.checks)
                processing.rejected.extend(committed.rejected)
                processing.pending_review.extend(committed.pending_review)
            processing.saved = list(records)
            if suggestions:
                maintained = await self._apply_suggestions(
                    suggestions, scope=scope, packet=packet, turn_id=turn_id, processing=processing
                )
        except Exception as exc:  # noqa: BLE001 - 记失败并等下次重试
            state = await asyncio.to_thread(self.store.mark_failed, turn_id, f"{type(exc).__name__}: {exc}")
            self.stats["failed"] += 1
            LOGGER.warning("提炼失败：turn=%s 状态=%s 错误=%s", turn_id, state, type(exc).__name__)
            return

        elapsed = int((datetime.now(UTC) - started).total_seconds() * 1000)
        self.stats["duration_ms"] += elapsed
        self.stats["turns"] += 1
        if records:
            await asyncio.to_thread(
                self.store.mark_stored, turn_id, extracted=len(candidates), persisted=len(records)
            )
            self.stats["stored"] += 1
        elif maintained:
            # 没有新候选，但这次确实改了已有记忆：不能记成「什么都没有」。
            await asyncio.to_thread(self.store.mark_maintained, turn_id, applied=maintained)
            self.stats["maintained"] += 1
        else:
            if not processing.pending_review and not processing.rejected:
                await asyncio.to_thread(self.store.mark_empty, turn_id)
                self.stats["empty"] += 1
        self.stats['maintenance_refused'] += sum(1 for r in processing.rejected if r.get('index', -1) >= original_candidate_count)
        await asyncio.to_thread(self.store.save_processing_result, turn_id, processing)

    async def _apply_suggestions(self, suggestions, *, scope: Scope, packet, turn_id: str, processing=None) -> int:
        """把更新建议应用到库里；返回**本次真正改动**的条数。

        应用是**幂等**的：同一条建议算同一个幂等键，所以这一轮失败后重试不会
        造出第二条版本。没接上维护服务时如实报数，不静默丢。

        **执行失败不是业务拒绝**：数据库写不进去这类问题重试之后可能就好了，
        这里抛出去让整轮走失败通道重试（重试复用已落库的建议，不重新调模型）。
        把执行失败只记一行日志、然后照常结束这轮，用户那次纠正就再也没人管了。
        """
        if self.maintenance is None:
            LOGGER.warning(
                "本次提炼含 %d 条内容更新建议，云端未接入内容维护，先只记录：turn=%s",
                len(suggestions),
                turn_id,
            )
            return 0
        applied_count = 0
        for suggestion in suggestions:
            outcome = await asyncio.to_thread(
                self.maintenance.apply,
                suggestion,
                scope=scope,
                packet=packet,
                turn_id=turn_id,
                project_id=packet.project_binding,
            )
            if outcome.get("failure"):
                raise MaintenanceExecutionError(
                    f"{suggestion.action}: {outcome.get('reason')}"
                )
            if outcome.get("applied"):
                if processing is not None and outcome.get('record'):
                    processing.saved.append(outcome['record'])
                # 幂等重放不再计数：那条变更上一轮已经记过了。
                if not outcome.get("replayed"):
                    applied_count += 1
                self.stats["maintenance_applied"] += 1
            else:
                if processing is not None:
                    entry = {'content':suggestion.content, 'reason':outcome.get('reason'), 'evidence_receipt':outcome.get('evidence_receipt') or {}}
                    (processing.pending_review if outcome.get('pending_review') else processing.rejected).append(entry)
                self.stats["maintenance_refused"] += 1
                LOGGER.info(
                    "更新建议未应用：action=%s 原因=%s", suggestion.action, outcome.get("reason")
                )
        return applied_count

    def summary(self) -> dict:
        turns = int(self.stats["turns"])
        return {
            **self.stats,
            "avg_duration_ms": int(self.stats["duration_ms"] / turns) if turns else 0,
        }


def build_worker(store: CloudMemoryStore, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS) -> CloudMemoryWorker:
    db_path = os.environ.get("MEMORY_DB_PATH", str(BASE / "data" / "memory_records.db"))
    repositories = MemoryRepositories(db_path, vector_config=QdrantConfig.from_env())
    repositories.initialize()
    extractor = build_extractor_from_env()
    semantic_extractor = build_semantic_extractor_from_env() if semantic_memory_enabled() else None
    if extractor is None and semantic_extractor is None:
        raise SystemExit("EXTRACT_API_KEY 未配置，拒绝启动提炼工人")
    writer = MemoryWriter(
        repositories,
        _embeddings_from_env(),
        on_change=ChangeRecorder(repositories, store),
    )
    return CloudMemoryWorker(
        repositories,
        store,
        writer,
        extractor,
        semantic_extractor=semantic_extractor,
        # 内容维护走独立开关：关掉它只停止对已有记录提更新，候选照常。
        maintenance=(
            MemoryMaintenanceService(writer, repositories)
            if content_maintenance_enabled()
            else None
        ),
        interval_seconds=interval_seconds,
    )


def _embeddings_from_env():
    from agent_core_adapters.bigmodel_embeddings import BigModelEmbeddingProvider

    return BigModelEmbeddingProvider.from_env()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = CloudMemoryStore(os.environ.get("MEMORY_DB_PATH", str(BASE / "data" / "memory_records.db")))
    store.initialize()
    worker = build_worker(store)
    maintenance = MemoryMaintenanceWorker(worker.writer)
    embedding = EmbeddingRetryWorker(worker.writer)
    maintenance.start()
    embedding.start()
    LOGGER.info(
        "云端记忆工人启动：db=%s 轮询=%ss 批=%d",
        store.db_path,
        worker.interval_seconds,
        worker.batch_size,
    )
    try:
        await worker.run()
    finally:
        maintenance.stop(timeout=5)
        embedding.stop(timeout=5)


if __name__ == "__main__":
    asyncio.run(_main())
