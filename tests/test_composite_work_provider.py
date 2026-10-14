from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agent_core.legacy_code_agent.code_agent_contracts import CodeAgentBudget, CodeAgentCheckpoint, CodeAgentOutcome
from agent_core.composite_work_provider import CompositeWorkProvider
from agent_core.contracts import Scope
from agent_core.work_provider import (
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
    validate_work_submit_arguments,
)


class RecordingProvider:
    def __init__(
        self,
        provider_id: str,
        task_types: tuple[str, ...],
        *,
        supports_resume: bool = False,
    ) -> None:
        self.capabilities = WorkProviderCapabilities(
            provider_id=provider_id,
            task_types=task_types,
            supports_cancel=True,
            supports_resume=supports_resume,
        )
        self.executed: list[str] = []
        self.cancelled: list[str] = []
        self.resumed: list[tuple[str, str]] = []

    async def execute(self, request, emit):
        self.executed.append(request.attempt_id)
        return WorkResult("completed", output={"provider": self.capabilities.provider_id})

    async def cancel(self, attempt_id):
        self.cancelled.append(attempt_id)
        return True

    async def resume(self, request, checkpoint, decision, emit):
        self.resumed.append((request.attempt_id, decision))
        return WorkResult("completed", output={"checkpoint": dict(checkpoint)})


def request(task_type: str, attempt_id: str = "attempt-1") -> WorkRequest:
    return WorkRequest(
        work_item_id="work-1",
        attempt_id=attempt_id,
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat-1"),
        objective="完成代码任务",
        task_type=task_type,
        input={"project_root": "C:/repo"} if task_type == "code.agent" else {"directory": "C:/repo"},
    )


def test_code_agent_contracts_are_frozen_and_validate_limits() -> None:
    budget = CodeAgentBudget()
    assert budget.max_steps > 0
    assert budget.max_tokens > 0
    assert budget.timeout_seconds > 0
    with pytest.raises(FrozenInstanceError):
        budget.max_steps = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="max_steps"):
        CodeAgentBudget(max_steps=0)

    checkpoint = CodeAgentCheckpoint(
        snapshot_id="snapshot-1",
        messages=({"role": "user", "content": "fix it"},),
        steps_used=2,
        tokens_used=100,
        original_action={"name": "code__run", "arguments": {"argv": ["python.exe"]}},
    )
    outcome = CodeAgentOutcome(status="completed", summary="done", checkpoint=checkpoint)
    assert outcome.checkpoint is checkpoint
    with pytest.raises(ValueError, match="status"):
        CodeAgentOutcome(status="running")


def test_code_agent_submit_requires_project_root() -> None:
    violation = validate_work_submit_arguments(
        {"task_type": "code.agent", "objective": "修复测试", "input": {}}
    )
    assert violation is not None
    assert violation.output == {"task_type": "code.agent", "missing": ["project_root"]}


def test_composite_rejects_duplicate_task_type() -> None:
    first = RecordingProvider("first", ("code.agent",))
    second = RecordingProvider("second", ("code.agent",))
    with pytest.raises(ValueError, match="code.agent"):
        CompositeWorkProvider([first, second])


@pytest.mark.asyncio
async def test_composite_dispatches_execute_and_cancel_to_owner() -> None:
    local = RecordingProvider("local", ("file.list",))
    code = RecordingProvider("code", ("code.agent",), supports_resume=True)
    composite = CompositeWorkProvider([local, code])

    result = await composite.execute(request("code.agent"), lambda event: None)
    assert result.output == {"provider": "code"}
    assert code.executed == ["attempt-1"]
    assert local.executed == []

    assert await composite.cancel("attempt-1") is True
    assert code.cancelled == ["attempt-1"]
    assert local.cancelled == []


@pytest.mark.asyncio
async def test_composite_resume_requires_resumable_owner() -> None:
    local = RecordingProvider("local", ("file.list",))
    code = RecordingProvider("code", ("code.agent",), supports_resume=True)
    composite = CompositeWorkProvider([local, code])

    result = await composite.resume(
        request("code.agent", "attempt-2"),
        {"snapshot_id": "snapshot-1"},
        "approved",
        lambda event: None,
    )
    assert result.status == "completed"
    assert code.resumed == [("attempt-2", "approved")]

    unsupported = await composite.resume(
        request("file.list", "attempt-3"),
        {},
        "approved",
        lambda event: None,
    )
    assert unsupported.status == "interrupted"
    assert "resume" in (unsupported.error or "").lower()

