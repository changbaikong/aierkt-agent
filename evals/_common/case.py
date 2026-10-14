"""统一的测试 case 数据结构，兼容单轮和多轮两种形态。

命名规范：M<套号>-<类别><序号>，如 M1-ROUTE-001；红线 case 加 -R 后缀（M3-AUTH-R002）。
单轮用 prompt；多轮用 turns。二者互斥（构造期校验）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Turn:
    """多轮 case 的一轮：发什么消息、走哪个入口、带什么 payload。"""

    message: str
    endpoint: str = "/api/chat"
    payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class Case:
    """一条矩阵 case。

    Attributes:
        case_id: M<套号>-<类别><序号>，红线加 -R。
        category: 类别名（exploratory / hard_execute / host_owned / carry_name 等）。
        expectation: 机器或人工判定的期望描述。
        prompt: 单轮用；与 turns 互斥。
        turns: 多轮用；与 prompt 互斥。
        kind: "unit"（不调 LLM）/ "llm"（真实调模型）。
        redline: 红线 case，失败即 0 分且总报告标红，永久保留。
        scorer: 选哪个 Scorer（auto / status / regex / tool_call / composite / human）。
        needs_human: True 表示机器判不了，留人工复核。
    """

    case_id: str
    category: str
    expectation: str
    prompt: str | None = None
    turns: tuple[Turn, ...] = ()
    kind: str = "unit"
    redline: bool = False
    scorer: str = "auto"
    needs_human: bool = False
    asserts: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        has_prompt = bool(self.prompt and self.prompt.strip())
        has_turns = bool(self.turns)
        if not has_prompt and not has_turns:
            raise ValueError(f"case {self.case_id} 必须有 prompt 或 turns")
        if has_prompt and has_turns:
            raise ValueError(f"case {self.case_id} 不能同时有 prompt 和 turns")
        if self.kind not in ("unit", "llm"):
            raise ValueError(f"case {self.case_id} kind 只能是 unit 或 llm，得到 {self.kind!r}")


def case_to_record(case: Case, *, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """把 case 连同结果序列化成一行 JSONL（沿用现有 evals 的 results.jsonl 格式）。"""
    record: dict[str, Any] = {
        "case_id": case.case_id,
        "category": case.category,
        "expectation": case.expectation,
        "kind": case.kind,
        "redline": case.redline,
        "needs_human": case.needs_human,
    }
    if case.prompt:
        record["prompt"] = case.prompt
    if case.turns:
        record["turns"] = [
            {"message": t.message, "endpoint": t.endpoint, "payload": t.payload}
            for t in case.turns
        ]
    if result:
        for key in ("status", "reply", "error", "executions", "responses", "provider_requests"):
            if key in result:
                record[key] = result[key]
    return record
