"""统一证据集：来源独立性、去重、冲突判定、停止条件与观测指标。

这些是"多来源交叉验证"的判定核心，必须能被单测直接钉住 —— 造几条结果就能验，
不用跑真实搜索。对应目标计划的验收点：
* 两个来源内容重复时按一个独立证据处理；
* 来源冲突时明确标注（回答阶段据此并列说明，不选边）；
* 停止条件是覆盖达标 / 预算用尽 / 连续没有新增独立信息。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from agent_core.search_evidence import (
    DEFAULT_TARGET_SOURCES,
    EvidenceSet,
    build_evidence_from_digest,
    build_evidence_set,
    describe_evidence,
    domain_of,
    estimate_relevance,
    freshness_bucket,
    load_gate_events,
    summarize_metrics,
)


def _row(url: str, title: str = "标题", snippet: str = "摘要", **extra) -> dict:
    return {"url": url, "title": title, "snippet": snippet, "platform": "web", **extra}


# ── 基础解析 ──────────────────────────────────────────────────────────
def test_domain_of_normalizes_and_rejects_junk():
    assert domain_of("https://www.Example.com/a/b?x=1") == "example.com"
    assert domain_of("http://news.sina.com.cn/x") == "news.sina.com.cn"
    assert domain_of("") == ""
    assert domain_of("not a url") == ""


def test_relevance_uses_query_terms():
    high = estimate_relevance("RTX 5090 价格", "RTX 5090 国行价格公布", "起售价公布")
    low = estimate_relevance("RTX 5090 价格", "春季养花指南", "浇水技巧")
    assert high > low
    assert 0.0 <= low <= 1.0


def test_freshness_bucket_separates_unknown_from_stale():
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert freshness_bucket("", now=now) == "unknown"
    assert freshness_bucket("2026-10-07", now=now) == "fresh"
    assert freshness_bucket("2026-10-01", now=now) == "recent"
    assert freshness_bucket("2024-01-01", now=now) == "stale"


# ── 来源独立性 ────────────────────────────────────────────────────────
def test_same_domain_multiple_pages_count_as_one_source():
    evidence = build_evidence_set("某显卡 价格", [
        _row("https://example.com/a", "某显卡价格 A"),
        _row("https://example.com/b", "某显卡价格 B"),
    ])
    assert len(evidence.sources) == 2
    assert evidence.independent_domains == ["example.com"]
    assert len(evidence.duplicates) == 1


def test_different_domains_count_as_independent_sources():
    evidence = build_evidence_set("某显卡 价格", [
        _row("https://a.example.com/x", "某显卡售价 4999 元"),
        _row("https://b.example.com/y", "某显卡售价公布"),
    ])
    assert len(evidence.independent_domains) == 2
    assert evidence.coverage_satisfied() is True


def test_reprinted_article_across_domains_is_one_evidence():
    """转载判定：不同域名、同一篇稿件 → 只算一个独立来源。"""
    body = (
        "某显卡今日正式开售，官方公布的国行起售价为四千九百九十九元，"
        "首批备货数量有限，感兴趣的用户可以关注各大电商平台的预售页面。"
    )
    evidence = build_evidence_set("某显卡 开售", [
        _row("https://news-a.example.com/1", body, body),
        _row("https://news-b.example.com/2", body, body),
    ])
    assert len(evidence.independent_domains) == 1, "同一篇稿件被两个站点转载不算两个来源"
    assert evidence.duplicates and evidence.duplicates[0].duplicate_of


def test_short_similar_titles_are_not_treated_as_reprints():
    """短标题词少，重合度高不代表转载，不能误判成同一来源。"""
    evidence = build_evidence_set("某显卡 售价", [
        _row("https://a.example.com/x", "某显卡 售价", "某显卡售价 4999 元"),
        _row("https://b.example.com/y", "某显卡 报价", "某显卡报价 5199 元"),
    ])
    assert len(evidence.independent_domains) == 2


def test_url_without_host_is_not_independent():
    evidence = build_evidence_set("x", [_row("https:///no-host", "无主机名")])
    assert evidence.independent_domains == []
    assert evidence.sources[0].independent is False


# ── 冲突 ──────────────────────────────────────────────────────────────
def test_numeric_conflict_between_independent_sources_is_flagged():
    evidence = EvidenceSet(query="某显卡 售价")
    evidence.add(title="某显卡售价 4999 元 开售", url="https://a.example.com/1",
                 snippet="某显卡售价 4999 元，现已开售。")
    evidence.add(title="某显卡售价 5199 元 开售", url="https://b.example.com/2",
                 snippet="某显卡售价 5199 元，现已开售。")
    conflicts = evidence.detect_conflicts()
    assert conflicts, "两个独立来源给出不同价格必须报冲突"
    assert any("4999" in conflict.detail and "5199" in conflict.detail for conflict in conflicts)
    assert any(source.conflict for source in evidence.sources)


def test_same_source_self_inconsistency_is_not_a_source_conflict():
    """同一家站点自己前后不一致，不算"来源冲突"（那是它自己的问题）。"""
    evidence = EvidenceSet(query="x")
    evidence.add(title="售价 4999 元", url="https://a.example.com/1", snippet="售价 4999 元。")
    evidence.add(title="售价 5199 元", url="https://a.example.com/2", snippet="售价 5199 元。")
    assert evidence.detect_conflicts() == []


def test_polarity_conflict_is_flagged():
    evidence = EvidenceSet(query="某新机 发布")
    evidence.add(title="某新机已发布", url="https://a.example.com/1", snippet="官方确认已发布。")
    evidence.add(title="某新机未发布", url="https://b.example.com/2", snippet="目前未发布。")
    conflicts = evidence.detect_conflicts()
    assert conflicts, "一边说已发布、一边说未发布必须报冲突"


def test_no_conflict_when_sources_agree():
    evidence = build_evidence_set("某显卡 售价", [
        _row("https://a.example.com/x", "某显卡售价 4999 元", "售价 4999 元。"),
        _row("https://b.example.com/y", "某显卡售价 4999 元", "售价 4999 元。"),
    ])
    assert evidence.conflicts == []


# ── 停止条件 ──────────────────────────────────────────────────────────
def test_stop_after_coverage_met():
    evidence = EvidenceSet(query="x", target_sources=DEFAULT_TARGET_SOURCES)
    evidence.add(title="A", url="https://a.example.com/1")
    evidence.note_search(gained=1, budget_left=2)
    assert evidence.stop_reason(budget_left=2) == ""  # 只有一个来源，继续
    evidence.add(title="B", url="https://b.example.com/2")
    evidence.note_search(gained=1, budget_left=1)
    assert evidence.stop_reason(budget_left=1) == "coverage_met"


def test_stop_when_budget_exhausted():
    evidence = EvidenceSet(query="x")
    evidence.add(title="A", url="https://a.example.com/1")
    evidence.note_search(gained=1, budget_left=0)
    assert evidence.stop_reason(budget_left=0) == "budget_exhausted"


def test_stop_when_no_new_independent_source():
    evidence = EvidenceSet(query="x")
    evidence.add(title="A", url="https://a.example.com/1")
    evidence.note_search(gained=1, budget_left=2)
    evidence.add(title="A2", url="https://a.example.com/2")
    evidence.note_search(gained=0, budget_left=1)
    assert evidence.stop_reason(budget_left=1) == "no_new_independent_source"


def test_empty_result_is_not_counted_as_a_barren_source_loop():
    """零结果说明关键词没命中，换个词往往有用：第一次空结果不该直接停。"""
    evidence = EvidenceSet(query="x")
    evidence.note_search(gained=0, budget_left=3, empty=True)
    assert evidence.stop_reason(budget_left=3) == ""
    evidence.note_search(gained=0, budget_left=2, empty=True)
    assert evidence.stop_reason(budget_left=2) == "empty_results"


def test_extra_platform_requirement_keeps_searching_for_bilibili():
    """热梗问题只在网页侧凑够两个来源还不够：还要一个别的平台（B站）。"""
    evidence = EvidenceSet(query="某梗", target_sources=1)
    evidence.add(title="A", url="https://a.example.com/1", platform="web:bocha")
    evidence.add(title="B", url="https://b.example.com/2", platform="web:bing")
    assert set(evidence.platforms) == {"web:bocha", "web:bing"}
    # 来源数达标，但这两个平台在开搜之前就有：还要继续找一个别的平台（B站）
    assert evidence.stop_reason(
        budget_left=1, prior_platforms={"web:bocha", "web:bing"}, want_extra_platform=True,
    ) == ""
    evidence.add(title="C", url="https://www.bilibili.com/video/BV1", platform="bilibili")
    assert evidence.stop_reason(
        budget_left=0, prior_platforms={"web:bocha", "web:bing"}, want_extra_platform=True,
    ) == "coverage_met"


# ── 摘要还原与表述 ────────────────────────────────────────────────────
def test_digest_roundtrip_keeps_independence_flags():
    """模型自己搜过的路径靠摘要还原，判定口径必须和门代查一致。"""
    evidence = build_evidence_set("x", [
        _row("https://a.example.com/1", "A"),
        _row("https://a.example.com/2", "B"),
    ])
    digest = evidence.to_digest()
    digest["searches"] = [{"query": "x", "results": digest["sources"]}]
    restored = build_evidence_from_digest(digest)
    assert len(restored.independent_domains) == 1
    assert len(restored.sources) == 2
    assert restored.searches == evidence.searches


def test_describe_evidence_reports_single_source_and_no_source():
    one = build_evidence_set("x", [_row("https://a.example.com/1", "A")])
    described = describe_evidence(one)
    assert described["independent_sources"] == "1"
    assert described["single_source"] == "true"
    assert "单一来源" in described["unconfirmed"]

    none = EvidenceSet(query="x")
    described_none = describe_evidence(none)
    assert described_none["independent_sources"] == "0"
    assert "无法确认" in described_none["limitation"]


def test_describe_evidence_accepts_digest_dict():
    digest = build_evidence_set("x", [
        _row("https://a.example.com/1", "A"),
        _row("https://b.example.com/2", "B"),
    ]).to_digest()
    described = describe_evidence(digest)
    assert described["independent_sources"] == "2"
    assert described["single_source"] == "false"


# ── 观测指标 ──────────────────────────────────────────────────────────
def test_summarize_metrics_aggregates_turn_events():
    events = [
        {"event": "intent", "turn_id": "t1", "intent": "fact_search"},
        {"event": "search", "turn_id": "t1"},
        {"event": "search", "turn_id": "t1"},
        {"event": "evidence_coverage", "turn_id": "t1", "independent_source_count": 2,
         "conflict": False, "single_source": False, "searches_used": 2},
        {"event": "answer", "turn_id": "t1", "independent_source_count": 2,
         "single_source": False, "conflict": False, "searches": 2},
        {"event": "evidence_coverage", "turn_id": "t2", "independent_source_count": 1,
         "conflict": True, "single_source": True, "searches_used": 1},
        {"event": "answer", "turn_id": "t2", "independent_source_count": 1,
         "single_source": True, "conflict": True, "searches": 1},
    ]
    metrics = summarize_metrics(events)
    assert metrics["turns"] == 2
    assert metrics["answered_turns"] == 2
    assert metrics["searches"] == 2  # 门代查次数
    assert metrics["avg_attempts_per_turn"] == 1.5
    assert metrics["avg_independent_sources"] == 1.5
    assert metrics["single_source_rate"] == 0.5
    assert metrics["multi_attempt_rate"] == 0.5
    assert metrics["conflict_rate"] == 0.5


def test_summarize_metrics_counts_model_side_searches():
    """模型自己搜的也算"尝试了几个来源"，否则多来源率会被算成 0。"""
    events = [
        {"event": "evidence_coverage", "turn_id": "t1", "independent_source_count": 7,
         "single_source": False, "conflict": False, "searches_used": 2, "model_searches": 2},
        {"event": "answer", "turn_id": "t1", "independent_source_count": 7,
         "single_source": False, "conflict": False, "searches": 2},
    ]
    metrics = summarize_metrics(events)
    assert metrics["avg_independent_sources"] == 7.0
    assert metrics["avg_attempts_per_turn"] == 2.0
    assert metrics["multi_attempt_rate"] == 1.0


def test_summarize_metrics_on_empty_events():
    metrics = summarize_metrics([])
    assert metrics["turns"] == 0
    assert metrics["avg_independent_sources"] == 0.0
    assert metrics["single_source_rate"] == 0.0
    assert metrics["multi_attempt_rate"] == 0.0


def test_load_gate_events_reads_log_lines(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(
        "2026-10-08 10:00:00 INFO agent_core.loop search_gate "
        + json.dumps({"event": "search", "turn_id": "t1", "query": "甲"}, ensure_ascii=False)
        + "\n不是搜索门日志的一行\n",
        encoding="utf-8",
    )
    events = load_gate_events(log)
    assert events == [{"event": "search", "turn_id": "t1", "query": "甲"}]
    assert load_gate_events(tmp_path / "missing.log") == []


# ── 配置 ──────────────────────────────────────────────────────────────
def test_target_sources_config_from_env(monkeypatch, tmp_path):
    from agent_core.search_gate import SearchGateConfig

    env = tmp_path / ".env"
    env.write_text("SEARCH_GATE_TARGET_SOURCES=3\n", encoding="utf-8")
    monkeypatch.delenv("SEARCH_GATE_TARGET_SOURCES", raising=False)
    config = SearchGateConfig.from_env(env)
    assert config.target_sources == 3
    monkeypatch.setenv("SEARCH_GATE_TARGET_SOURCES", "999")
    assert SearchGateConfig.from_env(env).target_sources == 4  # 上限夹住


def test_timestamps_in_rows_feed_freshness():
    """发布时间要落进 fresh/unknown 两档。

    日期用**相对当前时刻**算，不写死某一天 —— 写死的日期过两天就会从 fresh
    变成 recent，测试跟着时间失效（独立验收点出的问题）。
    """
    from datetime import datetime, timedelta, timezone

    fresh_day = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d")
    evidence = build_evidence_set("今天的新闻", [
        _row("https://a.example.com/1", "甲公司发布新品的消息", "内容", published_at=fresh_day),
        _row("https://b.example.com/2", "乙媒体报道同一场发布会", "内容"),
    ])
    buckets = {source.freshness for source in evidence.sources}
    assert "fresh" in buckets and "unknown" in buckets
    described = describe_evidence(evidence)
    assert "没有发布时间" in described["limitation"]
