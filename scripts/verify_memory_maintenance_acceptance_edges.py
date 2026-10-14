"""Independent maintenance acceptance counterexamples; temporary storage only.

The model output is deliberately adversarial. These checks test the program's
claimed evidence, version and recovery guarantees, not model success rates.
Exit 1 means an acceptance requirement failed; infrastructure errors propagate.
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker, MemoryMaintenanceWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import HistoryItem, MemoryCandidate, MemoryRef, MemoryUpdateSuggestion, turn_from_messages
from agent_core.memory_writes import MemoryWriter
from agent_core.memory_processing import MemoryReviewProcessor


class FixedReviewProvider:
    model = 'maintenance-independent-fixtures'
    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        turn = payload['packet']['turn_id']
        fixture = {
            'retry-correction': '预算三千', 'handoff-correction': '预算三千',
            'version-first': '项目采购预算三千',
        }.get(turn)
        rows = []
        for item in payload['items']:
            if fixture is None:
                rows.append(dict(index=item['index'],verdict='uncertain',reason='没有独立固定预期'))
            else:
                rows.append(dict(index=item['index'],verdict='supported',content=fixture,subject='采购预算',
                    reason='唯一旧预算引用与用户明确三千纠正',same_subject=True,preserves_negation=True,
                    preserves_conditions=True,change_authorized=True,
                    lifetime={'mode':'long_term','basis':'预算事实无期限'},
                    quotes=[{'message_id':turn,'quote':'不是五千'},{'message_id':turn,'quote':'是三千'}]))
        return SimpleNamespace(message={'content':json.dumps({'items':rows},ensure_ascii=False)},usage={})


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


SCOPE = Scope('acceptance-user', 'web', 'isolated', 'companion')


def packet(turn, text, *, history=(), refs=()):
    result = turn_from_messages(
        turn_id=turn, occurred_at=datetime.now(UTC).isoformat(timespec='milliseconds'),
        scope=SCOPE, user_text=text, published_reply='收到',
    )
    return replace(result, relevant_history=list(history), recalled_memory_refs=list(refs))


def correction(target, content):
    return MemoryUpdateSuggestion(
        action='correct', target_id=target['id'], expected_version=target['record_version'],
        content=content, kind='fact', content_status='reported',
        source_types=('user_statement',), reason='用户纠正预算',
    )


async def run():
    checks = []

    def check(identifier, requirement, passed, actual):
        checks.append(dict(id=identifier, requirement=requirement, passed=bool(passed), actual=actual))

    with tempfile.TemporaryDirectory(prefix='maintenance-independent-', ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        os.environ['QDRANT_MODE'] = 'local'
        os.environ['QDRANT_PATH'] = str(base / 'vectors')
        os.environ['MEMORY_SEMANTIC_ENABLED'] = 'true'
        repos = initialize_memory_repositories(base / 'records.db', base / 'vectors', vector_size=3)
        writer = MemoryWriter(repos, Embeddings())
        service = MemoryMaintenanceService(writer, repos)
        queue = MemoryCandidateQueue(base / 'jobs.db')
        queue.initialize()
        reviewer = FixedReviewProvider()
        async def prepare(p, candidates=(), suggestions=()):
            path = base / (p.turn_id + '-review.json')
            def persist(value):
                path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')
            return await MemoryReviewProcessor(writer, reviewer).prepare(p,candidates,suggestions,persist=persist)

        def seed(turn, content):
            return seed_semantic(writer,
                SCOPE, [MemoryCandidate(content=content, kind='fact', content_status='reported',
                                        source_types=('user_statement',), source_refs=(turn,))],
                turn_id=turn,
            )[0]

        try:
            # A storage outage must remain recoverable, not become a completed refusal.
            old = seed('retry-seed', '预算五千')
            job_id, _ = queue.enqueue_maintenance(packet('retry-correction', '不是五千，是三千'),
                                                [correction(old, '预算三千')])
            with patch.object(writer, 'apply_semantic_update', side_effect=sqlite3.OperationalError('simulated storage outage')):
                outcome = await MemoryMaintenanceWorker(queue, service, review_provider=reviewer).process_once()
            job = queue.get(job_id)
            check('BM-R1', '维护写入失败进入可恢复重试', job['status'] != 'completed' and job['attempts'] > 0,
                  dict(worker=outcome, status=job['status'], attempts=job['attempts'], error=job['last_error']))

            # Extraction succeeded but its durable handoff failed.
            class Extractor:
                provider = reviewer
                async def extract_maintenance(self, packet, **kwargs):
                    return [], [correction(old, '预算三千')]

            handoff_packet = packet('handoff-correction', '不是五千，是三千')
            candidate_job, _ = queue.enqueue(handoff_packet)
            with patch.object(queue, 'enqueue_maintenance', side_effect=sqlite3.OperationalError('simulated queue outage')):
                outcome = await MemoryCandidateWorker(queue, Extractor(), writer, maintenance_queue=queue, review_provider=reviewer).process_once()
            job = queue.get(candidate_job)
            check('BM-R2', '更新建议交接失败保留可恢复作业', job['status'] != 'completed' and job['attempts'] > 0,
                  dict(worker=outcome, status=job['status'], attempts=job['attempts'], suggestions_persisted=bool(job['candidates'])))

            # Two distinct turns based on the same historical version must not fork.
            old = seed('version-seed', '项目采购预算五千')
            first_packet = packet('version-first', '不是五千，是三千')
            _, prepared, _ = await prepare(first_packet,suggestions=[correction(old,'项目采购预算三千')])
            first = service.apply(prepared[0], scope=SCOPE,packet=first_packet,turn_id='version-first')
            second = service.apply(correction(old, '项目采购预算四千'), scope=SCOPE,
                                   packet=packet('version-second', '不是五千，是四千'), turn_id='version-second')
            ids = [item['record']['id'] for item in (first, second) if item.get('record')]
            rows = repos.records_by_ids(ids)
            active = [row['content'] for row in rows.values() if row['status'] == 'active' and not row['effective_record_id']]
            check('BM-R3', '已被替代的目标版本不能再分叉出第二条现行记忆', first['applied'] and not second['applied'] and len(active) == 1,
                  dict(first_applied=first['applied'], stale_applied=second['applied'], current_versions=active))

            # Parse actual model output, then use the actual service and writer.
            old = seed('evidence-seed', '项目长期原则是本地优先')
            evidence_packet = packet('evidence-current', '今天天气不错', refs=[MemoryRef(old['id'], 'fact', 'reported', old['content'], version=old['record_version'])])
            model_output = json.dumps({'memories': [dict(action='invalidate', target_id=old['id'],
                expected_version=old['record_version'], kind='fact', content_status='reported',
                reason='模型认为这条原则太旧了', source_types=['model_inference'], source_refs=[evidence_packet.turn_id])]}, ensure_ascii=False)
            _, suggestions, rejected = SemanticMemoryExtractor(None).parse_maintenance(model_output, packet=evidence_packet,
                known_ids=[old['id']], known_versions={old['id']:old['record_version']})
            applied = [service.apply(s, scope=SCOPE, packet=evidence_packet, turn_id=evidence_packet.turn_id) for s in suggestions]
            status = repos.records_by_ids([old['id']])[old['id']]['status']
            check('BM-R4', '模型自拟理由不能无新证据归档长期原则', status == 'active',
                  dict(parsed=len(suggestions), rejected=rejected, applied=[r['applied'] for r in applied], status=status))

            # An unrelated prior adoption is not evidence for the current claim.
            adoption_packet = packet('adoption-current', '部署方案还未决定', history=[
                HistoryItem('style-choice', 'user', '同意以后回答先说结论'),
                HistoryItem('cloud-proposal', 'assistant', '建议把服务部署到云端'),
            ])
            output = json.dumps({'memories':[dict(content='用户已采纳云端部署方案', kind='decision',
                content_status='accepted', source_types=['user_statement'], source_refs=['cloud-proposal'])]}, ensure_ascii=False)
            candidates, _, rejected = SemanticMemoryExtractor(None).parse_maintenance(output, packet=adoption_packet)
            prepared, _, _ = await prepare(adoption_packet,candidates=candidates)
            saved = writer.save_semantic_candidates(SCOPE,prepared,turn_id=adoption_packet.turn_id,packet=adoption_packet)
            check('BM-R5', '无关历史认同及助手引用不能证明当前方案已采纳', not any(r['content_status']=='accepted' for r in saved),
                  dict(rejected=rejected, saved=[dict(content=r['content'], status=r['content_status']) for r in saved]))

            # Correction words about another issue must not authorize this target.
            old = seed('unrelated-seed', '设备采购预算五千')
            unrelated_packet = packet('unrelated-current', '不是周一，是周二开会；设备采购预算还没变化',
                refs=[MemoryRef(old['id'], 'fact', 'reported', old['content'], version=old['record_version'])])
            output = json.dumps({'memories':[dict(action='correct', target_id=old['id'], expected_version=old['record_version'],
                content='设备采购预算三千', kind='fact', content_status='reported', source_types=['user_statement'],
                source_refs=[unrelated_packet.turn_id], reason='模型认为预算需要纠正')]}, ensure_ascii=False)
            _, suggestions, rejected = SemanticMemoryExtractor(None).parse_maintenance(output, packet=unrelated_packet,
                known_ids=[old['id']], known_versions={old['id']:old['record_version']})
            applied = [service.apply(s, scope=SCOPE, packet=unrelated_packet, turn_id=unrelated_packet.turn_id) for s in suggestions]
            check('BM-R6', '无关纠正词不能授权修改预算', not any(r['applied'] for r in applied),
                  dict(parsed=len(suggestions), rejected=rejected, changed=[r['record']['content'] for r in applied if r.get('record')]))
        finally:
            repos.close()
            gc.collect()

    report = dict(ok=all(c['passed'] for c in checks), passed=sum(c['passed'] for c in checks), total=len(checks),
                  checks=checks, live_model_calls=0, production_memory_touched=False,
                  limits=['Deterministic adversarial model output; verifies program guarantees, not model frequency.',
                          'Local temporary repositories only; no production deployment or cloud data comparison.'])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(run()))
