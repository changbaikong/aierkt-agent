from __future__ import annotations

from pathlib import Path

import pytest

from agent_core.capability_runtime import build_build_context
from agent_core.contracts import Scope
from agent_core.harness_config import HarnessConfig
from agent_core.work_module import (
    attach_code_agent_memory_retriever,
    build_work_module,
    resolve_work_provider,
)
from agent_core.work_provider import WorkProviderCapabilities, WorkRequest, WorkResult
from agent_tools.work import make_work_tools


class HarnessProvider:
    capabilities = WorkProviderCapabilities(
        "deepseek_harness", ("code.agent",), supports_cancel=True, supports_resume=True,
        selectable=True,
    )

    async def execute(self, request, emit):
        return WorkResult("completed", {"kind": "code"})

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("completed", {"resumed": decision})


def submit_task_types(host):
    submit = next(spec for spec in make_work_tools(host) if spec.name == "submit")
    return submit.schema["function"]["parameters"]["properties"]["task_type"]["enum"]


def test_work_module_exposes_code_agent_only_when_enabled(tmp_path: Path) -> None:
    roots = [tmp_path / "projects"]
    roots[0].mkdir()
    _, disabled = build_work_module(
        db_path=tmp_path / "disabled.db",
        allowed_roots=roots,
        code_agent_enabled=False,
    )
    assert "code.agent" not in disabled.provider.capabilities.task_types
    assert "code.agent" not in submit_task_types(disabled)

    _, enabled = build_work_module(
        db_path=tmp_path / "enabled.db",
        allowed_roots=roots,
        code_agent_enabled=True,
        harness_provider=HarnessProvider(),
    )
    assert "code.agent" in enabled.provider.capabilities.task_types
    assert "code.agent" in submit_task_types(enabled)
    assert enabled.provider.provider_for("file.list").capabilities.provider_id == "local-worker"
    assert enabled.provider.provider_for("code.agent").capabilities.provider_id == "deepseek_harness"


@pytest.mark.asyncio
async def test_enabled_but_missing_harness_fails_explicitly(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    _, host = build_work_module(
        db_path=tmp_path / "work.db",
        allowed_roots=[root],
        code_agent_enabled=True,
        harness_config=HarnessConfig(executable=str(tmp_path / "missing-harness.exe")),
        code_snapshot_root=tmp_path / "snapshots",
        code_artifact_dir=tmp_path / "artifacts",
        harness_sandbox_state_root=tmp_path / "harness-state",
    )
    provider = host.provider.provider_for("code.agent")
    request = WorkRequest(
        "w", "a", Scope(owner_id="test"), "Read only", task_type="code.agent",
        input={"project_root": str(root)},
    )

    result = await provider.execute(request, lambda event: None)

    assert result.status == "failed"
    assert "harness" in (result.error or "").lower()
    assert provider.capabilities.provider_id == "deepseek_harness"
    assert host.provider.provider_for("file.list") is not None


def test_code_agent_environment_is_parsed_once_into_build_context(tmp_path: Path) -> None:
    context = build_build_context(
        env={
            "CODE_AGENT_ENABLED": "true",
            "CODE_AGENT_RUNTIME_ROOT": str(tmp_path / "runtime"),
            "CODE_AGENT_PYTHON": str(tmp_path / "runtime/python.exe"),
            "CODE_AGENT_POWERSHELL": str(tmp_path / "runtime/pwsh.exe"),
            "CODE_AGENT_SEARCH": str(tmp_path / "runtime/rg.exe"),
            "CODE_AGENT_WORKERS": "3",
            "CODE_AGENT_VERIFICATION_CONFIG": str(tmp_path / "verification.json"),
            "CODE_AGENT_AUTO_APPLY_LOW_RISK": "true",
            "CODE_AGENT_APPLY_STATE_ROOT": str(tmp_path / "apply-state"),
        }
    )
    assert context.code_agent_enabled is True
    assert context.code_workers == 3
    assert context.code_python_executable.endswith("python.exe")
    assert context.code_verification_config.endswith("verification.json")
    assert context.code_agent_auto_apply_low_risk is True
    assert context.code_apply_state_root.endswith("apply-state")


def test_work_submit_remains_model_only_persistent_action(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    _, host = build_work_module(
        db_path=tmp_path / "work.db",
        allowed_roots=[root],
        code_agent_enabled=True,
        harness_provider=HarnessProvider(),
    )
    submit = next(spec for spec in make_work_tools(host) if spec.name == "submit")
    assert submit.deterministic_route_allowed is False
    assert submit.effect == "persistent"


def test_runtime_helpers_reach_providers_inside_composite(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    harness_provider = HarnessProvider()
    _, host = build_work_module(
        db_path=tmp_path / "work.db",
        allowed_roots=[root],
        code_agent_enabled=True,
        harness_provider=harness_provider,
    )
    retriever = object()

    local = resolve_work_provider(host, "file.list")
    attach_code_agent_memory_retriever(host, retriever)

    assert local.capabilities.provider_id == "local-worker"
    assert harness_provider.memory_retriever is retriever
