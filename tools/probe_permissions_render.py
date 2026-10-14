"""探针：绕开 window.confirm，直接验证「完全访问」状态下前端的真实渲染。

背景：`find role button click --name "本机完全访问"` 会弹出原生 window.confirm，
在 agent-browser 里这个原生模态会把 CDP 通道掐掉（命令 8 直接报 os error 10060）。
也就是说 —— **原生确认框一弹，agent-browser 就没法在同一次 batch 里接着走**。

所以这个探针换路子：用 `eval` 直接发 PUT 切模式（跳过确认框），再 reload 看渲染。
它验证的是"前端在 full_access 下显示对不对"，与"确认框会不会弹"分开验。
确认框那一层由 SettingsPage.jsx 的 `if (nextMode === PERMISSION_FULL_ACCESS)
{ if (!window.confirm(FULL_ACCESS_WARNING)) return }` 保证，且 JS 单测覆盖了文案。
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "fronted" / "arcueid_daily_50" / "fronteddd"
PYTHON = os.getenv("AGENT_PYTHON", sys.executable)
NODE = os.getenv("AGENT_NODE", "node")
VITE = FRONTEND_DIR / "node_modules" / "vite" / "bin" / "vite.js"
SHOTS = ROOT / "tmp_perm_verify" / "shots"
BACKEND = "http://127.0.0.1:8765"
SITE = "http://127.0.0.1:5299"

# eval 的 JS 必须走 base64（`eval -b`）——文档明确说这是"避免 shell 转义问题"的
# 方式。直接内联时 cmd /c 会把 JS 里的 `/` 当开关解析，报
# `SyntaxError: Invalid regular expression flags`，看着像 JS 写错，其实是转义被降级。
BODY = json.dumps({"account_id": "local-user", "mode": "full_access"})
JS = (
    "fetch('/api/permissions',{method:'PUT',"
    "headers:{'Content-Type':'application/json'},"
    f"body:{json.dumps(BODY)}}}).then(function(r){{return r.json()}})"
    ".then(function(j){return j.effective_mode+'/'+j.configured_mode})"
)
JS_B64 = base64.b64encode(JS.encode("utf-8")).decode("ascii")


def wait_for(url: str, timeout: float = 100.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=4)
            return True
        except Exception:
            time.sleep(0.7)
    return False


def state() -> str:
    with urllib.request.urlopen(f"{BACKEND}/api/permissions?account_id=local-user", timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return f"{data.get('configured_mode')}/{data.get('effective_mode')}"


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="perm-probe-"))
    env = dict(os.environ)
    env.update({
        "AGENT_CORE_DB": str(scratch / "core.db"),
        "AGENT_WORK_DB": str(scratch / "work.db"),
        "WEB_HOST": "127.0.0.1",
        "AGENT_DISABLED_MODULES": "media",
        "PYTHONPATH": str(ROOT),
    })

    backend = subprocess.Popen(
        [PYTHON, "-c",
         "import uvicorn; uvicorn.run('web_api:app', host='127.0.0.1', port=8765, log_level='warning')"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    vite = subprocess.Popen(
        [NODE, str(VITE), "--host", "127.0.0.1", "--port", "5299", "--strictPort"],
        cwd=str(FRONTEND_DIR), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    try:
        if not wait_for(f"{BACKEND}/api/permissions"):
            print("BACKEND DID NOT START"); return 1
        if not wait_for(SITE):
            print("VITE DID NOT START"); return 1
        print("servers up; state:", state())

        steps = [
            f"open {SITE}/?p=probe",
            "wait 5000",
            "get count .permission-ribbon",
            f"eval -b {JS_B64}",
            "wait 2500",
            "reload",
            "wait 5000",
            "get count .permission-ribbon",
            "get text .permission-ribbon",
            f"screenshot {SHOTS.as_posix()}/probe-chat-full-access.png",
            'find role button click --name "设置"',
            "wait 3500",
            "get text .permission-badge",
            "get text .permission-status",
            'find role button click --name "标准模式"',
            "wait 3000",
            "get count .permission-ribbon",
            f"screenshot {SHOTS.as_posix()}/probe-settings-back-standard.png",
        ]
        log = scratch / "ab.log"
        with open(log, "w", encoding="utf-8") as handle:
            done = subprocess.run(
                ["cmd", "/c", "agent-browser", "batch", "--bail", *steps],
                stdout=handle, stderr=subprocess.STDOUT, timeout=420, cwd=str(SHOTS),
            )
        raw = log.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        for encoding in ("utf-8", "utf-16", "gbk"):
            try:
                text = raw.decode(encoding); break
            except UnicodeDecodeError:
                continue
        print(f"--- probe rc={done.returncode} ---")
        print(text)
        print("final backend state:", state())
        return done.returncode
    finally:
        for proc in (vite, backend):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        subprocess.run(["cmd", "/c", "agent-browser", "close"], capture_output=True, timeout=90)


if __name__ == "__main__":
    sys.exit(main())
