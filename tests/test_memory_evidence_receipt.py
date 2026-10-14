"""核对回执：谁生成的、存在哪、什么时候作废。

计划要求回执由**处理端**生成并随记录／作业持久保存，不接受模型或外部请求自带的
「检查通过」；旧作业没有回执时先补检查再应用；内容、来源或目标版本变了，旧回执
立即失效。这里把这四条锁住。
"""
from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from agent_core.contracts import Scope
from agent_core.memory_evidence import RULE_VERSION, receipt, receipt_is_current
from agent_core.memory_horizon import LONG_TERM, SHORT_TERM, Lifetime
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryRef, turn_from_messages
from agent_core.memory_writes import MemoryWriter


class _Embeddings:
    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def test_a_receipt_lists_every_checked_item_with_its_source() -> None:
    payload = receipt(
        "部署方案改用方案B，安全评审已通过",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A"],
        item_text="部署方案为方案A",
        target_version=3,
        retention="long_term：项目阶段约束",
    )
    assert payload["rule_version"] == RULE_VERSION
    assert payload["target_version"] == 3
    assert payload["passed"] is False
    assert payload["unsupported"] >= 1
    assert any(item["category"] == "review_result" for item in payload["items"])
    assert payload["content_summary"].startswith("部署方案改用方案B")
    assert payload["source_summary"] == ["部署方案改用方案B", "不再用方案A"]
    assert payload["retention"] == "long_term：项目阶段约束"


def test_a_clean_receipt_is_marked_as_passed() -> None:
    payload = receipt(
        "部署方案改用方案B",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A"],
        item_text="部署方案为方案A",
    )
    assert payload["passed"] is True
    assert not payload["items"]


@pytest.mark.parametrize(
    ("content", "target", "expected"),
    [
        ("同一段内容", 1, True),
        ("换了内容", 1, False),
        ("同一段内容", 2, False),
        ("同一段内容", None, False),
    ],
)
def test_a_receipt_expires_when_content_or_version_changes(
    content: str, target, expected: bool
) -> None:
    payload = receipt("同一段内容", "", evidence_clauses=["同一段内容"], target_version=1)
    assert receipt_is_current(payload, content=content, target_version=target) is expected


def test_a_receipt_from_another_rule_version_is_not_reused() -> None:
    payload = receipt("同一段内容", "", evidence_clauses=["同一段内容"], target_version=1)
    payload["rule_version"] = "evidence-0"
    assert receipt_is_current(payload, content="同一段内容", target_version=1) is False


def _env(tmp_path):
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    writer = MemoryWriter(repos, _Embeddings())
    scope = Scope("receipt-user", "web", "receipt", "companion")
    packet = turn_from_messages(
        turn_id="turn-1",
        occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        scope=scope,
        user_text="部署方案改用方案B，不再用方案A。",
        published_reply="收到",
    )
    return repos, writer, scope, packet


def test_the_writer_persists_the_receipt_with_the_record(tmp_path) -> None:
    repos, writer, scope, packet = _env(tmp_path)
    try:
        saved = seed_semantic(writer,
            scope,
            [
                MemoryCandidate(
                    content="部署方案改用方案B",
                    kind="decision",
                    content_status="accepted",
                    source_types=("user_statement",),
                    source_refs=("turn-1",),
                )
            ],
            turn_id="turn-1",
            packet=packet,
        )
        metadata = json.loads(saved[0]["metadata"])
        stored = metadata["evidence_receipt"]
        assert stored["rule_version"] == RULE_VERSION
        assert stored["content_summary"] == "部署方案改用方案B"
        assert stored["passed"] is True
        assert metadata["horizon"] == LONG_TERM
    finally:
        repos.close()


def test_a_matching_stored_receipt_is_reused_instead_of_recomputed(tmp_path) -> None:
    repos, writer, scope, packet = _env(tmp_path)
    try:
        candidate = MemoryCandidate(
            content="部署方案改用方案B",
            kind="decision",
            content_status="accepted",
            source_types=("user_statement",),
            source_refs=("turn-1",),
        )
        from agent_core.memory_evidence import check_memory_content
        prior = check_memory_content(candidate, packet=packet)
        saved = writer.save_semantic_candidates(
            scope, [candidate], turn_id="turn-1", packet=packet, receipts=[prior]
        )
        stored = json.loads(saved[0]["metadata"])["evidence_receipt"]
        assert stored.get("reused") is True, "内容与目标版本没变时应复用旧回执"

        # 内容变了：旧回执作废，重算一份新的。
        other = replace(candidate, content="部署方案改用方案C")
        saved2 = writer.save_semantic_candidates(
            scope, [other], turn_id="turn-2", packet=packet, receipts=[prior]
        )
        assert not saved2, 'changed choice without a new quote cannot reuse a positive receipt'
    finally:
        repos.close()


def test_the_update_path_also_records_a_receipt(tmp_path) -> None:
    from agent_core.memory_maintenance import MemoryMaintenanceService, MemoryUpdateSuggestion

    repos, writer, scope, packet = _env(tmp_path)
    try:
        saved = seed_semantic(writer,
            scope,
            [
                MemoryCandidate(
                    content="部署方案为方案A",
                    kind="decision",
                    content_status="accepted",
                    source_types=("user_statement",),
                    source_refs=("seed",),
                )
            ],
            turn_id="seed",
        )[0]
        packet = replace(
            packet,
            # 证据时间必须在目标记录形成之后：这一条不是本用例要测的东西。
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            recalled_memory_refs=[
                MemoryRef(
                    saved["id"], saved["kind"], saved["content_status"], saved["content"], version=1
                )
            ],
        )
        service = MemoryMaintenanceService(writer, repos)
        suggestion = MemoryUpdateSuggestion(
                action="replace",
                target_id=saved["id"],
                expected_version=1,
                content="部署方案改用方案B",
                kind="decision",
                content_status="accepted",
                source_types=("user_statement",),
                source_refs=("turn-1",),
                reason="用户改用方案B",
            )
        import asyncio
        from support.memory_review import prepare_update, supported_row
        suggestion = asyncio.run(prepare_update(writer, packet, suggestion, [supported_row(
            '部署方案改用方案B', [('turn-1', '部署方案改用方案B')], subject='部署方案')]))
        outcome = service.apply(
            suggestion,
            scope=scope,
            packet=packet,
            turn_id="turn-1",
        )
        assert outcome["applied"] is True, outcome["reason"]
        assert outcome["evidence_receipt"]["passed"] is True
        metadata = json.loads(outcome["record"]["metadata"])
        assert metadata["evidence_receipt"]["rule_version"] == RULE_VERSION
    finally:
        repos.close()


def test_a_retention_basis_is_recorded_for_short_term_items() -> None:
    """短期项目也要在回执里写清保留方式，双端同步时才知道为什么留着。"""
    lifetime = Lifetime(SHORT_TERM, None, "当天状态")
    payload = receipt(
        "今天有点累",
        "",
        evidence_clauses=["今天有点累"],
        retention=lifetime.horizon + "：" + lifetime.basis,
    )
    assert payload["retention"] == "short_term：当天状态"
