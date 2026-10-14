"""Work module builder: exposes the Host to the main chat.

The main chat gets the whole chat-side surface from **one** place
(:func:`agent_tools.work.make_work_tools`), and that surface is exactly four
tools: ``work__submit`` / ``work__plan_submit`` / ``work__status`` /
``work__cancel``. Before 2026-09-14 the single-task controls were registered
privately by ``web_api.py``, so only the Web entry had them; now the module is
the single registration source and both entries get the same set.

What the chat never gets is *execution control*: plans are validated, persisted
and scheduled by the same ``WorkHost`` the Web page talks to. Each plan step
becomes an ordinary ``WorkItem``; Harness owns code tasks and the local worker
owns ordinary file and system tasks.

**Approving a waiting task is not a chat tool at all.** It used to be
(``work__approve``, risk=high), but that let the model vote on a high-risk action
on the user's behalf — and since ``work__submit`` is hard-routable (no
confirmation gate of its own), the two gates cancelled each other out. Approval
now happens only through the unified control plane
(``agent_core/approval_coordinator.py`` + ``/api/approvals``), where a human
click is the only input.

Since 2026-09-20 the Host also carries a ``permission_lookup``. That is the
**second** gate: account-level "完全访问" makes the Host stop injecting
``_requires_approval`` for screenshot / volume / file-organize. The lookup is
queried per execution, never cached per conversation, so switching the mode
takes effect on the very next task without a restart.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from agent_core.modules import ModuleSpec
from agent_core.contracts import ToolSpec
from agent_core.permission_mode import PermissionLayer
from agent_core.work_host import WorkHost

logger = logging.getLogger(__name__)


def resolve_work_roots(raw: str | None = None) -> list[Path]:
    value = raw if raw is not None else os.getenv("AGENT_WORK_ROOTS", str(Path.cwd()))
    return [Path(item).expanduser().resolve() for item in str(value).split(";") if item.strip()]


def build_work_module(
    *,
    db_path: str | Path | None = None,
    allowed_roots: list[str | Path] | None = None,
    max_workers: int = 2,
    code_workers: int = 1,
    permission_layer: PermissionLayer | None = None,
    core_db_path: str | Path | None = None,
    code_agent_enabled: bool = True,
    code_runtime_root: str | Path = "",
    code_powershell_executable: str | Path = "",
    code_python_executable: str | Path = "",
    code_search_executable: str | Path = "",
    code_snapshot_root: str | Path = "./data/code_snapshots",
    code_sandbox_state_root: str | Path = "./data/code_sandbox_state",
    code_artifact_dir: str | Path = "./data/code_artifacts",
    code_verification_config: str | Path = "",
    code_agent_auto_apply_low_risk: bool = False,
    code_apply_state_root: str | Path = "./data/code_apply_state",
    harness_enabled: bool = True,
    harness_provider: object | None = None,
    harness_config: object | None = None,
    harness_sandbox_state_root: str | Path = "./data/harness_sandbox_state",
) -> tuple[ModuleSpec, WorkHost]:
    """Build Work with Harness as its only code executor, never a legacy fallback."""
    from agent_core.local_worker_provider import LocalWorkerProvider
    from agent_tools.work import make_work_tools

    roots = [Path(item).expanduser().resolve() for item in allowed_roots] if allowed_roots else resolve_work_roots()
    layer = permission_layer or PermissionLayer(db_path=core_db_path or os.getenv("AGENT_CORE_DB", "agent_core.db"))
    local_provider = LocalWorkerProvider(allowed_roots=roots)
    effective_provider: object = local_provider
    if code_agent_enabled:
        if not harness_enabled:
            effective_code_provider = _UnavailableHarnessProvider(
                "DeepSeek Harness is disabled by AGENT_HARNESS_ENABLED; no fallback executor"
            )
        else:
            try:
                effective_code_provider = harness_provider or _build_harness_provider(
                    roots=roots,
                    runtime_root=code_runtime_root,
                    powershell_executable=code_powershell_executable,
                    python_executable=code_python_executable,
                    search_executable=code_search_executable,
                    snapshot_root=code_snapshot_root,
                    sandbox_state_root=code_sandbox_state_root,
                    artifact_dir=code_artifact_dir,
                    verification_config=code_verification_config,
                    auto_apply_low_risk=code_agent_auto_apply_low_risk,
                    apply_state_root=code_apply_state_root,
                    harness_config=harness_config,
                    harness_sandbox_state_root=harness_sandbox_state_root,
                )
            except Exception as exc:
                effective_code_provider = _UnavailableHarnessProvider(
                    f"DeepSeek Harness setup failed: {type(exc).__name__}: {exc}"
                )
        from agent_core.composite_work_provider import CompositeWorkProvider

        effective_provider = CompositeWorkProvider([local_provider, effective_code_provider])
    host = WorkHost(
        db_path or os.getenv("AGENT_WORK_DB", "agent_work.db"),
        provider=effective_provider,
        max_workers=max_workers,
        code_workers=code_workers,
        permission_lookup=layer.controller,
    )
    # Host 是清理链路的第二段，建好之后回填给权限层。
    layer.attach(work_host=host)

    def build_tools() -> list[ToolSpec]:
        return make_work_tools(host)

    spec = ModuleSpec(
        "work",
        build_tools,
        memory_policy="private_state",
        description="后台工作计划编排",
        start=host.start,
        stop=host.stop,
        runtime=host,
        # 声明**从真实产出提取**，不再维护手写名单。
        #
        # `declared_tools` 的用途是"工厂挂了也能报出本该有哪些工具"。手写一份
        # 名单能满足这个用途，但代价是它必须与真实产出手动保持同步 ——
        # 而没人会记得（旧静态清单漏 `weather` 就是这么炸的）。
        # 从产出提取之后，"声明与实现不一致"这件事在**构造那一刻**就不可能发生；
        # 剩下要守的只有"产出本身对不对"，那由能力目录的对账用例管。
        declared_tools=tuple(item.name for item in build_tools()),
    )
    return spec, host


def resolve_work_provider(host: object, task_type: str) -> object | None:
    """Return the concrete provider for a task type, composite or not."""
    provider = getattr(host, "provider", None)
    resolver = getattr(provider, "provider_for", None)
    if callable(resolver):
        return resolver(task_type)
    capabilities = getattr(provider, "capabilities", None)
    task_types = tuple(getattr(capabilities, "task_types", ()) or ())
    return provider if task_type in task_types else None


def attach_code_agent_memory_retriever(host: object, retriever: object) -> None:
    """Attach Host conversation context to the active Harness provider when enabled."""
    provider = resolve_work_provider(host, "code.agent")
    if provider is not None:
        setattr(provider, "memory_retriever", retriever)


def _build_harness_provider(
    *,
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
    harness_config: object | None,
    harness_sandbox_state_root: str | Path,
) -> object:
    """Build the DeepSeek Harness provider with the same host-side machinery.

    Two deliberate differences from the in-process code agent:

    * the Harness brings its own model route and its own tool loop, so a missing
      Host model provider does **not** disable it;
    * host verification and auto-apply are best-effort here. They need the
      managed runtime; without it the task still runs and reports an unverified
      patch, which is honest and keeps the approval chain intact.
    """
    from agent_core.deepseek_harness_provider import DeepSeekHarnessProvider
    from agent_core.harness_config import HarnessConfig
    from agent_core.harness_sandbox import HarnessSandboxGate
    from agent_core.workspace_snapshot import WorkspaceSnapshotService

    config = harness_config if isinstance(harness_config, HarnessConfig) else HarnessConfig.from_env()
    if str(harness_sandbox_state_root).strip():
        from dataclasses import replace as _replace

        config = _replace(config, sandbox_state_root=str(harness_sandbox_state_root))
    snapshots = WorkspaceSnapshotService(Path(snapshot_root), roots)

    verification_service = None
    auto_apply_coordinator = None
    try:
        from agent_core.code_agent_verification import (
            CodeAgentVerificationService,
            VerificationConfig,
        )
        from agent_core.sandbox_contracts import SandboxRuntimeConfig
        from agent_core.windows_sandbox import WindowsSandboxRunner
        from agent_core.workspace_apply import (
            AutoApplyPolicy,
            CodeAgentAutoApplyCoordinator,
            WorkspaceApplyService,
        )

        if all(str(item).strip() for item in (runtime_root, powershell_executable, python_executable)):
            runtime_config = SandboxRuntimeConfig(
                runtime_root=Path(runtime_root),
                powershell_executable=Path(powershell_executable),
                python_executable=Path(python_executable),
                search_executable=Path(search_executable) if str(search_executable).strip() else None,
            )
            sandbox_runner = WindowsSandboxRunner(
                runtime_config, state_root=Path(sandbox_state_root)
            )
            verification = (
                VerificationConfig.load(Path(verification_config), forbidden_roots=roots)
                if str(verification_config).strip()
                else VerificationConfig(version=1, projects={})
            )
            verification_service = CodeAgentVerificationService(
                config=verification,
                sandbox_runner=sandbox_runner,
                python_executable=runtime_config.python_executable,
                powershell_executable=runtime_config.powershell_executable,
            )
            apply_service = WorkspaceApplyService(Path(apply_state_root), forbidden_roots=roots)
            apply_service.recover_pending()
            auto_apply_coordinator = CodeAgentAutoApplyCoordinator(
                enabled=auto_apply_low_risk,
                snapshot_service=snapshots,
                sandbox_runner=sandbox_runner,
                verification_service=verification_service,
                apply_service=apply_service,
                policy=AutoApplyPolicy(),
            )
    except Exception:
        # Host verification is a bonus for this provider, never a prerequisite:
        # swallowing a *setup* failure here cannot hide an execution failure,
        # because the task still goes through the shared lifecycle and reports
        # "unverified" when no verifier exists. It must still be *visible* in the
        # log: "unverified" alone cannot tell you whether the verifier is missing
        # or the project simply has no test commands.
        logger.warning(
            "harness host verification is unavailable; patches will be reported as unverified",
            exc_info=True,
        )
        verification_service = None
        auto_apply_coordinator = None

    return DeepSeekHarnessProvider(
        snapshot_service=snapshots,
        artifact_dir=artifact_dir,
        config=config,
        sandbox_gate=HarnessSandboxGate(config),
        verification_service=verification_service,
        auto_apply_coordinator=auto_apply_coordinator,
        allowed_roots=roots,
    )


class _UnavailableHarnessProvider:
    from agent_core.work_provider import WorkProviderCapabilities

    capabilities = WorkProviderCapabilities(
        "deepseek_harness", ("code.agent",), supports_cancel=False, supports_resume=False,
        selectable=True,
    )

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def health(self) -> dict[str, object]:
        return {
            "provider_id": "deepseek_harness",
            "available": False,
            "probe": {"ok": False, "version": "", "sandbox_confirmed": False,
                      "workspace_root": "", "evidence": {}, "detail": self.reason},
            "handshake": {"ok": False, "stage": "", "initialize": False, "session_new": False,
                          "close": False, "resume": False, "agent": "", "agent_version": "",
                          "protocol_version": None, "session_capabilities": [], "detail": self.reason},
            "executable": False,
            "failure_stage": "config",
            "reason": self.reason,
        }

    async def execute(self, request: object, emit: object) -> object:
        from agent_core.work_provider import WorkResult

        return WorkResult("failed", error=self.reason)

    async def cancel(self, attempt_id: str) -> bool:
        return False

    async def resume(self, request: object, checkpoint: object, decision: str, emit: object) -> object:
        from agent_core.work_provider import WorkResult

        return WorkResult("failed", error=self.reason)
