"""Bounded evidence acquisition. No answer generation and no public narration."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .search_evidence import EvidenceSet, freshness_bucket
from .search_gate import INTENT_DEEP_SEARCH, INTENT_FRESH_SEARCH, RequestIntent, SearchGate

SearchCall = Callable[[str], Awaitable[dict[str, Any]]]
CoverageCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class SearchBudget:
    max_search_rounds: int = 3
    max_page_reads: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, 'max_search_rounds', max(0, min(3, self.max_search_rounds)))
        object.__setattr__(self, 'max_page_reads', max(0, min(2, self.max_page_reads)))


@dataclass(frozen=True)
class EvidenceBundle:
    """一次取证的完整结果：原始材料留在本地追踪，交出去的是**整理后的事实**。

    A4 的修法就落在这里：``materials`` 是候选材料（可追溯），``facts`` 是按用户
    问题整理过、带来源编号与适用条件的事实。只有 ``facts`` 会进入主回答的上下文；
    ``materials`` 只在整理失败时以"尚未核实"的身份兜底，并明确标记证据级别。
    """

    query: str
    intent: str
    claims: tuple[dict[str, Any], ...] = ()
    sources: tuple[dict[str, Any], ...] = ()
    duplicates: tuple[dict[str, Any], ...] = ()
    conflicts: tuple[dict[str, Any], ...] = ()
    reads: tuple[dict[str, Any], ...] = ()
    failures: tuple[dict[str, Any], ...] = ()
    searches_used: int = 0
    reads_used: int = 0
    stop_reason: str = ''
    coverage: str = 'missing'
    unconfirmed_points: tuple[str, ...] = ()
    #: 整理后的事实：{text, source_ids, conditions, evidence_level}
    facts: tuple[dict[str, Any], ...] = ()
    #: 候选材料（原始搜索摘要或原文片段，已按段落选取）。
    materials: tuple[dict[str, Any], ...] = ()
    #: 哪些候选材料被截取过（原网页比留存材料长）。
    truncated_materials: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()
    #: 整理器没跑成（没有覆盖检查、调用失败或输出不可解析）。
    materials_unverified: bool = False

    def answer_facts(self) -> dict[str, Any]:
        """只让事实材料过回答边界；运输日志、失败原因一律不带。

        整理成功 → 只给 ``facts``；整理失败 → 给带 ``materials_unverified`` 标记的
        候选材料，让主 Agent 自己收窄说法，而不是拿它当已核实结论。
        """
        payload: dict[str, Any] = {
            'facts': list(self.facts),
            'unconfirmed_points': list(self.unconfirmed_points),
            'uncovered': list(self.uncovered),
            'conditions': list(self.conditions),
            # 哪些来源的原文比留存材料长：模型据此知道"还有没看到的段落"。
            'truncated_materials': list(self.truncated_materials),
            'conflicts': [
                {k: v for k, v in conflict.items() if k in {'topic', 'detail', 'source_ids'}}
                for conflict in self.conflicts
            ],
            'materials_unverified': bool(self.materials_unverified),
            'limitations': {
                'no_evidence': not bool(self.facts or self.materials),
                'incomplete': self.coverage != 'covered',
                'has_original_text': any(
                    item.get('evidence_level') == 'original'
                    for item in (self.facts or self.materials)
                ),
            },
        }
        if self.materials_unverified and not self.facts:
            # 未核实材料只给有限的几条，并在字段名上就写明它的身份。
            payload['unverified_materials'] = [
                {k: material.get(k) for k in ('source_id', 'title', 'published_at', 'text', 'evidence_level', 'truncated')}
                for material in self.materials[:6]
            ]
        return payload


def _official(source: dict[str, Any]) -> bool:
    domain = source.get('domain', '')
    return (
        source.get('platform') == 'official'
        or '官方' in source.get('title', '')
        or domain.endswith(('.gov.cn', '.gov'))
        or any(domain == d or domain.endswith('.' + d) for d in (
            'openai.com', 'microsoft.com', 'apple.com', 'google.com', 'nvidia.com',
        ))
    )


#: 单条材料保留的上限（A4 的输入边界：候选材料最多 12 条、单条约 2200 字符）。
MAX_MATERIALS = 12
MATERIAL_CHARS = 2200
#: 整理后的事实条数与单条长度上限：事实要短，材料才长。
MAX_FACTS = 8
FACT_CHARS = 600


def _terms(text: str) -> set[str]:
    """粗分词：中文双字 + 英文数字词。够用来判断"这段跟问题有关吗"。"""
    lowered = str(text or '').lower()
    words = {word for word in re.findall(r'[a-z0-9][a-z0-9._+-]*', lowered) if len(word) > 1}
    chars = re.findall(r'[\u4e00-\u9fff]', lowered)
    words.update(chars[index] + chars[index + 1] for index in range(len(chars) - 1))
    return words


def select_excerpt(text: str, question: str, *, limit: int = MATERIAL_CHARS) -> tuple[str, bool]:
    """按段落选取与问题相关的原文片段，返回 ``(片段, 是否被截取)``。

    为什么不能只取开头：限制条件、价格前提、适用范围常常写在网页后半段，
    前截断会把它们整段丢掉，回答就会把"有条件的结论"说成"通用结论"。
    所以这里按整段打分、按原顺序拼回，并明确标记"材料被截取"。
    """
    body = str(text or '').strip()
    if len(body) <= limit:
        return body, False
    blocks = [block.strip() for block in re.split(r'\n\s*\n|\n', body) if block.strip()]
    if not blocks:
        return body[:limit], True
    query_terms = _terms(question)
    scored = [
        (len(query_terms & _terms(block)), index, block)
        for index, block in enumerate(blocks)
    ]
    ranked = sorted(scored, key=lambda item: (-item[0], item[1]))
    chosen: list[tuple[int, str]] = []
    used = 0
    for _score, index, block in ranked:
        if used and used + len(block) > limit:
            continue
        chosen.append((index, block[:limit]))
        used += len(block)
        if used >= limit:
            break
    if not chosen:
        return body[:limit], True
    chosen.sort(key=lambda item: item[0])
    return '\n'.join(block for _index, block in chosen)[:limit], True


class SearchOrchestrator:
    def __init__(
        self, search_round: SearchCall, *, read_page: SearchCall | None = None,
        search_video: SearchCall | None = None, assess_coverage: CoverageCall | None = None,
        on_stage: Callable[[str], Awaitable[None]] | None = None,
        budget: SearchBudget | None = None, timeout_seconds: float = 30.0,
    ) -> None:
        self.search_round = search_round
        self.read_page = read_page
        self.search_video = search_video
        self.assess_coverage = assess_coverage
        self.on_stage = on_stage
        self.budget = budget or SearchBudget()
        self.timeout_seconds = timeout_seconds

    async def run(self, question: str, intent: RequestIntent) -> EvidenceBundle:
        if not intent.needs_search:
            return EvidenceBundle(question, intent.intent, stop_reason='direct_chat')
        deep = intent.intent == INTENT_DEEP_SEARCH
        # Source count is a preference; it does not drive retries.
        evidence = EvidenceSet(query=question, intent=intent.intent, target_sources=1)
        searches = reads_used = 0
        reads: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        missing: list[str] = []
        stop_reason = 'budget_exhausted'
        coverage = 'missing'
        covered = False
        # 最近一次覆盖检查的整理结果（含关键事实 / 条件 / 未覆盖问题）。
        assessment: dict[str, Any] = {}
        organized: list[dict[str, Any]] = []
        uncovered: list[str] = []
        organized_ok = False
        queries = SearchGate.derive_queries(intent, question)
        # 只清理请求前缀/尾部礼貌词；不能全局删字（例如“并购”“并发”是主题的一部分）。
        queries = [
            re.sub(r'^(?:(?:请|帮我|调研|联网研究|全面|深入)\s*)+', '', q).strip()
            for q in queries
        ]
        queries = [
            re.sub(r'\s+(?:多个来源|多来源|查原文|核实|梳理|介绍|一下)$', '', q).strip() or q
            for q in queries
        ]
        queries = [
            re.sub(r'(?:并查原文|查原文核实|核实|梳理时间线).*$|\s+(?:并|以及)\s+(?:查原文|核实|梳理).*$','', q).strip() or q
            for q in queries
        ]
        query = queries[0] if queries else question
        used_queries: set[str] = set()
        read_urls: set[str] = set()
        previous_information: set[str] = set()
        video_used = False
        max_rounds = self.budget.max_search_rounds if deep else min(2, self.budget.max_search_rounds)
        async def stage(value: str) -> None:
            if self.on_stage:
                await self.on_stage(value)
                # 让阶段进度控制器有机会先发布当前节点，再进入下一步。
                # 否则“取得材料”与“开始读原文”在同一事件循环内会互相覆盖。
                # compose/publish 本身是异步任务，单次让出可能只让出给等待器；
                # 再让出一拍，确保阶段通知在下一次真实动作前有机会落库。
                await asyncio.sleep(0)
                await asyncio.sleep(0)

        def materials() -> list[dict[str, Any]]:
            """候选材料：原文优先，否则搜索摘要；按段落选取并标记截取。"""
            originals = {r['url']: r['excerpt'] for r in reads if r.get('fetch_status') == 'ok' and r.get('excerpt')}
            rows: list[dict[str, Any]] = []
            for index, source in enumerate(evidence.sources):
                if source.relevance < .15 or not (source.snippet or source.url in originals):
                    continue
                if not (source.independent or source.note.endswith('同一家站点）')):
                    continue
                is_original = source.url in originals
                text, truncated = select_excerpt(
                    originals.get(source.url, source.snippet) or '', question,
                )
                rows.append({
                    'source_id': f'source-{index + 1}', 'title': source.title,
                    'published_at': source.published_at, 'text': text,
                    'evidence_level': 'original' if is_original else 'snippet',
                    'independent': source.independent, 'truncated': truncated,
                })
            return rows[:MAX_MATERIALS]

        def organize(items: list[dict[str, Any]], checked: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
            """把覆盖检查返回的关键事实整理成可引用事实。

            三条硬约束：只能引用当前材料里存在的 source_id；证据级别跟着材料走
            （摘要不能升级成原文）；长材料里的条件原样保留。
            """
            allowed = {str(item.get('source_id') or ''): item for item in items}
            level_of = {
                key: str(item.get('evidence_level') or 'snippet') for key, item in allowed.items()
            }
            facts: list[dict[str, Any]] = []
            for raw in checked.get('key_facts') or []:
                if not isinstance(raw, dict):
                    continue
                text = str(raw.get('text') or '').strip()[:FACT_CHARS]
                if not text:
                    continue
                ids = [
                    str(value) for value in (raw.get('source_ids') or [])
                    if str(value) in allowed
                ][:4]
                if not ids:
                    # 引用不存在的来源编号：整条剔除，不能凭编号幻觉留下事实。
                    continue
                levels = {level_of[value] for value in ids}
                facts.append({
                    'text': text,
                    'source_ids': ids,
                    'conditions': str(raw.get('conditions') or '').strip()[:200],
                    'evidence_level': 'original' if levels == {'original'} else 'snippet',
                    # 依据的材料被截取过时如实标记：模型据此知道"后面可能还有条件"。
                    'truncated': any(bool(allowed[value].get('truncated')) for value in ids),
                })
                if len(facts) >= MAX_FACTS:
                    break
            gaps = [str(value)[:160] for value in (checked.get('uncovered') or []) if isinstance(value, str)][:5]
            return facts, gaps

        for round_index in range(max_rounds):
            use_video = bool(intent.meme and self.search_video and round_index > 0 and not video_used)
            callback = self.search_video if use_video else self.search_round
            if use_video:
                video_used = True
            key = ('video:' if use_video else 'web:') + query.strip().lower()
            if key in used_queries:
                stop_reason = 'no_new_information'
                break
            used_queries.add(key)
            # Reserve BEFORE awaiting: empty results, exceptions and timeout count equally.
            searches += 1
            try:
                output = await asyncio.wait_for(callback(query), self.timeout_seconds)
                failures.extend(output.get('failures') or output.get('provider_failures') or [])
            except Exception as exc:
                failures.append({'query': query, 'channel': 'video' if use_video else 'web', 'reason': str(exc)[:200]})
                if intent.meme and self.search_video and not video_used and round_index + 1 < max_rounds:
                    await stage('redirect')
                    continue
                stop_reason = 'all_channels_failed'
                break
            rows = output.get('results') or []
            for row in rows:
                if not isinstance(row, dict) or not str(row.get('url') or '').startswith(('http://', 'https://')):
                    continue
                evidence.add(
                    title=str(row.get('title') or ''), url=str(row['url']),
                    snippet=str(row.get('snippet') or row.get('summary') or ''),
                    platform=str(row.get('platform') or 'web'),
                    published_at=str(row.get('published_at') or ''), retrieval_query=query,
                )
            evidence.detect_conflicts()
            material = materials()
            information = {c['text'] for c in material}
            if round_index > 0 and not (information - previous_information):
                stop_reason = 'no_new_information'
                break
            previous_information = information
            covered = bool(material)
            missing = [] if covered else ['问题中的关键事实']
            followup = ''
            # Semantic coverage checks dimensions, relevance and dated claims, not site counts.
            if self.assess_coverage and material:
                try:
                    result = await asyncio.wait_for(self.assess_coverage(question, {
                        'intent': intent.intent, 'materials': material,
                        'conflicts': [c.to_dict() for c in evidence.conflicts],
                    }), self.timeout_seconds)
                    if isinstance(result, dict) and result:
                        assessment = result
                        organized_ok = True
                        if isinstance(result.get('covered'), bool):
                            covered = result['covered']
                            missing = [str(x)[:160] for x in result.get('missing', []) if isinstance(x, str)][:5]
                            followup = str(result.get('followup_query') or '')[:180]
                except Exception:
                    # A failed internal check must not cause more searching by itself.
                    assessment = {}
                    organized_ok = False
            elif deep:
                # Without a semantic check, do not manufacture a completeness verdict.
                covered = False
                missing = ['用户要求的调研维度']
            if intent.intent == INTENT_FRESH_SEARCH and material:
                dated = [s for s in evidence.sources if s.published_at and s.relevance >= .15]
                if dated and all(freshness_bucket(s.published_at) == 'stale' for s in dated):
                    covered = False
                    missing = ['当前有效的日期或价格']

            # Read only to resolve a specific gap, conflict, explicit original request, or official date/number.
            explicit_read = bool(re.search(r'(查原文|读原文|原始公告|原文阅读)', question))
            official_number = bool(re.search(r'(价格|多少钱|日期|发布|版本|售价)', question))
            should_read = explicit_read or bool(evidence.conflicts) or not covered
            candidates = sorted(
                [s for s in evidence.sources if s.relevance >= .15 and s.url not in read_urls],
                key=lambda s: (not _official(s.to_dict()), -s.relevance),
            )
            if official_number:
                should_read = should_read or any(_official(s.to_dict()) for s in candidates)
            if self.read_page and should_read:
                if candidates and reads_used < self.budget.max_page_reads:
                    await stage('materials_found')
                    # 阶段通知只描述真实已经发生的动作；读取之前让材料节点有机会发出。
                    await asyncio.sleep(0)
                for source in candidates:
                    if reads_used >= self.budget.max_page_reads:
                        break
                    if not deep and covered and not explicit_read and not evidence.conflicts and not _official(source.to_dict()):
                        continue
                    read_urls.add(source.url)
                    reads_used += 1
                    try:
                        result = await asyncio.wait_for(self.read_page(source.url), self.timeout_seconds)
                        status = str(result.get('fetch_status') or 'ok')
                        excerpt = str(result.get('content') or result.get('description') or result.get('excerpt') or '')[:5000]
                        reads.append({'url': source.url, 'fetch_status': status, 'excerpt': excerpt})
                        if status != 'ok' or not excerpt:
                            failures.append({'url': source.url, 'reason': 'original_unavailable'})
                        elif reads_used < self.budget.max_page_reads and len(candidates) > 1 and explicit_read:
                            await stage('source_read')
                            await asyncio.sleep(0)
                    except Exception as exc:
                        failures.append({'url': source.url, 'reason': str(exc)[:200]})
                    if not deep and not explicit_read and not evidence.conflicts:
                        break
                # Recheck only after new text arrived, using the same factual contract.
                if self.assess_coverage and any(r.get('excerpt') for r in reads):
                    try:
                        result = await asyncio.wait_for(self.assess_coverage(question, {
                            'intent': intent.intent, 'materials': materials(),
                            'conflicts': [c.to_dict() for c in evidence.conflicts],
                        }), self.timeout_seconds)
                        if isinstance(result, dict) and result:
                            assessment = result
                            organized_ok = True
                            if isinstance(result.get('covered'), bool):
                                covered = result['covered']
                                missing = [str(x)[:160] for x in result.get('missing', []) if isinstance(x, str)][:5]
                                followup = str(result.get('followup_query') or followup)[:180]
                    except Exception:
                        pass
            if explicit_read and not any(r.get('fetch_status') == 'ok' and r.get('excerpt') for r in reads):
                covered = False
                missing = list(dict.fromkeys([*missing, '原文内容']))
            coverage = 'covered' if covered else ('partial' if materials() else 'missing')
            if covered and not evidence.conflicts:
                stop_reason = 'coverage_met'
                break
            if round_index + 1 >= max_rounds:
                stop_reason = 'budget_exhausted'
                break
            if not material and not rows and round_index > 0:
                stop_reason = 'no_new_information'
                break
            # Retry only the missing/conflicting point, never for an extra domain.
            point = missing[0] if missing else (evidence.conflicts[0].topic if evidence.conflicts else '资料')
            await stage('conflict' if evidence.conflicts else 'redirect')
            query = followup or (queries[round_index + 1] if round_index + 1 < len(queries) else f'{queries[0] if queries else question} {point} 官方')

        identifiers = {s.source_id: f'source-{index + 1}' for index, s in enumerate(evidence.sources)}
        sources = tuple({**s.to_dict(), 'source_id': identifiers[s.source_id]} for s in evidence.sources if s.relevance >= .15)
        conflicts = tuple({**c.to_dict(), 'source_ids': [identifiers.get(s, s) for s in c.source_ids]} for c in evidence.conflicts)
        final_materials = materials()
        if organized_ok and assessment and final_materials:
            organized, uncovered = organize(final_materials, assessment)
            if not organized:
                # 整理器跑成了但一条事实都没落地（引用不存在 / 输出为空）：
                # 按"整理失败"处理，而不是把材料冒充成已确认事实。
                organized_ok = False
                uncovered = [str(x)[:160] for x in (assessment.get('uncovered') or []) if isinstance(x, str)][:5]
        else:
            organized_ok = False
        return EvidenceBundle(
            query=question, intent=intent.intent, claims=tuple(final_materials),
            sources=sources,
            duplicates=tuple(s.to_dict() for s in evidence.duplicates),
            conflicts=conflicts, reads=tuple(reads), failures=tuple(failures),
            searches_used=searches, reads_used=reads_used, stop_reason=stop_reason,
            coverage=coverage, unconfirmed_points=tuple(missing),
            facts=tuple(organized), materials=tuple(final_materials),
            truncated_materials=tuple(
                str(item.get('source_id') or '') for item in final_materials if item.get('truncated')
            ),
            conditions=tuple(
                str(fact.get('conditions') or '') for fact in organized if str(fact.get('conditions') or '').strip()
            )[:5],
            uncovered=tuple(uncovered),
            materials_unverified=not (organized_ok and organized),
        )
