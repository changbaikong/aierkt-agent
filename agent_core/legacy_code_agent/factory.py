"""Standalone factory for the archived loop; not imported by the live runtime."""

from __future__ import annotations

import uuid
from pathlib import Path


def build_legacy_code_provider(
    *,
    model_provider: object | None,
    roots: list[Path],
    runtime_root: str | Path,
    powershell_executable: str | Path,
    python_executable: str | Path,
    search_executable: str | Path,
    snapshot_root: str | Path,
    sandbox_state_root: str | Path,
    artifact_dir: str | Path,
    verification_config: str | Path,
    auto_apply_low_risk: bool,
    apply_state_root: str | Path,
) -> object:
    from agent_core.actions import ActionGateway
    from agent_core.legacy_code_agent.code_agent_contracts import CodeAgentBudget
    from agent_core.legacy_code_agent.code_agent_loop import CodeAgentLoop
    from agent_core.legacy_code_agent.code_agent_network import CodeAgentNetworkProxy
    from agent_core.legacy_code_agent.code_agent_provider import CodeAgentProvider
    from agent_core.legacy_code_agent.code_agent_tools import CodeAgentToolContext, make_code_agent_tools
    from agent_core.code_agent_verification import (
        CodeAgentVerificationService,
        VerificationConfig,
    )
    from agent_core.sandbox_contracts import SandboxRuntimeConfig
    from agent_core.tools import DefaultRiskPolicy, ToolRegistry
    from agent_core.windows_sandbox import WindowsSandboxRunner
    from agent_core.workspace_snapshot import WorkspaceSnapshotService
    from agent_core.workspace_apply import (
        AutoApplyPolicy,
        CodeAgentAutoApplyCoordinator,
        WorkspaceApplyService,
    )

    if model_provider is None:
        return _UnavailableLegacyProvider("code agent model provider is unavailable")
    required = [runtime_root, powershell_executable, python_executable]
    if any(not str(item).strip() for item in required):
        return _UnavailableLegacyProvider("code agent managed runtime is not configured")
    try:
        runtime_config = SandboxRuntimeConfig(
            runtime_root=Path(runtime_root),
            powershell_executable=Path(powershell_executable),
            python_executable=Path(python_executable),
            search_executable=Path(search_executable) if str(search_executable).strip() else None,
        )
        sandbox_runner = WindowsSandboxRunner(
            runtime_config,
            state_root=Path(sandbox_state_root),
        )
        snapshots = WorkspaceSnapshotService(Path(snapshot_root), roots)
        verification = (
            VerificationConfig.load(Path(verification_config), forbidden_roots=roots)
            if str(verification_config).strip()
            else VerificationConfig(version=1, projects={})
        )
    except Exception as exc:
        return _UnavailableLegacyProvider(
            f"code agent isolation is unavailable: {type(exc).__name__}: {exc}"
        )

    class _UnavailableSearch:
        def search(self, query: str, *, max_results: int, freshness: str = "noLimit") -> list[object]:
            raise RuntimeError("code agent web search backend is unavailable")

    try:
        from agent_tools.web_search import BoChaWebSearchProvider

        search_backend = BoChaWebSearchProvider.from_env() or _UnavailableSearch()
    except Exception:
        search_backend = _UnavailableSearch()
    network_proxy = CodeAgentNetworkProxy(search_backend)
    verification_service = CodeAgentVerificationService(
        config=verification,
        sandbox_runner=sandbox_runner,
        python_executable=runtime_config.python_executable,
        powershell_executable=runtime_config.powershell_executable,
    )
    apply_service = WorkspaceApplyService(Path(apply_state_root), forbidden_roots=roots)
    startup_recovery = apply_service.recover_pending()
    auto_apply = CodeAgentAutoApplyCoordinator(
        enabled=auto_apply_low_risk,
        snapshot_service=snapshots,
        sandbox_runner=sandbox_runner,
        verification_service=verification_service,
        apply_service=apply_service,
        policy=AutoApplyPolicy(),
    )
    holder: dict[str, object] = {}

    def loop_factory(request: object, snapshot: object) -> CodeAgentLoop:
        provider = holder["provider"]
        context = CodeAgentToolContext(
            attempt_id=request.attempt_id,
            snapshot=snapshot,
            sandbox_runner=sandbox_runner,
            python_executable=runtime_config.python_executable,
            powershell_executable=runtime_config.powershell_executable,
            search_executable=runtime_config.search_executable,
            submit_scope=request.scope,
            memory_retriever=getattr(provider, "memory_retriever", None),
            network_proxy=network_proxy,
        )
        registry = ToolRegistry()
        specs = make_code_agent_tools(context)
        for tool in specs:
            registry.register(tool)
        gateway = ActionGateway.standalone(
            registry,
            risk_policy=DefaultRiskPolicy(),
            approval_sink=lambda action, spec: uuid.uuid4().hex,
        )
        return CodeAgentLoop(model_provider, gateway, specs, CodeAgentBudget())

    provider = CodeAgentProvider(
        snapshot_service=snapshots,
        loop_factory=loop_factory,
        sandbox_runner=sandbox_runner,
        artifact_dir=artifact_dir,
        verification_service=verification_service,
        auto_apply_coordinator=auto_apply,
    )
    provider.memory_retriever = None
    provider.startup_recovery = startup_recovery
    holder["provider"] = provider
    return provider


class _UnavailableLegacyProvider:
    from agent_core.work_provider import WorkProviderCapabilities

    capabilities = WorkProviderCapabilities(
        "code-agent-unavailable", ("code.agent",), supports_cancel=False, supports_resume=False
    )

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def execute(self, request: object, emit: object) -> object:
        from agent_core.work_provider import WorkResult

        return WorkResult("failed", error=self.reason)

    async def cancel(self, attempt_id: str) -> bool:
        return False

    async def resume(self, request: object, checkpoint: object, decision: str, emit: object) -> object:
        from agent_core.work_provider import WorkResult

        return WorkResult("failed", error=self.reason)
