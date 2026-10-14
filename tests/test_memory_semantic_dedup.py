"""A model's explicit same-fact judgment must prevent a paraphrase write."""
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_core.contracts import Scope
from agent_core.memory_processing import MemoryReviewProcessor
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryRef, turn_from_messages
from agent_core.memory_writes import MemoryWriter
from support.memory_review import FixedReviewProvider, supported_row


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


@pytest.mark.asyncio
@pytest.mark.parametrize('old_text,new_text,subject', [
    ('回答先给结论', '回答先说结论', '回答顺序'),
    ('预算三千元', '预算3000元', '预算金额'),
])
async def test_model_same_fact_judgment_does_not_create_a_paraphrase_record(tmp_path, old_text, new_text, subject):
    repos = initialize_memory_repositories(tmp_path / 'records.db', tmp_path / 'vectors', vector_size=3)
    writer = MemoryWriter(repos, Embeddings())
    scope = Scope('dedup-test', 'web', 'two-turns', 'companion')
    durable = []
    review_log = tmp_path / 'durable-review-results.json'
    def persist_review(payload):
        durable.append(payload)
        review_log.write_text(json.dumps(durable, ensure_ascii=False), encoding='utf-8')
    try:
        first_packet = turn_from_messages(turn_id='first', occurred_at=datetime.now(UTC).isoformat(),
            scope=scope, user_text=old_text+'。', published_reply='收到')
        first = MemoryCandidate(old_text, 'preference', 'explicit', ('user_statement',), source_refs=('first',))
        first_row = supported_row(old_text, [('first', old_text)], subject=subject, change=False)
        prepared, _, result = await MemoryReviewProcessor(writer, FixedReviewProvider([first_row])).prepare(
            first_packet, [first], persist=persist_review)
        assert prepared and not result.pending_review
        old = writer.save_semantic_candidates(scope, prepared, packet=first_packet, turn_id='first')[0]

        second_packet = turn_from_messages(turn_id='second', occurred_at=datetime.now(UTC).isoformat(),
            scope=scope, user_text=new_text+'。', published_reply='收到')
        second_packet = replace(second_packet, recalled_memory_refs=[
            MemoryRef(old['id'], 'preference', 'explicit', old_text, version=1)])
        second = MemoryCandidate(new_text, 'preference', 'explicit', ('user_statement',), source_refs=('second',))
        second_row = supported_row(new_text, [('second', new_text)], subject=subject, change=False)
        # Explicit independent expectation, not a product binding or text-key result.
        second_row.update(reason='两种写法表达同一事实，没有新增条件或改变取值',
            same_fact=True, existing_memory_id=old['id'], write_action='keep_existing', creates_new_fact=False)
        reviewer = FixedReviewProvider([second_row])
        prepared, _, result = await MemoryReviewProcessor(writer, reviewer).prepare(
            second_packet, [second], persist=persist_review)
        assert reviewer.calls == 1
        persisted = json.loads(review_log.read_text(encoding='utf-8'))
        assert json.loads(persisted[-1]['raw'])['items'][0]['write_action'] == 'keep_existing', \
            'The model judgment must remain durable for subsequent agents to inspect.'
        writer.save_semantic_candidates(scope, prepared, packet=second_packet, turn_id='second')

        with repos.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM memory_records WHERE semantics_version=1')]
        assert len(rows) == 1, 'Model explicitly reported the same fact, but a paraphrase created another record: ' + repr(
            [(row['content'], row['record_version'], row['status']) for row in rows])
        assert rows[0]['id'] == old['id'] and rows[0]['record_version'] == 1
        assert rows[0]['content'] == old_text and rows[0]['status'] == 'active'
        assert not result.pending_review and not result.rejected
    finally:
        repos.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_reference', ['forged_id', 'other_owner', 'changed_version', 'archived'])
async def test_invalid_same_fact_reference_stays_pending_without_writes(tmp_path, invalid_reference):
    repos = initialize_memory_repositories(tmp_path / 'records.db', tmp_path / 'vectors', vector_size=3)
    writer = MemoryWriter(repos, Embeddings())
    scope = Scope('dedup-test', 'web', 'two-turns', 'companion')
    seed_scope = replace(scope, owner_id='another-owner') if invalid_reference == 'other_owner' else scope
    review_log = tmp_path / 'durable-review-results.json'
    durable = []

    def persist_review(payload):
        durable.append(payload)
        review_log.write_text(json.dumps(durable, ensure_ascii=False), encoding='utf-8')

    try:
        first_packet = turn_from_messages(turn_id='first', occurred_at=datetime.now(UTC).isoformat(),
            scope=seed_scope, user_text='回答先给结论。', published_reply='收到')
        first = MemoryCandidate('回答先给结论', 'preference', 'explicit', ('user_statement',), source_refs=('first',))
        first_row = supported_row('回答先给结论', [('first', '回答先给结论')], subject='回答顺序', change=False)
        prepared, _, result = await MemoryReviewProcessor(writer, FixedReviewProvider([first_row])).prepare(
            first_packet, [first], persist=persist_review)
        assert prepared and not result.pending_review
        old = writer.save_semantic_candidates(seed_scope, prepared, packet=first_packet, turn_id='first')[0]
        existing_id = 'forged-memory-id' if invalid_reference == 'forged_id' else old['id']
        second_packet = turn_from_messages(turn_id='second', occurred_at=datetime.now(UTC).isoformat(),
            scope=scope, user_text='回答先说结论。', published_reply='收到')
        second_packet = replace(second_packet, recalled_memory_refs=[
            MemoryRef(existing_id, 'preference', 'explicit', '回答先给结论', version=1)])
        # Simulate a legitimate concurrent change after recall, before review.
        with repos.connect() as db:
            if invalid_reference == 'changed_version':
                db.execute('UPDATE memory_records SET record_version=2 WHERE id=?', (old['id'],))
            elif invalid_reference == 'archived':
                db.execute("UPDATE memory_records SET status='archived' WHERE id=?", (old['id'],))
            before = [dict(row) for row in db.execute('SELECT * FROM memory_records ORDER BY id')]
        second = MemoryCandidate('回答先说结论', 'preference', 'explicit', ('user_statement',), source_refs=('second',))
        row = supported_row('回答先说结论', [('second', '回答先说结论')], subject='回答顺序', change=False)
        row.update(reason='两种写法表达相同事实，无需新增', same_fact=True,
            existing_memory_id=existing_id, write_action='keep_existing', creates_new_fact=False)
        reviewer = FixedReviewProvider([row])
        processor = MemoryReviewProcessor(writer, reviewer)
        prepared, suggestions, result = await processor.prepare(second_packet, [second], persist=persist_review)
        assert not prepared and not suggestions
        assert len(result.pending_review) == 1 and not result.rejected
        assert result.pending_review[0]['evidence_receipt']['review_error'] == 'invalid_existing_fact_reference'
        writer.save_semantic_candidates(scope, prepared, packet=second_packet, turn_id='second')
        persisted = json.loads(review_log.read_text(encoding='utf-8'))[-1]
        assert json.loads(persisted['raw'])['items'][0] == row
        # A restart must recover the durable decision and still require review.
        recovered, recovered_suggestions, recovered_result = await MemoryReviewProcessor(writer).prepare(
            second_packet, [second], cached=persisted)
        assert not recovered and not recovered_suggestions
        assert len(recovered_result.pending_review) == 1
        assert reviewer.calls == 1
        with repos.connect() as db:
            after = [dict(record) for record in db.execute('SELECT * FROM memory_records ORDER BY id')]
        assert after == before, 'Invalid reuse must neither create a candidate nor alter the target record.'
    finally:
        repos.close()
