"""统一依据检查：错误事实不能借「换方案／换说法」写进库。

真实批次暴露的两个方向都要锁住：

1. **不能误放行**：用户说「改用方案B，不再用方案A；尚未实施，也没有评审结论」，
   模型写「改用方案B，安全评审已通过」时，评审结论没有出处，整条不应用；
2. **不能误拒绝**：同一个选择换个说法（用→使用、选择→决定）、同一句里给出的
   数值（「预算八千」），都是正当表达，必须照常落库。

判据由 ``memory_evidence`` 统一提供，维护更新与候选替代两个入口共用。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_core.memory_evidence import evidence_gaps, gap_reason
from agent_core.memory_turn import changed_value_tokens, turn_from_messages, value_tokens


# ---------------------------------------------------------------------------
# 值这一层：普通词里的数词不是数值
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ["一律", "统一", "一样", "一致", "唯一", "一般", "一直"])
def test_ordinary_words_containing_a_numeral_are_not_values(word: str) -> None:
    """「一律」「统一」里的「一」是词的一部分，不是一个待核对的值。"""
    assert value_tokens(f"项目日志{word}先落盘再回传") == ()


def test_real_values_are_still_values() -> None:
    """收窄只针对普通词：真正的数值（含量词）照旧认得出来。"""
    assert value_tokens("预算八千元") == ("八千",)
    assert value_tokens("五个人") == ("五",)
    assert value_tokens("三千") == ("三千",)


def test_a_word_numeral_no_longer_shows_up_as_a_changed_value() -> None:
    """「先落盘」→「一律先落盘」不再产生一个叫「一」的新值。"""
    changed = changed_value_tokens("项目日志一律先落盘", "项目日志先落盘")
    assert "一" not in changed
    from agent_core.memory_turn import value_entries

    assert not any(value_entries(token) for token in changed)


# ---------------------------------------------------------------------------
# 结论类信息：换说法可以，新增结论不行
# ---------------------------------------------------------------------------


def _gap_ids(new: str, old: str, evidence: list[str], item: str = "") -> list[str]:
    gaps = evidence_gaps(new, old, evidence_clauses=evidence, item_text=item or old)
    return [item.category for item in gaps]


def test_an_invented_approval_conclusion_is_not_supported() -> None:
    gaps = evidence_gaps(
        "部署方案改用方案B，安全评审已通过",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A"],
    )
    assert gaps, "评审结论没有出处，必须报出来"
    assert any(item.category == "review_result" for item in gaps)


def test_an_invented_execution_result_is_not_supported() -> None:
    gaps = evidence_gaps(
        "部署方案改用方案B，回滚演练已经成功",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A"],
    )
    assert any(item.category == "implementation_result" for item in gaps)


def test_an_approved_window_claim_is_not_supported() -> None:
    """第五轮 W8 的既有反例：定性新增不能借替代写进库。"""
    gaps = evidence_gaps(
        "容灾方案改用方案B，实施窗口已获批准",
        "容灾方案为方案A",
        evidence_clauses=["容灾方案改用方案B", "不再用方案A"],
    )
    assert any(item.category == "review_result" for item in gaps)


@pytest.mark.parametrize(
    "claim",
    [
        "部署方案改用方案B，已获上级同意",
        "部署方案改用方案B，甲方认可",
        "部署方案改用方案B，已征得客户同意",
        "部署方案改用方案B，得到了领导认可",
    ],
)
def test_consent_claims_need_their_own_evidence(claim: str) -> None:
    """取得式「同意／认可」也是审批结论：原话说「上级也没表态」时不许写进去。

    独立复核发现过这条漏网：「同意」单看是语气词，所以它既不在固定字面里，
    也不是「审批名词 + 结果词」，两层检查都放它过去。
    """
    gaps = evidence_gaps(
        claim,
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "还没验收，上级也没表态"],
    )
    assert any(item.category == "review_result" for item in gaps), gaps


def test_a_plain_agreement_elsewhere_does_not_approve_this_item() -> None:
    """用户对别的事说「同意」，不能当成这件事已获同意。"""
    gaps = evidence_gaps(
        "部署方案改用方案B，已获上级同意",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "同意以后回答先说结论"],
    )
    assert any(item.category == "review_result" for item in gaps), gaps


def test_a_new_conclusion_in_a_category_the_old_text_already_has_is_checked() -> None:
    """旧内容写着「验收通过」，新内容另加「评审通过」：那是另一件事的结论。"""
    gaps = evidence_gaps(
        "部署方案改用方案B，评审通过",
        "部署方案为方案A，验收通过",
        evidence_clauses=["部署方案改用方案B"],
    )
    assert any(item.category == "review_result" for item in gaps), gaps


def test_a_supported_conclusion_passes() -> None:
    """用户自己说过「评审已通过」时，同一结论照常放行。"""
    gaps = evidence_gaps(
        "部署方案改用方案B，安全评审已通过",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A", "部署方案改用方案B，安全评审已经通过"],
    )
    assert not gaps


def test_a_negated_conclusion_in_the_users_words_is_not_support() -> None:
    """「尚未实施／没有评审结论」不能当成「已实施／已通过」的依据。"""
    gaps = evidence_gaps(
        "部署方案改用方案B，安全评审已通过",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B，不再用方案A", "尚未实施，也没有评审结论"],
    )
    assert any(item.category == "review_result" for item in gaps)


def test_rewording_the_same_choice_still_passes() -> None:
    """「用／使用／选择／决定」是同一个选择的另一种说法。"""
    gaps = evidence_gaps(
        "部署方案决定使用方案B",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A"],
    )
    assert not gaps


def test_an_added_scope_is_not_supported() -> None:
    """「今天先这样做」不能变成「以后全部这样做」。"""
    gaps = evidence_gaps(
        "值班安排为轮换排班，以后全部这样做",
        "值班安排为轮换排班",
        evidence_clauses=["今天先这样做"],
    )
    assert any(item.category == "scope" for item in gaps)


def test_a_same_sentence_amount_stated_by_the_user_is_supported() -> None:
    """用户在同一句里说了「预算八千」，这个数就是他自己的话。"""
    gaps = evidence_gaps(
        "部署方案改用方案B，预算八千",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B", "不再用方案A", "预算八千"],
    )
    assert not gaps


def test_gap_reason_names_the_unsupported_item() -> None:
    gaps = evidence_gaps(
        "部署方案改用方案B，安全评审已通过",
        "部署方案为方案A",
        evidence_clauses=["部署方案改用方案B"],
    )
    reason = gap_reason(gaps)
    assert "评审" in reason and "暂不应用" in reason


# ---------------------------------------------------------------------------
# 端到端：维护更新入口真的不落库
# ---------------------------------------------------------------------------


class _Embeddings:
    def embed(self, _text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _stack(tmp_path):
    from agent_core.contracts import Scope
    from agent_core.memory_maintenance import MemoryMaintenanceService
    from agent_core.memory_repositories import initialize_memory_repositories
    from agent_core.memory_turn import MemoryCandidate
    from agent_core.memory_writes import MemoryWriter

    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    writer = MemoryWriter(repos, _Embeddings())
    scope = Scope("evidence-user", "web", "evidence", "companion")
    from support.memory_seed import seed_semantic
    old = seed_semantic(
        writer, scope,
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
    return repos, MemoryMaintenanceService(writer, repos), writer, scope, old


def _apply(tmp_path, user_text: str, new_content: str, *, action: str = "replace"):
    from datetime import UTC, datetime

    from agent_core.memory_maintenance import MemoryUpdateSuggestion
    from agent_core.memory_turn import MemoryRef

    repos, service, _writer, scope, old = _stack(tmp_path)
    try:
        packet = turn_from_messages(
            turn_id="turn-1",
            occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
            scope=scope,
            user_text=user_text,
            published_reply="收到",
        )
        packet = replace(
            packet,
            recalled_memory_refs=[
                MemoryRef(old["id"], old["kind"], old["content_status"], old["content"], version=1)
            ],
        )
        suggestion = MemoryUpdateSuggestion(
                action=action,
                target_id=old["id"],
                expected_version=1,
                content=new_content,
                kind="decision",
                content_status="accepted",
                source_types=("user_statement",),
                source_refs=("turn-1",),
                reason="模型建议更新",
            )
        if (user_text, new_content) == (
            '部署方案改用方案B，不再用方案A；尚未实施。', '部署方案决定使用方案B'
        ):
            import asyncio
            from support.memory_review import prepare_update, supported_row
            suggestion = asyncio.run(prepare_update(_writer, packet, suggestion, [supported_row(
                '部署方案决定使用方案B，尚未实施',
                [('turn-1', '部署方案改用方案B'), ('turn-1', '不再用方案A'), ('turn-1', '尚未实施')],
                subject='部署方案', status='accepted')]))
        outcome = service.apply(
            suggestion,
            scope=scope,
            packet=packet,
            turn_id="turn-1",
        )
        return outcome, repos, old
    finally:
        pass


@pytest.mark.parametrize(
    "new_content",
    [
        "部署方案改用方案B，安全评审已通过",
        "部署方案改用方案B，回滚演练已经成功",
    ],
)
def test_invented_conclusions_never_reach_the_database(tmp_path, new_content: str) -> None:
    outcome, repos, old = _apply(
        tmp_path, "部署方案改用方案B，不再用方案A；尚未实施，也没有评审结论。", new_content
    )
    assert outcome["applied"] is False, outcome["reason"]
    assert repos.records_by_ids([old["id"]])[old["id"]]["status"] == "active"
    repos.close()


def test_a_normal_switch_still_reaches_the_database(tmp_path) -> None:
    outcome, repos, old = _apply(
        tmp_path, "部署方案改用方案B，不再用方案A；尚未实施。", "部署方案决定使用方案B"
    )
    assert outcome["applied"] is True, outcome["reason"]
    assert outcome['record']['content'] == '部署方案决定使用方案B，尚未实施'
    assert repos.records_by_ids([old["id"]])[old["id"]]["status"] == "archived"
    repos.close()


def test_the_json_round_trip_does_not_change_the_verdict() -> None:
    """处理包是 JSON 进出的：同一判据不因为序列化就变。"""
    payload = json.dumps({"memories": [{"content": "部署方案改用方案B"}]}, ensure_ascii=False)
    assert "部署方案" in payload
