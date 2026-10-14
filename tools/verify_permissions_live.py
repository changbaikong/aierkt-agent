"""账号级完全访问：只跑 API 层的端到端验证（不启前端构建产物）。

用途：确认 **真实 uvicorn 进程** 下 PUT/GET /api/permissions 的行为与
单测一致（单测用 TestClient，走的是同一份 app，但少了 lifespan 与
_initialize_runtime 的真实路径）。独立 DB + 独立端口，不碰用户数据。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = os.getenv("AGENT_PYTHON", sys.executable)
PORT = 8731
BASE = f"http://127.0.0.1:{PORT}"


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def main() -> int:
    scratch = Path(tempfile.mkdtemp(prefix="perm-verify-"))
    env = dict(os.environ)
    env.update({
        "AGENT_CORE_DB": str(scratch / "core.db"),
        "AGENT_WORK_DB": str(scratch / "work.db"),
        "WEB_HOST": "127.0.0.1",
        "WEB_PORT": str(PORT),
        "AGENT_DISABLED_MODULES": "media",  # 媒体模块要额外 DB，验证用不上
        "PYTHONPATH": str(ROOT),
    })

    log = open(scratch / "server.log", "w", encoding="utf-8")
    process = subprocess.Popen(
        [PYTHON, "-c",
         "import uvicorn, os; uvicorn.run('web_api:app', host='127.0.0.1', "
         f"port={PORT}, log_level='warning')"],
        cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT,
    )

    failures: list[str] = []
    try:
        # 等服务起来
        for _ in range(120):
            time.sleep(0.5)
            try:
                status, _body = call("GET", "/api/permissions?account_id=local-user")
                if status == 200:
                    break
            except Exception:
                continue
        else:
            print("SERVER DID NOT START")
            print((scratch / "server.log").read_text(encoding="utf-8", errors="replace")[-3000:])
            return 1

        # ① 默认标准模式
        status, body = call("GET", "/api/permissions?account_id=local-user")
        print(f"① GET default -> {status} configured={body.get('configured_mode')} "
              f"effective={body.get('effective_mode')} available={body.get('available')}")
        if status != 200 or body.get("effective_mode") != "standard":
            failures.append("default should be standard")
        # 2026-09-22 起排除表只剩 `work__plan_submit`：`work__approve` 整个从
        # 模型工具面删掉了（模型替用户批准高风险操作，会让两道闸门互相抵消），
        # 一个不存在的工具不需要被"排除"。
        if body.get("exclusions") != ["work__plan_submit"]:
            failures.append(f"exclusions wrong: {body.get('exclusions')}")

        # ② 开启完全访问
        status, body = call("PUT", "/api/permissions",
                            {"account_id": "local-user", "mode": "full_access"})
        print(f"② PUT full_access -> {status} configured={body.get('configured_mode')} "
              f"effective={body.get('effective_mode')} cleanup={body.get('cleanup')}")
        if status != 200 or body.get("effective_mode") != "full_access":
            failures.append("full access should be effective on loopback")
        if body.get("cleanup") is None:
            failures.append("cleanup payload missing")

        # ③ 再读一次，确认持久化
        status, body = call("GET", "/api/permissions?account_id=local-user")
        print(f"③ GET after switch -> {status} effective={body.get('effective_mode')}")
        if body.get("effective_mode") != "full_access":
            failures.append("mode did not persist")

        # ④ 别的账号不受影响
        status, body = call("GET", "/api/permissions?account_id=someone-else")
        print(f"④ GET other account -> effective={body.get('effective_mode')}")
        if body.get("effective_mode") != "standard":
            failures.append("other account leaked full access")

        # ⑤ 非法档位被拒
        status, body = call("PUT", "/api/permissions",
                            {"account_id": "local-user", "mode": "yolo"})
        print(f"⑤ PUT bad mode -> {status}")
        if status != 422:
            failures.append(f"bad mode should be 422, got {status}")

        # ⑥ 切回标准
        status, body = call("PUT", "/api/permissions",
                            {"account_id": "local-user", "mode": "standard"})
        print(f"⑥ PUT back to standard -> {status} effective={body.get('effective_mode')} "
              f"cleanup={body.get('cleanup')}")
        if body.get("effective_mode") != "standard":
            failures.append("switching back failed")
        if body.get("cleanup") is not None:
            failures.append("cleanup should not run when disabling")

    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()
        tail = (scratch / "server.log").read_text(encoding="utf-8", errors="replace")
        if failures:
            print("\n--- server log tail ---")
            print(tail[-2500:])

    if failures:
        print("\nFAILURES:")
        for item in failures:
            print("  -", item)
        return 1
    print("\nALL API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
