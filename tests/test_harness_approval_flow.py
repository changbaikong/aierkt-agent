"""End-to-end approval flow for a Harness task, at the Host level.

This is the test that ties the two halves together: a real ACP session (mock
agent subprocess) that edits the snapshot, the shared verification/apply
lifecycle deciding the change needs approval, and the existing Work Host
approval chain resuming the write-back afterwards.

It is the "最终补丁仍必须经过现有审批链" requirement, executable.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_core.code_agent_verification import VerificationRecord, snapshot_fingerprint
from agent_core.composite_work_provider import CompositeWorkProvider
from agent_core.contracts import Scope
from agent_core.deepseek_harness_provider import DeepSeekHarnessProvider
from agent_core.harness_config import HarnessConfig, HarnessTimeouts, VersionRequirement
from agent_core.harness_sandbox import CliResult, HarnessSandboxGate
from agent_core.work_host import WorkHost
from agent_core.workspace_snapshot import WorkspaceSnapshotService

MOCK_AGENT = Path(__file__).parent / "support" / "mock_acp_agent.py"

DUMP_CONFIG = """- id: sandbox-policy
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    workspaceRoot: !!js process.cwd()
- id: pwsh-sandbox
  name: '@deepseek-ai/dsh-pwsh-sandbox'
- id: approval
  config:
    policy: !!js process.env.DSH_PERMISSION_MODE === 'danger-full-access' ? 'never' : 'ask'
"""


async def fake_cli(argv, *, cwd, env, timeout):  # noqa: ANN001
    if "--version" in argv:
        return CliResult(0, "0.2.0-rc.2\n")
    return CliResult(0, DUMP_CONFIG)


class Verifier:
    async def verify(self, attempt_id, snapshot):  # noqa: ANN001
        return VerificationRecord(
            status="verified",
            reason="tests passed",
            project_root=str(snapshot.project_root),
            before_fingerprint="a" * 64,
            after_fingerprint="a" * 64,
            commands=({"argv": ["python", "-m", "pytest"], "exit_code": 0},),
        )


class ApprovalRequiredApply:
    """Stands in for the auto-apply coordinator: asks first, writes back after.

    The write-back is mimicked for the one file this test changes; the real
    coordinator does the same thing through ``WorkspaceApplyService``. Keeping it
    here means the test can assert the file really changed only after approval.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def process(self, attempt_id, snapshot, verification, **kwargs):  # noqa: ANN001
        self.calls.append({"attempt_id": attempt_id, **kwargs})
        if not kwargs.get("approval_granted"):
            fingerprint = snapshot_fingerprint(snapshot)
            return {
                "status": "approval_required",
                "reason": "protected_files",
                "risk": {
                    "content_fingerprint": fingerprint,
                    "reasons": ["protected_files"],
                    "files": 1,
                    "changed_lines": 1,
                    "content_bytes": 12,
                },
            }
        for changed in snapshot.diff().modified:
            source = snapshot.workspace_root / changed
            target = snapshot.project_root / changed
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        return {"status": "applied_verified", "recovery_package": "recovery-1"}


@pytest.mark.asyncio
async def test_harness_patch_waits_for_approval_then_applies(tmp_path: Path) -> None:
    allowed = tmp_path / "projects"
    project = allowed / "demo"
    project.mkdir(parents=True)
    (project / "app.py").write_text("value = 1\n", encoding="utf-8")

    report = tmp_path / "report.jsonl"
    config = HarnessConfig(
        executable=sys.executable,
        entry_args=(str(MOCK_AGENT),),
        profile="acp",
        env={
            "MOCK_ACP_SCENARIO": json.dumps(
                {
                    "session_id": "sess-approval",
                    "updates": [
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "改好了"},
                        }
                    ],
                    "write_files": {"app.py": "value = 2\n"},
                    "stop_reason": "end_turn",
                }
            ),
            "MOCK_ACP_REPORT": str(report),
        },
        required_version=VersionRequirement(minimum="0.0.1"),
        timeouts=HarnessTimeouts(preflight=10, initialize=15, session=15, prompt=8, cancel=2, close=5),
        prompt_timeout=8,
        sandbox_state_root=str(tmp_path / "sandbox-state"),
    )
    apply_service = ApprovalRequiredApply()
    harness = DeepSeekHarnessProvider(
        snapshot_service=WorkspaceSnapshotService(tmp_path / "snapshots", [allowed]),
        artifact_dir=tmp_path / "artifacts",
        config=config,
        sandbox_gate=HarnessSandboxGate(config, runner=fake_cli),
        verification_service=Verifier(),
        auto_apply_coordinator=apply_service,
        allowed_roots=[allowed],
    )
    host = WorkHost(
        tmp_path / "work.db",
        provider=CompositeWorkProvider([harness]),
        max_workers=1,
        code_workers=1,
    )
    scope = Scope(owner_id="alice", channel="web", conversation_id="chat", module_id="work")
    await host.start()
    try:
        submitted = await host.submit(
            scope,
            "把 value 改成 2",
            task_type="code.agent",
            input={"project_root": str(project), "provider": "deepseek_harness"},
        )
        work_item_id = submitted["work_item_id"]
        await _wait_for(lambda: host.get(work_item_id, scope)["status"] == "waiting_approval")

        detail = host.get(work_item_id, scope)
        approval = next(item for item in detail["approvals"] if item["status"] == "pending")
        assert approval["payload"]["tool"] == "code.apply"
        # A paused task must show 「等待授权」, not 「思考中」.
        assert detail["provider_activity"]["status"] == "waiting_permission"
        assert detail["provider_activity"]["waiting_reason"]
        # Nothing may be written back before the human decision.
        assert (project / "app.py").read_text(encoding="utf-8") == "value = 1\n"

        await host.decide_approval(approval["approval_id"], scope, "approved")
        await _wait_for(lambda: host.get(work_item_id, scope)["status"] == "completed")

        assert (project / "app.py").read_text(encoding="utf-8") == "value = 2\n"
        assert [call.get("approval_granted") for call in apply_service.calls] == [None, True]
        final = host.get(work_item_id, scope)
        assert final["provider_activity"]["status"] == "completed"
        assert final["attempts"][0]["provider_id"] == "deepseek_harness"
        assert any(event["event_type"] == "waiting_approval" for event in final["events"])
    finally:
        await host.stop()


async def _wait_for(predicate, *, timeout: float = 20.0) -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition never became true")
