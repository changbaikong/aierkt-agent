"""B3-R4：中文数词与阿拉伯数字的等值识别（正反两个方向）。

背景：库里存的是「服务器预算为5000元」，用户原话说「服务器预算不是五千，
应该是三千元」，模型写「服务器预算为3000元」。程序不能因为「原话里没有
3000 这四个字符」就判这次纠正没有依据 —— 三千和 3000 是同一个值。

反过来同样重要：原话里真的没有这个数时，必须照旧判为缺依据。否则这套等值
识别就变成了「什么都能过」的后门。
"""
from __future__ import annotations

import pytest

from agent_core.memory_turn import (
    changed_value_tokens,
    equivalent_values,
    value_entries,
    value_evidence_gaps,
    value_evidence_missing,
    value_evidenced,
    value_tokens,
)


# ---------------------------------------------------------------------------
# 值本身算不算同一个
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("三千", "3000"),
        ("三千元", "3000元"),
        ("三千元", "3000"),
        ("5000元", "五千元"),
        ("一万", "10000"),
        ("一万块", "10000元"),
        ("三百", "300"),
        ("两百", "200"),
        ("一千二百", "1200"),
        ("3,000元", "三千元"),
        ("一亿", "100000000"),
        ("百分之三十", "30%"),
    ],
)
def test_chinese_and_arabic_values_are_equivalent(left: str, right: str) -> None:
    assert equivalent_values(left, right) is True
    assert equivalent_values(right, left) is True


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("三千", "5000"),
        ("三千元", "3000人"),          # 数值一样，说的不是一回事
        ("五千元", "5000元" * 0 + "500元"),
        ("一万", "1000"),
        ("预算还没变化", "3000"),
    ],
)
def test_different_values_are_not_equivalent(left: str, right: str) -> None:
    assert equivalent_values(left, right) is False


def test_value_entries_keep_the_amount_and_its_unit() -> None:
    assert value_entries("服务器预算为3000元") == ((3000, "元"),)
    assert value_entries("服务器预算为三千元") == ((3000, "元"),)
    assert value_entries("预算3,000元") == ((3000, "元"),)
    # 「元」原话里省了，就如实记成没有量词，不能凭空补一个。
    assert value_entries("预算三千") == ((3000, ""),)


def test_grouped_number_is_one_value_not_three() -> None:
    """`3,000` 要当成一个数；不能切成 `3` 和 `000`，还去后面借一个「千」。"""
    assert value_tokens("预算3,000元") == ("3,000",)
    assert value_entries("预算3,000元") == ((3000, "元"),)
    assert equivalent_values("预算3,000元", "预算三千元") is True


def test_extra_punctuation_and_spacing_does_not_break_the_value() -> None:
    assert equivalent_values("预算为 3,000 元", "预算为三千元") is True


# ---------------------------------------------------------------------------
# 正向：原话里的中文数词要能支撑新内容里的阿拉伯数字（B3-R4 本身）
# ---------------------------------------------------------------------------


def test_a_chinese_numeral_in_the_users_words_evidences_the_arabic_digit() -> None:
    """B3-R4 的核心用例。"""
    evidence = "服务器预算不是五千，应该是三千元"
    assert value_evidenced(evidence, "3000") is True
    assert value_evidenced(evidence, "3000元") is True


def test_the_new_value_is_recognised_as_changed_and_then_supported() -> None:
    """改成三千：先认出「变了的值」，再确认它有原话依据。"""
    assert changed_value_tokens("服务器预算为3000元", "服务器预算为5000元") == ("3000",)
    assert value_evidence_missing(
        "服务器预算为3000元",
        "服务器预算为5000元",
        ["服务器预算不是五千，应该是三千元"],
    ) == ()


def test_the_unit_may_be_written_only_in_the_new_content() -> None:
    """原话说「预算改成三千」，新内容写「3000元」—— 量词是新内容补的，照样算有依据。"""
    assert value_evidenced("预算改成三千", "3000元") is True


def test_grouped_writing_in_the_users_words_supports_chinese_numeral() -> None:
    assert value_evidenced("预算改成 3,000 元", "三千元") is True


# ---------------------------------------------------------------------------
# 反向：原话里没有这个值，必须照样判为缺依据
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("evidence", "value"),
    [
        ("服务器预算还没变化", "3000"),
        ("服务器预算为5000元", "3000"),          # 原话只有旧值
        ("预算改成8000元", "3000"),
        ("今天天气不错", "3000"),
        ("预算是三千元", "1万"),
        ("三千人", "3000元"),                    # 数一样，但不是钱这件事
        ("", "3000"),
    ],
)
def test_a_value_absent_from_the_users_words_stays_unsupported(
    evidence: str, value: str
) -> None:
    assert value_evidenced(evidence, value) is False


def test_an_unsupported_new_value_is_still_reported() -> None:
    """原话说「还没变化」，模型却写 3000：这一项必须报出来。"""
    assert value_evidence_missing(
        "服务器预算为3000元",
        "服务器预算为5000元",
        ["服务器预算还没变化"],
    ) == ("3000",)


def test_a_different_number_in_the_users_words_does_not_help() -> None:
    """原话改成八千，新内容却写三千 —— 三千没有出处。"""
    assert value_evidence_missing(
        "服务器预算为3000元",
        "服务器预算为5000元",
        ["服务器预算改成8000元"],
    ) == ("3000",)


# ---------------------------------------------------------------------------
# 逐项核对：一项有依据不能把另一项带进来（B3-R3 依赖这个接口）
# ---------------------------------------------------------------------------


def test_each_changed_value_is_checked_on_its_own() -> None:
    """三千有出处，2026年12月31日没有 —— 只放行有出处的那一项。"""
    gaps = value_evidence_gaps(
        "采购项目预算三千，交付时间为2026年12月31日",
        "采购项目预算五千",
        ["采购项目预算不是五千，是三千；交付日期尚未确定"],
    )
    assert any(token == "三千" and source for token, source in gaps), "三千有出处"
    assert any(not source for _, source in gaps), "没有出处的日期必须单独报出来"


def test_the_supported_item_keeps_its_source_text() -> None:
    gaps = value_evidence_gaps(
        "服务器预算为3000元",
        "服务器预算为5000元",
        ["服务器预算不是五千，应该是三千元"],
    )
    assert gaps == (("3000", "服务器预算不是五千，应该是三千元"),)


def test_missing_helper_only_returns_the_unbacked_values() -> None:
    missing = value_evidence_missing(
        "采购项目预算三千，交付时间为2026年12月31日",
        "采购项目预算五千",
        ["采购项目预算不是五千，是三千"],
    )
    assert "三千" not in missing
    assert missing, "日期那一项没有出处，必须出现在缺失列表里"


def test_a_rewritten_but_unchanged_value_is_not_treated_as_a_changed_value() -> None:
    """旧内容写 5000元、新内容写「五千元」只是在换写法，数值这一项不算变了。

    这里只看**数值**那一项：换成写法后，`三千`/`5000` 这类数字片段必须消失，
    否则每次换个写法都会被当成「值改了」。剩下的中文实义片段沿用既有规则，
    不在本文件的判定范围。
    """
    changed = changed_value_tokens("预算五千元", "预算5000元")
    assert "五千" not in changed
    assert not any(value_tokens_are_numeric(token) for token in changed)


def value_tokens_are_numeric(token: str) -> bool:
    """这个片段是不是数值（用来把「换写法」和「真实义变化」分开）。"""
    return bool(value_entries(token))


# ---------------------------------------------------------------------------
# 既有语义没有被放宽
# ---------------------------------------------------------------------------


def test_mentions_item_is_not_relaxed_by_value_equivalence() -> None:
    """等值识别只作用于值，不能让「提到事项」变宽松。"""
    from agent_core.memory_turn import mentions_item

    assert mentions_item("我们讨论一下服务器预算", "服务器预算") is True
    assert mentions_item("今天天气不错", "服务器预算") is False


def test_details_that_are_unparseable_are_left_to_the_existing_rules() -> None:
    """不是数值的片段（纯中文实义片段）不在这里判，交给既有的按事项规则。"""
    assert value_entries("把方案改成本地优先") == ()
