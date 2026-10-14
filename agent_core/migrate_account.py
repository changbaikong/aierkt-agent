"""Idempotent migration of the pre-account single-user scope."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def migrate_legacy_scope(path: Path, account_id: str, *, legacy_owner: str = "test_owner") -> int:
    if not account_id.strip():
        raise ValueError("account_id is required")
    path = Path(path)
    if not path.exists():
        return 0
    changed = 0
    with sqlite3.connect(str(path), timeout=15.0) as db:
        db.execute("PRAGMA busy_timeout=15000")
        for table in ("tasks", "agent_core_confirmations", "agent_core_runs"):
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if "owner_id" not in columns:
                continue
            cur = db.execute(f"UPDATE {table} SET owner_id=? WHERE owner_id=?", (account_id, legacy_owner))
            changed += cur.rowcount
        db.commit()
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="append", required=True)
    parser.add_argument("--account-id", required=True)
    args = parser.parse_args()
    total = sum(migrate_legacy_scope(Path(path), args.account_id) for path in args.db)
    print(f"migrated_rows={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
