"""Independent expectations: user quotes and actual isolated database rows."""
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import HistoryItem, MemoryCandidate, turn_from_messages
from agent_core.memory_writes import MemoryWriter


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


@pytest.fixture
def env(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    yield repos, MemoryWriter(repos, Embeddings()), Scope('repair', 'web', 'gate', 'companion')
    repos.close()


def parsed(scope, user, content, kind='fact', status='reported', history=()):
    packet = turn_from_messages(turn_id='t', occurred_at=datetime.now(UTC).isoformat(), scope=scope,
                                user_text=user, published_reply='收到')
    packet = replace(packet, relevant_history=list(history))
    item = dict(content=content, kind=kind, content_status=status,
                source_types=['user_statement'], source_refs=['t', *[h.message_id for h in history]])
    candidates, _, _ = SemanticMemoryExtractor(None).parse_maintenance(
        json.dumps({'memories':[item]}, ensure_ascii=False), packet=packet)
    return packet, candidates


@pytest.mark.parametrize('user,content,expected', [
    ('项目预算为3000元。', '项目预算为9000元', False),
    ('项目预算为三千元。', '项目预算为3,000元', True),
    ('项目预算为3000元；参加人数9000人。', '项目预算为9000元', False),
    ('项目预算3000元；广告预算9000元。', '项目预算9000元', False),
    ('项目预算3000元。', '项目预算3000元', True),
])
def test_new_values_are_checked_against_the_same_subject(env, user, content, expected):
    repos, writer, scope = env
    packet, candidates = parsed(scope, user, content)
    writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')
    with repos.connect() as db:
        rows = db.execute("SELECT content FROM memory_records WHERE semantics_version=1").fetchall()
    assert bool(rows) is expected


def test_failed_approval_receipt_blocks_effective_write(env):
    repos, writer, scope = env
    packet, candidates = parsed(scope, '部署方案改用方案B；尚未实施，也没有评审结论。',
                                '部署方案改用方案B，安全评审已通过', 'decision', 'accepted')
    writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


def test_unique_ack_inherits_the_actual_proposal_scope(env):
    _, writer, scope = env
    history = [HistoryItem('proposal','assistant','以后回答先说结论可以吗？')]
    packet, candidates = parsed(scope, '嗯，就这样', '以后回答先说结论', 'preference', 'explicit', history)
    rows = writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')
    assert len(rows) == 1
    assert json.loads(rows[0]['metadata'])['evidence_receipt']['passed'] is True


def test_model_candidate_without_packet_is_not_a_seed_bypass(env):
    repos, writer, scope = env
    candidate = MemoryCandidate('预算9000元', 'fact', 'reported', ('user_statement',), source_refs=('made-up',))
    rows = writer.save_semantic_candidates(scope, [candidate], turn_id='t')
    assert not rows
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


@pytest.mark.parametrize('user,history,content', [
    ('嗯', [HistoryItem('p','assistant','建议改用方案B；建议下周部署。')], '用户选择方案B'),
    ('', [HistoryItem('p','assistant','建议以后回答先说结论。')], '以后回答先说结论'),
    ('预算3000元。', [], '用户喜欢蓝色'),
    ('暂不部署。', [], '暂不在本机部署'),
])
def test_silence_or_unbound_details_never_become_user_facts(env, user, history, content):
    _, writer, scope = env
    packet, candidates = parsed(scope, user, content, history=history)
    rows = writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')
    assert not rows


def test_a_cached_receipt_cannot_authorize_changed_sources(env):
    from agent_core.memory_evidence import receipt
    _, writer, scope = env
    packet, candidates = parsed(scope, '项目预算3000元。', '项目预算9000元')
    old_receipt = receipt('项目预算9000元', '', evidence_clauses=['项目预算9000元'])
    assert not writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t', receipts=[old_receipt])


def test_receipt_fingerprint_covers_content_beyond_the_summary():
    from agent_core.memory_evidence import receipt, receipt_is_current
    original = '日志原则' * 30 + '预算3000元'
    old = receipt(original, '', evidence_clauses=[original])
    assert not receipt_is_current(old, content='日志原则' * 30 + '预算9000元')


def test_direct_update_cannot_bypass_the_shared_evidence_gate(env):
    from agent_core.memory_turn import MemoryUpdateSuggestion
    from support.memory_seed import seed_semantic
    repos, writer, scope = env
    old = seed_semantic(writer, scope, [MemoryCandidate('项目预算3000元', 'fact', 'reported', ('user_statement',))], turn_id='seed')[0]
    suggestion = MemoryUpdateSuggestion('correct', content='项目预算9000元', kind='fact', content_status='reported',
                                        target_id=old['id'], expected_version=1, source_types=('user_statement',), source_refs=('t',))
    with pytest.raises(ValueError):
        writer.apply_semantic_update(scope, suggestion, target=old, turn_id='t')
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 1


def test_maintenance_uses_shared_source_binding_and_receipt(env):
    from agent_core.memory_maintenance import MemoryMaintenanceService
    from agent_core.memory_turn import MemoryRef, MemoryUpdateSuggestion
    from support.memory_seed import seed_semantic
    _, writer, scope = env
    old = seed_semantic(writer, scope, [MemoryCandidate('部署方案为方案A', 'decision', 'accepted', ('user_statement',))], turn_id='seed')[0]
    packet, _ = parsed(scope, '部署方案改用方案B，尚未实施。', '部署方案改用方案B', 'decision', 'accepted')
    packet = replace(packet, recalled_memory_refs=[MemoryRef(old['id'], 'decision', 'accepted', old['content'], version=1)])
    suggestion = MemoryUpdateSuggestion('replace', content='部署方案改用方案B，尚未实施', kind='decision', content_status='accepted',
                                        target_id=old['id'], expected_version=1, source_types=('user_statement',), source_refs=('t',))
    import asyncio
    from support.memory_review import prepare_update, supported_row
    suggestion = asyncio.run(prepare_update(writer, packet, suggestion, [supported_row(
        '部署方案改用方案B，尚未实施', [('t', '部署方案改用方案B'), ('t', '尚未实施')],
        subject='部署方案', status='accepted')]))
    outcome = MemoryMaintenanceService(writer, writer.repositories).apply(suggestion, scope=scope, packet=packet, turn_id='t')
    assert outcome['applied'], outcome['reason']
    assert outcome['evidence_receipt'].get('bindings')
    assert outcome['record']['content_status'] == 'accepted'


@pytest.mark.parametrize('user,content', [
    ('暂不部署。', '部署'),
    ('如果评审通过才部署。', '部署'),
    ('方案改用B，尚未实施。', '方案改用B，已经实施'),
])
def test_negation_and_conditions_cannot_be_dropped(env, user, content):
    _, writer, scope = env
    packet, candidates = parsed(scope, user, content)
    assert not writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')


@pytest.mark.parametrize('user,content', [
    ('部署方案改用方案B，尚未实施。', '部署方案改用方案B，尚未实施'),
    ('排期暂不调整。', '排期暂不调整'),
])
def test_selection_and_no_change_are_explicit_decisions(env, user, content):
    _, writer, scope = env
    packet, candidates = parsed(scope, user, content, 'decision', 'accepted')
    rows = writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')
    assert len(rows) == 1
    assert rows[0]['content_status'] == 'accepted'


@pytest.mark.parametrize('user,content', [
    ('用户不喜欢蓝色。', '用户喜欢蓝色'),
    ('用户喜欢蓝色。', '用户不喜欢蓝色'),
    ('张三管理李四。', '李四管理张三'),
    ('项目预算3000元，广告预算9000元。', '项目预算3000元，广告预算3000元'),
    ('项目预算3000元，广告预算9000元。', '项目预算9000元，广告预算3000元'),
])
def test_every_claim_preserves_polarity_direction_and_its_own_value(env, user, content):
    repos, writer, scope = env
    packet, candidates = parsed(scope, user, content)
    assert not writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


@pytest.mark.parametrize('user,content', [
    ('用户不喜欢蓝色。', '用户不喜欢蓝色'),
    ('张三管理李四。', '张三管理李四'),
    ('项目预算三千元，广告预算9000元。', '项目预算3,000元，广告预算九千元'),
    ('项目预算3000元，广告预算3000元。', '项目预算三千元，广告预算3000元'),
])
def test_faithful_claim_controls_remain_writable(env, user, content):
    _, writer, scope = env
    packet, candidates = parsed(scope, user, content)
    assert writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')


def test_thousands_separators_do_not_split_an_evidence_claim():
    from agent_core.memory_turn import clauses
    assert clauses('项目预算3,000元，广告预算9,000元') == ('项目预算3,000元', '广告预算9,000元')


def test_relation_particles_are_not_disposable_filler(env):
    _, writer, scope = env
    packet, candidates = parsed(scope, '张三是李四的老师。', '张三的李四是老师')
    assert not writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')


def test_short_correction_cannot_inherit_the_old_object_as_a_new_subject(env):
    from agent_core.memory_turn import MemoryRef, MemoryUpdateSuggestion
    from support.memory_seed import seed_semantic
    repos, writer, scope = env
    old = seed_semantic(writer, scope, [MemoryCandidate('张三的老师是李四', 'fact', 'reported', ('user_statement',))], turn_id='seed')[0]
    packet, _ = parsed(scope, '老师是王五。', '李四的老师是王五')
    packet = replace(packet, recalled_memory_refs=[MemoryRef(old['id'], 'fact', 'reported', old['content'], version=1)])
    suggestion = MemoryUpdateSuggestion('correct', content='李四的老师是王五', kind='fact', content_status='reported',
        target_id=old['id'], expected_version=1, source_types=('user_statement',), source_refs=('t',))
    with pytest.raises(ValueError):
        writer.apply_semantic_update(scope, suggestion, target=old, packet=packet, turn_id='t')
    with repos.connect() as db:
        assert db.execute('SELECT status FROM memory_records WHERE id=?', (old['id'],)).fetchone()[0] == 'active'
