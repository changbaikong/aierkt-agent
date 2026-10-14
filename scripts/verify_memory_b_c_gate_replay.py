"""Replay retained Q17 live outputs against isolated memory and a new conversation."""
import gc
import json
import os
import sys
import tempfile
import asyncio
import argparse
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from datetime import datetime, UTC
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor, build_semantic_extractor_from_env
from agent_core.memory_processing import MemoryReviewProcessor
from scripts.verify_memory_model_led_live import RecordingProvider, prepare_and_commit, snapshot
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_turn import (
    MemoryCandidate,
    MemoryRef,
    MemoryTurnPacket,
    candidate_from_payload,
    decode_suggestions,
    scope_from_packet,
)
from agent_core.memory_writes import MemoryWriter

class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class SavedReviewProvider:
    """Return a previously saved review response without contacting a model."""

    model = 'saved-output-replay'

    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(message={'content': self.raw}, usage={})


def _candidate_rows(rows):
    return [candidate_from_payload(row) for row in rows if isinstance(row, dict)]


def _suggestion_rows(rows):
    return decode_suggestions(json.dumps([row for row in rows if isinstance(row, dict)], ensure_ascii=False))


def _replace_ids(value, mapping):
    if not mapping:
        return value
    if isinstance(value, str):
        for old, new in mapping.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [_replace_ids(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _replace_ids(item, mapping) for key, item in value.items()}
    return value


def _seed_saved_rows(rows, writer, scope, project_id):
    """Recreate saved isolated seed rows and return old-id -> new-id mapping."""
    mapping = {}
    for row in rows or ():
        if not isinstance(row, dict) or not row.get('content'):
            continue
        try:
            metadata = json.loads(row.get('metadata') or '{}')
        except (TypeError, ValueError):
            metadata = {}
        kind = str(row.get('kind') or 'fact')
        content_status = str(row.get('content_status') or 'reported')
        source_types = tuple(metadata.get('source_types') or ('user_statement',))
        if kind == 'fact' and content_status == 'verified':
            source_types = ('tool_result',)
        if kind == 'plan' and content_status == 'completed':
            source_types = ('tool_result',)
        candidate = MemoryCandidate(
            content=str(row['content']), kind=kind, content_status=content_status,
            source_types=source_types, scope_kind=str(row.get('scope_kind') or 'project'),
            occurred_at=str(row.get('created_at') or ''),
            reason=str(metadata.get('reason') or ''), goal=str(metadata.get('goal') or ''),
            project_id=str(row.get('project_id') or project_id),
            project_label=str(metadata.get('project_label') or ''),
            topics=tuple(metadata.get('topics') or ()),
            source_refs=tuple(metadata.get('source_refs') or ('saved-seed',)),
            evidence_level=str(metadata.get('evidence_level') or ''),
            origin='program' if (kind, content_status) in {('fact', 'verified'), ('plan', 'completed')} else 'model_candidate',
            horizon=str(metadata.get('horizon') or ''),
        )
        seeded = seed_semantic(writer, scope, [candidate], turn_id='saved-seed', project_binding=project_id)
        if seeded and row.get('id'):
            mapping[str(row['id'])] = seeded[0]['id']
    return mapping


def _saved_extraction(sample, packet, known_ids, known_versions):
    """Parse the saved extraction response; payload rows are a compatibility fallback."""
    extraction_raw = next((call.get('raw') for call in sample.get('model_calls', [])
                           if call.get('stage') == 'extraction' and isinstance(call.get('raw'), str)
                           and call.get('raw').strip()), None)
    extraction_raw = extraction_raw or sample.get('replay_raw') or sample.get('original_raw')
    if extraction_raw:
        parsed = SemanticMemoryExtractor(None).parse_maintenance(
            extraction_raw, packet=packet, known_ids=known_ids, known_versions=known_versions)
        return parsed[0], parsed[1], parsed[2], extraction_raw, 'raw'
    candidates = _candidate_rows(sample.get('extracted_candidates') or [])
    suggestions = _suggestion_rows(sample.get('extracted_suggestions') or [])
    return candidates, suggestions, list(sample.get('parse_rejected') or []), None, 'payload'


def _replay_status(result, *, has_review):
    """Classify replay mechanics without turning them into semantic scores."""
    processing = result.get('processing') or {}
    result['replay_blockers'] = []
    if not has_review:
        result['replay_blockers'] = ['review_output_missing']
        return 'missing_review_output'
    if result.get('error_type'):
        return 'replay_error'
    if processing.get('pending_review'):
        blockers = []
        for entry in processing['pending_review']:
            receipt = entry.get('evidence_receipt') or {}
            reason = receipt.get('review_error') or receipt.get('binding_gap')
            if reason and reason not in blockers:
                blockers.append(reason)
        result['replay_blockers'] = blockers
        return 'saved_review_rejected_by_current_protocol'
    if processing.get('rejected'):
        result['replay_blockers'] = ['unsupported_by_saved_review']
        return 'saved_review_rejected_by_current_protocol'
    if processing.get('failures'):
        result['replay_blockers'] = ['processing_failure']
        return 'replay_failure'
    return 'replayed_to_commit_path'


def _saved_replay_report(evidence_path, results, replayed_output_count):
    """Build a replay report that separates missing fixtures from protocol rejects."""
    status_counts = dict(sorted(Counter(row.get('replay_status', 'replay_error') for row in results).items()))

    def status_rows(status):
        return [
            {
                'case': row.get('case', ''),
                'repeat': row.get('repeat'),
                'replay_status': row.get('replay_status'),
                'replay_blockers': row.get('replay_blockers', []),
            }
            for row in results
            if row.get('replay_status') == status
        ]

    return dict(
        source=str(evidence_path.relative_to(ROOT)) if evidence_path.is_relative_to(ROOT) else str(evidence_path),
        runs=results, mode='saved_output_replay', extraction_model_calls=0,
        model_calls=0, paid_model_calls=0, replayed_output_count=replayed_output_count,
        failed_calls=0, status_counts=status_counts,
        missing_review_outputs=status_rows('missing_review_output'),
        protocol_rejections=status_rows('saved_review_rejected_by_current_protocol'),
        semantic_acceptance='unknown', independent_semantic_review_required=True,
        production_memory_touched=False,
        limits=['仅重放已保存的原始提炼/复核输出，不产生模型调用。',
                '没有保存复核输出的样例保持未完成，不能据此判定语义通过。',
                '固定向量只验证保存、版本和召回通路，不验证语义排序。'])

async def collect(*, review_provider=None, output=None):
    os.environ['MEMORY_SEMANTIC_ENABLED'] = 'true'
    os.environ['MEMORY_SEMANTIC_RECALL_ENABLED'] = 'true'
    evidence = json.loads((ROOT / 'docs/2026-10-12-memory-b-c-gate-live.json').read_text(encoding='utf-8'))
    if review_provider is None:
        configured = build_semantic_extractor_from_env()
        if configured is None:
            raise RuntimeError('configured_review_model_missing')
        review_provider = configured.provider
        mode = 'configured_live_review'
    else:
        mode = 'injected_offline_fixture'
    provider = RecordingProvider(review_provider)
    results = []
    for sample in [x for x in evidence['runs'] if x['case'] == 'Q17']:
        call_start = len(provider.calls)
        result = dict(case='Q17',repeat=sample['repeat'],original_raw=sample['raw'],original_packet=sample['packet'],
            semantic_acceptance='unknown',pipeline_completed=False,
            independent_expectation='排期不调整与部署不部署分开；部署旧立即执行退出，新会话召回暂缓，不得补本机地点。')
        with tempfile.TemporaryDirectory(prefix='b-c-replay-', ignore_cleanup_errors=True) as folder:
            base = Path(folder)
            repos = initialize_memory_repositories(base / 'records.db', base / 'vectors', vector_size=3)
            try:
                scope = Scope('quality-user', 'web', 'quality-conversation', 'companion')
                project = 'isolated-quality-project'
                writer = MemoryWriter(repos, Embeddings())
                service = MemoryMaintenanceService(writer, repos)
                retriever = MemoryRetriever(repos, Embeddings())
                old = seed_semantic(writer, scope, [MemoryCandidate(
                    content='部署方案为立即执行', kind='decision', content_status='accepted',
                    source_types=('user_statement',), source_refs=('seed',), scope_kind='project',
                    project_id=project, occurred_at='2026-10-10T09:00:00+00:00')],
                    turn_id='seed', project_binding=project)[0]
                original = sample['packet']
                ref = MemoryRef(old['id'], old['kind'], old['content_status'], old['content'], version=old['record_version'])
                packet = MemoryTurnPacket(turn_id=original['turn_id'], occurred_at=datetime.now(UTC).isoformat(),
                    scope_owner=scope.owner_id, scope_channel=scope.channel, scope_conversation=scope.conversation_id,
                    scope_module=scope.module_id, project_binding=project,
                    user_text=original['user_text'], published_reply=original['published_reply'],
                    recalled_memory_refs=[ref])
                original_id = original['recalled_memory_refs'][0]['memory_id']
                raw = sample['raw'].replace(original_id, old['id'])
                candidates, suggestions, rejected = SemanticMemoryExtractor(None).parse_maintenance(
                    raw, packet=packet, known_ids=[old['id']], known_versions={old['id']: old['record_version']})
                result.update(packet=packet.as_payload(),replay_raw=raw,id_mapping={original_id:old['id']},
                    parsed_candidates=[asdict(x) for x in candidates],parsed_suggestions=[asdict(x) for x in suggestions],
                    parse_rejected=rejected,database_before=snapshot(repos))
                result.update(await prepare_and_commit(MemoryReviewProcessor(writer,provider),writer,service,
                    packet,candidates,suggestions,base,'replay',provider))
                fresh = repos.records_by_ids([old['id']])[old['id']]
                later = Scope(scope.owner_id, scope.channel, 'gate-new-conversation', scope.module_id)
                recalled = retriever.retrieve(later, '部署方案', project_id=project)
                result.update(old_after=fresh,new_conversation_recall=asdict(recalled),pipeline_completed=True)
            except Exception as exc:
                result['error_type'] = type(exc).__name__
            finally:
                result['database_after'] = snapshot(repos)
                result['review_calls'] = provider.calls[call_start:]
                results.append(result)
                repos.close()
                gc.collect()
    report = dict(source='docs/2026-10-12-memory-b-c-gate-live.json', runs=results,
        mode=mode,extraction_model_calls=0,review_model_calls=len(provider.calls),
        failed_review_calls=sum(x['status']!='completed' for x in provider.calls),
        semantic_acceptance='unknown',independent_semantic_review_required=True,production_memory_touched=False,
        limits=['提炼仅重放，真实复核调用单计；独立审查前语义unknown。', 'Fixed vectors verify current-version filtering, not ranking quality.'])
    if output:
        Path(output).parent.mkdir(parents=True,exist_ok=True)
        Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True,default=str), flush=True)
    return report


async def collect_saved_output_replay(*, evidence_path, output=None):
    """Replay retained extraction/review responses in an isolated database.

    This path intentionally has no configured model provider. A saved review
    response is fed through the normal validation and commit path; a run whose
    review response was never saved remains pending and cannot be committed.
    """
    evidence_path = Path(evidence_path)
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    results = []
    replayed_output_count = 0
    for sample in evidence.get('runs', []):
        call_start = replayed_output_count
        review_raw = (sample.get('review_cache') or {}).get('raw')
        has_review = isinstance(review_raw, str) and bool(review_raw.strip())
        result = dict(
            case=sample.get('case', ''), repeat=sample.get('repeat'),
            original_packet=sample.get('packet'),
            original_extraction_raw=next((call.get('raw') for call in sample.get('model_calls', [])
                                          if call.get('stage') == 'extraction'), None),
            original_review_raw=(sample.get('review_cache') or {}).get('raw'),
            semantic_acceptance='unknown', pipeline_completed=False,
            independent_expectation=sample.get('independent_expectation', ''),
            model_calls=[], paid_model_calls=0,
        )
        with tempfile.TemporaryDirectory(prefix='saved-memory-replay-', ignore_cleanup_errors=True) as folder:
            base = Path(folder)
            repos = initialize_memory_repositories(base / 'records.db', base / 'vectors', vector_size=3)
            try:
                original = MemoryTurnPacket.from_payload(sample.get('packet') or {})
                scope = scope_from_packet(original)
                project = original.project_binding or 'isolated-quality-project'
                writer = MemoryWriter(repos, Embeddings())
                service = MemoryMaintenanceService(writer, repos)
                mapping = _seed_saved_rows(sample.get('database_before') or [], writer, scope, project)
                refs = []
                for ref in original.recalled_memory_refs:
                    memory_id = mapping.get(ref.memory_id, ref.memory_id)
                    row = repos.records_by_ids([memory_id]).get(memory_id)
                    refs.append(MemoryRef(memory_id, ref.kind, ref.content_status,
                                         (row or {}).get('content', ref.excerpt),
                                         version=(row or {}).get('record_version', ref.version),
                                         layer=ref.layer, semantic_judgment=ref.semantic_judgment))
                original.recalled_memory_refs = refs
                sample_for_parse = _replace_ids(sample, mapping)
                candidates, suggestions, rejected, extraction_raw, extraction_source = _saved_extraction(
                    sample_for_parse, original, [r.memory_id for r in refs],
                    {r.memory_id: r.version for r in refs})
                result.update(
                    packet=original.as_payload(), id_mapping=mapping,
                    extracted_candidates=[asdict(x) for x in candidates],
                    extracted_suggestions=[asdict(x) for x in suggestions],
                    parse_rejected=rejected, extraction_source=extraction_source,
                    database_before=snapshot(repos),
                )
                replay_provider = SavedReviewProvider(_replace_ids(review_raw, mapping)) if review_raw else None
                if review_raw:
                    replayed_output_count += 1
                processor = MemoryReviewProcessor(writer, replay_provider)
                commit = await prepare_and_commit(processor, writer, service, original,
                    candidates, suggestions, base, 'saved-replay', replay_provider)
                result.update(commit)
                result['database_after'] = snapshot(repos)
                result['database_final'] = result['database_after']
                later = Scope(original.scope_owner, original.scope_channel,
                              original.scope_conversation + '-new', original.scope_module)
                query = original.user_text or '记忆'
                recalled = MemoryRetriever(repos, Embeddings()).retrieve(later, query, project_id=project, include_semantic=True)
                result['new_conversation_recall'] = asdict(recalled)
                result['pipeline_completed'] = bool(
                    not result.get('processing', {}).get('pending_review')
                    and not result.get('processing', {}).get('failures')
                    and not result.get('processing', {}).get('rejected')
                    and (result.get('prepared_candidates') or result.get('prepared_suggestions')
                         or (not candidates and not suggestions)))
            except Exception as exc:
                result['error_type'] = type(exc).__name__
                result['pipeline_completed'] = False
                result.setdefault('database_before', snapshot(repos))
                result['database_after'] = snapshot(repos)
                result['database_final'] = result['database_after']
            finally:
                result.setdefault('database_after', snapshot(repos))
                result.setdefault('database_final', result['database_after'])
                result['model_calls'] = []
                result['replayed_outputs'] = replayed_output_count - call_start
                repos.close()
                gc.collect()
        result['replay_status'] = _replay_status(result, has_review=has_review)
        results.append(result)
        report = _saved_replay_report(evidence_path, results, replayed_output_count)
        if output:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
        print(json.dumps({'progress': len(results), 'case': result['case'],
                          'repeat': result['repeat'], 'pipeline_completed': result['pipeline_completed'],
                          'semantic_acceptance': 'unknown'}, ensure_ascii=False), flush=True)
    report = _saved_replay_report(evidence_path, results, replayed_output_count)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, default=str), flush=True)
    return report

def run(**kwargs):
    watched = ('MEMORY_SEMANTIC_ENABLED','MEMORY_SEMANTIC_RECALL_ENABLED')
    previous = {key:os.environ.get(key) for key in watched}
    try:
        if kwargs.pop('free_replay', False):
            evidence_path = kwargs.pop('evidence_path', ROOT / 'docs/2026-10-13-memory-model-led-live.json')
            return asyncio.run(collect_saved_output_replay(evidence_path=evidence_path, **kwargs))
        return asyncio.run(collect(**kwargs))
    finally:
        for key,value in previous.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',default=str(ROOT/'docs/2026-10-13-memory-b-c-gate-model-review-replay.json'))
    parser.add_argument('--free-replay', action='store_true')
    parser.add_argument('--evidence', default=str(ROOT/'docs/2026-10-13-memory-model-led-live.json'))
    args=parser.parse_args()
    report=run(output=args.out, free_replay=args.free_replay, evidence_path=args.evidence)
    raise SystemExit(2 if all(x['pipeline_completed'] for x in report['runs']) else 1)
