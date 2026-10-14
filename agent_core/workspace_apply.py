"""Prepare immutable, host-owned change sets for later safe workspace application."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import stat
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .actions import ActionGateway
from .code_agent_verification import VerificationRecord, snapshot_fingerprint
from .contracts import ActionRequest, Scope, ToolSpec
from .tools import DefaultRiskPolicy, ToolRegistry
from .workspace_snapshot import WorkspaceSnapshot


_TEXT_SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".kts",
        ".md",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".rst",
        ".scala",
        ".swift",
        ".ts",
        ".tsx",
        ".txt",
    }
)
_PROTECTED_NAMES = frozenset(
    {
        "agents.md",
        "claude.md",
        "codex.md",
        "gemini.md",
        "copilot-instructions.md",
        "package.json",
        "dockerfile",
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
        "makefile",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "pyproject.toml",
        "requirements.txt",
        "yarn.lock",
        "uv.lock",
        ".gitlab-ci.yml",
        "azure-pipelines.yml",
        "jenkinsfile",
        ".travis.yml",
        "appveyor.yml",
    }
)
_CONFIG_SUFFIXES = frozenset({".cfg", ".ini", ".json", ".toml", ".yaml", ".yml"})
_APPROVABLE_REASONS = frozenset({"protected_file", "unsupported_file_type", "deletion"})


@dataclass(frozen=True)
class AutoApplyPolicy:
    max_files: int = 20
    max_changed_lines: int = 1000
    max_content_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_files", "max_changed_lines", "max_content_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class PreparedChange:
    path: str
    operation: str
    baseline_hash: str | None
    proposed_hash: str | None
    content: bytes | None
    changed_lines: int


@dataclass(frozen=True)
class PreparedChangeSet:
    project_root: Path
    snapshot_id: str
    content_fingerprint: str
    changes: tuple[PreparedChange, ...]
    eligible: bool
    reasons: tuple[str, ...]
    total_changed_lines: int
    total_content_bytes: int


class WorkspaceLockUnavailable(RuntimeError):
    """Another process or service instance owns the project apply lock."""


class ConcurrentTargetChange(RuntimeError):
    """The target path changed at the atomic replacement boundary."""


class ApplyCancelled(RuntimeError):
    """The caller cancelled an in-progress workspace transaction."""


@dataclass(frozen=True)
class ApplyResult:
    status: str
    reason: str
    transaction_dir: Path


_PROCESS_LOCK_GUARD = threading.Lock()
_PROCESS_LOCKS: set[str] = set()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _security_descriptor(path: Path) -> str | None:
    if os.name != "nt":
        return None
    import win32security

    information = win32security.DACL_SECURITY_INFORMATION
    descriptor = win32security.GetFileSecurity(str(path), information)
    return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
        descriptor, win32security.SDDL_REVISION_1, information
    )


def _restore_security_descriptor(path: Path, value: str | None) -> None:
    if os.name != "nt" or value is None:
        return
    import win32security

    descriptor = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
        value, win32security.SDDL_REVISION_1
    )
    win32security.SetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION, descriptor
    )


def _decode_text(data: bytes) -> str | None:
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _changed_lines(old: str, new: str) -> int:
    count = 0
    for line in difflib.ndiff(old.splitlines(), new.splitlines()):
        if line.startswith("+ ") or line.startswith("- "):
            count += 1
    return count


def _verified_commands(record: VerificationRecord) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        (
            item.get("runtime"), tuple(item.get("argv") or ()),
            item.get("cwd"), item.get("timeout_seconds"),
        )
        for item in record.commands
    )


def _protected(path: str) -> bool:
    relative = Path(path)
    lowered_parts = tuple(part.casefold() for part in relative.parts)
    name = relative.name.casefold()
    return (
        name in _PROTECTED_NAMES
        or name == ".env"
        or name.startswith(".env.")
        or (
            lowered_parts
            and lowered_parts[0]
            in {".github", ".gitlab", ".circleci", ".azure-pipelines", ".cursor"}
        )
        or (
            len(lowered_parts) >= 2
            and lowered_parts[0] in {"scripts", "bin"}
            and relative.stem.casefold().startswith(("start", "launch", "bootstrap"))
        )
    )


def _supported_text_path(path: str) -> bool:
    relative = Path(path)
    suffix = relative.suffix.casefold()
    if suffix in _CONFIG_SUFFIXES:
        return False
    return suffix in _TEXT_SOURCE_SUFFIXES


def prepare_change_set(
    snapshot: WorkspaceSnapshot,
    verification: VerificationRecord | None,
    policy: AutoApplyPolicy,
) -> PreparedChangeSet:
    fingerprint = snapshot_fingerprint(snapshot)
    reasons: set[str] = set()
    if verification is None or verification.status != "verified":
        reasons.add("unverified")
    elif verification.after_fingerprint != fingerprint:
        reasons.add("verification_mismatch")

    diff = snapshot.diff()
    changes: list[PreparedChange] = []
    baseline_hashes = {
        name: _sha256((snapshot.baseline_root / Path(name)).read_bytes())
        for name in (*diff.modified, *diff.deleted)
    }
    for name in sorted((*diff.added, *diff.modified, *diff.deleted)):
        if name in diff.deleted:
            reasons.add("deletion")
            changes.append(
                PreparedChange(name, "delete", baseline_hashes[name], None, None, 0)
            )
            continue
        target = snapshot.workspace_root / Path(name)
        content = target.read_bytes()
        new_text = _decode_text(content)
        old_content = (
            (snapshot.baseline_root / Path(name)).read_bytes()
            if name in diff.modified
            else b""
        )
        old_text = _decode_text(old_content)
        if new_text is None or old_text is None:
            reasons.add("binary_change")
            changed_lines = 0
        else:
            changed_lines = _changed_lines(old_text, new_text)
        if _protected(name):
            reasons.add("protected_file")
        elif not _supported_text_path(name):
            reasons.add("unsupported_file_type")
        changes.append(
            PreparedChange(
                path=name,
                operation="modify" if name in diff.modified else "add",
                baseline_hash=baseline_hashes.get(name),
                proposed_hash=_sha256(content),
                content=content,
                changed_lines=changed_lines,
            )
        )

    total_lines = sum(item.changed_lines for item in changes)
    total_bytes = sum(len(item.content or b"") for item in changes)
    if not changes:
        reasons.add("no_changes")
    if len(changes) > policy.max_files:
        reasons.add("file_limit")
    if total_lines > policy.max_changed_lines:
        reasons.add("line_limit")
    if total_bytes > policy.max_content_bytes:
        reasons.add("content_limit")
    if snapshot_fingerprint(snapshot) != fingerprint:
        reasons.add("snapshot_changed_during_preparation")
    return PreparedChangeSet(
        project_root=snapshot.project_root,
        snapshot_id=snapshot.snapshot_id,
        content_fingerprint=fingerprint,
        changes=tuple(changes),
        eligible=not reasons,
        reasons=tuple(sorted(reasons)),
        total_changed_lines=total_lines,
        total_content_bytes=total_bytes,
    )


class WorkspaceApplyService:
    """Apply prepared contents with conflict checks and durable recovery state."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        replace_file: Callable[[Path, Path], None] | None = None,
        forbidden_roots: Iterable[str | Path] = (),
    ) -> None:
        self.state_root = Path(state_root).absolute().resolve()
        for raw_root in forbidden_roots:
            root = Path(raw_root).resolve(strict=True)
            if self.state_root.is_relative_to(root):
                raise ValueError("apply state must be outside allowed project roots")
        self.lock_root = self.state_root / "locks"
        self.transaction_root = self.state_root / "transactions"
        self.lock_root.mkdir(parents=True, exist_ok=True)
        self.transaction_root.mkdir(parents=True, exist_ok=True)
        self._replace_file = replace_file

    @staticmethod
    def _project_key(project_root: Path) -> str:
        normalized = os.path.normcase(str(project_root.resolve(strict=True)))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @contextmanager
    def project_lock(self, project_root: Path) -> Iterator[None]:
        key = self._project_key(project_root)
        lock_path = self.lock_root / f"{key}.lock"
        handle = lock_path.open("a+b")
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        with _PROCESS_LOCK_GUARD:
            if key in _PROCESS_LOCKS:
                handle.close()
                raise WorkspaceLockUnavailable("project apply lock is already held")
            _PROCESS_LOCKS.add(key)
        locked = False
        try:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                raise WorkspaceLockUnavailable("project apply lock is already held") from exc
            yield
        finally:
            if locked:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()
            with _PROCESS_LOCK_GUARD:
                _PROCESS_LOCKS.discard(key)

    def apply(
        self,
        change_set: PreparedChangeSet,
        *,
        approval_granted: bool = False,
        cancelled: Callable[[], bool] | None = None,
    ) -> ApplyResult:
        if not change_set.eligible and approval_granted and not set(change_set.reasons).issubset(
            _APPROVABLE_REASONS
        ):
            return ApplyResult("unsafe_to_apply", ",".join(change_set.reasons), Path())
        if not change_set.eligible and not approval_granted:
            return ApplyResult("approval_required", ",".join(change_set.reasons), Path())
        with self.project_lock(change_set.project_root):
            return self._apply_locked(change_set, cancelled=cancelled)

    def _apply_locked(
        self,
        change_set: PreparedChangeSet,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> ApplyResult:
        if cancelled is not None and cancelled():
            return ApplyResult("cancelled", "apply_cancelled", Path())
        pending = self._pending_recovery(change_set.project_root)
        if pending is not None:
            return ApplyResult("recovery_required", pending, Path())
        conflict = self._preflight(change_set)
        if conflict is not None:
            return ApplyResult("conflict", conflict, Path())
        transaction_dir, metadata = self._create_transaction(change_set)
        metadata["status"] = "applying"
        self._write_metadata(transaction_dir, metadata)
        try:
            for entry in metadata["changes"]:
                if cancelled is not None and cancelled():
                    raise ApplyCancelled("workspace apply cancelled")
                self._apply_entry(change_set.project_root, transaction_dir, entry, metadata)
            metadata["status"] = "applied"
            self._write_metadata(transaction_dir, metadata)
            return ApplyResult("applied", "applied", transaction_dir)
        except Exception as exc:
            metadata["failure"] = f"{type(exc).__name__}: {exc}"
            self._write_metadata(transaction_dir, metadata)
            recovered = self._recover_locked(transaction_dir, metadata)
            if isinstance(exc, ConcurrentTargetChange) and recovered == "recovered":
                return ApplyResult("conflict", "concurrent_target_change", transaction_dir)
            if isinstance(exc, ApplyCancelled):
                return ApplyResult(recovered, "apply_cancelled", transaction_dir)
            return ApplyResult(recovered, "apply_failed", transaction_dir)

    def _pending_recovery(self, project_root: Path) -> str | None:
        key = self._project_key(project_root)
        for directory in self.transaction_root.iterdir():
            if not directory.is_dir():
                continue
            metadata_path = directory / "transaction.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                recorded_root = Path(metadata["project_root"])
                status = metadata["status"]
                if not isinstance(status, str):
                    raise ValueError("invalid recovery status")
                recorded_key = self._project_key(recorded_root)
            except (OSError, ValueError, KeyError, TypeError):
                return "recovery_record_unreadable"
            if recorded_key == key and status in {
                "prepared", "applying", "recovering", "manual_recovery_required"
            }:
                return status
        return None

    def recover_pending(self) -> list[ApplyResult]:
        results: list[ApplyResult] = []
        for transaction_dir in sorted(self.transaction_root.iterdir()):
            metadata_path = transaction_dir / "transaction.json"
            if not transaction_dir.is_dir() or not metadata_path.is_file():
                continue
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("status") not in {"prepared", "applying", "recovering"}:
                continue
            project_root = Path(metadata["project_root"])
            with self.project_lock(project_root):
                status = self._recover_locked(transaction_dir, metadata)
            results.append(ApplyResult(status, "restart_recovery", transaction_dir))
        return results

    def recover_transaction(self, transaction_dir: str | Path) -> ApplyResult:
        directory = Path(transaction_dir).resolve(strict=True)
        directory.relative_to(self.transaction_root.resolve(strict=True))
        metadata = json.loads((directory / "transaction.json").read_text(encoding="utf-8"))
        project_root = Path(metadata["project_root"])
        with self.project_lock(project_root):
            status = self._recover_locked(directory, metadata)
        return ApplyResult(status, "explicit_recovery", directory)

    def _recover_transaction_locked(self, transaction_dir: Path) -> ApplyResult:
        metadata = json.loads(
            (transaction_dir / "transaction.json").read_text(encoding="utf-8")
        )
        status = self._recover_locked(transaction_dir, metadata)
        return ApplyResult(status, "explicit_recovery", transaction_dir)

    def _preflight(self, change_set: PreparedChangeSet) -> str | None:
        root = change_set.project_root.resolve(strict=True)
        for change in change_set.changes:
            relative = Path(change.path)
            if relative.is_absolute() or ".." in relative.parts:
                return "unsafe_path"
            target = root / relative
            existing_parent = target.parent
            while not existing_parent.exists() and existing_parent != root:
                existing_parent = existing_parent.parent
            try:
                existing_parent.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                return "unsafe_path"
            try:
                for component in self._identity_chain(root, existing_parent):
                    path = root / Path(component["path"])
                    info = path.lstat()
                    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
                        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                    ):
                        return "reparse_point"
                for component in (target if target.exists() else existing_parent,):
                    info = component.lstat()
                    if component.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
                        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                    ):
                        return "reparse_point"
            except OSError:
                return "unsafe_path"
            if change.operation == "add":
                if target.exists() or target.is_symlink():
                    return "new_file_exists"
                continue
            if not target.is_file():
                return "baseline_missing"
            info = target.stat()
            if info.st_nlink != 1:
                return "hard_link_target"
            if _sha256(target.read_bytes()) != change.baseline_hash:
                return "baseline_mismatch"
        return None

    @staticmethod
    def _identity_chain(root: Path, target: Path) -> list[dict[str, Any]]:
        relative = target.relative_to(root)
        paths = [root]
        current = root
        for part in relative.parts:
            current = current / part
            paths.append(current)
        return [
            {
                "path": path.relative_to(root).as_posix(),
                "device": path.stat().st_dev,
                "inode": path.stat().st_ino,
            }
            for path in paths
        ]

    def _create_transaction(
        self, change_set: PreparedChangeSet
    ) -> tuple[Path, dict[str, Any]]:
        transaction_dir = self.transaction_root / uuid.uuid4().hex
        originals = transaction_dir / "originals"
        proposed = transaction_dir / "proposed"
        originals.mkdir(parents=True)
        proposed.mkdir()
        entries: list[dict[str, Any]] = []
        root = change_set.project_root.resolve(strict=True)
        for index, change in enumerate(change_set.changes):
            target = root / Path(change.path)
            target_info = target.stat() if target.exists() else None
            parent_anchor = target.parent
            while not parent_anchor.exists() and parent_anchor != root:
                parent_anchor = parent_anchor.parent
            parent_info = parent_anchor.stat()
            entry = {
                "index": index,
                "path": change.path,
                "operation": change.operation,
                "baseline_hash": change.baseline_hash,
                "proposed_hash": change.proposed_hash,
                "mode": target_info.st_mode if target_info else None,
                "device": target_info.st_dev if target_info else None,
                "inode": target_info.st_ino if target_info else None,
                "parent_anchor": parent_anchor.relative_to(root).as_posix(),
                "parent_device": parent_info.st_dev,
                "parent_inode": parent_info.st_ino,
                "parent_chain": self._identity_chain(root, parent_anchor),
                "security_descriptor": _security_descriptor(target) if target_info else None,
                "applied": False,
                "created_directories": [],
            }
            if change.operation in {"modify", "delete"}:
                backup = originals / Path(change.path)
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(target.read_bytes())
                self._flush_file(backup)
            if change.content is not None:
                proposal = proposed / Path(change.path)
                proposal.parent.mkdir(parents=True, exist_ok=True)
                proposal.write_bytes(change.content)
                self._flush_file(proposal)
            entries.append(entry)
        metadata = {
            "version": 1,
            "status": "prepared",
            "project_root": str(root),
            "snapshot_id": change_set.snapshot_id,
            "content_fingerprint": change_set.content_fingerprint,
            "changes": entries,
        }
        self._write_metadata(transaction_dir, metadata)
        return transaction_dir, metadata

    def _apply_entry(
        self,
        project_root: Path,
        transaction_dir: Path,
        entry: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        target = project_root / Path(entry["path"])
        for identity in entry.get("parent_chain") or []:
            component = project_root / Path(identity["path"])
            info = component.stat()
            if (
                info.st_dev != identity["device"]
                or info.st_ino != identity["inode"]
                or component.is_symlink()
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            ):
                raise RuntimeError("parent path changed after preflight")
        parent_anchor = project_root / Path(entry["parent_anchor"])
        parent_info = parent_anchor.stat()
        if (
            parent_info.st_dev != entry["parent_device"]
            or parent_info.st_ino != entry["parent_inode"]
            or parent_anchor.is_symlink()
            or getattr(parent_info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise RuntimeError("parent directory changed after preflight")
        missing: list[Path] = []
        parent = target.parent
        while not parent.exists() and parent != project_root:
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir()
            entry["created_directories"].append(
                directory.relative_to(project_root).as_posix()
            )
        self._write_metadata(transaction_dir, metadata)
        with self._identity_guard(project_root, target, entry):
            if entry["operation"] in {"modify", "delete"}:
                info = target.stat()
                if (
                    info.st_dev != entry["device"]
                    or info.st_ino != entry["inode"]
                    or info.st_nlink != 1
                    or _sha256(target.read_bytes()) != entry["baseline_hash"]
                ):
                    raise RuntimeError("target changed after preflight")
            elif target.exists() or target.is_symlink():
                raise RuntimeError("new target appeared after preflight")
            if entry["operation"] == "delete":
                target.unlink()
                entry["applied"] = True
                self._write_metadata(transaction_dir, metadata)
                return
            proposal = transaction_dir / "proposed" / Path(entry["path"])
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".apply.tmp", dir=target.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(proposal.read_bytes())
                    handle.flush()
                    os.fsync(handle.fileno())
                if entry["mode"] is not None:
                    os.chmod(temporary, entry["mode"])
                if self._replace_file is not None:
                    self._replace_file(temporary, target)
                elif entry["operation"] == "add":
                    os.rename(temporary, target)
                elif os.name == "nt":
                    self._replace_windows_checked(
                        temporary, target, entry, transaction_dir, metadata
                    )
                else:
                    os.replace(temporary, target)
                _restore_security_descriptor(target, entry.get("security_descriptor"))
            finally:
                temporary.unlink(missing_ok=True)
        entry["applied"] = True
        self._write_metadata(transaction_dir, metadata)

    def _replace_windows_checked(
        self,
        temporary: Path,
        target: Path,
        entry: dict[str, Any],
        transaction_dir: Path,
        metadata: dict[str, Any],
    ) -> None:
        import win32file

        backup = target.parent / f".{target.name}.{uuid.uuid4().hex}.replace.bak"
        entry["replacement_backup"] = str(backup)
        self._write_metadata(transaction_dir, metadata)
        win32file.ReplaceFile(
            str(temporary), str(target), str(backup), 0, None, None
        )
        if _sha256(backup.read_bytes()) != entry["baseline_hash"]:
            raise ConcurrentTargetChange("target changed during atomic replacement")
        backup.unlink()
        entry["replacement_backup"] = None
        self._write_metadata(transaction_dir, metadata)

    @contextmanager
    def _identity_guard(
        self, root: Path, target: Path, entry: dict[str, Any]
    ) -> Iterator[None]:
        if os.name != "nt":
            yield
            return
        import win32con
        import win32file

        handles: list[Any] = []
        try:
            for identity in entry.get("parent_chain") or []:
                component = root / Path(identity["path"])
                handle = win32file.CreateFile(
                    str(component), 0,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                    None, win32con.OPEN_EXISTING,
                    win32con.FILE_FLAG_BACKUP_SEMANTICS
                    | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
                handles.append(handle)
                information = win32file.GetFileInformationByHandle(handle)
                inode = (information[8] << 32) | information[9]
                if inode != identity["inode"]:
                    raise RuntimeError("parent identity changed before apply")
            if entry["operation"] in {"modify", "delete"} and self._replace_file is None:
                handle = win32file.CreateFile(
                    str(target), win32con.GENERIC_READ,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_DELETE,
                    None, win32con.OPEN_EXISTING,
                    win32file.FILE_FLAG_OPEN_REPARSE_POINT, None,
                )
                handles.append(handle)
                information = win32file.GetFileInformationByHandle(handle)
                inode = (information[8] << 32) | information[9]
                if inode != entry["inode"] or information[7] != 1:
                    raise RuntimeError("target identity changed before apply")
            yield
        finally:
            for handle in reversed(handles):
                handle.Close()

    def _recover_locked(self, transaction_dir: Path, metadata: dict[str, Any]) -> str:
        metadata["status"] = "recovering"
        self._write_metadata(transaction_dir, metadata)
        root = Path(metadata["project_root"]).resolve(strict=True)
        manual = False
        for entry in reversed(metadata["changes"]):
            target = root / Path(entry["path"])
            if not self._recovery_path_safe(root, target, entry):
                manual = True
                continue
            current_hash = _sha256(target.read_bytes()) if target.is_file() else None
            replacement_backup_value = entry.get("replacement_backup")
            replacement_backup = (
                Path(replacement_backup_value) if replacement_backup_value else None
            )
            if replacement_backup is not None and replacement_backup.is_file():
                backup_hash = _sha256(replacement_backup.read_bytes())
                if current_hash == entry["proposed_hash"]:
                    backup_mode = replacement_backup.stat().st_mode
                    backup_security = _security_descriptor(replacement_backup)
                    if not self._atomic_restore(
                        replacement_backup,
                        target,
                        backup_mode,
                        backup_security,
                        expected_hash=entry["proposed_hash"],
                    ):
                        manual = True
                        continue
                elif current_hash != backup_hash:
                    manual = True
                    continue
                replacement_backup.unlink(missing_ok=True)
                entry["replacement_backup"] = None
                self._write_metadata(transaction_dir, metadata)
                for relative in reversed(entry.get("created_directories") or []):
                    directory = root / Path(relative)
                    try:
                        directory.rmdir()
                    except OSError:
                        pass
                continue
            if entry["operation"] == "modify":
                if current_hash == entry["baseline_hash"]:
                    pass
                elif current_hash == entry["proposed_hash"]:
                    backup = transaction_dir / "originals" / Path(entry["path"])
                    if not self._atomic_restore(
                        backup,
                        target,
                        entry["mode"],
                        entry.get("security_descriptor"),
                        expected_hash=entry["proposed_hash"],
                    ):
                        manual = True
                        continue
                else:
                    manual = True
                    continue
            elif entry["operation"] == "delete":
                if current_hash == entry["baseline_hash"]:
                    pass
                elif current_hash is None:
                    backup = transaction_dir / "originals" / Path(entry["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not self._atomic_restore(
                        backup,
                        target,
                        entry["mode"],
                        entry.get("security_descriptor"),
                        expected_hash=None,
                    ):
                        manual = True
                        continue
                else:
                    manual = True
                    continue
            else:
                if current_hash is None:
                    pass
                elif current_hash == entry["proposed_hash"]:
                    target.unlink()
                else:
                    manual = True
                    continue
            for relative in reversed(entry.get("created_directories") or []):
                directory = root / Path(relative)
                try:
                    directory.rmdir()
                except OSError:
                    pass
        metadata["status"] = "manual_recovery_required" if manual else "recovered"
        self._write_metadata(transaction_dir, metadata)
        return metadata["status"]

    @staticmethod
    def _recovery_path_safe(root: Path, target: Path, entry: dict[str, Any]) -> bool:
        try:
            for identity in entry.get("parent_chain") or []:
                component = root / Path(identity["path"])
                info = component.lstat()
                if info.st_dev != identity["device"] or info.st_ino != identity["inode"]:
                    return False
            current = root
            for part in target.relative_to(root).parts:
                current = current / part
                if not current.exists() and not current.is_symlink():
                    continue
                info = current.lstat()
                if current.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                ):
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _atomic_restore(
        self,
        source: Path,
        target: Path,
        mode: int | None,
        security_descriptor: str | None,
        *,
        expected_hash: str | None,
    ) -> bool:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".restore.tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(temporary, mode)
            if expected_hash is None:
                try:
                    os.rename(temporary, target)
                except FileExistsError:
                    return False
            elif os.name == "nt":
                import win32con
                import win32file

                handle = win32file.CreateFile(
                    str(target), win32con.GENERIC_READ,
                    win32con.FILE_SHARE_READ | win32con.FILE_SHARE_DELETE,
                    None, win32con.OPEN_EXISTING,
                    win32file.FILE_FLAG_OPEN_REPARSE_POINT, None,
                )
                try:
                    if _sha256(target.read_bytes()) != expected_hash:
                        return False
                    replaced = target.parent / (
                        f".{target.name}.{uuid.uuid4().hex}.restore.bak"
                    )
                    win32file.ReplaceFile(
                        str(temporary), str(target), str(replaced), 0, None, None
                    )
                    if _sha256(replaced.read_bytes()) != expected_hash:
                        win32file.ReplaceFile(
                            str(replaced), str(target), None, 0, None, None
                        )
                        return False
                    replaced.unlink(missing_ok=True)
                finally:
                    handle.Close()
            else:
                if _sha256(target.read_bytes()) != expected_hash:
                    return False
                os.replace(temporary, target)
            _restore_security_descriptor(target, security_descriptor)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _flush_file(path: Path) -> None:
        with path.open("rb+") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _write_metadata(transaction_dir: Path, metadata: dict[str, Any]) -> None:
        target = transaction_dir / "transaction.json"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".transaction.", suffix=".tmp", dir=transaction_dir
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(metadata, handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


class WorkspaceApplyActionGateway:
    """Internal ActionGateway facade; no model receives these tool schemas."""

    def __init__(self, service: WorkspaceApplyService) -> None:
        self.service = service
        self._pending: dict[str, tuple[str, Any]] = {}
        registry = ToolRegistry()
        common = {
            "effect": "persistent",
            "risk": "low",
            "visibility": "internal",
            "allowed_origins": frozenset({"work_host"}),
            "capability_id": "code_agent",
        }
        registry.register(
            ToolSpec(
                "code__workspace_apply",
                self._schema("code__workspace_apply"),
                self._execute_apply,
                **common,
            )
        )
        registry.register(
            ToolSpec(
                "code__workspace_recover",
                self._schema("code__workspace_recover"),
                self._execute_recover,
                **common,
            )
        )
        self.gateway = ActionGateway.standalone(
            registry, risk_policy=DefaultRiskPolicy()
        )

    @staticmethod
    def _schema(name: str) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "operation_token": {"type": "string", "minLength": 1}
                    },
                    "required": ["operation_token"],
                    "additionalProperties": False,
                },
            },
        }

    def _execute_apply(self, arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        token = str(arguments["operation_token"])
        kind, payload = self._pending[token]
        if kind != "apply":
            raise ValueError("operation token is not an apply request")
        change_set, cancelled = payload
        result = self.service._apply_locked(change_set, cancelled=cancelled)
        return {
            "status": result.status,
            "reason": result.reason,
            "transaction_dir": str(result.transaction_dir),
        }

    def _execute_recover(self, arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        token = str(arguments["operation_token"])
        kind, payload = self._pending[token]
        if kind != "recover":
            raise ValueError("operation token is not a recovery request")
        result = self.service._recover_transaction_locked(payload)
        return {
            "status": result.status,
            "reason": result.reason,
            "transaction_dir": str(result.transaction_dir),
        }

    async def apply(
        self,
        change_set: PreparedChangeSet,
        *,
        scope: Scope | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ApplyResult:
        return await self._invoke("apply", (change_set, cancelled), scope=scope)

    async def recover(
        self, transaction_dir: Path, *, scope: Scope | None = None
    ) -> ApplyResult:
        return await self._invoke("recover", transaction_dir, scope=scope)

    async def _invoke(self, kind: str, payload: Any, *, scope: Scope | None) -> ApplyResult:
        token = uuid.uuid4().hex
        self._pending[token] = (kind, payload)
        name = "code__workspace_apply" if kind == "apply" else "code__workspace_recover"
        action = ActionRequest(
            action_id=f"{name}-{token}",
            name=name,
            arguments={"operation_token": token},
            scope=scope or Scope("system", "work", "code-agent-apply", "work"),
            origin="work_host",
            run_id=token,
            turn_id=token,
            tool_call_id=token,
        )
        try:
            observation = await self.gateway.invoke(action)
        finally:
            self._pending.pop(token, None)
        if observation.status != "completed":
            return ApplyResult(
                "failed",
                observation.error or observation.error_kind or "action_gateway_rejected",
                Path(),
            )
        output = dict(observation.output or {})
        return ApplyResult(
            str(output.get("status") or "failed"),
            str(output.get("reason") or "action_gateway_failed"),
            Path(str(output.get("transaction_dir") or "")),
        )


class CodeAgentAutoApplyCoordinator:
    """Join trusted verification, policy, apply, post-check, and recovery."""

    def __init__(
        self,
        *,
        enabled: bool,
        snapshot_service: Any,
        sandbox_runner: Any,
        verification_service: Any,
        apply_service: WorkspaceApplyService,
        policy: AutoApplyPolicy,
        action_gateway: WorkspaceApplyActionGateway | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.snapshot_service = snapshot_service
        self.sandbox_runner = sandbox_runner
        self.verification_service = verification_service
        self.apply_service = apply_service
        self.policy = policy
        self.action_gateway = action_gateway or WorkspaceApplyActionGateway(apply_service)

    async def process(
        self,
        attempt_id: str,
        snapshot: WorkspaceSnapshot,
        verification: VerificationRecord,
        *,
        approval_granted: bool = False,
        expected_fingerprint: str | None = None,
        scope: Scope | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        current_fingerprint = snapshot_fingerprint(snapshot)
        if approval_granted and expected_fingerprint != current_fingerprint:
            return {
                "status": "approval_required",
                "reason": "content_changed_since_approval",
                "risk": {"content_fingerprint": current_fingerprint},
            }
        change_set = prepare_change_set(snapshot, verification, self.policy)
        risk = {
            "eligible": change_set.eligible,
            "reasons": list(change_set.reasons),
            "files": len(change_set.changes),
            "changed_lines": change_set.total_changed_lines,
            "content_bytes": change_set.total_content_bytes,
            "content_fingerprint": change_set.content_fingerprint,
        }
        if not self.enabled:
            return {"status": "patch_only", "risk": risk}
        if not change_set.eligible and not approval_granted:
            return {"status": "approval_required", "risk": risk}
        if not change_set.eligible and not set(change_set.reasons).issubset(
            _APPROVABLE_REASONS
        ):
            return {"status": "unsafe_to_apply", "reason": ",".join(change_set.reasons), "risk": risk}

        with self.apply_service.project_lock(change_set.project_root):
            applied = await self.action_gateway.apply(
                change_set, scope=scope, cancelled=cancelled
            )
            if applied.status != "applied":
                return {
                    "status": applied.status,
                    "reason": applied.reason,
                    "risk": risk,
                    "recovery_package": str(applied.transaction_dir) if applied.transaction_dir else "",
                }
            if cancelled is not None and cancelled():
                recovered = await self.action_gateway.recover(
                    applied.transaction_dir, scope=scope
                )
                return {
                    "status": recovered.status,
                    "reason": "apply_cancelled",
                    "risk": risk,
                    "recovery_package": str(applied.transaction_dir),
                }

            post_attempt = f"{attempt_id}-post-{uuid.uuid4().hex}"
            final_attempt = f"{attempt_id}-final-{uuid.uuid4().hex}"
            post_snapshot = None
            final_snapshot = None
            post_record: VerificationRecord | None = None
            failure_reason = "post_apply_verification_failed"
            try:
                post_snapshot = self.snapshot_service.create(post_attempt, change_set.project_root)
                if snapshot_fingerprint(post_snapshot) != change_set.content_fingerprint:
                    failure_reason = "post_apply_content_mismatch"
                else:
                    self.sandbox_runner.prepare_attempt(post_attempt, post_snapshot.workspace_root)
                    post_record = await self.verification_service.verify(post_attempt, post_snapshot)
                    if cancelled is not None and cancelled():
                        failure_reason = "apply_cancelled"
                    elif _verified_commands(post_record) != _verified_commands(verification):
                        failure_reason = "post_apply_commands_changed"
                    elif (
                        post_record.status == "verified"
                        and post_record.after_fingerprint == change_set.content_fingerprint
                    ):
                        final_snapshot = self.snapshot_service.create(
                            final_attempt, change_set.project_root
                        )
                        if (
                            snapshot_fingerprint(final_snapshot)
                            == change_set.content_fingerprint
                        ):
                            if cancelled is not None and cancelled():
                                failure_reason = "apply_cancelled"
                            else:
                                return {
                                    "status": "applied_verified",
                                    "risk": risk,
                                    "verification": post_record.as_dict(),
                                    "recovery_package": str(applied.transaction_dir),
                                }
                        else:
                            failure_reason = "project_changed_during_post_verification"
            except Exception as exc:
                failure_reason = f"post_apply_verification_error:{type(exc).__name__}"
            finally:
                try:
                    self.sandbox_runner.cleanup_attempt(post_attempt)
                except Exception:
                    pass
                if post_snapshot is not None:
                    try:
                        self.snapshot_service.cleanup(post_attempt)
                    except Exception:
                        pass
                if final_snapshot is not None:
                    try:
                        self.snapshot_service.cleanup(final_attempt)
                    except Exception:
                        pass

            recovered = await self.action_gateway.recover(
                applied.transaction_dir, scope=scope
            )
            return {
                "status": recovered.status,
                "reason": failure_reason,
                "risk": risk,
                "verification": post_record.as_dict() if post_record else {},
                "recovery_package": str(applied.transaction_dir),
            }
