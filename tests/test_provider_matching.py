"""Host 与 Provider 的能力匹配检查：按 task_type 报告、三档结论、真实身份记录。

三种被验收点名的情况都覆盖：Harness（验证链有/无两态）、local-worker、
不可用 provider。所有 matcher 结论都要与真实执行行为对得上：
degraded 就是"只出补丁"，rejected 就是"不该被选中"。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from agent_core.composite_work_provider import CompositeWorkProvider
from agent_core.contracts import Scope
from agent_core.deepseek_harness_provider import DeepSeekHarnessProvider
from agent_core.harness_acp import (
    ACP_METHOD_INITIALIZE,
    ACP_METHOD_SESSION_CLOSE,
    ACP_METHOD_SESSION_NEW,
    ACP_METHOD_SESSION_RESUME,
)
from agent_core.harness_sandbox import HarnessInspection, SandboxLease
from agent_core.local_worker_provider import LocalWorkerProvider
from agent_core.provider_matching import DEGRADED, MATCHED, REJECTED, evaluate_provider_match
from agent_core.work_host import WorkHost
from agent_core.work_module import _UnavailableHarnessProvider
from agent_core.work_provider import (
    TaskTypeCapabilities,
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
    capabilities_for_task_type,
)


# --------------------------------------------------------------------------- #
# Stubs
# --------------------------------------------------------------------------- #

HANDSHAKE_RESULT = {
    "protocolVersion": 1,
    "agentInfo": {"name": "mock-harness", "version": "0.2.0"},
    "agentCapabilities": {"sessionCapabilities": ["resume"]},
}


class FakeAcpConnection:
    """Scriptable ACP connection for the no-model handshake."""

    exit_code = 0

    def __init__(self, script: dict | None = None) -> None:
        self.script = dict(script or {})
        self.closed = False
        self.requests: list[str] = []

    async def request(self, method, params, timeout=None):  # noqa: ANN001
        self.requests.append(method)
        action = self.script.get(method)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(method, params)
        if action is not None:
            return action
        if method == ACP_METHOD_INITIALIZE:
            return HANDSHAKE_RESULT
        if method == ACP_METHOD_SESSION_NEW:
            return {"sessionId": "sess-health"}
        return {}

    async def notify(self, method, params):  # noqa: ANN002
        pass

    async def close(self, grace_seconds=5):  # noqa: ANN001
        self.closed = True
        return 0


def fake_connection_factory(connection: FakeAcpConnection):
    async def factory(spec, **handlers):  # noqa: ANN001
        return connection

    return factory


class StubGate:
    """Stand-in for the sandbox gate: probe + acquire without a process."""

    def __init__(self, config) -> None:
        self.config = config

    async def probe(self, *, workspace_root) -> HarnessInspection:
        return HarnessInspection(version="0.2.0", rows={"sandbox-policy": "present"}, raw="")

    async def acquire(self, *, attempt_id, workspace_root, allowed_roots=()) -> SandboxLease:
        return SandboxLease(
            attempt_id=attempt_id,
            mode="workspace-write",
            workspace_root=str(workspace_root),
            token="",
            token_path="",
            evidence={"version": "0.2.0", "mode": "workspace-write", "rows": ["sandbox-policy"]},
        )

    def confirm_capabilities(self, handshake) -> dict:
        return {"session_capabilities": ["resume"]}

    def release(self, lease) -> None:
        pass


class StubVerifier:
    async def verify(self, attempt_id, snapshot):
        raise AssertionError("matcher must never run verification")


class StubApplyCoordinator:
    enabled = False


def build_harness(
    tmp_path,
    *,
    verified: bool,
    executable: str = "",
    script: dict | None = None,
    connection: FakeAcpConnection | None = None,
    gate: StubGate | None = None,
    health_cache_seconds: float = 0.0,
) -> DeepSeekHarnessProvider:
    from agent_core.harness_config import HarnessConfig

    # 默认造一个真实存在的可执行文件：握手前有 spawn 检查，路径必须存在
    # 才能走到连接工厂那一层。
    if not executable:
        exe = tmp_path / "harness.exe"
        exe.write_bytes(b"stub")
        executable = str(exe)
    config = HarnessConfig(
        enabled=True,
        executable=executable,
        sandbox_mode="workspace-write",
        env={"DSH_PERMISSION_MODE": "workspace-write"},
    )
    return DeepSeekHarnessProvider(
        snapshot_service=object(),
        artifact_dir=tmp_path / "artifacts",
        config=config,
        sandbox_gate=gate or StubGate(config),
        connection_factory=fake_connection_factory(connection or FakeAcpConnection(script)),
        verification_service=StubVerifier() if verified else None,
        auto_apply_coordinator=StubApplyCoordinator() if verified else None,
        health_cache_seconds=health_cache_seconds,
    )


class StubLocalProvider:
    """A fully capable concrete executor without health probes."""

    def __init__(self, provider_id: str, task_types) -> None:
        self.capabilities = WorkProviderCapabilities(
            provider_id=provider_id,
            task_types=tuple(task_types),
            supports_cancel=True,
            supports_resume=True,
        )

    def task_capabilities(self, task_type: str) -> TaskTypeCapabilities:
        owned = task_type in self.capabilities.task_types
        return TaskTypeCapabilities(
            task_type=task_type,
            can_execute=owned,
            supports_cancel=owned,
            cancel_confirmable=owned,
            supports_resume=owned,
            resume_has_checkpoint=owned,
            emits_events=owned,
            accepts_snapshot=owned,
            verifiable_result=owned,
            host_verification_chain=owned,
            sandbox_confirmed=owned,
        )

    async def execute(self, request, emit):
        return WorkResult("completed", output={"provider": self.capabilities.provider_id})

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("completed", output={"decision": decision})


class BrokenProvider:
    """A registered executor that can prove nothing and cannot run."""

    capabilities = WorkProviderCapabilities(
        "broken_worker", ("code.agent",), supports_cancel=False, supports_resume=False,
        selectable=True,
    )

    async def health(self) -> dict:
        return {"provider_id": "broken_worker", "available": False, "reason": "no runtime"}

    async def execute(self, request, emit):
        return WorkResult("failed", error="broken")

    async def cancel(self, attempt_id):
        return False

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("failed", error="broken")


# --------------------------------------------------------------------------- #
# Per-task-type capability reports
# --------------------------------------------------------------------------- #


def test_harness_reports_code_agent_capabilities_per_task_type(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=True)
    report = harness.task_capabilities("code.agent")
    assert report.can_execute
    assert report.supports_cancel and report.cancel_confirmable
    assert report.supports_resume and report.resume_has_checkpoint
    assert report.emits_events and report.accepts_snapshot
    assert report.verifiable_result and report.host_verification_chain
    assert report.sandbox_confirmed
    # 非 code.agent 类型不归它：归属必须如实报 False，不能冒认。
    assert not harness.task_capabilities("file.list").can_execute


def test_harness_reports_missing_verification_chain_honestly(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=False)
    report = harness.task_capabilities("code.agent")
    assert report.can_execute
    assert not report.host_verification_chain
    assert any("补丁" in note for note in report.notes)


def test_composite_resolves_capabilities_from_real_owner() -> None:
    local = StubLocalProvider("local-worker", ("file.list",))
    code = StubLocalProvider("deepseek_harness", ("code.agent",))
    composite = CompositeWorkProvider([local, code])
    # 不再用全局 any()：组合层自身不声称取消/恢复能力。
    assert composite.capabilities.supports_cancel is False
    assert composite.capabilities.supports_resume is False
    assert composite.task_capabilities("file.list").can_execute
    assert composite.task_capabilities("code.agent").can_execute
    # 类型没人认领 → 匹配检查必须能据此拒绝。
    assert not composite.task_capabilities("screen.capture").can_execute


def test_local_worker_reports_local_task_capabilities(tmp_path) -> None:
    worker = LocalWorkerProvider(allowed_roots=[tmp_path])
    report = worker.task_capabilities("file.list")
    assert report.can_execute and report.cancel_confirmable
    assert not report.supports_resume  # 本地短任务没有恢复协议，如实报告
    assert not report.accepts_snapshot


def test_provider_without_report_derives_only_provable_flags() -> None:
    class Bare:
        capabilities = WorkProviderCapabilities("bare", ("code.agent",), supports_cancel=True, supports_resume=True)

    report = capabilities_for_task_type(Bare(), "code.agent")
    # 没有专门报告时，"验证链/快照/事件"不能被默认说成有。
    assert report.can_execute and report.supports_cancel
    assert not report.host_verification_chain
    assert not report.accepts_snapshot


# --------------------------------------------------------------------------- #
# Verdicts: matched / degraded / rejected
# --------------------------------------------------------------------------- #


def test_harness_with_verification_chain_is_matched(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=True)
    verdict = evaluate_provider_match(harness, "code.agent", provider_id="deepseek_harness")
    assert verdict.verdict == MATCHED
    assert verdict.reasons == ()


def test_harness_without_verifier_is_degraded_patch_only(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=False)
    verdict = evaluate_provider_match(harness, "code.agent", provider_id="deepseek_harness")
    assert verdict.verdict == DEGRADED
    assert any("验证链" in reason for reason in verdict.reasons)
    assert "verification" in {check.name for check in verdict.checks}


def test_health_outage_overrides_self_reported_capabilities(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=True)
    verdict = evaluate_provider_match(
        harness,
        "code.agent",
        provider_id="deepseek_harness",
        health={"provider_id": "deepseek_harness", "available": False, "reason": "config broken"},
    )
    # 自报全套能力、live 探测失败 → 按悲观方向收：rejected。
    assert verdict.verdict == REJECTED
    assert any("config broken" in reason for reason in verdict.reasons)


@pytest.mark.asyncio
async def test_unavailable_provider_is_rejected() -> None:
    broken = _UnavailableHarnessProvider("DeepSeek Harness is disabled by AGENT_HARNESS_ENABLED")
    health = await broken.health()
    verdict = evaluate_provider_match(
        broken, "code.agent", provider_id="deepseek_harness", health=health
    )
    assert verdict.verdict == REJECTED


def test_local_tasks_do_not_require_resume_or_snapshot(tmp_path) -> None:
    worker = LocalWorkerProvider(allowed_roots=[tmp_path])
    for task_type in ("file.list", "file.organize", "screen.capture", "system.volume.adjust"):
        verdict = evaluate_provider_match(worker, task_type, provider_id="local-worker")
        assert verdict.verdict == MATCHED, (task_type, verdict.reasons)


def test_unknown_task_type_ownership_is_rejected(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=True)
    verdict = evaluate_provider_match(harness, "file.list", provider_id="deepseek_harness")
    assert verdict.verdict == REJECTED
    assert any("file.list" in reason for reason in verdict.reasons)


# --------------------------------------------------------------------------- #
# Layered health: executable rules the verdict
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_health_handshake_failure_forces_rejected_even_when_probe_ok(tmp_path) -> None:
    from agent_core.harness_acp import ACP_METHOD_SESSION_NEW, AcpProtocolError

    harness = build_harness(
        tmp_path, verified=True, script={ACP_METHOD_SESSION_NEW: AcpProtocolError("refused")}
    )
    health = await harness.health()
    # 沙箱层是好的（available 旧语义不变），但 ACP 会话起不来 → 不可执行。
    assert health["available"] is True
    assert health["probe"]["ok"] is True
    assert health["handshake"]["ok"] is False
    assert health["executable"] is False
    assert health["failure_stage"] == "handshake.session_new"
    verdict = evaluate_provider_match(
        harness, "code.agent", provider_id="deepseek_harness", health=health
    )
    assert verdict.verdict == REJECTED
    assert any("不可执行" in reason for reason in verdict.reasons)


def test_health_without_executable_field_falls_back_to_available(tmp_path) -> None:
    harness = build_harness(tmp_path, verified=True)
    # 旧格式健康报告没有 executable 字段：退回 available 规则，不误杀。
    verdict = evaluate_provider_match(
        harness,
        "code.agent",
        provider_id="deepseek_harness",
        health={"provider_id": "deepseek_harness", "available": True},
    )
    assert verdict.verdict == MATCHED


@pytest.mark.asyncio
async def test_health_result_is_cached_in_process(tmp_path) -> None:
    connection = FakeAcpConnection()
    harness = build_harness(tmp_path, verified=True, connection=connection, health_cache_seconds=300.0)
    first = await harness.health()
    assert first.get("cached", False) is False
    assert first["executable"] is True
    second = await harness.health()
    assert second["cached"] is True
    # 缓存命中：握手只发生过一次，第二次没有再起会话。
    assert connection.requests.count(ACP_METHOD_INITIALIZE) == 1
    forced = await harness.health(force=True)
    assert forced.get("cached", False) is False
    assert connection.requests.count(ACP_METHOD_INITIALIZE) == 2


# --------------------------------------------------------------------------- #
# Host integration: real provider identity on attempts + diagnostics
# --------------------------------------------------------------------------- #


def build_composite_host(tmp_path, *, code_result: str = "completed") -> WorkHost:
    local = StubLocalProvider("local-worker", ("file.list",))
    code = StubLocalProvider("deepseek_harness", ("code.agent",))

    async def execute(request: WorkRequest, emit) -> WorkResult:
        if code_result == "failed" and request.task_type == "code.agent":
            return WorkResult("failed", error="injected failure")
        return await StubLocalProvider.execute(code, request, emit)

    code.execute = execute  # type: ignore[method-assign]
    composite = CompositeWorkProvider([local, code])
    return WorkHost(tmp_path / "work.db", provider=composite, max_workers=1, code_workers=1)


@pytest.mark.asyncio
async def test_plan_attempts_record_concrete_provider_ids(tmp_path) -> None:
    host = build_composite_host(tmp_path)
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat", module_id="work")
    # 先启动再提交：submit_plan 会把计划置为 running，而 start() 的恢复路径
    # 会把"运行中"的计划标记为中断（那是给 Host 重启用的语义）。
    await host.start()
    plan = await host.submit_plan(
        scope,
        "混合计划",
        [
            {"step_id": "s1", "task_type": "file.list", "objective": "列目录", "input": {"directory": str(tmp_path)}},
            {
                "step_id": "s2",
                "task_type": "code.agent",
                "objective": "改代码",
                "input": {"project_root": str(tmp_path)},
                "depends_on": ["s1"],
            },
        ],
    )
    try:
        for _ in range(300):
            detail = host.get_plan(plan["plan_id"], scope)
            if detail["status"] == "completed":
                break
            await asyncio.sleep(0.01)
        assert detail["status"] == "completed"
        for step in detail["steps"]:
            attempts = host.ledger.list_attempts(step["work_item_id"])
            assert attempts, step["step_id"]
            expected = "local-worker" if step["task_type"] == "file.list" else "deepseek_harness"
            assert attempts[0].provider_id == expected, step["step_id"]
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_plan_retry_attempt_keeps_concrete_provider(tmp_path) -> None:
    host = build_composite_host(tmp_path, code_result="failed")
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat", module_id="work")
    await host.start()
    plan = await host.submit_plan(
        scope,
        "会失败的计划",
        [
            {
                "step_id": "s1",
                "task_type": "code.agent",
                "objective": "改代码",
                "input": {"project_root": str(tmp_path)},
                "failure_policy": "retry",
            }
        ],
    )
    try:
        for _ in range(600):
            detail = host.get_plan(plan["plan_id"], scope)
            if detail["status"] in {"failed", "completed"}:
                break
            await asyncio.sleep(0.01)
        attempts = host.ledger.list_attempts(detail["steps"][0]["work_item_id"])
        assert len(attempts) >= 2  # 原始 attempt + 至少一次自动重试
        assert all(attempt.provider_id == "deepseek_harness" for attempt in attempts)
    finally:
        await host.stop()


@pytest.mark.asyncio
async def test_match_provider_resolves_composite_to_concrete_owner(tmp_path) -> None:
    host = build_composite_host(tmp_path)
    report = await host.match_provider(task_type="code.agent")
    assert report["provider_id"] == "deepseek_harness"
    assert report["verdict"] == MATCHED
    # 每种任务类型的结论只来自它自己的执行者。
    local_report = await host.match_provider(task_type="file.list")
    assert local_report["provider_id"] == "local-worker"
    assert local_report["verdict"] == MATCHED


@pytest.mark.asyncio
async def test_capability_report_covers_three_provider_kinds(tmp_path) -> None:
    local = StubLocalProvider("local-worker", ("file.list",))
    harness = build_harness(tmp_path, verified=False)
    composite = CompositeWorkProvider([local, harness, BrokenProvider()])
    host = WorkHost(tmp_path / "work.db", provider=composite)
    report = await host.capability_report()
    by_key = {(entry["provider_id"], entry["task_type"]): entry for entry in report}
    assert by_key[("local-worker", "file.list")]["verdict"] == MATCHED
    # Harness 能跑但缺验证链 → degraded（只出补丁）。
    assert by_key[("deepseek_harness", "code.agent")]["verdict"] == DEGRADED
    # 不可用 provider：健康探测失败 → rejected。
    assert by_key[("broken_worker", "code.agent")]["verdict"] == REJECTED
    assert any("no runtime" in reason for reason in by_key[("broken_worker", "code.agent")]["reasons"])


@pytest.mark.asyncio
async def test_startup_logs_capability_acceptance(tmp_path, caplog, monkeypatch) -> None:
    from agent_core import work_host as work_host_module

    # 验收日志默认有宽限期（给 live 探测让路）；测试里直接关掉。
    monkeypatch.setattr(work_host_module, "ACCEPTANCE_LOG_DELAY_SECONDS", 0.0)
    host = build_composite_host(tmp_path)
    with caplog.at_level(logging.INFO, logger="agent_core.work_host"):
        await host.start()
        try:
            for _ in range(100):
                if any("capability acceptance:" in record.message for record in caplog.records):
                    break
                await asyncio.sleep(0.01)
        finally:
            await host.stop()
    messages = [record.message for record in caplog.records if "capability acceptance:" in record.message]
    assert messages
    joined = "\n".join(messages)
    assert "provider=local-worker task_type=file.list verdict=matched" in joined
    assert "provider=deepseek_harness task_type=code.agent" in joined
