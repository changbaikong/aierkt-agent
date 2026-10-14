"""只读审计：历史记录里**可能被默认短期政策误清掉**的项目阶段约束。

计划第 2 条要求：旧记录先只读审计、列出可能误过期的项目约束，本次**不批量恢复**
生产记录。这个脚本就是那一步：

- 只读打开（SQLite ``mode=ro``），一句写操作都没有；
- 命中条件：状态已经不在现行集合（expired / archived），而且内容是「阶段约束」
  的写法（既有事项名词，又有暂不／尚未／先／继续这类阶段措辞）；
- 输出计数、逐条明细与证据文件路径；不修改任何一条记录。

用法：``python scripts/audit_expired_constraints.py [数据库路径]``，
默认读 ``data/memory_records.db``（本机记忆库）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.memory_horizon import is_phase_constraint

DEFAULT_DB = ROOT / "data" / "memory_records.db"
#: 到期原因里「时间到了」这一类：被替代／取消不算误过期。
TIME_REASONS = ("expired", "decay", "cold_retention")


def audit(db_path: Path) -> dict:
    if not db_path.exists():
        return {"ok": False, "error": f"数据库不存在：{db_path}", "hits": [], "scanned": 0}
    # 只读连接：写入会直接抛错，从机制上保证不动数据。
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT id, content, kind, status, content_status, layer, created_at, expires_at, "
            "purge_after, deletion_reason, metadata FROM memory_records"
        ).fetchall()
    finally:
        connection.close()
    hits: list[dict] = []
    for row in rows:
        status = str(row["status"] or "")
        if status == "active":
            continue
        reason = str(row["deletion_reason"] or "")
        content = str(row["content"] or "")
        if not is_phase_constraint(content):
            continue
        hits.append(
            {
                "id": row["id"],
                "content": content,
                "kind": row["kind"],
                "status": status,
                "layer": row["layer"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "deletion_reason": reason,
                "time_based": reason in TIME_REASONS or bool(row["expires_at"]),
                "metadata": row["metadata"],
            }
        )
    return {
        "ok": True,
        "database": str(db_path),
        "scanned": len(rows),
        "suspected": len(hits),
        "time_based_suspected": sum(1 for item in hits if item["time_based"]),
        "hits": hits,
        "read_only": True,
        "restored_any": False,
        "run_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
    }


def main(argv: list[str]) -> int:
    db_path = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_DB
    report = audit(db_path)
    out = ROOT / ".pytmp" / "evidence" / "2026-10-12-memory-legacy-constraint-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "hits"}
    print(json.dumps(summary, ensure_ascii=False))
    for item in report.get("hits", [])[:20]:
        print(
            f"- [{item['status']}] {item['content'][:40]} "
            f"| expires_at={item['expires_at']} | reason={item['deletion_reason']}"
        )
    print(f"证据：{out}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
