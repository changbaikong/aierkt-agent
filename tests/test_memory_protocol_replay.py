"""Replay paid failures verbatim; fixtures prove transport, never live semantics."""
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_core.contracts import Scope
from agent_core.memory_processing import MemoryReviewProcessor
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryTurnPacket, decode_candidates
from agent_core.memory_writes import MemoryWriter

EVIDENCE = Path(__file__).parents[1] / 'docs/2026-10-13-memory-model-led-live.json'


def real_run(case, repeat):
    runs = json.loads(EVIDENCE.read_text(encoding='utf-8'))['runs']
    return next(row for row in runs if row['case'] == case and row['repeat'] == repeat)


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


class Replay:
    def __init__(self, raw):
        self.raw, self.calls = raw, 0

    async def complete(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(message={'content': self.raw})


@pytest.fixture
def stack(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    yield repos, MemoryWriter(repos, Embeddings())
    repos.close()


@pytest.mark.asyncio
async def test_real_q3_second_misplaced_adoption_saves_without_another_model(stack, monkeypatch):
    from agent_core.memory_retrieval import MemoryRetriever
    repos, writer = stack
    run = real_run('Q3', 2)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    items = decode_candidates(json.dumps(run['extracted_candidates'], ensure_ascii=False))
    raw = run['review_cache']['raw']
    provider, durable = Replay(raw), []
    prepared, _, result = await MemoryReviewProcessor(writer, provider).prepare(packet, items, persist=durable.append)
    assert len(prepared) == 1, result.as_dict()
    assert provider.calls == 1, 'unambiguous field migration must not call another model'
    saved = writer.save_semantic_candidates(Scope(packet.scope_owner, packet.scope_channel, packet.scope_conversation, packet.scope_module), prepared,
        packet=packet, turn_id=packet.turn_id, project_binding=packet.project_binding)
    assert len(saved) == 1
    assert '先说结论' in saved[0]['content']
    assert '说明' not in saved[0]['content'], 'assistant-added explanation preference is unsupported'
    assert saved[0]['expires_at'] is None
    assert durable[0]['raw'] == raw, 'original failure must remain byte-for-byte unchanged'
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED', 'true')
    recalled = MemoryRetriever(repos, Embeddings()).retrieve(Scope(packet.scope_owner, packet.scope_channel, packet.scope_conversation, packet.scope_module),
        '回答偏好', project_id=packet.project_binding, include_semantic=True)
    assert saved[0]['id'] in {r['id'] for r in recalled.warm}
    new_session = MemoryRetriever(repos, Embeddings()).retrieve(Scope(packet.scope_owner, packet.scope_channel, 'new', packet.scope_module),
        '回答偏好', project_id=packet.project_binding, include_semantic=True)
    assert saved[0]['id'] not in {r['id'] for r in new_session.warm}, 'archived model scope was conversation; do not fabricate owner scope'


@pytest.mark.asyncio
async def test_explicit_model_scope_saves_preference_for_new_session(stack):
    """Protocol fixture, separate from unchanged paid evidence."""
    from agent_core.memory_retrieval import MemoryRetriever
    repos, writer = stack
    run = real_run('Q3', 2)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    fixture = json.loads(run['review_cache']['raw'])
    fixture['items'][0]['scope_kind'] = 'owner'
    prepared, _, result = await MemoryReviewProcessor(writer, Replay(json.dumps(fixture))).prepare(packet,
        decode_candidates(json.dumps(run['extracted_candidates'])), persist=lambda _: None)
    rows = writer.save_semantic_candidates(Scope(packet.scope_owner, packet.scope_channel, packet.scope_conversation, packet.scope_module),
        prepared, packet=packet, turn_id=packet.turn_id, project_binding=packet.project_binding)
    recalled = MemoryRetriever(repos, Embeddings()).retrieve(Scope(packet.scope_owner, packet.scope_channel, 'new', packet.scope_module),
        '回答偏好', project_id=packet.project_binding, include_semantic=True)
    assert rows[0]['id'] in {r['id'] for r in recalled.warm}, result.as_dict()


@pytest.mark.asyncio
async def test_real_q3_first_false_meaning_flag_is_not_changed_by_adapter(stack):
    repos, writer = stack
    run = real_run('Q3', 1)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    provider = Replay(run['review_cache']['raw'])
    prepared, _, result = await MemoryReviewProcessor(writer, provider).prepare(packet,
        decode_candidates(json.dumps(run['extracted_candidates'], ensure_ascii=False)), persist=lambda _: None)
    assert not prepared
    assert provider.calls == 1, 'semantic disagreement is not a formatting repair'
    assert result.pending_review[0]['evidence_receipt']['review_error'] == 'semantic_flag_false:same_subject'
    assert json.loads(provider.raw)['items'][0]['same_subject'] is False
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_real_q1_valid_phase_part_survives_sibling_with_missing_days(stack):
    _, writer = stack
    run = real_run('Q1', 1)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    provider = Replay(run['review_cache']['raw'])
    prepared, _, result = await MemoryReviewProcessor(writer, provider).prepare(packet,
        decode_candidates(json.dumps(run['extracted_candidates'], ensure_ascii=False)), persist=lambda _: None)
    assert [p.content for p in prepared] == ['这个项目暂时先不部署'], result.as_dict()
    assert provider.calls == 2, 'one response-wide format repair, never unlimited repairs'
    rows = writer.save_semantic_candidates(Scope(packet.scope_owner, packet.scope_channel, packet.scope_conversation, packet.scope_module),
        prepared, packet=packet, turn_id=packet.turn_id, project_binding=packet.project_binding)
    assert rows[0]['expires_at'] is None
    assert result.pending_review
    assert 'days' in str(result.as_dict()), 'missing duration must be diagnosed, not silently defaulted'


def test_source_span_restores_original_without_clause_copying():
    from agent_core.memory_protocol import source_catalog, resolve_quotes
    packet = MemoryTurnPacket.from_payload(real_run('Q3', 2)['packet'])
    sources = source_catalog(packet)
    source = next(s for s in sources if s['message_id'] == 'q3-user')
    assert source['text'] == '嗯，就这样'
    resolved = resolve_quotes([dict(source_id=source['source_id'], start=0, end=len(source['text']))], sources)
    assert resolved[0]['quote'] == '嗯，就这样'
    assert resolved[0]['message_id'] == 'q3-user'
    with pytest.raises(ValueError, match='span'):
        resolve_quotes([dict(source_id=source['source_id'], start=0, end=99)], sources)


def test_schema_missing_duration_and_adoption_conflict_cannot_be_normalized():
    from agent_core.memory_protocol import decode_response
    packet = MemoryTurnPacket.from_payload(real_run('Q3', 2)['packet'])
    data = json.loads(real_run('Q3', 2)['review_cache']['raw'])
    data['items'][0]['adoption'] = False
    result = decode_response(json.dumps(data), packet=packet)
    assert result['format_errors'][0]['code'] == 'adoption_conflict'


@pytest.mark.asyncio
async def test_cache_changes_when_protocol_or_prompt_version_changes(stack, monkeypatch):
    from agent_core import memory_processing
    _, writer = stack
    run = real_run('Q3', 2)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    items = decode_candidates(json.dumps(run['extracted_candidates']))
    provider, saved = Replay(run['review_cache']['raw']), []
    processor = MemoryReviewProcessor(writer, provider)
    await processor.prepare(packet, items, persist=saved.append)
    await processor.prepare(packet, items, cached=saved[-1], persist=saved.append)
    assert provider.calls == 1
    monkeypatch.setattr(memory_processing, 'PROTOCOL_VERSION', 'next-protocol')
    await processor.prepare(packet, items, cached=saved[-1], persist=saved.append)
    assert provider.calls == 2
    monkeypatch.setattr(memory_processing, 'PROMPT_VERSION', 'next-prompt')
    await processor.prepare(packet, items, cached=saved[-1], persist=saved.append)
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_format_repair_cannot_rewrite_valid_sibling(stack):
    _, writer = stack
    run = real_run('Q1', 1)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    raw = run['review_cache']['raw']
    edited = json.loads(raw)
    edited['items'][0]['parts'][0]['content'] = '现在部署'
    class Repair(Replay):
        async def complete(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(message={'content': raw if self.calls == 1 else json.dumps(edited)})
    provider = Repair(raw)
    prepared, _, result = await MemoryReviewProcessor(writer, provider).prepare(packet,
        decode_candidates(json.dumps(run['extracted_candidates'])), persist=lambda _: None)
    assert [p.content for p in prepared] == ['这个项目暂时先不部署'], result.as_dict()
    assert provider.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['unknown_source_id', 'semantic_false'])
async def test_mixed_format_failure_cannot_repair_rejected_judgment(stack, fault):
    repos, writer = stack
    run = real_run('Q1', 1)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    first = json.loads(run['review_cache']['raw'])
    if fault == 'unknown_source_id':
        first['items'][0]['parts'][0]['quotes'] = [dict(source_id='invented', start=0, end=1)]
    else:
        part = first['items'][0]['parts'][0]
        part['same_subject'] = False
        part['lifetime'] = dict(mode='short_term', basis='test missing duration')
    repair = json.loads(run['review_cache']['raw'])
    # This response would restore the first part. The original refusal must win.
    class Responses(Replay):
        async def complete(self, *args, **kwargs):
            self.calls += 1
            return SimpleNamespace(message={'content': json.dumps(first if self.calls == 1 else repair)})
    provider = Responses('')
    prepared, _, result = await MemoryReviewProcessor(writer, provider).prepare(packet,
        decode_candidates(json.dumps(run['extracted_candidates'])), persist=lambda _: None)
    rows = writer.save_semantic_candidates(Scope(packet.scope_owner, packet.scope_channel,
        packet.scope_conversation, packet.scope_module), prepared, packet=packet,
        turn_id=packet.turn_id, project_binding=packet.project_binding)
    assert rows == [], result.as_dict()
    assert len([p for p in result.pending_review if p['index'] == 0]) == 2
    assert (fault if fault == 'unknown_source_id' else 'same_subject') in str(result.as_dict())
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_format_repair_prompt_change_invalidates_cache(stack, monkeypatch):
    from agent_core import memory_processing
    _, writer = stack
    run = real_run('Q3', 2)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    items = decode_candidates(json.dumps(run['extracted_candidates']))
    provider, saved = Replay(run['review_cache']['raw']), []
    processor = MemoryReviewProcessor(writer, provider)
    await processor.prepare(packet, items, persist=saved.append)
    monkeypatch.setattr(memory_processing, 'REVIEW_FORMAT_REPAIR_PROMPT', 'changed repair contract')
    await processor.prepare(packet, items, cached=saved[-1], persist=saved.append)
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_incompatible_kind_status_is_accounted_before_commit(stack):
    _, writer = stack
    run = real_run('Q1', 1)
    packet = MemoryTurnPacket.from_payload(run['packet'])
    data = json.loads(run['review_cache']['raw'])
    data['items'][0]['parts'][1]['lifetime']['days'] = 1
    prepared, _, result = await MemoryReviewProcessor(writer, Replay(json.dumps(data))).prepare(packet,
        decode_candidates(json.dumps(run['extracted_candidates'])), persist=lambda _: None)
    assert [p.content for p in prepared] == ['这个项目暂时先不部署'], result.as_dict()
    rows = writer.save_semantic_candidates(Scope(packet.scope_owner, packet.scope_channel,
        packet.scope_conversation, packet.scope_module), prepared, packet=packet,
        turn_id=packet.turn_id, project_binding=packet.project_binding)
    assert len(rows) == 1
    assert 'content_status' in str(result.as_dict())


@pytest.mark.parametrize('fault', ['same_subject', 'source_id'])
def test_duplicate_index_does_not_erase_original_rejection(fault):
    from agent_core.memory_protocol import decode_response, preserve_valid_items
    packet = MemoryTurnPacket.from_payload(real_run('Q3', 2)['packet'])
    good = json.loads(real_run('Q3', 2)['review_cache']['raw'])
    bad = json.loads(json.dumps(good))
    if fault == 'same_subject':
        bad['items'][0]['same_subject'] = False
    else:
        bad['items'][0]['quotes'] = [dict(source_id='invented', start=0, end=1)]
    bad['items'].append(good['items'][0])
    initial = decode_response(json.dumps(bad), packet=packet)
    repaired = decode_response(json.dumps(good), packet=packet)
    assert preserve_valid_items(initial, repaired) == []


@pytest.mark.parametrize('invalid_index', [False, 0.0])
def test_non_integer_index_cannot_remove_prior_rejection(invalid_index):
    from agent_core.memory_protocol import decode_response, preserve_valid_items
    packet = MemoryTurnPacket.from_payload(real_run('Q3', 2)['packet'])
    data = json.loads(real_run('Q3', 2)['review_cache']['raw'])
    repaired = decode_response(json.dumps(data), packet=packet)
    data['items'][0]['same_subject'] = False
    data['items'].append({**data['items'][0], 'index': invalid_index})
    initial = decode_response(json.dumps(data), packet=packet)
    assert preserve_valid_items(initial, repaired)[0]['same_subject'] is False


@pytest.mark.parametrize('fault', ['adoption_false', 'binding_source'])
def test_missing_duration_cannot_repair_original_adoption_or_binding(fault):
    from agent_core.memory_protocol import decode_response, preserve_valid_items
    packet = MemoryTurnPacket.from_payload(real_run('Q3', 2)['packet'])
    good = json.loads(real_run('Q3', 2)['review_cache']['raw'])
    normalized = decode_response(json.dumps(good), packet=packet)['items'][0]
    normalized['change_authorized'] = True
    initial_row = json.loads(json.dumps(normalized))
    initial_row['lifetime'] = dict(mode='short_term', basis='missing duration')
    if fault == 'adoption_false':
        initial_row['adoption'] = False
    else:
        initial_row['adoption_binding']['proposal']['source_id'] = 'invented'
    repaired_row = json.loads(json.dumps(normalized))
    repaired_row['lifetime'] = dict(mode='short_term', days=3, basis='repaired duration')
    initial = decode_response(json.dumps(dict(items=[initial_row])), packet=packet)
    repaired = decode_response(json.dumps(dict(items=[repaired_row])), packet=packet)
    preserved = preserve_valid_items(initial, repaired)[0]
    if fault == 'adoption_false':
        assert preserved['adoption'] is False
    else:
        assert preserved['protocol_error'] == 'unknown_source_id'
