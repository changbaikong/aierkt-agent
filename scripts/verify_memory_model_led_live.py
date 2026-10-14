"""Isolated live evidence collection; semantic acceptance needs independent review."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.verify_memory_quality_live import cases, Embeddings, PROJECT, SCOPE
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor, build_semantic_extractor_from_env
from agent_core.memory_processing import MemoryReviewProcessor
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_turn import MemoryCandidate, MemoryRef, MemoryTurnPacket
from agent_core.memory_writes import MemoryWriter

EXPECTATIONS = [
    '暂不部署是明确阶段决定；无回复不能补成部署地点或实施。',
    '三项建议后的嗯不能证明任一建议已被明确采纳。',
    '唯一提议后明确认同支持先说结论的长期回答偏好。',
    '预算纠正为三千；旧五千退出现行集合，新会话召回三千。',
    '服务器预算3000与三千等值；旧5000退出现行集合。',
    '已选择B且不再A；实施未完成，旧选择退出现行集合。',
    '回答风格已同意；云端部署仍待分析，不能借用无关同意。',
    '失败天气工具不能证明天气；本地优先旧原则保持有效。',
    '采购预算三千；交付日期未知，不能补具体日期。',
    '助手自称核对通过不能证明完成或生产授权。',
    '日志落盘再回传为长期原则；今日疲惫只能短期保存。',
    '展会三千与设备采购五千是不同事项；设备旧值不改。',
    '同义表达支持回滚方案从A改B，不能混淆事项。',
    '部署选择B；无实施和评审结论，不得补审批通过。',
    '容灾选择B；演练和上线未做，不得补执行成功。',
    '暂不部署是阶段约束，不能按默认一天期限失效。',
    '排期不调整与部署暂缓分开；部署旧立即执行须被纠正。',
    '日志一律落盘再回传且不得直接回传；普通词一不是数值。',
]

class RecordingProvider:
    def __init__(self, inner):
        self.inner, self.calls, self.stage = inner, [], 'extraction'
        self.model = getattr(inner, 'model', '')

    async def complete(self, messages, **kwargs):
        entry = {'stage': self.stage, 'request': messages, 'options': kwargs,
                 'status': 'started', 'raw': None, 'usage': {}}
        self.calls.append(entry)
        started = time.monotonic()
        try:
            response = await self.inner.complete(messages, **kwargs)
            message = response.message
            entry.update(status='completed', raw=message.get('content') if isinstance(message, dict) else str(message),
                         usage=response.usage)
            if not str(entry['raw'] or '').strip():
                entry.update(status='invalid_response', error_type='EmptyModelOutput')
            return response
        except BaseException as exc:
            entry.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            entry['latency_ms'] = round((time.monotonic() - started) * 1000)

def snapshot(repos):
    with repos.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM memory_records ORDER BY created_at,id')]

def packet_for(case, turn, refs=()):
    return MemoryTurnPacket(turn_id=turn, occurred_at=datetime.now(UTC).isoformat(timespec='milliseconds'),
        scope_owner=SCOPE.owner_id, scope_channel=SCOPE.channel, scope_conversation=SCOPE.conversation_id,
        scope_module=SCOPE.module_id, project_binding=PROJECT, user_text=case['user'],
        published_reply=case.get('reply', ''), relevant_history=case.get('history', []),
        recalled_memory_refs=list(refs), evidence_refs=case.get('evidence', []))

async def prepare_and_commit(processor, writer, service, packet, candidates, suggestions, base, label, provider):
    cache_path = base / (label + '-review.json')
    def persist(value):
        cache_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    if provider is not None:
        provider.stage = label + '_review'
    prepared, changes, processing = await processor.prepare(packet, candidates, suggestions,
        cached=json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else None, persist=persist)
    saved = writer.save_semantic_candidates(SCOPE, prepared, turn_id=packet.turn_id,
        occurred_at=packet.occurred_at, known_ids=[r.memory_id for r in packet.recalled_memory_refs],
        project_binding=PROJECT, packet=packet)
    applied = [service.apply(change, scope=SCOPE, packet=packet, turn_id=packet.turn_id,
                            project_id=PROJECT) for change in changes]
    return {'prepared_candidates': [asdict(x) for x in prepared], 'prepared_suggestions': [asdict(x) for x in changes],
            'processing': processing.as_dict(), 'review_cache': json.loads(cache_path.read_text(encoding='utf-8')) if cache_path.exists() else None,
            'saved': saved, 'applied': applied}

async def run(repeat=3, output=None, selected=None):
    if repeat < 1:
        raise ValueError('repeat must be positive')
    os.environ['MEMORY_SEMANTIC_ENABLED'] = 'true'
    configured = build_semantic_extractor_from_env()
    if configured is None:
        raise RuntimeError('主回答模型未配置')
    provider = RecordingProvider(configured.provider)
    extractor = SemanticMemoryExtractor(provider, temperature=configured.temperature, max_token=configured.max_token)
    runs = []
    chosen = [c for c in cases() if not selected or c['id'] in selected]
    if not chosen:
        raise ValueError('no matching cases')
    for case in chosen:
        for index in range(repeat):
            result = {'case': case['id'], 'title': case['title'], 'repeat': index + 1,
                      'independent_expectation': EXPECTATIONS[int(case['id'][1:]) - 1],
                      'semantic_acceptance': 'unknown', 'independent_review': {'status': 'unknown', 'findings': []},
                      'pipeline_completed': False, 'mechanical_findings': []}
            call_start = len(provider.calls)
            with tempfile.TemporaryDirectory(prefix='model-led-memory-', ignore_cleanup_errors=True) as folder:
                base = Path(folder)
                repos = initialize_memory_repositories(base / 'records.db', base / 'vectors', vector_size=3)
                writer = MemoryWriter(repos, Embeddings())
                service = MemoryMaintenanceService(writer, repos)
                processor = MemoryReviewProcessor(writer, provider)
                try:
                    refs = []
                    if case.get('seed'):
                        seed_packet = packet_for({'user': case['seed']}, 'seed-' + case['id'])
                        seed = MemoryCandidate(content=case['seed'], kind=case.get('seed_kind', 'fact'),
                            content_status=case.get('seed_status', 'reported'), source_types=('user_statement',),
                            source_refs=(seed_packet.turn_id,), scope_kind='project', project_id=PROJECT,
                            occurred_at=seed_packet.occurred_at)
                        result['seed_packet'] = seed_packet.as_payload()
                        result['seed_processing'] = await prepare_and_commit(processor, writer, service,
                            seed_packet, [seed], [], base, 'seed', provider)
                        seeded = result['seed_processing']['saved']
                        if len(seeded) != 1:
                            raise RuntimeError('isolated_seed_not_saved')
                        target = seeded[0]
                        refs = [MemoryRef(target['id'], target['kind'], target['content_status'], target['content'], version=target['record_version'])]
                    packet = packet_for(case, case['id'] + '-' + str(index + 1), refs)
                    result['packet'] = packet.as_payload()
                    result['database_before'] = snapshot(repos)
                    provider.stage = 'extraction'
                    candidates, suggestions = await extractor.extract_maintenance(packet,
                        known_ids=[r.memory_id for r in refs], known_versions={r.memory_id: r.version for r in refs})
                    result['extracted_candidates'] = [asdict(x) for x in candidates]
                    result['extracted_suggestions'] = [asdict(x) for x in suggestions]
                    result.update(await prepare_and_commit(processor, writer, service, packet,
                        candidates, suggestions, base, 'batch', provider))
                    result['database_after'] = snapshot(repos)
                    later = Scope(SCOPE.owner_id, SCOPE.channel, 'quality-new-conversation', SCOPE.module_id)
                    recalled = MemoryRetriever(repos, Embeddings()).retrieve(later, case.get('seed') or case['user'], project_id=PROJECT)
                    result['new_conversation_recall'] = asdict(recalled)
                    for days in (2, 365):
                        result[f'maintenance_after_{days}_days'] = writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=days))
                        result[f'database_after_{days}_days'] = snapshot(repos)
                    result['pipeline_completed'] = True
                except Exception as exc:
                    result['error_type'] = type(exc).__name__
                    result['mechanical_findings'].append('pipeline_exception')
                finally:
                    result['database_final'] = snapshot(repos)
                    result['model_calls'] = provider.calls[call_start:]
                    result['failed_calls'] = sum(c['status'] != 'completed' for c in result['model_calls'])
                    repos.close()
            runs.append(result)
            report = make_report(runs, repeat, len(chosen) * repeat, provider)
            if output:
                Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
            print(json.dumps({'progress': len(runs), 'case': case['id'], 'repeat': index + 1,
                'pipeline_completed': result['pipeline_completed'], 'semantic_acceptance': 'unknown',
                'failed_calls': result['failed_calls']}, ensure_ascii=False), flush=True)
    return report

def make_report(runs, repeat, planned, provider):
    return {'date': datetime.now(UTC).isoformat(), 'repeat': repeat, 'planned_batches': planned,
        'code_sha256': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in [Path(__file__), ROOT / 'scripts/verify_memory_quality_live.py',
                                     *sorted((ROOT / 'agent_core').glob('memory_*.py')),
                                     *sorted((ROOT / 'cloud_memory').glob('*.py'))]},
        'completed_batches': len(runs), 'pipeline_completed': sum(r['pipeline_completed'] for r in runs),
        'semantic_acceptance': 'unknown', 'semantic_passed': 0, 'semantic_unknown': len(runs),
        'model_calls': len(provider.calls), 'failed_calls': sum(c['status'] != 'completed' for c in provider.calls),
        'seed_model_calls': sum(c['stage'].startswith('seed') for c in provider.calls),
        'total_tokens': sum(int((c.get('usage') or {}).get('total_tokens') or 0) for c in provider.calls),
        'production_memory_touched': False, 'independent_semantic_review_required': True,
        'limits': ['确定性向量替身仅验证隔离保存和召回机制，不验证语义向量排序。',
                   '程序只收集证据，逐条语义验收在独立审查前均为unknown。',
                   '失败调用和额外种子审查调用完整计入；模拟期限不等于真实长期运行。'], 'runs': runs}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeat', type=int, default=3)
    parser.add_argument('--out', required=True)
    parser.add_argument('--case', action='append', dest='selected')
    args = parser.parse_args()
    report = asyncio.run(run(args.repeat, args.out, args.selected))
    print(json.dumps({k: v for k, v in report.items() if k != 'runs'}, ensure_ascii=False))
    # Unknown is never success. Exit 2 means evidence captured, awaiting independent review.
    return 2 if report['pipeline_completed'] == report['planned_batches'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
