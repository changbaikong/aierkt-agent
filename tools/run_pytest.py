"""在 WorkBuddy shim 环境下可靠跑 pytest 的小工具。

为什么需要它：
- 本机 bash 没有 coreutils（ls/head/tail/date 全无），管道和 $(date) 都不能用；
- 全量 pytest 会撞 safe-delete 闸门（一个 turn 内批量删临时目录 > 50 条即 SystemExit），
  所以既要**按文件分块**跑，也要**每块一个全新的空临时根**（详见 `_fresh_temp_root`）；
- **测试产物一律写系统临时目录**：临时根、完整日志都放 `%TEMP%`，
  项目根目录只留终端摘要，避免仓库被动积累垃圾。
- 输出重定向到文件再读，比拿管道拿 stdout 稳（前台跑有概率被 SIGTERM）。

用法：
    python tools/run_pytest.py tests/test_permissions.py tests/test_web_api.py
    python tools/run_pytest.py --all
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys
import tempfile

PYTHON = os.getenv("AGENT_PYTHON", sys.executable)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 日志不落仓库：完整输出写系统临时目录，终端只给摘要与失败摘要。
LOG_DIR = os.path.join(tempfile.gettempdir(), "agent-pytest-logs")

# 失败时在终端回显的日志行数上限（完整日志在 LOG_DIR 里）。
_ERROR_TAIL = 25


def _fresh_temp_root(tag: str) -> str:
    """为这一块单独开一个**全新的空临时根**，返回它的路径。

    这是 safe-delete 闸门的根治办法，而不是"分块跑"本身：

    pytest 只保留最近 3 个 basetemp，更早的会改名成 `garbage-*`，并在
    **下一次运行开始时**递归删除。那批删除会被 WorkBuddy shell shim 的批量删除守卫
    按"一个 turn 内的删除条数"计数（阈值 50），超了就抛
    `SAFE_DELETE_BULK_REJECTED` + `SystemExit(1)` —— 看着像代码崩了，其实是环境闸门。
    实测 `%TEMP%\\pytest-of-<用户>\\garbage-*` 积到 393 个文件之后，
    **每个**用 `tmp_path` 的测试都会挂在 setup 阶段。

    把 `TEMP`/`TMP`/`TMPDIR` 都指到一个刚建出来的空目录，pytest 的父目录
    一开始就是空的 → 没有垃圾可清 → 守卫不触发。

    **用 `tempfile.mkdtemp()` 而不是"时间戳拼名字"**：mkdtemp 带随机后缀且会
    原子占位，同一秒内并发跑两块也拿不到同一个目录。

    **退出时不要删这个目录**：递归删除它本身又会撞上同一道闸门。
    """
    return tempfile.mkdtemp(prefix=f"pt-root-{tag}-")


def _collect_all() -> list[str]:
    """`--all`：递归收集本项目的测试，**包含矩阵测试**（`tests/matrices/`）。

    必须自己过滤 `legacy_backend`：`pytest.ini` 的 `norecursedirs` 只在使用
    目录参数时生效，一旦显式传文件路径就不再起作用，收集阶段照样会因缺
    backend 模块报错。
    """
    files = glob.glob(os.path.join(ROOT, "tests", "**", "test_*.py"), recursive=True)
    keep = []
    for path in sorted(files):
        if os.sep + "legacy_backend" + os.sep in path:
            continue
        if os.sep + "__pycache__" + os.sep in path:
            continue
        if os.path.basename(path) == "conftest.py":
            continue
        keep.append(path)
    return keep


def run(files: list[str], label: str = "") -> tuple[int, str, str]:
    """跑一块测试，返回 (退出码, 完整输出, 日志路径)。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    tag = label or "adhoc"
    temp_root = _fresh_temp_root(tag)
    basetemp = os.path.join(temp_root, "basetemp")
    log_path = os.path.join(LOG_DIR, f"{tag}.txt")
    cmd = [
        PYTHON,
        "-m",
        "pytest",
        *files,
        "-q",
        "--no-header",
        "-p",
        "no:cacheprovider",
        f"--basetemp={basetemp}",
    ]
    # 只改子进程的环境：父进程（以及调用方的 TEMP）保持原样。
    child_env = dict(os.environ)
    child_env["TEMP"] = child_env["TMP"] = child_env["TMPDIR"] = temp_root
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.run(
            cmd, cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT, text=True
        )
    with open(log_path, encoding="utf-8", errors="replace") as log:
        output = log.read()
    return proc.returncode, output, log_path


def _summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        if "passed" in line or "failed" in line or "error" in line:
            return line.strip()
    return (output.splitlines() or ["<no output>"])[-1].strip()


def _error_excerpt(output: str) -> str:
    """失败时挑出可读的几行：优先 FAILED/ERROR 行，没有就回退到最后几行。"""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    hits = [line for line in lines if line.startswith(("FAILED", "ERROR", "E   "))]
    if not hits:
        hits = lines[-_ERROR_TAIL:]
    return "\n".join(hits[: _ERROR_TAIL * 2])


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--all" in sys.argv or not args:
        files = _collect_all()
        chunks = [files[i : i + 8] for i in range(0, len(files), 8)]
    else:
        chunks = [args]

    worst = 0
    for index, chunk in enumerate(chunks, 1):
        code, output, log_path = run(chunk, label=f"block{index:02d}")
        print(f"[block{index:02d}] {_summary(output)}  (log: {log_path})")
        if code != 0:
            print(f"--- block{index:02d} failures ---")
            print(_error_excerpt(output))
        worst = max(worst, code)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
