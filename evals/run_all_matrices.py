"""统一 runner：一次跑全 7 套矩阵 + 汇总评分。

对应酥主"同时测试"要求。编排：
  1. 套 1-6 的单元部分（tests/matrices/test_matrix_*.py）：一次 subprocess 调 pytest，
     --basetemp 指向全新临时目录（规避 Windows safe-delete 闸门），--junit-xml 生成结果。
  2. 套 5/6/7 的 LLM 部分（evals/matrix_*.py）：各自 subprocess，读 JSONL。
  3. 聚合：解析 junit XML + 3 份 JSONL → all_matrices.json + all_matrices.md。
     红线 case 任一失败 → 总报告标 RED_LINE_FAIL 且退出码非 0。

CLI：
  --only M1 M5        只跑指定套
  --skip-llm          只跑单元（CI 默认，无 API key 依赖）
  --output-dir DIR    结果目录（默认 evals/results）
  --limit N           每套 LLM 限 case 数（控成本）
  --python PATH       指定 python 解释器（默认本进程 sys.executable）

跑法：
  python evals/run_all_matrices.py                  # 全 7 套（LLM 需 key）
  python evals/run_all_matrices.py --skip-llm        # 只单元（CI）
  python evals/run_all_matrices.py --only M1 M2      # 只跑指定套
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals._common.runtime import new_basetemp

PYTEST_INI = PROJECT_ROOT / "pytest.ini"
MATRICES_DIR = PROJECT_ROOT / "tests" / "matrices"

# 套 → (单元测试文件, LLM 脚本)
MATRIX_MAP: dict[str, dict[str, str]] = {
    "M1": {"unit": "test_matrix_routing.py"},
    "M2": {"unit": "test_matrix_intent.py"},
    "M3": {"unit": "test_matrix_tools.py"},
    "M4": {"unit": "test_matrix_plan.py"},
    "M5": {"unit": "test_matrix_subagent.py", "llm": "matrix_subagent_security.py"},
    "M6": {"unit": "test_matrix_cross_channel.py", "llm": "matrix_cross_channel.py"},
    "M7": {"llm": "matrix_memory_carryover.py"},
}
ALL_SUITES = list(MATRIX_MAP.keys())


def _has_llm_key() -> bool:
    if any(os.environ.get(n) for n in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_BASE_URL")):
        return True
    return (PROJECT_ROOT / ".env").exists()


def _run_pytest_suites(suites: list[str], output_dir: Path, python: str) -> dict[str, Any]:
    """跑指定套的单元部分，返回 {suite: {total, passed, failed, cases: [...]}}。"""
    test_files = [MATRICES_DIR / MATRIX_MAP[s]["unit"] for s in suites if "unit" in MATRIX_MAP[s]]
    if not test_files:
        return {}
    basetemp = new_basetemp("runner-pytest-")
    junit_path = output_dir / "unit_junit.xml"
    cmd = [
        python, "-m", "pytest",
        *[str(f) for f in test_files],
        "-c", str(PYTEST_INI),
        "-m", "matrix",
        "--basetemp", basetemp,
        "--junit-xml", str(junit_path),
        "-q", "--tb=short",
    ]
    subprocess.run(cmd, cwd=str(PROJECT_ROOT))  # 不检查退出码，失败也继续聚合
    return _parse_junit(junit_path, suites)


def _parse_junit(junit_path: Path, suites: list[str]) -> dict[str, Any]:
    """解析 junit XML，按 suite 前缀（M1/M2...）分组。"""
    result: dict[str, dict[str, Any]] = {s: {"total": 0, "passed": 0, "failed": 0, "cases": []} for s in suites}
    if not junit_path.exists():
        return result
    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        return result
    for tc in tree.iter("testcase"):
        name = tc.get("name", "")
        # 从函数名提取 suite 前缀：test_M1_ROUTE_001_xxx → M1
        parts = name.split("_")
        suite = ""
        if len(parts) >= 2 and parts[1] in MATRIX_MAP:
            suite = parts[1]
        if suite not in result:
            continue
        failed = tc.find("failure") is not None or tc.find("error") is not None
        case_id = name[len("test_"):] if name.startswith("test_") else name
        result[suite]["total"] += 1
        if failed:
            result[suite]["failed"] += 1
        else:
            result[suite]["passed"] += 1
        result[suite]["cases"].append({"case_id": case_id, "passed": not failed, "redline": case_id.endswith("-R") or "-R0" in case_id})
    return result


def _run_llm_suite(suite: str, output_dir: Path, python: str, limit: int | None) -> dict[str, Any]:
    """跑一个 LLM 套脚本，读 JSONL，返回 {total, passed, failed, redline_failed, cases}。"""
    script = MATRIX_MAP[suite].get("llm")
    if not script:
        return {"total": 0, "passed": 0, "failed": 0, "redline_failed": 0, "cases": [], "skipped": False}
    out_jsonl = output_dir / f"{suite.lower()}.jsonl"
    cmd = [python, str(PROJECT_ROOT / "evals" / script), "--output", str(out_jsonl)]
    if limit:
        cmd += ["--limit", str(limit)]
    proc = subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True)
    skipped = "SKIP" in (proc.stdout or "")
    cases: list[dict[str, Any]] = []
    if out_jsonl.exists():
        for line in out_jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            cases.append(rec)
    total = len(cases)
    passed = sum(1 for c in cases if c.get("score", 0) >= 2)
    failed = total - passed
    redline_failed = sum(1 for c in cases if c.get("redline_broken"))
    return {"total": total, "passed": passed, "failed": failed, "redline_failed": redline_failed,
            "cases": cases, "skipped": skipped}


def _aggregate(unit_results: dict, llm_results: dict, output_dir: Path) -> dict[str, Any]:
    """聚合单元 + LLM，写 all_matrices.json + .md，返回总报告。"""
    matrices: dict[str, Any] = {}
    any_redline_failed = False
    any_skipped = False
    for suite in ALL_SUITES:
        u = unit_results.get(suite, {})
        l = llm_results.get(suite, {})
        entry = {
            "unit": {"total": u.get("total", 0), "passed": u.get("passed", 0), "failed": u.get("failed", 0)},
            "llm": {"total": l.get("total", 0), "passed": l.get("passed", 0), "failed": l.get("failed", 0),
                    "redline_failed": l.get("redline_failed", 0), "skipped": l.get("skipped", False)},
        }
        entry["total"] = entry["unit"]["total"] + entry["llm"]["total"]
        entry["passed"] = entry["unit"]["passed"] + entry["llm"]["passed"]
        entry["failed"] = entry["unit"]["failed"] + entry["llm"]["failed"]
        if l.get("redline_failed"):
            any_redline_failed = True
        if l.get("skipped"):
            any_skipped = True
        matrices[suite] = entry

    report = {
        "matrices": matrices,
        "red_line_fail": any_redline_failed,
        "llm_skipped": any_skipped,
        "summary": {
            "total": sum(m["total"] for m in matrices.values()),
            "passed": sum(m["passed"] for m in matrices.values()),
            "failed": sum(m["failed"] for m in matrices.values()),
        },
    }
    (output_dir / "all_matrices.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(report, output_dir / "all_matrices.md")
    return report


def _write_markdown(report: dict, path: Path) -> None:
    lines = ["# 7 套提示词测试矩阵 · 汇总报告", ""]
    lines.append(f"总 case: {report['summary']['total']} | 通过: {report['summary']['passed']} | 失败: {report['summary']['failed']}")
    if report["red_line_fail"]:
        lines.append("\n**红线失败：存在 redline case 被破，需立即修复。**")
    if report["llm_skipped"]:
        lines.append("\n_部分 LLM 套被 skip（无 API key）。_")
    lines.append("\n| 套 | 单元 total/pass/fail | LLM total/pass/fail/redline |")
    lines.append("|---|---|---|")
    for suite, m in report["matrices"].items():
        u, l = m["unit"], m["llm"]
        lines.append(f"| {suite} | {u['total']}/{u['passed']}/{u['failed']} | {l['total']}/{l['passed']}/{l['failed']}/{l['redline_failed']}{' (skip)' if l['skipped'] else ''} |")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None, help="只跑指定套（如 M1 M5）")
    parser.add_argument("--skip-llm", action="store_true", help="只跑单元（CI 默认）")
    parser.add_argument("--output-dir", type=Path, default=Path("evals/results"))
    parser.add_argument("--limit", type=int, default=None, help="LLM 套限 case 数")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    suites = args.only or ALL_SUITES
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[runner] 跑套: {suites}，skip-llm={args.skip_llm}")
    unit_results = _run_pytest_suites(suites, output_dir, args.python)
    print(f"[runner] 单元完成：{unit_results}")

    llm_results: dict[str, Any] = {}
    if not args.skip_llm and _has_llm_key():
        for suite in suites:
            if "llm" in MATRIX_MAP.get(suite, {}):
                print(f"[runner] 跑 LLM 套 {suite}...")
                llm_results[suite] = _run_llm_suite(suite, output_dir, args.python, args.limit)
                print(f"[runner] {suite} LLM 完成：{llm_results[suite]}")
    else:
        print("[runner] 跳过 LLM 套（--skip-llm 或无 API key）")

    report = _aggregate(unit_results, llm_results, output_dir)
    print(f"[runner] 汇总：total={report['summary']['total']} passed={report['summary']['passed']} failed={report['summary']['failed']}")
    print(f"[runner] 报告：{output_dir / 'all_matrices.md'}")
    if report["red_line_fail"]:
        print("[runner] RED_LINE_FAIL：红线 case 被破")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
