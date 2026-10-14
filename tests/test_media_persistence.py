"""
测试媒体任务持久化
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from agent_core.contracts import Scope
from agent_core.media_persistence import (
    ArtifactRecord,
    MediaJobRecord,
    MediaTaskStore,
)


@pytest.fixture
def temp_db():
    """临时数据库"""
    with TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_media.db"
        yield db_path


@pytest.fixture
def store(temp_db):
    """测试用存储"""
    return MediaTaskStore(temp_db)


@pytest.fixture
def sample_scope():
    """示例 scope"""
    return Scope(
        owner_id="user123",
        channel="test_channel",
        conversation_id="conv456",
        module_id="media",
    )


def test_init_creates_tables(temp_db):
    """测试初始化创建表"""
    store = MediaTaskStore(temp_db)

    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('media_jobs', 'media_artifacts')"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()

    assert "media_jobs" in tables
    assert "media_artifacts" in tables


def test_save_and_get_job(store, sample_scope):
    """测试保存和获取任务"""
    now = datetime.now(timezone.utc).isoformat()

    job = MediaJobRecord(
        job_id="tts-20260820-abc123",
        kind="tts",
        status="queued",
        scope=sample_scope,
        request_json='{"text": "hello"}',
        queued_at=now,
    )

    store.save_job(job)

    retrieved = store.get_job("tts-20260820-abc123")
    assert retrieved is not None
    assert retrieved.job_id == "tts-20260820-abc123"
    assert retrieved.kind == "tts"
    assert retrieved.status == "queued"
    assert retrieved.scope.owner_id == "user123"
    assert retrieved.request_json == '{"text": "hello"}'


def test_get_job_rejects_different_scope(store, sample_scope):
    now = datetime.now(timezone.utc).isoformat()
    job = MediaJobRecord("scoped-job", "tts", "queued", sample_scope, "{}", queued_at=now)
    store.save_job(job)

    other_scope = Scope("other", sample_scope.channel, sample_scope.conversation_id, sample_scope.module_id)
    assert store.get_job("scoped-job", other_scope) is None


def test_claim_queued_job_is_atomic_and_scope_bound(store, sample_scope):
    now = datetime.now(timezone.utc).isoformat()
    job = MediaJobRecord("claim-job", "tts", "queued", sample_scope, "{}", queued_at=now)
    store.save_job(job)

    claimed = store.claim_queued_job("claim-job", sample_scope, lease_seconds=60)
    assert claimed is not None
    assert claimed.claim_id
    assert store.claim_queued_job("claim-job", sample_scope, lease_seconds=60) is None
    assert store.get_job("claim-job", sample_scope).status == "running"

    other_scope = Scope("other", sample_scope.channel, sample_scope.conversation_id, sample_scope.module_id)
    assert store.claim_queued_job("claim-job", other_scope, lease_seconds=60) is None


def test_update_job_status(store, sample_scope):
    """测试更新任务状态"""
    now = datetime.now(timezone.utc).isoformat()

    job = MediaJobRecord(
        job_id="tts-20260820-xyz789",
        kind="tts",
        status="queued",
        scope=sample_scope,
        request_json='{"text": "test"}',
        queued_at=now,
    )

    store.save_job(job)

    # 更新状态
    job.status = "running"
    job.started_at = now
    store.save_job(job)

    retrieved = store.get_job("tts-20260820-xyz789")
    assert retrieved.status == "running"
    assert retrieved.started_at == now


def test_list_jobs_by_status(store, sample_scope):
    """测试按状态列出任务"""
    now = datetime.now(timezone.utc).isoformat()

    # 创建多个任务
    for i, status in enumerate(["queued", "queued", "running", "completed"]):
        job = MediaJobRecord(
            job_id=f"job-{i}",
            kind="tts",
            status=status,
            scope=sample_scope,
            request_json='{}',
            queued_at=now,
        )
        store.save_job(job)

    queued = store.list_jobs_by_status("queued")
    assert len(queued) == 2

    running = store.list_jobs_by_status("running")
    assert len(running) == 1

    completed = store.list_jobs_by_status("completed")
    assert len(completed) == 1


def test_save_and_get_artifact(store, sample_scope):
    """测试保存和获取 artifact"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=24)

    artifact = ArtifactRecord(
        artifact_id="art-20260820-abc123",
        scope=sample_scope,
        media_type="audio/wav",
        size_bytes=12345,
        file_path="user123/test_channel/art-20260820-abc123.wav",
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )

    store.save_artifact(artifact)

    retrieved = store.get_artifact("art-20260820-abc123", sample_scope)
    assert retrieved is not None
    assert retrieved.artifact_id == "art-20260820-abc123"
    assert retrieved.media_type == "audio/wav"
    assert retrieved.size_bytes == 12345


def test_artifact_scope_isolation(store):
    """测试 artifact scope 隔离"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=24)

    scope1 = Scope(owner_id="user1", channel="ch1", conversation_id="conv1", module_id="media")
    scope2 = Scope(owner_id="user2", channel="ch2", conversation_id="conv2", module_id="media")

    artifact = ArtifactRecord(
        artifact_id="art-shared",
        scope=scope1,
        media_type="audio/wav",
        size_bytes=1000,
        file_path="test.wav",
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )

    store.save_artifact(artifact)

    # scope1 可以访问
    retrieved1 = store.get_artifact("art-shared", scope1)
    assert retrieved1 is not None

    # scope2 无法访问
    retrieved2 = store.get_artifact("art-shared", scope2)
    assert retrieved2 is None


def test_list_expired_artifacts(store, sample_scope):
    """测试列出过期 artifact"""
    now = datetime.now(timezone.utc)

    # 已过期
    expired1 = ArtifactRecord(
        artifact_id="art-expired-1",
        scope=sample_scope,
        media_type="audio/wav",
        size_bytes=1000,
        file_path="expired1.wav",
        created_at=(now - timedelta(hours=25)).isoformat(),
        expires_at=(now - timedelta(hours=1)).isoformat(),
    )

    # 未过期
    valid = ArtifactRecord(
        artifact_id="art-valid",
        scope=sample_scope,
        media_type="audio/wav",
        size_bytes=1000,
        file_path="valid.wav",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=24)).isoformat(),
    )

    store.save_artifact(expired1)
    store.save_artifact(valid)

    expired = store.list_expired_artifacts()
    assert len(expired) == 1
    assert expired[0].artifact_id == "art-expired-1"


def test_delete_artifact(store, sample_scope):
    """测试删除 artifact"""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=24)

    artifact = ArtifactRecord(
        artifact_id="art-to-delete",
        scope=sample_scope,
        media_type="audio/wav",
        size_bytes=1000,
        file_path="delete.wav",
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )

    store.save_artifact(artifact)
    assert store.get_artifact("art-to-delete", sample_scope) is not None

    store.delete_artifact("art-to-delete")
    assert store.get_artifact("art-to-delete", sample_scope) is None


def test_delete_old_jobs(store, sample_scope):
    """测试删除旧任务"""
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(days=8)).isoformat()
    recent_time = (now - timedelta(days=1)).isoformat()

    # 旧的已完成任务
    old_job = MediaJobRecord(
        job_id="old-completed",
        kind="tts",
        status="completed",
        scope=sample_scope,
        request_json='{}',
        queued_at=old_time,
        completed_at=old_time,
    )

    # 最近的已完成任务
    recent_job = MediaJobRecord(
        job_id="recent-completed",
        kind="tts",
        status="completed",
        scope=sample_scope,
        request_json='{}',
        queued_at=recent_time,
        completed_at=recent_time,
    )

    # 旧的正在运行任务（不应删除）
    old_running = MediaJobRecord(
        job_id="old-running",
        kind="tts",
        status="running",
        scope=sample_scope,
        request_json='{}',
        queued_at=old_time,
        started_at=old_time,
    )

    store.save_job(old_job)
    store.save_job(recent_job)
    store.save_job(old_running)

    # 删除 7 天前的任务
    cutoff = (now - timedelta(days=7)).isoformat()
    deleted_count = store.delete_old_jobs(cutoff)

    assert deleted_count == 1
    assert store.get_job("old-completed") is None
    assert store.get_job("recent-completed") is not None
    assert store.get_job("old-running") is not None


def test_wal_mode_enabled(temp_db):
    """测试 WAL 模式已启用"""
    store = MediaTaskStore(temp_db)

    conn = sqlite3.connect(temp_db)
    cursor = conn.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    conn.close()

    assert mode.lower() == "wal"


def test_concurrent_writes(temp_db, sample_scope):
    """测试并发写入"""
    store = MediaTaskStore(temp_db)

    # 模拟并发写入
    now = datetime.now(timezone.utc).isoformat()

    for i in range(10):
        job = MediaJobRecord(
            job_id=f"concurrent-{i}",
            kind="tts",
            status="queued",
            scope=sample_scope,
            request_json='{}',
            queued_at=now,
        )
        store.save_job(job)

    # 验证所有任务都已保存
    queued = store.list_jobs_by_status("queued")
    assert len(queued) >= 10
