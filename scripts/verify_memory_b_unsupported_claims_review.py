"""Independent checks: a replacement must not add an unsupported approval claim."""
import gc
import json
from pathlib import Path
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, UTC

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryRef, turn_from_messages
from agent_core.memory_writes import MemoryWriter

class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]

def run():
    checks = []
    variants = [
        ("positive-control", "部署方案改用方案B", True),
        ("approval-invention", "部署方案改用方案B，安全评审已通过", False),
        ("execution-invention", "部署方案改用方案B，回滚演练已经成功", False),
    ]
    for name, content, expected_applied in variants:
        with tempfile.TemporaryDirectory(prefix="independent-b-claim-", ignore_cleanup_errors=True) as folder:
            base = Path(folder)
            repos = initialize_memory_repositories(base / "records.db", base / "vectors", vector_size=3)
            try:
                scope = Scope("independent-review", "web", name, "companion")
                writer = MemoryWriter(repos, Embeddings())
                service = MemoryMaintenanceService(writer, repos)
                old = seed_semantic(writer, scope, [MemoryCandidate(
                    content="部署方案为方案A", kind="decision", content_status="accepted",
                    source_types=("user_statement",), source_refs=("seed",))], turn_id="seed")[0]
                packet = turn_from_messages(turn_id=name, occurred_at=datetime.now(UTC).isoformat(),
                    scope=scope, user_text="部署方案改用方案B，不再用方案A；尚未实施，也没有评审结论。",
                    published_reply="收到，选择B，实施与评审尚待完成。")
                packet = replace(packet, recalled_memory_refs=[MemoryRef(old["id"], old["kind"],
                    old["content_status"], old["content"], version=old["record_version"])])
                raw = dict(action="replace", target_id=old["id"], expected_version=old["record_version"],
                    content=content, kind="decision", content_status="accepted",
                    source_types=["user_statement"], source_refs=[name], reason="用户更换方案")
                candidates, suggestions, rejected = SemanticMemoryExtractor(None).parse_maintenance(
                    json.dumps({"memories": [raw]}, ensure_ascii=False), packet=packet,
                    known_ids=[old["id"]], known_versions={old["id"]: old["record_version"]})
                receipts = [service.apply(s, scope=scope, packet=packet, turn_id=name) for s in suggestions]
                applied = any(r["applied"] for r in receipts)
                old_after = repos.records_by_ids([old["id"]])[old["id"]]
                checks.append(dict(id=name, passed=applied == expected_applied,
                    expected_applied=expected_applied, actual_applied=applied,
                    user_text=packet.user_text, synthetic_model_item=raw, rejected=rejected,
                    receipts=[dict(applied=r["applied"], reason=r.get("reason"),
                                   new_content=(r.get("record") or {}).get("content"),
                                   content_status=(r.get("record") or {}).get("content_status")) for r in receipts],
                    old_status=old_after["status"]))
            finally:
                repos.close()
                gc.collect()
    report = dict(ok=all(c["passed"] for c in checks), passed=sum(c["passed"] for c in checks),
                  total=len(checks), checks=checks, live_model_calls=0,
                  production_memory_touched=False,
                  limits=["Hand-built model outputs prove an application boundary failure, not its live frequency."])
    print(json.dumps(report, ensure_ascii=True))
    return report

if __name__ == "__main__":
    report = run()
    raise SystemExit(0 if report["ok"] else 1)
