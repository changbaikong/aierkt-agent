"""Replay actual live Q1 candidates and check retention without further model calls."""
import gc
import json
from pathlib import Path
import sys
import tempfile
from datetime import UTC, datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate
from agent_core.memory_writes import MemoryWriter

class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]

def run():
    evidence = json.loads((ROOT / 'docs/2026-10-12-memory-quality-independent-rerun-evidence.json').read_text(encoding='utf-8'))
    checks = []
    for result in evidence['runs']:
        if result['case'] != 'Q1':
            continue
        with tempfile.TemporaryDirectory(prefix='b-retention-replay-', ignore_cleanup_errors=True) as folder:
            base = Path(folder)
            repos = initialize_memory_repositories(base / 'records.db', base / 'vectors', vector_size=3)
            try:
                writer = MemoryWriter(repos, Embeddings())
                scope = Scope('retention-review', 'web', 'original', 'companion')
                saved = seed_semantic(writer, scope, [MemoryCandidate(**c) for c in result['candidates']],
                    turn_id='Q1-'+str(result['repeat']), project_binding='isolated-quality-project')
                before = [{k:r.get(k) for k in ('id','content','status','content_status','expires_at','scope_kind','project_id')} for r in saved]
                writer.run_maintenance(now=datetime.now(UTC)+timedelta(days=2))
                after = repos.records_by_ids([r['id'] for r in saved])
                checks.append(dict(case='Q1', repeat=result['repeat'], passed=all(r['status'] in ('active','candidate') for r in after.values()),
                    user_text=result['packet']['user_text'], raw_model_response=result['raw'], before=before,
                    after=[{k:r.get(k) for k in ('id','content','status','content_status','expires_at')} for r in after.values()],
                    expected='Without a deadline or cancelling evidence, the project constraint remains effective.'))
            finally:
                repos.close()
                gc.collect()
    report = dict(ok=all(c['passed'] for c in checks), passed=sum(c['passed'] for c in checks), total=len(checks),
        checks=checks, live_model_calls=0, production_memory_touched=False,
        limits=['Replays recorded live candidates unchanged; two-day advancement is simulated, not a real long-running observation.'])
    print(json.dumps(report, ensure_ascii=True))
    return report

if __name__ == '__main__':
    report = run()
    raise SystemExit(0 if report['ok'] else 1)
