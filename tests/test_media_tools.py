"""
测试媒体工具
"""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.contracts import EFFECT_PERSISTENT, Scope
from agent_core.media_persistence import ArtifactRecord, MediaTaskStore
from agent_core.media_service import MediaTaskService
from agent_tools.media import (
    make_media_cancel_tool,
    make_media_get_artifact_tool,
    make_media_get_status_tool,
    make_media_submit_asr_tool,
    make_media_submit_tts_tool,
)


@pytest.fixture
def temp_dirs():
    """临时目录"""
    with TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        db_path = base / "media.db"
        artifact_dir = base / "artifacts"
        yield db_path, artifact_dir


@pytest.fixture
def service(temp_dirs):
    """测试用服务"""
    db_path, artifact_dir = temp_dirs
    store = MediaTaskStore(db_path)
    return MediaTaskService(
        store=store,
        artifact_dir=artifact_dir,
        tts_url="http://mock-tts:8766",
        tts_max_workers=2,
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
async def test_submit_tts_tool(service, sample_scope):
    """测试 TTS 提交工具"""
    tool = make_media_submit_tts_tool(service)

    result = await tool.handler(text="你好，世界", voice=None, scope=sample_scope)

    assert result.status == "ok"
    assert "job_id" in result.output
    assert result.output["job_id"].startswith("tts-")
    assert "TTS 任务已提交" in result.output["message"]


@pytest.mark.asyncio
async def test_submit_tts_tool_empty_text(service, sample_scope):
    """测试 TTS 提交工具空文本"""
    tool = make_media_submit_tts_tool(service)

    result = await tool.handler(text="", voice=None, scope=sample_scope)

    assert result.status == "failed"
    assert "文本不能为空" in result.error


@pytest.mark.asyncio
async def test_submit_asr_tool(service, sample_scope):
    """测试 ASR 提交工具"""
    tool = make_media_submit_asr_tool(service)
    now = datetime.now(timezone.utc)
    service.store.save_artifact(
        ArtifactRecord(
            artifact_id="art-123",
            scope=sample_scope,
            media_type="audio/wav",
            size_bytes=10,
            file_path="user123/test_channel/input.wav",
            created_at=now.isoformat(),
            expires_at=(now + timedelta(hours=24)).isoformat(),
        )
    )

    result = await tool.handler(input_artifact_id="art-123", scope=sample_scope)

    assert result.status == "ok"
    assert "job_id" in result.output
    assert result.output["job_id"].startswith("asr-")
    assert "ASR 任务已提交" in result.output["message"]


@pytest.mark.asyncio
async def test_get_status_tool_queued(service, sample_scope):
    """测试状态查询工具 - queued 状态"""
    tool_submit = make_media_submit_tts_tool(service)
    tool_status = make_media_get_status_tool(service)

    # 提交任务
    submit_result = await tool_submit.handler(text="测试", voice=None, scope=sample_scope)
    job_id = submit_result.output["job_id"]

    # 查询状态
    status_result = await tool_status.handler(job_id=job_id, scope=sample_scope)

    assert status_result.status == "ok"
    assert "queued" in status_result.output["message"].lower()


@pytest.mark.asyncio
async def test_get_status_tool_completed(service, sample_scope):
    """测试状态查询工具 - completed 状态"""
    tool_status = make_media_get_status_tool(service)

    # 手动创建一个已完成的任务
    from agent_core.media_persistence import MediaJobRecord

    now = datetime.now(timezone.utc).isoformat()
    job = MediaJobRecord(
        job_id="completed-job",
        kind="tts",
        status="completed",
        scope=sample_scope,
        request_json='{"text": "test"}',
        artifact_id="art-123",
        queued_at=now,
        completed_at=now,
    )
    service.store.save_job(job)

    # 查询状态
    status_result = await tool_status.handler(job_id="completed-job", scope=sample_scope)

    assert status_result.status == "ok"
    assert "任务已完成" in status_result.output["message"]
    assert "art-123" in status_result.output["message"]
    assert status_result.output["artifact_id"] == "art-123"


@pytest.mark.asyncio
async def test_get_status_tool_failed(service, sample_scope):
    """测试状态查询工具 - failed 状态"""
    tool_status = make_media_get_status_tool(service)

    # 手动创建一个失败的任务
    from agent_core.media_persistence import MediaJobRecord

    now = datetime.now(timezone.utc).isoformat()
    job = MediaJobRecord(
        job_id="failed-job",
        kind="tts",
        status="failed",
        scope=sample_scope,
        request_json='{"text": "test"}',
        error="TTS service error",
        queued_at=now,
        completed_at=now,
    )
    service.store.save_job(job)

    # 查询状态
    status_result = await tool_status.handler(job_id="failed-job", scope=sample_scope)

    assert status_result.status == "failed"
    assert "任务失败" in status_result.error
    assert "TTS service error" in status_result.error


@pytest.mark.asyncio
async def test_get_status_tool_nonexistent(service, sample_scope):
    """测试状态查询工具 - 任务不存在"""
    tool_status = make_media_get_status_tool(service)

    status_result = await tool_status.handler(job_id="nonexistent", scope=sample_scope)

    assert status_result.status == "failed"
    assert "任务不存在" in status_result.error


@pytest.mark.asyncio
async def test_cancel_tool(service, sample_scope):
    """测试取消工具"""
    tool_submit = make_media_submit_tts_tool(service)
    tool_cancel = make_media_cancel_tool(service)

    # 提交任务
    submit_result = await tool_submit.handler(text="测试取消", voice=None, scope=sample_scope)
    job_id = submit_result.output["job_id"]

    # 取消任务（走 ToolExecutor 的调用形状：handler(arguments, scope)）
    cancel_result = await tool_cancel.handler({"job_id": job_id}, sample_scope)

    assert cancel_result.status == "ok"
    assert "取消" in cancel_result.output["message"]
    assert service.store.get_job(job_id).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_completed_job(service, sample_scope):
    """测试取消已完成的任务"""
    tool_cancel = make_media_cancel_tool(service)

    # 手动创建一个已完成的任务
    from agent_core.media_persistence import MediaJobRecord

    now = datetime.now(timezone.utc).isoformat()
    job = MediaJobRecord(
        job_id="completed-job",
        kind="tts",
        status="completed",
        scope=sample_scope,
        request_json='{"text": "test"}',
        queued_at=now,
        completed_at=now,
    )
    service.store.save_job(job)

    # 尝试取消
    cancel_result = await tool_cancel.handler({"job_id": "completed-job"}, sample_scope)

    assert cancel_result.status == "ok"
    assert "已终止" in cancel_result.output["message"] or "无法取消" in cancel_result.output["message"]


@pytest.mark.asyncio
async def test_get_artifact_tool(service, sample_scope, temp_dirs):
    """测试获取 artifact 工具"""
    _, artifact_dir = temp_dirs
    tool_artifact = make_media_get_artifact_tool(service)

    # 创建一个 artifact
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=24)

    # 创建文件
    test_file = artifact_dir / "user123" / "test_channel" / "test.wav"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_bytes(b"test audio")

    artifact = ArtifactRecord(
        artifact_id="art-test",
        scope=sample_scope,
        media_type="audio/wav",
        size_bytes=10,
        file_path="user123/test_channel/test.wav",
        created_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )
    service.store.save_artifact(artifact)

    # 获取 artifact
    result = await tool_artifact.handler(artifact_id="art-test", scope=sample_scope)

    assert result.status == "ok"
    assert "art-test" in result.output["message"]
    assert "audio/wav" in result.output["message"]
    assert "10" in result.output["message"]
    assert result.output["artifact_id"] == "art-test"


@pytest.mark.asyncio
async def test_get_artifact_tool_nonexistent(service, sample_scope):
    """测试获取不存在的 artifact"""
    tool_artifact = make_media_get_artifact_tool(service)

    result = await tool_artifact.handler(artifact_id="nonexistent", scope=sample_scope)

    assert result.status == "failed"
    assert "不存在" in result.error or "无权访问" in result.error


@pytest.mark.asyncio
async def test_tool_name_namespace(service):
    """测试工具名称命名空间"""
    tool_tts = make_media_submit_tts_tool(service)
    tool_asr = make_media_submit_asr_tool(service)
    tool_status = make_media_get_status_tool(service)
    tool_cancel = make_media_cancel_tool(service)
    tool_artifact = make_media_get_artifact_tool(service)

    assert tool_tts.name == "media.submit_tts"
    assert tool_asr.name == "media.submit_asr"
    assert tool_status.name == "media.get_status"
    assert tool_cancel.name == "media.cancel"
    assert tool_artifact.name == "media.get_artifact"


@pytest.mark.asyncio
async def test_tool_risk_levels(service):
    """媒体工具的风险级别。

    取消语音任务会把用户排队的活儿直接掐掉（可能丢产物、可能白等），
    和"查询状态"不是一回事，所以它是唯一的高风险工具。
    其余四个保持低风险 —— 本轮不动 TTS/ASR 执行链。
    """
    tool_tts = make_media_submit_tts_tool(service)
    tool_asr = make_media_submit_asr_tool(service)
    tool_status = make_media_get_status_tool(service)
    tool_cancel = make_media_cancel_tool(service)
    tool_artifact = make_media_get_artifact_tool(service)

    assert tool_tts.risk == "low"
    assert tool_asr.risk == "low"
    assert tool_status.risk == "low"
    assert tool_artifact.risk == "low"
    assert tool_cancel.risk == "high"


def test_cancel_tool_metadata_asks_for_confirmation(service):
    """取消必须先暂停等用户点头，且确认标题是中文。"""
    tool = make_media_cancel_tool(service)

    assert tool.risk == "high"
    assert tool.requires_confirmation is True
    assert tool.confirmation_title == "取消语音任务"
    assert tool.effect == EFFECT_PERSISTENT
    assert tool.deterministic_route_allowed is False


def test_cancel_tool_denies_anonymous_and_confirms_for_regular_scope(service):
    """匿名会话连确认的机会都不给；普通会话进入待确认。"""
    from agent_core.tools import DefaultRiskPolicy

    tool = make_media_cancel_tool(service)
    policy = DefaultRiskPolicy()

    assert policy.authorize(tool, Scope("anonymous:abc", "web", "c1", "media"), {"job_id": "j"}) == "deny"
    assert policy.authorize(tool, Scope("user123", "web", "c1", "media"), {"job_id": "j"}) == "confirm"
    # 查询类工具不受影响：只读查询不该被打扰
    assert policy.authorize(make_media_get_status_tool(service),
                            Scope("user123", "web", "c1", "media"), {"job_id": "j"}) == "allow"


@pytest.mark.asyncio
async def test_already_completed_job_is_not_described_as_cancelled(service, sample_scope):
    """已经跑完的任务不能被说成"已撤销"。"""
    from agent_core.media_persistence import MediaJobRecord

    now = datetime.now(timezone.utc).isoformat()
    service.store.save_job(MediaJobRecord(
        job_id="completed-job", kind="tts", status="completed", scope=sample_scope,
        request_json='{"text": "test"}', queued_at=now, completed_at=now,
    ))
    tool = make_media_cancel_tool(service)

    result = await tool.handler({"job_id": "completed-job"}, sample_scope)

    assert result.status == "ok"
    assert "无法取消" in result.output["message"]
    assert "已标记为取消" not in result.output["message"]
    assert service.store.get_job("completed-job").status == "completed"


@pytest.mark.asyncio
async def test_cancel_tool_rejects_an_empty_job_id(service, sample_scope):
    """没有 job_id 就不该碰服务 —— 让它去"猜一个任务"只会换来误导性的错误。"""
    tool = make_media_cancel_tool(service)

    for empty in ({}, {"job_id": ""}, {"job_id": "   "}):
        result = await tool.handler(dict(empty), sample_scope)

        assert result.status == "failed"
        assert result.error_kind == "permanent"
        assert "任务 ID" in result.error


@pytest.mark.asyncio
async def test_cancel_tool_hides_other_scopes_jobs(service, sample_scope):
    """别人的任务：既不能取消，也不能从错误里看出它确实存在。"""
    from agent_core.contracts import Scope

    tool_submit = make_media_submit_tts_tool(service)
    # submit_tts 仍是关键字签名（本轮不动它），只有 cancel 改成了执行器形状
    job_id = (await tool_submit.handler(
        text="别人的任务", voice=None, scope=sample_scope)).output["job_id"]
    tool = make_media_cancel_tool(service)
    outsider = Scope("someone-else", sample_scope.channel, sample_scope.conversation_id, "media")

    result = await tool.handler({"job_id": job_id}, outsider)

    assert result.status == "failed"
    assert result.error == "任务不存在或无权访问"
    assert service.store.get_job(job_id).status == "queued"


@pytest.mark.asyncio
async def test_cancel_tool_on_a_missing_job(service, sample_scope):
    tool = make_media_cancel_tool(service)

    result = await tool.handler({"job_id": "no-such-job"}, sample_scope)

    assert result.status == "failed"
    assert result.error == "任务不存在或无权访问"


@pytest.mark.asyncio
async def test_cancel_tool_keeps_failed_status(service, sample_scope):
    """失败就是失败，不能被说成"已经取消"。"""
    from agent_core.media_persistence import MediaJobRecord

    now = datetime.now(timezone.utc).isoformat()
    service.store.save_job(MediaJobRecord(
        job_id="failed-job", kind="tts", status="failed", scope=sample_scope,
        request_json='{"text": "test"}', queued_at=now, completed_at=now,
    ))
    tool = make_media_cancel_tool(service)

    result = await tool.handler({"job_id": "failed-job"}, sample_scope)

    assert result.status == "ok"
    assert result.output["status"] == "failed"
    assert "已标记为取消" not in result.output["message"]
    assert service.store.get_job("failed-job").status == "failed"


# ----------------------------------------------------------------------
# 取消走主 Chat 的确认流程（AgentLoop 级）
# ----------------------------------------------------------------------
def _confirm_loop(service, scope, db, job_id):
    """搭一个"模型请求取消语音任务"的最小 AgentLoop。

    注册的是**生产**的 ``make_media_cancel_tool``，名字就是 ``media.cancel``。
    以前这里套了一层 ``_loop_ready_cancel_tool`` 适配器把关键字签名掰成
    执行器形状 —— 那是用一个生产里不存在的组件证明"真实链路可用"，
    结论是假的：真实链路上 ToolExecutor 会把整个 arguments 字典当成
    ``job_id`` 传下去，取消根本不会发生。现在取消工具本身就是
    ``(arguments, scope)``，不需要、也不允许再套适配层。
    """
    import json as _json

    from agent_core.contracts import ProviderResponse
    from agent_core.loop import AgentLoop
    from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
    from agent_core.tools import ToolRegistry

    class ScriptedProvider:
        max_tokens = 512

        def __init__(self):
            self.responses = iter([
                {"role": "assistant", "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "media.cancel", "arguments": _json.dumps({"job_id": job_id})},
                }]},
                {"role": "assistant", "content": "已经停下来了。"},
            ])

        async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
            return ProviderResponse(next(self.responses), {"completion_tokens": 1})

    registry = ToolRegistry()
    registry.register(make_media_cancel_tool(service))
    return AgentLoop(
        ScriptedProvider(), registry, scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
    )


@pytest.fixture
def recording_service(service):
    """记录 cancel_job 被调用了几次，其余交给真服务。"""

    class Recording:
        def __init__(self, inner):
            self.inner = inner
            self.calls = []

        async def cancel_job(self, job_id, scope):
            self.calls.append(job_id)
            return await self.inner.cancel_job(job_id, scope)

    return Recording(service)


@pytest.mark.asyncio
async def test_cancel_pauses_before_touching_the_service(recording_service, sample_scope, tmp_path):
    """确认之前 service 不能被调用 —— 这是"等待确认"的全部意义。"""
    tool = make_media_submit_tts_tool(recording_service.inner)
    submitted = await tool.handler(text="要取消的这段", voice=None, scope=sample_scope)
    job_id = submitted.output["job_id"]

    loop = _confirm_loop(recording_service, sample_scope, tmp_path / "core.db", job_id)
    outcome = await loop.run("取消这个语音任务")

    assert outcome.status == "paused"
    assert outcome.action_id
    assert recording_service.calls == []
    assert recording_service.inner.store.get_job(job_id).status == "queued"


@pytest.mark.asyncio
async def test_confirmed_cancel_executes_exactly_once(recording_service, sample_scope, tmp_path):
    """真实链路上：批准后服务被调用一次，数据库状态真的变成 cancelled。

    断言落库（而不只是"服务被调了"）是因为这正是上一次被适配层掩盖掉的
    那半截：服务调了不等于任务真的停了。
    """
    tool = make_media_submit_tts_tool(recording_service.inner)
    job_id = (await tool.handler(text="要取消的这段", voice=None, scope=sample_scope)).output["job_id"]

    loop = _confirm_loop(recording_service, sample_scope, tmp_path / "core.db", job_id)
    paused = await loop.run("取消这个语音任务")
    completed = await loop.resume(paused.action_id, "confirmed")
    repeated = await loop.resume(paused.action_id, "confirmed")

    assert completed.status == "completed"
    assert repeated.status == "failed"           # 同一个 action_id 不能被用第二次
    assert recording_service.calls == [job_id]   # 用真实 job_id 调的，不是字典
    assert recording_service.inner.store.get_job(job_id).status == "cancelled"


@pytest.mark.asyncio
async def test_rejected_cancel_never_calls_the_service(recording_service, sample_scope, tmp_path):
    tool = make_media_submit_tts_tool(recording_service.inner)
    job_id = (await tool.handler(text="要取消的这段", voice=None, scope=sample_scope)).output["job_id"]

    loop = _confirm_loop(recording_service, sample_scope, tmp_path / "core.db", job_id)
    paused = await loop.run("取消这个语音任务")

    rejected = await loop.resume(paused.action_id, "rejected")

    assert rejected.status == "completed"
    assert recording_service.calls == []
    assert recording_service.inner.store.get_job(job_id).status == "queued"


@pytest.mark.asyncio
async def test_end_to_end_workflow(service, sample_scope, temp_dirs):
    """测试端到端工作流"""
    _, artifact_dir = temp_dirs

    tool_submit = make_media_submit_tts_tool(service)
    tool_status = make_media_get_status_tool(service)

    # Mock TTS 服务
    mock_audio_content = b"test audio data"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.content = mock_audio_content

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client_class.return_value = mock_client

        # 1. 提交任务
        submit_result = await tool_submit.handler(text="端到端测试", voice=None, scope=sample_scope)
        assert submit_result.status == "ok"
        job_id = submit_result.output["job_id"]

        # 2. 查询状态（queued）
        status_result = await tool_status.handler(job_id=job_id, scope=sample_scope)
        assert status_result.status == "ok"
        assert "queued" in status_result.output["message"].lower()

        # 3. 手动执行任务
        job_record = service.store.get_job(job_id)
        await service._execute_tts(job_record)

        # 4. 查询状态（completed）
        status_result = await tool_status.handler(job_id=job_id, scope=sample_scope)
        assert status_result.status == "ok"
        assert "完成" in status_result.output["message"]
        assert "artifact_id" in status_result.output

        # 5. 获取 artifact
        artifact_id = status_result.output["artifact_id"]
        artifact = await service.get_artifact(artifact_id, sample_scope)
        assert artifact is not None
        assert artifact.path.exists()
        assert artifact.path.read_bytes() == mock_audio_content
