"""Isolated agent tool/commit checks. Fixed embeddings prove transport only."""
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket
from agent_core.memory_writes import MemoryWriter
from scripts.memory_test_support import seed_semantic


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


@pytest.fixture
def stack(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    writer = MemoryWriter(repos, Embeddings())
    scope = Scope('owner', 'web', 'old-session', 'companion')
    old = seed_semantic(writer, scope, [MemoryCandidate('部署方案为立即执行', 'decision', 'accepted',
        ('user_statement',), scope_kind='project', project_id='project', source_refs=('old-turn',),
        horizon='long_term')], turn_id='old-turn', project_binding='project')[0]
    packet = MemoryTurnPacket('new-turn', (datetime.now(UTC)+timedelta(seconds=1)).isoformat(),
        'owner', 'web', 'new-session', project_binding='project',
        user_text='排期方案暂不调整；部署方案暂时先不部署。', published_reply='排期不动，部署先放本机。')
    yield repos, writer, old, packet
    repos.close()


def tool(name, args, ident='call'):
    return dict(id=ident, type='function', function=dict(name=name, arguments=json.dumps(args)))


def decision(index, content, subject, start, end, *, action='add', target=None):
    return dict(index=index, verdict='supported', write_action=action, certainty='certain',
        content=content, subject=subject, kind='decision', content_status='accepted', scope_kind='project',
        reason='原话阶段决定，地点来自助手补充不采纳', same_subject=True, preserves_negation=True,
        preserves_conditions=True, change_authorized=action=='replace',
        quotes=[dict(source_id='s0', start=start, end=end)],
        lifetime=dict(mode='phase', policy='phase', basis='直到改变或结束', end_condition='用户改变决定'),
        target_id=target['id'] if target else None, target_version=target['record_version'] if target else None)


class Scripted:
    model = 'offline-tools'

    def __init__(self, old, packet, *, keep=False):
        self.old, self.packet, self.keep, self.calls = old, packet, keep, 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        assert len(kwargs['tools']) == 4
        if self.calls == 1:
            data = json.loads(messages[1]['content'])
            assert data['packet']['recalled_memory_refs'] == []
            assert data['packet']['user_text'] == self.packet.user_text
            message = dict(content='', tool_calls=[tool('search_memory', dict(query='部署方案'), 'search')])
        elif self.calls == 2:
            hits = json.loads(messages[-1]['content'])['records']
            assert self.old['id'] in {r['id'] for r in hits}
            message = dict(content='', tool_calls=[tool('read_memory', dict(memory_id=self.old['id']), 'read')])
        else:
            full = json.loads(messages[-1]['content'])
            assert full['record']['content'] == '部署方案为立即执行'
            text = self.packet.user_text
            first_text, second_text = text.rstrip('。').split('；')
            first = decision(0, first_text, '排期方案', text.index(first_text), text.index(first_text)+len(first_text))
            second = decision(1, second_text, '部署方案', text.index(second_text), text.index(second_text)+len(second_text), action='replace', target=self.old)
            if self.keep:
                second.update(write_action='keep_existing', same_fact=True, creates_new_fact=False,
                              change_authorized=False, content='部署方案为立即执行')
                plan = dict(items=[second])
            else:
                plan = dict(items=[first, second])
            message = dict(content='', tool_calls=[tool('submit_memory_plan', plan)])
        return SimpleNamespace(message=message, usage={'completion_tokens': 100})


@pytest.mark.asyncio
async def test_agent_searches_without_initial_recall_and_replaces_atomically(stack):
    from agent_core.memory_model_agent import MemoryModelAgent
    from agent_core.memory_retrieval import MemoryRetriever
    repos, writer, old, packet = stack
    provider, durable = Scripted(old, packet), []
    agent = MemoryModelAgent(writer, provider)
    plan = await agent.prepare(packet, persist=durable.append)
    assert repos.records_by_ids([old['id']])[old['id']]['status'] == 'active', 'submission only persists a plan'
    assert durable[-1]['plan'] == plan['plan']
    result = agent.commit(packet, plan)
    assert len(result.saved) == 2, result.as_dict()
    fresh = repos.records_by_ids([old['id']])[old['id']]
    assert fresh['status'] == 'archived'
    assert fresh['effective_record_id'] in {r['id'] for r in result.saved}
    assert all('本机' not in r['content'] and r['expires_at'] is None for r in result.saved)
    for record in result.saved:
        judgment = json.loads(record['metadata'])['evidence_receipt']['semantic_judgment']
        assert judgment['quotes'][0]['quote'] == record['content']
    recalled = MemoryRetriever(repos, Embeddings()).retrieve(Scope('owner', 'web', 'later', 'companion'),
        '部署方案', project_id='project', include_semantic=True)
    assert old['id'] not in {r['id'] for r in recalled.warm}
    assert any(r['content'] == '部署方案暂时先不部署' for r in recalled.warm)
    calls = provider.calls
    resumed = await agent.prepare(packet, cached=durable[-1], persist=durable.append)
    repeated = agent.commit(packet, resumed)
    assert {r['id'] for r in repeated.saved} == {r['id'] for r in result.saved}
    assert provider.calls == calls, 'completed judgment and commit are reused after restart'


@pytest.mark.asyncio
async def test_agent_cannot_read_other_owner_or_use_forged_tool_arguments(stack):
    from agent_core.memory_model_agent import MemoryModelAgent
    repos, writer, old, packet = stack
    other = seed_semantic(writer, Scope('other', 'web', 'private', 'companion'), [
        MemoryCandidate('private data', 'decision', 'accepted', ('user_statement',),
            scope_kind='conversation', source_refs=('private-turn',), horizon='long_term')], turn_id='private-turn')[0]
    class Attempts:
        async def complete(self, messages, **kwargs):
            return SimpleNamespace(message=dict(content='', tool_calls=[
                tool('read_memory', dict(memory_id=other['id']), 'private'),
                tool('search_memory', dict(query='部署', owner_id='other'), 'forged')]), usage={})
    durable = []
    prepared = await MemoryModelAgent(writer, Attempts()).prepare(packet, persist=durable.append)
    assert prepared['status'] == 'unresolved'
    assert all('private data' not in json.dumps(t, ensure_ascii=False) for t in prepared['trace'])
    assert any(t.get('error') == 'invalid_tool_arguments' for t in prepared['trace'])
    assert repos.records_by_ids([old['id']])[old['id']]['status'] == 'active'


@pytest.mark.asyncio
async def test_response_saved_before_crash_is_processed_without_another_model_call(stack):
    from agent_core.memory_model_agent import MemoryModelAgent
    _, writer, old, packet = stack
    provider, durable = Scripted(old, packet), []
    def crash_after_saved_response(state):
        durable.append(state)
        if len(state['responses']) == 1:
            raise RuntimeError('simulated restart after response saved')
    agent = MemoryModelAgent(writer, provider)
    with pytest.raises(RuntimeError):
        await agent.prepare(packet, persist=crash_after_saved_response)
    resumed = await agent.prepare(packet, cached=durable[-1], persist=durable.append)
    assert resumed['status'] == 'prepared'
    assert provider.calls == 3
    assert resumed['usage']['model_calls'] == 3
    assert len(resumed['responses']) == 3
    assert len(agent.commit(packet, resumed).saved) == 2


@pytest.mark.asyncio
async def test_keep_existing_does_not_bypass_negative_judgment_flags(stack):
    from agent_core.memory_model_agent import MemoryModelAgent
    _, writer, old, packet = stack
    class NegativeKeep(Scripted):
        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            if self.calls == 3:
                plan = json.loads(response.message['tool_calls'][0]['function']['arguments'])
                plan['items'][0]['same_subject'] = False
                response.message['tool_calls'][0]['function']['arguments'] = json.dumps(plan)
            return response
    agent = MemoryModelAgent(writer, NegativeKeep(old, packet, keep=True))
    state = await agent.prepare(packet, persist=lambda _: None)
    result = agent.commit(packet, state)
    assert result.rejected, result.as_dict()
    assert not any(c.get('existing_memory_id') for c in result.checks)


@pytest.mark.asyncio
async def test_missing_agent_field_gets_exactly_one_format_repair(stack):
    from agent_core.memory_model_agent import MemoryModelAgent
    _, writer, old, packet = stack
    class MissingField(Scripted):
        async def complete(self, messages, **kwargs):
            if self.calls < 3:
                response = await super().complete(messages, **kwargs)
                if self.calls == 3:
                    plan = json.loads(response.message['tool_calls'][0]['function']['arguments'])
                    plan['items'] = [plan['items'][1]]
                    del plan['items'][0]['certainty']
                    response.message['tool_calls'][0]['function']['arguments'] = json.dumps(plan)
                return response
            self.calls += 1
            tool_ids = [m['tool_call_id'] for m in messages if m['role'] == 'tool']
            assert len(tool_ids) == len(set(tool_ids)), 'each tool result published once'
            assert json.loads(messages[-1]['content'])['error'] == 'format_repair_once'
            piece = '部署方案暂时先不部署'
            plan = dict(items=[decision(1, piece, '部署方案', packet.user_text.index(piece),
                packet.user_text.index(piece)+len(piece), action='replace', target=old)])
            return SimpleNamespace(message=dict(content='', tool_calls=[tool('submit_memory_plan', plan, 'repair')]), usage={})
    provider = MissingField(old, packet)
    agent = MemoryModelAgent(writer, provider)
    state = await agent.prepare(packet, persist=lambda _: None)
    assert provider.calls == 4
    assert state['status'] == 'prepared'
    assert len(agent.commit(packet, state).saved) == 1


@pytest.mark.asyncio
async def test_local_worker_uses_agent_and_retries_only_commit_after_database_failure(stack, monkeypatch):
    from agent_core.memory_model_agent import MemoryModelAgent
    from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker
    repos, writer, old, packet = stack
    queue = MemoryCandidateQueue(repos.db_path)
    queue.initialize()
    ident, _ = queue.enqueue(packet)
    provider = Scripted(old, packet)
    agent = MemoryModelAgent(writer, provider)
    original, commits = agent.commit, []
    def interrupted_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        commits.append(result)
        if len(commits) == 1:
            raise sqlite3.OperationalError('simulated crash after memory transaction')
        return result
    monkeypatch.setattr(agent, 'commit', interrupted_commit)
    worker = MemoryCandidateWorker(queue, None, writer, model_agent=agent)
    first = await worker.process_once()
    assert first['failed'] == 1
    saved_state = json.loads(queue.get(ident)['agent_state'])
    assert saved_state['status'] == 'prepared'
    assert queue.get(ident)['extracted'] == 0, 'legacy extractor must not also process this turn'
    second = await worker.process_once(now=datetime.now(UTC)+timedelta(seconds=31))
    assert second['stored'] == 1
    assert provider.calls == 3
    assert {r['id'] for r in commits[0].saved} == {r['id'] for r in commits[1].saved}
    assert queue.get(ident)['execution_attempts'] == 2


@pytest.mark.asyncio
async def test_no_agent_configuration_keeps_attempts_for_configuration_recovery(stack):
    from agent_core.memory_model_agent import MemoryModelAgent
    from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker
    repos, writer, old, packet = stack
    queue = MemoryCandidateQueue(repos.db_path)
    queue.initialize()
    ident, _ = queue.enqueue(packet)
    agent = MemoryModelAgent(writer, None)
    worker = MemoryCandidateWorker(queue, None, writer, model_agent=agent)
    await worker.process_once()
    assert queue.get(ident)['execution_attempts'] == 0
    assert queue.get(ident)['status'] == 'pending_review'
    agent.provider = Scripted(old, packet)
    await worker.process_once()
    assert queue.get(ident)['status'] == 'completed'


def test_claim_renewal_and_stale_owner_guard_are_atomic(stack):
    from agent_core.memory_queue import MemoryCandidateQueue
    repos, _, _, packet = stack
    queue = MemoryCandidateQueue(repos.db_path)
    queue.initialize()
    ident, _ = queue.enqueue(packet)
    now = datetime.now(UTC)
    first = queue.claim(now=now, limit=1)[0]
    assert queue.claim(now=now+timedelta(seconds=100), limit=1) == []
    assert queue.renew_agent_lease(ident, first['lease_owner'], now=now+timedelta(seconds=100))
    assert queue.claim(now=now+timedelta(seconds=121), limit=1) == []
    second = queue.claim(now=now+timedelta(seconds=221), limit=1)[0]
    assert first['lease_owner'] != second['lease_owner']
    assert not queue.renew_agent_lease(ident, first['lease_owner'])
    with repos.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(ValueError, match='agent_lease_lost'):
            queue.agent_guard(ident, first['lease_owner'])(db)
    assert queue.claim(now=now+timedelta(seconds=601), limit=1) == []
    assert queue.get(ident)['status'] == 'unresolved'


@pytest.mark.asyncio
async def test_cloud_worker_uses_same_agent_and_reuses_prepared_plan(stack, monkeypatch):
    from agent_core.memory_model_agent import MemoryModelAgent
    from cloud_memory.store import CloudMemoryStore
    from cloud_memory.worker import CloudMemoryWorker, ChangeRecorder
    repos, writer, old, packet = stack
    store = CloudMemoryStore(repos.db_path)
    store.initialize()
    writer.on_change = ChangeRecorder(repos, store)
    store.enqueue_turn(dict(turn_id=packet.turn_id, owner_id=packet.scope_owner, channel=packet.scope_channel,
        conversation_id=packet.scope_conversation, module_id=packet.scope_module,
        user_text=packet.user_text, packet=packet.as_payload()))
    provider, agent = Scripted(old, packet), None
    agent = MemoryModelAgent(writer, provider)
    original, receipts = agent.commit, []
    def interrupted_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        receipts.append(result)
        if len(receipts) == 1:
            raise sqlite3.OperationalError('simulated crash after cloud memory transaction')
        return result
    monkeypatch.setattr(agent, 'commit', interrupted_commit)
    worker = CloudMemoryWorker(repos, store, writer, None, model_agent=agent)
    worker.use_semantic = True
    await worker.run_once()
    assert store.queue_counts() == {'failed':1}
    with store.connect() as db:
        row = dict(db.execute('SELECT * FROM memory_ingest_queue').fetchone())
        assert json.loads(row['agent_state'])['status'] == 'prepared'
        assert row['extraction_done'] == 0
        db.execute("UPDATE memory_ingest_queue SET next_retry_at=NULL")
    await worker.run_once()
    assert provider.calls == 3
    assert store.queue_counts() == {'stored':1}
    assert {r['id'] for r in receipts[0].saved} == {r['id'] for r in receipts[1].saved}
    changes = store.changes_since(0, owner_id='owner')
    assert any(c['record_id'] == old['id'] and c['payload']['effective_record_id'] for c in changes)


def test_cloud_restart_does_not_requeue_another_live_worker(stack):
    from cloud_memory.store import CloudMemoryStore
    repos, _, _, packet = stack
    store = CloudMemoryStore(repos.db_path)
    store.initialize()
    store.enqueue_turn(dict(turn_id=packet.turn_id, packet=packet.as_payload()))
    claim = store.claim_due(limit=1)[0]
    assert claim['lease_owner']
    assert store.requeue_stale() == 0
    assert store.claim_due(limit=1) == []
