from __future__ import annotations

import pytest

from agent_core.contracts import Scope
from agent_core.work_provider import (
    WorkEvent,
    WorkProvider,
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
)


def test_work_request_and_result_are_serializable_value_objects() -> None:
    request = WorkRequest(
        work_item_id="work-1",
        attempt_id="attempt-1",
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat-1"),
        objective="整理下载目录",
        context={"sort": "name"},
        allowed_tools=("files__list",),
        allowed_roots=("C:/Users/alice/Downloads",),
        budget={"max_steps": 5},
    )
    event = WorkEvent(attempt_id=request.attempt_id, event_type="started", payload={"provider": "local"})
    result = WorkResult(status="completed", output={"count": 3}, evidence=[{"event_id": event.event_id}])

    assert request.scope.owner_id == "alice"
    assert request.allowed_tools == ("files__list",)
    assert event.event_id
    assert result.status == "completed"


def test_work_provider_protocol_exposes_capabilities_and_lifecycle_methods() -> None:
    class Provider:
        capabilities = WorkProviderCapabilities(
            provider_id="local-worker",
            task_types=("file",),
            supports_cancel=True,
            supports_resume=True,
        )

        async def execute(self, request, emit):
            return WorkResult(status="completed")

        async def cancel(self, attempt_id):
            return True

        async def resume(self, request, checkpoint, decision, emit):
            return WorkResult(status="completed")

    provider = Provider()
    assert isinstance(provider, WorkProvider)
    assert provider.capabilities.supports_resume


def test_work_result_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        WorkResult(status="running")


def test_work_request_has_explicit_task_type_and_rejects_user_approval_manifest() -> None:
    request = WorkRequest(
        work_item_id="work-1",
        attempt_id="attempt-1",
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat-1"),
        task_type="file.list",
        objective="列出文件",
        input={"directory": "C:/Users/alice/Downloads"},
    )
    assert request.task_type == "file.list"
    assert request.input["directory"].endswith("Downloads")

    with pytest.raises(ValueError, match="approved_moves"):
        WorkRequest(
            work_item_id="work-2",
            attempt_id="attempt-2",
            scope=Scope(owner_id="alice", channel="web", conversation_id="chat-1"),
            task_type="file.organize",
            objective="整理文件",
            input={"directory": "C:/Users/alice/Downloads", "approved_moves": []},
        )
