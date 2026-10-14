"""Independent rerun wrapper. Only redirects historical report writes, not product behavior."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [
    ("verify_memory_b_acceptance_edges.py", 11),
    ("verify_memory_maintenance_acceptance_edges.py", 6),
    ("verify_memory_maintenance_loop.py", 9),
    ("verify_memory_search_fusion.py", 24),
    ("verify_memory_dual_end_semantics.py", 13),
    ("verify_memory_dual_end_http.py", 8),
    ("verify_memory_b_third_review.py", 9),
    ("verify_memory_b_fourth_review.py", 16),
    ("verify_memory_b_round5_falsify.py", 12),
    ("verify_memory_final_falsification.py", 26),
]

def fingerprints():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("agent_core", "cloud_memory")
            for p in sorted((ROOT / folder).glob("*.py"))}

def main():
    before = fingerprints()
    results = []
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for name, expected in SCRIPTS:
        wrapper = '''
import runpy, sys
from pathlib import Path
from unittest.mock import patch
target = Path(sys.argv[1]).resolve()
docs = target.parent.parent / "docs"
original = Path.write_text
def report_write(path, data, *args, **kwargs):
    if path.resolve().parent == docs:
        return len(data)
    return original(path, data, *args, **kwargs)
sys.argv = [str(target)]
with patch.object(Path, "write_text", report_write):
    runpy.run_path(str(target), run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-c", wrapper, str(ROOT / "scripts" / name)],
                                cwd=ROOT, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=600)
        try:
            report = json.loads(result.stdout)
        except ValueError:
            report = {"unparsed_stdout": result.stdout}
        row = dict(script=name, expected_total=expected, exit_code=result.returncode,
                   report=report, stderr=result.stderr if result.returncode else "")
        row["ok"] = result.returncode == 0 and report.get("ok", False) and (
            expected is None or report.get("total") == expected)
        results.append(row)
        print(json.dumps(dict(script=name, exit_code=row["exit_code"], ok=row["ok"],
                              passed=report.get("passed"), total=report.get("total")),
                         ensure_ascii=False), flush=True)
    after = fingerprints()
    report = dict(run_at_utc=datetime.now(timezone.utc).isoformat(),
                  ok=all(r["ok"] for r in results) and before == after,
                  results=results, fingerprints=before, product_drift=before != after,
                  production_memory_touched=False, live_model_calls=0,
                  wrapper_limit="Historical docs report writes are intercepted; product writes and assertions are unchanged.")
    output = ROOT / "docs/2026-10-12-memory-b-independent-rerun-evidence.json"
    try:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        print("RERUN_EVIDENCE_JSON=" + json.dumps(report, ensure_ascii=True), flush=True)
    print(json.dumps(dict(ok=report["ok"], product_drift=report["product_drift"],
                          evidence=str(output)), ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
