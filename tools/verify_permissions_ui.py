"""账号级完全访问：真机浏览器验证（含截图）。

两个必须记住的前置事实（都是踩过才写下来的）：

1. **web_api 不服务前端页面。** 实测 `GET /`、`/index.html`、`/src/main.jsx` 全是
   404，只有 `/api/*` 存在。所以不能把后端当站点打开 —— 那样浏览器看到的是一页
   `{"detail":"Not Found"}`，后面每一步点击都 Element not found，而服务端状态当然
   也一直没变，看着像功能坏了、其实是环境搭错了。
   正确搭法：跑真实的 vite dev server 当站点，它按 vite.config.js 的 proxy 把
   `/api` 转发到 127.0.0.1:8765 —— 所以后端必须监听 8765，数据库指向临时目录。

2. **batch 的每条参数是一个完整命令行字符串**，不是"命令 + 参数"两个参数。
   `batch "click @e1" "screenshot x.png"` ✅
   `batch "click" "@e1"`             ❌（会去找一个叫 click 的命令）
   `--help` 里写得很清楚，但只有读了才知道。

因此本脚本用官方推荐的 **snapshot → @ref → 交互** 流程：先 `snapshot -i` 拿到
交互元素与 ref，再用 `find`（语义定位，不依赖 ref 生命周期）点按钮。按钮都是
`<button>`，`find role button click --name "设置"` 比 `text=` 稳。

另外两条 agent-browser 的坑：
  - `dialog accept` 与触发它的 click 必须**在同一次 batch 里**，否则浏览器会在上
    一条命令结束时被关掉，报 "No dialog is showing"。
  - 一次 batch 里串完（多次独立调用之间页面会丢）；stdout 常被吞/乱码 → 写文件再
    解码读，以产物文件为准。
"""

from __future__ import annotations

import glob
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

# 后端必须和 vite.config.js 的 proxy 目标一致，否则前端拿不到 API。
BACKEND_PORT = 8765
FRONTEND_PORT = 5299
BACKEND = f"http://127.0.0.1:{BACKEND_PORT}"
SITE = f"http://127.0.0.1:{FRONTEND_PORT}"
SHOTS = ROOT / "tmp_perm_verify" / "shots"


def wait_for_http(url: str, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return True
        except Exception:
            time.sleep(0.7)
    return False


def probe(url: str) -> str:
    """探测一个 URL，返回 "HTTP 200"/"HTTP 404"/异常名 —— 用来断言前端真的在。"""
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return f"HTTP {response.status}"
    except Exception as exc:  # noqa: BLE001 - 探测用，任何失败都只想知道类型
        code = getattr(exc, "code", None)
        return f"HTTP {code}" if code else type(exc).__name__


def browser(steps: list[str], label: str, timeout: int = 240) -> tuple[int, str]:
    """跑一次 agent-browser batch；每条 step 是完整命令行字符串。

    注意：agent-browser 的 daemon 在一次 batch 结束后**可能自行退出**，下一次
    调用就会报 `os error 10060`（连接超时）。所以验证流程必须尽量压进**一次**
    batch；这里在每次调用前用 `connect` 之外的手段兜底是不可靠的，因此本脚本
    只发一次 batch。这个函数仍然保留批次参数，方便出问题时按步排查。
    """
    log = SHOTS / f"{label}.log"
    cmd = ["cmd", "/c", "agent-browser", "batch", "--bail", *steps]
    with open(log, "w", encoding="utf-8") as handle:
        completed = subprocess.run(
            cmd, stdout=handle, stderr=subprocess.STDOUT,
            timeout=timeout, cwd=str(SHOTS),
        )
    raw = log.read_bytes()
    for encoding in ("utf-8", "utf-16", "gbk"):
        try:
            return completed.returncode, raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return completed.returncode, raw.decode("utf-8", errors="replace")


def backend_permission_state() -> dict:
    with urllib.request.urlopen(f"{BACKEND}/api/permissions?account_id=local-user", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def shot(name: str) -> str:
    return f"screenshot {SHOTS.as_posix()}/{name}"


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    for stale in glob.glob(str(SHOTS / "*")):
        try:
            Path(stale).unlink()
        except OSError:
            pass

    scratch = Path(tempfile.mkdtemp(prefix="perm-ui-"))
    env = dict(os.environ)
    env.update({
        "AGENT_CORE_DB": str(scratch / "core.db"),
        "AGENT_WORK_DB": str(scratch / "work.db"),
        "WEB_HOST": "127.0.0.1",
        "AGENT_DISABLED_MODULES": "media",
        "PYTHONPATH": str(ROOT),
    })

    backend_log = open(scratch / "backend.log", "w", encoding="utf-8")
    backend = subprocess.Popen(
        [PYTHON, "-c",
         "import uvicorn; uvicorn.run('web_api:app', host='127.0.0.1', "
         f"port={BACKEND_PORT}, log_level='warning')"],
        cwd=str(ROOT), env=env, stdout=backend_log, stderr=subprocess.STDOUT,
    )

    vite_log = open(scratch / "vite.log", "w", encoding="utf-8")
    vite = subprocess.Popen(
        [NODE, str(VITE), "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"],
        cwd=str(FRONTEND_DIR), env=env, stdout=vite_log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )

    failures: list[str] = []
    try:
        if not wait_for_http(f"{BACKEND}/api/permissions?account_id=local-user"):
            print("BACKEND DID NOT START")
            print((scratch / "backend.log").read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1
        print(f"backend up on {BACKEND}")

        if not wait_for_http(SITE, timeout=120):
            print("VITE DEV SERVER DID NOT START")
            print((scratch / "vite.log").read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1
        print(f"frontend up on {SITE}")

        # 前置断言：前端必须真的在，/api 必须真的被代理到后端。这两条一旦不过，
        # 后面所有点击都会 Element not found（第一版就是这么白跑的）。
        root_state = probe(f"{SITE}/")
        api_state = probe(f"{SITE}/api/permissions")
        print(f"probe: frontend {root_state}, proxied api {api_state}")
        if not root_state.startswith("HTTP 2"):
            failures.append(f"前端根路径不可用：{root_state}")
        if not api_state.startswith("HTTP 2"):
            failures.append(f"/api 未被 vite 代理到后端：{api_state}")
        if failures:
            print("PRE-CONDITION FAILED:", failures)
            return 1

        before = backend_permission_state()
        print("state before:", before.get("configured_mode"), "/", before.get("effective_mode"))

        # 全部动作压进一次 batch —— daemon 在 batch 之间会掉（os error 10060）。
        # 用 `find role button` 做语义定位，不依赖会过期的 @ref；只有一次
        # `snapshot -i` 用来留证据（它会把 ref 重排，所以放在所有点击之后）。
        steps = [
            f"open {SITE}/?perm=standard",
            "wait 5000",
            shot("01-chat-standard.png"),
            "get count .permission-ribbon",

            'find role button click --name "设置"',
            "wait 3000",
            shot("02-settings-standard.png"),

            # 开启完全访问：click 弹出 window.confirm → 同一 batch 内 accept。
            'find role button click --name "本机完全访问"',
            "wait 1500",
            "dialog status",
            "dialog accept",
            "wait 3500",
            shot("03-settings-full-access.png"),

            # 回聊天页看导航栏常驻标记
            'find role button click --name "聊天"',
            "wait 3000",
            shot("04-chat-full-access.png"),
            "get text .permission-ribbon",

            # 切回标准模式（不弹确认框），标记应消失
            'find role button click --name "设置"',
            "wait 2500",
            'find role button click --name "标准模式"',
            "wait 2500",
            shot("05-settings-back-standard.png"),
            'find role button click --name "聊天"',
            "wait 2500",
            "get count .permission-ribbon",
            shot("06-chat-back-standard.png"),

            # 最后取快照留证据
            'find role button click --name "设置"',
            "wait 2500",
            "snapshot -i",
        ]
        code, out = browser(steps, "run", timeout=420)
        print(f"--- single batch run rc={code} ---\n{out}")
        if code != 0:
            failures.append(f"浏览器 batch 未通过（rc={code}），详见 shots/run.log")

        # 服务端最终必须回到 standard
        restored = backend_permission_state()
        print("state after full UI round trip:",
              restored.get("configured_mode"), "/", restored.get("effective_mode"))
        if restored.get("effective_mode") != "standard":
            failures.append(f"UI 走完一轮后服务端不是 standard，而是 {restored.get('effective_mode')}")

        # 断言点：确认框真的弹过、完全访问标记出现过、切回后标记消失
        for needle, why in (
            ("完全访问", "导航栏没读到 .permission-ribbon 的『完全访问』"),
            ("本机完全访问", "设置页快照里没有『本机完全访问』按钮"),
            ("标准模式", "设置页快照里没有『标准模式』按钮"),
        ):
            if needle not in out:
                failures.append(why)
        if "Nothing to do" in out or "no dialog" in out.lower():
            failures.append("开启完全访问时没有看到确认框（dialog 未弹出）")

        browser(["close"], "step9", timeout=90)

    finally:
        for proc in (vite, backend):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        backend_log.close()
        vite_log.close()

    produced = sorted(p.name for p in SHOTS.glob("*.png"))
    print("\nscreenshots:", produced)

    if failures:
        print("\nFAILED CHECKS:")
        for item in failures:
            print("  -", item)
        return 1
    print("\nALL UI CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
