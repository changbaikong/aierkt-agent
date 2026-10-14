from __future__ import annotations

import sqlite3

import pytest

from agent_core.contracts import AccountTaskScope, Scope
from agent_core.persistence import SQLiteConfirmationStore


def test_confirmation_locks_allow_one_pending_action_per_full_scope(tmp_path) -> None:
    store = SQLiteConfirmationStore(tmp_path / "core.db")
    first = Scope("owner", "web", "first", "companion")
    second = Scope("owner", "web", "second", "companion")
    first_request = store.create(run_id="first-run", scope=first, tool_call_id="first-call", tool="danger", arguments={}, title="First")
    second_request = store.create(run_id="second-run", scope=second, tool_call_id="second-call", tool="danger", arguments={}, title="Second")

    store.create_lock(AccountTaskScope("owner", "companion"), first_request.action_id, first)
    store.create_lock(AccountTaskScope("owner", "companion"), second_request.action_id, second)

    assert store.active_lock(first).action_id == first_request.action_id
    assert store.active_lock(second).action_id == second_request.action_id


def test_legacy_account_module_lock_table_is_migrated_to_full_scope_key(tmp_path) -> None:
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE agent_core_locks (
            owner_id TEXT NOT NULL, module_id TEXT NOT NULL, channel TEXT NOT NULL,
            conversation_id TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE,
            reason TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, module_id)
        )"""
    )
    connection.execute(
        "INSERT INTO agent_core_locks VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("owner", "companion", "web", "existing", "existing-action", "waiting_confirmation", "2026-01-01T00:00:00"),
    )
    connection.commit()
    connection.close()

    store = SQLiteConfirmationStore(database)
    first = Scope("owner", "web", "existing", "companion")
    second = Scope("owner", "web", "new", "companion")
    store.create_lock(AccountTaskScope("owner", "companion"), "new-action", second)

    check = sqlite3.connect(database)
    primary_key = [row[1] for row in check.execute("PRAGMA table_info(agent_core_locks)") if row[5]]
    action_ids = [row[0] for row in check.execute("SELECT action_id FROM agent_core_locks ORDER BY action_id")]
    check.close()
    assert primary_key == ["owner_id", "channel", "conversation_id", "module_id"]
    assert action_ids == ["existing-action", "new-action"]


def test_legacy_lock_migration_failure_preserves_original_table(tmp_path) -> None:
    database = tmp_path / "invalid-legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """CREATE TABLE agent_core_locks (
            owner_id TEXT NOT NULL, module_id TEXT NOT NULL, channel TEXT NOT NULL,
            conversation_id TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            PRIMARY KEY (owner_id, module_id)
        )"""
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="legacy schema is missing required columns"):
        SQLiteConfirmationStore(database)

    check = sqlite3.connect(database)
    columns = [row[1] for row in check.execute("PRAGMA table_info(agent_core_locks)")]
    check.close()
    assert columns == ["owner_id", "module_id", "channel", "conversation_id", "action_id", "created_at"]
