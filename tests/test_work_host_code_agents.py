from __future__ import annotations

import asyncio

import pytest

from agent_core.contracts import Scope
from agent_core.work_host import WorkHost
from agent_core.work_provider import WorkProviderCapabilities, WorkResult


async def settle(host, work_item_id, scope, terminal=None):
    terminal = terminal or {"completed", "failed", "cancelled", "waiting_approval"}
    for _ in range(200):
        detail = host.get(work_item_id, scope)
        if detail["status"] in terminal:
            return detail
        await asyncio.sleep(0.01)
    raise AssertionError(f"work did not settle: {detail}")


class IsolatedProvider:
    capabilities = WorkProviderCapabilities(
        "composite-test",
        ("file.list", "code.agent"),
        supports_cancel=True,
        supports_resume=True,
    )

    def __init__(self):
        self.code_started = asyncio.Event()
        self.release_code = asyncio.Event()
        self.short_finished = asyncio.Event()

    async def execute(self, request, emit):
        if request.task_type == "code.agent":
            self.code_started.set()
            await self.release_code.wait()
            return WorkResult("completed", {"kind": "code"})
        self.short_finished.set()
        return WorkResult("completed", {"kind": "short"})

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("completed", {"resumed": decision})


@pytest.mark.asyncio
async def test_code_worker_does_not_block_short_task_queue(tmp_path) -> None:
    provider = IsolatedProvider()
    host = WorkHost(
        tmp_path / "work.db",
        provider=provider,
        max_workers=1,
        code_workers=1,
    )
    scope = Scope("alice", "web", "chat", "work")
    await host.start()
    try:
        code = await host.submit(
            scope,
            "long code task",
            task_type="code.agent",
            input={"project_root": str(tmp_path)},
        )
        await asyncio.wait_for(provider.code_started.wait(), timeout=1)
        short = await host.submit(
            scope,
            "list files",
            task_type="file.list",
            input={"directory": str(tmp_path)},
        )

        await asyncio.wait_for(provider.short_finished.wait(), timeout=1)
        short_detail = await settle(host, short["work_item_id"], scope)
        assert short_detail["status"] == "completed"
        assert host.get(code["work_item_id"], scope)["status"] == "running"

        provider.release_code.set()
        code_detail = await settle(host, code["work_item_id"], scope)
        assert code_detail["status"] == "completed"
    finally:
        provider.release_code.set()
        await host.stop()


class ApprovalProvider:
    capabilities = WorkProviderCapabilities(
        "code-test",
        ("code.agent",),
        supports_cancel=True,
        supports_resume=True,
    )

    def __init__(self):
        self.resume_calls = []

    async def execute(self, request, emit):
        return WorkResult(
            "waiting_approval",
            output={"approval_id": "gateway-approval", "tool": "code__run"},
            checkpoint={
                "snapshot_id": "snapshot-1",
                "messages": [],
                "steps_used": 1,
                "tokens_used": 10,
                "original_action": {
                    "approval_id": "gateway-approval",
                    "name": "code__run",
                    "arguments": {"secret_path": "C:/private"},
                    "turn_id": request.attempt_id,
                },
            },
            approval={"tool": "code__run", "summary": "run command"},
        )

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        self.resume_calls.append((checkpoint, decision))
        return WorkResult("completed", {"decision": decision})


@pytest.mark.asyncio
async def test_code_approval_persists_checkpoint_and_rejection_resumes_model(tmp_path) -> None:
    provider = ApprovalProvider()
    host = WorkHost(
        tmp_path / "work.db", provider=provider, max_workers=1, code_workers=1
    )
    scope = Scope("alice", "web", "chat", "work")
    await host.start()
    try:
        submitted = await host.submit(
            scope,
            "run guarded command",
            task_type="code.agent",
            input={"project_root": str(tmp_path)},
        )
        waiting = await settle(host, submitted["work_item_id"], scope)
        assert waiting["status"] == "waiting_approval"
        attempt_id = waiting["attempts"][0]["attempt_id"]
        checkpoint = host.ledger.get_checkpoint(attempt_id)
        assert checkpoint["original_action"]["arguments"]["secret_path"] == "C:/private"
        approval = waiting["approvals"][0]
        assert "secret_path" not in str(approval["payload"])

        decided = await host.decide_approval(
            approval["approval_id"], scope, "rejected"
        )
        assert decided["status"] == "queued"
        completed = await settle(
            host, submitted["work_item_id"], scope, terminal={"completed", "failed"}
        )
        assert completed["status"] == "completed"
        assert provider.resume_calls[0][1] == "rejected"
        assert provider.resume_calls[0][0]["original_action"]["arguments"]["secret_path"] == "C:/private"
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_waiting_code_approval_survives_host_restart(tmp_path) -> None:
    provider = ApprovalProvider()
    path = tmp_path / "work.db"
    scope = Scope("alice", "web", "chat", "work")
    first = WorkHost(path, provider=provider, code_workers=1)
    await first.start()
    submitted = await first.submit(
        scope,
        "wait",
        task_type="code.agent",
        input={"project_root": str(tmp_path)},
    )
    waiting = await settle(first, submitted["work_item_id"], scope)
    approval_id = waiting["approvals"][0]["approval_id"]
    await first.stop()

    second = WorkHost(path, provider=provider, code_workers=1)
    await second.start()
    try:
        restored = second.get(submitted["work_item_id"], scope)
        assert restored["status"] == "waiting_approval"
        assert restored["approvals"][0]["approval_id"] == approval_id
    finally:
        await second.stop()

