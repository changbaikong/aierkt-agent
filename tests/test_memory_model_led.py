"""Expected meaning is set by the test, then checked against real isolated rows."""
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent_core.contracts import Scope
from agent_core.memory_processing import MemoryReviewProcessor, REVIEW_PROMPT, REVIEW_TIMEOUT_SECONDS
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryRef, MemoryUpdateSuggestion, turn_from_messages
from agent_core.memory_writes import MemoryWriter


class Embeddings:
    def embed(self, text):
        return [.1, .2, .3]


class Judge:
    def __init__(self, verdict, content, quote, reason):
        self.verdict, self.content, self.quote, self.reason = verdict, content, quote, reason
        self.calls = 0
        self.input = None

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.input = json.loads(messages[-1]['content'])
        row = dict(index=0, verdict=self.verdict, content=self.content,
                   quotes=[dict(message_id='t', quote=self.quote)], reason=self.reason,
                   subject='张三的教师关系', same_subject=True, preserves_negation=True,
                   preserves_conditions=True, change_authorized=True,
                   lifetime={'mode':'long_term','basis':'用户陈述持续事实'})
        return SimpleNamespace(message={'content':json.dumps({'items':[row]}, ensure_ascii=False)})


@pytest.fixture
def env(tmp_path):
    repos = initialize_memory_repositories(tmp_path/'records.db', tmp_path/'vectors', vector_size=3)
    writer = MemoryWriter(repos, Embeddings())
    scope = Scope('model-led', 'web', 'c', 'companion')
    yield repos, writer, scope
    repos.close()


def packet(scope, text):
    return turn_from_messages(turn_id='t', occurred_at=datetime.now(UTC).isoformat(), scope=scope,
                              user_text=text, published_reply='收到')


def candidate(text):
    return MemoryCandidate(text, 'fact', 'reported', ('user_statement',), source_refs=('t',))


def test_review_prompt_separates_phase_lifetime_from_explicit_short_term_duration():
    assert '“暂时”本身不等于一天' in REVIEW_PROMPT
    assert '一次请求或当天情绪按政策一天、临时提醒三天' in REVIEW_PROMPT
    assert 'short_term 必须有原话明确的天数' not in REVIEW_PROMPT
    assert '阶段决定使用 phase' in REVIEW_PROMPT


def test_review_prompt_uses_same_schema_as_validator():
    from agent_core.memory_protocol import RESPONSE_SCHEMA
    assert 'adoption 必须是与 index、verdict 同级的字段' in REVIEW_PROMPT
    assert json.loads(REVIEW_PROMPT.splitlines()[-1]) == RESPONSE_SCHEMA
    assert set(RESPONSE_SCHEMA['properties']['items']['items']['properties']['quotes']['items']['required']) == {'source_id', 'start', 'end'}


def test_review_default_timeout_matches_b_call_budget():
    assert REVIEW_TIMEOUT_SECONDS == 60.0


@pytest.mark.asyncio
async def test_model_rejection_is_consumed_even_when_old_word_rules_thought_it_supported(env):
    repos, writer, scope = env
    p = packet(scope, '张三是李四的老师。')
    judge = Judge('unsupported', '张三的李四是老师', '张三是李四的老师', '关系结构被改变，不能保存')
    prepared, _, result = await MemoryReviewProcessor(writer, judge).prepare(p, [candidate(judge.content)], persist=lambda x:None)
    assert judge.calls == 1
    assert not prepared and result.rejected
    with repos.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records WHERE semantics_version=1').fetchone()[0] == 0


@pytest.mark.asyncio
async def test_model_supported_paraphrase_is_saved_with_its_judgment_and_quotes(env):
    repos, writer, scope = env
    p = packet(scope, '张三的老师是李四。')
    judge = Judge('supported', '张三由李四教导', '张三的老师是李四', '教师是李四，学生是张三，人物角色一致')
    durable = []
    prepared, _, result = await MemoryReviewProcessor(writer, judge).prepare(p, [candidate(judge.content)], persist=durable.append)
    assert prepared, result.as_dict()
    rows = writer.save_semantic_candidates(scope, prepared, packet=p, turn_id='t')
    assert len(rows) == 1
    meta = json.loads(rows[0]['metadata'])
    judgment = meta['evidence_receipt']['semantic_judgment']
    assert judgment['reason'] == judge.reason
    assert judgment['quotes'][0]['quote'] == judge.quote
    assert durable[0]['raw']


@pytest.mark.asyncio
async def test_model_gets_original_packet_target_and_prior_judgment(env):
    from support.memory_seed import seed_semantic
    repos, writer, scope = env
    old = seed_semantic(writer, scope, [candidate('张三的老师是李四')], turn_id='seed')[0]
    p = replace(packet(scope, '老师是王五。'), recalled_memory_refs=[MemoryRef(old['id'],'fact','reported',old['content'],version=1)])
    s = MemoryUpdateSuggestion('correct',content='李四的老师是王五',kind='fact',content_status='reported',
        target_id=old['id'],expected_version=1,source_types=('user_statement',),source_refs=('t',))
    judge = Judge('unsupported', s.content, '老师是王五', '错误继承旧宾语为新主语')
    _, prepared, result = await MemoryReviewProcessor(writer, judge).prepare(p, [], [s], persist=lambda x:None)
    assert judge.calls == 1
    assert not prepared and result.rejected
    assert judge.input['packet']['user_text'] == '老师是王五。'
    assert judge.input['items'][0]['target']['content'] == old['content']
    assert repos.records_by_ids([old['id']])[old['id']]['status'] == 'active'


class StructuredJudge:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0
    async def complete(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(message={'content':json.dumps({'items':self.rows},ensure_ascii=False)})


def reviewed_row(content, quote, *, mode='long_term', days=None, subject='事项', change=True):
    return dict(index=0,verdict='supported',content=content,subject=subject,reason='测试预期的原话含义',
        same_subject=True,preserves_negation=True,preserves_conditions=True,change_authorized=change,
        quotes=[dict(message_id='t',quote=quote)],lifetime=dict(mode=mode,days=days,basis='该事项原话'))


@pytest.mark.asyncio
async def test_model_splits_distinct_matters_and_controls_each_lifetime(env):
    repos, writer, scope = env
    p = packet(scope,'临时提醒保留三天。日志原则是长期保留错误记录。')
    item = candidate('临时提醒保留三天，日志原则是长期保留错误记录')
    parts = [reviewed_row('临时提醒保留三天','临时提醒保留三天',mode='short_term',days=3,subject='临时提醒'),
             reviewed_row('日志原则是长期保留错误记录','日志原则是长期保留错误记录',subject='日志原则')]
    judge = StructuredJudge([dict(index=0,verdict='supported',parts=parts)])
    prepared,_,result = await MemoryReviewProcessor(writer,judge).prepare(p,[item],persist=lambda x:None)
    assert judge.calls == 1
    assert len(prepared) == 2,result.as_dict()
    rows = writer.save_semantic_candidates(scope,prepared,packet=p,turn_id='t')
    assert len(rows) == 2
    reminder = next(r for r in rows if '临时提醒' in r['content'])
    principle = next(r for r in rows if '日志原则' in r['content'])
    assert datetime.fromisoformat(reminder['expires_at']) - datetime.fromisoformat(reminder['created_at']) == __import__('datetime').timedelta(days=3)
    assert principle['expires_at'] is None
    assert json.loads(principle['metadata'])['evidence_receipt']['semantic_judgment']['subject'] == '日志原则'


@pytest.mark.asyncio
async def test_model_keeps_valid_part_when_sibling_part_needs_clarification(env):
    _, writer, scope = env
    p = packet(scope, '临时提醒保留三天。继续做本机验收。')
    item = candidate('临时提醒保留三天，继续做本机验收')
    parts = [
        reviewed_row('临时提醒保留三天', '临时提醒保留三天', mode='short_term', days=3, subject='临时提醒'),
        reviewed_row('继续做本机验收', '继续做本机验收', mode='short_term', days=None, subject='本机验收'),
    ]
    judge = StructuredJudge([dict(index=0, verdict='supported', parts=parts)])
    prepared, _, result = await MemoryReviewProcessor(writer, judge).prepare(p, [item], persist=lambda x: None)
    assert [row.content for row in prepared] == ['临时提醒保留三天']
    assert len(result.pending_review) == 1
    assert result.pending_review[0]['part_index'] == 1


@pytest.mark.asyncio
async def test_exact_original_decision_still_gets_model_lifetime(env):
    _,writer,scope = env
    p = packet(scope,'排期暂不调整。')
    item = MemoryCandidate('排期暂不调整','decision','accepted',('user_statement',),source_refs=('t',))
    judge = StructuredJudge([reviewed_row(item.content,item.content,mode='phase',subject='排期')])
    prepared,_,result = await MemoryReviewProcessor(writer,judge).prepare(p,[item],persist=lambda x:None)
    assert judge.calls == 1
    rows = writer.save_semantic_candidates(scope,prepared,packet=p,turn_id='t')
    assert rows[0]['expires_at'] is None
    assert json.loads(rows[0]['metadata'])['lifetime_basis'] == '该事项原话'


@pytest.mark.asyncio
async def test_reviewed_paraphrase_survives_restart_without_repeating_model(env):
    from agent_core.memory_turn import decode_candidates, serialize_candidates
    repos, writer, scope = env
    p = packet(scope, '张三的老师是李四。')
    judge = Judge('supported', '张三由李四教导', '张三的老师是李四', '角色一致')
    prepared, _, _ = await MemoryReviewProcessor(writer, judge).prepare(p, [candidate(judge.content)], persist=lambda x:None)
    assert prepared
    restored = decode_candidates(serialize_candidates(prepared))
    restarted = MemoryWriter(repos, Embeddings())
    again, _, _ = await MemoryReviewProcessor(restarted, judge).prepare(p, restored, persist=lambda x:None)
    assert judge.calls == 1
    assert len(restarted.save_semantic_candidates(scope, again, packet=p, turn_id='t')) == 1


@pytest.mark.asyncio
async def test_old_review_does_not_authorize_changed_meaning(env):
    repos, writer, scope = env
    p = packet(scope, '张三的老师是李四。')
    judge = Judge('supported', '张三由李四教导', '张三的老师是李四', '角色一致')
    prepared, _, _ = await MemoryReviewProcessor(writer, judge).prepare(p, [candidate(judge.content)], persist=lambda x:None)
    assert prepared
    assert not writer.save_semantic_candidates(scope, [replace(prepared[0],content='李四由张三教导')], packet=p,turn_id='t')


def test_model_supplied_positive_dictionary_is_not_a_service_review(env):
    _, writer, scope = env
    p = packet(scope, '张三的老师是李四。')
    fake = replace(candidate('李四由张三教导'), review_receipt={'semantic_judgment':{'verdict':'supported','reason':'已核对'}})
    assert not writer.save_semantic_candidates(scope, [fake], packet=p,turn_id='t')


@pytest.mark.asyncio
async def test_durable_review_is_required_before_nonliteral_memory_commit(env):
    _, writer, scope = env
    p = packet(scope, '张三的老师是李四。')
    judge = Judge('supported', '张三由李四教导', '张三的老师是李四', '角色一致')
    prepared, _, result = await MemoryReviewProcessor(writer, judge).prepare(p, [candidate(judge.content)])
    assert not prepared and result.pending_review


@pytest.mark.asyncio
async def test_retrieved_judgment_reaches_the_next_memory_agent_input(env, monkeypatch):
    from agent_core.memory_retrieval import MemoryRetriever
    from agent_core.memory_agent import MemoryAgentSession
    from agent_core.memory import MemoryContext
    from agent_core.memory_extract import render_turn_packet
    repos, writer, scope = env
    p = packet(scope, '张三的老师是李四。')
    item = replace(candidate('张三由李四教导'), scope_kind='project',project_id='teachers')
    p = replace(p, project_binding='teachers')
    judge = Judge('supported',item.content,'张三的老师是李四','张三为学生，李四为教师')
    prepared,_,_ = await MemoryReviewProcessor(writer,judge).prepare(p,[item],persist=lambda x:None)
    row = writer.save_semantic_candidates(scope,prepared,packet=p,turn_id='t',project_binding='teachers')[0]
    monkeypatch.setenv('MEMORY_SEMANTIC_ENABLED','true')
    later = Scope(scope.owner_id,scope.channel,'later',scope.module_id)
    recalled = MemoryRetriever(repos,Embeddings()).retrieve(later,'张三',project_id='teachers')
    assert row['id'] in {r['id'] for r in recalled.warm}
    agent = object.__new__(MemoryAgentSession)
    agent.loop = SimpleNamespace()
    handoff = agent._turn_handoff(MemoryContext(warm=recalled.warm),SimpleNamespace(sources=[]))
    next_packet = turn_from_messages(turn_id='next',occurred_at=datetime.now(UTC).isoformat(),scope=later,
        user_text='现在老师是谁？',published_reply='李四',handoff=handoff)
    assert judge.reason in render_turn_packet(next_packet)
    assert judge.quote in render_turn_packet(next_packet)
    context = MemoryRetriever(repos,Embeddings()).build_context(later,'张三',project_id='teachers')
    assert judge.reason in context.injected_text
    assert judge.quote in context.injected_text


@pytest.mark.asyncio
async def test_semantic_change_authorization_marks_pending_without_word_markers(env):
    from support.memory_seed import seed_semantic
    from agent_core.memory_binding import valid_pending_change
    repos,writer,scope = env
    old = seed_semantic(writer,scope,[candidate('张三的老师是李四')],turn_id='seed')[0]
    p = packet(scope,'张三如今跟着王五学习。')
    s = MemoryUpdateSuggestion('correct',content='张三的老师是王五',kind='fact',content_status='reported',
        target_id=old['id'],expected_version=1,source_types=('user_statement',),source_refs=('t',))
    judge = StructuredJudge([reviewed_row(s.content,'张三如今跟着王五学习',subject='张三的老师')])
    _, prepared, result = await MemoryReviewProcessor(writer,judge).prepare(p,[],[s],persist=lambda x:None)
    assert prepared,result.as_dict()
    pending = repos.records_by_ids([old['id']])[old['id']]
    assert valid_pending_change(pending)
    assert pending['review_state'] == 'pending'


@pytest.mark.asyncio
async def test_model_authorized_superseding_candidate_reaches_commit(env):
    from support.memory_seed import seed_semantic
    repos,writer,scope = env
    old = seed_semantic(writer,scope,[candidate('张三的老师是李四')],turn_id='seed')[0]
    p = replace(packet(scope,'张三现在拜王五为师。'),recalled_memory_refs=[MemoryRef(old['id'],'fact','reported',old['content'],version=1)])
    item = replace(candidate('王五教张三'),supersedes=(old['id'],))
    judge = StructuredJudge([reviewed_row(item.content,'张三现在拜王五为师',subject='张三的老师')])
    prepared,_,result = await MemoryReviewProcessor(writer,judge).prepare(p,[item],persist=lambda x:None)
    assert prepared,result.as_dict()
    rows = writer.save_semantic_candidates(scope,prepared,packet=p,turn_id='t',known_ids=[old['id']])
    assert len(rows) == 1
    assert repos.records_by_ids([old['id']])[old['id']]['status'] == 'archived'


@pytest.mark.asyncio
async def test_mismatched_cache_is_not_durable_authority(env):
    _,writer,scope = env
    p = packet(scope,'张三的老师是李四。')
    judge = Judge('supported','张三由李四教导','张三的老师是李四','角色一致')
    prepared,_,result = await MemoryReviewProcessor(writer,judge).prepare(p,[candidate(judge.content)],cached={'key':'stale','items':[]})
    assert not prepared and result.pending_review


def test_parser_leaves_semantic_status_for_the_reviewing_agent(env):
    from agent_core.memory_extract import SemanticMemoryExtractor
    _,_,scope = env
    p = packet(scope,'选型就锁定乙吧。')
    extractor = SemanticMemoryExtractor(None)
    items,rejected = extractor.parse(json.dumps({'memories':[dict(content='选型锁定乙',kind='decision',content_status='accepted',source_types=['user_statement'],source_refs=['t'])]},ensure_ascii=False),packet=p)
    assert items and items[0].content_status == 'accepted'


def test_program_label_is_not_a_public_missing_evidence_bypass(env):
    _,writer,scope = env
    assert not writer.save_semantic_candidates(scope,[replace(candidate('虚构事实'),origin='program')],turn_id='t')


@pytest.mark.asyncio
async def test_model_cannot_add_approval_without_any_actual_approval_source(env):
    _, writer, scope = env
    p = packet(scope,'张三的老师是李四。')
    judge = Judge('supported','张三的老师是李四，安全评审已经通过','张三的老师是李四','已核对')
    prepared,_,result = await MemoryReviewProcessor(writer,judge).prepare(p,[candidate('张三由李四教导')],persist=lambda x:None)
    assert not prepared


def test_exact_new_quote_still_cannot_replace_an_unrelated_old_record(env):
    from support.memory_seed import seed_semantic
    repos, writer, scope = env
    old = seed_semantic(writer,scope,[candidate('张三的老师是李四')],turn_id='seed')[0]
    p = packet(scope,'广告预算3000元。')
    s = MemoryUpdateSuggestion('correct',content='广告预算3000元',kind='fact',content_status='reported',
        target_id=old['id'],expected_version=1,source_types=('user_statement',),source_refs=('t',))
    with pytest.raises(ValueError):
        writer.apply_semantic_update(scope,s,target=old,packet=p,turn_id='t')
    assert repos.records_by_ids([old['id']])[old['id']]['status'] == 'active'


@pytest.mark.asyncio
async def test_unfamiliar_success_word_is_judged_by_model(env):
    _,writer,scope = env
    p = packet(scope,'上线已经办妥。')
    judge = Judge('supported','上线已完成','上线已经办妥','办妥表示上线完成，属于原话忠实改写')
    prepared,_,result = await MemoryReviewProcessor(writer,judge).prepare(p,[candidate(judge.content)],persist=lambda x:None)
    assert judge.calls == 1
    assert prepared,result.as_dict()
    assert writer.save_semantic_candidates(scope,prepared,packet=p,turn_id='t')


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [None, 'true', 1])
async def test_update_requires_explicit_boolean_model_authorization(env, value):
    from support.memory_seed import seed_semantic
    repos, writer, scope = env
    old = seed_semantic(writer,scope,[candidate('张三的老师是李四')],turn_id='seed')[0]
    p = packet(scope,'张三的老师改成王五。')
    s = MemoryUpdateSuggestion('correct',content='张三由王五教导',kind='fact',content_status='reported',
        target_id=old['id'],expected_version=1,source_types=('user_statement',),source_refs=('t',))
    class InvalidJudge(Judge):
        async def complete(self, messages, **kwargs):
            response = await super().complete(messages, **kwargs)
            data = json.loads(response.message['content'])
            data['items'][0]['change_authorized'] = value
            response.message['content'] = json.dumps(data,ensure_ascii=False)
            return response
    judge = InvalidJudge('supported',s.content,'张三的老师改成王五','确认老师变更')
    _, prepared, result = await MemoryReviewProcessor(writer,judge).prepare(p,[],[s],persist=lambda x:None)
    assert not prepared, '缺少明确授权不得签发替代凭据'
    assert repos.records_by_ids([old['id']])[old['id']]['status'] == 'active'
