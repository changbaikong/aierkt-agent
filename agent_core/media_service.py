"""
媒体任务服务核心

协调 TTS/ASR 任务的提交、执行、状态跟踪和 artifact 管理。
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import httpx

from agent_core.contracts import Scope
from agent_core.media_persistence import (
    ArtifactRecord,
    MediaJobRecord,
    MediaTaskStore,
)


@dataclass
class MediaJobRequest:
    """媒体任务请求"""
    kind: Literal["tts", "asr"]
    scope: Scope
    # TTS 参数
    text: str | None = None
    language: str = "zh"
    voice: str | None = None
    # ASR 参数
    input_artifact_id: str | None = None
    # 通用
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaJobStatus:
    """媒体任务状态"""
    job_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    artifact_id: str | None = None
    transcript: str | None = None
    error: str | None = None
    queued_at: str = ""
    started_at: str | None = None
    completed_at: str | None = None


@dataclass
class Artifact:
    """Artifact 元信息"""
    artifact_id: str
    scope: Scope
    media_type: str
    size_bytes: int
    created_at: str
    expires_at: str
    path: Path


class MediaTaskService:
    """媒体任务服务"""

    def __init__(
        self,
        store: MediaTaskStore,
        artifact_dir: Path,
        tts_url: str = "http://127.0.0.1:8766",
        tts_max_workers: int = 2,
        tts_timeout: int = 90,
        asr_url: str | None = None,
        asr_max_workers: int = 4,
        asr_timeout: int = 120,
        artifact_expiry_hours: int = 24,
    ):
        self.store = store
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        # TTS 配置
        self.tts_url = tts_url
        self.tts_semaphore = asyncio.Semaphore(tts_max_workers)
        self.tts_timeout = tts_timeout

        # ASR 配置
        self.asr_url = asr_url
        self.asr_semaphore = asyncio.Semaphore(asr_max_workers)
        self.asr_timeout = asr_timeout

        # Artifact 过期时间
        self.artifact_expiry_hours = artifact_expiry_hours

        # 任务取消标记
        self._cancelled_jobs: set[str] = set()

        # 后台任务
        self._worker_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._execution_tasks: set[asyncio.Task] = set()
        self._started = False

    async def start(self) -> None:
        """启动后台任务"""
        if self._started and self._worker_task is not None and not self._worker_task.done():
            return
        # 同步 SQLite：启动发生在 lifespan 里，直接在 loop 上调它会把
        # "服务开始接受请求"推迟一个 recover 扫描的时间。走工作线程。
        await asyncio.to_thread(self.store.recover_expired_claims)
        self._started = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        """停止后台任务"""
        if not self._started:
            return
        self._started = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        tasks = list(self._execution_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._execution_tasks.clear()

    async def submit_job(self, request: MediaJobRequest) -> MediaJobStatus:
        """提交任务。**存储是同步 SQLite，一律走工作线程。**

        这个方法由 HTTP 接口直接调用，在 loop 上做一次写库会让同时进行的
        聊天流与 SSE 一起等这一次落盘。
        """
        if request.kind == "asr":
            if not request.input_artifact_id:
                raise ValueError("input_artifact_id is required")
            if await asyncio.to_thread(self.store.get_artifact, request.input_artifact_id, request.scope) is None:
                raise ValueError("input artifact does not exist or is not accessible")
        job_id = f"{request.kind}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        record = MediaJobRecord(
            job_id=job_id,
            kind=request.kind,
            status="queued",
            scope=request.scope,
            request_json=json.dumps({
                "text": request.text,
                "language": request.language,
                "voice": request.voice,
                "input_artifact_id": request.input_artifact_id,
                "options": request.options,
            }),
            queued_at=now,
        )

        await asyncio.to_thread(self.store.save_job, record)

        return MediaJobStatus(
            job_id=job_id,
            status="queued",
            queued_at=now,
        )

    async def get_job_status(self, job_id: str, scope: Scope | None = None) -> MediaJobStatus | None:
        """查询任务状态"""
        record = await asyncio.to_thread(self.store.get_job, job_id, scope)
        if not record:
            return None

        return MediaJobStatus(
            job_id=record.job_id,
            status=record.status,
            artifact_id=record.artifact_id,
            transcript=record.transcript,
            error=record.error,
            queued_at=record.queued_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )

    async def cancel_job(self, job_id: str, scope: Scope | None = None) -> MediaJobStatus | None:
        """取消任务"""
        record = await asyncio.to_thread(self.store.get_job, job_id, scope)
        if not record:
            return None

        if record.status in ("completed", "failed", "cancelled"):
            # 已终止的任务不能取消
            return MediaJobStatus(
                job_id=record.job_id,
                status=record.status,
                artifact_id=record.artifact_id,
                transcript=record.transcript,
                error=record.error,
                queued_at=record.queued_at,
                started_at=record.started_at,
                completed_at=record.completed_at,
            )

        self._cancelled_jobs.add(job_id)
        if scope is None:
            # Legacy internal callers should not be used by request handlers.
            scope = record.scope
        cancelled = await asyncio.to_thread(self.store.cancel_job, job_id, scope)
        if cancelled is not None:
            record = cancelled

        return MediaJobStatus(
            job_id=record.job_id,
            status=record.status,
            queued_at=record.queued_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
        )

    async def get_artifact(self, artifact_id: str, scope: Scope) -> Artifact | None:
        """获取 artifact"""
        record = await asyncio.to_thread(self.store.get_artifact, artifact_id, scope)
        if not record:
            return None

        file_path = self.artifact_dir / record.file_path
        if not await asyncio.to_thread(file_path.exists):
            return None

        return Artifact(
            artifact_id=record.artifact_id,
            scope=record.scope,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            created_at=record.created_at,
            expires_at=record.expires_at,
            path=file_path,
        )

    async def _worker_loop(self) -> None:
        """Worker 循环：处理队列中的任务

        存储调用全部走工作线程：这个循环每秒醒一次，占着 loop 会连带卡住
        SSE 推送与聊天请求 —— 而它做的事只是"扫一眼队列有没有活"。
        """
        while True:
            try:
                await asyncio.to_thread(self.store.recover_expired_claims)
                # 获取队列中的任务
                queued_jobs = await asyncio.to_thread(self.store.list_jobs_by_status, "queued", limit=10)

                for job_record in queued_jobs:
                    if job_record.job_id in self._cancelled_jobs:
                        continue
                    claimed = await asyncio.to_thread(
                        self.store.claim_queued_job, job_record.job_id, job_record.scope
                    )
                    if claimed is None:
                        continue
                    task = asyncio.create_task(self._execute_job(claimed))
                    self._execution_tasks.add(task)
                    task.add_done_callback(self._execution_tasks.discard)

                await asyncio.sleep(1)  # 轮询间隔

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Worker loop error: {e}")
                await asyncio.sleep(5)

    async def _execute_job(self, job_record: MediaJobRecord) -> None:
        """执行单个已 claim 的任务。"""
        if not job_record.claim_id:
            claimed = await asyncio.to_thread(
                self.store.claim_queued_job, job_record.job_id, job_record.scope
            )
            if claimed is None:
                return
            job_record = claimed

        # 检查是否已取消
        if job_record.job_id in self._cancelled_jobs:
            job_record.status = "cancelled"
            job_record.completed_at = datetime.now(timezone.utc).isoformat()
            await self._save_terminal(job_record)
            self._cancelled_jobs.discard(job_record.job_id)
            return

        try:
            if job_record.kind == "tts":
                await self._execute_tts(job_record)
            elif job_record.kind == "asr":
                await self._execute_asr(job_record)
            else:
                raise ValueError(f"Unknown job kind: {job_record.kind}")

        except asyncio.CancelledError:
            # 任务被取消
            job_record.status = "cancelled"
            job_record.completed_at = datetime.now(timezone.utc).isoformat()
            await self._save_terminal(job_record)
            self._cancelled_jobs.discard(job_record.job_id)

        except Exception as e:
            # 任务失败
            job_record.status = "failed"
            job_record.error = str(e)
            job_record.completed_at = datetime.now(timezone.utc).isoformat()
            await self._save_terminal(job_record)

    async def _save_terminal(self, job_record: MediaJobRecord) -> None:
        """把任务终态写进存储。同步 SQLite → 工作线程。"""
        finish = getattr(self.store, "finish_claimed_job", None)
        if callable(finish) and job_record.claim_id:
            await asyncio.to_thread(finish, job_record)
        else:
            await asyncio.to_thread(self.store.save_job, job_record)

    async def _execute_tts(self, job_record: MediaJobRecord) -> None:
        """执行 TTS 任务"""
        if not job_record.claim_id:
            claimed = await asyncio.to_thread(
                self.store.claim_queued_job, job_record.job_id, job_record.scope
            )
            if claimed is None:
                return
            job_record = claimed
        async with self.tts_semaphore:
            # 检查取消
            if job_record.job_id in self._cancelled_jobs:
                raise asyncio.CancelledError()

            # 解析请求参数
            request_data = json.loads(job_record.request_json)
            text = request_data["text"]

            # 调用 TTS 服务
            async with httpx.AsyncClient(timeout=self.tts_timeout) as client:
                response = await client.post(
                    f"{self.tts_url}/tts",
                    json={"text": text},
                )

            if response.status_code != 200:
                raise RuntimeError(f"TTS service returned {response.status_code}")

            # 保存 artifact：建目录 + 写文件都是同步磁盘 IO，一起挪到工作线程。
            artifact_id = f"art-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
            relative_path = f"{job_record.scope.owner_id}/{job_record.scope.channel}/{artifact_id}.wav"
            file_path = self.artifact_dir / relative_path
            await asyncio.to_thread(file_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(file_path.write_bytes, response.content)

            # 保存 artifact 记录
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(hours=self.artifact_expiry_hours)

            artifact_record = ArtifactRecord(
                artifact_id=artifact_id,
                scope=job_record.scope,
                media_type="audio/wav",
                size_bytes=len(response.content),
                file_path=relative_path,
                created_at=now.isoformat(),
                expires_at=expires_at.isoformat(),
            )
            await asyncio.to_thread(self.store.save_artifact, artifact_record)

            # 更新任务状态
            job_record.status = "completed"
            job_record.artifact_id = artifact_id
            job_record.completed_at = datetime.now(timezone.utc).isoformat()
            await self._save_terminal(job_record)

    async def _execute_asr(self, job_record: MediaJobRecord) -> None:
        """执行 ASR 任务（待实现）"""
        if not job_record.claim_id:
            claimed = await asyncio.to_thread(
                self.store.claim_queued_job, job_record.job_id, job_record.scope
            )
            if claimed is None:
                return
            job_record = claimed
        async with self.asr_semaphore:
            if job_record.job_id in self._cancelled_jobs:
                raise asyncio.CancelledError()

            # TODO: 实现 ASR 逻辑
            raise NotImplementedError("ASR not implemented yet")

    async def _cleanup_loop(self) -> None:
        """清理循环：删除过期 artifact 和旧任务

        存储与文件删除全走工作线程：每小时醒一次只是节奏，真正会卡住 loop 的是
        "扫一遍过期 artifact 再逐个 unlink" —— 那就是几百次同步磁盘往返。
        """
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时清理一次

                # 清理过期 artifact
                expired = await asyncio.to_thread(self.store.list_expired_artifacts)
                for artifact in expired:
                    file_path = self.artifact_dir / artifact.file_path
                    if await asyncio.to_thread(file_path.exists):
                        await asyncio.to_thread(file_path.unlink)
                    await asyncio.to_thread(self.store.delete_artifact, artifact.artifact_id)

                # 清理 7 天前的旧任务
                cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                await asyncio.to_thread(self.store.delete_old_jobs, cutoff)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"Cleanup loop error: {e}")
