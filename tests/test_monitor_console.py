from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from monitor_console import (
    AgentRun,
    HealthCheck,
    MonitorSnapshot,
    _compact_progress,
    _compact_scope,
    _compact_time,
    _short_run_id,
    _scope_label,
    active_panel_title,
    collect_snapshot,
    _retrieval_summary,
    _write_summary,
    split_runs,
)


class MonitorConsoleSnapshotTests(unittest.TestCase):
    def test_diagnostic_summaries_fit_small_terminal(self) -> None:
        snapshot = MonitorSnapshot(
            layer_counts={"hot": 0, "warm": 0, "cold": 0},
            embedding_counts={},
            write_health=HealthCheck("not_configured", "memory store is not connected to this agent"),
            retrieval_health={
                "sqlite": HealthCheck("not_configured", "memory store is not connected to this agent"),
                "fts": HealthCheck("not_configured", "memory store is not connected to this agent"),
                "vector": HealthCheck("not_configured", "vector store is not connected to this agent"),
            },
        )

        self.assertLessEqual(len(_write_summary(snapshot)), 72)
        self.assertLessEqual(len(_retrieval_summary(snapshot)), 72)

    def test_compact_run_fields_fit_small_terminal_columns(self) -> None:
        run = AgentRun("1234567890abcdef", "owner", "desktop", "conversation", "running", 2, 17, "2026-08-14T12:34:56", False)

        self.assertEqual(_short_run_id(run), "12345678")
        self.assertLessEqual(len(_compact_scope(run)), 20)
        self.assertEqual(_compact_time(run), "12:34:56")
        self.assertEqual(_compact_progress(run), "s2/t17")

    def test_active_panel_title_reports_active_count_not_total_history_count(self) -> None:
        snapshot = MonitorSnapshot(
            agents=[
                AgentRun("running", "owner", "cli", "one", "running", 0, 0, "2026-08-14T12:00:00", False),
                AgentRun("completed", "owner", "cli", "two", "completed", 0, 0, "2026-08-14T12:00:00", False),
            ],
            agent_health=HealthCheck("ok", "agent_core_runs readable"),
            pending_confirmations=1,
        )

        self.assertEqual(active_panel_title(snapshot, 1), "Active 1 | pending 1 | state [green]OK[/] agent_core_runs readable")

    def test_split_runs_keeps_active_and_finished_runs_in_separate_groups(self) -> None:
        runs = [
            AgentRun("running", "owner", "cli", "one", "running", 0, 0, "2026-08-14T12:00:00", False),
            AgentRun("paused", "owner", "cli", "two", "paused", 0, 0, "2026-08-14T12:00:00", False),
            AgentRun("completed", "owner", "cli", "three", "completed", 0, 0, "2026-08-14T12:00:00", False),
            AgentRun("failed", "owner", "cli", "four", "failed", 0, 0, "2026-08-14T12:00:00", False),
        ]

        active, finished = split_runs(runs)

        self.assertEqual([run.run_id for run in active], ["running", "paused"])
        self.assertEqual([run.run_id for run in finished], ["completed", "failed"])

    def test_split_runs_limits_finished_display_to_latest_five(self) -> None:
        runs = [
            AgentRun(f"finished-{index}", "owner", "cli", str(index), "completed", 0, 0, f"2026-08-14T12:00:{index:02d}", False)
            for index in range(7)
        ]

        active, finished = split_runs(runs)

        self.assertEqual(active, [])
        self.assertEqual([run.run_id for run in finished], [f"finished-{index}" for index in range(5)])

    def test_scope_label_uses_flat_agent_run_fields(self) -> None:
        run = AgentRun("run-1", "owner", "cli", "conversation", "running", 2, 17, "2026-08-14T12:00:00", False)

        self.assertEqual(_scope_label(run), "owner/cli/conversation/companion")

    def test_snapshot_reports_embedding_failure_and_missing_vector_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.db"
            memory_path = root / "memory.db"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    """CREATE TABLE agent_core_runs (
                        run_id TEXT, owner_id TEXT, channel TEXT, conversation_id TEXT,
                        status TEXT, messages_json TEXT, steps INTEGER, tokens INTEGER, updated_at TEXT
                    )"""
                )
                connection.execute(
                    "INSERT INTO agent_core_runs VALUES ('run-1', 'owner', 'cli', 'conversation', 'running', '[]', 2, 17, '2026-08-14T12:00:00')"
                )
                connection.commit()
            finally:
                connection.close()

            connection = sqlite3.connect(memory_path)
            try:
                connection.execute(
                    """CREATE TABLE memory_records (
                        layer TEXT, status TEXT, embedding_status TEXT,
                        retry_count INTEGER, last_embedding_error TEXT
                    )"""
                )
                connection.execute("CREATE VIRTUAL TABLE memory_records_fts USING fts5(content)")
                connection.execute(
                    "INSERT INTO memory_records VALUES ('warm', 'candidate', 'failed', 2, 'embedding endpoint unavailable')"
                )
                connection.execute("INSERT INTO memory_records_fts(content) VALUES ('memory')")
                connection.commit()
            finally:
                connection.close()

            snapshot = collect_snapshot(
                state_db=state_path,
                memory_db=memory_path,
                vector_path=root / "missing-vectors",
                stale_after_seconds=30,
            )

        self.assertEqual(snapshot.agents[0].run_id, "run-1")
        self.assertEqual(snapshot.agents[0].phase, "running")
        self.assertEqual(snapshot.write_health.level, "degraded")
        self.assertEqual(snapshot.embedding_counts, {"failed": 1})
        self.assertEqual(snapshot.retrieval_health["sqlite"].level, "ok")
        self.assertEqual(snapshot.retrieval_health["fts"].level, "ok")
        self.assertEqual(snapshot.retrieval_health["vector"].level, "unavailable")

    def test_snapshot_marks_memory_as_not_configured_when_current_agent_has_no_memory_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "agent.db"
            connection = sqlite3.connect(state_path)
            try:
                connection.execute(
                    """CREATE TABLE agent_core_runs (
                        run_id TEXT, owner_id TEXT, channel TEXT, conversation_id TEXT,
                        status TEXT, messages_json TEXT, steps INTEGER, tokens INTEGER, updated_at TEXT
                    )"""
                )
                connection.execute(
                    """CREATE TABLE agent_core_confirmations (
                        action_id TEXT, run_id TEXT, owner_id TEXT, channel TEXT,
                        conversation_id TEXT, status TEXT
                    )"""
                )
                connection.execute(
                    "INSERT INTO agent_core_runs VALUES ('run-2', 'owner', 'cli', 'conversation', 'paused', '[]', 1, 8, '2026-08-14T12:00:00')"
                )
                connection.execute(
                    "INSERT INTO agent_core_confirmations VALUES ('action-1', 'run-2', 'owner', 'cli', 'conversation', 'pending')"
                )
                connection.commit()
            finally:
                connection.close()

            snapshot = collect_snapshot(
                state_db=state_path,
                memory_db=None,
                vector_path=None,
                stale_after_seconds=30,
            )

        self.assertEqual(snapshot.pending_confirmations, 1)
        self.assertEqual(snapshot.write_health.level, "not_configured")
        self.assertEqual(snapshot.retrieval_health["sqlite"].level, "not_configured")
        self.assertEqual(snapshot.retrieval_health["vector"].level, "not_configured")
