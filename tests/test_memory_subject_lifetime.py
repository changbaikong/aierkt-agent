"""Lifetimes are checked against quotes and real database expiry timestamps."""
import json
import asyncio
from datetime import UTC, datetime, timedelta
from dataclasses import replace

import pytest

from agent_core.contracts import Scope
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, turn_from_messages
from agent_core.memory_writes import MemoryWriter
from agent_core.memory_processing import MemoryReviewProcessor
from support.memory_review import FixedReviewProvider, supported_row


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


@pytest.fixture
def env(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    yield repos, MemoryWriter(repos, Embeddings()), Scope('expiry','web','c','companion')
    repos.close()


def save(env, user, content, kind='decision', status='accepted', horizon='long_term', *, mode='long_term', days=None, parts=None, quote=None):
    repos, writer, scope = env
    packet = turn_from_messages(turn_id='t', occurred_at=datetime.now(UTC).isoformat(), scope=scope,
                                user_text=user, published_reply='收到')
    candidate = MemoryCandidate(content, kind, status, ('user_statement',), source_refs=('t',), horizon=horizon)
    fixture_quotes = {
        '项目日志一律先落盘再回传。另外临时提醒保留三天。': ['项目日志一律先落盘再回传'],
        '云端部署的建议你再分析一下。': ['云端部署的建议你再分析一下'],
        '今天心情很累。': ['今天心情很累'],
        '暂不部署，继续做本机验收。': ['暂不部署', '继续做本机验收'],
        '项目日志一律先落盘再回传；临时提醒保留三天。': ['项目日志一律先落盘再回传', '临时提醒保留三天'],
    }
    quotes = [('t', text) for text in fixture_quotes[user]] if quote is None else [('t', quote)]
    row = supported_row(content, quotes, subject='当前测试事项', mode=mode, days=days, status=status)
    if parts:
        row['parts'] = [supported_row(text, [('t', quote)], subject=subject, mode=part_mode, days=part_days, status=status)
                        for text, quote, subject, part_mode, part_days in parts]
    provider = FixedReviewProvider([row])
    durable = []
    candidates, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
        packet, [candidate], [], persist=durable.append))
    assert provider.calls == 1
    assert candidates, result.as_dict()
    return writer.save_semantic_candidates(scope, candidates, packet=packet, turn_id='t')[0]


def test_an_unrelated_three_day_reminder_never_expires_the_log_principle(env):
    record = save(env, '项目日志一律先落盘再回传。另外临时提醒保留三天。', '项目日志一律先落盘再回传', quote='项目日志一律先落盘再回传')
    assert record['expires_at'] is None
    env[1].run_maintenance(now=datetime.now(UTC)+timedelta(days=4))
    assert env[0].records_by_ids([record['id']])[record['id']]['status'] == 'active'


@pytest.mark.parametrize('user,content,kind,status,declared', [
    ('云端部署的建议你再分析一下。', '云端部署的建议你再分析一下', 'open_question', 'open', 'short_term'),
    ('今天心情很累。', '今天心情很累', 'fact', 'reported', 'long_term'),
])
def test_one_off_request_and_daily_mood_get_one_day(env, user, content, kind, status, declared):
    record = save(env, user, content, kind, status, declared, mode='short_term', days=1)
    delta = datetime.fromisoformat(record['expires_at']) - datetime.fromisoformat(record['created_at'])
    assert delta == timedelta(days=1)


def test_phase_constraint_is_based_on_user_decision(env):
    record = save(env, '暂不部署，继续做本机验收。', '暂不部署，继续做本机验收', horizon='short_term', mode='phase')
    assert record['expires_at'] is None


def test_model_added_phase_words_do_not_create_a_phase_lifetime():
    from agent_core.memory_horizon import resolve_lifetime
    lifetime = resolve_lifetime('云端部署建议尚未得出结论', kind='open_question', declared='long_term',
                                source_text='云端部署的建议你再分析一下。')
    assert lifetime.expires_in == timedelta(days=1)


def test_different_subjects_are_split_and_expire_independently(env):
    record = save(env, '项目日志一律先落盘再回传；临时提醒保留三天。',
                  '项目日志一律先落盘再回传；临时提醒保留三天', parts=[
                      ('项目日志一律先落盘再回传', '项目日志一律先落盘再回传', '项目日志', 'long_term', None),
                      ('临时提醒保留三天', '临时提醒保留三天', '临时提醒', 'short_term', 3),
                  ])
    with env[0].connect() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM memory_records WHERE semantics_version=1')]
    assert len(rows) == 2
    principle = next(r for r in rows if r['content'] == '项目日志一律先落盘再回传')
    reminder = next(r for r in rows if r['content'] == '临时提醒保留三天')
    assert principle['expires_at'] is None
    assert datetime.fromisoformat(reminder['expires_at']) - datetime.fromisoformat(reminder['created_at']) == timedelta(days=3)
