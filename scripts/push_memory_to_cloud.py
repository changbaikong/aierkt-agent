"""把本机新增的对话轮次增量推给云端记忆系统。

用法：
    python scripts/push_memory_to_cloud.py            # 推一次
    python scripts/push_memory_to_cloud.py --dry-run  # 只报告要推几条

配套：云端 /opt/agent/app/cloud_ingest_api.py 接收 → cloud_memory_worker.py 提炼入库。
凭据从 .env 读 CLOUD_MEMORY_URL / CLOUD_INGEST_TOKEN；水位存在 data/cloud_push_state.json。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=False)

DB = Path(os.environ.get("MEMORY_DB_PATH", ROOT / "data" / "memory_records.db"))
STATE = Path(os.environ.get("CLOUD_PUSH_STATE", ROOT / "data" / "cloud_push_state.json"))
URL = os.environ.get("CLOUD_MEMORY_URL", "").rstrip("/")
TOKEN = os.environ.get("CLOUD_INGEST_TOKEN", "").strip()
BATCH = int(os.environ.get("CLOUD_PUSH_BATCH", "200"))


def _load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.part")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def _fetch_turns(since: str) -> list[dict]:
    """按 created_at 水位取增量记忆行，聚合成「轮」再推。"""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, content, created_at, metadata FROM memory_records "
        "WHERE created_at > ? ORDER BY created_at LIMIT ?",
        (since, BATCH),
    ).fetchall()
    conn.close()
    turns = []
    for row in rows:
        meta = {}
        try:
            meta = json.loads(row["metadata"] or "{}")
        except Exception:
            pass
        user_text = meta.get("user_text") or row["content"]
        turns.append(
            {
                "turn_id": row["id"],
                "user_text": user_text,
                "assistant_reply": meta.get("assistant_reply", ""),
                "run_id": meta.get("run_id", ""),
                "created_at": row["created_at"],
            }
        )
    return turns


def _post(turn: dict) -> bool:
    req = urllib.request.Request(
        f"{URL}/ingest",
        data=json.dumps(turn, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as exc:
        print(f"  云端拒绝 {turn['turn_id'][:8]}：HTTP {exc.code} {exc.read()[:200]!r}")
    except Exception as exc:
        print(f"  推送失败 {turn['turn_id'][:8]}：{type(exc).__name__}")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not URL or not TOKEN:
        print("缺 CLOUD_MEMORY_URL 或 CLOUD_INGEST_TOKEN，先配到 .env")
        return 3

    state = _load_state()
    since = state.get("last_created_at", "")
    turns = _fetch_turns(since)
    print(f"水位 {since or '（首次）'} → 待推 {len(turns)} 条")
    if args.dry_run or not turns:
        return 0

    ok = 0
    high = since
    for turn in turns:
        if _post(turn):
            ok += 1
            high = max(high, turn["created_at"])
        else:
            break  # 失败就停，下次从同一水位重来，不跳数据
    state["last_created_at"] = high
    state["last_push_at"] = turns[0]["created_at"]
    _save_state(state)
    print(f"成功推送 {ok}/{len(turns)} 条，水位推进到 {high}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
