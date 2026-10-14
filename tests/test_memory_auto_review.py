"""Recovery checks use isolated persistent queues and inspect the actual memory database."""
import json
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent_core.contracts import Scope
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, turn_from_messages
from agent_core.memory_writes import MemoryWriter


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


class Reviewer:
    def __init__(self, quote='暂不部署', content='暂不部署'):
        self.calls = 0
        self.quote, self.content = quote, content
    async def complete(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(message={'content': json.dumps({'items':[
            review_judgment(0, self.content, self.quote)]}, ensure_ascii=False)})


class EmptyThenValidReviewer(Reviewer):
    """首次格式响应为空，单次修正调用后给出合法复核。"""

    async def complete(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(message={'content': ''})
        return SimpleNamespace(message={'content': json.dumps({'items': [
            review_judgment(0, self.content, self.quote)]}, ensure_ascii=False)})


def review_judgment(index, content, quote, *, message_id='t', subject='部署安排',
                    verdict='supported', change_authorized=True):
    """Independent model fixture: expected meanings never come from product checks."""
    return dict(index=index, verdict=verdict, content=content, subject=subject,
                reason='用户原话明确暂缓该事项，没有指定地点或新增条件',
                same_subject=True, preserves_negation=True, preserves_conditions=True,
                change_authorized=change_authorized,
                lifetime=dict(mode='phase', basis='用户暂缓该事项，直到后续明确改变', days=None),
                quotes=[dict(message_id=message_id, quote=quote)])


async def seed_old_decision(writer, scope):
    from dataclasses import replace
    from agent_core.memory_processing import MemoryReviewProcessor
    packet = turn_from_messages(turn_id='seed', occurred_at=datetime.now(UTC).isoformat(),
                                scope=scope, user_text='部署立即执行。', published_reply='')
    packet = replace(packet, project_binding='deployment-project')
    class SeedReviewer:
        async def complete(self, messages, **kwargs):
            row = review_judgment(0, '部署立即执行', '部署立即执行', message_id='seed')
            row['reason'] = '用户明确决定立即部署'
            row['lifetime'] = dict(mode='long_term', basis='用户未限定部署决定期限', days=None)
            return SimpleNamespace(message={'content':json.dumps({'items':[row]})})
    item = MemoryCandidate('部署立即执行', 'decision', 'accepted', ('user_statement',),
                           source_refs=('seed',), scope_kind='project', project_id='deployment-project')
    candidates, _, result = await MemoryReviewProcessor(writer, SeedReviewer()).prepare(packet, [item], persist=lambda _: None)
    assert not result.pending_review and not result.rejected
    return writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='seed', project_binding='deployment-project')[0]


class Extractor:
    def __init__(self, provider):
        self.provider = provider
        self.calls = 0
    async def extract(self, packet, **kwargs):
        self.calls += 1
        return [MemoryCandidate('暂不在本机部署', 'decision', 'accepted', ('user_statement',), source_refs=('t',))]


@pytest.fixture
def stack(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    queue = MemoryCandidateQueue(tmp_path/'jobs.db')
    queue.initialize()
    writer = MemoryWriter(repos, Embeddings())
    scope = Scope('review', 'web', 'recovery', 'companion')
    packet = turn_from_messages(turn_id='t', occurred_at=datetime.now(UTC).isoformat(), scope=scope,
                                user_text='暂不部署。', published_reply='先放本机')
    job_id, _ = queue.enqueue(packet)
    yield repos, queue, writer, job_id
    repos.close()


@pytest.mark.asyncio
async def test_background_review_rebuilds_only_the_supported_decision(stack):
    repos, queue, writer, job_id = stack
    reviewer = Reviewer()
    result = await MemoryCandidateWorker(queue, Extractor(reviewer), writer).process_once()
    assert reviewer.calls == 1
    with repos.connect() as db:
        rows = db.execute('SELECT * FROM memory_records WHERE semantics_version=1').fetchall()
    assert len(rows) == 1
    assert rows[0]['content'] == '暂不部署'
    assert rows[0]['content_status'] == 'accepted'
    job = queue.get(job_id)
    assert json.loads(job['review_results'])['completed'] is True
    assert result['stored'] == 1


@pytest.mark.asyncio
async def test_empty_review_response_gets_one_format_repair_and_is_durable(stack):
    _, queue, writer, job_id = stack
    reviewer = EmptyThenValidReviewer()
    await MemoryCandidateWorker(queue, Extractor(reviewer), writer).process_once()
    assert reviewer.calls == 2
    assert queue.get(job_id)['status'] == 'completed'
    review = json.loads(queue.get(job_id)['review_results'])
    assert review['repair_attempted'] is True
    assert review['raw']


@pytest.mark.asyncio
async def test_saved_review_survives_database_failure_and_worker_restart(stack):
    repos, queue, writer, job_id = stack
    reviewer = Reviewer()
    extractor = Extractor(reviewer)
    original = writer.process_semantic_candidates
    def crash(*args, **kwargs):
        assert queue.get(job_id).get('review_results'), 'review must be durable before memory commit'
        raise RuntimeError('transient database fault')
    writer.process_semantic_candidates = crash
    await MemoryCandidateWorker(queue, extractor, writer).process_once()
    assert queue.get(job_id)['status'] == 'failed'
    writer.process_semantic_candidates = original
    await MemoryCandidateWorker(queue, extractor, writer).process_once(now=datetime.now(UTC)+timedelta(minutes=3))
    assert reviewer.calls == 1
    assert extractor.calls == 1
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 1


@pytest.mark.asyncio
async def test_forged_review_quote_stays_unresolved(stack):
    repos, queue, writer, job_id = stack
    reviewer = Reviewer(quote='已经批准本机部署', content='已经批准本机部署')
    await MemoryCandidateWorker(queue, Extractor(reviewer), writer).process_once()
    assert reviewer.calls == 1
    assert queue.get(job_id)['status'] == 'pending_review'
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unconfigured_review_is_not_reported_as_empty_or_complete(stack):
    _, queue, writer, job_id = stack
    await MemoryCandidateWorker(queue, Extractor(None), writer).process_once()
    assert queue.get(job_id)['status'] == 'pending_review'
    assert 'not_configured' in queue.get(job_id)['last_error']


@pytest.mark.asyncio
async def test_review_timeout_retries_three_times_and_keeps_failure(stack):
    _, queue, writer, job_id = stack
    class HangingReviewer:
        calls = 0
        async def complete(self, *args, **kwargs):
            self.calls += 1
            await asyncio.Event().wait()
    reviewer = HangingReviewer()
    worker = MemoryCandidateWorker(queue, Extractor(reviewer), writer, review_timeout_seconds=.01)
    for attempt in range(4):
        await worker.process_once(now=datetime.now(UTC)+timedelta(minutes=10 * attempt))
    job = queue.get(job_id)
    assert job['status'] == 'failed'
    assert job['attempts'] == 3
    assert reviewer.calls == 3
    assert 'TimeoutError' in job['last_error']


@pytest.mark.asyncio
async def test_completed_review_is_not_repeated_after_memory_saved_then_crash(stack):
    repos, queue, writer, job_id = stack
    reviewer = Reviewer()
    extractor = Extractor(reviewer)
    original = queue.finish_processing
    def crash(*args, **kwargs):
        raise RuntimeError('crash after memory commit')
    queue.finish_processing = crash
    with pytest.raises(RuntimeError):
        await MemoryCandidateWorker(queue, extractor, writer).process_once()
    queue.finish_processing = original
    await MemoryCandidateWorker(queue, extractor, writer).process_once(now=datetime.now(UTC)+timedelta(minutes=3))
    assert reviewer.calls == 1
    assert extractor.calls == 1
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('user,configured,should_suspend', [
    ('暂不部署。', True, True), ('暂不部署。', False, False), ('排期暂不调整。', True, False)])
async def test_only_a_model_authorized_change_hides_the_old_decision(stack, monkeypatch, user, configured, should_suspend):
    from dataclasses import replace
    from agent_core.memory_turn import MemoryRef, MemoryUpdateSuggestion
    from agent_core.memory_retrieval import MemoryRetriever
    repos, queue, writer, job_id = stack
    scope = Scope('review', 'web', 'recovery', 'companion')
    old = await seed_old_decision(writer, scope)
    assert old['scope_kind'] == 'project', old
    packet = turn_from_messages(turn_id='t', occurred_at=datetime.now(UTC).isoformat(), scope=scope,
                                user_text=user, published_reply='先放本机')
    packet = replace(packet, project_binding='deployment-project', recalled_memory_refs=[MemoryRef(old['id'], 'decision', 'accepted', old['content'], version=1)])
    with queue.connect() as db:
        db.execute('UPDATE memory_candidate_jobs SET packet=? WHERE job_id=?', (json.dumps(packet.as_payload()), job_id))
    suggestion = MemoryUpdateSuggestion('replace', content='暂不在本机部署', kind='decision', content_status='accepted',
                                        target_id=old['id'], expected_version=1, source_refs=('t',), source_types=('user_statement',))
    class ChangeReviewer:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            row = review_judgment(0, '暂不部署', user.rstrip('。'),
                                  verdict='supported' if should_suspend else 'unsupported',
                                  change_authorized=should_suspend)
            if not should_suspend:
                row.update(subject='排期安排', reason='用户只谈排期，没有改变部署决定', same_subject=False)
            return SimpleNamespace(message={'content':json.dumps({'items':[row]})})
    reviewer = ChangeReviewer()
    class UpdateExtractor:
        provider = reviewer if configured else None
        async def extract_maintenance(self, *args, **kwargs):
            return [], [suggestion]
    await MemoryCandidateWorker(queue, UpdateExtractor(), writer, maintenance_queue=queue).process_once()
    assert reviewer.calls == int(configured)
    fresh = repos.records_by_ids([old['id']])[old['id']]
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
    recalled = MemoryRetriever(repos, Embeddings()).retrieve(Scope('review','web','new','companion'), '部署立即执行', project_id='deployment-project', include_semantic=True)
    assert (old['id'] in {r['id'] for r in recalled.warm}) is (not should_suspend)
    assert fresh['status'] == 'active', 'pending update must preserve the original record'
    assert bool(json.loads(fresh['metadata']).get('pending_change')) is should_suspend


@pytest.mark.asyncio
async def test_cloud_uses_the_same_review_and_persistent_result(stack, tmp_path, monkeypatch):
    from cloud_memory.store import CloudMemoryStore
    from cloud_memory.worker import CloudMemoryWorker
    repos, queue, writer, job_id = stack
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
    monkeypatch.setenv('MEMORY_PROCESSING_ENDPOINT', 'cloud')
    store = CloudMemoryStore(tmp_path/'cloud.db')
    store.initialize()
    packet = json.loads(queue.get(job_id)['packet'])
    store.enqueue_turn(dict(turn_id='t', owner_id='review', channel='web', conversation_id='recovery', module_id='companion',
                            user_text='暂不部署。', assistant_reply='先放本机', occurred_at=packet['occurred_at'], packet=packet))
    reviewer = Reviewer()
    worker = CloudMemoryWorker(repos, store, writer, None, semantic_extractor=Extractor(reviewer))
    await worker.run_once()
    assert reviewer.calls == 1
    with store.connect() as db:
        job = dict(db.execute('SELECT * FROM memory_ingest_queue').fetchone())
    assert json.loads(job['review_results'])['completed']
    assert json.loads(job['processing_result'])['saved'][0]['content'] == '暂不部署'
    with repos.connect() as db:
        assert db.execute('SELECT content FROM memory_records WHERE semantics_version=1').fetchone()[0] == '暂不部署'


@pytest.mark.asyncio
async def test_maintenance_worker_rebuilds_and_commits_a_faithful_update(stack):
    from dataclasses import replace
    from agent_core.memory_queue import MemoryMaintenanceWorker
    from agent_core.memory_maintenance import MemoryMaintenanceService
    from agent_core.memory_turn import MemoryRef, MemoryUpdateSuggestion, MemoryTurnPacket
    repos, queue, writer, job_id = stack
    scope = Scope('review', 'web', 'recovery', 'companion')
    old = await seed_old_decision(writer, scope)
    packet = replace(MemoryTurnPacket.from_payload(json.loads(queue.get(job_id)['packet'])),
                     occurred_at=datetime.now(UTC).isoformat(),
                     project_binding='deployment-project',
                     recalled_memory_refs=[MemoryRef(old['id'], 'decision', 'accepted', old['content'], version=1)])
    suggestion = MemoryUpdateSuggestion('replace', content='暂不在本机部署', kind='decision', content_status='accepted',
                                        target_id=old['id'], expected_version=1, source_refs=('t',), source_types=('user_statement',))
    maintenance_id, _ = queue.enqueue_maintenance(packet, [suggestion])
    reviewer = Reviewer()
    await MemoryMaintenanceWorker(queue, MemoryMaintenanceService(writer, repos), review_provider=reviewer).process_once()
    assert reviewer.calls == 1
    assert queue.get(maintenance_id)['status'] == 'completed'
    old_fresh = repos.records_by_ids([old['id']])[old['id']]
    assert old_fresh['status'] == 'archived'
    new = repos.records_by_ids([old_fresh['effective_record_id']])[old_fresh['effective_record_id']]
    assert new['content'] == '暂不部署'
    assert new['record_version'] == 2
    # Reclaim a job whose memory commit succeeded but whose final acknowledgement
    # was lost: a completed review must not be called again.
    with queue.connect() as db:
        db.execute("UPDATE memory_candidate_jobs SET status='failed',attempts=1,next_attempt_at=NULL WHERE job_id=?", (maintenance_id,))
    await MemoryMaintenanceWorker(queue, MemoryMaintenanceService(writer, repos), review_provider=reviewer).process_once()
    assert reviewer.calls == 1
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 2


@pytest.mark.asyncio
async def test_review_cache_is_invalidated_by_changed_content_or_source(stack):
    from dataclasses import replace
    from agent_core.memory_processing import MemoryReviewProcessor
    from agent_core.memory_turn import MemoryTurnPacket
    _, queue, writer, job_id = stack
    packet = MemoryTurnPacket.from_payload(json.loads(queue.get(job_id)['packet']))
    reviewer = Reviewer()
    saved = []
    item = MemoryCandidate('暂不在本机部署', 'decision', 'accepted', ('user_statement',), source_refs=('t',))
    processor = MemoryReviewProcessor(writer, reviewer)
    await processor.prepare(packet, [item], persist=saved.append)
    await processor.prepare(packet, [item], cached=saved[-1], persist=saved.append)
    assert reviewer.calls == 1
    await processor.prepare(packet, [replace(item, content='暂不在云端部署')], cached=saved[-1], persist=saved.append)
    assert reviewer.calls == 2
    await processor.prepare(replace(packet, user_text='部署暂缓。'), [item], cached=saved[-1], persist=saved.append)
    assert reviewer.calls == 3


@pytest.mark.asyncio
async def test_a_review_cannot_override_a_conflicting_number(stack):
    from dataclasses import replace
    from agent_core.memory_processing import MemoryReviewProcessor
    from agent_core.memory_turn import MemoryTurnPacket
    _, queue, writer, job_id = stack
    packet = replace(MemoryTurnPacket.from_payload(json.loads(queue.get(job_id)['packet'])), user_text='预算3000元。')
    reviewer = Reviewer(quote='预算3000元', content='预算9000元')
    item = MemoryCandidate('预算9000元', 'fact', 'reported', ('user_statement',), source_refs=('t',))
    candidates, _, result = await MemoryReviewProcessor(writer, reviewer).prepare(packet, [item])
    assert reviewer.calls == 0
    assert not candidates and len(result.rejected) == 1


def test_candidate_serialization_keeps_the_declared_horizon_and_project_label():
    from agent_core.memory_turn import decode_candidates, serialize_candidates
    item = MemoryCandidate('今天很累', 'fact', 'reported', ('user_statement',), horizon='short_term', project_label='label')
    restored = decode_candidates(serialize_candidates([item]))[0]
    assert restored.horizon == 'short_term'
    assert restored.project_label == 'label'


@pytest.mark.asyncio
async def test_pending_configuration_resumes_through_the_background_worker(stack):
    repos, queue, writer, job_id = stack
    extractor = Extractor(None)
    await MemoryCandidateWorker(queue, extractor, writer).process_once()
    assert queue.get(job_id)['status'] == 'pending_review'
    reviewer = Reviewer()
    extractor.provider = reviewer
    await MemoryCandidateWorker(queue, extractor, writer).process_once()
    assert reviewer.calls == 1
    assert extractor.calls == 1
    assert queue.get(job_id)['status'] == 'completed'


@pytest.mark.asyncio
async def test_multiple_uncertain_items_share_exactly_one_review_call(stack):
    repos, queue, writer, job_id = stack
    with queue.connect() as db:
        payload = json.loads(queue.get(job_id)['packet'])
        payload['user_text'] = '暂不部署；排期暂不调整。'
        db.execute('UPDATE memory_candidate_jobs SET packet=? WHERE job_id=?', (json.dumps(payload), job_id))
    class BatchReviewer:
        calls = 0
        async def complete(self, messages, **kwargs):
            self.calls += 1
            data = json.loads(messages[-1]['content'])
            rows = []
            assert [item['index'] for item in data['items']] == [0, 1]
            rows = [review_judgment(0, '暂不部署', '暂不部署'),
                    review_judgment(1, '排期暂不调整', '排期暂不调整', subject='排期安排')]
            return SimpleNamespace(message={'content':json.dumps({'items':rows})})
    class BatchExtractor(Extractor):
        async def extract(self, packet, **kwargs):
            return [MemoryCandidate(content, 'decision', 'accepted', ('user_statement',), source_refs=('t',))
                    for content in ('暂不在本机部署', '本机排期暂不调整')]
    reviewer = BatchReviewer()
    await MemoryCandidateWorker(queue, BatchExtractor(reviewer), writer).process_once()
    assert reviewer.calls == 1
    with repos.connect() as db:
        assert {r[0] for r in db.execute('SELECT content FROM memory_records WHERE semantics_version=1')} == {'暂不部署', '排期暂不调整'}
