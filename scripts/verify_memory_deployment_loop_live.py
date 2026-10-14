"""Collect one isolated real-model deployment correction and new-session answer.

No batch retries, substitute vectors, semantic grading, or production databases.
The seeded old decision is an explicit test premise, not a model acceptance result.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.contracts import AgentRequest, Scope
from agent_core.loop import AgentLoop
from agent_core.memory_agent import MemoryAgentSession
from agent_core.memory_extract import build_semantic_extractor_from_env
from agent_core.memory_model_agent import MemoryModelAgent
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever, SUPERSEDED_STATUSES
from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket
from agent_core.memory_writes import MemoryWriter
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.prompts import PromptComposer
from agent_core.tools import ToolRegistry
from agent_core_adapters.bigmodel_embeddings import BigModelEmbeddingProvider
from scripts.memory_test_support import seed_semantic

PROJECT = 'isolated-deployment-loop'
OLD_TEXT = '部署方案已决定立即部署'
USER_TEXT = '部署方案暂时先不部署。'
ASSISTANT_TEXT = '那就先放本机运行。'
QUESTION = '这个项目现在决定怎么部署？'


class RecordingProvider:
    """Snapshot complete messages, including legal tool calls with empty content."""
    def __init__(self, inner, report, checkpoint):
        self.inner, self.report, self.checkpoint = inner, report, checkpoint
        self.model, self.stage = getattr(inner, 'model', ''), 'memory_agent'
        report.setdefault('model_calls', [])

    async def complete(self, messages, **kwargs):
        entry = dict(stage=self.stage, request=deepcopy(messages), options=deepcopy(kwargs),
                     status='started', response=None, usage={})
        self.report['model_calls'].append(entry)
        self.checkpoint()
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(self.inner.complete(messages, **kwargs), timeout=60)
            message = deepcopy(response.message or {})
            entry.update(status='completed', response=message,
                         usage=deepcopy(getattr(response, 'usage', {}) or {}))
            if not message.get('tool_calls') and not str(message.get('content') or '').strip():
                entry.update(status='invalid_response', error_type='EmptyModelOutput')
            return response
        except BaseException as exc:
            entry.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            entry['latency_ms'] = round((time.monotonic()-started)*1000)
            self.checkpoint()


class RecordingEmbeddings:
    def __init__(self, inner, report):
        self.inner, self.report = inner, report
        self.dimensions, self.model = inner.dimensions, inner.model
        report['embedding_calls'] = []

    def embed(self, text):
        entry = dict(text=text, status='started')
        self.report['embedding_calls'].append(entry)
        started = time.monotonic()
        try:
            vector = self.inner.embed(text)
            if len(vector) != self.dimensions:
                raise ValueError('configured_embedding_dimensions_mismatch')
            entry.update(status='completed', dimensions=len(vector),
                vector_sha256=hashlib.sha256(json.dumps(vector).encode()).hexdigest())
            return vector
        except BaseException as exc:
            entry.update(status='failed', error_type=type(exc).__name__)
            raise
        finally:
            entry['latency_ms'] = round((time.monotonic()-started)*1000)


def snapshot(repos):
    with repos.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM memory_records ORDER BY created_at,id')]


def check_commit(report, state, old):
    """Structural checks only; the saved meaning and answer need independent review."""
    findings = report['mechanical_findings']
    result = json.loads(report['queue'].get('processing_result') or '{}')
    saved = result.get('saved') or []
    if len(saved) != 1:
        findings.append('expected_one_saved_deployment_decision')
    latest = {r['id']: r for r in report['database_after']}
    fresh_old = latest[old['id']]
    if (fresh_old['status'] != 'archived' or
            fresh_old.get('effective_record_id') not in {r['id'] for r in saved}):
        findings.append('old_decision_not_replaced')
    traces = state.get('trace') or []
    if not any(t['name'] == 'search_memory' and old['id'] in
        {r['id'] for r in (t.get('result') or {}).get('records', [])} for t in traces):
        findings.append('agent_did_not_find_old_decision')
    if not any(t['name'] == 'read_memory' and
        (t.get('result') or {}).get('record', {}).get('id') == old['id'] for t in traces):
        findings.append('agent_did_not_read_old_decision')
    for row in saved:
        judgment = json.loads(row['metadata'])['evidence_receipt']['semantic_judgment']
        if row['expires_at'] is not None or judgment.get('lifetime', {}).get('mode') != 'phase':
            findings.append('stage_decision_has_wrong_lifetime')
        quotes = judgment.get('quotes') or []
        if not any(q.get('message_id') == report['packet']['turn_id'] and
                   q.get('source_type') == 'user_statement' for q in quotes):
            findings.append('new_decision_missing_current_user_source')
        if any(q.get('source_type') == 'assistant_proposal' for q in quotes):
            findings.append('unaccepted_assistant_proposal_used')
    if report['queue']['extracted'] or report['queue']['pipeline'] != 'agent':
        findings.append('legacy_extraction_also_used')


async def collect_answer(base, report, provider, embeddings, writer, old, checkpoint,
                         *, conversation_id='fresh-session'):
    # Explicit runtime stores keep all writes inside the same isolated workspace.
    runtime = Path(base)/'answer-runtime.db'
    report['runtime_store'] = str(runtime)
    packet = MemoryTurnPacket.from_payload(report['packet'])
    later = Scope(packet.scope_owner, packet.scope_channel, conversation_id, packet.scope_module)
    loop = AgentLoop(provider, ToolRegistry(), later,
        run_store=SQLiteRunStore(runtime), confirmation_store=SQLiteConfirmationStore(runtime), max_steps=2)
    session = MemoryAgentSession(loop, writer, MemoryRetriever(writer.repositories, embeddings),
        prompt_composer=PromptComposer())
    provider.stage = 'fresh_session_answer'
    report['new_session'] = dict(scope=asdict(later), history=[], question=QUESTION, status='started')
    checkpoint()
    outcome = await asyncio.wait_for(session.run(AgentRequest(QUESTION, scope=later, history=[])), timeout=180)
    published = [r for r in writer.list_records(later) if r['layer'] == 'hot']
    report['new_session'].update(status=outcome.status, reply=outcome.reply,
        handoff=outcome.handoff, published_records=published)
    recalled = (outcome.handoff or {}).get('recalled_memory_refs') or []
    current = [r for r in recalled if r.get('content_status') not in SUPERSEDED_STATUSES]
    report['new_session']['current_memory_refs'] = current
    report['new_session']['historical_memory_refs'] = [r for r in recalled if r not in current]
    saved = json.loads(report['queue']['processing_result']).get('saved') or []
    if not {r['id'] for r in saved} <= {r['memory_id'] for r in current}:
        report['mechanical_findings'].append('fresh_session_did_not_recall_new_decision')
    if old['id'] in {r['memory_id'] for r in current}:
        report['mechanical_findings'].append('fresh_session_recalled_old_as_current_decision')
    if outcome.status != 'completed' or not outcome.reply:
        report['mechanical_findings'].append('fresh_session_answer_not_completed')
    if any(c['status'] != 'completed' for c in report.get('embedding_calls', [])):
        report['mechanical_findings'].append('real_embedding_call_failed')
    report['pipeline_completed'] = not report['mechanical_findings']
    checkpoint()


async def collect_case(base, report, provider, embeddings, checkpoint):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    report.update(pipeline_completed=False, semantic_acceptance='unknown',
        independent_review_required=True, mechanical_findings=[],
        production_memory_touched=False,
        seed_premise='给定旧决定立即部署；种子不计作真实模型理解验收',
        limits=['仅本地后台和正式新会话回答入口；未覆盖云端、HTTP、生产部署或连续运行。',
                '单一样例不能代替阶段2全部验收，也不能打开C门槛。'])
    repos = initialize_memory_repositories(base/'records.db', base/'vectors',
        vector_size=embeddings.dimensions)
    try:
        writer = MemoryWriter(repos, embeddings)
        old_scope = Scope('isolated-owner', 'web', 'old-session', 'companion')
        old = seed_semantic(writer, old_scope, [MemoryCandidate(OLD_TEXT, 'decision', 'accepted',
            ('user_statement',), scope_kind='project', project_id=PROJECT, horizon='long_term')],
            turn_id='old-deployment-turn', project_binding=PROJECT)[0]
        report['seed_record'] = old
        packet = MemoryTurnPacket('deployment-change-turn', datetime.now(UTC).isoformat(),
            old_scope.owner_id, old_scope.channel, 'changed-session',
            scope_module=old_scope.module_id, project_binding=PROJECT,
            user_text=USER_TEXT, published_reply=ASSISTANT_TEXT, recalled_memory_refs=[])
        report['packet'] = packet.as_payload()
        report['database_before'] = snapshot(repos)
        queue = MemoryCandidateQueue(repos.db_path)
        queue.initialize()
        ident, _ = queue.enqueue(packet)
        agent = MemoryModelAgent(writer, provider)
        provider.stage = 'memory_agent'
        worker = MemoryCandidateWorker(queue, None, writer, model_agent=agent)
        report['worker_result'] = await worker.process_once()
        report['queue'] = queue.get(ident)
        report['database_after'] = snapshot(repos)
        checkpoint()
        if report['queue']['status'] != 'completed':
            report['mechanical_findings'].append('agent_job_not_completed')
            return report
        state = json.loads(report['queue']['agent_state'])
        check_commit(report, state, old)
        if report['mechanical_findings']:
            return report
        await collect_answer(base, report, provider, embeddings, writer, old, checkpoint)
        return report
    finally:
        report['database_final'] = snapshot(repos)
        checkpoint()
        repos.close()


async def run(output, *, answer_from=None):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(date=datetime.now(UTC).isoformat(), model_calls=[], embedding_calls=[],
                  attempts=1, stage2_complete=False, c_gate_open=False)
    if answer_from:
        source = Path(answer_from).resolve()
        if source == output:
            raise ValueError('must_preserve_original_evidence')
        report = json.loads(source.read_text(encoding='utf-8'))
        report.update(date=datetime.now(UTC).isoformat(), pipeline_completed=False,
            semantic_acceptance='unknown', mechanical_findings=[],
            source_evidence=str(source), source_evidence_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            previous_answer=deepcopy(report.get('new_session')), answer_recheck_attempts=1)
    def checkpoint():
        temporary = output.with_suffix(output.suffix+'.part')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')
        temporary.replace(output)
    try:
        configured = build_semantic_extractor_from_env()
        if configured is None:
            raise RuntimeError('memory_model_not_configured')
        vector_provider = BigModelEmbeddingProvider.from_env()
        configuration = dict(model=configured.provider.model,
            embedding_model=vector_provider.model, dimensions=vector_provider.dimensions,
            model_call_seconds=60, execution_seconds=180, task_seconds=600)
        if answer_from and configuration != report['configuration']:
            raise ValueError('configured_model_or_embedding_changed')
        report['configuration'] = configuration
        base = Path(report['isolated_workspace']) if answer_from else Path(tempfile.mkdtemp(prefix='deployment-memory-loop-'))
        if answer_from and (base.resolve().parent != Path(tempfile.gettempdir()).resolve() or
                            not base.name.startswith('deployment-memory-loop-')):
            raise ValueError('not_an_isolated_deployment_workspace')
        report['isolated_workspace'] = str(base)
        report['recheck_code_sha256' if answer_from else 'code_sha256'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), ROOT/'agent_core/memory_model_agent.py',
                      ROOT/'agent_core/memory_job_runtime.py', ROOT/'agent_core/memory_queue.py',
                      ROOT/'agent_core/memory_writes.py', ROOT/'agent_core/memory_retrieval.py',
                      ROOT/'agent_core/memory_agent.py', ROOT/'agent_core/memory_protocol.py']}
        # Scoped to this isolated process. Keep the configured model and service.
        os.environ['MEMORY_SEMANTIC_ENABLED'] = 'true'
        os.environ['AGENT_MEMORY_PROJECT'] = PROJECT
        provider = RecordingProvider(configured.provider, report, checkpoint)
        embeddings = RecordingEmbeddings(vector_provider, report)
        if answer_from:
            # Keep the first attempt's calls and failure answer; no second judgment,
            # seed, queue execution, or semantic version is generated.
            prior_embeddings = json.loads(source.read_text(encoding='utf-8'))['embedding_calls']
            report['embedding_calls'][:0] = prior_embeddings
            repos = initialize_memory_repositories(base/'records.db', base/'vectors',
                vector_size=embeddings.dimensions)
            try:
                saved = json.loads(report['queue']['processing_result'])['saved']
                rows = repos.records_by_ids([report['seed_record']['id'], *(r['id'] for r in saved)])
                for expected in report['database_after']:
                    if expected['id'] in rows and any(rows[expected['id']].get(k) != expected.get(k)
                        for k in ('owner_id', 'content', 'status', 'record_version', 'effective_record_id')):
                        raise ValueError('isolated_memory_changed_since_original_run')
                if len(rows) != len(saved)+1:
                    raise ValueError('original_memory_not_available')
                await collect_answer(base, report, provider, embeddings, MemoryWriter(repos, embeddings),
                    report['seed_record'], checkpoint, conversation_id='fresh-session-recheck')
                report['database_final'] = snapshot(repos)
            finally:
                repos.close()
        else:
            await collect_case(base, report, provider, embeddings, checkpoint)
    except BaseException as exc:
        report.update(pipeline_completed=False, error_type=type(exc).__name__)
    finally:
        report['failed_calls'] = sum(c['status'] != 'completed' for c in report['model_calls'])
        report['total_tokens'] = sum(int(c['usage'].get('total_tokens') or 0) for c in report['model_calls'])
        checkpoint()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--answer-from', help='Reuse this saved isolated run; call only the new-session answer.')
    args = parser.parse_args()
    report = asyncio.run(run(args.out, answer_from=args.answer_from))
    print(json.dumps({k:report.get(k) for k in ('pipeline_completed', 'semantic_acceptance',
        'error_type', 'mechanical_findings', 'failed_calls', 'total_tokens')}, ensure_ascii=False))
    print(json.dumps(dict(model_calls=len(report['model_calls']),
        embedding_calls=len(report['embedding_calls']), evidence=str(Path(args.out).resolve())), ensure_ascii=False))
    # Evidence capture cannot grade meaning. 2 means ready for independent review.
    return 2 if report.get('pipeline_completed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
