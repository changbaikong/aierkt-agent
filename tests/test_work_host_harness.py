"""WorkHost-level tests for provider routing, activity persistence and recovery.

Everything here runs without the Harness: the point is the Host's contract — who
runs a task, what is persisted about it, what happens when an attempt stops
without a trustworthy result, and what a restart may and may not resume.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_core.composite_work_provider import CompositeWorkProvider
from agent_core.contracts import Scope
from agent_core.harness_acp import AcpHandshake
from agent_core.provider_activity import ProviderActivity
from agent_core.work_host import WorkHost
from agent_core.work_provider import (
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StubProvider:
    """A provider that records calls and can emit provider activity events."""

    def __init__(
        self,
        provider_id: str = "deepseek_harness",
        *,
        selectable: bool = True,
        result: WorkResult | None = None,
        activity: dict | None = None,
        cancel_ok: bool = True,
        cancel_state: str | None = "confirmed",
    ) -> None:
        self.capabilities = WorkProviderCapabilities(
            provider_id=provider_id,
            task_types=("code.agent",),
            supports_cancel=True,
            supports_resume=True,
            selectable=selectable,
        )
        self.result = result or WorkResult("completed", output={"ok": True})
        self.activity = activity
        self.cancel_ok = cancel_ok
        self.cancel_state = cancel_state
        self.executed: list[WorkRequest] = []
        self.resumed: list[tuple[WorkRequest, dict, str]] = []
        self.cancelled: list[str] = []

    async def execute(self, request: WorkRequest, emit) -> WorkResult:  # noqa: ANN001
        self.executed.append(request)
        if self.activity is not None:
            await _emit_activity(emit, request.attempt_id, self.activity)
        return self.result

    async def resume(self, request: WorkRequest, checkpoint, decision: str, emit) -> WorkResult:  # noqa: ANN001
        self.resumed.append((request, dict(checkpoint), decision))
        if self.activity is not None:
            await _emit_activity(emit, request.attempt_id, self.activity)
        return self.result

    async def cancel(self, attempt_id: str) -> bool:
        self.cancelled.append(attempt_id)
        return self.cancel_ok

    def cancel_status(self, attempt_id: str) -> str | None:
        return self.cancel_state

    def recovery_checkpoint(self, activity) -> dict | None:  # noqa: ANN001
        details = dict((activity or {}).get("details") or {})
        session_id = str((activity or {}).get("session_id") or "")
        if not session_id or not details.get("snapshot_id"):
            return None
        return {
            "kind": "harness_session",
            "session_id": session_id,
            "snapshot_id": details["snapshot_id"],
            "original_attempt": (activity or {}).get("attempt_id"),
        }

    async def health(self) -> dict:
        return {"provider_id": self.capabilities.provider_id, "available": True, "reason": ""}


async def _emit_activity(emit, attempt_id: str, payload: dict) -> None:  # noqa: ANN001
    from agent_core.work_provider import WorkEvent

    record = {"attempt_id": attempt_id, "provider_id": "deepseek_harness", **payload}
    value = emit(WorkEvent(attempt_id, "provider_activity", record))
    if asyncio.iscoroutine(value):
        await value


def scope() -> Scope:
    return Scope(owner_id="alice", channel="web", conversation_id="chat", module_id="work")


def build_host(tmp_path: Path, *providers: StubProvider) -> WorkHost:
    return WorkHost(
        tmp_path / "work.db",
        provider=CompositeWorkProvider(list(providers)),
        max_workers=1,
        code_workers=1,
    )


async def wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.02) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_explicit_provider_selector_routes_the_attempt(tmp_path: Path) -> None:
    legacy = StubProvider("code-agent", selectable=False)
    harness = StubProvider("deepseek_harness")
    host = build_host(tmp_path, legacy, harness)
    await host.start()
    try:
        submitted = await host.submit(
            scope(),
            "改一下项目",
            task_type="code.agent",
            input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
        )
        await wait_for(lambda: host.get(submitted["work_item_id"], scope())["status"] == "completed")

        detail = host.get(submitted["work_item_id"], scope())
        assert detail["attempts"][0]["provider_id"] == "deepseek_harness"
        assert [request.provider_selector for request in harness.executed] == ["deepseek_harness"]
        assert legacy.executed == []
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_task_without_selector_runs_harness_and_records_its_identity(tmp_path: Path) -> None:
    harness = StubProvider("deepseek_harness")
    host = build_host(tmp_path, harness)
    await host.start()
    try:
        submitted = await host.submit(
            scope(),
            "改一下项目",
            task_type="code.agent",
            input={"project_root": str(tmp_path)},
        )
        await wait_for(lambda: host.get(submitted["work_item_id"], scope())["status"] == "completed")

        assert [request.provider_selector for request in harness.executed] == ["deepseek_harness"]
        assert host.get(submitted["work_item_id"], scope())["attempts"][0]["provider_id"] == "deepseek_harness"
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_unknown_provider_selector_is_rejected_before_persisting(tmp_path: Path) -> None:
    host = build_host(tmp_path, StubProvider("code-agent", selectable=False))
    await host.start()
    try:
        with pytest.raises(ValueError, match="unknown work provider"):
            await host.submit(
                scope(),
                "改一下项目",
                task_type="code.agent",
                input={"project_root": str(tmp_path), "provider": "not-a-provider"},
            )
        assert host.list(scope()) == []
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_provider_activity_is_persisted_and_exposed(tmp_path: Path) -> None:
    activity = {
        "status": "tool_running",
        "step": "执行工具：写文件",
        "tool": "Write app.py",
        "session_id": "sess-1",
        "confidence": "assumed",
        "started_at": now(),
        "details": {"snapshot_id": "snap-1"},
    }
    harness = StubProvider("deepseek_harness", activity=activity)
    host = build_host(tmp_path, harness)
    await host.start()
    try:
        submitted = await host.submit(
            scope(),
            "改一下项目",
            task_type="code.agent",
            input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
        )
        await wait_for(lambda: host.get(submitted["work_item_id"], scope())["status"] == "completed")

        detail = host.get(submitted["work_item_id"], scope())
        top = detail["provider_activity"]
        assert top["session_id"] == "sess-1"
        assert top["provider_name"] == "DeepSeek Harness"
        # The provider said "tool_running" and never closed the projection; the
        # Host must not leave a finished task looking busy.
        assert top["status"] == "completed"
        assert top["finished_at"]
        assert detail["attempts"][0]["provider_activity"]["status"] == "completed"
        stored = host.ledger.get_provider_activity(detail["attempts"][0]["attempt_id"])
        assert stored["tool"] == "Write app.py"
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_unconfirmed_cancel_ends_as_unknown_result(tmp_path: Path) -> None:
    harness = StubProvider(
        "deepseek_harness",
        result=WorkResult("failed", error="never got there"),
        cancel_ok=False,
        cancel_state="unknown",
    )
    host = build_host(tmp_path, harness)
    await host.start()
    try:
        submitted = await host.submit(
            scope(),
            "长任务",
            task_type="code.agent",
            input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
        )
        cancelled = await host.cancel(submitted["work_item_id"], scope())

        assert cancelled is False
        detail = host.get(submitted["work_item_id"], scope())
        assert detail["status"] == "unknown_result"
        assert detail["attempts"][0]["error"] == "cancel not confirmed by provider"
        events = [event["event_type"] for event in detail["events"]]
        assert "unknown_result" in events
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_unknown_result_cannot_be_retried_without_confirmation(tmp_path: Path) -> None:
    harness = StubProvider("deepseek_harness")
    host = build_host(tmp_path, harness)
    await host.start()
    try:
        work = host.ledger.create_work_item(
            scope=scope(),
            objective="结果未知的任务",
            task_type="code.agent",
            input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
        )
        attempt = host.ledger.create_attempt(work.work_item_id, provider_id="deepseek_harness")
        host.ledger.claim_attempt(attempt.attempt_id)
        host.ledger.finish_attempt(attempt.attempt_id, status="unknown_result", error="lost")

        with pytest.raises(ValueError, match="explicit confirmation"):
            await host.retry(work.work_item_id, scope())

        queued = await host.retry(work.work_item_id, scope(), confirm_unknown=True)
        assert queued["status"] == "queued"
        await wait_for(lambda: len(harness.executed) == 1)
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_restart_resumes_session_owning_attempt_once(tmp_path: Path) -> None:
    harness = StubProvider("deepseek_harness")
    host = build_host(tmp_path, harness)
    work = host.ledger.create_work_item(
        scope=scope(),
        objective="长任务",
        task_type="code.agent",
        input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
    )
    attempt = host.ledger.create_attempt(work.work_item_id, provider_id="deepseek_harness")
    host.ledger.claim_attempt(attempt.attempt_id)
    host.ledger.save_provider_activity(
        ProviderActivity(
            attempt_id=attempt.attempt_id,
            provider_id="deepseek_harness",
            status="tool_running",
            session_id="sess-1",
            started_at=now(),
            details={"snapshot_id": "snap-1"},
        )
    )

    await host.start()
    try:
        await wait_for(lambda: len(harness.resumed) == 1, timeout=15)
        await wait_for(
            lambda: host.get(work.work_item_id, scope())["status"] == "completed", timeout=15
        )

        attempts = host.ledger.list_attempts(work.work_item_id)
        assert len(attempts) == 2
        successor = next(item for item in attempts if item.attempt_id != attempt.attempt_id)
        checkpoint = host.ledger.get_checkpoint(successor.attempt_id)
        assert checkpoint["provider_checkpoint"]["session_id"] == "sess-1"
        assert checkpoint["resume_decision"] == "confirmed"
        _, checkpoint_arg, decision = harness.resumed[0]
        assert checkpoint_arg["kind"] == "harness_session"
        assert decision == "confirmed"

        # A second restart must not multiply attempts: the successor is the only
        # remaining candidate and it is already terminal.
        await host.stop()
        await host.start()
        await asyncio.sleep(0.2)
        assert len(host.ledger.list_attempts(work.work_item_id)) == 2
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_restart_never_resumes_unknown_result_attempt(tmp_path: Path) -> None:
    harness = StubProvider("deepseek_harness")
    host = build_host(tmp_path, harness)
    work = host.ledger.create_work_item(
        scope=scope(),
        objective="结果未知的长任务",
        task_type="code.agent",
        input={"project_root": str(tmp_path), "provider": "deepseek_harness"},
    )
    attempt = host.ledger.create_attempt(work.work_item_id, provider_id="deepseek_harness")
    host.ledger.claim_attempt(attempt.attempt_id)
    host.ledger.save_provider_activity(
        ProviderActivity(
            attempt_id=attempt.attempt_id,
            provider_id="deepseek_harness",
            status="unknown_result",
            session_id="sess-1",
            started_at=now(),
            details={"snapshot_id": "snap-1"},
        )
    )

    await host.start()
    try:
        await asyncio.sleep(0.3)
        assert harness.resumed == []
        assert len(host.ledger.list_attempts(work.work_item_id)) == 1
        assert host.get(work.work_item_id, scope())["status"] == "interrupted"
    finally:
        await host.stop()


def test_composite_describe_reports_selection_semantics(tmp_path: Path) -> None:
    legacy = StubProvider("code-agent", selectable=False)
    harness = StubProvider("deepseek_harness")
    composite = CompositeWorkProvider([legacy, harness])

    described = {item["provider_id"]: item for item in composite.describe()}

    assert described["code-agent"]["default"] is True
    assert described["code-agent"]["selectable"] is False
    assert described["deepseek_harness"]["default"] is False
    assert described["deepseek_harness"]["selectable"] is True
    assert composite.provider_for("code.agent") is legacy
    assert composite.provider_by_id("deepseek_harness") is harness


@pytest.mark.asyncio
async def test_composite_refuses_unknown_explicit_provider(tmp_path: Path) -> None:
    composite = CompositeWorkProvider([StubProvider("code-agent", selectable=False)])
    request = WorkRequest(
        work_item_id="w",
        attempt_id="a",
        scope=scope(),
        objective="x",
        task_type="code.agent",
        input={"project_root": str(tmp_path), "provider": "nope"},
    )

    result = await composite.execute(request, lambda event: None)

    assert result.status == "failed"
    assert "unknown work provider" in (result.error or "")


def test_work_module_registers_harness_as_only_code_provider(tmp_path: Path) -> None:
    """The real wiring path always defaults to Harness without a legacy owner."""
    from agent_core.work_module import build_work_module

    root = tmp_path / "projects"
    root.mkdir()
    _, host = build_work_module(
        db_path=tmp_path / "work.db",
        allowed_roots=[root],
        code_agent_enabled=True,
        harness_enabled=True,
        code_snapshot_root=tmp_path / "snapshots",
        code_artifact_dir=tmp_path / "artifacts",
        code_apply_state_root=tmp_path / "apply",
        code_sandbox_state_root=tmp_path / "sandbox",
        harness_sandbox_state_root=tmp_path / "harness-state",
    )

    harness = host.provider.provider_by_id("deepseek_harness")
    assert harness is not None
    assert harness.capabilities.selectable is True
    assert host.provider.provider_for("code.agent") is harness
    described = {item["provider_id"] for item in host.provider_registry()}
    assert described == {"local-worker", "deepseek_harness"}


def test_handshake_capability_gate_rejects_missing_resume() -> None:
    from agent_core.harness_config import HarnessConfig
    from agent_core.harness_sandbox import HarnessSandboxGate, SandboxUnavailable

    gate = HarnessSandboxGate(HarnessConfig(), cache_seconds=0)
    handshake = AcpHandshake.from_result(
        {"protocolVersion": 1, "agentCapabilities": {"sessionCapabilities": {"close": {}}}}
    )

    with pytest.raises(SandboxUnavailable) as caught:
        gate.confirm_capabilities(handshake)

    assert caught.value.reason == "acp_capability_missing"


def test_ledger_provider_activity_round_trip(tmp_path: Path) -> None:
    host = build_host(tmp_path, StubProvider("deepseek_harness"))
    activity = ProviderActivity(
        attempt_id="a1",
        provider_id="deepseek_harness",
        status="waiting_permission",
        session_id="s1",
        waiting_reason="等待授权：写文件",
        confidence="assumed",
        started_at=now(),
    )

    host.ledger.save_provider_activity(activity)
    stored = host.ledger.get_provider_activity("a1")

    assert stored["status"] == "waiting_permission"
    assert stored["waiting_reason"] == "等待授权：写文件"
    assert stored["provider_name"] == "DeepSeek Harness"
    assert stored["elapsed_ms"] >= 0
    assert json.loads(json.dumps(stored))["session_id"] == "s1"
    # elapsed_ms is computed per read, so it must not take part in the compare.
    bulk = host.ledger.list_provider_activities(["a1", "missing"])
    assert set(bulk) == {"a1"}
    stored.pop("elapsed_ms")
    bulk["a1"].pop("elapsed_ms")
    assert bulk["a1"] == stored
