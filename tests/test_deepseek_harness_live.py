"""Live integration tests against the installed DeepSeek Harness.

These are skipped by default: they start a real desktop Harness process, and the
prompt variant spends real model credits. Enable with:

    AGENT_HARNESS_LIVE=1            # protocol-level checks
    AGENT_HARNESS_LIVE_PROMPT=1     # the full execute() run, with one prompt

What the protocol test proves: the bridge's own transport, capability gate and
resume path work against the shipped build, not just against the mock. What the
prompt test proves: a task submitted to the provider really edits only the
snapshot, and the Host still produces the patch.

If the Harness is not installed the tests skip with an explicit reason instead of
failing, because "not installed" is a deployment state, not a regression.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from agent_core.contracts import Scope
from agent_core.deepseek_harness_provider import DeepSeekHarnessProvider
from agent_core.harness_acp import (
    ACP_METHOD_INITIALIZE,
    ACP_METHOD_SESSION_CLOSE,
    ACP_METHOD_SESSION_NEW,
    ACP_METHOD_SESSION_RESUME,
    AcpConnection,
    AcpLaunchSpec,
)
from agent_core.harness_config import HarnessConfig, parse_version
from agent_core.harness_sandbox import HarnessSandboxGate, SandboxUnavailable, harness_argv
from agent_core.work_provider import WorkRequest
from agent_core.workspace_snapshot import WorkspaceSnapshotService

LIVE = os.getenv("AGENT_HARNESS_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}
LIVE_PROMPT = os.getenv("AGENT_HARNESS_LIVE_PROMPT", "").strip().lower() in {"1", "true", "yes", "on"}

pytestmark = pytest.mark.skipif(
    not LIVE, reason="set AGENT_HARNESS_LIVE=1 to run against the installed Harness"
)


def live_config(**overrides) -> HarnessConfig:
    config = HarnessConfig.from_env()
    if not str(config.executable).strip() or not Path(config.executable).is_file():
        pytest.skip("DeepSeek Harness is not installed on this machine")
    return config


def live_launch(config: HarnessConfig, lease, workspace: Path) -> AcpLaunchSpec:
    argv = harness_argv(config, profile_args=())
    return AcpLaunchSpec(
        executable=argv[0],
        args=tuple(argv[1:]),
        cwd=str(workspace),
        env={**config.env, **lease.env_overrides},
    )


@pytest.mark.asyncio
async def test_live_handshake_session_resume_and_close(tmp_path: Path) -> None:
    config = live_config()
    gate = HarnessSandboxGate(config, cache_seconds=0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")
    lease = await gate.acquire(
        attempt_id="live-protocol", workspace_root=workspace, allowed_roots=[tmp_path]
    )
    connection = await AcpConnection.spawn(live_launch(config, lease, workspace))
    try:
        handshake = await connection.request(
            ACP_METHOD_INITIALIZE,
            {"protocolVersion": int(config.protocol_version), "clientCapabilities": {}},
            timeout=float(config.timeouts.initialize),
        )
        from agent_core.harness_acp import AcpHandshake

        parsed = AcpHandshake.from_result(handshake)
        evidence = gate.confirm_capabilities(parsed)
        assert evidence["protocol_version"] == config.protocol_version
        assert "resume" in evidence["session_capabilities"]
        assert parsed.name_text

        created = await connection.request(
            ACP_METHOD_SESSION_NEW,
            {"cwd": str(workspace), "mcpServers": []},
            timeout=float(config.timeouts.session),
        )
        session_id = created["sessionId"]
        assert session_id

        await connection.request(
            ACP_METHOD_SESSION_CLOSE, {"sessionId": session_id}, timeout=float(config.timeouts.close)
        )
        resumed = await connection.request(
            ACP_METHOD_SESSION_RESUME,
            {"sessionId": session_id, "cwd": str(workspace), "mcpServers": []},
            timeout=float(config.timeouts.session),
        )
        assert isinstance(resumed, dict)
        await connection.request(
            ACP_METHOD_SESSION_CLOSE, {"sessionId": session_id}, timeout=float(config.timeouts.close)
        )
    finally:
        await connection.close(grace_seconds=10)
        gate.release(lease)


@pytest.mark.asyncio
async def test_live_health_probe_and_capability_match(tmp_path: Path) -> None:
    """Layered no-model health probe + match verdict against the shipped build.

    No model credits: only ``--version`` / ``--dump-config`` probes and the ACP
    handshake (initialize / session/new / close / resume / close). What this
    proves beyond the mock tests: the real probe layers pass on this machine,
    the in-process cache serves the second check without starting a second
    Harness process, and the match verdict agrees with the evidence.
    """
    from agent_core.provider_matching import DEGRADED, MATCHED, REJECTED, evaluate_provider_match

    config = live_config()
    provider = DeepSeekHarnessProvider(
        snapshot_service=WorkspaceSnapshotService(tmp_path / "snapshots", [tmp_path]),
        artifact_dir=tmp_path / "artifacts",
        config=config,
        sandbox_gate=HarnessSandboxGate(config, cache_seconds=0),
        allowed_roots=[tmp_path],
    )
    health = await provider.health()
    assert health["available"] is True, health["reason"]
    assert health["probe"]["ok"] is True, health["probe"]
    assert parse_version(health["probe"]["version"]) is not None
    assert health["probe"]["sandbox_confirmed"] is True
    assert health["handshake"]["ok"] is True, health["handshake"]
    assert health["handshake"]["resume"] is True
    assert health["executable"] is True
    assert health["failure_stage"] == ""
    assert health["acp"] is True
    chain = dict(health["host_chain"])
    assert chain["snapshot_service"] is True

    # 第二次检查命中进程内缓存：不再起新的 Harness 进程。
    cached = await provider.health()
    assert cached["cached"] is True
    assert cached["executable"] is True

    verdict = evaluate_provider_match(
        provider, "code.agent", provider_id="deepseek_harness", health=health
    )
    # 这个 provider 没接验证链：结论必须是 degraded，且逐项检查全部通过、
    # 只差 verification 一项。matched 或 rejected 都说明报告与证据脱节。
    assert verdict.verdict == DEGRADED, verdict.as_dict()
    assert verdict.verdict not in {MATCHED, REJECTED}
    hard = [check for check in verdict.checks if check.name != "verification"]
    assert all(check.passed for check in hard), verdict.as_dict()
    assert not verdict.capabilities.host_verification_chain


@pytest.mark.asyncio
@pytest.mark.skipif(
    not LIVE_PROMPT, reason="set AGENT_HARNESS_LIVE_PROMPT=1 to spend a real model turn"
)
async def test_live_prompt_edits_only_the_snapshot(tmp_path: Path) -> None:
    config = live_config()
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")
    snapshots = WorkspaceSnapshotService(tmp_path / "snapshots", [allowed])
    provider = DeepSeekHarnessProvider(
        snapshot_service=snapshots,
        artifact_dir=tmp_path / "artifacts",
        config=config,
        sandbox_gate=HarnessSandboxGate(config, cache_seconds=0),
        allowed_roots=[allowed],
    )
    request = WorkRequest(
        work_item_id="live-work",
        attempt_id="live-attempt",
        scope=Scope(owner_id="local-user", channel="web", conversation_id="live"),
        objective=(
            "把 app.py 里的 value 从 1 改成 2，只改这一行，不要做别的改动。"
        ),
        task_type="code.agent",
        input={"project_root": str(project)},
        provider_id="deepseek_harness",
    )
    events: list[dict] = []

    result = await asyncio.wait_for(
        provider.execute(request, lambda event: events.append({"type": event.event_type, **dict(event.payload)})),
        timeout=float(config.prompt_timeout) + 120,
    )

    assert result.status in {"completed", "failed", "unknown_result"}
    assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n", (
        "the original project must never be modified by the agent"
    )
    if result.status == "completed":
        assert "value = 2" in result.output["patch"]
    else:
        # A real turn can fail for a deployment reason (the desktop app keeps its
        # model credentials in its own store; a bridge-launched CLI needs
        # DEEPSEEK_API_KEY in the environment). That is allowed — silently
        # passing is not: the failure must carry the agent's own reason.
        assert result.error, "a failed live run must explain itself"
        assert any(
            "no API key" in (item.get("error") or "")
            or "llm-" in (item.get("error") or "")
            or "error" in (item.get("error") or "").lower()
            for item in events
            if item["type"] == "provider_activity"
        ) or result.status == "unknown_result"
    activity = [item for item in events if item["type"] == "provider_activity"]
    assert activity and activity[0]["status"] == "connecting"
    print(
        json.dumps(
            {
                "status": result.status,
                "session": result.output.get("session_id"),
                "error": result.error,
            },
            ensure_ascii=False,
        )
    )
