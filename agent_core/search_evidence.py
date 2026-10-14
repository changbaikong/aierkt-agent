"""统一证据集：把检索结果整理成"可交叉验证"的来源集合。

【它解决什么】
搜索能返回一堆链接，不等于"有多个来源"。三种假象最常见：

1. 同一篇文章被多个站点转载 —— 看着三条结果，其实只有一个信息源；
2. 同一家站点（或同一平台）的不同页面 —— 域名不独立，互相不能算佐证；
3. 一条说"已发布"、另一条说"未发布" —— 有多来源，但说法冲突，不能挑一边。

本模块只做纯逻辑（不联网、不调模型），把检索结果变成 ``EvidenceSet``：
每条来源带**来源标识 / 域名 / 平台 / 发布时间 / 相关性 / 是否独立 / 冲突状态**，
并回答三个问题：覆盖够不够、要不要继续搜、证据该怎么如实表述。

【为什么单独成模块】
判定逻辑必须能被单测直接钉住（造几条结果就能验），而不是埋在搜索循环里
靠"跑一遍真实搜索看看"来间接验证。搜索循环只负责调用它。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

__all__ = [
    "EvidenceSource",
    "EvidenceConflict",
    "EvidenceSet",
    "EvidenceStopDecision",
    "build_evidence_set",
    "domain_of",
    "estimate_relevance",
    "freshness_bucket",
    "describe_evidence",
    "summarize_metrics",
]

# ── 覆盖口径 ────────────────────────────────────────────────────────
#: 普通事实问题优先观察不同域名的独立来源，但来源数量不是强制停止条件；
#: 一个直接、可信的来源已经足够回答简单问题。
DEFAULT_TARGET_SOURCES = 2

#: 摘要相似到这个比例就按"同一篇内容"处理（转载判定的阈值）。
#: 取高值：中文标题词少，"某显卡 售价"和"某显卡 报价"的 Jaccard 也能到 0.6 以上，
#: 阈值低了会把**不同媒体对同一件事的独立报道**误判成转载。
DUPLICATE_SIMILARITY = 0.92

#: 太短的文本不做相似度判定（几个字的重合说明不了任何问题）。
MIN_SIMILARITY_LENGTH = 12

#: 容忍几次"没有新增独立来源"的检索。取 1：换一个关键词没带来新站点，
#: 再换一个还是同一批站点就说明白搜了 —— 于是最多两次空手就停。
MAX_BARREN_SEARCHES = 1

#: 连续多少次"零结果"就放弃换词。空结果说明这个关键词没命中（换个词往往有用），
#: 所以第一次不算白搜；连续两次零结果才停。
MAX_EMPTY_SEARCHES = 2

_TAG_RE = re.compile(r"<[^>]+>")
_PUNCT_RE = re.compile(r"[\s\u3000,，。.、；;：:！!？?“”\"'（）()\[\]【】<>《》\-—_/\\|~`*#]+")
_NUMBER_RE = re.compile(
    r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>元|块|万|亿|美元|人民币|%|％|折|人|名|个|次|条|岁|年|月|日|天|km|公里|克|千克|公斤)?"
)
_YEAR_RE = re.compile(r"(19|20)\d{2}\s*年?")
_NEGATIONS = ("尚未", "未", "没有", "不再", "不能", "不会", "禁止", "取消", "否认", "停止")
_STOPWORDS = {"最新", "现在", "多少", "什么", "怎么", "哪个", "哪些", "是否", "以及", "请问", "一下"}


# ---------------------------------------------------------------------------
# 基础解析
# ---------------------------------------------------------------------------
def domain_of(url: str) -> str:
    """取域名（小写、去 www）。解析失败返回空串 —— 空域名永远不会被算作独立来源。"""
    try:
        host = urlparse(str(url or "").strip()).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _clean_text(value: Any) -> str:
    return _TAG_RE.sub(" ", str(value or "")).replace("\u3000", " ").strip()


def _cjk_bigrams(text: str) -> list[str]:
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    return [chars[index] + chars[index + 1] for index in range(len(chars) - 1)]


def _terms(text: str) -> set[str]:
    """粗分词：英文/数字按词，中文按双字。够用且零依赖，不引第三方分词器。"""
    lowered = str(text or "").lower()
    latin = re.findall(r"[a-z0-9][a-z0-9._+-]*", lowered)
    words = {word for word in latin if len(word) > 1}
    words.update(_cjk_bigrams(lowered))
    return words - _STOPWORDS


def _similarity(left: str, right: str) -> float:
    """标题/摘要相似度：词集合的 Jaccard 系数（0~1）。"""
    left_terms, right_terms = _terms(left), _terms(right)
    if not left_terms or not right_terms:
        return 0.0
    union = left_terms | right_terms
    return len(left_terms & right_terms) / len(union) if union else 0.0


def estimate_relevance(query: str, title: str, snippet: str = "", summary: str = "") -> float:
    """结果与本次检索词的相关性（0~1）。

    只看检索词在标题/摘要里覆盖了多少 —— 这是"这条结果到底是不是在讲我要问的事"
    的最低成本近似；完全不相关的结果不能拿来当多来源计数。
    """
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    haystack = f"{title} {snippet} {summary}"
    hit = query_terms & _terms(haystack)
    return round(len(hit) / len(query_terms), 3)


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y年%m月%d日",
        "%Y年%m月%d日 %H:%M",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def freshness_bucket(published_at: str, *, now: datetime | None = None) -> str:
    """把发布时间归到 ``fresh`` / ``recent`` / ``stale`` / ``unknown`` 四档。

    ``unknown`` 不等于"过时"，只是没有时间证据 —— 表述时必须区分这两件事。
    """
    parsed = _parse_datetime(published_at)
    if parsed is None:
        return "unknown"
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    age = moment - parsed
    if age < timedelta(0):
        return "fresh"
    if age <= timedelta(days=2):
        return "fresh"
    if age <= timedelta(days=45):
        return "recent"
    return "stale"


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class EvidenceSource:
    """一条来源。``independent=False`` 表示它没能提供新的独立信息。"""

    source_id: str
    title: str = ""
    url: str = ""
    domain: str = ""
    platform: str = ""
    published_at: str = ""
    snippet: str = ""
    retrieval_query: str = ""
    relevance: float = 0.0
    freshness: str = "unknown"
    independent: bool = True
    duplicate_of: str = ""
    conflict: bool = False
    conflict_note: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "domain": self.domain,
            "platform": self.platform,
            "published_at": self.published_at,
            "snippet": self.snippet[:200],
            "retrieval_query": self.retrieval_query,
            "relevance": self.relevance,
            "freshness": self.freshness,
            "independent": self.independent,
            "duplicate_of": self.duplicate_of,
            "conflict": self.conflict,
            "conflict_note": self.conflict_note,
            "note": self.note,
        }


@dataclass(frozen=True)
class EvidenceConflict:
    """两条独立来源对同一件事给出不同说法。"""

    topic: str
    detail: str
    source_ids: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "detail": self.detail,
            "source_ids": list(self.source_ids),
            "domains": list(self.domains),
        }


@dataclass(frozen=True)
class EvidenceStopDecision:
    """还要不要继续搜。"""

    stop: bool
    reason: str
    target_sources: int
    independent_sources: int
    gained: int = 0
    barren_searches: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stop": self.stop,
            "reason": self.reason,
            "target_sources": self.target_sources,
            "independent_sources": self.independent_sources,
            "gained": self.gained,
            "barren_searches": self.barren_searches,
        }


@dataclass
class EvidenceSet:
    """本轮检索证据集合。所有"够不够、要不要再搜、怎么表述"的判断都走这里。"""

    query: str = ""
    target_sources: int = DEFAULT_TARGET_SOURCES
    intent: str = ""
    sources: list[EvidenceSource] = field(default_factory=list)
    conflicts: list[EvidenceConflict] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)
    searches: int = 0
    barren_searches: int = 0
    empty_searches: int = 0
    _urls: set[str] = field(default_factory=set, repr=False)
    _titles: set[str] = field(default_factory=set, repr=False)
    _domains: set[str] = field(default_factory=set, repr=False)

    # ── 收集 ────────────────────────────────────────────────────────
    def add(
        self,
        *,
        title: str = "",
        url: str = "",
        snippet: str = "",
        summary: str = "",
        platform: str = "",
        published_at: str = "",
        retrieval_query: str = "",
        relevance_query: str = "",
        independent: bool | None = None,
        duplicate_of: str = "",
        note: str = "",
    ) -> EvidenceSource:
        """加入一条结果，并当场判定它是新独立来源、转载复制，还是同一家站点的重复。

        ``independent`` / ``duplicate_of`` / ``note`` 显式给出时以传入值为准：
        从已有证据摘要还原（模型自己搜过的路径）时要沿用当时的判定，不能重算 ——
        重算会把"同一家站点的第二个页面"又算成一个来源。
        """
        clean_url = str(url or "").strip()
        clean_title = _clean_text(title)
        domain = domain_of(clean_url)
        query = retrieval_query or relevance_query or self.query
        relevance = estimate_relevance(query or self.query, clean_title, snippet, summary)
        url_key = clean_url.rstrip("/").lower()
        title_key = _PUNCT_RE.sub("", clean_title).lower()[:60]

        if independent is None:
            independent = True
            if url_key and url_key in self._urls:
                independent, duplicate_of, note = False, url_key, "同一链接重复返回"
            elif title_key and title_key in self._titles:
                independent, duplicate_of, note = False, title_key, "标题完全相同的重复结果"
            elif not domain:
                independent, note = False, "无法解析域名，不能算独立来源"
            else:
                for existing in self.sources:
                    if not existing.independent:
                        continue
                    if existing.domain == domain:
                        independent, duplicate_of = False, existing.source_id
                        note = f"与 {existing.domain} 的其他页面同源（同一家站点）"
                        break
                    similarity = max(
                        _similarity(clean_title, existing.title)
                        if min(len(clean_title), len(existing.title)) >= MIN_SIMILARITY_LENGTH else 0.0,
                        _similarity(snippet or summary, existing.snippet)
                        if min(len(snippet or summary), len(existing.snippet)) >= MIN_SIMILARITY_LENGTH else 0.0,
                    )
                    if similarity >= DUPLICATE_SIMILARITY:
                        independent, duplicate_of = False, existing.source_id
                        note = f"内容与 {existing.domain} 的结果高度重复（转载/同一稿件）"
                        break

        source = EvidenceSource(
            source_id=url_key or f"{domain}#{len(self.sources)}",
            title=clean_title,
            url=clean_url,
            domain=domain,
            platform=str(platform or ""),
            published_at=str(published_at or "").strip(),
            snippet=_clean_text(snippet or summary),
            retrieval_query=query,
            relevance=relevance,
            freshness=freshness_bucket(published_at),
            independent=independent,
            duplicate_of=duplicate_of,
            note=note,
        )
        if url_key:
            self._urls.add(url_key)
        if title_key:
            self._titles.add(title_key)
        if domain:
            self._domains.add(domain)
        self.sources.append(source)
        return source

    def add_failure(self, tool: str, reason: str, *, query: str = "", url: str = "") -> None:
        self.failures.append({"tool": tool, "query": query, "url": url, "reason": str(reason or "")[:200]})

    # ── 视图 ────────────────────────────────────────────────────────
    @property
    def independent_sources(self) -> list[EvidenceSource]:
        return [source for source in self.sources if source.independent]

    @property
    def independent_domains(self) -> list[str]:
        seen: list[str] = []
        for source in self.independent_sources:
            if source.domain and source.domain not in seen:
                seen.append(source.domain)
        return seen

    @property
    def duplicates(self) -> list[EvidenceSource]:
        return [source for source in self.sources if not source.independent]

    @property
    def platforms(self) -> list[str]:
        """独立来源覆盖到的平台（bocha/bing/bilibili…），用于"多平台"覆盖判断。"""
        seen: list[str] = []
        for source in self.independent_sources:
            if source.platform and source.platform not in seen:
                seen.append(source.platform)
        return seen

    @property
    def known(self) -> bool:
        """有没有任何可用证据（哪怕是重复来源）。"""
        return bool(self.sources)

    def urls(self, *, limit: int = 3) -> list[str]:
        found: list[str] = []
        for source in self.sources:
            if source.url.startswith("http") and source.url not in found:
                found.append(source.url)
            if len(found) >= limit:
                break
        return found

    def coverage_satisfied(self) -> bool:
        """是否已有至少一个可用独立来源。来源数量不再驱动无意义补搜。"""
        return len(self.independent_domains) >= max(1, int(self.target_sources))

    # ── 冲突 ────────────────────────────────────────────────────────
    def detect_conflicts(self) -> list[EvidenceConflict]:
        """找出独立来源之间的数值/正反说法冲突（规则判定，保守：宁缺勿错报）。"""
        found: list[EvidenceConflict] = []
        found.extend(self._numeric_conflicts())
        found.extend(self._polarity_conflicts())
        deduped: list[EvidenceConflict] = []
        seen: set[tuple[str, str]] = set()
        for conflict in found:
            key = (conflict.topic, conflict.detail)
            if key not in seen:
                seen.add(key)
                deduped.append(conflict)
        self.conflicts = deduped
        if deduped:
            flagged: dict[str, str] = {}
            for conflict in deduped:
                for source_id in conflict.source_ids:
                    flagged.setdefault(source_id, conflict.detail)
            for index, source in enumerate(self.sources):
                if source.source_id in flagged:
                    self.sources[index] = replace(
                        source, conflict=True, conflict_note=flagged[source.source_id],
                    )
        return deduped

    def _numeric_conflicts(self) -> list[EvidenceConflict]:
        # metric → {value → [(source, sentence)]}，只在**不同域名**之间比较
        buckets: dict[str, dict[str, list[tuple[EvidenceSource, str]]]] = {}
        for source in self.independent_sources:
            for sentence in _split_sentences(f"{source.title}。{source.snippet}"):
                for metric, number in _metric_numbers(sentence):
                    if not metric or len(number) < 2:
                        continue
                    buckets.setdefault(metric, {}).setdefault(number, []).append((source, sentence))
        conflicts: list[EvidenceConflict] = []
        for metric, values in buckets.items():
            if len(values) < 2:
                continue
            groups = list(values.items())
            left_value, left_hits = groups[0]
            right_value, right_hits = groups[1]
            left_domains = {hit[0].domain for hit in left_hits}
            right_domains = {hit[0].domain for hit in right_hits}
            if not left_domains or left_domains & right_domains:
                continue  # 同一来源自相矛盾不算"来源冲突"，交给模型表述
            conflicts.append(EvidenceConflict(
                topic=metric,
                detail=f"「{metric}」出现不同数字：{left_value}（{', '.join(sorted(left_domains))}）"
                       f" vs {right_value}（{', '.join(sorted(right_domains))}）",
                source_ids=tuple(hit[0].source_id for hit in left_hits[:1] + right_hits[:1]),
                domains=tuple(sorted(left_domains | right_domains)),
            ))
        return conflicts

    def _polarity_conflicts(self) -> list[EvidenceConflict]:
        """同一主语上"已/未"正反说法并列：例如一边说已发布，一边说未发布。"""
        patterns = (
            ("发布", "已发布", "未发布"),
            ("上市", "已上市", "未上市"),
            ("确认", "已确认", "未确认"),
            ("开售", "已开售", "未开售"),
            ("回应", "已回应", "未回应"),
        )
        conflicts: list[EvidenceConflict] = []
        for topic, positive, negative in patterns:
            positive_hits = [source for source in self.independent_sources if positive in _source_text(source)]
            negative_hits = [
                source for source in self.independent_sources
                if negative in _source_text(source) or any(
                    f"{negation}{positive}" in _source_text(source) for negation in _NEGATIONS
                )
            ]
            positive_domains = {source.domain for source in positive_hits}
            negative_domains = {source.domain for source in negative_hits}
            if positive_domains and negative_domains and not positive_domains & negative_domains:
                conflicts.append(EvidenceConflict(
                    topic=topic,
                    detail=f"关于「{topic}」说法冲突：{', '.join(sorted(positive_domains))} 说「{positive}」，"
                           f"{', '.join(sorted(negative_domains))} 说「{negative}」",
                    source_ids=tuple(
                        source.source_id for source in (positive_hits[:1] + negative_hits[:1])
                    ),
                    domains=tuple(sorted(positive_domains | negative_domains)),
                ))
        return conflicts

    # ── 停止条件 ────────────────────────────────────────────────────
    def note_search(self, *, gained: int, budget_left: int, empty: bool = False) -> EvidenceStopDecision:
        """记录一次检索带来的新增独立来源数，并给出"还要不要继续搜"。

        停止条件（目标计划）：
        1. 证据覆盖达标 —— 独立来源数够了；
        2. 预算用尽；
        3. 连续检索没有新增独立信息（换词也只是同一批站点）。

        ``empty``：本次零结果。零结果说明关键词没命中，换个词往往就有，
        所以它不按"同一个来源打转"计数，而是单独记连续空结果次数。
        """
        self.searches += 1
        if empty:
            self.empty_searches += 1
        else:
            self.empty_searches = 0
            self.barren_searches = 0 if gained > 0 else self.barren_searches + 1
        return self.stop_decision(budget_left=budget_left)

    def stop_reason(
        self,
        *,
        budget_left: int,
        prior_platforms: set[str] | None = None,
        want_extra_platform: bool = False,
    ) -> str:
        """覆盖是否达标；返回空串表示**还要继续搜**。

        调用契约：停止条件是"覆盖达标 / 预算用尽 / 连续没有新增独立信息"，
        所以只有这三种情况返回非空；"来源还不够但还有预算"必须继续换关键词。

        默认口径只看独立来源数；``want_extra_platform``（热梗类问题要 B站）时
        再多要一个本次新增的平台 —— 只有网页来源就停手，会漏掉视频平台的解释。
        """
        if not self.coverage_satisfied():
            if budget_left <= 0:
                return "budget_exhausted"
            # 本平台的第一次"空手而归"不叫重复劳动：很多问题就是第二个关键词才命中。
            if self.empty_searches >= MAX_EMPTY_SEARCHES:
                return "empty_results"
            if self.barren_searches >= MAX_BARREN_SEARCHES:
                return "no_new_independent_source"
            return ""  # 覆盖还不够：继续换关键词找独立来源
        if want_extra_platform:
            known = set(prior_platforms or ())
            if not (set(self.platforms) - known):
                if budget_left <= 0:
                    return "budget_exhausted"
                if self.barren_searches >= MAX_BARREN_SEARCHES:
                    return "no_new_independent_source"
                return ""  # 来源够了但平台还单一：继续（下一个计划项就是 B站）
        return "coverage_met"

    def stop_decision(self, *, budget_left: int) -> EvidenceStopDecision:
        if self.coverage_satisfied():
            return EvidenceStopDecision(
                stop=True, reason="coverage_met", target_sources=self.target_sources,
                independent_sources=len(self.independent_domains), barren_searches=self.barren_searches,
            )
        if budget_left <= 0:
            return EvidenceStopDecision(
                stop=True, reason="budget_exhausted", target_sources=self.target_sources,
                independent_sources=len(self.independent_domains), barren_searches=self.barren_searches,
            )
        if self.barren_searches > MAX_BARREN_SEARCHES:
            return EvidenceStopDecision(
                stop=True, reason="no_new_independent_source", target_sources=self.target_sources,
                independent_sources=len(self.independent_domains), barren_searches=self.barren_searches,
            )
        return EvidenceStopDecision(
            stop=False, reason="need_more_sources", target_sources=self.target_sources,
            independent_sources=len(self.independent_domains), barren_searches=self.barren_searches,
        )

    # ── 序列化 ──────────────────────────────────────────────────────
    def to_digest(self) -> dict[str, Any]:
        """给重写提示词与回答前检查用的证据摘要（结构稳定，模型可读）。"""
        return {
            "query": self.query,
            "intent": self.intent,
            "target_sources": self.target_sources,
            "independent_source_count": len(self.independent_domains),
            "independent_domains": self.independent_domains,
            "coverage_satisfied": self.coverage_satisfied(),
            "searches": self.searches,
            "conflicts": [conflict.to_dict() for conflict in self.conflicts],
            "sources": [source.to_dict() for source in self.sources],
            "duplicates": [
                {"source_id": source.source_id, "duplicate_of": source.duplicate_of, "note": source.note}
                for source in self.duplicates
            ],
            "failures": self.failures,
        }


def _split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。；;！!\n]", str(text or "")) if part.strip()]


def _metric_numbers(sentence: str) -> list[tuple[str, str]]:
    """从一句话里抠出「指标 → 数值」对，用于跨来源比较同一件事的数字。"""
    pairs: list[tuple[str, str]] = []
    for match in _NUMBER_RE.finditer(sentence):
        number = match.group("number")
        unit = match.group("unit") or ""
        # 指标名取数字前面的一小段（去掉标点），"售价 4999 元" → 指标"售价 元"
        prefix = _PUNCT_RE.sub("", sentence[: match.start()])[-12:]
        # 产品名和修饰词会让同一价格在不同标题中得到不同前缀；
        # 统一保留明确的价格指标，才能识别跨站点报价冲突。
        metric_match = re.search(r"(售价|价格|报价|费用|成本|金额|折扣)", prefix)
        if metric_match:
            prefix = metric_match.group(1)
        metric = f"{prefix}{unit}" if unit and prefix else (prefix or unit)
        if metric:
            pairs.append((metric, f"{number}{unit}"))
    return pairs


def _source_text(source: EvidenceSource) -> str:
    return f"{source.title} {source.snippet}"


# ---------------------------------------------------------------------------
# 组装：从检索回执造证据集
# ---------------------------------------------------------------------------
def build_evidence_set(
    query: str,
    results: Iterable[dict[str, Any]],
    *,
    retrieval_query: str = "",
    target_sources: int = DEFAULT_TARGET_SOURCES,
) -> EvidenceSet:
    """从一批结果行造证据集（单测与"模型自己搜过"的路径都用它）。"""
    evidence = EvidenceSet(query=query, target_sources=target_sources)
    for row in results:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        evidence.add(
            title=str(row.get("title") or ""),
            url=url,
            snippet=str(row.get("snippet") or ""),
            summary=str(row.get("summary") or ""),
            platform=str(row.get("platform") or ""),
            published_at=str(row.get("published_at") or ""),
            retrieval_query=retrieval_query or query,
        )
    if evidence.sources:
        evidence.detect_conflicts()
    return evidence


def _as_rows(value: Any) -> list[dict[str, Any]]:
    """摘要字段防御：只接受"条目列表"，其它类型（含 EvidenceSet 摘要里的计数）忽略。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, dict)]


def build_evidence_from_digest(
    digest: dict[str, Any],
    *,
    query: str = "",
    target_sources: int = DEFAULT_TARGET_SOURCES,
) -> EvidenceSet:
    """从 ``_gate_digest_from_working`` 那种工具回执摘要还原证据集。

    模型自己搜过的路径没有 EvidenceSet 对象，只能事后还原；还原口径必须和
    门代查一致，否则"模型搜的算两个来源、门搜的算一个"会自相矛盾。
    """
    evidence = EvidenceSet(query=query, target_sources=target_sources)
    for search in _as_rows(digest.get("searches")):
        retrieval_query = str(search.get("query") or query)
        for row in _as_rows(search.get("results")):
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            evidence.add(
                title=str(row.get("title") or ""),
                url=url,
                snippet=str(row.get("snippet") or ""),
                platform=str(row.get("platform") or ""),
                published_at=str(row.get("published_at") or ""),
                retrieval_query=retrieval_query,
            )
            evidence.searches = max(evidence.searches, 1)
    # 已经是证据集的摘要（含 sources）时，直接沿用它的独立来源判定：
    # 重新按 URL 计数会把"同一家站点的第二个页面"又算成一个来源。
    prefilled = [row for row in _as_rows(digest.get("sources")) if str(row.get("url") or "").strip()]
    if prefilled:
        rebuilt = EvidenceSet(query=str(digest.get("query") or query), target_sources=target_sources)
        rebuilt.intent = str(digest.get("intent") or "")
        for row in prefilled:
            rebuilt.add(
                title=str(row.get("title") or ""),
                url=str(row.get("url") or ""),
                snippet=str(row.get("snippet") or ""),
                platform=str(row.get("platform") or ""),
                published_at=str(row.get("published_at") or ""),
                retrieval_query=str(row.get("retrieval_query") or ""),
                independent=bool(row.get("independent")) if row.get("independent") is not None else None,
                duplicate_of=str(row.get("duplicate_of") or ""),
                note=str(row.get("note") or ""),
            )
        rebuilt.searches = (
            int(digest["searches"]) if isinstance(digest.get("searches"), (int, float)) else rebuilt.searches
        )
        evidence = rebuilt
    for failure in _as_rows(digest.get("failures")):
        evidence.add_failure(
            str(failure.get("tool") or ""), str(failure.get("reason") or ""),
            query=str(failure.get("query") or ""), url=str(failure.get("url") or ""),
        )
    if evidence.sources:
        evidence.detect_conflicts()
    return evidence


# ---------------------------------------------------------------------------
# 表述与观测
# ---------------------------------------------------------------------------
def describe_evidence(
    evidence: EvidenceSet | dict[str, Any],
    *,
    query: str = "",
) -> dict[str, str]:
    """把证据状态翻成三句话：已确认什么、哪些没确认、证据限制在哪。

    目标计划要求回答不能整段含糊。这里的口径固定成三段，写进重写提示词，
    也用来生成回复末尾的来源摘要：
    * ``confirmed``    : 有独立来源支持的部分（附独立来源数）；
    * ``unconfirmed``  : 单一来源或只有摘要级证据的部分；
    * ``limitation``   : 检索失败、覆盖不足、说法冲突等限制。
    """
    if isinstance(evidence, EvidenceSet):
        pass
    elif isinstance(evidence, dict):
        evidence = build_evidence_from_digest(evidence, query=query)
    else:
        raise TypeError(f"unsupported evidence payload: {type(evidence).__name__}")
    independent = len(evidence.independent_domains)
    confirmed = "、".join(evidence.independent_domains) if independent else ""
    unconfirmed = ""
    limitation = ""
    if independent == 0:
        limitation = "没有取得可用来源，只能说明无法确认，不能说成不存在"
    elif independent == 1:
        unconfirmed = f"目前只有单一来源（{confirmed}），细节以该来源为准"
    if evidence.duplicates:
        limitation = (limitation + "；" if limitation else "") + (
            f"{len(evidence.duplicates)} 条结果属于重复转载，按同一来源计数"
        )
    if evidence.conflicts:
        topics = "、".join(conflict.topic for conflict in evidence.conflicts)
        limitation = (limitation + "；" if limitation else "") + f"来源之间在「{topics}」上说法冲突，必须并列说明"
    if evidence.failures:
        limitation = (limitation + "；" if limitation else "") + f"{len(evidence.failures)} 次检索/读取失败，如实说明"
    undated = [source for source in evidence.independent_sources if source.freshness == "unknown"]
    if undated:
        limitation = (limitation + "；" if limitation else "") + f"{len(undated)} 条来源没有发布时间，时效无法判断"
    return {
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "limitation": limitation,
        "independent_sources": str(independent),
        "single_source": "true" if independent == 1 else "false",
        "conflict": "true" if evidence.conflicts else "false",
    }


def summarize_metrics(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """把搜索门事件流聚合成观测指标（目标计划第 4 步：先记录，再调阈值）。

    说明两个口径，避免越读越糊涂：
    * ``attempts`` —— 这一回合一共发起过几次检索，**模型自己搜的也算**。
      "每回合至少两个来源"是靠这个数体现的；门代查的次数单独记在 ``gate_searches``。
    * ``multi_attempt_rate`` —— 有多少比例的回合真的尝试了不止一个来源，
      也就是"搜到一条就收工"的比例（越低越好）。

    ``avg_independent_sources`` / ``single_source_rate`` / ``conflict_rate``
    的口径完全来自证据集判定，不受模型侧调用次数影响。
    """
    turns: dict[str, dict[str, Any]] = {}
    rewrites = 0
    gate_searches = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        turn = str(event.get("turn_id") or event.get("turn") or "unknown")
        record = turns.setdefault(turn, {
            "attempts": 0, "sources": 0, "single_source": False, "conflict": False, "answered": False,
        })
        name = str(event.get("event") or "")
        if name == "search":
            gate_searches += 1
            record["attempts"] = max(record["attempts"], gate_searches)
        elif name == "rewrite":
            rewrites += 1
        elif name in {"evidence_coverage", "answer"}:
            used = int(event.get("searches_used") or 0)
            model_used = int(event.get("model_searches") or event.get("searches") or 0)
            record["attempts"] = max(record["attempts"], used, model_used)
            sources = int(event.get("independent_source_count") or 0)
            if name == "evidence_coverage" or sources or event.get("single_source"):
                record["answered"] = True
            record["sources"] = max(record["sources"], sources)
            record["conflict"] = record["conflict"] or bool(event.get("conflict"))
            record["single_source"] = record["single_source"] or bool(event.get("single_source"))
    answered = [record for record in turns.values() if record["answered"]]
    total = len(answered) or 0

    def _rate(count: int) -> float:
        return round(count / total, 2) if total else 0.0

    # 新搜索编排链路直接记录每回合摘要；同时保留旧字段，便于已有监控面板平滑迁移。
    pipeline_turns = [
        event for event in events
        if isinstance(event, dict) and ("search_trigger" in event or "direct_chat" in event)
    ]
    direct_count = sum(1 for event in pipeline_turns if event.get("direct_chat"))
    search_events = [event for event in pipeline_turns if event.get("search_trigger")]
    deep_count = sum(1 for event in search_events if event.get("deep_search"))
    fast_count = sum(1 for event in search_events if event.get("fast_search"))
    def _avg(name: str) -> float:
        values = [float(event.get(name) or 0) for event in search_events]
        return round(sum(values) / len(values), 2) if values else 0.0
    return {
        "turns": len(turns),
        "answered_turns": total,
        "searches": gate_searches,
        "searches_per_turn": round(gate_searches / total, 2) if total else 0.0,
        "avg_attempts_per_turn": round(
            sum(record["attempts"] for record in answered) / total, 2
        ) if total else 0.0,
        "avg_independent_sources": round(
            sum(record["sources"] for record in answered) / total, 2
        ) if total else 0.0,
        "single_source_rate": _rate(sum(1 for record in answered if record["single_source"])),
        "multi_attempt_rate": _rate(sum(1 for record in answered if record["attempts"] > 1)),
        "conflict_rate": _rate(sum(1 for record in answered if record["conflict"])),
        "rewrites": rewrites,
        "direct_chat_rate": round(direct_count / len(pipeline_turns), 2) if pipeline_turns else 0.0,
        "search_trigger_rate": round(len(search_events) / len(pipeline_turns), 2) if pipeline_turns else 0.0,
        "fast_search_rate": round(fast_count / len(search_events), 2) if search_events else 0.0,
        "deep_search_rate": round(deep_count / len(search_events), 2) if search_events else 0.0,
        "avg_search_rounds": _avg("search_rounds"),
        "avg_page_reads": _avg("page_reads"),
        "rewrite_rate": round(sum(1 for event in search_events if event.get("rewrite")) / len(search_events), 2) if search_events else 0.0,
        "search_failure_rate": round(sum(1 for event in search_events if event.get("search_failure")) / len(search_events), 2) if search_events else 0.0,
        "time_to_first_token": _avg("time_to_first_token"),
    }


def load_gate_events(log_path: str | Any) -> list[dict[str, Any]]:
    """从日志文件里捞出 ``search_gate {...}`` JSON 事件，供 summary 聚合。"""
    from pathlib import Path

    path = Path(log_path)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        marker = line.find("search_gate ")
        if marker < 0:
            continue
        payload = line[marker + len("search_gate "):].strip()
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
