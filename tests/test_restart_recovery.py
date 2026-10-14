"""
测试进程重启后的任务恢复
"""
import pytest
from pathlib import Path
from typing import Any
from agent_core.contracts import Scope, AgentRequest, ProviderResponse, ToolSpec, ToolCall, ToolResult
from agent_core.provider import LLMProvider
from agent_core.loop import AgentLoop
from agent_core.tools import ToolRegistry
from agent_core.persistence import SQLiteRunStore, SQLiteConfirmationStore
from agent_core.media_persistence import MediaTaskStore, MediaJobRecord
from agent_core.media_service import MediaTaskService, MediaJobRequest


class MockProvider(LLMProvider):
    """Mock LLM Provider for testing"""
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        if self.call_count >= len(self.responses):
            raise RuntimeError("no more mock responses")
        response = self.responses[self.call_count]
        self.call_count += 1
        return ProviderResponse(
            message=response,
            usage={"prompt_tokens": 10, "completion_tokens": 20},
        )


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """临时数据库路径"""
    return tmp_path / "test_restart.db"


@pytest.fixture
def scope() -> Scope:
    return Scope(
        owner_id="test_owner",
        channel="desktop",
        conversation_id="test_conv",
        module_id="core"
    )


@pytest.fixture
def registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def test_tool_handler(call: ToolCall, scope: Scope) -> ToolResult:
        return ToolResult(status="success", output={"result": "test result"})

    registry.register(ToolSpec(
        name="test_tool",
        schema={
            "name": "test_tool",
            "description": "test tool",
            "input_schema": {"type": "object", "properties": {}}
        },
        handler=test_tool_handler,
        risk="low"
    ))
    return registry


@pytest.mark.asyncio
async def test_conversation_history_survives_restart(
    temp_db: Path,
    registry: ToolRegistry,
    scope: Scope
):
    """验证对话历史在重启后可恢复"""
    provider = MockProvider([
        {"role": "assistant", "content": "First response", "stop_reason": "end_turn"}
    ])

    # 第一次运行
    loop1 = AgentLoop(
        provider=provider,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
    )

    request = AgentRequest(user_text="first message", scope=scope)
    outcome1 = await loop1.run(request)
    assert outcome1.status == "completed"

    run_id_1 = outcome1.run_id

    # 模拟进程重启：创建新的 loop 实例，共享同一个数据库
    provider2 = MockProvider([
        {"role": "assistant", "content": "Second response", "stop_reason": "end_turn"}
    ])

    loop2 = AgentLoop(
        provider=provider2,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
    )

    # 第二次运行应该能访问到第一次的历史
    # 通过 run_store 获取上一次的历史
    run_record = loop2.run_store.load(run_id_1, scope)
    assert run_record is not None
    assert run_record.status == "completed"
    assert len(run_record.messages) > 0

    # 使用上一次的历史继续对话
    request2 = AgentRequest(
        user_text="second message",
        scope=scope,
        history=run_record.messages
    )
    outcome2 = await loop2.run(request2)
    assert outcome2.status == "completed"


@pytest.mark.asyncio
async def test_pending_confirmation_survives_restart(
    temp_db: Path,
    registry: ToolRegistry,
    scope: Scope
):
    """验证待确认的动作在重启后可恢复"""
    # Provider 返回一个需要确认的工具调用
    provider = MockProvider([
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "dangerous_tool", "arguments": "{}"}}
            ],
            "stop_reason": "tool_use"
        }
    ])

    # 注册一个高风险工具
    async def dangerous_tool_handler(call: ToolCall, scope: Scope) -> ToolResult:
        return ToolResult(status="success", output={"result": "危险操作完成"})

    registry.register(ToolSpec(
        name="dangerous_tool",
        schema={
            "name": "dangerous_tool",
            "description": "危险工具",
            "input_schema": {"type": "object", "properties": {}}
        },
        handler=dangerous_tool_handler,
        risk="high"
    ))

    # 第一次运行，触发确认
    loop1 = AgentLoop(
        provider=provider,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
    )

    request = AgentRequest(user_text="run dangerous", scope=scope)
    outcome1 = await loop1.run(request)

    # 验证进入 paused 状态
    assert outcome1.status == "paused"
    action_id = outcome1.action_id  # 从 action_id 字段获取
    assert action_id  # 确保 action_id 不为空

    # 模拟进程重启：创建新的 loop 实例
    provider2 = MockProvider([
        {"role": "assistant", "content": "Done after approval", "stop_reason": "end_turn"}
    ])

    loop2 = AgentLoop(
        provider=provider2,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
    )

    # 从持久化存储中恢复待确认的动作
    pending = loop2.confirmation_store.get(action_id, scope)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.tool == "dangerous_tool"

    # 使用相同的 run_id 创建新的 loop（模拟重启后恢复）
    provider3 = MockProvider([
        {"role": "assistant", "content": "Done after approval", "stop_reason": "end_turn"}
    ])

    loop3 = AgentLoop(
        provider=provider3,
        registry=registry,
        scope=scope,
        run_store=SQLiteRunStore(str(temp_db)),
        confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        run_id=pending.run_id  # 使用持久化的 run_id
    )

    # 用户批准后继续
    outcome2 = await loop3.resume(action_id, "confirmed")


@pytest.mark.asyncio
async def test_media_job_survives_restart(
    temp_db: Path,
    scope: Scope
):
    """验证媒体任务在重启后可以恢复"""
    # 创建临时 artifact 目录
    artifact_dir = temp_db.parent / "artifacts"
    artifact_dir.mkdir(exist_ok=True)

    # 第一次运行：提交媒体任务
    store1 = MediaTaskStore(temp_db)
    media_service1 = MediaTaskService(store=store1, artifact_dir=artifact_dir)

    request = MediaJobRequest(
        kind="tts",
        scope=scope,
        text="Hello world",
        language="en",
        voice="default"
    )
    status = await media_service1.submit_job(request)
    job_id = status.job_id

    assert job_id

    # 验证任务已创建
    status1 = await media_service1.get_job_status(job_id)
    assert status1 is not None
    assert status1.status == "queued"

    # 模拟进程重启：创建新的 MediaTaskService 实例
    store2 = MediaTaskStore(temp_db)
    media_service2 = MediaTaskService(store=store2, artifact_dir=artifact_dir)

    # 从持久化存储中恢复任务
    status2 = await media_service2.get_job_status(job_id)
    assert status2 is not None
    assert status2.status == "queued"

    # 验证可以通过 store 直接查询任务记录
    record = store2.get_job(job_id)
    assert record is not None
    assert record.kind == "tts"
    assert record.status == "queued"

    # 验证可以通过 store 更新任务状态
    record.status = "running"
    store2.save_job(record)

    status3 = await media_service2.get_job_status(job_id)
    assert status3 is not None
    assert status3.status == "running"

    # 完成任务
    record.status = "completed"
    record.artifact_id = "test_audio.wav"
    store2.save_job(record)

    status4 = await media_service2.get_job_status(job_id)
    assert status4 is not None
    assert status4.status == "completed"
    assert status4.artifact_id == "test_audio.wav"



@pytest.mark.asyncio
async def test_media_jobs_survive_restart(
    temp_db: Path,
    scope: Scope
):
    """验证媒体任务在重启后可恢复"""
    from datetime import datetime, timezone
    import json

    # 第一次运行，创建媒体任务
    store1 = MediaTaskStore(temp_db)

    job1 = MediaJobRecord(
        job_id="job_123",
        kind="tts",
        status="queued",
        scope=scope,
        request_json=json.dumps({"text": "Hello world", "voice": "default"}),
        queued_at=datetime.now(timezone.utc).isoformat()
    )
    store1.save_job(job1)

    # 验证任务已创建
    retrieved1 = store1.get_job("job_123")
    assert retrieved1 is not None
    assert retrieved1.status == "queued"

    # 模拟进程重启：创建新的 store 实例
    store2 = MediaTaskStore(temp_db)

    # 从持久化存储中恢复任务
    job2 = store2.get_job("job_123")
    assert job2 is not None
    assert job2.status == "queued"
    assert job2.kind == "tts"

    # 更新任务状态
    job2.status = "completed"
    job2.artifact_id = "artifact_123"
    job2.completed_at = datetime.now(timezone.utc).isoformat()
    store2.save_job(job2)

    # 再次查询，验证状态已更新
    job3 = store2.get_job("job_123")
    assert job3.status == "completed"
    assert job3.artifact_id == "artifact_123"


@pytest.mark.asyncio
async def test_run_store_handles_concurrent_writes_after_restart(
    temp_db: Path,
    registry: ToolRegistry,
    scope: Scope
):
    """验证重启后多个进程并发写入 run_store 不会冲突"""
    import asyncio

    async def run_agent(loop_id: int):
        provider = MockProvider([
            {"role": "assistant", "content": f"Response {loop_id}", "stop_reason": "end_turn"}
        ])

        loop = AgentLoop(
            provider=provider,
            registry=registry,
            scope=Scope(
                owner_id=scope.owner_id,
                channel=scope.channel,
                conversation_id=f"conv_{loop_id}",
                module_id=scope.module_id
            ),
            run_store=SQLiteRunStore(str(temp_db)),
            confirmation_store=SQLiteConfirmationStore(str(temp_db)),
        )

        request = AgentRequest(user_text=f"message {loop_id}", scope=loop.scope)
        outcome = await loop.run(request)
        return outcome.status

    # 模拟 5 个并发 agent 运行
    results = await asyncio.gather(*[run_agent(i) for i in range(5)])

    # 验证所有运行都成功
    assert all(status == "completed" for status in results)
