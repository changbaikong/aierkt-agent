"""一条命令重跑全部离线验收，汇总成一份结果。

【为什么要有这个入口】
六份脚本各自能跑，但"今天到底跑全了没有"这件事没有人管：漏跑一份，通过数看着
还是绿的。这里把六份脚本 + 记忆相关测试一次跑完，逐份对预期条数，写一份汇总证据，
只有全部通过才返回 0。

【默认不跑什么】
两条真实模型脚本（`verify_memory_maintenance_live.py`、`verify_memory_answer_chain_live.py`）
会真的调模型、真的搜外网：默认**不跑**，需要时显式加 `--with-live`。
生产和正式配置一个都不碰：所有脚本都用临时库/临时目录。

用法：
    python scripts/verify_memory_acceptance_all.py                # 六份脚本 + 记忆测试
    python scripts/verify_memory_acceptance_all.py --no-tests     # 只跑六份脚本
    python scripts/verify_memory_acceptance_all.py --with-live    # 额外跑两条真实模型脚本
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 离线验收脚本与各自的预期条数。预期写死在这里：脚本少跑几条也要能被发现。
OFFLINE_SCRIPTS: tuple[tuple[str, str, int], ...] = (
    ("原 B 边界反例", "verify_memory_b_acceptance_edges.py", 11),
    ("内容维护反例（BM-R1—BM-R6）", "verify_memory_maintenance_acceptance_edges.py", 6),
    ("离线维护闭环", "verify_memory_maintenance_loop.py", 9),
    ("搜索与记忆融合", "verify_memory_search_fusion.py", 24),
    ("同一包两端语义对照", "verify_memory_dual_end_semantics.py", 20),
    ("走真实 HTTP 的双端闭环", "verify_memory_dual_end_http.py", 12),
)

#: 真实模型脚本：会调模型、搜外网，默认不跑。预期只记条数，成败看脚本自己的返回码。
LIVE_SCRIPTS: tuple[tuple[str, str], ...] = (
    ("真实模型维护样例", "verify_memory_maintenance_live.py"),
    ("真实四轮回答链路", "verify_memory_answer_chain_live.py"),
)

MEMORY_TEST_GLOB = ("test_memory_*.py", "test_cloud_memory*.py")
MEMORY_TEST_FILES = ("test_agent_core_memory.py", "test_module_memory_isolation.py")


def run_script(name: str, relative: str, *, timeout: int = 1800) -> dict:
    """跑一份脚本并解析它打出来的 JSON。"""
    path = ROOT / "scripts" / relative
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    seconds = round(time.monotonic() - started, 1)
    report: dict = {}
    try:
        report = json.loads(completed.stdout or "{}")
    except ValueError:
        report = {"parse_error": (completed.stdout or "")[-400:]}
    return {
        "name": name,
        "script": f"scripts/{relative}",
        "exit_code": completed.returncode,
        "seconds": seconds,
        "passed": report.get("passed"),
        "total": report.get("total"),
        "ok": bool(report.get("ok")),
        "stderr_tail": (completed.stderr or "")[-300:] if completed.returncode else "",
    }


def memory_test_files() -> list[str]:
    paths: list[str] = []
    for pattern in MEMORY_TEST_GLOB:
        paths.extend(str(item) for item in sorted((ROOT / "tests").glob(pattern)))
    paths.extend(str(ROOT / "tests" / name) for name in MEMORY_TEST_FILES)
    return paths


def run_memory_tests(*, timeout: int = 3600) -> dict:
    files = memory_test_files()
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *files, "-q", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    tail = output.strip().splitlines()[-1] if output.strip() else ""
    return {
        "files": len(files),
        "exit_code": completed.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "summary": tail[-200:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="重跑全部离线验收并汇总")
    parser.add_argument("--no-tests", action="store_true", help="跳过记忆相关测试")
    parser.add_argument("--with-live", action="store_true", help="额外跑两条真实模型脚本")
    parser.add_argument("--out", default="docs/memory-acceptance-summary.json", help="汇总证据写到哪")
    args = parser.parse_args()

    results: list[dict] = []
    problems: list[str] = []

    for name, relative, expected in OFFLINE_SCRIPTS:
        item = run_script(name, relative)
        item["expected_total"] = expected
        item["ok_all"] = item["exit_code"] == 0 and item["ok"] and item["total"] == expected
        results.append(item)
        if item["exit_code"] != 0 or not item["ok"]:
            problems.append(f"{relative} 未通过（退出码 {item['exit_code']}）")
        elif item["total"] != expected:
            problems.append(f"{relative} 条数不对：预期 {expected}，实际 {item['total']}")
        print(
            f"[{'OK ' if item['ok_all'] else 'BAD'}] {name}: "
            f"{item['passed']}/{item['total']}（{item['seconds']}s）"
        )

    tests = None
    if not args.no_tests:
        tests = run_memory_tests()
        print(f"[{'OK ' if tests['exit_code'] == 0 else 'BAD'}] 记忆相关测试 {tests['files']} 个文件: {tests['summary']}")
        if tests["exit_code"] != 0:
            problems.append("记忆相关测试未通过")

    live: list[dict] = []
    if args.with_live:
        for name, relative in LIVE_SCRIPTS:
            item = run_script(name, relative, timeout=3600)
            live.append(item)
            print(f"[{'OK ' if item['exit_code'] == 0 else 'BAD'}] {name}: 退出码 {item['exit_code']}（{item['seconds']}s）")
            if item["exit_code"] != 0:
                problems.append(f"{relative} 未通过（真实模型脚本）")

    summary = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": not problems,
        "problems": problems,
        "offline_scripts": results,
        "memory_tests": tests,
        "live_scripts": live,
        "production_memory_touched": False,
        "notes": [
            "所有离线脚本用临时库/临时目录，不碰生产记忆与正式配置",
            "真实模型脚本默认不跑（会调模型、搜外网），加 --with-live 才跑",
            "生产云端部署与双端生产数据对照不在本入口覆盖范围内",
        ],
    }
    target = ROOT / args.out
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": summary["ok"], "problems": problems, "evidence": str(target.relative_to(ROOT))}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
