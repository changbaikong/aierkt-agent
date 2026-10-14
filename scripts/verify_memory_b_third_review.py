"""Independent B review beyond the previous fixed examples. Temporary data only.

Control cases verify normal statements still work. Adversarial model responses
test the program's guarantees, not the probability of a model making an error.
The script emits JSON; exit 1 means acceptance failure, crashes propagate.
"""
from __future__ import annotations

import gc
import json
import logging
import os
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import EvidenceRef, MemoryCandidate, MemoryRef, turn_from_messages
from agent_core.memory_writes import MemoryWriter


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


def run():
    checks = []
    scope = Scope('review-user', 'web', 'third-review', 'companion')

    def check(identifier, requirement, passed, actual):
        checks.append(dict(id=identifier, requirement=requirement, passed=bool(passed), actual=actual))

    with tempfile.TemporaryDirectory(prefix='memory-third-review-', ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        os.environ['QDRANT_MODE'] = 'local'
        os.environ['MEMORY_SEMANTIC_ENABLED'] = 'true'
        repos = initialize_memory_repositories(base / 'records.db', base / 'vectors', vector_size=3)
        writer = MemoryWriter(repos, Embeddings())
        service = MemoryMaintenanceService(writer, repos)
        extractor = SemanticMemoryExtractor(None)

        def packet(turn, text, target=None, evidence=()):
            p = turn_from_messages(turn_id=turn, occurred_at=datetime.now(UTC).isoformat(timespec='milliseconds'),
                                   scope=scope, user_text=text, published_reply='收到')
            refs = [] if target is None else [MemoryRef(target['id'], target['kind'], target['content_status'],
                                                       target['content'], version=target['record_version'])]
            return replace(p, recalled_memory_refs=refs, evidence_refs=list(evidence))

        def parse(p, item, target=None):
            kwargs = {} if target is None else dict(known_ids=[target['id']], known_versions={target['id']:target['record_version']})
            return extractor.parse_maintenance(json.dumps({'memories':[item]}, ensure_ascii=False), packet=p, **kwargs)

        def seed(turn, text):
            return seed_semantic(writer, scope, [MemoryCandidate(content=text, kind='fact',
                content_status='reported', source_types=('user_statement',), source_refs=(turn,))], turn_id=turn)[0]

        def update(p, target, text, **extra):
            item = dict(action='correct', target_id=target['id'], expected_version=target['record_version'], content=text,
                        kind='fact', content_status='reported', source_types=['user_statement'], source_refs=[p.turn_id],
                        reason='模型建议更新')
            item.update(extra)
            _, suggestions, rejected = parse(p, item, target)
            outcomes = [service.apply(s, scope=scope, packet=p, turn_id=p.turn_id) for s in suggestions]
            return outcomes, rejected

        try:
            # Normal controls: automatic saving and ordinary corrections remain usable.
            p = packet('control-adoption', '同意采用本地部署方案')
            candidates, _, rejected = parse(p, dict(content='采用本地部署方案', kind='decision', content_status='accepted',
                                                    source_types=['user_statement'], source_refs=[p.turn_id]))
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            check('CONTROL-1', '同一事项明确采纳可自动保存', len(saved)==1 and saved[0]['content_status']=='accepted',
                  dict(statuses=[r['content_status'] for r in saved], rejected=rejected))
            old = seed('control-budget', '控制项目预算五千')
            outcomes, rejected = update(packet('control-change', '控制项目预算不是五千，是三千', old), old, '控制项目预算三千')
            check('CONTROL-2', '正常纠正可以更新现行预算', len(outcomes)==1 and outcomes[0]['applied'],
                  dict(applied=[r['applied'] for r in outcomes], rejected=rejected))

            p = packet('current-adoption', '同意以后回答先说结论；云端部署的建议你再分析一下')
            candidates, _, rejected = parse(p, dict(content='用户已采纳云端部署方案', kind='decision', content_status='accepted',
                                                    source_types=['user_statement'], source_refs=[p.turn_id]))
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            check('B3-R1', '本轮无关同意不能证明云端部署已采纳', not any(r['content_status']=='accepted' for r in saved),
                  dict(saved=[dict(content=r['content'], status=r['content_status']) for r in saved], rejected=rejected))

            old = seed('tool-principle', '开发项目长期原则是本地优先')
            p = packet('tool-weather', '帮我看天气', old,
                       [EvidenceRef('weather-failed', '天气查询连接失败，没有查到天气', outcome='failed')])
            outcomes, rejected = update(p, old, '', action='invalidate', source_types=['tool_result'],
                                         reason='模型认为原则已经失效')
            current = repos.records_by_ids([old['id']])[old['id']]
            check('B3-R2', '无关且失败的工具回执不能归档项目原则', current['status']=='active',
                  dict(status=current['status'], applied=[r['applied'] for r in outcomes], rejected=rejected))

            old = seed('multi-field-seed', '采购项目预算五千')
            p = packet('multi-field-change', '采购项目预算不是五千，是三千；交付日期尚未确定', old)
            outcomes, rejected = update(p, old, '采购项目预算三千，交付时间为2026年12月31日')
            unsupported = any(r['applied'] and '2026' in str((r.get('record') or {}).get('content')) for r in outcomes)
            check('B3-R3', '一个数字有来源不能让同条新增日期也通过', not unsupported,
                  dict(applied=[dict(applied=r['applied'], content=(r.get('record') or {}).get('content'), reason=r['reason']) for r in outcomes], rejected=rejected))

            old = seed('paraphrase-seed', '服务器预算为5000元')
            p = packet('paraphrase-change', '服务器预算不是五千，应该是三千元', old)
            outcomes, rejected = update(p, old, '服务器预算为3000元')
            check('B3-R4', '中文数字原话可支持等值阿拉伯数字纠正', len(outcomes)==1 and outcomes[0]['applied'],
                  dict(applied=[r['applied'] for r in outcomes], reasons=[r['reason'] for r in outcomes], rejected=rejected))

            old = seed('bypass-seed', '数据项目原则是本地优先')
            p = packet('bypass-add', '今天天气不错', old)
            candidates, _, rejected = parse(p, dict(action='add', content='助手建议数据项目改成云端优先', kind='plan',
                content_status='proposed', source_types=['assistant_suggestion'], source_refs=[p.turn_id], supersedes=[old['id']]), old)
            seed_semantic(writer, scope, candidates, turn_id=p.turn_id, known_ids=[old['id']])
            status = repos.records_by_ids([old['id']])[old['id']]['status']
            check('B3-R5', '新增建议不能通过替代字段绕过维护证据保护', status=='active',
                  dict(old_status=status, parsed=len(candidates), rejected=rejected))

            # on_change is a real failing callback, not a forged receipt.
            old = seed('sync-seed', '同步项目预算五千')
            def fail_sync(*args):
                raise OSError('simulated sync notification outage')
            writer.on_change = fail_sync
            p = packet('sync-change', '同步项目预算不是五千，是三千', old)
            outcomes, rejected = update(p, old, '同步项目预算三千')
            writer.on_change = None
            verify = outcomes[0]['verify'] if outcomes else {}
            check('B3-R6', '变更通知失败不能回执同步已发出且全部完成',
                  bool(outcomes) and verify.get('sync') != 'emitted' and verify.get('partial') is True,
                  dict(applied=[r['applied'] for r in outcomes], verify=verify, rejected=rejected))

            p = packet('short-emotion', '今天因为等待验收有些烦躁')
            candidates, _, rejected = parse(p, dict(content='今天因为等待验收有些烦躁', kind='fact', content_status='reported',
                source_types=['user_statement'], source_refs=[p.turn_id], horizon='short_term'))
            saved = seed_semantic(writer, scope, candidates, turn_id=p.turn_id)
            record_id = saved[0]['id'] if saved else ''
            maintenance = writer.run_maintenance(now=datetime.now(UTC)+timedelta(days=365))
            fresh = repos.records_by_ids([record_id]).get(record_id)
            retains_horizon = bool(saved and json.loads(saved[0]['metadata']).get('horizon') == 'short_term')
            no_longterm_emotion = not fresh or fresh['status'] != 'active'
            check('B3-R7', '当日情绪的短期语义被保存并由到期政策处理',
                  retains_horizon and no_longterm_emotion,
                  dict(saved=len(saved), horizon_retained=retains_horizon, status_after_one_year=(fresh or {}).get('status'),
                       expires_at=(fresh or {}).get('expires_at'), maintenance=maintenance, rejected=rejected))
        finally:
            repos.close()
            gc.collect()

    report = dict(ok=all(c['passed'] for c in checks), passed=sum(c['passed'] for c in checks), total=len(checks), checks=checks,
                  live_model_calls=0, production_memory_touched=False,
                  limits=['Synthetic adversarial model outputs test program guarantees, not live-model frequency.',
                          'Local temporary storage only; no deployment or production data changes.'])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(run())
