"""``DeepSeekHarnessProvider``: run ``code.agent`` tasks on the installed Harness.

Division of labour (fixed by the design, not by preference):

* the **Host** owns task identity, persistence, the snapshot, verification, the
  patch and the approval chain;
* **WorkHost** owns queueing, the ledger, cancel, restart recovery and writing
  results back;
* the **Harness** owns model inference and its own internal tool loop, inside
  the sandbox it was started with.

This provider is the bridge in the middle. It never lets the Harness touch the
real project (it only ever sees the snapshot directory), never runs without a
confirmed sandbox preflight, and never downgrades to a plain host process: if
any gate fails the attempt ends as ``failed`` before a process exists.

Two failure semantics are worth spelling out because they are *not* the same as
"failed":

* **``unknown_result``** — the agent stopped in a way we could not confirm
  (process died mid-turn, cancel without confirmation, resume refused). The task
  may have half-applied something, so it is never retried automatically.
* **task-complete-but-unverified** — the Harness finished cleanly and the Host
  could not verify the result. The work still completes, with
  ``confidence = assumed`` and the verification status in the output.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .code_task_contracts import CodeAgentOutcome
from .code_task_lifecycle import CodeTaskLifecycle
from .harness_acp import (
    ACP_METHOD_FS_READ_TEXT_FILE,
    ACP_METHOD_FS_WRITE_TEXT_FILE,
    ACP_METHOD_INITIALIZE,
    ACP_METHOD_REQUEST_PERMISSION,
    ACP_METHOD_SESSION_CANCEL,
    ACP_METHOD_SESSION_CLOSE,
    ACP_METHOD_SESSION_NEW,
    ACP_METHOD_SESSION_PROMPT,
    ACP_METHOD_SESSION_RESUME,
    ACP_METHOD_SESSION_UPDATE,
    ACP_PROTOCOL_VERSION,
    AcpConnection,
    AcpDisconnected,
    AcpError,
    AcpHandshake,
    AcpLaunchSpec,
    AcpProtocolError,
    AcpSpawnError,
    AcpTimeout,
)
from .harness_config import HarnessConfig
from .harness_sandbox import HarnessSandboxGate, SandboxLease, SandboxUnavailable, harness_argv
from .provider_activity import ProviderActivity
from .work_provider import (
    TaskTypeCapabilities,
    WorkProviderCapabilities,
    WorkRequest,
    WorkResult,
)
from .workspace_snapshot import WorkspaceSnapshot

#: ``session/resume`` per the installed build; ``session/load`` is the older
#: spec name and is kept as a documented fallback for other builds.
RESUME_METHODS = (ACP_METHOD_SESSION_RESUME, "session/load")

#: Attempt checkpoint kinds this provider owns.
CHECKPOINT_HARNESS_SESSION = "harness_session"
CHECKPOINT_CODE_APPLY = "code_apply"

#: Markers that turn a permission request into a refusal regardless of paths:
#: network and machine-level operations are outside a task snapshot's scope.
OUT_OF_SCOPE_MARKERS = (
    "http://",
    "https://",
    "ws://",
    "wss://",
    "invoke-webrequest",
    "invoke-restmethod",
    "curl ",
    "wget ",
    "iwr ",
    "netsh ",
    "reg add",
    "reg delete",
    "schtasks",
    "sc.exe",
    "sc config",
    "net user",
    "net localgroup",
    "runas",
    "set-mppreference",
    "new-localuser",
)

_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\)[^\s\"'<>|,;)]*")
_URL = re.compile(r"\b(?:https?|wss?|ftp)://[^\s\"'<>|]+", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class _HarnessRun:
    """Mutable state of one attempt while its ACP session is alive."""

    attempt_id: str
    request: WorkRequest
    objective: str
    snapshot: WorkspaceSnapshot
    lease: SandboxLease
    activity: ProviderActivity
    emit: Any = None
    connection: AcpConnection | None = None
    session_id: str = ""
    handshake: AcpHandshake | None = None
    message_parts: list[str] = field(default_factory=list)
    tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    permission_events: list[dict[str, Any]] = field(default_factory=list)
    prompt_future: asyncio.Future[Any] | None = None
    stop_reason: str = ""
    disconnect: AcpDisconnected | None = None
    disconnect_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_requested: bool = False
    cancel_confirmed: bool = False
    cancel_status: str | None = None
    scope_unverified: bool = False
    resumed: bool = False
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def message_text(self) -> str:
        return "".join(self.message_parts).strip()


class DeepSeekHarnessProvider:
    """Work provider that drives the installed Harness over ACP."""

    capabilities = WorkProviderCapabilities(
        provider_id="deepseek_harness",
        task_types=("code.agent",),
        supports_cancel=True,
        supports_resume=True,
        # The sole live code executor. The id remains available for explicit
        # callers; requests without an id resolve to this provider as well.
        selectable=True,
    )

    def __init__(
        self,
        *,
        snapshot_service: Any,
        artifact_dir: str | Path,
        config: HarnessConfig | None = None,
        sandbox_gate: HarnessSandboxGate | None = None,
        connection_factory: Callable[..., Any] | None = None,
        verification_service: Any = None,
        auto_apply_coordinator: Any = None,
        artifact_ttl_hours: int = 24,
        allowed_roots: Sequence[str | Path] = (),
        health_cache_seconds: float = 300.0,
    ) -> None:
        self.config = config or HarnessConfig.from_env()
        self.sandbox_gate = sandbox_gate or HarnessSandboxGate(self.config)
        self._connection_factory = connection_factory
        self.allowed_roots = tuple(str(item) for item in allowed_roots)
        self.lifecycle = CodeTaskLifecycle(
            snapshot_service=snapshot_service,
            artifact_dir=artifact_dir,
            artifact_ttl_hours=artifact_ttl_hours,
            verification_service=verification_service,
            auto_apply_coordinator=auto_apply_coordinator,
        )
        self._runs: dict[str, _HarnessRun] = {}
        #: Cancel outcomes outlive the run: ``WorkHost`` asks "did that cancel
        #: actually stop it?" *after* the attempt finished, and a run that has
        #: already been torn down must still be able to answer.
        self._cancel_outcomes: dict[str, str] = {}
        #: 健康报告的进程内缓存。无模型探测会真的起一个 Harness 进程做 ACP
        #: 握手，网页每次刷新都来一遍既慢又没意义；300 秒内的重复检查直接复用。
        self._health_cache_seconds = max(0.0, float(health_cache_seconds))
        self._health_cache: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ #
    # Capability / health surface
    # ------------------------------------------------------------------ #

    def task_capabilities(self, task_type: str) -> TaskTypeCapabilities:
        """按 task_type 的能力报告，Host 匹配检查从这里取事实。

        每一栏都能指到实现：取消确认是 ``cancel`` 等 prompt 结束或标 unknown；
        恢复检查点是 ``recovery_checkpoint`` 要求 session+snapshot 双凭据；
        事件流逐工具/逐阶段发 ``provider_activity``；快照由生命周期强制
        （先建快照、只写快照）；沙箱是逐 attempt 的 fail-closed 预检。
        唯一"看配置"的是验证链：验证器或回写协调器没建起来就如实报 False。
        """
        if task_type != "code.agent":
            return TaskTypeCapabilities(
                task_type=task_type,
                can_execute=False,
                notes=("DeepSeek Harness 只执行 code.agent 任务",),
            )
        chain = (
            self.lifecycle.verification_service is not None
            and self.lifecycle.auto_apply_coordinator is not None
        )
        return TaskTypeCapabilities(
            task_type="code.agent",
            can_execute=True,
            supports_cancel=True,
            cancel_confirmable=True,
            supports_resume=True,
            resume_has_checkpoint=True,
            emits_events=True,
            accepts_snapshot=True,
            verifiable_result=True,
            host_verification_chain=chain,
            sandbox_confirmed=True,
            notes=() if chain else ("Host 验证链未就位：只能生成补丁，不能自动应用",),
        )

    async def health(self, *, force: bool = False) -> dict[str, Any]:
        """分层健康检查，日常检查**不调用模型**。

        四层状态，层层递进，`failure_stage` 指出卡在哪一层：

        * ``probe`` —— 配置校验 + 临时目录里的沙箱预检（真实 CLI 的
          ``--version`` / ``--dump-config``，确认沙箱模式、工作目录绑定、
          审批策略和 Windows 沙箱组件）；
        * ``handshake`` —— 起真实 ACP 进程，依次 ``initialize`` →
          ``session/new`` → ``session/close`` → ``session/resume`` →
          ``session/close``；**不含** ``session/prompt``，所以不消耗模型额度；
        * ``available`` —— 旧语义保留：配置与沙箱可用（不含握手）；
        * ``executable`` —— 配置、沙箱、ACP 无模型探测全部通过，即
          "现在提交任务至少能起得来"（模型路由等运行期问题不在其中）。

        结果按 ``health_cache_seconds``（默认 300 秒）进程内缓存，避免网页
        刷新反复起 Harness 进程；``force=True`` 跳过缓存。
        """
        now = time.monotonic()
        if (
            not force
            and self._health_cache is not None
            and self._health_cache_seconds > 0
            and now - self._health_cache[0] <= self._health_cache_seconds
        ):
            return {**self._health_cache[1], "cached": True}
        report = await self._health_report()
        if self._health_cache_seconds > 0:
            self._health_cache = (time.monotonic(), report)
        return report

    async def _health_report(self) -> dict[str, Any]:
        chain = self._host_chain_report()
        report: dict[str, Any] = {
            "provider_id": self.capabilities.provider_id,
            # 旧语义：配置与沙箱可用（不含 ACP 握手）。老调用方继续用它。
            "available": False,
            # 新增四层状态。
            "probe": {"ok": False, "version": "", "sandbox_confirmed": False,
                      "workspace_root": "", "evidence": {}, "detail": ""},
            "handshake": {"ok": False, "stage": "", "initialize": False, "session_new": False,
                          "close": False, "resume": False, "agent": "", "agent_version": "",
                          "protocol_version": None, "session_capabilities": [], "detail": ""},
            "executable": False,
            "failure_stage": "",
            "reason": "",
            "executable_path": str(self.config.executable),
            "version": "",
            "required_version": self.config.required_version.describe(),
            "sandbox": self.config.sandbox_mode_value,
            "acp": False,
            "session_capabilities": list(self.config.required_session_capabilities),
            "host_chain": chain,
            "warnings": [],
        }
        if not chain["verifier"]:
            report["warnings"].append(
                "Host 验证器不可用：任务可以运行，但只会生成补丁，不会自动写回原项目"
            )
        elif not chain["auto_apply"]:
            report["warnings"].append("回写协调器不可用：验证结果只能落成待审批的补丁")

        problems = self.config.validate()
        if problems:
            report["failure_stage"] = "config"
            report["reason"] = "; ".join(problems)
            return report

        workspace, scope_roots = self._health_workspace()
        lease: SandboxLease | None = None
        try:
            try:
                lease = await self.sandbox_gate.acquire(
                    attempt_id=f"health-{uuid.uuid4().hex[:12]}",
                    workspace_root=workspace,
                    allowed_roots=scope_roots,
                )
            except SandboxUnavailable as exc:
                report["failure_stage"] = _probe_stage(exc.reason)
                report["reason"] = f"{exc.reason}: {exc.detail}"
                report["probe"]["detail"] = f"{exc.reason}: {exc.detail}"
                return report
            except Exception as exc:  # 预检本身的意外错误也算探测失败
                report["failure_stage"] = "probe"
                report["reason"] = f"{type(exc).__name__}: {exc}"
                report["probe"]["detail"] = report["reason"]
                return report

            evidence = dict(lease.evidence)
            report["version"] = str(evidence.get("version") or "")
            report["probe"] = {
                "ok": True,
                "version": report["version"],
                "sandbox_confirmed": True,
                "workspace_root": lease.workspace_root,
                "evidence": {
                    "mode": evidence.get("mode"),
                    "workspace_expression": evidence.get("workspace_expression"),
                    "approval_expression": evidence.get("approval_expression"),
                    "rows": evidence.get("rows"),
                },
                "detail": "",
            }
            report["acp"] = True
            report["available"] = True

            handshake = await self._health_handshake(lease)
            report["handshake"] = handshake
            if handshake.get("ok"):
                report["executable"] = True
                report["session_capabilities"] = list(handshake.get("session_capabilities") or [])
            else:
                report["failure_stage"] = str(handshake.get("stage") or "handshake")
                report["reason"] = str(handshake.get("detail") or "ACP 握手失败")
            return report
        finally:
            if lease is not None:
                self.sandbox_gate.release(lease)
            shutil.rmtree(workspace, ignore_errors=True)

    def _health_workspace(self) -> tuple[Path, list[str]]:
        """给健康预检一个一次性工作目录，优先放在快照根目录下。

        放在快照根下面不是随意的：真实任务的预检要求工作目录落在
        allowed_roots ∪ 快照根之内，健康检查用**同一条规则**，预检通过的
        才是任务真的会走的那条路。
        """
        snapshot_root = getattr(self.lifecycle.snapshot_service, "snapshot_root", None)
        if snapshot_root is not None:
            root = Path(snapshot_root)
            root.mkdir(parents=True, exist_ok=True)
            workspace = Path(tempfile.mkdtemp(prefix="harness-health-", dir=root))
            return workspace, [*self.allowed_roots, str(root)]
        workspace = Path(tempfile.mkdtemp(prefix="harness-health-"))
        return workspace, []

    async def _health_handshake(self, lease: SandboxLease) -> dict[str, Any]:
        """无模型 ACP 握手：把"会话链路能走通"一次验完。

        步骤与真实任务完全同源（同一个启动参数、同一个连接工厂、同一个
        能力门），只去掉 prompt —— 握手失败而任务还能跑通的情形，就是要
        在这里被拦下来的那类误报。
        """
        info: dict[str, Any] = {
            "ok": False,
            "stage": "",
            "initialize": False,
            "session_new": False,
            "close": False,
            "resume": False,
            "agent": "",
            "agent_version": "",
            "protocol_version": None,
            "session_capabilities": [],
            "detail": "",
        }

        def fail(stage: str, detail: str) -> dict[str, Any]:
            info["stage"] = stage
            info["detail"] = detail
            return info

        spec = self._launch_spec_for(lease)
        if not Path(spec.executable).is_file():
            return fail("handshake.spawn", f"Harness 可执行文件不存在：{spec.executable}")
        factory = self._connection_factory or self._default_connection
        try:
            connection = await factory(
                spec,
                on_notification=lambda method, params: None,
                on_request=lambda method, params: None,
                on_disconnect=lambda error: None,
            )
        except AcpSpawnError as exc:
            return fail("handshake.spawn", str(exc))
        except SandboxUnavailable as exc:
            return fail("handshake.spawn", f"{exc.reason}: {exc.detail}")
        try:
            try:
                result = await connection.request(
                    ACP_METHOD_INITIALIZE,
                    {
                        "protocolVersion": int(self.config.protocol_version or ACP_PROTOCOL_VERSION),
                        "clientCapabilities": {},
                    },
                    timeout=float(self.config.timeouts.initialize),
                )
                handshake = AcpHandshake.from_result(result)
                evidence = self.sandbox_gate.confirm_capabilities(handshake)
                info["initialize"] = True
                info["agent"] = handshake.name_text
                info["agent_version"] = handshake.version_text
                info["protocol_version"] = handshake.protocol_version
                info["session_capabilities"] = list(evidence.get("session_capabilities") or [])
            except SandboxUnavailable as exc:  # 能力门拒绝（协议版本/会话能力不符）
                return fail("handshake.initialize", f"{exc.reason}: {exc.detail}")
            except (AcpDisconnected, AcpTimeout, AcpProtocolError) as exc:
                return fail("handshake.initialize", str(exc))

            session_id = ""
            try:
                created = await connection.request(
                    ACP_METHOD_SESSION_NEW,
                    {"cwd": lease.workspace_root, "mcpServers": []},
                    timeout=float(self.config.timeouts.session),
                )
                session_id = str((created or {}).get("sessionId") or "").strip()
                if not session_id:
                    return fail("handshake.session_new", "Harness 未返回 session id")
                info["session_new"] = True
            except (AcpDisconnected, AcpTimeout, AcpProtocolError) as exc:
                return fail("handshake.session_new", str(exc))

            try:
                await connection.request(
                    ACP_METHOD_SESSION_CLOSE,
                    {"sessionId": session_id},
                    timeout=float(self.config.timeouts.close),
                )
                info["close"] = True
            except (AcpDisconnected, AcpTimeout, AcpProtocolError) as exc:
                return fail("handshake.close", str(exc))

            last_error: Exception | None = None
            for method in RESUME_METHODS:
                try:
                    await connection.request(
                        method,
                        {"sessionId": session_id, "cwd": lease.workspace_root, "mcpServers": []},
                        timeout=float(self.config.timeouts.session),
                    )
                    info["resume"] = True
                    break
                except AcpProtocolError as exc:
                    last_error = exc
                    if method == RESUME_METHODS[-1]:
                        return fail("handshake.resume", f"无法恢复会话 {session_id}：{last_error}")
                    continue
                except (AcpDisconnected, AcpTimeout) as exc:
                    return fail("handshake.resume", str(exc))

            try:
                await connection.request(
                    ACP_METHOD_SESSION_CLOSE,
                    {"sessionId": session_id},
                    timeout=float(self.config.timeouts.close),
                )
            except (AcpDisconnected, AcpTimeout, AcpProtocolError) as exc:
                return fail("handshake.close", str(exc))
            info["ok"] = True
            return info
        finally:
            try:
                await connection.close(grace_seconds=float(self.config.timeouts.close))
            except Exception:
                pass

    def _host_chain_report(self) -> dict[str, Any]:
        snapshot_service = getattr(self.lifecycle, "snapshot_service", None)
        auto_apply = getattr(self.lifecycle, "auto_apply_coordinator", None)
        return {
            "snapshot_service": snapshot_service is not None,
            "verifier": getattr(self.lifecycle, "verification_service", None) is not None,
            "auto_apply": auto_apply is not None,
            "auto_apply_enabled": bool(getattr(auto_apply, "enabled", False)),
        }

    # ------------------------------------------------------------------ #
    # Execute
    # ------------------------------------------------------------------ #

    async def execute(self, request: WorkRequest, emit: Any) -> WorkResult:
        if request.task_type != "code.agent":
            return WorkResult("failed", error=f"unsupported task_type: {request.task_type}")
        project_root = request.task_input.get("project_root")
        if not isinstance(project_root, str) or not project_root.strip():
            return WorkResult("failed", error="code.agent requires input.project_root")
        try:
            snapshot = self.lifecycle.create_snapshot(request.attempt_id, project_root)
        except Exception as exc:
            return WorkResult("failed", error=f"snapshot creation failed: {type(exc).__name__}: {exc}")

        await self.lifecycle.emit(
            emit,
            request.attempt_id,
            "progress",
            {"phase": "snapshot_ready", "snapshot_id": snapshot.snapshot_id},
        )
        lease: SandboxLease | None = None
        run: _HarnessRun | None = None
        try:
            lease = await self._acquire_sandbox(request, snapshot)
            run = self._new_run(request, snapshot, lease, emit)
            await self._publish_activity(run, emit, status="connecting", step="启动 DeepSeek Harness")
            return await self._open_and_prompt(run)
        except SandboxUnavailable as exc:
            if run is not None:
                # The run already existed, so the failure belongs to *this*
                # attempt's projection (no new synthetic activity).
                await self._publish_activity(
                    run,
                    emit,
                    status="failed",
                    step="无法启动 Harness",
                    error=f"{exc.reason}: {exc.detail}",
                    confidence="unknown",
                    finished_at=_now_iso(),
                )
                await self._emit_unavailable(emit, request.attempt_id, exc)
            else:
                await self._fail_closed(request, emit, exc)
            return WorkResult("failed", error=f"harness unavailable: {exc.reason}: {exc.detail}")
        except Exception as exc:
            if run is not None:
                await self._publish_activity(
                    run,
                    emit,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    finished_at=_now_iso(),
                )
            return WorkResult("failed", error=f"deepseek harness failed: {type(exc).__name__}: {exc}")
        finally:
            if run is not None:
                await self._teardown(run)
            if lease is not None:
                self.sandbox_gate.release(lease)

    async def _acquire_sandbox(self, request: WorkRequest, snapshot: WorkspaceSnapshot) -> SandboxLease:
        """Run the fail-closed preflight for one attempt's snapshot.

        The scope list contains two different things on purpose:

        * the Host's work roots — a project may only be snapshotted from there,
          which ``WorkspaceSnapshotService`` already enforces;
        * the snapshot root — the agent's cwd is a *snapshot*, which by design
          lives outside the user's project tree, so requiring it to be inside the
          work roots would refuse every legitimate task.
        """
        scope_roots = list(self.allowed_roots)
        snapshot_root = getattr(self.lifecycle.snapshot_service, "snapshot_root", None)
        if snapshot_root is not None:
            scope_roots.append(str(snapshot_root))
        return await self.sandbox_gate.acquire(
            attempt_id=request.attempt_id,
            workspace_root=snapshot.workspace_root,
            allowed_roots=scope_roots,
        )

    async def _fail_closed(self, request: WorkRequest, emit: Any, exc: SandboxUnavailable) -> None:
        """Record *why* nothing ran; no process is started on this path."""
        activity = ProviderActivity(
            attempt_id=request.attempt_id,
            provider_id=self.capabilities.provider_id,
            status="failed",
            started_at=_now_iso(),
            updated_at=_now_iso(),
            finished_at=_now_iso(),
            error=f"{exc.reason}: {exc.detail}",
            confidence="unknown",
            details={"fail_closed": exc.reason},
        )
        await self.lifecycle.emit(emit, request.attempt_id, "provider_activity", activity.as_dict())
        await self._emit_unavailable(emit, request.attempt_id, exc)

    async def _emit_unavailable(self, emit: Any, attempt_id: str, exc: SandboxUnavailable) -> None:
        """One explicit event for "no agent process ran", whatever the gate was."""
        await self.lifecycle.emit(
            emit,
            attempt_id,
            "provider_unavailable",
            {"reason": exc.reason, "detail": exc.detail},
        )

    def _new_run(
        self,
        request: WorkRequest,
        snapshot: WorkspaceSnapshot,
        lease: SandboxLease,
        emit: Any,
    ) -> _HarnessRun:
        activity = ProviderActivity(
            attempt_id=request.attempt_id,
            provider_id=self.capabilities.provider_id,
            status="connecting",
            step="启动 DeepSeek Harness",
            started_at=_now_iso(),
            updated_at=_now_iso(),
            confidence="unknown",
            details={
                "sandbox": dict(lease.evidence),
                "workspace_root": lease.workspace_root,
                # The snapshot id travels with the projection so a Host restart
                # can rebuild a resume checkpoint without guessing.
                "snapshot_id": snapshot.snapshot_id,
            },
        )
        run = _HarnessRun(
            attempt_id=request.attempt_id,
            request=request,
            objective=request.objective,
            snapshot=snapshot,
            lease=lease,
            activity=activity,
            emit=emit,
        )
        self._runs[request.attempt_id] = run
        return run

    # ------------------------------------------------------------------ #
    # ACP session
    # ------------------------------------------------------------------ #

    def _launch_spec(self, run: _HarnessRun) -> AcpLaunchSpec:
        return self._launch_spec_for(run.lease)

    def _launch_spec_for(self, lease: SandboxLease) -> AcpLaunchSpec:
        argv = harness_argv(self.config, profile_args=())
        environment = dict(self.config.env)
        environment.update(lease.env_overrides)
        return AcpLaunchSpec(
            executable=argv[0],
            args=tuple(argv[1:]),
            cwd=lease.workspace_root,
            env=environment,
        )

    async def _spawn(self, run: _HarnessRun) -> AcpConnection:
        spec = self._launch_spec(run)
        if not Path(spec.executable).is_file():
            raise SandboxUnavailable(
                "harness_not_found", f"Harness 可执行文件不存在：{spec.executable}"
            )
        factory = self._connection_factory or self._default_connection
        try:
            connection = await factory(
                spec,
                on_notification=lambda method, params: self._on_notification(run, method, params),
                on_request=lambda method, params: self._on_request(run, method, params),
                on_disconnect=lambda error: self._on_disconnect(run, error),
            )
        except AcpSpawnError as exc:
            raise SandboxUnavailable("harness_not_runnable", str(exc)) from exc
        run.connection = connection
        return connection

    @staticmethod
    async def _default_connection(spec: AcpLaunchSpec, **handlers: Any) -> AcpConnection:
        return await AcpConnection.spawn(spec, **handlers)

    async def _open_and_prompt(self, run: _HarnessRun) -> WorkResult:
        connection = await self._spawn(run)
        handshake = await self._initialize(run, connection)
        run.handshake = handshake
        session_id = await self._start_session(run, connection)
        run.session_id = session_id
        await self._publish_activity(run, run.emit, session_id=session_id)
        await self.lifecycle.emit(
            run.emit,
            run.attempt_id,
            "provider_session",
            {
                "session_id": session_id,
                "resumed": run.resumed,
                "cwd": run.lease.workspace_root,
                "provider": self.capabilities.provider_id,
            },
        )
        return await self._prompt_and_collect(run, self._prompt_text(run))

    async def _initialize(self, run: _HarnessRun, connection: AcpConnection) -> AcpHandshake:
        try:
            result = await connection.request(
                ACP_METHOD_INITIALIZE,
                {
                    "protocolVersion": int(self.config.protocol_version or ACP_PROTOCOL_VERSION),
                    # No client capabilities: this build never asks the client to
                    # read or write files, so advertising them would only widen
                    # the surface for no benefit.
                    "clientCapabilities": {},
                },
                timeout=float(self.config.timeouts.initialize),
            )
        except AcpDisconnected as exc:
            raise SandboxUnavailable("acp_disconnected", _disconnect_detail(exc)) from exc
        except AcpTimeout as exc:
            raise SandboxUnavailable("acp_initialize_timeout", str(exc)) from exc
        except AcpProtocolError as exc:
            raise SandboxUnavailable(
                "acp_initialize_failed", f"ACP initialize 失败：{exc}"
            ) from exc
        handshake = AcpHandshake.from_result(result)
        evidence = self.sandbox_gate.confirm_capabilities(handshake)
        run.activity = run.activity.with_changes(
            details={**dict(run.activity.details), "acp": evidence, "sandbox": dict(run.lease.evidence)}
        )
        return handshake

    async def _start_session(self, run: _HarnessRun, connection: AcpConnection) -> str:
        params = {"cwd": run.lease.workspace_root, "mcpServers": []}
        try:
            result = await connection.request(
                ACP_METHOD_SESSION_NEW, params, timeout=float(self.config.timeouts.session)
            )
        except AcpDisconnected as exc:
            raise SandboxUnavailable("acp_disconnected", _disconnect_detail(exc)) from exc
        except AcpTimeout as exc:
            raise SandboxUnavailable("acp_session_timeout", str(exc)) from exc
        except AcpProtocolError as exc:
            raise SandboxUnavailable(
                "acp_session_refused", f"Harness 拒绝创建会话：{exc}"
            ) from exc
        session_id = str((result or {}).get("sessionId") or "").strip()
        if not session_id:
            raise SandboxUnavailable(
                "acp_session_missing", "Harness 未返回 session id，无法绑定任务"
            )
        return session_id

    async def _resume_session(self, run: _HarnessRun, connection: AcpConnection, session_id: str) -> None:
        last_error: Exception | None = None
        for method in RESUME_METHODS:
            try:
                await connection.request(
                    method,
                    {"sessionId": session_id, "cwd": run.lease.workspace_root, "mcpServers": []},
                    timeout=float(self.config.timeouts.session),
                )
                run.resumed = True
                return
            except AcpProtocolError as exc:
                last_error = exc
                if method == RESUME_METHODS[-1]:
                    break
                continue
            except (AcpTimeout, AcpDisconnected):
                raise
        raise SandboxUnavailable(
            "acp_resume_refused",
            f"无法恢复 Harness 会话 {session_id}：{last_error}",
        )

    async def _prompt_and_collect(self, run: _HarnessRun, text: str) -> WorkResult:
        connection = run.connection
        assert connection is not None
        await self._publish_activity(run, run.emit, status="thinking", step="等待模型回复", tool=None)
        run.prompt_future = asyncio.ensure_future(
            connection.request(
                ACP_METHOD_SESSION_PROMPT,
                {"sessionId": run.session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=float(self.config.timeouts.prompt),
            )
        )
        # The prompt future is also awaited by ``cancel``; if the host cancels
        # this task first, nobody would read its exception. That is a warning
        # ("exception was never retrieved"), not a bug, but warnings from a
        # working cancel path are noise nobody should have to triage.
        run.prompt_future.add_done_callback(_consume_exception)
        try:
            result = await asyncio.shield(run.prompt_future)
            run.stop_reason = str((result or {}).get("stopReason") or "")
        except AcpDisconnected as exc:
            return await self._unknown_result(run, "harness_disconnected", _disconnect_detail(exc))
        except AcpTimeout:
            return await self._unknown_result(
                run,
                "harness_timeout",
                f"Harness 在 {self.config.timeouts.prompt} 秒内没有结束本轮任务",
            )
        except AcpProtocolError as exc:
            if run.cancel_requested:
                return await self._unknown_result(
                    run, "cancel_unconfirmed", f"取消期间 Harness 返回错误：{exc}"
                )
            # The agent answered with a JSON-RPC error (no credentials, model
            # route failure, ...). That is a *reported* failure, so the task ends
            # as failed with the agent's own message preserved.
            await self._publish_activity(
                run,
                run.emit,
                status="failed",
                step="Harness 报告失败",
                error=str(exc),
                confidence="unknown",
                finished_at=_now_iso(),
            )
            return WorkResult(
                "failed",
                error=f"harness prompt failed: {exc}",
                output=self._diagnostics(run),
            )
        finally:
            run.prompt_future = None

        return await self._result_from_stop_reason(run)

    async def _result_from_stop_reason(self, run: _HarnessRun) -> WorkResult:
        stop_reason = run.stop_reason or "unknown"
        if run.cancel_requested or stop_reason == "cancelled":
            if run.cancel_confirmed or stop_reason == "cancelled":
                run.cancel_confirmed = True
                run.cancel_status = "confirmed"
                await self._publish_activity(
                    run,
                    run.emit,
                    status="cancelled",
                    step="已取消",
                    finished_at=_now_iso(),
                )
                return WorkResult(
                    "cancelled",
                    output={**self._diagnostics(run), "cancel_confirmed": True},
                    error="cancelled by user",
                )
            return await self._unknown_result(
                run, "cancel_unconfirmed", "取消请求没有得到 Harness 确认"
            )
        if stop_reason != "end_turn":
            await self._publish_activity(
                run,
                run.emit,
                status="failed",
                error=f"harness stop reason: {stop_reason}",
                finished_at=_now_iso(),
            )
            return WorkResult(
                "failed",
                error=f"harness stop reason: {stop_reason}",
                output=self._diagnostics(run),
            )

        outcome = CodeAgentOutcome(
            "completed",
            summary=run.message_text[: self.config.max_message_chars],
            output=self._diagnostics(run),
            evidence=tuple(self._evidence(run)),
        )
        result = await self.lifecycle.result_from_outcome(
            run.request, run.snapshot, outcome, run.emit
        )
        verification_status = str(result.output.get("verification_status") or "unverified")
        confidence = "confirmed" if verification_status == "verified" else "assumed"
        if run.scope_unverified or result.status == "waiting_approval":
            confidence = "assumed"
        if result.status == "completed":
            await self._publish_activity(
                run,
                run.emit,
                status="completed",
                step="已完成",
                tool=None,
                confidence=confidence,
                finished_at=_now_iso(),
            )
        elif result.status == "waiting_approval":
            # The agent stopped, but the Host is now waiting for a human: that is
            # exactly 「等待授权」, not 「思考中」. Leaving it as "thinking" made a
            # paused task look busy on the work page.
            await self._publish_activity(
                run,
                run.emit,
                status="waiting_permission",
                step="等待补丁写回审批",
                tool=None,
                waiting_reason=str(result.approval.get("summary") or "补丁需要审批"),
                confidence="assumed",
            )
        return result

    async def _unknown_result(self, run: _HarnessRun, reason: str, detail: str) -> WorkResult:
        """Terminal state for "we do not know whether the agent's work happened"."""
        run.cancel_status = "unknown"
        await self._publish_activity(
            run,
            run.emit,
            status="unknown_result",
            step="结果未知",
            error=detail,
            confidence="unknown",
            finished_at=_now_iso(),
            details={**dict(run.activity.details), "unknown_reason": reason},
        )
        await self.lifecycle.emit(
            run.emit,
            run.attempt_id,
            "provider_disconnected" if run.disconnect is not None else "provider_unconfirmed",
            {
                "reason": reason,
                "detail": detail,
                "exit_code": getattr(run.disconnect, "exit_code", None),
                "stderr_tail": getattr(run.disconnect, "stderr_tail", ""),
            },
        )
        return WorkResult(
            "unknown_result",
            output=self._diagnostics(run),
            error=f"{reason}: {detail}",
        )

    # ------------------------------------------------------------------ #
    # ACP callbacks
    # ------------------------------------------------------------------ #

    def _on_disconnect(self, run: _HarnessRun, error: AcpDisconnected) -> None:
        run.disconnect = error
        run.disconnect_event.set()

    async def _on_notification(self, run: _HarnessRun, method: str, params: Mapping[str, Any]) -> None:
        if method != ACP_METHOD_SESSION_UPDATE:
            return
        session_id = str(params.get("sessionId") or "")
        if run.session_id and session_id and session_id != run.session_id:
            return
        update = params.get("update")
        if not isinstance(update, Mapping):
            return
        await self._handle_update(run, update)

    async def _handle_update(self, run: _HarnessRun, update: Mapping[str, Any]) -> None:
        kind = str(update.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            text = _text_of(update.get("content"))
            if text:
                run.message_parts.append(text)
            return
        if kind == "agent_thought_chunk":
            if run.activity.status != "thinking":
                await self._publish_activity(
                    run, run.emit, status="thinking", step="思考中", tool=None
                )
            return
        if kind == "tool_call":
            await self._handle_tool_call(run, update)
            return
        if kind == "tool_call_update":
            await self._handle_tool_call_update(run, update)
            return
        if kind == "usage_update":
            run.usage = {
                "used": update.get("used"),
                "size": update.get("size"),
            }
            return
        if kind == "config_option_update":
            run.activity = run.activity.with_changes(
                details={
                    **dict(run.activity.details),
                    "config_options": list(update.get("configOptions") or []),
                }
            )
            return

    async def _handle_tool_call(self, run: _HarnessRun, update: Mapping[str, Any]) -> None:
        tool_call_id = str(update.get("toolCallId") or "")
        title = str(update.get("title") or update.get("kind") or "tool")[: self.config.max_tool_label]
        record = {
            "tool_call_id": tool_call_id,
            "title": title,
            "kind": str(update.get("kind") or ""),
            "status": str(update.get("status") or "in_progress"),
        }
        if tool_call_id:
            run.tool_calls[tool_call_id] = record
        await self.lifecycle.emit(
            run.emit,
            run.attempt_id,
            "provider_tool",
            {**record, "phase": "started"},
        )
        await self._publish_activity(
            run,
            run.emit,
            status="tool_running",
            tool=title,
            step=f"执行工具：{title}",
        )

    async def _handle_tool_call_update(self, run: _HarnessRun, update: Mapping[str, Any]) -> None:
        tool_call_id = str(update.get("toolCallId") or "")
        status = str(update.get("status") or "")
        record = run.tool_calls.setdefault(tool_call_id, {"tool_call_id": tool_call_id})
        if status:
            record["status"] = status
        summary = _tool_content_text(update.get("content"))
        if summary:
            record["summary"] = summary[: self.config.max_tool_label]
        # 结构化操作类型从**同一条工具记录**继承：更新事件本身不带 kind，
        # 而自由文本的 summary/title（"运行 pytest -q"）绝不能当依据。
        # 聊天侧只有拿到这个字段才敢说"在改代码"或"在跑测试"。
        kind = str(record.get("kind") or update.get("kind") or "")
        await self.lifecycle.emit(
            run.emit,
            run.attempt_id,
            "provider_tool",
            {
                "tool_call_id": tool_call_id,
                "title": str(record.get("title") or ""),
                "kind": kind,
                "status": status or str(record.get("status") or ""),
                "phase": "finished",
                "summary": summary[: self.config.max_tool_label],
            },
        )
        if status in {"completed", "failed"} and run.activity.status == "tool_running":
            await self._publish_activity(
                run,
                run.emit,
                status="thinking",
                tool=None,
                step="思考中" if status == "completed" else "工具执行失败，继续处理",
            )

    async def _on_request(self, run: _HarnessRun, method: str, params: Mapping[str, Any]) -> Any:
        if method == ACP_METHOD_REQUEST_PERMISSION:
            return await self._handle_permission(run, params)
        if method in {ACP_METHOD_FS_READ_TEXT_FILE, ACP_METHOD_FS_WRITE_TEXT_FILE}:
            # The client advertised no fs capabilities; a build that calls these
            # anyway gets a refusal instead of silent file access.
            await self._record_permission(
                run,
                decision="denied",
                tool=method,
                reason="bridge does not expose host filesystem to the agent",
                scope="unsupported_method",
            )
            raise AcpProtocolError(f"{method} is not offered by this bridge")
        return None

    async def _handle_permission(self, run: _HarnessRun, params: Mapping[str, Any]) -> Mapping[str, Any]:
        tool_call = params.get("toolCall")
        tool_call = dict(tool_call) if isinstance(tool_call, Mapping) else {}
        options = [dict(item) for item in params.get("options") or () if isinstance(item, Mapping)]
        decision, scope, reason = self._permission_decision(run, tool_call)
        tool_label = str(
            tool_call.get("title") or tool_call.get("toolCallId") or tool_call.get("kind") or "tool"
        )[: self.config.max_tool_label]
        await self._record_permission(
            run, decision=decision, tool=tool_label, reason=reason, scope=scope
        )
        if decision == "allowed":
            option_id = _select_option(options, "allow_once") or _select_option(options, "allow")
            if option_id is None:
                await self._record_permission(
                    run,
                    decision="denied",
                    tool=tool_label,
                    reason="agent offered no allow option",
                    scope="no_allow_option",
                )
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": option_id}}
        option_id = _select_option(options, "reject_once") or _select_option(options, "reject")
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}

    def _permission_decision(
        self, run: _HarnessRun, tool_call: Mapping[str, Any]
    ) -> tuple[str, str, str]:
        """Decide one permission request: snapshot scope in, everything else out.

        ``unverified`` means the tool call carried no path or URL at all. The
        request is still allowed — the Harness' own sandbox is the enforcement
        boundary, and refusing every opaque tool call would make the bridge
        unusable — but the fact is recorded (``scope_unverified``) so the result
        confidence drops to ``assumed`` instead of silently claiming certainty.
        """
        haystack = _flatten_strings(tool_call)
        lowered = " ".join(haystack).casefold()
        # URLs are checked before the marker list so the recorded reason says
        # "network" rather than the generic "out of scope operation".
        urls = [item for item in haystack if _URL.search(item)]
        if urls:
            return "denied", "network", "网络操作不属于任务快照范围"
        for marker in OUT_OF_SCOPE_MARKERS:
            if marker in lowered:
                return "denied", "out_of_scope_operation", f"操作超出任务快照范围：{marker.strip()}"

        root = Path(run.lease.workspace_root)
        paths: list[str] = []
        for item in haystack:
            paths.extend(match.group(0) for match in _ABSOLUTE_PATH.finditer(item))
        outside = [item for item in paths if not _inside(item, root)]
        if outside:
            preview = ", ".join(outside[:3])
            return "denied", "outside_snapshot", f"路径不在任务快照内：{preview}"
        if not paths:
            run.scope_unverified = True
            return "allowed", "unverified", "工具调用未暴露路径，按快照沙箱放行"
        return "allowed", "snapshot", "操作在任务快照范围内"

    async def _record_permission(
        self,
        run: _HarnessRun,
        *,
        decision: str,
        tool: str,
        reason: str,
        scope: str,
    ) -> None:
        payload = {
            "decision": decision,
            "tool": tool,
            "reason": reason,
            "scope": scope,
            "workspace_root": run.lease.workspace_root,
        }
        run.permission_events.append(payload)
        if decision == "denied":
            run.activity = run.activity.with_changes(
                details={
                    **dict(run.activity.details),
                    "last_denied_permission": payload,
                }
            )
        await self.lifecycle.emit(run.emit, run.attempt_id, "provider_permission", payload)

    # ------------------------------------------------------------------ #
    # Cancellation
    # ------------------------------------------------------------------ #

    async def cancel(self, attempt_id: str) -> bool:
        run = self._runs.get(attempt_id)
        if run is None:
            return False
        if run.connection is None or not run.session_id:
            run.cancel_status = "unknown"
            return False
        run.cancel_requested = True
        await self._publish_activity(run, run.emit, status="cancelling", step="正在取消")
        try:
            await run.connection.notify(ACP_METHOD_SESSION_CANCEL, {"sessionId": run.session_id})
        except AcpError:
            run.cancel_status = "unknown"
            return False
        future = run.prompt_future
        if future is None:
            # Nothing in flight: the cancel is confirmed by construction.
            run.cancel_confirmed = True
            run.cancel_status = "confirmed"
            return True
        try:
            await asyncio.wait_for(asyncio.shield(future), timeout=float(self.config.timeouts.cancel))
        except asyncio.TimeoutError:
            run.cancel_status = "unknown"
            await self._publish_activity(
                run,
                run.emit,
                status="unknown_result",
                step="取消失败：Harness 未确认停止",
                error="cancel not confirmed",
                confidence="unknown",
                finished_at=_now_iso(),
            )
            return False
        except (AcpDisconnected, AcpError, asyncio.CancelledError):
            run.cancel_status = "unknown"
            return False
        run.cancel_confirmed = True
        run.cancel_status = "confirmed"
        return True

    def cancel_status(self, attempt_id: str) -> str | None:
        run = self._runs.get(attempt_id)
        if run is not None and run.cancel_status:
            return run.cancel_status
        return self._cancel_outcomes.get(attempt_id)

    # ------------------------------------------------------------------ #
    # Resume
    # ------------------------------------------------------------------ #

    def recovery_checkpoint(self, activity: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        """Build the resume checkpoint for an attempt that a restart interrupted.

        Returns ``None`` when there is nothing safe to resume: no session id, or
        no snapshot id to prove which workspace the session belongs to. The Host
        treats ``None`` as "leave it interrupted", which is the conservative
        answer.
        """
        payload = dict(activity or {})
        session_id = str(payload.get("session_id") or "").strip()
        details = payload.get("details")
        details = dict(details) if isinstance(details, Mapping) else {}
        snapshot_id = str(details.get("snapshot_id") or "").strip()
        if not session_id or not snapshot_id:
            return None
        if str(payload.get("status") or "") in {"completed", "failed", "disconnected", "unknown_result"}:
            return None
        return {
            "kind": CHECKPOINT_HARNESS_SESSION,
            "session_id": session_id,
            "snapshot_id": snapshot_id,
            "original_attempt": str(payload.get("attempt_id") or ""),
            "previous_status": str(payload.get("status") or ""),
        }

    async def resume(
        self,
        request: WorkRequest,
        checkpoint: Mapping[str, Any],
        decision: str,
        emit: Any,
    ) -> WorkResult:
        kind = str(checkpoint.get("kind") or "")
        if kind == CHECKPOINT_CODE_APPLY:
            # The resumed attempt never opens an ACP session (it only writes back
            # an already-approved patch), but it still belongs to this provider —
            # so it must still publish a projection, or the work page would keep
            # showing the *previous* attempt's state for a finished task.
            await self._emit_standalone_activity(
                request.attempt_id,
                emit,
                status="thinking",
                step="写回已批准的补丁",
                details={"checkpoint": CHECKPOINT_CODE_APPLY},
            )
            result = await self.lifecycle.resume_apply_approval(
                request, checkpoint, decision, emit
            )
            await self._emit_standalone_activity(
                request.attempt_id,
                emit,
                status=_activity_status_for(result.status),
                step=_activity_step_for(result.status),
                error=result.error,
                waiting_reason="补丁需要审批" if result.status == "waiting_approval" else None,
                confidence="confirmed" if result.status == "completed" else "unknown",
                finished_at=None if result.status == "waiting_approval" else _now_iso(),
            )
            return result
        if kind != CHECKPOINT_HARNESS_SESSION:
            return WorkResult(
                "failed",
                error=f"unsupported harness checkpoint: {kind or 'missing kind'}",
            )
        session_id = str(checkpoint.get("session_id") or "").strip()
        snapshot_id = str(checkpoint.get("snapshot_id") or "").strip()
        if not session_id or not snapshot_id:
            return WorkResult("failed", error="harness checkpoint is missing session or snapshot id")
        try:
            snapshot = self.lifecycle.load_snapshot(str(checkpoint.get("original_attempt") or request.attempt_id))
        except Exception as exc:
            return WorkResult(
                "unknown_result",
                error=f"cannot restore snapshot for harness resume: {type(exc).__name__}: {exc}",
            )
        if snapshot.snapshot_id != snapshot_id:
            return WorkResult(
                "unknown_result",
                error="harness checkpoint snapshot does not match the stored snapshot",
            )

        lease: SandboxLease | None = None
        run: _HarnessRun | None = None
        try:
            lease = await self._acquire_sandbox(request, snapshot)
            run = self._new_run(request, snapshot, lease, emit)
            run.session_id = session_id
            run.resumed = True
            await self._publish_activity(
                run,
                emit,
                status="connecting",
                step=f"恢复会话 {session_id}",
                details={
                    **dict(run.activity.details),
                    "resumed_session": session_id,
                    "previous_status": str(checkpoint.get("previous_status") or ""),
                },
            )
            connection = await self._spawn(run)
            handshake = await self._initialize(run, connection)
            run.handshake = handshake
            await self._resume_session(run, connection, session_id)
            await self.lifecycle.emit(
                emit,
                request.attempt_id,
                "provider_session",
                {
                    "session_id": session_id,
                    "resumed": True,
                    "cwd": run.lease.workspace_root,
                    "provider": self.capabilities.provider_id,
                },
            )
            return await self._prompt_and_collect(run, self._resume_prompt_text(run))
        except SandboxUnavailable as exc:
            # Resume refused, or the sandbox could not be re-confirmed: the run
            # may already have produced side effects, so the result is unknown
            # and the task must not be retried automatically.
            if run is not None:
                await self._publish_activity(
                    run,
                    emit,
                    status="unknown_result",
                    step="恢复失败",
                    error=f"{exc.reason}: {exc.detail}",
                    confidence="unknown",
                    finished_at=_now_iso(),
                )
            else:
                await self._fail_closed(request, emit, exc)
            return WorkResult(
                "unknown_result",
                error=f"harness resume failed: {exc.reason}: {exc.detail}",
            )
        except Exception as exc:
            return WorkResult(
                "unknown_result",
                error=f"harness resume failed: {type(exc).__name__}: {exc}",
            )
        finally:
            if run is not None:
                await self._teardown(run)
            if lease is not None:
                self.sandbox_gate.release(lease)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    #: 任务边界说明。**不是客套话**：实测模型会把任务文本里的项目绝对路径
    #: 当成工作目录，直接去读、去改原项目（连快照都不用）。Harness 的
    #: `dsh-fs-sandbox` 只在目标超出"工作目录 + 平台临时目录"时才拒绝写入，
    #: 所以项目恰好位于系统临时目录下时，那次越界是被规则放行的。边界写进
    #: 提示词是第一道闸门；第二道是 Host 侧的 project_scope 核对（改了原项目
    #: 就判失败，见 `code_task_lifecycle.inspect_project_scope`）。
    _BOUNDARY_RULE = (
        "重要边界：你**只能**读写当前工作目录（就是这次的快照目录）。\n"
        "任务文本里出现的任何项目路径都只是标识，禁止用绝对路径去读写工作目录之外的"
        "文件或目录；需要参考原项目内容时，就使用工作目录里的对应文件。\n"
        "如果你的改动落在了工作目录之外，这次任务会被判为失败。\n"
    )

    def _prompt_text(self, run: _HarnessRun) -> str:
        return (
            "你在这个工作目录里执行一个代码任务，这个目录是任务快照。\n"
            f"{self._BOUNDARY_RULE}"
            f"任务目标：{run.objective}\n"
            "完成后请用简短的一段话总结你做了什么、改了哪些文件。"
        )

    def _resume_prompt_text(self, run: _HarnessRun) -> str:
        return (
            "上一次运行在处理这个任务时被中断了，现在会话已经恢复在同一工作目录。\n"
            f"{self._BOUNDARY_RULE}"
            f"原任务目标：{run.objective}\n"
            "请先检查当前目录的状态，继续把任务做完，然后简短总结结果。"
        )

    async def _emit_standalone_activity(self, attempt_id: str, emit: Any, **fields: Any) -> None:
        """Publish a projection for an attempt that has no live ACP run."""
        base: dict[str, Any] = {
            "attempt_id": attempt_id,
            "provider_id": self.capabilities.provider_id,
            "status": "thinking",
            "started_at": _now_iso(),
            "updated_at": _now_iso(),
            "confidence": "assumed",
        }
        base.update(fields)
        activity = ProviderActivity(**base)
        await self.lifecycle.emit(emit, attempt_id, "provider_activity", activity.as_dict())

    async def _publish_activity(self, run: _HarnessRun, emit: Any, **changes: Any) -> None:
        """Persist+emit the projection, but only when something visible changed."""
        updated = run.activity.with_changes(**changes)
        if _visible_fields(updated) == _visible_fields(run.activity):
            run.activity = updated
            return
        run.activity = updated
        await self.lifecycle.emit(
            emit, run.attempt_id, "provider_activity", updated.as_dict()
        )

    async def _teardown(self, run: _HarnessRun) -> None:
        """Close the ACP session and the process; keeps the last known state."""
        connection = run.connection
        if connection is not None and run.session_id:
            try:
                await connection.request(
                    ACP_METHOD_SESSION_CLOSE,
                    {"sessionId": run.session_id},
                    timeout=float(self.config.timeouts.close),
                )
            except Exception:
                pass
        exit_code = None
        if connection is not None:
            try:
                exit_code = await connection.close(grace_seconds=float(self.config.timeouts.close))
            except Exception:
                exit_code = getattr(connection, "exit_code", None)
        run.activity = run.activity.with_changes(
            details={**dict(run.activity.details), "exit_code": exit_code}
        )
        if run.disconnect is not None and not run.activity.is_terminal:
            await self._publish_activity(
                run,
                run.emit,
                status="disconnected",
                step="连接已断开",
                disconnect_reason=run.disconnect.args[0] if run.disconnect.args else "harness disconnected",
                confidence="unknown",
                finished_at=_now_iso(),
            )
        await self.lifecycle.emit(
            run.emit,
            run.attempt_id,
            "provider_closed",
            {"session_id": run.session_id, "exit_code": exit_code, "resumed": run.resumed},
        )
        if run.cancel_status:
            self._cancel_outcomes[run.attempt_id] = run.cancel_status
            while len(self._cancel_outcomes) > 64:
                self._cancel_outcomes.pop(next(iter(self._cancel_outcomes)))
        self._runs.pop(run.attempt_id, None)

    def _diagnostics(self, run: _HarnessRun) -> dict[str, Any]:
        return {
            "provider": self.capabilities.provider_id,
            "provider_name": "DeepSeek Harness",
            "session_id": run.session_id,
            "resumed": run.resumed,
            "stop_reason": run.stop_reason,
            "message": run.message_text[: self.config.max_message_chars],
            "tool_calls": list(run.tool_calls.values()),
            "permissions": list(run.permission_events),
            "usage": dict(run.usage),
            "sandbox": dict(run.lease.evidence),
            "acp": {
                "protocol_version": getattr(run.handshake, "protocol_version", None),
                "agent": getattr(run.handshake, "name_text", ""),
                "agent_version": getattr(run.handshake, "version_text", ""),
            },
            "scope_unverified": run.scope_unverified,
            "exit_code": getattr(run.connection, "exit_code", None),
        }

    def _evidence(self, run: _HarnessRun) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = [
            {
                "name": "harness_session",
                "session_id": run.session_id,
                "resumed": run.resumed,
                "stop_reason": run.stop_reason,
            }
        ]
        for call in run.tool_calls.values():
            evidence.append({"name": "harness_tool", **dict(call)})
        for permission in run.permission_events:
            evidence.append({"name": "harness_permission", **permission})
        return evidence


def _consume_exception(future: Any) -> None:
    """Mark a task's exception as observed (see the prompt-future comment)."""
    try:
        if not future.cancelled():
            future.exception()
    except Exception:  # pragma: no cover - defensive
        pass


def _activity_status_for(result_status: str) -> str:
    return {
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
        "waiting_approval": "waiting_permission",
    }.get(result_status, "unknown_result")


def _probe_stage(reason: str) -> str:
    """把沙箱预检的失败原因归位到探测层的具体阶段。"""
    if reason.startswith("version"):
        return "probe.version"
    if reason.startswith("workspace"):
        return "probe.workspace"
    if reason == "harness_unavailable":
        return "config"
    return "probe.sandbox"


def _activity_step_for(result_status: str) -> str:
    return {
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "waiting_approval": "等待补丁写回审批",
    }.get(result_status, "结果未知")


def _visible_fields(activity: ProviderActivity) -> tuple[Any, ...]:
    return (
        activity.status,
        activity.step,
        activity.tool,
        activity.waiting_reason,
        activity.error,
        activity.disconnect_reason,
        activity.confidence,
        activity.finished_at,
        activity.session_id,
    )


def _text_of(content: Any) -> str:
    if isinstance(content, Mapping):
        if str(content.get("type") or "text") == "text":
            return str(content.get("text") or "")
        return ""
    if isinstance(content, str):
        return content
    return ""


def _tool_content_text(content: Any) -> str:
    chunks: list[str] = []
    items = content if isinstance(content, Sequence) and not isinstance(content, (str, bytes)) else [content]
    for item in items:
        if isinstance(item, Mapping):
            nested = item.get("content")
            if isinstance(nested, Mapping):
                chunks.append(_text_of(nested))
            else:
                chunks.append(_text_of(item))
        elif isinstance(item, str):
            chunks.append(item)
    return "".join(chunk for chunk in chunks if chunk).strip()


def _flatten_strings(value: Any, *, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        output: list[str] = []
        for key, item in value.items():
            output.extend(_flatten_strings(key, depth=depth + 1))
            output.extend(_flatten_strings(item, depth=depth + 1))
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        output = []
        for item in value:
            output.extend(_flatten_strings(item, depth=depth + 1))
        return output
    return []


def _inside(candidate: str, root: Path) -> bool:
    try:
        path = Path(candidate).expanduser()
        if not path.is_absolute():
            return True
        resolved = path.resolve()
    except (OSError, ValueError):
        return False
    root_text = str(root).rstrip("\\/").casefold()
    path_text = str(resolved).casefold()
    return path_text == root_text or path_text.startswith(root_text + "\\") or path_text.startswith(root_text + "/")


def _select_option(options: Sequence[Mapping[str, Any]], kind: str) -> str | None:
    for option in options:
        if str(option.get("kind") or "").casefold() == kind:
            option_id = str(option.get("optionId") or "").strip()
            if option_id:
                return option_id
    return None


def _disconnect_detail(error: AcpDisconnected) -> str:
    parts = [str(error)]
    if error.exit_code is not None:
        parts.append(f"exit_code={error.exit_code}")
    tail = str(error.stderr_tail or "").strip()
    if tail:
        parts.append(f"stderr={tail[-400:]}")
    return "; ".join(parts)
