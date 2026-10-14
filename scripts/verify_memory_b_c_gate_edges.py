"""Independent B-to-C gate probes. Synthetic outputs; isolated storage only."""
import gc
import json
import sys
import tempfile
from pathlib import Path
from datetime import datetime, UTC, timedelta
from dataclasses import replace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import HistoryItem, turn_from_messages
from agent_core.memory_writes import MemoryWriter

class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]

def run():
    scenarios = [
        dict(id='normal-add-budget', user='项目预算为3000元。', content='项目预算为3000元', kind='fact', status='reported', should_save=True),
        dict(id='invented-add-budget', user='项目预算为3000元。', content='项目预算为9000元', kind='fact', status='reported', should_save=False),
        dict(id='normal-add-selection', user='部署方案改用方案B；尚未实施，也没有评审结论。', content='部署方案改用方案B，尚未实施', kind='decision', status='accepted', should_save=True),
        dict(id='invented-add-approval', user='部署方案改用方案B；尚未实施，也没有评审结论。', content='部署方案改用方案B，安全评审已通过', kind='decision', status='accepted', should_save=False),
        dict(id='unrelated-duration', user='项目日志一律先落盘再回传。另外这条临时提醒保留三天。', content='项目日志一律先落盘再回传', kind='decision', status='accepted', should_save=True, horizon='long_term', keep_after_four_days=True),
        dict(id='short-request-not-phase', user='云端部署的建议你再分析一下。', content='用户要求继续分析云端部署建议', kind='open_question', status='open', should_save=True, horizon='short_term', expect_short=True),
        dict(id='unique-ack-receipt', user='嗯，就这样', content='用户同意助手以后回答时先说结论', kind='preference', status='explicit', should_save=True,
             history=[HistoryItem('assistant-history','assistant','以后回答先说结论可以吗？'),HistoryItem('user-history','user','嗯，就这样')], expect_passed_receipt=True),
    ]
    checks=[]
    for case in scenarios:
        with tempfile.TemporaryDirectory(prefix='b-c-gate-', ignore_cleanup_errors=True) as folder:
            base=Path(folder)
            repos=initialize_memory_repositories(base/'records.db',base/'vectors',vector_size=3)
            try:
                scope=Scope('gate-review','web',case['id'],'companion')
                packet=turn_from_messages(turn_id=case['id'],occurred_at=datetime.now(UTC).isoformat(),scope=scope,
                    user_text=case['user'],published_reply='收到')
                packet=replace(packet,relevant_history=case.get('history',[]))
                item=dict(content=case['content'],kind=case['kind'],content_status=case['status'],
                    source_types=['user_statement'],source_refs=[case['id']],horizon=case.get('horizon','long_term'))
                if case.get('history'):
                    item['source_types'].append('assistant_suggestion')
                    item['source_refs'].extend(['assistant-history','user-history'])
                candidates,suggestions,rejected=SemanticMemoryExtractor(None).parse_maintenance(
                    json.dumps({'memories':[item]},ensure_ascii=False),packet=packet)
                writer=MemoryWriter(repos,Embeddings())
                saved=writer.save_semantic_candidates(scope,candidates,turn_id=packet.turn_id,packet=packet)
                rows=[r for r in saved if r.get('id')]
                metadata=[json.loads(r['metadata']) if isinstance(r['metadata'],str) else r['metadata'] for r in rows]
                passed=bool(rows)==case['should_save']
                if case.get('expect_short'):
                    passed=passed and bool(rows) and all(r.get('expires_at') for r in rows)
                if case.get('expect_passed_receipt'):
                    passed=passed and bool(rows) and all(m.get('evidence_receipt',{}).get('passed') for m in metadata)
                after=[]
                if case.get('keep_after_four_days'):
                    writer.run_maintenance(now=datetime.now(UTC)+timedelta(days=4))
                    after=list(repos.records_by_ids([r['id'] for r in rows]).values())
                    passed=passed and bool(after) and all(r['status'] in ('active','candidate') for r in after)
                checks.append(dict(id=case['id'],passed=bool(passed),user_text=case['user'],synthetic_model_item=item,
                    rejected=rejected,saved=[{k:r.get(k) for k in ('id','content','kind','content_status','status','expires_at')} for r in rows],
                    metadata=metadata,after_four_days=[{k:r.get(k) for k in ('content','status','expires_at')} for r in after]))
            finally:
                repos.close()
                gc.collect()
    report=dict(ok=all(c['passed'] for c in checks),passed=sum(c['passed'] for c in checks),total=len(checks),checks=checks,
        production_memory_touched=False,live_model_calls=0,
        limits=['Synthetic outputs show program boundaries, not how often a live model produces them.','Four-day lifecycle checks use simulated time.'])
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return report

if __name__=='__main__':
    report=run()
    raise SystemExit(0 if report['ok'] else 1)
