"""Provider-level tests for the DeepSeek Harness ACP bridge.

The agent is the mock ACP subprocess (real stdio, real framing). Only two things
are stubbed, and each for a stated reason:

* the sandbox CLI probe, because the installed Harness must not be started by
  unit tests — the real gate is still exercised, with a fake ``--version`` /
  ``--dump-config`` runner, so row parsing and confirmation logic are covered;
* the Host verifier, because verification belongs to a different test file.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from agent_core.code_agent_verification import VerificationRecord
from agent_core.contracts import Scope
from agent_core.deepseek_harness_provider import (
    CHECKPOINT_CODE_APPLY,
    CHECKPOINT_HARNESS_SESSION,
    DeepSeekHarnessProvider,
)
from agent_core.harness_config import HarnessConfig, HarnessTimeouts, VersionRequirement
from agent_core.harness_acp import (
    ACP_METHOD_INITIALIZE,
    ACP_METHOD_SESSION_NEW,
    ACP_METHOD_SESSION_RESUME,
    AcpProtocolError,
    AcpSpawnError,
    AcpTimeout,
)
from agent_core.harness_sandbox import (
    CliResult,
    HarnessSandboxGate,
    SandboxUnavailable,
)
from agent_core.provider_activity import PROVIDER_ACTIVITY_EVENT_TYPE
from agent_core.work_provider import WorkRequest
from agent_core.workspace_snapshot import WorkspaceSnapshotService

MOCK_AGENT = Path(__file__).parent / "support" / "mock_acp_agent.py"

DUMP_CONFIG = """- id: sandbox-policy
  name: '@deepseek-ai/dsh-sandbox-policy'
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    workspaceRoot: !!js process.cwd()
- id: pwsh-sandbox
  name: '@deepseek-ai/dsh-pwsh-sandbox'
  disabled: !!js process.platform !== 'win32'
- id: approval
  name: '@deepseek-ai/dsh-user-approval'
  config:
    policy: !!js >-
      (process.env.DSH_PERMISSION_MODE ?? 'workspace-write') ===
      'danger-full-access' ? 'never' : 'ask'
"""


async def fake_cli(argv, *, cwd, env, timeout):  # noqa: ANN001 - matches CliRunner
    if "--version" in argv:
        return CliResult(0, "0.2.0-rc.2\n")
    return CliResult(0, DUMP_CONFIG)


def make_config(tmp_path: Path, scenario: dict | None = None, **overrides) -> HarnessConfig:
    report = tmp_path / "report.jsonl"
    payload = dict(scenario or {})
    environment = {
        "MOCK_ACP_SCENARIO": json.dumps(payload),
        "MOCK_ACP_REPORT": str(report),
    }
    timeouts = HarnessTimeouts(
        preflight=10, initialize=15, session=15, prompt=8, cancel=2, close=5
    )
    settings = dict(
        enabled=True,
        executable=sys.executable,
        entry_args=(str(MOCK_AGENT),),
        profile="acp",
        env=environment,
        required_version=VersionRequirement(minimum="0.0.1"),
        timeouts=timeouts,
        prompt_timeout=8,
        sandbox_state_root=str(tmp_path / "sandbox-state"),
        max_message_chars=2000,
    )
    settings.update(overrides)
    return HarnessConfig(**settings)


def read_report(tmp_path: Path) -> list[dict]:
    path = tmp_path / "report.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class Verifier:
    def __init__(self, status: str = "verified") -> None:
        self.status = status

    async def verify(self, attempt_id, snapshot):  # noqa: ANN001
        return VerificationRecord(
            status=self.status,
            reason=self.status,
            project_root=str(snapshot.project_root),
            before_fingerprint="a" * 64,
            after_fingerprint="a" * 64,
            commands=(),
        )


def build(tmp_path: Path, *, scenario: dict | None = None, verification=None, config=None, gate=None, factory=None):
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    harness_config = config or make_config(tmp_path, scenario)
    provider = DeepSeekHarnessProvider(
        snapshot_service=snapshots,
        artifact_dir=tmp_path / "artifacts",
        config=harness_config,
        sandbox_gate=gate or HarnessSandboxGate(harness_config, runner=fake_cli),
        connection_factory=factory,
        verification_service=verification,
        allowed_roots=[allowed],
    )
    request = WorkRequest(
        work_item_id="work-1",
        attempt_id="attempt-1",
        scope=Scope(owner_id="alice", channel="web", conversation_id="chat"),
        objective="把 app.py 里的 value 改成 2",
        task_type="code.agent",
        input={"project_root": str(project)},
        provider_id="deepseek_harness",
    )
    return provider, project, request


def activity_events(events) -> list[dict]:  # noqa: ANN001
    return [
        dict(event.payload)
        for event in events
        if event.event_type == PROVIDER_ACTIVITY_EVENT_TYPE
    ]


@pytest.mark.asyncio
async def test_successful_run_produces_patch_and_activity(tmp_path: Path) -> None:
    scenario = {
        "session_id": "sess-1",
        "updates": [
            {"sessionUpdate": "agent_thought_chunk"},
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Write app.py",
                "kind": "other",
                "status": "in_progress",
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "t1",
                "status": "completed",
            },
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "已把 value 改成 2"},
            },
        ],
        "write_files": {"app.py": "value = 2\n"},
        "stop_reason": "end_turn",
    }
    provider, project, request = build(tmp_path, scenario=scenario)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "completed"
    assert "value = 2" in result.output["patch"]
    assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert result.output["session_id"] == "sess-1"
    assert result.output["stop_reason"] == "end_turn"
    assert "value 改成 2" in result.output["message"]
    assert {item["title"] for item in result.output["tool_calls"]} == {"Write app.py"}
    assert {artifact.media_type for artifact in result.artifacts} == {
        "text/x-diff",
        "application/json",
    }

    statuses = [item["status"] for item in activity_events(events)]
    assert statuses[0] == "connecting"
    assert "tool_running" in statuses
    assert statuses[-1] == "completed"
    assert any(event.event_type == "provider_session" for event in events)
    # No host verifier was configured: the check never ran, so the result must
    # not claim certainty — and must say "not_run", not "unverified".
    assert result.output["verification_status"] == "not_run"
    assert activity_events(events)[-1]["confidence"] == "assumed"
    final = activity_events(events)[-1]
    assert final["session_id"] == "sess-1"
    assert final["details"]["sandbox"]["mode"] == "workspace-write"
    assert final["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_verified_run_reports_confirmed_confidence(tmp_path: Path) -> None:
    scenario = {
        "write_files": {"app.py": "value = 3\n"},
        "updates": [
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}
        ],
    }
    provider, _project, request = build(tmp_path, scenario=scenario, verification=Verifier())
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "completed"
    assert result.output["verification_status"] == "verified"
    assert activity_events(events)[-1]["confidence"] == "confirmed"
    assert activity_events(events)[-1]["finished_at"]


@pytest.mark.asyncio
async def test_permission_inside_snapshot_is_allowed(tmp_path: Path) -> None:
    scenario = {
        "permission_requests": 1,
        "permission_raw_input": {"path": "__CWD__/src/new_file.py"},
        "write_files": {"app.py": "value = 4\n"},
    }
    provider, _project, request = build(tmp_path, scenario=scenario)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "completed"
    answers = [item["answer"] for item in read_report(tmp_path) if item.get("event") == "permission"]
    assert answers[0]["result"]["outcome"]["optionId"] == "allow-once"
    permissions = [
        event.payload for event in events if event.event_type == "provider_permission"
    ]
    assert permissions[0]["decision"] == "allowed"
    assert permissions[0]["scope"] == "snapshot"


@pytest.mark.asyncio
async def test_permission_outside_snapshot_is_denied(tmp_path: Path) -> None:
    scenario = {
        "permission_requests": 1,
        "permission_raw_input": {"path": "C:/Windows/System32/drivers/etc/hosts"},
    }
    provider, _project, request = build(tmp_path, scenario=scenario)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    # The agent may keep going after a rejected tool call; what must never
    # happen is the tool call being allowed.
    assert result.status in {"completed", "cancelled"}
    answers = [item["answer"] for item in read_report(tmp_path) if item.get("event") == "permission"]
    assert answers[0]["result"]["outcome"]["optionId"] == "reject-once"
    permissions = [
        event.payload for event in events if event.event_type == "provider_permission"
    ]
    assert permissions[0]["decision"] == "denied"
    assert permissions[0]["scope"] == "outside_snapshot"


@pytest.mark.asyncio
async def test_permission_for_network_operation_is_denied(tmp_path: Path) -> None:
    scenario = {
        "permission_requests": 1,
        "permission_tool_title": "Fetch docs",
        "permission_raw_input": {"url": "https://example.com/spec"},
    }
    provider, _project, request = build(tmp_path, scenario=scenario)
    events = []

    await provider.execute(request, lambda event: events.append(event))

    permissions = [
        event.payload for event in events if event.event_type == "provider_permission"
    ]
    assert permissions[0]["decision"] == "denied"
    assert permissions[0]["scope"] == "network"


@pytest.mark.asyncio
async def test_permission_without_evidence_is_allowed_but_flagged(tmp_path: Path) -> None:
    scenario = {
        "permission_requests": 1,
        "permission_raw_input": {},
        "write_files": {"app.py": "value = 5\n"},
    }
    provider, _project, request = build(tmp_path, scenario=scenario)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "completed"
    permissions = [
        event.payload for event in events if event.event_type == "provider_permission"
    ]
    assert permissions[0]["scope"] == "unverified"
    # An unverifiable scope must lower the reported confidence, never hide it.
    assert activity_events(events)[-1]["confidence"] == "assumed"


@pytest.mark.asyncio
async def test_process_crash_yields_unknown_result(tmp_path: Path) -> None:
    scenario = {
        "updates": [{"sessionUpdate": "agent_thought_chunk"}],
        "crash_after_updates": True,
        "crash_exit_code": 7,
    }
    provider, _project, request = build(tmp_path, scenario=scenario)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "unknown_result"
    assert "harness_disconnected" in (result.error or "")
    assert result.output["exit_code"] == 7
    disconnects = [
        event.payload for event in events if event.event_type == "provider_disconnected"
    ]
    assert disconnects and disconnects[0]["exit_code"] == 7
    final = activity_events(events)[-1]
    assert final["status"] == "unknown_result"
    assert final["confidence"] == "unknown"


@pytest.mark.asyncio
async def test_prompt_timeout_yields_unknown_result(tmp_path: Path) -> None:
    config = make_config(tmp_path, {"hang_prompt": True}, prompt_timeout=1)
    config = HarnessConfig(**{**config.__dict__, "timeouts": HarnessTimeouts(preflight=10, initialize=15, session=15, prompt=1, cancel=1, close=5)})
    provider, _project, request = build(tmp_path, config=config)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "unknown_result"
    assert "harness_timeout" in (result.error or "")
    assert activity_events(events)[-1]["status"] == "unknown_result"


@pytest.mark.asyncio
async def test_cancel_is_confirmed_by_stop_reason(tmp_path: Path) -> None:
    provider, _project, request = build(tmp_path, scenario={"hang_prompt": True})
    events = []
    task = asyncio.ensure_future(provider.execute(request, lambda event: events.append(event)))
    await _wait_for_status(events, "thinking")

    cancelled = await provider.cancel("attempt-1")
    result = await task

    assert cancelled is True
    assert provider.cancel_status("attempt-1") == "confirmed"
    assert result.status == "cancelled"
    assert activity_events(events)[-1]["status"] == "cancelled"
    assert any(item.get("event") == "cancel" for item in read_report(tmp_path))


@pytest.mark.asyncio
async def test_unconfirmed_cancel_marks_unknown_result(tmp_path: Path) -> None:
    provider, _project, request = build(
        tmp_path, scenario={"hang_prompt": True, "ignore_cancel": True}
    )
    events = []
    task = asyncio.ensure_future(provider.execute(request, lambda event: events.append(event)))
    await _wait_for_status(events, "thinking")

    cancelled = await provider.cancel("attempt-1")

    assert cancelled is False
    assert provider.cancel_status("attempt-1") == "unknown"
    assert activity_events(events)[-1]["status"] == "unknown_result"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_missing_acp_capability_fails_closed(tmp_path: Path) -> None:
    provider, _project, request = build(tmp_path, scenario={"session_capabilities": {}})
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "failed"
    assert "acp_capability_missing" in (result.error or "")
    # The refusal happens before any session exists, so no work could have started.
    assert not any(item.get("event") == "session_new" for item in read_report(tmp_path))
    assert activity_events(events)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_sandbox_failure_never_starts_the_agent(tmp_path: Path) -> None:
    spawned: list[object] = []

    async def factory(*args, **kwargs):  # noqa: ANN002, ANN003
        spawned.append(args)
        raise AssertionError("the bridge must not spawn the Harness when the sandbox gate fails")

    config = make_config(tmp_path, {}, executable=str(tmp_path / "missing" / "DeepSeek Harness.exe"))
    provider, _project, request = build(tmp_path, config=config, factory=factory)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "failed"
    assert "harness" in (result.error or "")
    assert spawned == []
    unavailable = [
        event.payload for event in events if event.event_type == "provider_unavailable"
    ]
    assert unavailable and unavailable[0]["reason"] in {
        "harness_not_runnable",
        "version_unreadable",
        "harness_not_found",
    }
    assert activity_events(events)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_resume_uses_session_resume_and_continues(tmp_path: Path) -> None:
    scenario = {
        "write_files": {"app.py": "value = 6\n"},
        "updates": [
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "继续完成"}}
        ],
    }
    provider, _project, request = build(tmp_path, scenario=scenario)
    snapshot = provider.lifecycle.create_snapshot("attempt-old", request.task_input["project_root"])
    checkpoint = {
        "kind": CHECKPOINT_HARNESS_SESSION,
        "session_id": "sess-old",
        "snapshot_id": snapshot.snapshot_id,
        "original_attempt": "attempt-old",
    }
    events = []

    result = await provider.resume(request, checkpoint, "confirmed", lambda event: events.append(event))

    assert result.status == "completed"
    assert result.output["resumed"] is True
    assert result.output["session_id"] == "sess-old"
    assert any(item.get("event") == "session_resume" for item in read_report(tmp_path))


@pytest.mark.asyncio
async def test_resume_refused_reports_unknown_result(tmp_path: Path) -> None:
    provider, _project, request = build(tmp_path, scenario={"resume_fails": True})
    snapshot = provider.lifecycle.create_snapshot("attempt-old", request.task_input["project_root"])
    checkpoint = {
        "kind": CHECKPOINT_HARNESS_SESSION,
        "session_id": "sess-old",
        "snapshot_id": snapshot.snapshot_id,
        "original_attempt": "attempt-old",
    }
    events = []

    result = await provider.resume(request, checkpoint, "confirmed", lambda event: events.append(event))

    assert result.status == "unknown_result"
    assert "acp_resume_refused" in (result.error or "")
    assert activity_events(events)[-1]["status"] == "unknown_result"


@pytest.mark.asyncio
async def test_resume_checkpoint_mismatch_is_unknown(tmp_path: Path) -> None:
    provider, _project, request = build(tmp_path, scenario={})
    provider.lifecycle.create_snapshot("attempt-old", request.task_input["project_root"])
    checkpoint = {
        "kind": CHECKPOINT_HARNESS_SESSION,
        "session_id": "sess-old",
        "snapshot_id": "not-the-snapshot",
        "original_attempt": "attempt-old",
    }

    result = await provider.resume(request, checkpoint, "confirmed", lambda event: None)

    assert result.status == "unknown_result"
    assert "snapshot" in (result.error or "")


@pytest.mark.asyncio
async def test_apply_approval_checkpoint_uses_shared_lifecycle(tmp_path: Path) -> None:
    provider, _project, request = build(tmp_path, scenario={})
    snapshot = provider.lifecycle.create_snapshot("attempt-old", request.task_input["project_root"])
    checkpoint = {
        "kind": CHECKPOINT_CODE_APPLY,
        "snapshot_id": snapshot.snapshot_id,
        "original_attempt": "attempt-old",
        "content_fingerprint": "x",
        "verification": {
            "status": "verified",
            "reason": "verified",
            "project_root": str(snapshot.project_root),
            "before_fingerprint": "a" * 64,
            "after_fingerprint": "a" * 64,
            "commands": [],
        },
        "outcome": {"summary": "done", "output": {}, "evidence": []},
    }

    result = await provider.resume(request, checkpoint, "rejected", lambda event: None)

    assert result.status == "completed"
    assert result.output["apply_status"] == "patch_only"
    assert result.output["apply"]["reason"] == "approval_rejected"


def test_sandbox_gate_refuses_a_mode_the_harness_reports_differently(tmp_path: Path) -> None:
    """A dump that says `read-only` while we asked for `workspace-write` is refused."""
    from agent_core.harness_sandbox import parse_config_rows

    mismatched = DUMP_CONFIG.replace(
        "mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'", "mode: read-only"
    )
    config = make_config(tmp_path, {})
    gate = HarnessSandboxGate(config, runner=fake_cli, cache_seconds=0)
    inspection = _inspection("0.2.0-rc.2", parse_config_rows(mismatched))

    with pytest.raises(SandboxUnavailable) as caught:
        gate.confirm_sandbox(inspection, workspace_root=tmp_path)

    assert caught.value.reason == "sandbox_mode_not_confirmed"


def _inspection(version: str, rows: dict) -> object:
    from agent_core.harness_sandbox import HarnessInspection

    return HarnessInspection(version=version, rows=rows)


def test_recovery_checkpoint_requires_session_and_snapshot() -> None:
    provider = DeepSeekHarnessProvider.__new__(DeepSeekHarnessProvider)

    assert provider.recovery_checkpoint({"session_id": "s", "details": {}}) is None
    assert (
        provider.recovery_checkpoint(
            {"session_id": "s", "details": {"snapshot_id": "snap"}, "status": "unknown_result"}
        )
        is None
    )
    checkpoint = provider.recovery_checkpoint(
        {
            "attempt_id": "a1",
            "session_id": "s1",
            "status": "tool_running",
            "details": {"snapshot_id": "snap-1"},
        }
    )
    assert checkpoint == {
        "kind": CHECKPOINT_HARNESS_SESSION,
        "session_id": "s1",
        "snapshot_id": "snap-1",
        "original_attempt": "a1",
        "previous_status": "tool_running",
    }


@pytest.mark.asyncio
async def test_incompatible_harness_version_fails_closed(tmp_path: Path) -> None:
    async def old_runner(argv, *, cwd, env, timeout):  # noqa: ANN001
        if "--version" in argv:
            return CliResult(0, "0.1.9\n")
        return CliResult(0, DUMP_CONFIG)

    config = make_config(tmp_path, {}, required_version=VersionRequirement(minimum="0.2.0"))
    gate = HarnessSandboxGate(config, runner=old_runner, cache_seconds=0)
    provider, _project, request = build(tmp_path, config=config, gate=gate)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "failed"
    assert "version_incompatible" in (result.error or "")
    assert not any(item.get("event") == "session_new" for item in read_report(tmp_path))
    unavailable = [
        event.payload for event in events if event.event_type == "provider_unavailable"
    ]
    assert unavailable and unavailable[0]["reason"] == "version_incompatible"


def test_version_requirement_accepts_prereleases_of_the_floor() -> None:
    from agent_core.harness_config import VersionRequirement, parse_version

    assert parse_version("0.2.0-rc.2") == ((0, 2, 0), "rc.2")
    assert parse_version("not-a-version") is None
    assert VersionRequirement(minimum="0.2.0").satisfied_by("0.2.0-rc.2") is True
    assert VersionRequirement(minimum="0.2.0", allow_prerelease=False).satisfied_by("0.2.0-rc.2") is False
    assert VersionRequirement(minimum="0.2.0").satisfied_by("0.3.1") is True
    assert VersionRequirement(minimum="0.2.0").satisfied_by("0.1.9") is False
    assert VersionRequirement(minimum="0.2.0").satisfied_by("") is False


@pytest.mark.asyncio
async def test_health_reports_configuration_problems(tmp_path: Path) -> None:
    config = make_config(tmp_path, {}, executable="")
    provider, _project, _request = build(tmp_path, config=config)

    report = await provider.health()

    assert report["available"] is False
    assert "AGENT_HARNESS_EXECUTABLE" in report["reason"]


@pytest.mark.asyncio
async def test_health_reports_available_with_fake_probe(tmp_path: Path) -> None:
    provider, _project, _request = build(tmp_path, scenario={})

    report = await provider.health()

    assert report["available"] is True
    assert report["version"] == "0.2.0-rc.2"
    assert report["acp"] is True
    assert report["sandbox"] == "workspace-write"


@pytest.mark.asyncio
async def test_forbidden_sandbox_mode_is_refused_at_gate(tmp_path: Path) -> None:
    config = make_config(tmp_path, {}, sandbox_mode="danger-full-access")
    provider, _project, request = build(tmp_path, config=config)
    events = []

    result = await provider.execute(request, lambda event: events.append(event))

    assert result.status == "failed"
    unavailable = [
        event.payload for event in events if event.event_type == "provider_unavailable"
    ]
    assert unavailable and unavailable[0]["reason"] == "harness_unavailable"
    assert "unsandboxed" in unavailable[0]["detail"]


async def _wait_for_status(events, status: str, *, timeout: float = 10.0) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if any(
            event.event_type == PROVIDER_ACTIVITY_EVENT_TYPE
            and dict(event.payload).get("status") == status
            for event in events
        ):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"provider never reported status {status}")


# --------------------------------------------------------------------------- #
# Layered health: no-model probe + ACP handshake, failure_stage per layer
# --------------------------------------------------------------------------- #


def make_probe_runner(version: str = "0.2.0-rc.2", dump: str = DUMP_CONFIG):
    async def runner(argv, *, cwd, env, timeout):  # noqa: ANN001
        if "--version" in argv:
            return CliResult(0, f"{version}\n")
        return CliResult(0, dump)

    return runner


def scripted_factory(connection):
    async def factory(spec, **handlers):  # noqa: ANN001
        return connection

    return factory


class ScriptedConnection:
    """One ACP connection whose replies (or errors) are scripted per method."""

    exit_code = 0

    def __init__(self, script: dict) -> None:
        self.script = dict(script)

    async def request(self, method, params, timeout=None):  # noqa: ANN001
        action = self.script.get(method)
        if isinstance(action, Exception):
            raise action
        if callable(action):
            return action(method, params)
        if method == ACP_METHOD_INITIALIZE:
            return HANDSHAKE_INIT_RESULT
        if method == ACP_METHOD_SESSION_NEW:
            return {"sessionId": "sess-health"}
        return {}

    async def notify(self, method, params):  # noqa: ANN002
        pass

    async def close(self, grace_seconds=5):  # noqa: ANN001
        return 0


HANDSHAKE_INIT_RESULT = {
    "protocolVersion": 1,
    "agentInfo": {"name": "mock-acp", "version": "9.9.9"},
    "agentCapabilities": {"sessionCapabilities": {"resume": {}, "close": {}, "list": {}}},
    "authMethods": [],
}


def build_health_provider(tmp_path: Path, *, runner=None, factory=None, scenario=None):
    allowed = tmp_path / "projects"
    (allowed / "demo").mkdir(parents=True, exist_ok=True)
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    config = make_config(tmp_path, scenario)
    gate = HarnessSandboxGate(config, runner=runner or fake_cli, cache_seconds=0)
    return DeepSeekHarnessProvider(
        snapshot_service=snapshots,
        artifact_dir=tmp_path / "artifacts",
        config=config,
        sandbox_gate=gate,
        connection_factory=factory,
        allowed_roots=[allowed],
        health_cache_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_health_reports_version_incompatibility_as_probe_failure(tmp_path: Path) -> None:
    provider = build_health_provider(tmp_path, runner=make_probe_runner(version="0.0.0"))
    health = await provider.health()
    assert health["available"] is False
    assert health["executable"] is False
    assert health["failure_stage"] == "probe.version"
    assert "version_incompatible" in health["reason"]


@pytest.mark.asyncio
async def test_health_reports_missing_sandbox_rows_as_probe_failure(tmp_path: Path) -> None:
    dump = "- id: approval\n  name: '@x/approval'\n"
    provider = build_health_provider(tmp_path, runner=make_probe_runner(dump=dump))
    health = await provider.health()
    assert health["available"] is False
    assert health["executable"] is False
    assert health["failure_stage"] == "probe.sandbox"
    assert "sandbox_configuration_missing" in health["reason"]


@pytest.mark.asyncio
async def test_health_reports_workspace_mismatch_as_probe_failure(tmp_path: Path) -> None:
    dump = DUMP_CONFIG.replace("process.cwd()", "'C:/elsewhere/project'")
    provider = build_health_provider(tmp_path, runner=make_probe_runner(dump=dump))
    health = await provider.health()
    assert health["failure_stage"] == "probe.sandbox"
    assert "sandbox_workspace_mismatch" in health["reason"]
    assert health["executable"] is False


@pytest.mark.asyncio
async def test_health_reports_unconditional_allow_as_probe_failure(tmp_path: Path) -> None:
    dump = DUMP_CONFIG.replace(
        """policy: !!js >-
      (process.env.DSH_PERMISSION_MODE ?? 'workspace-write') ===
      'danger-full-access' ? 'never' : 'ask'""",
        "policy: never",
    )
    provider = build_health_provider(tmp_path, runner=make_probe_runner(dump=dump))
    health = await provider.health()
    assert health["failure_stage"] == "probe.sandbox"
    assert "sandbox_approval_never" in health["reason"]


@pytest.mark.asyncio
async def test_health_spawn_failure_keeps_probe_ok_but_not_executable(tmp_path: Path) -> None:
    def factory(spec, **handlers):  # noqa: ANN001
        raise AcpSpawnError("cannot start harness")

    provider = build_health_provider(tmp_path, factory=factory)
    health = await provider.health()
    # 沙箱探测是好的（available 旧语义不变），但进程起不来 → 不可执行。
    assert health["available"] is True
    assert health["probe"]["ok"] is True
    assert health["handshake"]["ok"] is False
    assert health["handshake"]["stage"] == "handshake.spawn"
    assert health["executable"] is False
    assert health["failure_stage"] == "handshake.spawn"


@pytest.mark.asyncio
async def test_health_initialize_timeout_fails_handshake(tmp_path: Path) -> None:
    connection = ScriptedConnection({ACP_METHOD_INITIALIZE: AcpTimeout("init timeout")})
    provider = build_health_provider(tmp_path, factory=scripted_factory(connection))
    health = await provider.health()
    assert health["available"] is True
    assert health["handshake"]["stage"] == "handshake.initialize"
    assert health["handshake"]["initialize"] is False
    assert health["executable"] is False


@pytest.mark.asyncio
async def test_health_session_create_failure_fails_handshake(tmp_path: Path) -> None:
    connection = ScriptedConnection({ACP_METHOD_SESSION_NEW: AcpProtocolError("refused")})
    provider = build_health_provider(tmp_path, factory=scripted_factory(connection))
    health = await provider.health()
    assert health["handshake"]["initialize"] is True
    assert health["handshake"]["stage"] == "handshake.session_new"
    assert health["handshake"]["session_new"] is False
    assert health["executable"] is False


@pytest.mark.asyncio
async def test_health_resume_failure_fails_handshake(tmp_path: Path) -> None:
    connection = ScriptedConnection(
        {
            ACP_METHOD_SESSION_RESUME: AcpProtocolError("not resumable"),
            "session/load": AcpProtocolError("not resumable"),
        }
    )
    provider = build_health_provider(tmp_path, factory=scripted_factory(connection))
    health = await provider.health()
    assert health["handshake"]["session_new"] is True
    assert health["handshake"]["close"] is True
    assert health["handshake"]["stage"] == "handshake.resume"
    assert health["handshake"]["resume"] is False
    assert health["executable"] is False


@pytest.mark.asyncio
async def test_health_no_model_handshake_succeeds_against_mock_agent(tmp_path: Path) -> None:
    """The full no-model sequence (no session/prompt) proves the session chain."""
    provider = build_health_provider(tmp_path)
    health = await provider.health()
    assert health["probe"]["ok"] is True
    assert health["probe"]["sandbox_confirmed"] is True
    handshake = health["handshake"]
    assert handshake["ok"] is True
    assert handshake["initialize"] and handshake["session_new"]
    assert handshake["close"] and handshake["resume"]
    assert handshake["session_capabilities"] == ["close", "list", "resume"]
    assert health["executable"] is True
    assert health["available"] is True
    assert health["failure_stage"] == ""
    assert not health.get("cached", False)
