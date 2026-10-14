"""Structured, bounded file operations for the Host work provider."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Iterable


class FileWorkService:
    def __init__(self, allowed_roots: Iterable[str | Path]) -> None:
        roots = tuple(Path(root).expanduser().resolve() for root in allowed_roots)
        if not roots:
            raise ValueError("at least one allowed root is required")
        self.allowed_roots = roots

    def _safe_path(self, value: str | Path) -> Path:
        candidate = Path(value).expanduser()
        if candidate.is_symlink():
            raise PermissionError("symlink paths are not allowed")
        resolved = candidate.resolve(strict=False)
        for root in self.allowed_roots:
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        raise PermissionError("path is outside the allowed file roots")

    @staticmethod
    def _regular_file(path: Path) -> bool:
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            return False

    def list_files(
        self,
        directory: str | Path,
        *,
        sort_by: str = "name",
        descending: bool = False,
        recursive: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        root = self._safe_path(directory)
        if not root.is_dir() or root.is_symlink():
            raise NotADirectoryError(str(root))
        if sort_by not in {"name", "size", "modified"}:
            raise ValueError("sort_by must be name, size, or modified")
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError("offset must be non-negative and limit must be 1..100")
        iterator = root.rglob("*") if recursive else root.iterdir()
        rows = []
        for path in iterator:
            if not self._regular_file(path):
                continue
            info = path.stat()
            rows.append({"name": path.name, "path": str(path), "size": info.st_size, "modified": info.st_mtime})
        key = {"name": lambda item: item["name"].casefold(), "size": lambda item: item["size"], "modified": lambda item: item["modified"]}[sort_by]
        rows.sort(key=key, reverse=descending)
        return {"directory": str(root), "total": len(rows), "offset": offset, "limit": limit, "items": rows[offset:offset + limit]}

    def list_directories(self, directory: str | Path | None = None) -> dict:
        """供目录选择器使用的子目录列举。

        directory 为 None（或空白）时返回允许根目录作为顶层；
        否则复用 _safe_path 做根目录约束，只列直接子目录（跳过 symlink）。
        """
        if directory is None or not str(directory).strip():
            roots = [{"name": root.name or str(root), "path": str(root)} for root in self.allowed_roots]
            return {"directory": "", "parent": None, "items": roots}
        root = self._safe_path(directory)
        if not root.is_dir() or root.is_symlink():
            raise NotADirectoryError(str(root))
        try:
            parent: str | None = str(self._safe_path(root.parent))
        except PermissionError:
            parent = None
        items = []
        for path in root.iterdir():
            try:
                if path.is_symlink() or not path.is_dir():
                    continue
            except OSError:
                continue
            items.append({"name": path.name, "path": str(path)})
        items.sort(key=lambda item: item["name"].casefold())
        return {"directory": str(root), "parent": parent, "items": items[:200]}

    def preview_sort_by_extension(self, directory: str | Path) -> dict:
        root = self._safe_path(directory)
        if not root.is_dir() or root.is_symlink():
            raise NotADirectoryError(str(root))
        moves, skipped = [], []
        for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
            if not self._regular_file(path):
                continue
            suffix = path.suffix.lower().lstrip(".") or "no_extension"
            target = root / suffix / path.name
            info = path.stat()
            item = {"source": str(path), "target": str(target), "size": info.st_size, "modified": info.st_mtime}
            if target.exists() or target.is_symlink():
                skipped.append({**item, "reason": "destination_exists"})
            else:
                moves.append(item)
        return {"directory": str(root), "moves": moves, "skipped": skipped}

    def apply_moves(self, moves: list[dict]) -> dict:
        applied, skipped = [], []
        for item in moves:
            source = self._safe_path(item.get("source", ""))
            target = self._safe_path(item.get("target", ""))
            if not self._regular_file(source):
                return {"status": "stale", "applied": applied, "skipped": skipped, "reason": "source_missing", "source": str(source)}
            info = source.stat()
            if int(item.get("size", -1)) != info.st_size or float(item.get("modified", -1)) != info.st_mtime:
                return {"status": "stale", "applied": applied, "skipped": skipped, "reason": "source_changed", "source": str(source)}
            if target.exists() or target.is_symlink():
                skipped.append({"source": str(source), "target": str(target), "reason": "destination_exists"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(source, target)
            except FileExistsError:
                skipped.append({"source": str(source), "target": str(target), "reason": "destination_exists"})
                continue
            applied.append({"source": str(source), "target": str(target), "status": "moved"})
        return {"status": "completed", "applied": applied, "skipped": skipped}
