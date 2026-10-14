import sqlite3

from agent_core.migrate_account import migrate_legacy_scope


def test_migrate_legacy_scope_is_idempotent(tmp_path):
    path = tmp_path / "tasks.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tasks (owner_id TEXT, title TEXT)")
        db.execute("INSERT INTO tasks VALUES ('test_owner', 'wake')")
        db.commit()
    assert migrate_legacy_scope(path, "account-1") == 1
    assert migrate_legacy_scope(path, "account-1") == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT owner_id FROM tasks").fetchone()[0] == "account-1"
