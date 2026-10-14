"""Host-owned test configuration and snapshot-bound verification records."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .sandbox_contracts import SandboxCommand, _reject_reparse_points, _reject_special_path
from .workspace_snapshot import WorkspaceSnapshot


@dataclass(frozen=True)
class VerificationCommand:
    runtime: str
    args: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if self.runtime not in {"python", "powershell"}:
            raise ValueError("verification runtime must be python or powershell")
        if not self.args or any(not isinstance(item, str) or not item for item in self.args):
            raise ValueError("verification args must contain non-empty strings")
        relative = Path(self.cwd)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("verification cwd must stay inside the snapshot")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("verification timeout_seconds must be positive")


@dataclass(frozen=True)
class VerificationConfig:
    version: int
    projects: Mapping[str, tuple[VerificationCommand, ...]]

    @staticmethod
    def _project_key(path: Path) -> str:
        return os.path.normcase(str(path.resolve(strict=True)))

    def commands_for(self, project_root: Path) -> tuple[VerificationCommand, ...]:
        return tuple(self.projects.get(self._project_key(project_root), ()))

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        forbidden_roots: Iterable[str | Path] = (),
    ) -> "VerificationConfig":
        raw_path = Path(path).absolute()
        _reject_special_path(raw_path, "verification config")
        _reject_reparse_points(raw_path, "verification config")
        config_path = raw_path.resolve(strict=True)
        for raw_root in forbidden_roots:
            root = Path(raw_root).resolve(strict=True)
            try:
                config_path.relative_to(root)
            except ValueError:
                continue
            raise ValueError("verification config must be host-owned outside project roots")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"version", "projects"}:
            raise ValueError("verification config contains unknown or missing fields")
        if payload["version"] != 1 or not isinstance(payload["projects"], list):
            raise ValueError("unsupported verification config")
        projects: dict[str, tuple[VerificationCommand, ...]] = {}
        for raw_project in payload["projects"]:
            if not isinstance(raw_project, dict) or set(raw_project) != {"project_root", "commands"}:
                raise ValueError("verification project contains unknown or missing fields")
            root = Path(raw_project["project_root"]).resolve(strict=True)
            if not root.is_dir():
                raise ValueError("verification project_root must be a directory")
            raw_commands = raw_project["commands"]
            if not isinstance(raw_commands, list) or not raw_commands:
                raise ValueError("verification commands must be a non-empty list")
            commands: list[VerificationCommand] = []
            for raw in raw_commands:
                allowed = {"runtime", "args", "cwd", "timeout_seconds"}
                if not isinstance(raw, dict) or not set(raw).issubset(allowed):
                    raise ValueError("verification command contains unknown fields")
                if "runtime" not in raw or "args" not in raw or not isinstance(raw["args"], list):
                    raise ValueError("verification command requires runtime and args")
                commands.append(
                    VerificationCommand(
                        runtime=raw["runtime"],
                        args=tuple(raw["args"]),
                        cwd=str(raw.get("cwd", ".")),
                        timeout_seconds=cls._timeout(raw.get("timeout_seconds", 300)),
                    )
                )
            key = cls._project_key(root)
            if key in projects:
                raise ValueError("duplicate verification project_root")
            projects[key] = tuple(commands)
        return cls(version=1, projects=projects)

    @staticmethod
    def _timeout(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("verification timeout_seconds must be an integer")
        return value


@dataclass(frozen=True)
class VerificationRecord:
    status: str
    reason: str
    project_root: str
    before_fingerprint: str
    after_fingerprint: str
    commands: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def snapshot_fingerprint(snapshot: WorkspaceSnapshot) -> str:
    root = snapshot.workspace_root.resolve(strict=True)
    hashes: dict[str, str] = {}
    pending = [(root, Path())]
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
    while pending:
        directory, relative = pending.pop()
        for entry in os.scandir(directory):
            child_relative = relative / entry.name
            if not relative.parts and entry.name.casefold() == ".sandbox":
                continue
            info = entry.stat(follow_symlinks=False)
            if entry.is_symlink() or getattr(info, "st_file_attributes", 0) & reparse_attribute:
                raise ValueError(
                    f"verification workspace contains a reparse point: {child_relative.as_posix()}"
                )
            child = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                pending.append((child, child_relative))
                continue
            if not entry.is_file(follow_symlinks=False):
                raise ValueError(
                    f"verification workspace contains a non-regular file: {child_relative.as_posix()}"
                )
            digest = hashlib.sha256()
            with child.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            hashes[child_relative.as_posix()] = digest.hexdigest()
    encoded = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CodeAgentVerificationService:
    def __init__(
        self,
        *,
        config: VerificationConfig,
        sandbox_runner: Any,
        python_executable: Path,
        powershell_executable: Path,
    ) -> None:
        self.config = config
        self.sandbox_runner = sandbox_runner
        self.executables = {
            "python": Path(python_executable),
            "powershell": Path(powershell_executable),
        }

    async def verify(self, attempt_id: str, snapshot: WorkspaceSnapshot) -> VerificationRecord:
        before = snapshot_fingerprint(snapshot)
        commands = self.config.commands_for(snapshot.project_root)
        project_root = str(snapshot.project_root.resolve())
        if not commands:
            return VerificationRecord(
                status="not_run",
                reason="no_trusted_test_configuration",
                project_root=project_root,
                before_fingerprint=before,
                after_fingerprint=before,
            )

        evidence: list[Mapping[str, Any]] = []
        reason = "verified"
        for configured in commands:
            cwd = (snapshot.workspace_root / configured.cwd).resolve(strict=True)
            cwd.relative_to(snapshot.workspace_root.resolve(strict=True))
            executable = self.executables[configured.runtime]
            command = SandboxCommand(
                argv=(str(executable), *configured.args),
                cwd=cwd,
                timeout_seconds=configured.timeout_seconds,
            )
            result = await self.sandbox_runner.run(attempt_id, command)
            evidence.append(
                {
                    "runtime": configured.runtime,
                    "argv": list(command.argv),
                    "cwd": configured.cwd,
                    "timeout_seconds": configured.timeout_seconds,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "cancelled": result.cancelled,
                    "killed": result.killed,
                    "output_truncated": result.output_truncated,
                    "termination_reason": result.termination_reason,
                    "stdout": result.stdout[:4000],
                    "stderr": result.stderr[:4000],
                }
            )
            if snapshot_fingerprint(snapshot) != before:
                reason = "snapshot_changed_during_verification"
                break
            if result.timed_out:
                reason = "command_timed_out"
                break
            if result.cancelled:
                reason = "command_cancelled"
                break
            if result.output_truncated:
                reason = "evidence_incomplete"
                break
            if result.exit_code != 0 or result.killed:
                reason = "command_failed"
                break

        after = snapshot_fingerprint(snapshot)
        if after != before:
            reason = "snapshot_changed_during_verification"
        status = "verified" if reason == "verified" else "unverified"
        return VerificationRecord(
            status=status,
            reason=reason,
            project_root=project_root,
            before_fingerprint=before,
            after_fingerprint=after,
            commands=tuple(evidence),
        )
