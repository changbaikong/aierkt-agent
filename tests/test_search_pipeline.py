"""Entry routing, bounded evidence acquisition and isolated answer composition."""
import asyncio
import importlib.util
import json

import pytest

from agent_core.contracts import ProviderResponse
from agent_core.search_gate import SearchGate, classify_request_rules


@pytest.mark.parametrize('text', [
    '详细写个故事', '比较这两段代码', '详细解释这段代码',
    '总结我提供的新闻资料', '水为什么会沸腾',
])
def test_material_and_stable_requests_do_not_search(text):
    assert classify_request_rules(text).intent == 'direct_chat'


def test_uncertain_chat_draft_does_not_trigger_classification():
    class Provider:
        async def complete(self, *args, **kwargs):
            raise AssertionError('ordinary chat must not call an intent model')
    intent = asyncio.run(SearchGate(Provider()).classify_request('我今天好累', candidate_reply='可能是工作压力。'))
    assert intent.intent == 'direct_chat'
    assert intent.source == 'rule'


def pipeline():
    assert importlib.util.find_spec('agent_core.search_orchestrator'), 'missing independent search orchestration'
    from agent_core.search_orchestrator import SearchBudget, SearchOrchestrator
    return SearchBudget, SearchOrchestrator


def row(domain='official.example', snippet='Nova 是一家研究机构，成立于2020年。'):
    return {'title': 'Nova 简介', 'url': f'https://{domain}/intro', 'snippet': snippet, 'platform': 'web'}


def test_fast_search_stops_with_one_relevant_source_without_reading():
    Budget, Orchestrator = pipeline()
    queries, reads = [], []
    async def search(query):
        queries.append(query)
        return {'results': [row()]}
    async def read(url):
        reads.append(url)
        return {'content': '正文'}
    bundle = asyncio.run(Orchestrator(search, read_page=read, budget=Budget()).run('Nova 是谁', classify_request_rules('Nova 是谁')))
    assert bundle.searches_used == 1
    assert bundle.reads_used == 0
    assert bundle.stop_reason == 'coverage_met'
    assert bundle.sources[0]['url'] == 'https://official.example/intro'


@pytest.mark.parametrize('response', [None, {'results': []}])
def test_failed_and_empty_searches_count_and_stop(response):
    Budget, Orchestrator = pipeline()
    async def search(query):
        if response is None:
            raise RuntimeError('offline')
        return response
    bundle = asyncio.run(Orchestrator(search, budget=Budget()).run('Nova 是谁', classify_request_rules('Nova 是谁')))
    assert 1 <= bundle.searches_used <= 2
    assert bundle.coverage == 'missing'
    assert bundle.stop_reason in {'all_channels_failed', 'no_new_information', 'budget_exhausted'}


def test_copy_across_channels_counts_once():
    Budget, Orchestrator = pipeline()
    async def search(query):
        return {'results': [row(), {**row('copy.example'), 'platform': 'bing'}]}
    bundle = asyncio.run(Orchestrator(search, budget=Budget()).run('Nova 是谁', classify_request_rules('Nova 是谁')))
    assert sum(s['independent'] for s in bundle.sources) == 1
    assert len(bundle.duplicates) == 1
    assert bundle.searches_used == 1


def test_explicit_reads_and_searches_obey_hard_budget():
    Budget, Orchestrator = pipeline()
    async def search(query):
        return {'results': [row(), row('other.example', 'Nova 的项目仍在研究中。')]}
    async def read(url):
        raise RuntimeError('reader offline')
    async def assess(question, facts):
        return {'covered': False, 'missing': ['时间线'], 'followup_query': 'Nova 时间线 官方'}
    bundle = asyncio.run(Orchestrator(search, read_page=read, assess_coverage=assess, budget=Budget(max_search_rounds=3, max_page_reads=2)).run('请调研 Nova 并查原文核实时间线', classify_request_rules('请调研 Nova 并查原文核实时间线')))
    assert bundle.searches_used <= 3
    assert bundle.reads_used == 2
    assert len(bundle.failures) >= 2


def test_answer_model_sees_facts_without_internal_logs():
    """整理后的事实进主回答；运输日志（失败原因/次数/停因）一概不过边界。"""
    Budget, Orchestrator = pipeline()
    async def search(query):
        return {'results': [row()], 'failures': [{'reason': 'secret transport error'}]}
    bundle = asyncio.run(Orchestrator(search, budget=Budget()).run('Nova 是谁', classify_request_rules('Nova 是谁')))
    payload = json.dumps(bundle.answer_facts(), ensure_ascii=False)
    assert 'secret transport error' not in payload
    assert 'searches_used' not in payload
    assert 'stop_reason' not in payload
    assert 'https://' not in payload
    assert bundle.materials_unverified is True, '没有整理器时必须标明材料尚未核实'


def test_organized_facts_keep_conditions_and_drop_unknown_source_ids():
    """事实整理只认材料里存在的来源编号，并保留适用条件。"""
    Budget, Orchestrator = pipeline()
    async def search(query):
        return {'results': [row()]}
    async def assess(question, materials):
        return {
            'covered': False, 'missing': ['发布日期'], 'uncovered': ['是否仍在运营'],
            'key_facts': [
                {'text': 'Nova 成立于2020年', 'source_ids': ['source-1'], 'conditions': '仅限官网口径'},
                {'text': '凭空捏造的事实', 'source_ids': ['source-9'], 'conditions': ''},
            ],
        }
    bundle = asyncio.run(Orchestrator(search, assess_coverage=assess, budget=Budget()).run('Nova 是谁', classify_request_rules('Nova 是谁')))
    assert len(bundle.facts) == 1
    assert bundle.facts[0]['conditions'] == '仅限官网口径'
    assert bundle.facts[0]['evidence_level'] == 'snippet'
    assert bundle.materials_unverified is False
    assert bundle.uncovered == ('是否仍在运营',)


def test_original_read_failure_keeps_snippet_evidence_level():
    """原文读取失败时事实仍是摘要级别，不能升级成"读过原文/官方确认"。"""
    Budget, Orchestrator = pipeline()
    async def search(query):
        return {'results': [row()]}
    async def read(url):
        raise RuntimeError('reader offline')
    async def assess(question, materials):
        return {'covered': True, 'missing': [], 'key_facts': [
            {'text': 'Nova 成立于2020年', 'source_ids': ['source-1'], 'conditions': ''},
        ]}
    bundle = asyncio.run(Orchestrator(
        search, read_page=read, assess_coverage=assess, budget=Budget(),
    ).run('Nova 是谁并查原文', classify_request_rules('Nova 是谁并查原文')))
    assert bundle.facts and bundle.facts[0]['evidence_level'] == 'snippet'
    assert any(
        item.get('url') == 'https://official.example/intro' for item in bundle.failures
    ), "读取失败要如实记录在内部失败清单里"


def test_late_paragraph_conditions_survive_material_selection():
    """限制条件写在网页后半段时也要进入材料，而不是被前截断丢掉。"""
    from agent_core.search_orchestrator import select_excerpt

    tail = "限制条件：仅限企业版，个人版不支持多人协作。"
    body = "Nova 产品介绍。" + "\n" + "细节说明。" * 800 + "\n" + tail
    excerpt, truncated = select_excerpt(body, "Nova 企业版支持哪些功能？")
    assert truncated is True
    assert "仅限企业版" in excerpt, "后半段的限制条件必须保留"
    assert len(excerpt) <= 2200


def test_sources_are_part_of_outcome_not_reply():
    from agent_core.contracts import AgentOutcome
    assert 'sources' in AgentOutcome.__dataclass_fields__
    outcome = AgentOutcome('completed', 'run', reply='结论', sources=({'url': 'https://example.com'},), answer_status='partially_answered')
    assert outcome.reply == '结论'


def build_loop(tmp_path, provider, handler, bus=None):
    from agent_core.contracts import Scope, ToolSpec, EFFECT_READ
    from agent_core.loop import AgentLoop
    from agent_core.tools import ToolRegistry
    from agent_core.persistence import SQLiteRunStore, SQLiteConfirmationStore
    registry = ToolRegistry()
    registry.register(ToolSpec('search__web', {'type': 'function', 'function': {'name': 'search__web', 'parameters': {'type': 'object'}}}, handler, effect=EFFECT_READ, deterministic_route_allowed=True))
    return AgentLoop(provider, registry, Scope('u', 'web', 'pipeline', 'companion'), search_gate=SearchGate(provider), event_bus=bus,
                     run_store=SQLiteRunStore(tmp_path / 'core.db'), confirmation_store=SQLiteConfirmationStore(tmp_path / 'core.db'))


class PipelineProvider:
    def __init__(self, timeline, *, audit_bad=False):
        self.timeline = timeline
        self.requests = []
        self.audit_bad = audit_bad

    async def complete(self, messages, **kwargs):
        self.requests.append(messages)
        systems = [str(message.get('content') or '') for message in messages if message.get('role') == 'system']
        marker = next((text for text in systems if text), '')
        if any(text.startswith('你是检索材料整理器') for text in systems):
            self.timeline.append('coverage')
            text = json.dumps({
                'covered': True, 'missing': [], 'uncovered': [],
                'key_facts': [{
                    'text': 'Nova 是一家研究机构，成立于2020年。',
                    'source_ids': ['source-1'], 'conditions': '',
                }],
            }, ensure_ascii=False)
        elif any(text.startswith('你是最终核查器') for text in systems):
            self.timeline.append('review')
            text = json.dumps({'publishable': not self.audit_bad, 'issue': '收窄细节'})
        elif any(text.startswith('请依据上面同一份对话背景') for text in systems):
            self.timeline.append('rewrite')
            text = 'Nova 是一家研究机构。'
        else:
            self.timeline.append('chat')
            text = 'Nova 是一家研究机构。'
        return ProviderResponse(message={'role': 'assistant', 'content': text}, usage={})


def test_search_happens_before_answer_and_sources_are_persisted(tmp_path):
    timeline = []
    def handler(arguments, scope):
        timeline.append('search')
        return {'status': 'ok', 'output': {'results': [row()]}}
    provider = PipelineProvider(timeline)
    loop = build_loop(tmp_path, provider, handler)
    outcome = asyncio.run(loop.run('Nova 是谁'))
    assert timeline == ['search', 'coverage', 'chat', 'review']
    assert outcome.reply == 'Nova 是一家研究机构。'
    assert outcome.sources[0]['url'] == 'https://official.example/intro'
    assert loop.public_messages[-1]['sources'] == list(outcome.sources)
    assert '来源' not in outcome.reply
    # 主回答走主循环：它看到的是整理后的事实（工具证据），不是孤立回答器的四条历史
    answer_request = provider.requests[-2]
    assert 'tool' in {message['role'] for message in answer_request}
    payload = json.dumps(answer_request, ensure_ascii=False)
    assert 'facts' in payload
    assert 'stop_reason' not in payload, '运输日志不能进入主回答上下文'


def test_chat_skips_all_search_quality_calls_and_keeps_streaming(tmp_path):
    from agent_core.events import InMemoryEventBus, EventType
    timeline, deltas = [], []
    bus = InMemoryEventBus()
    async def listener(event):
        if event.type.value == 'chat.delta':
            deltas.append(event.data['delta'])
    bus.subscribe(EventType.CHAT_DELTA, listener)
    class StreamingProvider(PipelineProvider):
        async def stream_complete(self, messages, *, on_delta, **kwargs):
            await on_delta('陪你歇一会儿。')
            assert deltas == ['陪你歇一会儿。']
            return ProviderResponse(message={'role': 'assistant', 'content': '陪你歇一会儿。'}, usage={})
    def handler(arguments, scope):
        raise AssertionError('chat searched')
    loop = build_loop(tmp_path, StreamingProvider(timeline), handler, bus)
    result = asyncio.run(loop.run('我今天好累'))
    assert result.reply == '陪你歇一会儿。'
    assert timeline == []


def test_audit_correction_does_not_restart_search(tmp_path):
    timeline = []
    def handler(arguments, scope):
        timeline.append('search')
        return {'status': 'ok', 'output': {'results': [row()]}}
    result = asyncio.run(build_loop(tmp_path, PipelineProvider(timeline, audit_bad=True), handler).run('Nova 是谁'))
    assert timeline.count('search') == 1, '审查不通过不能自动重新取证'
    assert timeline.count('rewrite') == 1, '最多重写一次'
    assert timeline.count('review') == 2, '重写后必须再核查一次'
    assert result.sources


def test_completed_event_and_history_preserve_structured_sources(tmp_path):
    from types import SimpleNamespace
    from agent_core.contracts import AgentOutcome, AgentRequest, Scope
    from agent_core.conversation_manager import ConversationManager
    from agent_core.conversation_repository import ConversationRepository
    from agent_core.events import InMemoryEventBus, EventType
    async def scenario():
        sources = ({'url': 'https://official.example/intro', 'title': 'Nova'},)
        class Session:
            async def run(self, request):
                return AgentOutcome('completed', 'run', reply='结论', sources=sources, answer_status='partially_answered')
        bus, events = InMemoryEventBus(), []
        bus.subscribe(EventType.CHAT_COMPLETED, events.append)
        scope = Scope('u', 'web', 'metadata')
        repo = ConversationRepository(tmp_path / 'history.db')
        manager = ConversationManager(lambda _: Session(), event_bus=bus, conversation_repository=repo)
        try:
            handle = await manager.submit(AgentRequest('问题', scope=scope))
            await handle.result
            assert events[-1].data['sources'] == list(sources)
            assert events[-1].data['answer_status'] == 'partially_answered'
            message = repo.history(scope)[-1]
            assert message['content'] == '结论'
            assert message['sources'] == list(sources)
            assert message['answer_status'] == 'partially_answered'
        finally:
            await manager.shutdown()
    asyncio.run(scenario())
