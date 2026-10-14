"""Read-only terminal monitor for agent activity and memory dependencies."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

DEFAULT_STATE_DB = Path("agent_core.db")
MEMORY_LAYERS = ("hot", "warm", "cold")
FINISHED_PHASES = frozenset({"completed", "failed", "cancelled"})
FINISHED_DISPLAY_LIMIT = 5


def _ensure_utf8_stdio() -> None:
    """Windows 控制台默认用 GBK(cp936)，UTF-8 的中文日志会乱码；统一强制 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class HealthCheck:
    level: str
    detail: str


@dataclass(frozen=True)
class AgentRun:
    run_id: str
    owner_id: str
    channel: str
    conversation_id: str
    phase: str
    steps: int
    tokens: int
    updated_at: str
    is_stale: bool
    module_id: str = "companion"


@dataclass
class MonitorSnapshot:
    agents: list[AgentRun] = field(default_factory=list)
    agent_health: HealthCheck = field(default_factory=lambda: HealthCheck("unknown", "not sampled"))
    pending_confirmations: int = 0
    layer_counts: dict[str, int] = field(default_factory=dict)
    embedding_counts: dict[str, int] = field(default_factory=dict)
    max_retry_count: int = 0
    latest_embedding_error: str = ""
    write_health: HealthCheck = field(default_factory=lambda: HealthCheck("unknown", "not sampled"))
    retrieval_health: dict[str, HealthCheck] = field(default_factory=dict)
    memory_log_lines: list[str] = field(default_factory=list)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def _readonly_database(path: Path):
    connection = _readonly_connection(path)
    try:
        yield connection
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name=?", (name,)
    ).fetchone() is not None


def _parse_agent_rows(rows: Iterable[sqlite3.Row], stale_after_seconds: float) -> list[AgentRun]:
    now = datetime.now(UTC)
    result: list[AgentRun] = []
    for row in rows:
        updated = datetime.fromisoformat(row["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        stale = row["status"] not in {"paused", "completed", "failed", "cancelled"} and (now - updated).total_seconds() > stale_after_seconds
        result.append(
            AgentRun(
                run_id=row["run_id"],
                owner_id=row["owner_id"],
                channel=row["channel"],
                conversation_id=row["conversation_id"],
                module_id=row["module_id"] if "module_id" in row.keys() else "companion",
                phase=row["status"],
                steps=int(row["steps"]),
                tokens=int(row["tokens"]),
                updated_at=row["updated_at"],
                is_stale=stale,
            )
        )
    return result


def _sample_agents(path: Path, stale_after_seconds: float) -> tuple[list[AgentRun], HealthCheck, int]:
    if not path.exists():
        return [], HealthCheck("unavailable", f"state database not found: {path}"), 0
    try:
        with _readonly_database(path) as connection:
            if not _table_exists(connection, "agent_core_runs"):
                return [], HealthCheck("unavailable", "agent_core_runs table not found"), 0
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_core_runs)")}
            module_column = "module_id" if "module_id" in columns else "'companion' AS module_id"
            rows = connection.execute(
                "SELECT run_id, owner_id, channel, conversation_id, " + module_column + ", "
                "status, steps, tokens, updated_at FROM agent_core_runs "
                "ORDER BY updated_at DESC, run_id DESC"
            ).fetchall()
            pending_confirmations = 0
            if _table_exists(connection, "agent_core_confirmations"):
                pending_confirmations = int(connection.execute(
                    "SELECT COUNT(*) FROM agent_core_confirmations WHERE status='pending'"
                ).fetchone()[0])
        return _parse_agent_rows(rows, stale_after_seconds), HealthCheck("ok", "agent_core_runs readable"), pending_confirmations
    except (sqlite3.Error, ValueError, OSError) as exc:
        return [], HealthCheck("unavailable", f"state database unreadable: {type(exc).__name__}: {exc}"), 0


def _sample_memory(path: Path | None) -> tuple[dict[str, int], dict[str, int], int, str, HealthCheck, dict[str, HealthCheck]]:
    if path is None:
        check = HealthCheck("not_configured", "memory store is not connected to this agent")
        return {}, {}, 0, "", check, {"sqlite": check, "fts": check}
    unavailable = {
        "sqlite": HealthCheck("unavailable", "memory database not sampled"),
        "fts": HealthCheck("unavailable", "memory database not sampled"),
    }
    if not path.exists():
        return {}, {}, 0, "", HealthCheck("unavailable", f"memory database not found: {path}"), unavailable
    try:
        with _readonly_database(path) as connection:
            if not _table_exists(connection, "memory_records"):
                checks = {
                    "sqlite": HealthCheck("unavailable", "memory_records table not found"),
                    "fts": HealthCheck("unavailable", "memory_records table not found"),
                }
                return {}, {}, 0, "", HealthCheck("unavailable", "memory_records table not found"), checks
            layer_counts = {
                str(row["layer"]): int(row["count"])
                for row in connection.execute("SELECT layer, COUNT(*) AS count FROM memory_records GROUP BY layer")
            }
            embedding_counts = {
                str(row["embedding_status"]): int(row["count"])
                for row in connection.execute("SELECT embedding_status, COUNT(*) AS count FROM memory_records GROUP BY embedding_status")
            }
            max_retry = int(connection.execute("SELECT COALESCE(MAX(retry_count), 0) FROM memory_records").fetchone()[0])
            error_row = connection.execute(
                """SELECT last_embedding_error FROM memory_records
                   WHERE last_embedding_error IS NOT NULL AND last_embedding_error <> ''
                   ORDER BY rowid DESC LIMIT 1"""
            ).fetchone()
            latest_error = str(error_row[0]) if error_row else ""
            fts_exists = _table_exists(connection, "memory_records_fts")
            if fts_exists:
                connection.execute("SELECT rowid FROM memory_records_fts LIMIT 1").fetchall()
                fts_health = HealthCheck("ok", "FTS5 readable")
            else:
                fts_health = HealthCheck("unavailable", "memory_records_fts table not found")
        failed = embedding_counts.get("failed", 0) + embedding_counts.get("dead_letter", 0)
        write_health = HealthCheck(
            "degraded" if failed else "ok",
            f"{failed} failed/dead-letter embedding(s)" if failed else "memory records readable",
        )
        return layer_counts, embedding_counts, max_retry, latest_error, write_health, {
            "sqlite": HealthCheck("ok", "memory_records readable"),
            "fts": fts_health,
        }
    except (sqlite3.Error, OSError) as exc:
        detail = f"memory database unreadable: {type(exc).__name__}: {exc}"
        checks = {"sqlite": HealthCheck("unavailable", detail), "fts": HealthCheck("unavailable", detail)}
        return {}, {}, 0, "", HealthCheck("unavailable", detail), checks


def _read_local_points(storage_sqlite: Path) -> int | None:
    """只读 Qdrant 本地 collection 的点数，不启动 QdrantClient（避免抢锁）。

    Qdrant 本地模式每个 collection 就是 ``collection/<name>/storage.sqlite``，
    points 存一张表里。以 mode=ro 打开，agent 进程运行时也能并发读。
    """
    try:
        uri = f"{storage_sqlite.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            row = connection.execute("SELECT COUNT(*) FROM points").fetchone()
            return int(row[0]) if row else None
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return None


def _sample_vector_store(path: Path | None) -> HealthCheck:
    if path is None:
        return HealthCheck("not_configured", "vector store is not connected to this agent")
    if not path.exists():
        return HealthCheck("unavailable", f"vector directory not found: {path}")
    details: list[str] = []
    missing: list[str] = []
    for layer in MEMORY_LAYERS:
        collection = f"{layer}_memories_vectors"
        count = _read_local_points(path / "collection" / collection / "storage.sqlite")
        if count is None:
            missing.append(collection)
            continue
        details.append(f"{layer}={count}")
    if missing:
        return HealthCheck("unavailable", "missing collection(s): " + ", ".join(missing))
    return HealthCheck("ok", " ".join(details))


def _tail_log(path: Path, max_lines: int = 12) -> list[str]:
    """只读日志文件尾部最近若干行（chat_cli 不断写入该文件）。

    只在文件末尾 seek 出一段缓冲来读，日志文件再大也按固定量读，
    ``--once`` / 每轮刷新都开销很小。
    """
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        if size == 0:
            return []
        buf_bytes = min(size, max_lines * 1024)  # 每行按 ~1KB 预留给中文字符
        with open(path, "rb") as fh:
            fh.seek(size - buf_bytes)
            data = fh.read()
    except OSError:
        return []
    lines = [line for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-max_lines:]


def _sample_memory_log(path: Path | None) -> list[str]:
    if path is None:
        return []
    return _tail_log(path)


def collect_snapshot(
    *,
    state_db: Path,
    memory_db: Path | None,
    vector_path: Path | None,
    stale_after_seconds: float,
    memory_log: Path | None = None,
) -> MonitorSnapshot:
    agents, agent_health, pending_confirmations = _sample_agents(state_db, stale_after_seconds)
    layer_counts, embedding_counts, max_retry, latest_error, write_health, retrieval_health = _sample_memory(memory_db)
    retrieval_health["vector"] = _sample_vector_store(vector_path)
    return MonitorSnapshot(
        agents=agents,
        agent_health=agent_health,
        pending_confirmations=pending_confirmations,
        layer_counts=layer_counts,
        embedding_counts=embedding_counts,
        max_retry_count=max_retry,
        latest_embedding_error=latest_error,
        write_health=write_health,
        retrieval_health=retrieval_health,
        memory_log_lines=_sample_memory_log(memory_log),
    )


def _status_text(check: HealthCheck) -> str:
    styles = {"ok": "green", "degraded": "yellow", "unavailable": "red", "not_configured": "cyan", "unknown": "dim"}
    return f"[{styles.get(check.level, 'white')}]{check.level.upper()}[/] {check.detail}"


def _scope_label(run: AgentRun) -> str:
    return f"{run.owner_id}/{run.channel}/{run.conversation_id}/{run.module_id}"


def split_runs(
    runs: Iterable[AgentRun],
    *,
    finished_limit: int = FINISHED_DISPLAY_LIMIT,
) -> tuple[list[AgentRun], list[AgentRun]]:
    active: list[AgentRun] = []
    finished: list[AgentRun] = []
    for run in runs:
        (finished if run.phase in FINISHED_PHASES else active).append(run)
    return active, finished[:max(0, finished_limit)]


def active_panel_title(snapshot: MonitorSnapshot, active_count: int) -> str:
    return (
        f"Active {active_count} | pending {snapshot.pending_confirmations} "
        f"| state {_status_text(snapshot.agent_health)}"
    )


def _short_run_id(run: AgentRun) -> str:
    return run.run_id[:8]


def _compact_scope(run: AgentRun) -> str:
    value = _scope_label(run)
    return value if len(value) <= 20 else value[:17] + "..."


def _compact_time(run: AgentRun) -> str:
    try:
        return datetime.fromisoformat(run.updated_at).strftime("%H:%M:%S")
    except ValueError:
        return run.updated_at[-8:]


def _compact_progress(run: AgentRun) -> str:
    return f"s{run.steps}/t{run.tokens}"


def _compact_phase(run: AgentRun) -> str:
    if run.is_stale:
        return "STALE"
    return {
        "completed": "done",
        "cancelled": "cancel",
        "waiting_confirmation": "waiting",
    }.get(run.phase, run.phase)


def _health_code(check: HealthCheck | None) -> str:
    """Return a short status token suitable for a narrow terminal."""
    if check is None:
        return "?"
    return {
        "ok": "OK",
        "degraded": "DEG",
        "unavailable": "OFF",
        "not_configured": "N/C",
        "unknown": "?",
    }.get(check.level, check.level.upper()[:4])


def _fit_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3] + "..."


def _write_summary(snapshot: MonitorSnapshot) -> str:
    layers = " ".join(f"{layer}={snapshot.layer_counts.get(layer, 0)}" for layer in MEMORY_LAYERS)
    if snapshot.embedding_counts:
        embeddings = " ".join(
            f"{status[:4]}={count}" for status, count in sorted(snapshot.embedding_counts.items())
        )
    else:
        embeddings = "none"
    summary = f"Writes {_health_code(snapshot.write_health)} | {layers} | emb={embeddings} | retry={snapshot.max_retry_count}"
    if snapshot.latest_embedding_error:
        summary += f" | {_fit_text(snapshot.latest_embedding_error, 18)}"
    return _fit_text(summary, 72)


def _retrieval_summary(snapshot: MonitorSnapshot) -> str:
    statuses = " ".join(
        f"{name}={_health_code(snapshot.retrieval_health.get(name))}"
        for name in ("sqlite", "fts", "vector")
    )
    return _fit_text(f"Retrieval {statuses}", 72)


def _agent_table(runs: Iterable[AgentRun], empty_detail: str):
    from rich.table import Table

    table = Table(show_header=True, expand=False, box=None, padding=(0, 1))
    table.add_column("Run")
    table.add_column("Scope")
    table.add_column("Phase")
    table.add_column("Time")
    table.add_column("Stat")
    has_rows = False
    for run in runs:
        has_rows = True
        table.add_row(
            _short_run_id(run),
            _compact_scope(run),
            _compact_phase(run),
            _compact_time(run),
            _compact_progress(run),
        )
    if not has_rows:
        table.add_row("-", "-", "-", "-", empty_detail)
    return table


def render_snapshot(snapshot: MonitorSnapshot):
    """Build Rich renderables only when the interactive UI is requested."""
    from rich.console import Group
    from rich.panel import Panel
    from rich.text import Text

    active_runs, finished_runs = split_runs(snapshot.agents)
    active_table = _agent_table(active_runs, "no active runs")
    finished_table = _agent_table(finished_runs, "no finished runs")

    return Group(
        Panel(active_table, title=active_panel_title(snapshot, len(active_runs)), expand=False),
        Panel(finished_table, title=f"Finished latest {FINISHED_DISPLAY_LIMIT}", expand=False),
        Panel(Text(_write_summary(snapshot)), title="Writes", expand=False),
        Panel(Text(_retrieval_summary(snapshot)), title="Retrieval", expand=False),
        Panel(
            Text("\n".join(_fit_text(line, 118) for line in snapshot.memory_log_lines) or "no memory log yet"),
            title="Memory Log",
            expand=False,
        ),
    )


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only agent and memory health monitor")
    parser.add_argument("--state-db", type=Path, default=DEFAULT_STATE_DB)
    parser.add_argument("--memory-db", type=Path, default=Path("data") / "memory_records.db",
                        help="memory_records.db path (default: data/memory_records.db)")
    parser.add_argument("--memory-log", type=Path, default=None,
                        help="memory operation log path (default: <memory-db dir>/memory.log)")
    parser.add_argument("--vector-path", type=Path, default=Path("data") / "memory_vectors",
                        help="Qdrant local directory (default: data/memory_vectors)")
    parser.add_argument("--refresh-seconds", type=_positive_float, default=2.0)
    parser.add_argument("--stale-after-seconds", type=_positive_float, default=15.0)
    parser.add_argument("--once", action="store_true", help="render one snapshot and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _ensure_utf8_stdio()
    args = parse_args(argv)
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        print("rich is required; install dependencies with: pip install -r requirements.txt", file=sys.stderr)
        return 1

    def snapshot():
        return collect_snapshot(
            state_db=args.state_db,
            memory_db=args.memory_db,
            vector_path=args.vector_path,
            stale_after_seconds=args.stale_after_seconds,
            memory_log=args.memory_log or args.memory_db.parent / "memory.log",
        )

    if args.once:
        Console().print(render_snapshot(snapshot()))
        return 0
    try:
        with Live(render_snapshot(snapshot()), refresh_per_second=max(1.0, 1.0 / args.refresh_seconds)) as live:
            while True:
                time.sleep(args.refresh_seconds)
                live.update(render_snapshot(snapshot()))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
