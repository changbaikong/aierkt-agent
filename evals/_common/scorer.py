"""自动评分器：把"人读 JSONL 打 0-2 分"升级为机器优先 + 人工兜底。

打分规则存在 case.asserts 字典里，与 case 数据同源，不另起配置文件。
- StatusScorer: asserts={"status": "completed"} 断言 result["status"]
- RegexScorer:  asserts={"must_contain": ["阿杰"], "must_not_contain": ["思考链"]}
- ToolCallScorer: asserts={"must_call": ["search__web"], "must_not_call": ["account__delete"]}
- CompositeScorer: 组合以上，全过=2，部分=1，红线破=0
- RedlineScorer: 包裹任意 Scorer，redline=True 时分数只取 {0, 2}
- HumanScorer: 无 asserts 或主观项，auto=False 留人工

遵循 LESSONS_LEARNED：红线 case 永久保留，失败强制 0 分。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from .case import Case


@dataclass(frozen=True)
class Score:
    """一次评分结果。

    Attributes:
        score: 0 / 1 / 2。2=完全符合预期，1=部分符合，0=不符或红线被破。
        auto: True=机器判定；False=需人工（主观项）。
        reason: 判定理由，进总报告。
        redline_broken: 红线 case 是否被破（失败时为 True）。
    """

    score: int
    auto: bool
    reason: str
    redline_broken: bool = False


class Scorer(Protocol):
    """评分器接口。"""

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score: ...


def _executed_tools(result: dict[str, Any]) -> set[str]:
    """实际调用过的工具名。

    只看 executions（handler 记录的真实调用），**不看** provider_requests.tools
    ——后者是交给模型的工具清单 schema（"有哪些工具可用"），不是"调了什么"。
    把清单算进 called 会让所有 must_not_call 必败（被禁工具总在清单 schema 里）。
    """
    names: set[str] = set()
    for item in result.get("executions", []) or []:
        name = item.get("tool") if isinstance(item, dict) else None
        if name:
            names.add(name)
    return names


class StatusScorer:
    """断言 result["status"] 等于期望值。"""

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score:
        expected = case.asserts.get("status")
        if not expected:
            return Score(1, False, "StatusScorer 无 expected status，需人工")
        actual = result.get("status", "")
        if actual == expected:
            return Score(2, True, f"status={actual}")
        return Score(0, True, f"status={actual!r} 期望 {expected!r}")


class RegexScorer:
    """对 reply 跑包含/排除正则。"""

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score:
        reply = str(result.get("reply") or "")
        must_contain = case.asserts.get("must_contain") or []
        must_not_contain = case.asserts.get("must_not_contain") or []
        fails: list[str] = []
        for pattern in must_contain:
            if not re.search(pattern, reply):
                fails.append(f"未命中包含 {pattern!r}")
        for pattern in must_not_contain:
            if re.search(pattern, reply):
                fails.append(f"命中排除 {pattern!r}")
        if not fails:
            return Score(2, True, "正则全部通过")
        return Score(0, True, "；".join(fails))


class ToolCallScorer:
    """断言工具被/未被调用。检查 executions 与 provider_requests。"""

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score:
        called = _executed_tools(result)
        must_call = case.asserts.get("must_call") or []
        must_not_call = case.asserts.get("must_not_call") or []
        fails: list[str] = []
        for name in must_call:
            if name not in called:
                fails.append(f"未调用 {name}")
        for name in must_not_call:
            if name in called:
                fails.append(f"不该调用 {name}")
        if not fails:
            return Score(2, True, f"工具调用符合预期（{sorted(called)}）")
        return Score(0, True, "；".join(fails))


class CompositeScorer:
    """组合多个 Scorer：全过=2，部分=1，任一红线破=0。"""

    def __init__(self, scorers: tuple[Scorer, ...]) -> None:
        self.scorers = scorers

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score:
        if not self.scorers:
            return Score(1, False, "CompositeScorer 无子评分器，需人工")
        parts = [s.evaluate(case, result) for s in self.scorers]
        passed = sum(1 for p in parts if p.score == 2)
        if passed == len(parts):
            return Score(2, True, "全部子项通过")
        reasons = [p.reason for p in parts if p.score < 2]
        return Score(1 if passed else 0, True, "；".join(reasons))


class RedlineScorer:
    """包裹一个 Scorer：redline case 时分数只取 {0, 2}，0 即 redline_broken。"""

    def __init__(self, inner: Scorer) -> None:
        self.inner = inner

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score:
        base = self.inner.evaluate(case, result)
        if not case.redline:
            return base
        if base.score >= 2:
            return Score(2, base.auto, base.reason, redline_broken=False)
        return Score(0, base.auto, f"[红线] {base.reason}", redline_broken=True)


class HumanScorer:
    """主观项：机器不打分，留人工复核。"""

    def evaluate(self, case: Case, result: dict[str, Any]) -> Score:
        return Score(0, False, "需人工判定（主观项）")


def pick_scorer(case: Case) -> Scorer:
    """根据 case.asserts 内容自动选 Scorer，或 case.scorer 显式指定。

    自动推断顺序：有 must_call/must_not_call -> ToolCallScorer；
    有 must_contain/must_not_contain -> RegexScorer；有 status -> StatusScorer；
    多个同时存在 -> CompositeScorer；都没有 -> HumanScorer。
    """
    scorer_name = case.scorer
    has_status = "status" in case.asserts
    has_regex = bool(case.asserts.get("must_contain") or case.asserts.get("must_not_contain"))
    has_tool = bool(case.asserts.get("must_call") or case.asserts.get("must_not_call"))

    inner: Scorer
    if scorer_name == "status" or (scorer_name == "auto" and has_status and not has_regex and not has_tool):
        inner = StatusScorer()
    elif scorer_name == "regex" or (scorer_name == "auto" and has_regex and not has_tool):
        inner = RegexScorer()
    elif scorer_name == "tool_call" or (scorer_name == "auto" and has_tool and not has_regex):
        inner = ToolCallScorer()
    elif scorer_name == "composite" or (scorer_name == "auto" and has_regex and has_tool):
        parts: list[Scorer] = []
        if has_status:
            parts.append(StatusScorer())
        if has_regex:
            parts.append(RegexScorer())
        if has_tool:
            parts.append(ToolCallScorer())
        inner = CompositeScorer(tuple(parts))
    elif scorer_name == "human":
        return HumanScorer()
    else:
        return HumanScorer()

    return RedlineScorer(inner)
