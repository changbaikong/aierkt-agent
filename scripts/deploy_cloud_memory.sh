#!/usr/bin/env bash
# 把云端记忆系统部署到服务器：解包、换服务单元、配账号口令。
#
# 用法（在服务器上以 root 跑）：
#   bash scripts/deploy_cloud_memory.sh /tmp/deploy.tar.gz
#
# 幂等：重复执行不会叠加出第二份服务。旧的单文件版（cloud_ingest_api.py /
# cloud_memory_worker.py）会被停掉并删除 —— 留着就会出现两个进程抢同一个
# 接收端口，表现为"有时候收得进去、有时候收不进去"。
set -euo pipefail

ARCHIVE="${1:-/tmp/deploy.tar.gz}"
APP=/opt/agent/app
VENV=/opt/agent/venv

echo "== 校验与解包 =="
test -f "$ARCHIVE" || { echo "找不到 $ARCHIVE"; exit 3; }
sha256sum "$ARCHIVE"
cd "$APP"
tar xzf "$ARCHIVE"
chmod +x "$VENV/bin/python"

echo "== 停用旧服务 =="
for unit in agent-ingest agent-memory agent-memory-api agent-memory-worker; do
  systemctl stop "$unit" 2>/dev/null || true
  systemctl disable "$unit" 2>/dev/null || true
done
rm -f "$APP/cloud_ingest_api.py" "$APP/cloud_memory_worker.py"

echo "== 账号口令 =="
if ! grep -q '^CLOUD_MEMORY_TOKENS=' "$APP/.env"; then
  # 沿用已有的单口令，但把它绑定到具体账号上。
  # 只用单口令的话任何拿到它的人都能读走任意账号的记忆 —— 越权读一次就是长期泄露。
  LEGACY="$(grep '^CLOUD_INGEST_TOKEN=' "$APP/.env" | head -1 | cut -d= -f2- || true)"
  if [ -z "$LEGACY" ]; then
    LEGACY="$("$VENV/bin/python" -c 'import secrets;print(secrets.token_urlsafe(24))')"
  fi
  printf 'CLOUD_MEMORY_TOKENS=%s:local-user\n' "$LEGACY" >> "$APP/.env"
  echo "已写入 CLOUD_MEMORY_TOKENS（账号 local-user）"
else
  echo "CLOUD_MEMORY_TOKENS 已存在，未改动"
fi
chmod 600 "$APP/.env"

echo "== 写服务单元 =="
cat > /etc/systemd/system/agent-memory-api.service <<'UNIT'
[Unit]
Description=Cloud Memory Sync API
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/agent/app
ExecStart=/opt/agent/venv/bin/python -m uvicorn cloud_memory.api:app --host 127.0.0.1 --port 8790
Restart=always
RestartSec=5
StandardOutput=append:/var/log/agent-memory-api.log
StandardError=append:/var/log/agent-memory-api.log

[Install]
WantedBy=multi-user.target
UNIT

cat > /etc/systemd/system/agent-memory-worker.service <<'UNIT'
[Unit]
Description=Cloud Memory Extraction Worker
After=network.target qdrant.service
Requires=qdrant.service

[Service]
Type=simple
WorkingDirectory=/opt/agent/app
ExecStart=/opt/agent/venv/bin/python -m cloud_memory.worker
Restart=always
RestartSec=10
StandardOutput=append:/var/log/agent-memory-worker.log
StandardError=append:/var/log/agent-memory-worker.log

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now agent-memory-api.service agent-memory-worker.service
sleep 8

echo "== 状态 =="
systemctl is-active agent-memory-api agent-memory-worker qdrant
echo "--- api"
tail -n 5 /var/log/agent-memory-api.log || true
echo "--- worker"
tail -n 5 /var/log/agent-memory-worker.log || true
