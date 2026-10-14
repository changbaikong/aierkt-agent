#!/usr/bin/env bash
# 清掉验收专用账号在云端留下的数据。
#
# 验收会在云端真的写下记录（不然验不出东西），但它属于一次性产物，
# 验收完就该收拾干净。只按账号删，本地那位用户的记忆一条都不碰。
#
# 用法（服务器上以 root 跑）：
#   bash scripts/cloud_memory_cleanup_probe.sh [账号]     # 默认 acceptance-probe
set -euo pipefail

OWNER="${1:-acceptance-probe}"
APP=/opt/agent/app

if [ "$OWNER" = "local-user" ]; then
  echo "拒绝：local-user 是真实账号，这个脚本只清验收账号"
  exit 4
fi

cd "$APP"
/opt/agent/venv/bin/python - "$OWNER" <<'PY'
import sys

from dotenv import load_dotenv

from agent_core.memory_repositories import MemoryRepositories
from agent_core.vector_config import QdrantConfig

load_dotenv("/opt/agent/app/.env", override=False)

from cloud_memory.store import CloudMemoryStore  # noqa: E402  (要在 dotenv 之后)

owner = sys.argv[1]
db = "/opt/agent/app/data/memory_records.db"

store = CloudMemoryStore(db)
repos = MemoryRepositories(db, vector_config=QdrantConfig.from_env())

with repos.connect() as conn:
    ids = [row[0] for row in conn.execute("SELECT id FROM memory_records WHERE owner_id=?", (owner,))]
print(f"账号 {owner}：{len(ids)} 条记录")

# 向量先删：记录删了之后就没有 id 可查，向量会永远留在集合里占位。
for record_id in ids:
    for layer in ("hot", "warm", "cold"):
        try:
            repos.vector_client.delete(f"{layer}_memories_vectors", [record_id])
        except Exception:
            pass

with store.connect() as conn:
    conn.execute("DELETE FROM memory_records WHERE owner_id=?", (owner,))
    conn.execute("DELETE FROM memory_ingest_queue WHERE owner_id=?", (owner,))
    conn.execute("DELETE FROM memory_changes WHERE owner_id=?", (owner,))
    conn.execute("DELETE FROM memory_access_log WHERE owner_id=?", (owner,))
print("已清理")
PY
