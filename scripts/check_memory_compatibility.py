"""只读兼容审计：老记忆库遇上新版语义契约，有没有会挡路的数据。

【为什么要在启用前查】
新版语义链路读的是同一张 `memory_records` 表：老记录（semantics_version=0、
content_status 为空或旧取值）和新记录混在一起。启用之前先确认没有会让新链路
出问题的存量数据 —— 例如契约里不存在的类型/状态组合、悬空的现行版本引用、
同一事项出现两条现行版本。

**只读**：sqlite 以 mode=ro 打开，不写一个字节。退出码 0 = 没有阻断问题，1 = 有。

用法：
    python scripts/check_memory_compatibility.py [--db data/memory_records.db]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.memory_turn import CONTENT_KINDS, CONTENT_STATUSES, REVIEW_STATES


def audit(db_path: Path) -> dict:
    problems: list[dict] = []
    facts: dict = {}
    uri = f"file:{db_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        total = db.execute("SELECT COUNT(*) AS n FROM memory_records").fetchone()["n"]
        new_semantic = db.execute(
            "SELECT COUNT(*) AS n FROM memory_records WHERE semantics_version>=1"
        ).fetchone()["n"]
        facts["db"] = str(db_path)
        facts["records"] = total
        facts["semantic_v1"] = new_semantic
        facts["by_kind"] = {
            str(row["kind"]): int(row["n"])
            for row in db.execute(
                "SELECT kind, COUNT(*) AS n FROM memory_records GROUP BY kind ORDER BY n DESC"
            )
        }
        facts["by_status"] = {
            str(row["status"]): int(row["n"])
            for row in db.execute("SELECT status, COUNT(*) AS n FROM memory_records GROUP BY status")
        }
        facts["content_status_present"] = {
            str(row["content_status"]): int(row["n"])
            for row in db.execute(
                "SELECT content_status, COUNT(*) AS n FROM memory_records "
                "WHERE content_status IS NOT NULL GROUP BY content_status"
            )
        }

        # 1) 类型/状态组合不在契约里：新链路按七类读写，遇到未知组合时行为没有定义。
        allowed_pairs = {(kind, status) for kind, statuses in CONTENT_STATUSES.items() for status in statuses}
        offenders = [
            dict(row)
            for row in db.execute(
                "SELECT id, kind, content_status, content FROM memory_records "
                "WHERE content_status IS NOT NULL AND content_status<>''"
            )
            if (str(row["kind"]), str(row["content_status"])) not in allowed_pairs
        ]
        # 老陪伴记录（semantics_version=0）用旧枚举，不算阻断；只有新语义记录才必须合规。
        blocking_pairs = [
            item
            for item in offenders
            if db.execute(
                "SELECT semantics_version FROM memory_records WHERE id=?", (item["id"],)
            ).fetchone()[0]
            >= 1
        ]
        facts["unknown_pairs_total"] = len(offenders)
        facts["unknown_pairs_new_semantic"] = len(blocking_pairs)
        if blocking_pairs:
            problems.append(
                {
                    "kind": "unknown_kind_status_pair",
                    "detail": "新语义记录里出现契约外的 类型/状态 组合",
                    "samples": blocking_pairs[:3],
                }
            )

        # 2) 同一事项两条现行版本：读的时候到底用哪条无法判断。
        duplicates = [
            dict(row)
            for row in db.execute(
                "SELECT owner_id, conversation_id, module_id, semantics_key, COUNT(*) AS n "
                "FROM memory_records WHERE semantics_version>=1 AND status='active' "
                "AND (effective_record_id IS NULL OR effective_record_id='') "
                "AND semantics_key IS NOT NULL AND semantics_key<>'' "
                "GROUP BY owner_id, conversation_id, module_id, semantics_key HAVING n>1"
            )
        ]
        facts["duplicate_current_versions"] = len(duplicates)
        if duplicates:
            problems.append(
                {"kind": "duplicate_current_version", "detail": "同一事项有多条现行版本", "samples": duplicates[:3]}
            )

        # 3) 悬空的现行版本引用：旧版本指到一条不存在的记录。
        dangling = [
            dict(row)
            for row in db.execute(
                "SELECT id, effective_record_id FROM memory_records "
                "WHERE effective_record_id IS NOT NULL AND effective_record_id<>'' "
                "AND effective_record_id NOT IN (SELECT id FROM memory_records)"
            )
        ]
        facts["dangling_effective_refs"] = len(dangling)
        if dangling:
            problems.append(
                {"kind": "dangling_effective_reference", "detail": "现行版本引用指向不存在的记录", "samples": dangling[:3]}
            )

        # 4) review_state 取值：新版按 none/pending/settled 读写。
        weird_review = [
            dict(row)
            for row in db.execute(
                "SELECT id, review_state FROM memory_records WHERE review_state IS NOT NULL"
            )
            if str(row["review_state"]) not in REVIEW_STATES
        ]
        facts["unknown_review_states"] = len(weird_review)
        if weird_review:
            problems.append({"kind": "unknown_review_state", "detail": "复核状态取值不在契约里", "samples": weird_review[:3]})

        # 5) record_version 必须有值且非负（新链路按它做版本保护）。
        bad_versions = db.execute(
            "SELECT COUNT(*) AS n FROM memory_records WHERE record_version IS NULL OR record_version<0"
        ).fetchone()["n"]
        facts["bad_record_versions"] = bad_versions
        if bad_versions:
            problems.append({"kind": "bad_record_version", "detail": "记录版本号为空或负数"})

        # 6) 七类内容类型是否只出现在新记录上（老记录混进新类型会让筛选混淆）。
        legacy_with_new_kinds = db.execute(
            "SELECT COUNT(*) AS n FROM memory_records WHERE semantics_version=0 AND kind IN "
            f"({','.join('?' for _ in CONTENT_KINDS)})",
            CONTENT_KINDS,
        ).fetchone()["n"]
        facts["legacy_rows_with_semantic_kinds"] = legacy_with_new_kinds

    return {
        "ok": not problems,
        "problems": problems,
        "facts": facts,
        "read_only": True,
        "notes": [
            "只读审计：sqlite 以 mode=ro 打开",
            "老陪伴记录（semantics_version=0）沿用旧枚举，不算阻断；只有新语义记录必须合规",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="记忆库与新版语义契约的只读兼容审计")
    parser.add_argument("--db", default=str(ROOT / "data" / "memory_records.db"))
    parser.add_argument("--out", default="", help="把结果另存为 JSON")
    args = parser.parse_args()
    report = audit(Path(args.db))
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
