"""Start the local Web API and wait until its health endpoint is reachable."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen


def _health(url: str) -> tuple[str, dict]:
    """探测健康接口。

    三种结果：

    * ``healthy`` / ``degraded`` 之外的 **有响应** 状态 → 原样返回，调用方据此判"端口被占"；
    * ``unhealthy`` —— 有 HTTP 响应，但内容不是我们的接口（多半是别的服务）；
    * ``not_listening`` —— 连不上，可以往里起。

    注意：连接失败抛的是 ``TimeoutError`` / ``ConnectionError``（``OSError`` 家族），
    **不是** ``URLError``，所以不能靠 ``except URLError`` 判定"没在监听"——
    否则空端口会被误报成 ``unhealthy``，调用方会当成"端口被占"，直接放弃启动。
    """
    try:
        with urlopen(url, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = str(payload.get("status") or "unknown")
            return status, payload
    except json.JSONDecodeError as exc:
        return "unhealthy", {"error": f"响应不是 JSON：{exc}"}
    except Exception as exc:
        return "not_listening", {"error": str(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start agentcore Web API and wait for health")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    root = Path(__file__).resolve().parent
    address = f"http://{args.host}:{args.port}"
    health_url = f"{address}/api/health"
    status, detail = _health(health_url)
    if status != "not_listening":
        print(f"Backend already responds at {address}: {status}")
        return 0 if status in {"healthy", "degraded"} else 2

    process = subprocess.Popen(
        [sys.executable, str(root / "web_api.py")],
        cwd=str(root),
        # web_api.py 的端口读 WEB_PORT（默认 8765）。不传的话 --port 会被静默忽略：
        # 探测的是自定义端口，服务却绑在 8765 上。
        env={**os.environ, "WEB_PORT": str(args.port)},
    )

    def stop(_signum, _frame):
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    deadline = time.monotonic() + max(0.1, args.timeout)
    last_detail = detail
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print(f"Backend exited before becoming healthy (code {process.returncode}).")
            return int(process.returncode or 1)
        status, last_detail = _health(health_url)
        if status in {"healthy", "degraded"}:
            print(f"Backend ready at {address} ({status}).")
            try:
                return process.wait()
            except KeyboardInterrupt:
                stop(None, None)
                return 130
        if status != "not_listening":
            print(f"Backend is listening at {address} but reports {status}: {last_detail}")
            return 2
        time.sleep(0.25)

    process.terminate()
    print(f"Backend did not become ready within {args.timeout:.1f}s: {last_detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
