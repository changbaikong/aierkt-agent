"""寿命规则：项目阶段约束不因「暂时」一天后失效，当天状态照旧短期。

真实批次暴露的问题：用户说「这个项目暂时先不部署，继续做本机验收」，模型把它
标成 short_term，程序就无条件给一天有效期，两天后的维护把它清掉了 —— 用户从没
说过只限今天。

这里锁三件事：
1. 阶段约束长期保留，模拟推进两天后仍然有效；
2. 当天情绪按短期政策到期；
3. 用户明说的期限（保留三天）按原话执行。
另外锁住「长期声明不被关键词覆盖」这条既有行为，防止按词推断误杀长期原则。
"""
from __future__ import annotations
from support.memory_review import supported_row

from scripts.memory_test_support import seed_semantic
import gc
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_core.contracts import Scope
from agent_core.memory_horizon import resolve_lifetime
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, turn_from_messages
from agent_core.memory_writes import MemoryWriter


class _Embeddings:
    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# 纯判定：不改库也能看出规则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "declared"),
    [
        ("项目暂时不部署，继续做本机验收", "short_term"),
        ("该方案先设计，尚未实施", "short_term"),
        ("这个项目暂时先不部署", ""),
        ("部署工作现阶段继续本机验收", "short_term"),
        # 独立复核发现：同一条内容里带一句当天状态时，阶段约束不能被降级。
        ("项目先不部署，继续联调，今天有点累", "short_term"),
        ("项目暂时先不部署，今天我有点累", "short_term"),
        ("项目先不部署，继续联调，心情不太好", ""),
    ],
)
def test_phase_constraints_are_kept_long_term(content: str, declared: str) -> None:
    """模型把它标成短期也一样：阶段约束按长期保留。"""
    lifetime = resolve_lifetime(content, declared=declared)
    assert lifetime.horizon == "long_term"
    assert lifetime.expires_in is None


@pytest.mark.parametrize(
    "content",
    ["今天有点累", "今天因为等待验收有些烦躁", "这会儿心情一般", "今天天气不错"],
)
def test_same_day_states_stay_short_term(content: str) -> None:
    lifetime = resolve_lifetime(content, declared="short_term")
    assert lifetime.horizon == "short_term"
    assert lifetime.expires_in == timedelta(days=1)


@pytest.mark.parametrize(
    ("said", "days"),
    [
        ("这条约束保留三天：服务器先只读。", 3),
        ("服务器先只读，期限五天。", 5),
        ("只限今天：先别部署。", 1),
        ("服务器只读一周内有效。", 7),
    ],
)
def test_a_duration_the_user_stated_is_used_verbatim(said: str, days: int) -> None:
    """用户明说期限时按原话算，不套默认的一天。"""
    lifetime = resolve_lifetime("服务器先只读", source_text=said)
    assert lifetime.horizon == "short_term"
    assert lifetime.expires_in == timedelta(days=days)


def test_a_long_term_declaration_is_not_overridden_by_words() -> None:
    """长期原则里出现「今天」很正常，不能按关键词判成短期。"""
    lifetime = resolve_lifetime("长期原则：今天的事情今天做完", declared="long_term")
    assert lifetime.horizon == "long_term"


# ---------------------------------------------------------------------------
# 端到端：真的落库、真的推进时间
# ---------------------------------------------------------------------------


def _env(tmp_path: Path):
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    return repos, MemoryWriter(repos, _Embeddings())


def _save(writer, scope, packet, item, *, turn_id="turn-1", mode='phase', days=None, quotes=None, parts=None, **kwargs):
    from agent_core.memory_extract import SemanticMemoryExtractor
    import json as _json

    raw = _json.dumps({"memories": [item]}, ensure_ascii=False)
    candidates, _suggestions, rejected = SemanticMemoryExtractor(None).parse_maintenance(
        raw, packet=packet
    )
    assert len(candidates) == 1, rejected
    import asyncio
    from agent_core.memory_processing import MemoryReviewProcessor
    from support.memory_review import FixedReviewProvider, supported_row
    assert quotes, 'Each positive test provides its own complete source clauses'
    row = supported_row(item['content'], quotes, subject='当前测试事项', mode=mode, days=days)
    if parts:
        row['parts'] = parts
    durable = []
    provider = FixedReviewProvider([row])
    candidates, _, result = asyncio.run(MemoryReviewProcessor(writer, provider).prepare(
        packet, candidates, [], persist=durable.append))
    assert provider.calls == 1 and durable
    assert candidates, result.as_dict()
    return writer.save_semantic_candidates(
        scope, candidates, turn_id=turn_id, packet=packet, **kwargs
    )


def test_a_phase_constraint_survives_two_days_of_maintenance(tmp_path: Path) -> None:
    repos, writer = _env(tmp_path)
    try:
        scope = Scope("horizon-user", "web", "phase", "companion")
        packet = turn_from_messages(
            turn_id="turn-1",
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            scope=scope,
            user_text="这个项目暂时先不部署，继续做本机验收。",
            published_reply="收到",
        )
        saved = _save(
            writer,
            scope,
            packet,
            {
                "content": "项目暂时不部署，继续做本机验收",
                "kind": "decision",
                "content_status": "accepted",
                "source_types": ["user_statement"],
                "source_refs": ["turn-1"],
                "horizon": "short_term",
            },
            quotes=[('turn-1', '这个项目暂时先不部署'), ('turn-1', '继续做本机验收')],
            parts=[
                supported_row('项目暂时不部署', [('turn-1', '这个项目暂时先不部署')], subject='项目部署', mode='phase'),
                supported_row('项目继续做本机验收', [('turn-1', '继续做本机验收')], subject='本机验收', mode='phase'),
            ],
        )
        assert len(saved) == 2, 'Deployment and local acceptance have separate model judgments'
        assert {r['content'] for r in saved} == {'项目暂时不部署', '项目继续做本机验收'}
        assert saved[0]["expires_at"] in (None, ""), "阶段约束不该被写上一天有效期"
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=2))
        fresh = repos.records_by_ids([saved[0]["id"]])[saved[0]["id"]]
        assert fresh["status"] == "active", "没有新证据时，阶段约束两天后仍然有效"
    finally:
        repos.close()
        gc.collect()


def test_a_same_day_mood_expires_on_schedule(tmp_path: Path) -> None:
    repos, writer = _env(tmp_path)
    try:
        scope = Scope("horizon-user", "web", "mood", "companion")
        packet = turn_from_messages(
            turn_id="turn-2",
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            scope=scope,
            user_text="今天有点累，先歇会儿。",
            published_reply="好",
        )
        saved = _save(
            writer,
            scope,
            packet,
            {
                "content": "今天有点累",
                "kind": "fact",
                "content_status": "reported",
                "source_types": ["user_statement"],
                "source_refs": ["turn-2"],
                "horizon": "short_term",
            },
            turn_id="turn-2",
            mode='short_term', days=1,
            quotes=[('turn-2', '今天有点累')],
        )
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=2))
        fresh = repos.records_by_ids([saved[0]["id"]])[saved[0]["id"]]
        assert fresh["status"] != "active", "当天情绪按短期政策到期"
    finally:
        repos.close()
        gc.collect()


def test_an_explicit_three_day_constraint_uses_three_days(tmp_path: Path) -> None:
    repos, writer = _env(tmp_path)
    try:
        scope = Scope("horizon-user", "web", "three-days", "companion")
        packet = turn_from_messages(
            turn_id="turn-3",
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            scope=scope,
            user_text="这条约束保留三天：服务器先只读。",
            published_reply="好",
        )
        saved = _save(
            writer,
            scope,
            packet,
            {
                "content": "服务器先只读",
                "kind": "decision",
                "content_status": "accepted",
                "source_types": ["user_statement"],
                "source_refs": ["turn-3"],
                "horizon": "short_term",
            },
            turn_id="turn-3",
            mode='short_term', days=3,
            quotes=[('turn-3', '这条约束保留三天：服务器先只读')],
        )
        expires = datetime.fromisoformat(str(saved[0]["expires_at"]))
        remaining = expires - datetime.now(UTC)
        assert timedelta(days=2, hours=20) < remaining < timedelta(days=3, hours=1)
        # 两天后仍然有效（三天还没到），三天半后失效。
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=2))
        still = repos.records_by_ids([saved[0]["id"]])[saved[0]["id"]]
        assert still["status"] != "expired"
        writer.run_maintenance(now=datetime.now(UTC) + timedelta(days=4))
        gone = repos.records_by_ids([saved[0]["id"]])[saved[0]["id"]]
        assert gone["status"] == "expired"
    finally:
        repos.close()
        gc.collect()


def test_the_same_rule_applies_to_an_update(tmp_path: Path) -> None:
    """更新走的是另一条落库路径，寿命规则必须是同一条。"""
    from dataclasses import replace

    from agent_core.memory_maintenance import MemoryMaintenanceService, MemoryUpdateSuggestion
    from agent_core.memory_turn import MemoryRef

    repos, writer = _env(tmp_path)
    try:
        scope = Scope("horizon-user", "web", "update", "companion")

        def _seed(content: str) -> dict:
            return seed_semantic(writer,
                scope,
                [
                    MemoryCandidate(
                        content=content,
                        kind="decision",
                        content_status="accepted",
                        source_types=("user_statement",),
                        source_refs=("seed",),
                    )
                ],
                turn_id=f"seed-{content}",
            )[0]

        service = MemoryMaintenanceService(writer, repos)

        def _apply(saved: dict, user_text: str, content: str, *, turn: str) -> dict:
            packet = turn_from_messages(
                turn_id=turn,
                occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
                scope=scope,
                user_text=user_text,
                published_reply="好",
            )
            packet = replace(
                packet,
                recalled_memory_refs=[
                    MemoryRef(
                        saved["id"],
                        saved["kind"],
                        saved["content_status"],
                        saved["content"],
                        version=saved["record_version"],
                    )
                ],
            )
            suggestion = MemoryUpdateSuggestion(
                    action="replace",
                    target_id=saved["id"],
                    expected_version=int(saved["record_version"]),
                    content=content,
                    kind="decision",
                    content_status="accepted",
                    source_types=("user_statement",),
                    source_refs=(turn,),
                    reason="用户改用方案B",
                    horizon="short_term",
                )
            import asyncio
            from support.memory_review import prepare_update, supported_row
            quotes = ([(turn, '项目部署方案改用方案B'), (turn, '暂时先不实施')] if turn == 'turn-4'
                      else [(turn, '回滚方案改用方案C'), (turn, '这条保留三天')])
            row = supported_row(content, quotes, subject='部署方案' if turn == 'turn-4' else '回滚方案',
                                mode='phase' if turn == 'turn-4' else 'short_term',
                                days=None if turn == 'turn-4' else 3)
            reviewed = asyncio.run(prepare_update(writer, packet, suggestion, [row]))
            return service.apply(
                reviewed,
                scope=scope,
                packet=packet,
                turn_id=turn,
            )

        first = _seed("项目部署方案为方案A")
        phase = _apply(
            first,
            "项目部署方案改用方案B，暂时先不实施。",
            "项目部署方案改用方案B，暂时先不实施",
            turn="turn-4",
        )
        assert phase["applied"] is True, phase["reason"]
        assert phase["record"]["expires_at"] in (None, ""), "阶段约束的新版本不该被写上一有效期限"
        assert "phase" in str(phase["record"].get("metadata") or "")

        second = _seed("回滚方案为方案X")
        timed = _apply(
            second, "回滚方案改用方案C，这条保留三天。", "回滚方案改用方案C", turn="turn-5"
        )
        assert timed["applied"] is True, timed["reason"]
        expires = datetime.fromisoformat(str(timed["record"]["expires_at"]))
        remaining = expires - datetime.now(UTC)
        assert timedelta(days=2, hours=20) < remaining < timedelta(days=3, hours=1)
    finally:
        repos.close()
        gc.collect()
