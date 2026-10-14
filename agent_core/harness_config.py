"""Configuration for the DeepSeek Harness ACP bridge.

Everything the bridge needs to start the installed Harness is read from the
environment, because that is where this project already keeps deployment
choices (``AGENT_WORK_DB``, ``AGENT_CORE_DB``, ``AGENT_WORK_ROOTS``). Defaults
are derived from the *installed* desktop app, so a machine with the Harness
installed needs no configuration at all.

Values that are security-relevant are deliberately restrictive:

* ``sandbox_mode`` defaults to ``workspace-write`` and ``danger-full-access`` is
  rejected at *configuration load* time, not at execution time — a task must
  never be able to talk the bridge into running the agent unsandboxed.
* ``min_version`` is compared against the CLI's own ``--version`` output, and a
  build whose ``--version`` cannot be read fails closed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

#: Sandbox modes the bridge refuses to run with, whatever the caller asks.
#: ``danger-full-access`` means "no sandbox and never ask": running there would
#: silently change the execution and permission semantics of every task.
FORBIDDEN_SANDBOX_MODES = frozenset({"danger-full-access", "none", "off", "disabled"})

#: Sandbox modes the bridge accepts by default.
DEFAULT_ALLOWED_SANDBOX_MODES = ("workspace-write", "read-only")

#: Relative path (from the install root) of the CLI entry the desktop shim runs.
DESKTOP_CLI_ENTRY = (
    "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
)

_VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+](.*))?$")


def _env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else str(value).strip()


def default_install_root() -> Path | None:
    """The installed desktop app, newest candidate first.

    ``LOCALAPPDATA`` is the documented install location for a per-user install;
    the environment override exists so a machine-wide install can point at it
    without editing code.
    """
    override = _env_text("AGENT_HARNESS_INSTALL_ROOT")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override))
    local_app_data = os.getenv("LOCALAPPDATA") or ""
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "DeepSeek Harness")
        candidates.append(Path(local_app_data) / "DeepSeek Harness")
    for candidate in candidates:
        if (candidate / "DeepSeek Harness.exe").is_file():
            return candidate
    return None


def default_executable() -> str:
    root = default_install_root()
    if root is None:
        return ""
    return str(root / "DeepSeek Harness.exe")


def default_acp_entry() -> str:
    root = default_install_root()
    if root is None:
        return ""
    return str(root / DESKTOP_CLI_ENTRY.replace("/", os.sep))


@dataclass(frozen=True)
class VersionRequirement:
    """Version floor with an explicit prerelease rule.

    The desktop app ships release candidates (``0.2.0-rc.2`` today). A plain
    semver comparison would call that *lower* than ``0.2.0`` and refuse to run a
    perfectly good build, so a prerelease of the required version is accepted
    when ``allow_prerelease`` is on. Turning it off is one env var away.
    """

    minimum: str = "0.2.0"
    allow_prerelease: bool = True

    def satisfied_by(self, version: str) -> bool:
        actual = parse_version(version)
        required = parse_version(self.minimum)
        if actual is None or required is None:
            return False
        actual_release, actual_pre = actual
        required_release, _required_pre = required
        if actual_release > required_release:
            return True
        if actual_release < required_release:
            return False
        if actual_pre and not self.allow_prerelease:
            return False
        return True

    def describe(self) -> str:
        suffix = "（允许预发布版）" if self.allow_prerelease else ""
        return f">={self.minimum}{suffix}"


def parse_version(text: str) -> tuple[tuple[int, int, int], str] | None:
    """``'0.2.0-rc.2'`` → ``((0, 2, 0), 'rc.2')``; unparsable → None."""
    match = _VERSION_PATTERN.match(str(text or "").strip())
    if match is None:
        return None
    major, minor, patch, prerelease = match.groups()
    return (int(major), int(minor), int(patch)), (prerelease or "")


@dataclass(frozen=True)
class HarnessTimeouts:
    preflight: int = 60
    initialize: int = 30
    session: int = 30
    prompt: int = 1800
    cancel: int = 20
    close: int = 10


@dataclass(frozen=True)
class HarnessConfig:
    enabled: bool = True
    executable: str = ""
    #: Arguments that come before the profile selection, e.g.
    #: ``--expose-internals <cli.js>``.
    entry_args: tuple[str, ...] = ()
    profile: str = "acp"
    extra_args: tuple[str, ...] = ()
    #: Env passed to every Harness process. Always wins over the inherited env.
    env: Mapping[str, str] = field(default_factory=dict)
    version_args: tuple[str, ...] = ("--version",)
    dump_config_args: tuple[str, ...] = ("--dump-config",)
    required_version: VersionRequirement = field(default_factory=VersionRequirement)
    protocol_version: int = 1
    required_session_capabilities: tuple[str, ...] = ("resume",)
    sandbox_mode: str = "workspace-write"
    allowed_sandbox_modes: tuple[str, ...] = DEFAULT_ALLOWED_SANDBOX_MODES
    sandbox_state_root: str = "./data/harness_sandbox_state"
    require_sandbox_token: bool = True
    #: Rows the *Harness' own* ``--dump-config`` must show for the sandbox to be
    #: considered confirmed. A build without these rows is refused.
    required_config_rows: tuple[str, ...] = ("sandbox-policy", "pwsh-sandbox", "approval")
    prompt_timeout: int = 1800
    timeouts: HarnessTimeouts = field(default_factory=HarnessTimeouts)
    #: Cap on tool-call titles kept in the activity projection (UI only).
    max_tool_label: int = 200
    #: Maximum bytes of agent message text kept in the result summary.
    max_message_chars: int = 4000

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "HarnessConfig":
        source = dict(os.environ if env is None else env)

        def text(name: str, default: str = "") -> str:
            value = source.get(name)
            return default if value is None else str(value).strip()

        def flag(name: str, default: bool) -> bool:
            raw = source.get(name)
            if raw is None or not str(raw).strip():
                return default
            return str(raw).strip().casefold() in {"1", "true", "yes", "on"}

        def integer(name: str, default: int) -> int:
            raw = source.get(name)
            if raw is None or not str(raw).strip():
                return default
            try:
                return max(1, int(float(str(raw).strip())))
            except (TypeError, ValueError):
                return default

        def items(name: str, default: Sequence[str]) -> tuple[str, ...]:
            raw = source.get(name)
            if raw is None or not str(raw).strip():
                return tuple(default)
            return tuple(item.strip() for item in str(raw).split(";") if item.strip())

        executable = text("AGENT_HARNESS_EXECUTABLE") or default_executable()
        entry = text("AGENT_HARNESS_ACP_ENTRY") or default_acp_entry()
        entry_args = items("AGENT_HARNESS_ACP_ENTRY_ARGS", ("--expose-internals",))
        if entry:
            entry_args = (*entry_args, entry)
        child_env = {
            "ELECTRON_RUN_AS_NODE": "1",
            "DSH_PERMISSION_MODE": text("AGENT_HARNESS_SANDBOX_MODE", "workspace-write")
            or "workspace-write",
        }
        dsh_home = text("AGENT_HARNESS_HOME")
        if dsh_home:
            child_env["DSH_HOME"] = dsh_home
        extra_env = text("AGENT_HARNESS_ENV")
        for pair in extra_env.split(";"):
            if "=" in pair:
                key, _, value = pair.partition("=")
                if key.strip():
                    child_env[key.strip()] = value.strip()

        return cls(
            enabled=flag("AGENT_HARNESS_ENABLED", True),
            executable=executable,
            entry_args=tuple(entry_args),
            profile=text("AGENT_HARNESS_PROFILE", "acp") or "acp",
            extra_args=items("AGENT_HARNESS_ACP_ARGS", ()),
            env=child_env,
            version_args=items("AGENT_HARNESS_VERSION_ARGS", ("--version",)),
            dump_config_args=items("AGENT_HARNESS_DUMP_ARGS", ("--dump-config",)),
            required_version=VersionRequirement(
                minimum=text("AGENT_HARNESS_MIN_VERSION", "0.2.0") or "0.2.0",
                allow_prerelease=flag("AGENT_HARNESS_ALLOW_PRERELEASE", True),
            ),
            protocol_version=integer("AGENT_HARNESS_PROTOCOL_VERSION", 1),
            required_session_capabilities=items(
                "AGENT_HARNESS_REQUIRED_SESSION_CAPS", ("resume",)
            ),
            sandbox_mode=text("AGENT_HARNESS_SANDBOX_MODE", "workspace-write")
            or "workspace-write",
            allowed_sandbox_modes=items(
                "AGENT_HARNESS_ALLOWED_SANDBOX_MODES", DEFAULT_ALLOWED_SANDBOX_MODES
            ),
            sandbox_state_root=text(
                "AGENT_HARNESS_SANDBOX_STATE_ROOT", "./data/harness_sandbox_state"
            )
            or "./data/harness_sandbox_state",
            require_sandbox_token=flag("AGENT_HARNESS_SANDBOX_TOKEN_REQUIRED", True),
            required_config_rows=items(
                "AGENT_HARNESS_REQUIRED_CONFIG_ROWS",
                ("sandbox-policy", "pwsh-sandbox", "approval"),
            ),
            prompt_timeout=integer("AGENT_HARNESS_PROMPT_TIMEOUT", 1800),
            timeouts=HarnessTimeouts(
                preflight=integer("AGENT_HARNESS_PREFLIGHT_TIMEOUT", 60),
                initialize=integer("AGENT_HARNESS_INIT_TIMEOUT", 30),
                session=integer("AGENT_HARNESS_SESSION_TIMEOUT", 30),
                prompt=integer("AGENT_HARNESS_PROMPT_TIMEOUT", 1800),
                cancel=integer("AGENT_HARNESS_CANCEL_TIMEOUT", 20),
                close=integer("AGENT_HARNESS_CLOSE_TIMEOUT", 10),
            ),
            max_message_chars=integer("AGENT_HARNESS_MAX_MESSAGE_CHARS", 4000),
        )

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #

    def validate(self) -> list[str]:
        """Return the reasons this configuration cannot be used (empty = ok).

        Returning *all* reasons is deliberate: "Harness 不可用" as a single
        opaque message is what makes these failures hard to diagnose.
        """
        problems: list[str] = []
        if not self.enabled:
            problems.append("DeepSeek Harness provider is disabled by configuration")
        if not str(self.executable).strip():
            problems.append(
                "Harness executable not found; set AGENT_HARNESS_EXECUTABLE "
                "(installed app: %LOCALAPPDATA%\\Programs\\DeepSeek Harness)"
            )
        mode = str(self.sandbox_mode).strip().casefold()
        if mode in FORBIDDEN_SANDBOX_MODES:
            problems.append(
                f"sandbox mode '{self.sandbox_mode}' is refused: the bridge never runs "
                "the agent unsandboxed"
            )
        allowed = {item.casefold() for item in self.allowed_sandbox_modes}
        if mode not in allowed:
            problems.append(f"sandbox mode '{self.sandbox_mode}' is not in {sorted(allowed)}")
        if self.env.get("DSH_PERMISSION_MODE", "").strip().casefold() in FORBIDDEN_SANDBOX_MODES:
            problems.append("DSH_PERMISSION_MODE asks for an unsandboxed run")
        return problems

    @property
    def sandbox_mode_value(self) -> str:
        return str(self.sandbox_mode).strip()

    def argv(self, *, profile_args: Sequence[str] = ()) -> tuple[str, ...]:
        """Full argument vector for one Harness process."""
        return (
            *self.entry_args,
            "--profile",
            self.profile,
            *profile_args,
            *self.extra_args,
        )
