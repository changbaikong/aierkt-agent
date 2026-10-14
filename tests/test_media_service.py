"""
测试媒体任务服务
"""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.contracts import Scope
from agent_core.media_persistence import MediaTaskStore
from agent_core.media_service import MediaJobRequest, MediaTaskService


@pytest.fixture
def temp_dirs():
    """临时目录"""
    with TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        db_path = base / "media.db"
        artifact_dir = base / "artifacts"
        yield db_path, artifact_dir


@pytest.fixture
def store(temp_dirs):
    """测试用存储"""
    db_path, _ = temp_dirs
    return MediaTaskStore(db_path)


@pytest.fixture
def service(temp_dirs, store):
    """测试用服务"""
    _, artifact_dir = temp_dirs
    return MediaTaskService(
        store=store,
        artifact_dir=artifact_dir,
        tts_url="http://mock-tts:8766",
        tts_max_workers=2,
        tts_timeout=90,
        asr_url="http://mock-asr:8767",
        asr_max_workers=4,
        asr_timeout=120,
        artifact_expiry_hours=24,
    )


@pytest.fixture
def sample_scope():
    """示例 scope"""
    return Scope(
        owner_id="user123",
        channel="test_channel",
        conversation_id="conv456",
        module_id="media",
    )


@pytest.mark.asyncio
async def test_submit_tts_job(service, sample_scope):
    """测试提交 TTS 任务"""
    request = MediaJobRequest(
        kind="tts",
        scope=sample_scope,
        text="你好，世界",
        language="zh",
    )

    status = await service.submit_job(request)

    assert status.job_id.startswith("tts-")
    assert status.status == "queued"
    assert status.queued_at is not None


@pytest.mark.asyncio
async def test_get_job_status(service, sample_scope):
    """测试查询任务状态"""
    request = MediaJobRequest(
        kind="tts",
        scope=sample_scope,
        text="测试",
    )

    submit_status = await service.submit_job(request)
    job_id = submit_status.job_id

    retrieved_status = await service.get_job_status(job_id)
    assert retrieved_status is not None
    assert retrieved_status.job_id == job_id
    assert retrieved_status.status == "queued"


@pytest.mark.asyncio
async def test_cancel_queued_job(service, sample_scope):
    """测试取消队列中的任务"""
    request = MediaJobRequest(
        kind="tts",
        scope=sample_scope,
        text="测试取消",
    )

    submit_status = await service.submit_job(request)
    job_id = submit_status.job_id

    # 取消任务
    cancel_status = await service.cancel_job(job_id)
    assert cancel_status is not None
    assert cancel_status.status == "cancelled"
    assert cancel_status.completed_at is not None

    # 再次查询应该是 cancelled
    retrieved_status = await service.get_job_status(job_id)
    assert retrieved_status.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_nonexistent_job(service):
    """测试取消不存在的任务"""
    status = await service.cancel_job("nonexistent-job-id")
    assert status is None


@pytest.mark.asyncio
async def test_execute_tts_job_success(service, sample_scope, temp_dirs):
    """测试执行 TTS 任务成功"""
    _, artifact_dir = temp_dirs

    # Mock TTS 服务响应
    mock_audio_content = b"fake audio data"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = mock_audio_content

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        # 提交任务
        request = MediaJobRequest(
            kind="tts",
            scope=sample_scope,
            text="你好",
        )
        submit_status = await service.submit_job(request)
        job_id = submit_status.job_id

        # 获取任务记录并执行
        job_record = service.store.get_job(job_id)
        await service._execute_tts(job_record)

        # 验证任务完成
        final_status = await service.get_job_status(job_id)
        assert final_status.status == "completed"
        assert final_status.artifact_id is not None

        # 验证 artifact 文件存在
        artifact = await service.get_artifact(final_status.artifact_id, sample_scope)
        assert artifact is not None
        assert artifact.path.exists()
        assert artifact.path.read_bytes() == mock_audio_content


@pytest.mark.asyncio
async def test_execute_tts_job_failure(service, sample_scope):
    """测试执行 TTS 任务失败"""
    mock_response = MagicMock()
    mock_response.status_code = 500

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        request = MediaJobRequest(
            kind="tts",
            scope=sample_scope,
            text="失败测试",
        )
        submit_status = await service.submit_job(request)
        job_id = submit_status.job_id

        job_record = service.store.get_job(job_id)
        await service._execute_job(job_record)

        final_status = await service.get_job_status(job_id)
        assert final_status.status == "failed"
        assert final_status.error is not None


@pytest.mark.asyncio
async def test_worker_loop_processes_jobs(service, sample_scope):
    """测试 worker 循环处理任务"""
    mock_audio_content = b"test audio"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = mock_audio_content

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        # 启动服务
        await service.start()

        # 提交任务
        request = MediaJobRequest(
            kind="tts",
            scope=sample_scope,
            text="worker 测试",
        )
        submit_status = await service.submit_job(request)
        job_id = submit_status.job_id

        # 等待任务处理
        await asyncio.sleep(2)

        # 验证任务完成
        final_status = await service.get_job_status(job_id)
        assert final_status.status in ("completed", "running")  # 可能还在处理中

        # 停止服务
        await service.stop()


@pytest.mark.asyncio
async def test_artifact_scope_isolation(service):
    """测试 artifact scope 隔离"""
    scope1 = Scope(owner_id="user1", channel="ch1", conversation_id="conv1", module_id="media")
    scope2 = Scope(owner_id="user2", channel="ch2", conversation_id="conv2", module_id="media")

    mock_audio_content = b"test audio"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = mock_audio_content

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        # scope1 提交任务
        request1 = MediaJobRequest(
            kind="tts",
            scope=scope1,
            text="scope1 测试",
        )
        submit_status1 = await service.submit_job(request1)
        job_id1 = submit_status1.job_id

        # 执行任务
        job_record1 = service.store.get_job(job_id1)
        await service._execute_tts(job_record1)

        # 获取 artifact_id
        final_status1 = await service.get_job_status(job_id1)
        artifact_id = final_status1.artifact_id

        # scope1 可以访问
        artifact1 = await service.get_artifact(artifact_id, scope1)
        assert artifact1 is not None

        # scope2 无法访问
        artifact2 = await service.get_artifact(artifact_id, scope2)
        assert artifact2 is None


@pytest.mark.asyncio
async def test_concurrency_limit(service, sample_scope):
    """测试并发限制"""
    # TTS 最大并发是 2
    assert service.tts_semaphore._value == 2

    # 提交 5 个任务
    job_ids = []
    for i in range(5):
        request = MediaJobRequest(
            kind="tts",
            scope=sample_scope,
            text=f"并发测试 {i}",
        )
        status = await service.submit_job(request)
        job_ids.append(status.job_id)

    # 验证所有任务都已提交
    assert len(job_ids) == 5

    # 验证 semaphore 限制（实际执行时会生效）
    # 这里只测试初始值
    assert service.tts_semaphore._value == 2


@pytest.mark.asyncio
async def test_get_nonexistent_job(service):
    """测试查询不存在的任务"""
    status = await service.get_job_status("nonexistent-job-id")
    assert status is None


@pytest.mark.asyncio
async def test_get_nonexistent_artifact(service, sample_scope):
    """测试获取不存在的 artifact"""
    artifact = await service.get_artifact("nonexistent-artifact-id", sample_scope)
    assert artifact is None


@pytest.mark.asyncio
async def test_cleanup_expired_artifacts(service, sample_scope, temp_dirs):
    """测试清理过期 artifact"""
    _, artifact_dir = temp_dirs

    # 创建一个过期的 artifact
    from datetime import timedelta

    from agent_core.media_persistence import ArtifactRecord

    now = datetime.now(timezone.utc)
    expired_at = (now - timedelta(hours=1)).isoformat()

    # 创建文件
    test_file = artifact_dir / "user123" / "test_channel" / "expired.wav"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_bytes(b"expired audio")

    artifact = ArtifactRecord(
        artifact_id="expired-artifact",
        scope=sample_scope,
        media_type="audio/wav",
        size_bytes=13,
        file_path="user123/test_channel/expired.wav",
        created_at=(now - timedelta(hours=25)).isoformat(),
        expires_at=expired_at,
    )
    service.store.save_artifact(artifact)

    # 手动触发清理
    expired = service.store.list_expired_artifacts()
    for artifact in expired:
        file_path = service.artifact_dir / artifact.file_path
        if file_path.exists():
            file_path.unlink()
        service.store.delete_artifact(artifact.artifact_id)

    # 验证文件已删除
    assert not test_file.exists()

    # 验证记录已删除
    retrieved = service.store.get_artifact("expired-artifact", sample_scope)
    assert retrieved is None
