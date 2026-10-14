"""Code-agent-only tools constrained to one workspace snapshot."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..contracts import EFFECT_PERSISTENT, EFFECT_READ, ToolSpec
from ..sandbox_contracts import SandboxCommand, _reject_reparse_points
from ..workspace_snapshot import SnapshotValidationError, WorkspaceSnapshot


@dataclass(frozen=True)
class CodeAgentToolContext:
    attempt_id: str
    snapshot: WorkspaceSnapshot
    sandbox_runner: Any
    python_executable: Path
    powershell_executable: Path
    search_executable: Path | None = None
    max_read_bytes: int = 1024 * 1024
    max_write_bytes: int = 1024 * 1024
    max_search_matches: int = 200
    submit_scope: Any = None
    memory_retriever: Any = None
    network_proxy: Any = None


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _relative_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise SnapshotValidationError("path must be a non-empty relative path")
    path = Path(raw)
    normalized = raw.replace("/", "\\")
    if path.is_absolute() or normalized.startswith("\\") or ".." in path.parts:
        raise SnapshotValidationError("path must stay inside the snapshot")
    if path.parts and path.parts[0].casefold() == ".sandbox":
        raise SnapshotValidationError("path is reserved for sandbox runtime state")
    return path


def _workspace_path(
    context: CodeAgentToolContext,
    raw: Any,
    *,
    must_exist: bool,
    directory: bool | None = None,
) -> Path:
    relative = _relative_path(raw)
    root = context.snapshot.workspace_root.resolve(strict=True)
    candidate = root / relative
    inspect_target = candidate if candidate.exists() or candidate.is_symlink() else candidate.parent
    try:
        _reject_reparse_points(inspect_target, "snapshot path")
        resolved = inspect_target.resolve(strict=True)
        resolved.relative_to(root)
    except Exception as exc:
        raise SnapshotValidationError(f"snapshot path is unsafe: {exc}") from exc
    if must_exist and not candidate.exists():
        raise FileNotFoundError(str(relative))
    if candidate.exists():
        try:
            final = candidate.resolve(strict=True)
            final.relative_to(root)
            _reject_reparse_points(candidate, "snapshot path")
        except Exception as exc:
            raise SnapshotValidationError(f"snapshot path is unsafe: {exc}") from exc
        if directory is True and not final.is_dir():
            raise NotADirectoryError(str(relative))
        if directory is False and not final.is_file():
            raise IsADirectoryError(str(relative))
        return final
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_header_path(value: str) -> str | None:
    token = value.strip().split("\t", 1)[0]
    if token == "/dev/null":
        return None
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    _relative_path(token)
    return token


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")


def _apply_unified_patch(context: CodeAgentToolContext, patch: str) -> list[str]:
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("patch must be a non-empty unified diff")
    lines = patch.splitlines(keepends=True)
    index = 0
    changed: list[str] = []
    while index < len(lines):
        if not lines[index].startswith("--- "):
            raise ValueError("patch must start each file with ---")
        old_name = _parse_header_path(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("patch file header is incomplete")
        new_name = _parse_header_path(lines[index][4:])
        index += 1
        if old_name is not None and new_name is not None and old_name != new_name:
            raise ValueError("patch renames are not supported")
        name = new_name or old_name
        if name is None:
            raise ValueError("patch file has no path")
        target = _workspace_path(
            context,
            name,
            must_exist=old_name is not None,
            directory=False if old_name is not None else None,
        )
        original = target.read_text(encoding="utf-8").splitlines(keepends=True) if old_name else []
        output: list[str] = []
        cursor = 0
        saw_hunk = False
        while index < len(lines) and lines[index].startswith("@@ "):
            saw_hunk = True
            match = _HUNK.match(lines[index].rstrip("\r\n"))
            if match is None:
                raise ValueError("invalid patch hunk header")
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
            hunk_start = max(0, old_start - 1)
            if hunk_start < cursor or hunk_start > len(original):
                raise ValueError("patch hunk is out of order")
            output.extend(original[cursor:hunk_start])
            cursor = hunk_start
            index += 1
            consumed_old = 0
            produced_new = 0
            while index < len(lines) and not lines[index].startswith(("@@ ", "--- ")):
                line = lines[index]
                if line.startswith("\\ No newline at end of file"):
                    index += 1
                    continue
                if not line or line[0] not in " +-":
                    raise ValueError("invalid patch hunk line")
                marker, text = line[0], line[1:]
                if marker in " -":
                    if cursor >= len(original) or original[cursor] != text:
                        raise ValueError("patch context does not match snapshot")
                    if marker == " ":
                        output.append(original[cursor])
                        produced_new += 1
                    cursor += 1
                    consumed_old += 1
                else:
                    output.append(text)
                    produced_new += 1
                index += 1
            if consumed_old != old_count or produced_new != new_count:
                raise ValueError("patch hunk counts do not match")
        if not saw_hunk:
            raise ValueError("patch file has no hunks")
        output.extend(original[cursor:])
        if new_name is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = "".join(output).encode("utf-8")
            if len(data) > context.max_write_bytes:
                raise ValueError("patched file exceeds write limit")
            _atomic_write(target, data)
        changed.append(name)
    return changed


def make_code_agent_tools(context: CodeAgentToolContext) -> list[ToolSpec]:
    executables = {
        "python": context.python_executable,
        "powershell": context.powershell_executable,
    }
    if context.search_executable is not None:
        executables["search"] = context.search_executable

    def list_files(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        raw = arguments.get("path", ".")
        directory = _workspace_path(context, raw, must_exist=True, directory=True)
        offset = max(0, int(arguments.get("offset", 0)))
        limit = max(1, min(500, int(arguments.get("limit", 100))))
        root = context.snapshot.workspace_root
        entries = []
        for item in sorted(directory.iterdir(), key=lambda value: value.name.casefold()):
            _reject_reparse_points(item, "snapshot entry")
            entries.append(
                {
                    "path": item.relative_to(root).as_posix(),
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )
        return {"entries": entries[offset : offset + limit], "total": len(entries)}

    def read_file(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        path = _workspace_path(context, arguments["path"], must_exist=True, directory=False)
        offset = max(0, int(arguments.get("offset", 0)))
        limit = max(1, min(context.max_read_bytes, int(arguments.get("limit", context.max_read_bytes))))
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit + 1)
        truncated = len(data) > limit
        data = data[:limit]
        return {
            "text": data.decode("utf-8"),
            "bytes": len(data),
            "offset": offset,
            "truncated": truncated,
        }

    def search(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        expression = re.compile(str(arguments["pattern"]))
        target = _workspace_path(context, arguments.get("path", "."), must_exist=True)
        files: Iterable[Path] = [target] if target.is_file() else target.rglob("*")
        matches: list[dict[str, Any]] = []
        root = context.snapshot.workspace_root
        for path in files:
            if len(matches) >= context.max_search_matches:
                break
            if not path.is_file():
                continue
            _reject_reparse_points(path, "snapshot search path")
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if expression.search(line):
                    matches.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(matches) >= context.max_search_matches:
                        break
        return {"matches": matches, "truncated": len(matches) >= context.max_search_matches}

    def write_file(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        target = _workspace_path(context, arguments["path"], must_exist=False)
        text = arguments["text"]
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        data = text.encode("utf-8")
        if len(data) > context.max_write_bytes:
            raise ValueError("write exceeds byte limit")
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
        return {"path": target.relative_to(context.snapshot.workspace_root).as_posix(), "bytes": len(data)}

    def apply_patch(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        changed = _apply_unified_patch(context, arguments["patch"])
        return {"changed": changed, "count": len(changed)}

    def knowledge_search(arguments: dict[str, Any], scope: Any) -> dict[str, Any]:
        query = str(arguments["query"]).strip()
        token_budget = int(arguments.get("token_budget", 2000))
        result = context.memory_retriever.retrieve(
            context.submit_scope or scope,
            query,
            token_budget=token_budget,
        )

        def limited(items: Any, maximum: int = 20) -> list[dict[str, Any]]:
            output = []
            for item in list(items or [])[:maximum]:
                record = dict(item)
                if "content" in record:
                    record["content"] = str(record["content"])[:4000]
                output.append(record)
            return output

        return {
            "query": query,
            "hot": limited(result.hot),
            "warm": limited(result.warm),
            "cold_evidence": limited(result.cold_evidence),
            "citations": limited(result.citations),
            "metadata": dict(result.metadata or {}),
        }

    def web_search(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        return context.network_proxy.search(
            arguments["query"], max_results=int(arguments.get("max_results", 5))
        )

    def web_read(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        return context.network_proxy.read(arguments["url"])

    async def run(arguments: dict[str, Any], _scope: Any) -> dict[str, Any]:
        runtime = arguments["runtime"]
        executable = executables[runtime]
        cwd = _workspace_path(context, arguments.get("cwd", "."), must_exist=True, directory=True)
        args = arguments.get("args", [])
        command = SandboxCommand(
            argv=(str(executable), *(str(item) for item in args)),
            cwd=cwd,
            env=dict(arguments.get("env") or {}),
            timeout_seconds=arguments.get("timeout_seconds"),
        )
        result = await context.sandbox_runner.run(context.attempt_id, command)
        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "cancelled": result.cancelled,
            "killed": result.killed,
            "output_truncated": result.output_truncated,
            "termination_reason": result.termination_reason,
        }

    common = {"allowed_origins": frozenset({"code_agent"}), "capability_id": "code_agent", "visibility": "internal"}
    tools = [
        ToolSpec(
            "code__list",
            _schema("code__list", "List a snapshot directory.", {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}, []),
            list_files,
            effect=EFFECT_READ,
            risk="low",
            **common,
        ),
        ToolSpec(
            "code__read",
            _schema("code__read", "Read UTF-8 text from the snapshot.", {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1}}, ["path"]),
            read_file,
            effect=EFFECT_READ,
            risk="low",
            **common,
        ),
        ToolSpec(
            "code__search",
            _schema("code__search", "Search UTF-8 snapshot files.", {"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]),
            search,
            effect=EFFECT_READ,
            risk="low",
            **common,
        ),
        ToolSpec(
            "code__write",
            _schema("code__write", "Atomically write a UTF-8 snapshot file.", {"path": {"type": "string"}, "text": {"type": "string"}}, ["path", "text"]),
            write_file,
            effect=EFFECT_PERSISTENT,
            risk="low",
            **common,
        ),
        ToolSpec(
            "code__apply_patch",
            _schema("code__apply_patch", "Apply a strict unified diff inside the snapshot.", {"patch": {"type": "string"}}, ["patch"]),
            apply_patch,
            effect=EFFECT_PERSISTENT,
            risk="low",
            **common,
        ),
        ToolSpec(
            "code__run",
            _schema(
                "code__run",
                "Run a managed runtime in the Windows sandbox.",
                {
                    "runtime": {"type": "string", "enum": sorted(executables)},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "env": {"type": "object", "additionalProperties": {"type": "string"}},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                ["runtime", "args"],
            ),
            run,
            effect=EFFECT_PERSISTENT,
            risk="low",
            timeout_seconds=330.0,
            **common,
        ),
    ]
    if context.memory_retriever is not None:
        tools.append(
            ToolSpec(
                "code__knowledge_search",
                _schema(
                    "code__knowledge_search",
                    "Search knowledge from the original conversation scope.",
                    {
                        "query": {"type": "string", "minLength": 1},
                        "token_budget": {"type": "integer", "minimum": 1, "maximum": 8000},
                    },
                    ["query"],
                ),
                knowledge_search,
                effect=EFFECT_READ,
                risk="low",
                **common,
            )
        )
    if context.network_proxy is not None:
        tools.extend(
            [
                ToolSpec(
                    "code__web_search",
                    _schema(
                        "code__web_search",
                        "Search the web through the host proxy.",
                        {
                            "query": {"type": "string", "minLength": 1},
                            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                        },
                        ["query"],
                    ),
                    web_search,
                    effect=EFFECT_READ,
                    risk="low",
                    **common,
                ),
                ToolSpec(
                    "code__web_read",
                    _schema(
                        "code__web_read",
                        "Read a public HTTP(S) text page through the host proxy.",
                        {"url": {"type": "string", "minLength": 1}},
                        ["url"],
                    ),
                    web_read,
                    effect=EFFECT_READ,
                    risk="low",
                    **common,
                ),
            ]
        )
    return tools
