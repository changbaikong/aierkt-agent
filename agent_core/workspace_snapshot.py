"""Create fail-closed per-attempt copies of project workspaces."""

from __future__ import annotations

import hashlib
import difflib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .sandbox_contracts import _reject_reparse_points, _reject_special_path


class SnapshotValidationError(ValueError):
    """A project path or snapshot tree is unsafe."""


_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "dist",
        "build",
        "target",
        ".venv",
        ".sandbox",
        "venv",
        "env",
    }
)
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx"})
_TASK_DATABASE_NAMES = frozenset(
    {"work.db", "work.sqlite", "work_ledger.db", "agent.db", "tasks.db"}
)
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x00000400)
_PROTECTED_NAMES = frozenset(
    {
        "agents.md",
        "claude.md",
        "codex.md",
        "gemini.md",
        "copilot-instructions.md",
        "requirements.txt",
        "package.json",
        "pyproject.toml",
        "poetry.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "cargo.lock",
        "cargo.toml",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "uv.lock",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        "jenkinsfile",
        ".travis.yml",
        "appveyor.yml",
    }
)


@dataclass(frozen=True)
class WorkspaceDiff:
    patch: str
    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    binary_changes: tuple[str, ...] = ()
    protected_changes: tuple[str, ...] = ()
    total_bytes: int = 0
    current_hashes: dict[str, str] | None = None


@dataclass(frozen=True)
class WorkspaceSnapshot:
    snapshot_id: str
    attempt_id: str
    project_root: Path
    attempt_root: Path
    workspace_root: Path
    baseline_root: Path
    manifest_path: Path

    def diff(self) -> WorkspaceDiff:
        return _build_diff(self)

    def project_changes(self) -> tuple[str, ...]:
        """原项目相对**基线**的内容改动；正常情况下必须是空的。

        快照的全部意义是"任务期间原项目原封不动"，只有 Host 的审批链能改它。
        这里非空意味着有别的写入者绕过审批链动了原项目 —— 实测 DeepSeek Harness
        会拿任务文本里的绝对路径直接编辑原项目，而这一版沙箱既没拦、也没有向
        客户端发权限请求。发现这种情况必须让任务失败，而不是继续宣称"原项目未改动"。

        扫描沿用快照自己的排除规则（``__pycache__``、``.pytest_cache``、
        ``node_modules`` 等目录不算），所以"在原项目里跑了一次 pytest"不会误报，
        真正的内容增删改才会。
        """
        baseline = _scan_files(self.baseline_root)
        current = _scan_files(self.project_root)
        names = set(baseline) | set(current)
        return tuple(
            sorted(
                name
                for name in names
                if baseline.get(name, {}).get("sha256") != current.get(name, {}).get("sha256")
            )
        )


def _same_or_inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE
    )


def _excluded(relative: Path, *, directory: bool) -> bool:
    name = relative.name
    lowered = name.casefold()
    if directory:
        parts = tuple(part.casefold() for part in relative.parts)
        if parts == ("data", "memory_vectors"):
            return True
        return lowered in _EXCLUDED_DIRECTORIES
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if Path(lowered).suffix in _SECRET_SUFFIXES:
        return True
    return lowered in _TASK_DATABASE_NAMES


def _file_record(relative: Path, target: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {
        "path": relative.as_posix(),
        "type": "file",
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _scan_files(root: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    pending = [(root, Path())]
    while pending:
        directory, relative = pending.pop()
        for entry in os.scandir(directory):
            path = Path(entry.path)
            child = relative / entry.name
            if _is_reparse(path):
                raise SnapshotValidationError(
                    f"snapshot tree contains a reparse point: {child.as_posix()}"
                )
            if entry.is_dir(follow_symlinks=False):
                if _excluded(child, directory=True):
                    continue
                pending.append((path, child))
            elif entry.is_file(follow_symlinks=False):
                record = _file_record(child, path)
                record["absolute_path"] = path
                records[child.as_posix()] = record
            else:
                raise SnapshotValidationError(
                    f"snapshot tree contains a non-regular file: {child.as_posix()}"
                )
    return records


def _text_content(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _protected(path: str) -> bool:
    relative = Path(path)
    lowered = relative.name.casefold()
    parts = tuple(part.casefold() for part in relative.parts)
    return (
        lowered in _PROTECTED_NAMES
        or lowered == ".env"
        or lowered.startswith(".env.")
        or Path(lowered).suffix in _SECRET_SUFFIXES
        or (
            parts
            and parts[0]
            in {".github", ".gitlab", ".circleci", ".azure-pipelines", ".cursor"}
        )
    )


def _build_diff(snapshot: WorkspaceSnapshot) -> WorkspaceDiff:
    baseline = _scan_files(snapshot.baseline_root)
    current = _scan_files(snapshot.workspace_root)
    baseline_names = set(baseline)
    current_names = set(current)
    added = sorted(current_names - baseline_names)
    deleted = sorted(baseline_names - current_names)
    modified = sorted(
        name
        for name in baseline_names & current_names
        if baseline[name]["sha256"] != current[name]["sha256"]
    )
    changed = [*added, *modified, *deleted]
    binary: list[str] = []
    patches: list[str] = []
    for name in changed:
        old_path = baseline.get(name, {}).get("absolute_path")
        new_path = current.get(name, {}).get("absolute_path")
        old_text = _text_content(old_path) if isinstance(old_path, Path) else ""
        new_text = _text_content(new_path) if isinstance(new_path, Path) else ""
        if old_text is None or new_text is None:
            binary.append(name)
            continue
        from_file = f"a/{name}" if old_path is not None else "/dev/null"
        to_file = f"b/{name}" if new_path is not None else "/dev/null"
        patches.extend(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                new_text.splitlines(keepends=True),
                fromfile=from_file,
                tofile=to_file,
            )
        )
    protected = tuple(sorted(name for name in changed if _protected(name)))
    return WorkspaceDiff(
        patch="".join(patches),
        added=tuple(added),
        modified=tuple(modified),
        deleted=tuple(deleted),
        binary_changes=tuple(sorted(binary)),
        protected_changes=protected,
        total_bytes=sum(int(current[name]["size"]) for name in added + modified),
        current_hashes={name: str(item["sha256"]) for name, item in current.items()},
    )


class WorkspaceSnapshotService:
    """Safely copy allowed local projects into host-owned attempt directories."""

    def __init__(
        self,
        snapshot_root: str | Path,
        allowed_roots: Iterable[str | Path],
    ) -> None:
        self.snapshot_root = Path(snapshot_root).absolute()
        try:
            _reject_special_path(self.snapshot_root, "snapshot_root")
            _reject_reparse_points(self.snapshot_root, "snapshot_root")
        except Exception as exc:
            raise SnapshotValidationError(str(exc)) from exc
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.snapshot_root = self.snapshot_root.resolve(strict=True)
        self.state_root = self.snapshot_root / ".state"
        self.state_root.mkdir(exist_ok=True)

        roots: list[Path] = []
        for raw in allowed_roots:
            root = self._resolve_directory(Path(raw), "allowed root")
            roots.append(root)
        if not roots:
            raise SnapshotValidationError("at least one allowed root is required")
        self.allowed_roots = tuple(roots)

    @staticmethod
    def _key(attempt_id: str) -> str:
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise SnapshotValidationError("attempt_id is required")
        return hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _resolve_directory(path: Path, field: str) -> Path:
        try:
            _reject_special_path(path, field)
            _reject_reparse_points(path, field)
            resolved = path.resolve(strict=True)
        except Exception as exc:
            raise SnapshotValidationError(str(exc)) from exc
        if not resolved.is_dir():
            raise SnapshotValidationError(f"{field} must be a directory")
        return resolved

    def _paths(self, attempt_id: str) -> tuple[str, Path, Path]:
        key = self._key(attempt_id)
        return key, self.snapshot_root / f"attempt-{key}", self.state_root / f"{key}.json"

    def _validate_project(self, project_root: Path) -> Path:
        project = self._resolve_directory(project_root, "project_root")
        if not any(_same_or_inside(root, project) for root in self.allowed_roots):
            raise SnapshotValidationError("project_root must be inside an allowed root")
        return project

    def create(self, attempt_id: str, project_root: str | Path) -> WorkspaceSnapshot:
        project = self._validate_project(Path(project_root))
        key, attempt_root, manifest_path = self._paths(attempt_id)
        if attempt_root.exists() or manifest_path.exists():
            raise SnapshotValidationError("snapshot already exists for attempt")
        partial = self.snapshot_root / f".{key}.partial"
        if partial.exists():
            raise SnapshotValidationError("partial snapshot already exists")
        workspace = partial / "workspace"
        baseline = partial / "baseline"
        records: list[dict[str, Any]] = []
        partial.mkdir()
        workspace.mkdir()
        try:
            self._copy_tree(project, workspace, Path(), records)
            shutil.copytree(workspace, baseline)
            payload = {
                "version": 1,
                "snapshot_id": key,
                "attempt_id": attempt_id,
                "project_root": str(project),
                "files": sorted(records, key=lambda item: item["path"]),
            }
            temporary_manifest = self.state_root / f".{key}.json.tmp"
            temporary_manifest.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(partial, attempt_root)
            os.replace(temporary_manifest, manifest_path)
        except Exception:
            if partial.exists():
                shutil.rmtree(partial)
            temporary_manifest = self.state_root / f".{key}.json.tmp"
            temporary_manifest.unlink(missing_ok=True)
            raise
        return WorkspaceSnapshot(
            snapshot_id=key,
            attempt_id=attempt_id,
            project_root=project,
            attempt_root=attempt_root,
            workspace_root=attempt_root / "workspace",
            baseline_root=attempt_root / "baseline",
            manifest_path=manifest_path,
        )

    def load(self, attempt_id: str) -> WorkspaceSnapshot:
        key, attempt_root, manifest_path = self._paths(attempt_id)
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if payload.get("version") != 1:
                raise ValueError("unsupported version")
            if payload.get("snapshot_id") != key or payload.get("attempt_id") != attempt_id:
                raise ValueError("identity mismatch")
            project = self._validate_project(Path(payload["project_root"]))
            files = payload.get("files")
            if not isinstance(files, list):
                raise ValueError("files must be a list")
            for item in files:
                relative = Path(item["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("unsafe manifest path")
                if item.get("type") != "file" or not isinstance(item.get("sha256"), str):
                    raise ValueError("invalid file record")
            workspace = attempt_root / "workspace"
            baseline = attempt_root / "baseline"
            if not attempt_root.is_dir() or not workspace.is_dir() or not baseline.is_dir():
                raise ValueError("snapshot tree is missing")
            if _is_reparse(attempt_root) or _is_reparse(workspace) or _is_reparse(baseline):
                raise ValueError("snapshot tree contains a reparse point")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SnapshotValidationError(f"invalid snapshot manifest: {exc}") from exc
        return WorkspaceSnapshot(
            snapshot_id=key,
            attempt_id=attempt_id,
            project_root=project,
            attempt_root=attempt_root,
            workspace_root=workspace,
            baseline_root=baseline,
            manifest_path=manifest_path,
        )

    def _copy_tree(
        self,
        source: Path,
        destination: Path,
        relative: Path,
        records: list[dict[str, Any]],
    ) -> None:
        try:
            entries = list(os.scandir(source))
        except OSError as exc:
            raise SnapshotValidationError(f"cannot inspect project tree: {exc}") from exc
        for entry in entries:
            source_path = Path(entry.path)
            child_relative = relative / entry.name
            try:
                if _is_reparse(source_path):
                    raise SnapshotValidationError(
                        f"project tree contains a reparse point: {child_relative.as_posix()}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    if _excluded(child_relative, directory=True):
                        continue
                    target_dir = destination / entry.name
                    target_dir.mkdir()
                    self._copy_tree(source_path, target_dir, child_relative, records)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise SnapshotValidationError(
                        f"project tree contains a non-regular file: {child_relative.as_posix()}"
                    )
                if _excluded(child_relative, directory=False):
                    continue
                target = destination / entry.name
                shutil.copyfile(source_path, target, follow_symlinks=False)
                if _is_reparse(source_path):
                    raise SnapshotValidationError(
                        f"project tree changed to a reparse point: {child_relative.as_posix()}"
                    )
                records.append(_file_record(child_relative, target))
            except OSError as exc:
                raise SnapshotValidationError(
                    f"cannot copy project entry {child_relative.as_posix()}: {exc}"
                ) from exc

    def cleanup(self, attempt_id: str) -> None:
        _, attempt_root, manifest_path = self._paths(attempt_id)
        resolved_parent = attempt_root.parent.resolve(strict=True)
        if resolved_parent != self.snapshot_root:
            raise SnapshotValidationError("snapshot cleanup target escaped snapshot_root")
        if attempt_root.exists():
            if _is_reparse(attempt_root):
                raise SnapshotValidationError("snapshot cleanup target is a reparse point")
            shutil.rmtree(attempt_root)
        manifest_path.unlink(missing_ok=True)
