"""The production Work surface has exactly one code executor: Harness."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_core.contracts import Scope
from agent_core.work_module import build_work_module, resolve_work_provider
from agent_core.work_provider import WorkProviderCapabilities, WorkRequest, WorkResult
from agent_tools.work import make_work_tools


class HarnessEndpoint:
    """Replace the external process, while keeping Work submission/ledger real."""

    capabilities = WorkProviderCapabilities(
        "deepseek_harness", ("code.agent",), supports_cancel=True, supports_resume=True,
        selectable=True,
    )

    def __init__(self):
        self.executed = []
        self.resumed = []

    async def execute(self, request, emit):
        self.executed.append(request)
        return WorkResult("failed", error="Harness endpoint unavailable in this test")

    async def cancel(self, attempt_id):
        return False

    async def resume(self, request, checkpoint, decision, emit):
        self.resumed.append(request)
        return WorkResult("failed", error="Harness endpoint unavailable in this test")


def build_host(tmp_path: Path, **kwargs):
    _, host = build_work_module(
        db_path=tmp_path / "work.db",
        core_db_path=tmp_path / "core.db",
        allowed_roots=[tmp_path],
        code_snapshot_root=tmp_path / "snapshots",
        code_artifact_dir=tmp_path / "artifacts",
        code_apply_state_root=tmp_path / "apply",
        code_sandbox_state_root=tmp_path / "sandbox",
        harness_sandbox_state_root=tmp_path / "harness-state",
        **kwargs,
    )
    return host


def test_default_work_surface_exposes_harness_as_only_code_executor(tmp_path: Path):
    host = build_host(tmp_path)

    provider = resolve_work_provider(host, "code.agent")
    assert provider is not None
    assert provider.capabilities.provider_id == "deepseek_harness"
    registry = {item["provider_id"]: item for item in host.provider_registry()}
    assert set(registry) == {"local-worker", "deepseek_harness"}
    assert registry["deepseek_harness"]["default"] is True
    assert host.provider.provider_for("file.list").capabilities.provider_id == "local-worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_chat_submission_records_harness_even_when_process_fails(tmp_path: Path, explicit: bool):
    host = build_host(tmp_path, code_agent_enabled=True, harness_provider=HarnessEndpoint())
    scope = Scope(owner_id="test", channel="web", module_id="chat", conversation_id="routing")
    submit = next(tool for tool in make_work_tools(host) if tool.name == "submit")
    task_input = {"project_root": str(tmp_path)}
    if explicit:
        task_input["provider"] = "deepseek_harness"
    result = await submit.handler(
        {"task_type": "code.agent", "objective": "Read README only", "input": task_input}, scope
    )
    assert result["status"] == "queued"
    work_id = result["work_item_id"]
    work_scope = Scope(owner_id="test", channel="web", module_id="work", conversation_id="routing")
    before = host.get(work_id, work_scope)
    assert before["attempts"][0]["provider_id"] == "deepseek_harness"

    await host.start()
    try:
        async with asyncio.timeout(5):
            while host.get(work_id, work_scope)["status"] != "failed":
                await asyncio.sleep(0.02)
        after = host.get(work_id, work_scope)
        assert after["attempts"][0]["provider_id"] == "deepseek_harness"
        assert len(after["attempts"]) == 1
        assert "Harness endpoint unavailable" in after["attempts"][0]["error"]
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_disabled_harness_fails_without_registering_legacy(tmp_path: Path):
    host = build_host(tmp_path, code_agent_enabled=True, harness_enabled=False)
    provider = resolve_work_provider(host, "code.agent")
    assert provider.capabilities.provider_id == "deepseek_harness"
    request = WorkRequest("w", "a", Scope(owner_id="test"), "Read only", task_type="code.agent",
                          input={"project_root": str(tmp_path)})
    result = await provider.execute(request, lambda event: None)
    assert result.status == "failed"
    assert "disabled" in result.error.lower()
    assert host.provider.provider_by_id("code-agent") is None


@pytest.mark.asyncio
async def test_explicit_legacy_selector_is_rejected_before_task_creation(tmp_path: Path):
    host = build_host(tmp_path, code_agent_enabled=True, harness_provider=HarnessEndpoint())
    with pytest.raises(ValueError, match="unknown work provider"):
        await host.submit(
            Scope(owner_id="test", module_id="work"), "Read only", task_type="code.agent",
            input={"project_root": str(tmp_path), "provider": "code-agent"},
        )
    assert host.ledger.list_work_items(Scope(owner_id="test", module_id="work")) == []


def test_old_attempt_identity_is_not_reinterpreted_as_harness(tmp_path: Path):
    host = build_host(tmp_path, code_agent_enabled=True, harness_provider=HarnessEndpoint())
    selected = host._request_provider_id(
        task_type="code.agent", stored_provider_id="code-agent",
        work_input={"project_root": str(tmp_path)}, context={},
    )
    assert selected == "code-agent"


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["code-agent", "composite-worker"])
@pytest.mark.parametrize("action", ["restart", "retry", "approve"])
async def test_historical_code_tasks_never_switch_executor(tmp_path: Path, stored: str, action: str):
    harness = HarnessEndpoint()
    host = build_host(tmp_path, harness_provider=harness)
    scope = Scope(owner_id="test", module_id="work")
    work = host.ledger.create_work_item(
        scope=scope, objective="Historical change", task_type="code.agent",
        input={"project_root": str(tmp_path)},
    )
    previous = host.ledger.create_attempt(work.work_item_id, provider_id=stored)
    if action == "retry":
        host.ledger.claim_attempt(previous.attempt_id)
        host.ledger.finish_attempt(previous.attempt_id, status="failed", error="previous failure")
        result = await host.retry(work.work_item_id, scope)
        target_id = result["attempt_id"]
    elif action == "approve":
        host.ledger.claim_attempt(previous.attempt_id)
        host.ledger.save_checkpoint(previous.attempt_id, {"kind": "code_apply", "snapshot_id": "old"})
        approval = host.ledger.create_approval(previous.attempt_id, {"changes": []})
        result = await host.decide_approval(approval.approval_id, scope, "approved")
        target_id = result["attempt_id"]
    else:
        target_id = previous.attempt_id
    await host.start()
    try:
        async with asyncio.timeout(5):
            while host.ledger.get_attempt(target_id).status not in {"failed", "completed"}:
                await asyncio.sleep(0.02)
        target = host.ledger.get_attempt(target_id)
        assert target.status == "failed"
        assert target.provider_id == stored
        assert "unknown work provider" in target.error
        assert harness.executed == []
        assert harness.resumed == []
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_historical_explicit_harness_task_keeps_its_executor(tmp_path: Path):
    harness = HarnessEndpoint()
    host = build_host(tmp_path, harness_provider=harness)
    scope = Scope(owner_id="test", module_id="work")
    work = host.ledger.create_work_item(
        scope=scope, objective="Existing Harness task", task_type="code.agent",
        input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
    )
    attempt = host.ledger.create_attempt(work.work_item_id, provider_id="composite-worker")
    await host.start()
    try:
        async with asyncio.timeout(5):
            while host.ledger.get_attempt(attempt.attempt_id).status != "failed":
                await asyncio.sleep(0.02)
        assert len(harness.executed) == 1
        assert harness.executed[0].provider_selector == "deepseek_harness"
    finally:
        await host.stop()
