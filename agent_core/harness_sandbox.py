"""Fail-closed preflight for the DeepSeek Harness sandbox.

The Harness does not announce its sandbox over ACP: ``initialize`` returns only
``agentCapabilities`` (mcp / prompt / session) — verified against the installed
build. Sandbox confinement is decided *before* the protocol starts, from
``DSH_PERMISSION_MODE`` plus the child process' working directory, and the
Harness' own ``--dump-config`` prints the composed rows that prove it
(``sandbox-policy`` with ``mode``/``workspaceRoot``, ``pwsh-sandbox``,
``approval``).

So "only accept a sandbox configuration the Harness itself confirms" is
implemented as: run ``--dump-config`` in the same shape we are about to launch
(with the same mode environment and the snapshot as cwd), and require those rows
to be present and consistent. Anything missing, mismatched or unreadable means
:class:`SandboxUnavailable` and the bridge **never starts the agent** — there is
no fallback path to a plain host process, by construction: the provider only
spawns after :meth:`HarnessSandboxGate.acquire` returns a lease.

The remaining pieces:

* ``--version`` is read from the same CLI and compared with the configured
  requirement. An unreadable version is a failure, not a pass.
* The bridge creates its own per-attempt **binding token** file. The Harness has
  no token handshake, so this is honestly only a bridge-side marker: it binds
  the lease to one attempt and its creation is the third fail-closed gate
  (an unwritable lease directory means we cannot guarantee the run is tied to
  this attempt, so we refuse).
* ``workspace_root`` must resolve to a real directory inside the Host's allowed
  roots. A snapshot that escapes the allowed roots is refused here, before any
  process exists.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .harness_acp import AcpHandshake
from .harness_config import FORBIDDEN_SANDBOX_MODES, HarnessConfig, parse_version

RowText = str


class SandboxUnavailable(RuntimeError):
    """The bridge refuses to run: some preflight gate did not pass.

    ``reason`` is a short stable code (used in events and tests);
    ``detail`` is the human-readable explanation shown to the user.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail or reason


@dataclass(frozen=True)
class CliResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


CliRunner = Callable[..., Any]


async def run_cli(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout: float,
) -> CliResult:
    """Run one short-lived Harness CLI command and capture its output."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd or None,
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, ValueError) as exc:
        raise SandboxUnavailable(
            "harness_not_runnable", f"cannot start Harness CLI: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        return CliResult(-1, "", "timed out", timed_out=True)
    return CliResult(
        process.returncode if process.returncode is not None else -1,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def harness_argv(config: HarnessConfig, *, profile_args: Sequence[str] = ()) -> list[str]:
    """Executable plus arguments, unwrapping a ``.cmd``/``.bat`` shim if needed.

    A direct ``.exe`` launch is preferred: killing the process we started also
    kills the agent. Going through ``cmd.exe`` is kept as an escape hatch for
    installs that only ship the shim, and it is documented here because that
    path can leave an orphaned child if the wrapper is killed.
    """
    executable = str(config.executable)
    args = list(config.argv(profile_args=profile_args))
    if executable.casefold().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        return [comspec, "/c", executable, *args]
    return [executable, *args]


@dataclass(frozen=True)
class HarnessInspection:
    """What the Harness' own CLI said about itself."""

    version: str
    rows: Mapping[str, RowText] = field(default_factory=dict)
    raw: str = ""

    def row(self, row_id: str) -> RowText:
        return str(self.rows.get(row_id) or "")


def parse_config_rows(text: str) -> dict[str, str]:
    """Group ``--dump-config`` output into ``row id → raw row text``.

    The dump is YAML-ish ("``- id: X``" then indented keys). Parsing it with a
    real YAML loader would need the ``!!js`` tags to exist, and a schema change
    upstream would then throw instead of degrading. Keeping the raw text per row
    keeps this robust: every check searches inside one row's text.
    """
    rows: dict[str, str] = {}
    current_id: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_id and buffer:
            rows[current_id] = "\n".join(buffer)

    for line in str(text).splitlines():
        match = re.match(r"^-\s+id:\s*(\S+)\s*$", line)
        if match:
            flush()
            current_id = match.group(1).strip().strip("'\"")
            buffer = [line]
            continue
        if current_id is not None:
            buffer.append(line)
    flush()
    return rows


@dataclass(frozen=True)
class SandboxLease:
    """A confirmed sandbox configuration bound to one attempt."""

    attempt_id: str
    mode: str
    workspace_root: str
    token: str
    token_path: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    @property
    def env_overrides(self) -> dict[str, str]:
        """Environment additions that bind the child to this lease."""
        return {
            "DSH_PERMISSION_MODE": self.mode,
            "DSH_HARNESS_ATTEMPT_TOKEN": self.token,
        }


class HarnessSandboxGate:
    """All fail-closed checks that must pass before an agent process starts."""

    def __init__(
        self,
        config: HarnessConfig,
        *,
        runner: CliRunner | None = None,
        cache_seconds: float = 300.0,
    ) -> None:
        self.config = config
        self._runner = runner or run_cli
        self._cache_seconds = max(0.0, float(cache_seconds))
        self._cache: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------------ #
    # Cached probes
    # ------------------------------------------------------------------ #

    async def _cached(self, key: str, factory: Callable[[], Any]) -> Any:
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and self._cache_seconds > 0:
            stored_at, value = cached
            if now - stored_at <= self._cache_seconds:
                return value
        value = await factory()
        if self._cache_seconds > 0:
            self._cache[key] = (now, value)
        return value

    def invalidate(self) -> None:
        self._cache.clear()

    async def probe(self, *, workspace_root: Path) -> HarnessInspection:
        """Version + composed configuration, both read from the Harness CLI."""
        version = await self._cached("version", self._probe_version)
        rows = await self._cached(
            f"dump::{self.config.sandbox_mode_value}",
            lambda: self._probe_dump_config(workspace_root),
        )
        return HarnessInspection(version=version, rows=rows, raw="\n".join(rows.values()))

    async def _probe_version(self) -> str:
        argv = harness_argv(self.config, profile_args=self.config.version_args)
        result = await self._runner(
            argv,
            cwd=str(Path.cwd()),
            env=self.config.env,
            timeout=float(self.config.timeouts.preflight),
        )
        if result.timed_out:
            raise SandboxUnavailable("version_unreadable", "读取 Harness 版本超时")
        text = f"{result.stdout}\n{result.stderr}"
        version = ""
        for line in reversed([item.strip() for item in text.splitlines() if item.strip()]):
            if parse_version(line):
                version = line.lstrip("v")
                break
        if not version:
            raise SandboxUnavailable(
                "version_unreadable",
                f"无法从 Harness 读出可解析的版本号（退出码 {result.exit_code}）",
            )
        if not self.config.required_version.satisfied_by(version):
            raise SandboxUnavailable(
                "version_incompatible",
                f"Harness 版本 {version} 不满足要求 {self.config.required_version.describe()}",
            )
        return version

    async def _probe_dump_config(self, workspace_root: Path) -> dict[str, str]:
        argv = harness_argv(self.config, profile_args=self.config.dump_config_args)
        result = await self._runner(
            argv,
            cwd=str(workspace_root),
            env=self.config.env,
            timeout=float(self.config.timeouts.preflight),
        )
        if result.timed_out:
            raise SandboxUnavailable("sandbox_probe_timeout", "读取 Harness 沙箱配置超时")
        if result.exit_code != 0:
            raise SandboxUnavailable(
                "sandbox_probe_failed",
                f"Harness 沙箱配置读取失败（退出码 {result.exit_code}）",
            )
        rows = parse_config_rows(result.stdout)
        if not rows:
            raise SandboxUnavailable(
                "sandbox_probe_empty", "Harness 未输出任何配置行，无法确认沙箱"
            )
        return rows

    # ------------------------------------------------------------------ #
    # Sandbox confirmation
    # ------------------------------------------------------------------ #

    def confirm_sandbox(self, inspection: HarnessInspection, *, workspace_root: Path) -> dict[str, Any]:
        """Check the Harness' own rows describe a confined run for this snapshot."""
        mode = self.config.sandbox_mode_value
        rows = inspection.rows
        missing = [row for row in self.config.required_config_rows if row not in rows]
        if missing:
            raise SandboxUnavailable(
                "sandbox_configuration_missing",
                f"Harness 未声明沙箱配置行：{', '.join(missing)}",
            )

        policy_row = inspection.row("sandbox-policy")
        mode_expr = _entry_value(policy_row, "mode")
        if not mode_expr:
            raise SandboxUnavailable("sandbox_mode_missing", "Harness 沙箱策略未声明 mode")
        confirmed_mode = _confirmed_mode(mode_expr, mode)
        if confirmed_mode is None:
            raise SandboxUnavailable(
                "sandbox_mode_not_confirmed",
                f"Harness 沙箱策略未确认模式 {mode}（读到：{mode_expr.strip()}）",
            )
        if confirmed_mode.casefold() in FORBIDDEN_SANDBOX_MODES:
            raise SandboxUnavailable(
                "sandbox_mode_forbidden", f"Harness 沙箱策略指向 {confirmed_mode}"
            )
        if confirmed_mode.casefold() != mode.casefold():
            # A literal that differs from what we asked for means the rows and the
            # launch disagree; refuse rather than run under an unknown mode.
            raise SandboxUnavailable(
                "sandbox_mode_not_confirmed",
                f"Harness 沙箱策略是 {confirmed_mode}，与请求的 {mode} 不一致",
            )

        root_expr = _entry_value(policy_row, "workspaceRoot") or _entry_value(policy_row, "workspace_root")
        if not root_expr:
            raise SandboxUnavailable(
                "sandbox_workspace_unbound", "Harness 沙箱策略未绑定工作目录"
            )
        if not _workspace_bound(root_expr, workspace_root):
            raise SandboxUnavailable(
                "sandbox_workspace_mismatch",
                f"Harness 沙箱工作目录与任务快照不一致（读到：{root_expr.strip()}）",
            )

        approval_row = inspection.row("approval")
        approval_expr = _entry_value(approval_row, "policy")
        if not approval_expr:
            raise SandboxUnavailable("sandbox_approval_missing", "Harness 权限策略未声明 policy")
        if _is_unconditional_never(approval_expr):
            raise SandboxUnavailable(
                "sandbox_approval_never", "Harness 权限策略被设为永久放行"
            )

        pwsh_row = inspection.row("pwsh-sandbox")
        if _row_statically_disabled(pwsh_row):
            raise SandboxUnavailable(
                "sandbox_pwsh_disabled", "Harness 的 Windows 沙箱组件被禁用"
            )

        return {
            "mode": mode,
            "mode_expression": mode_expr.strip(),
            "workspace_root": str(workspace_root),
            "workspace_expression": root_expr.strip(),
            "approval_expression": " ".join(approval_expr.split()),
            "rows": sorted(rows),
            "version": inspection.version,
        }

    def confirm_capabilities(self, handshake: AcpHandshake) -> dict[str, Any]:
        """ACP capability gate: protocol version plus the resume surface."""
        if int(handshake.protocol_version) != int(self.config.protocol_version):
            raise SandboxUnavailable(
                "acp_protocol_mismatch",
                f"ACP 协议版本 {handshake.protocol_version} 与期望的 "
                f"{self.config.protocol_version} 不一致",
            )
        session_caps = handshake.agent_capabilities.get("sessionCapabilities")
        session_caps = dict(session_caps) if isinstance(session_caps, Mapping) else {}
        missing = [cap for cap in self.config.required_session_capabilities if cap not in session_caps]
        if missing:
            raise SandboxUnavailable(
                "acp_capability_missing",
                f"Harness 未声明 ACP 能力：{', '.join(missing)}",
            )
        return {
            "protocol_version": handshake.protocol_version,
            "agent_name": handshake.name_text,
            "agent_version": handshake.version_text,
            "session_capabilities": sorted(session_caps),
            "prompt_capabilities": dict(
                handshake.agent_capabilities.get("promptCapabilities") or {}
            ),
        }

    # ------------------------------------------------------------------ #
    # Lease
    # ------------------------------------------------------------------ #

    async def acquire(
        self,
        *,
        attempt_id: str,
        workspace_root: str | Path,
        allowed_roots: Sequence[str | Path] = (),
    ) -> SandboxLease:
        """Run every gate; raise :class:`SandboxUnavailable` instead of falling back."""
        problems = self.config.validate()
        if problems:
            raise SandboxUnavailable("harness_unavailable", "; ".join(problems))

        root = Path(workspace_root).expanduser()
        try:
            root = root.resolve(strict=True)
        except OSError as exc:
            raise SandboxUnavailable(
                "workspace_missing", f"任务快照目录不可用：{type(exc).__name__}: {exc}"
            ) from exc
        if not root.is_dir():
            raise SandboxUnavailable("workspace_missing", f"任务快照不是目录：{root}")
        if allowed_roots:
            inside = False
            for allowed in allowed_roots:
                try:
                    resolved = Path(allowed).expanduser().resolve()
                except OSError:
                    continue
                if _is_within(root, resolved):
                    inside = True
                    break
            if not inside:
                raise SandboxUnavailable(
                    "workspace_out_of_scope", f"任务快照不在允许的根目录内：{root}"
                )

        inspection = await self.probe(workspace_root=root)
        evidence = self.confirm_sandbox(inspection, workspace_root=root)
        token, token_path = self._create_token(attempt_id)
        evidence["token_file"] = token_path
        return SandboxLease(
            attempt_id=attempt_id,
            mode=self.config.sandbox_mode_value,
            workspace_root=str(root),
            token=token,
            token_path=token_path,
            evidence=evidence,
        )

    def _create_token(self, attempt_id: str) -> tuple[str, str]:
        """Create the per-attempt binding token; failure is fail-closed.

        The file is the durable proof that a lease existed for this attempt: it
        is written before the agent starts and removed when the attempt ends.
        """
        if not self.config.require_sandbox_token:
            return "", ""
        directory = Path(self.config.sandbox_state_root).expanduser()
        try:
            directory.mkdir(parents=True, exist_ok=True)
            token = secrets.token_hex(16)
            path = directory / f"{_safe_name(attempt_id)}.token"
            path.write_text(token, encoding="utf-8")
            try:  # best effort: on Windows this only clears the read-only bit
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise SandboxUnavailable(
                "sandbox_token_failed",
                f"无法创建沙箱令牌：{type(exc).__name__}: {exc}",
            ) from exc
        return token, str(path)

    def release(self, lease: SandboxLease) -> None:
        """Remove the lease token; never raises (teardown must not mask errors)."""
        if not lease.token_path:
            return
        try:
            Path(lease.token_path).unlink(missing_ok=True)
        except OSError:
            pass


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value)) or "attempt"


def _is_within(path: Path, root: Path) -> bool:
    """True when ``path`` is ``root`` or below it (case-insensitive on Windows)."""
    try:
        path_text = os.path.normcase(str(path))
        root_text = os.path.normcase(str(root))
    except (TypeError, ValueError):
        return False
    if path_text == root_text:
        return True
    return path_text.startswith(root_text.rstrip("\\/") + os.sep)


def _row_lines(row: str) -> list[str]:
    return [line for line in str(row or "").splitlines() if line.strip()]


def _entry_value(row: str, key: str) -> str:
    """Value of ``key:`` inside a config row, folded across continuation lines.

    ``!!js >-`` blocks put the expression on the following lines, so the value
    is everything after the key, joined until the next key at the same indent
    (or a deeper block that belongs to a different entry).
    """
    lines = _row_lines(row)
    pattern = re.compile(rf"^(\s*){re.escape(key)}:\s*(.*)$")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match is None:
            continue
        indent = len(match.group(1))
        value = match.group(2).strip()
        collected = [value] if value else []
        for follow in lines[index + 1 :]:
            follow_indent = len(follow) - len(follow.lstrip())
            if follow_indent <= indent and re.match(r"^\s*[\w.-]+:", follow):
                break
            collected.append(follow.strip())
        return " ".join(item for item in collected if item).strip()
    return ""


def _confirmed_mode(expression: str, requested: str) -> str | None:
    """Which mode does this Harness expression resolve to for our launch?

    Accepts a literal (``mode: workspace-write``) or an env-driven expression
    (``process.env.DSH_PERMISSION_MODE ?? 'workspace-write'``). For the latter
    the bridge *sets* that variable itself, so the requested mode is what the
    Harness will evaluate; the fallback default is returned as the second-best
    answer so the caller can still refuse a forbidden default.
    """
    text = " ".join(str(expression or "").split())
    if not text:
        return None
    literal = re.fullmatch(r"['\"]?([A-Za-z0-9_-]+)['\"]?", text)
    if literal:
        return literal.group(1)
    if "DSH_PERMISSION_MODE" not in text:
        return None
    fallback = re.search(r"\?\?\s*['\"]?([A-Za-z0-9_-]+)['\"]?", text)
    if fallback and fallback.group(1).casefold() in FORBIDDEN_SANDBOX_MODES:
        return fallback.group(1)
    return requested


def _workspace_bound(expression: str, workspace_root: Path) -> bool:
    """Is the sandbox bound to *our* snapshot directory?"""
    text = " ".join(str(expression or "").split())
    if not text:
        return False
    if "process.cwd()" in text:
        # The provider spawns the Harness with cwd == snapshot root, so the
        # expression resolves to the snapshot; the spawn is checked separately.
        return True
    quoted = text.strip("'\"")
    try:
        candidate = Path(quoted).expanduser().resolve()
    except OSError:
        return False
    return _is_within(workspace_root, candidate) and _is_within(candidate, workspace_root)


def _is_unconditional_never(expression: str) -> bool:
    text = " ".join(str(expression or "").split())
    if "DSH_PERMISSION_MODE" in text:
        return False
    stripped = text.strip("'\"").casefold()
    return stripped in {"never", "allow", "always"}


def _row_statically_disabled(row: str) -> bool:
    text = " ".join(str(row or "").split())
    if "disabled:" not in text:
        return False
    value = ""
    for line in str(row or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("disabled:"):
            value = stripped.split(":", 1)[1].strip()
            break
    return value.casefold() == "true"
