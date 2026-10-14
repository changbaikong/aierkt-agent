"""首次接入：核对本地库与云端库的差异，以本地为基线对齐，云端独有的一条不删。

【在哪跑】
**在服务器上跑**，因为要同时看到两个库文件。本机的库先用 SQLite 在线备份
导出一份干净的单文件，传上去：

    # 本机（agent 停掉之后）
    python -c "import sqlite3;s=sqlite3.connect('data/memory_records.db');d=sqlite3.connect('/tmp/local-memory.db');s.backup(d);d.close();s.close()"
    scp -i ~/.ssh/<你的密钥名> /tmp/local-memory.db root@<服务器>:/tmp/

    # 服务器
    python scripts/cloud_memory_baseline.py --local-db /tmp/local-memory.db            # 只核对
    python scripts/cloud_memory_baseline.py --local-db /tmp/local-memory.db --apply    # 对齐

【为什么不用连 HTTP】
对齐的产物是云端变更流水里的新条目。本地的正式客户端下一次同步（启动时就会跑一次）
自然会把这些变更拉回去并合并，不需要在这里替它做一遍 —— 少一条路径就少一处
"这里对齐了、那边没对齐"的可能。

【为什么先备份】
这一步会改写云端的记忆记录，而云端那份是正本。备份是这类操作唯一能回退的手段。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

from cloud_memory.store import PAYLOAD_COLUMNS, CloudMemoryStore  # noqa: E402

DEFAULT_CLOUD_DB = "/opt/agent/app/data/memory_records.db"
COMPARED_COLUMNS = tuple(PAYLOAD_COLUMNS)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def backup(db_path: Path, *, label: str) -> Path | None:
    """用 SQLite 在线备份接口导出一份干净的单文件。

    直接复制正在使用的库会拿到「主文件是新的、WAL 还是旧的」这种残缺快照，
    恢复时表现为少掉最近一批写入 —— 而且不报错。
    """
    if not db_path.exists():
        return None
    target = db_path.with_name(f"{db_path.stem}.backup-{label}-{_stamp()}.db")
    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def read_records(db_path: Path, *, owner_id: str) -> dict[str, dict]:
    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM memory_records WHERE owner_id=?", (owner_id,)).fetchall()
    return {str(row["id"]): dict(row) for row in rows}


def _differs(local: dict, cloud: dict) -> list[str]:
    changed = []
    for column in COMPARED_COLUMNS:
        left, right = local.get(column), cloud.get(column)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            if float(left) == float(right):
                continue
        elif str(left) == str(right):
            continue
        changed.append(column)
    return changed


def reconcile(local_records: dict[str, dict], cloud_records: dict[str, dict]) -> dict:
    local_only = sorted(set(local_records) - set(cloud_records))
    cloud_only = sorted(set(cloud_records) - set(local_records))
    differing = []
    for record_id in sorted(set(local_records) & set(cloud_records)):
        changed = _differs(local_records[record_id], cloud_records[record_id])
        if changed:
            differing.append({"id": record_id, "columns": changed})
    return {"local_only": local_only, "cloud_only": cloud_only, "differing": differing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--local-db", required=True, help="本机库的副本路径")
    parser.add_argument("--cloud-db", default=DEFAULT_CLOUD_DB, help="云端库路径")
    parser.add_argument("--owner", default="local-user")
    parser.add_argument("--apply", action="store_true", help="真的执行对齐；不加则只核对")
    parser.add_argument("--json", default="", help="把核对结果写成 JSON")
    args = parser.parse_args()

    local_path = Path(args.local_db)
    cloud_path = Path(args.cloud_db)
    if not local_path.exists():
        print(f"本机库副本不存在：{local_path}")
        return 3
    if not cloud_path.exists():
        print(f"云端库不存在：{cloud_path}")
        return 3

    print("备份：")
    # 备份放云端库同一个目录下，方便一起管理。
    for label, path in (("cloud", cloud_path), ("local", local_path)):
        target = backup(path, label=label)
        print(f"  {label}: {target if target else '（文件不存在，跳过）'}")

    local_records = read_records(local_path, owner_id=args.owner)
    cloud = CloudMemoryStore(cloud_path)
    cloud.initialize()
    cloud_records = cloud.existing_records(owner_id=args.owner)
    report = reconcile(local_records, cloud_records)

    print(f"\n核对（账号 {args.owner}）：本地 {len(local_records)} 条 / 云端 {len(cloud_records)} 条")
    print(f"  只在本地：{len(report['local_only'])}")
    print(f"  只在云端：{len(report['cloud_only'])}（原样保留，不删）")
    print(f"  两边都有但不同：{len(report['differing'])}")
    for item in report["cloud_only"][:10]:
        print(f"    云端独有：{item}")
    for item in report["differing"][:10]:
        print(f"    差异：{item['id']} 列={','.join(item['columns'])}")

    result = {"owner": args.owner, "diff": report, "applied": bool(args.apply)}
    if args.apply:
        # 只差 updated_at 的不动：内容一模一样，硬把本地那个较旧的时间戳推上去，
        # 只会让云端的时间往回走，还会凭空多出一批变更发给本地 ——
        # 本地拿到之后发现内容没变，等于白跑一趟。
        timestamps_only = {
            item["id"] for item in report["differing"] if set(item["columns"]) <= {"updated_at"}
        }
        content_diff = {
            item["id"] for item in report["differing"] if set(item["columns"]) - {"updated_at"}
        }
        targets = [
            record for record_id, record in local_records.items()
            if record_id in report["local_only"] or record_id in content_diff
        ]
        print(f"\n需要对齐 {len(targets)} 条"
              f"（其中被跳过的「仅时间戳不同」{len(timestamps_only)} 条）")

        inserted = updated = same = 0
        for record in targets:
            outcome = cloud.import_record(record)
            inserted += outcome == "inserted"
            updated += outcome == "updated"
            same += outcome == "same"
        result["import"] = {
            "inserted": inserted, "updated": updated, "same": same,
            "skipped_timestamp_only": len(timestamps_only),
        }
        print(f"基线导入：新增 {inserted} / 更新 {updated} / 一致 {same}")
        print(f"云端变更编号推进到 {cloud.max_seq(owner_id=args.owner)}")
        print("本地客户端下次同步（启动时会自动跑一次）就会把这些变更拉回去。")
    else:
        print("\n（只核对，没有改动任何数据。加 --apply 才会对齐。）")

    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n核对结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
