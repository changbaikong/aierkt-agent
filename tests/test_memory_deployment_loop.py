"""The isolated worker-to-new-session path; these substitutes prove wiring only."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


class Embeddings:
    dimensions = 3
    model = 'offline-fixed'

    def embed(self, text):
        return [.1, .2, .3]


class DeploymentProvider:
    model = 'offline-tools'

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            packet = json.loads(messages[1]['content'])['packet']
            assert packet['recalled_memory_refs'] == []
            assert packet['user_text'] == '部署方案暂时先不部署。'
            name, args = 'search_memory', dict(query='部署方案')
        elif self.calls == 2:
            records = json.loads(messages[-1]['content'])['records']
            target = next(r for r in records if r['content'] == '部署方案已决定立即部署')
            name, args = 'read_memory', dict(memory_id=target['id'])
        elif self.calls == 3:
            target = json.loads(messages[-1]['content'])['record']
            name, args = 'submit_memory_plan', dict(items=[dict(index=0,
                verdict='supported', write_action='replace', certainty='certain',
                content='部署方案暂时先不部署', subject='部署方案', kind='decision',
                content_status='accepted', scope_kind='project', reason='用户改变阶段决定',
                same_subject=True, preserves_negation=True, preserves_conditions=True,
                change_authorized=True, quotes=[dict(source_id='s0', start=0, end=11)],
                lifetime=dict(mode='phase', policy='phase', basis='到改变或结束',
                    end_condition='用户重新决定部署'), target_id=target['id'],
                target_version=target['record_version'])])
        else:
            assert self.calls == 4
            system = messages[0]['content']
            assert '部署方案暂时先不部署' in system
            current = system.split('[HISTORICAL_EVIDENCE]')[0]
            assert '部署方案已决定立即部署' not in current
            if '部署方案已决定立即部署' in system:
                assert '[HISTORICAL_EVIDENCE]' in system
                assert 'superseded' in system.split('[HISTORICAL_EVIDENCE]')[1]
            assert '本机' not in system
            assert messages[-1]['content'] == '这个项目现在决定怎么部署？'
            return SimpleNamespace(message=dict(role='assistant', content='目前决定暂不部署。'),
                usage={'completion_tokens': 20})
        return SimpleNamespace(message=dict(role='assistant', content='', tool_calls=[dict(
            id=f'call-{self.calls}', type='function', function=dict(name=name,
                arguments=json.dumps(args, ensure_ascii=False)))]), usage={'completion_tokens': 100})


@pytest.mark.asyncio
async def test_worker_update_reaches_a_new_sessions_real_answer_entry(tmp_path, monkeypatch):
    from scripts.verify_memory_deployment_loop_live import collect_case, RecordingProvider
    monkeypatch.setenv('AGENT_MEMORY_PROJECT', 'isolated-deployment-loop')
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
    report, inner = {}, DeploymentProvider()
    provider = RecordingProvider(inner, report, lambda: None)
    await collect_case(tmp_path, report, provider, Embeddings(), lambda: None)
    assert report['pipeline_completed'], report
    assert report['mechanical_findings'] == []
    assert report['new_session']['reply'] == '目前决定暂不部署。'
    assert report['new_session']['history'] == []
    assert report['queue']['pipeline'] == 'agent'
    assert report['queue']['extracted'] == 0
    assert report['queue']['status'] == 'completed'
    state = json.loads(report['queue']['agent_state'])
    assert [t['name'] for t in state['trace']] == ['search_memory', 'read_memory', 'submit_memory_plan']
    assert inner.calls == 4
    assert len(report['model_calls']) == 4
    assert all(c['status'] == 'completed' for c in report['model_calls'])
    assert report['semantic_acceptance'] == 'unknown', 'wiring cannot certify model understanding'
    assert report['new_session']['published_records'][-1]['content'] == report['new_session']['reply']
    assert report['runtime_store'] == str(tmp_path / 'answer-runtime.db')


@pytest.mark.asyncio
async def test_failed_agent_does_not_pay_for_a_new_session_answer(tmp_path, monkeypatch):
    from scripts.verify_memory_deployment_loop_live import collect_case, RecordingProvider
    class FailedProvider:
        model = 'offline-failure'

        async def complete(self, messages, **kwargs):
            raise TimeoutError('isolated failure')
    monkeypatch.setenv('AGENT_MEMORY_PROJECT', 'isolated-deployment-loop')
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
    report = {}
    provider = RecordingProvider(FailedProvider(), report, lambda: None)
    await collect_case(tmp_path, report, provider, Embeddings(), lambda: None)
    assert not report['pipeline_completed']
    assert len(report['model_calls']) == 1
    assert report['model_calls'][0]['status'] == 'failed'
    assert report['database_after'][0]['status'] == 'active'
    assert 'new_session' not in report


@pytest.mark.asyncio
async def test_saved_real_tool_outputs_replay_and_keep_decision_distinct_from_execution(tmp_path, monkeypatch):
    from agent_core.contracts import Scope
    from agent_core.memory_model_agent import MemoryModelAgent
    from agent_core.memory_repositories import initialize_memory_repositories
    from agent_core.memory_retrieval import MemoryRetriever
    from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket
    from agent_core.memory_writes import MemoryWriter
    from scripts.memory_test_support import seed_semantic
    evidence = Path(__file__).resolve().parents[1] / 'docs/2026-10-13-memory-deployment-loop-live.json'
    raw = json.loads(evidence.read_text(encoding='utf-8'))
    originals = [c['response'] for c in raw['model_calls'] if c['stage'] == 'memory_agent']
    packet = MemoryTurnPacket.from_payload(raw['packet'])
    scope = Scope(packet.scope_owner, packet.scope_channel, 'old-session', packet.scope_module)
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    try:
        writer = MemoryWriter(repos, Embeddings())
        old = seed_semantic(writer, scope, [MemoryCandidate(raw['seed_record']['content'],
            'decision', 'accepted', ('user_statement',), scope_kind='project',
            project_id=packet.project_binding, horizon='long_term')],
            turn_id='old-deployment-turn', project_binding=packet.project_binding,
            occurred_at=raw['seed_record']['created_at'])[0]
        class Replay:
            model = raw['configuration']['model']
            calls = 0

            async def complete(self, messages, **kwargs):
                message = json.loads(json.dumps(originals[self.calls], ensure_ascii=False).replace(
                    raw['seed_record']['id'], old['id']))
                self.calls += 1
                return SimpleNamespace(message=message, usage={})
        replay = Replay()
        agent = MemoryModelAgent(writer, replay)
        state = await agent.prepare(packet, persist=lambda _:None)
        result = agent.commit(packet, state)
        assert len(result.saved) == 1, result.as_dict()
        saved = result.saved[0]
        assert '暂时先不部署' in saved['content'] and '本机' not in saved['content']
        assert saved['expires_at'] is None
        assert repos.records_by_ids([old['id']])[old['id']]['effective_record_id'] == saved['id']
        assert replay.calls == len(originals) == 3
        monkeypatch.setenv('AGENT_MEMORY_PROJECT', packet.project_binding)
        monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
        fresh = Scope(scope.owner_id, scope.channel, 'brand-new-session', scope.module_id)
        context = MemoryRetriever(repos, Embeddings()).build_context(fresh, '这个项目现在决定怎么部署？')
        current = [r for r in context.warm if r['content_status'] not in ('superseded','rejected')]
        assert saved['id'] in {r['id'] for r in current}
        assert old['id'] not in {r['id'] for r in current}
        # The captured real answer wrongly asserted execution; its new context must
        # distinguish decision evidence from evidence of actual actions.
        assert '当前决定、选择或计划，不能据此声称已经执行或目前正在执行' in context.injected_text
        assert '没有实施依据时，实际执行状态保持未知' in context.injected_text
    finally:
        repos.close()


@pytest.mark.asyncio
async def test_answer_recheck_does_not_execute_worker_or_generate_another_version(tmp_path, monkeypatch):
    from agent_core.memory_queue import MemoryCandidateQueue
    from agent_core.memory_repositories import initialize_memory_repositories
    from agent_core.memory_writes import MemoryWriter
    from scripts.verify_memory_deployment_loop_live import collect_answer, collect_case, RecordingProvider, snapshot
    monkeypatch.setenv('AGENT_MEMORY_PROJECT', 'isolated-deployment-loop')
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
    report = {}
    await collect_case(tmp_path, report, RecordingProvider(DeploymentProvider(), report, lambda:None),
        Embeddings(), lambda:None)
    assert report['pipeline_completed']
    before = report['queue'].copy()
    semantic_before = {r['id']:r for r in report['database_after'] if r['kind'] == 'decision'}
    class AnswerOnly:
        model = 'offline-tools'

        async def complete(self, messages, **kwargs):
            assert not kwargs.get('tools')
            assert '没有实施依据时，实际执行状态保持未知' in messages[0]['content']
            return SimpleNamespace(message=dict(role='assistant', content='当前决定暂不部署。'), usage={})
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    try:
        await collect_answer(tmp_path, report, RecordingProvider(AnswerOnly(), report, lambda:None),
            Embeddings(), MemoryWriter(repos, Embeddings()), report['seed_record'], lambda:None,
            conversation_id='another-empty-session')
        queue = MemoryCandidateQueue(repos.db_path)
        assert queue.get(before['job_id']) == before
        semantic_after = {r['id']:r for r in snapshot(repos) if r['kind'] == 'decision'}
        assert semantic_after.keys() == semantic_before.keys(), 'answer replay must not create a decision'
        # Retrieval records access without changing the judgment or its version.
        access_fields = {'access_count', 'last_accessed_at', 'updated_at'}
        for ident, old in semantic_before.items():
            current = semantic_after[ident]
            assert {k:v for k,v in current.items() if k not in access_fields} == {
                k:v for k,v in old.items() if k not in access_fields}
            assert current['access_count'] >= old['access_count']
        assert len(report['model_calls']) == 5
        assert report['new_session']['history'] == []
        assert report['new_session']['reply'] == '当前决定暂不部署。'
    finally:
        repos.close()
