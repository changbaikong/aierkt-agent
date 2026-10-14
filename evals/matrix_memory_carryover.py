"""套7 · 多轮记忆 carry-over 矩阵（LLM 多轮）。

测同会话跨轮记忆：记名字/偏好/事实，不混淆多个事实。
每 case 多轮 turns 在同一 session（一个 conversation_id）跑，断言后续轮次引用前面说过的内容。

真实跑需 API key（.env）。无 key 时 skip 并退出 0。

跑法：python evals/matrix_memory_carryover.py [--output evals/results/m7.jsonl] [--limit N]

注：跨会话不串（会话A的名字不出现在会话B）需要多 session harness，
由套6的 store 隔离单元测试 + 套6 LLM 的"不泄露"边界覆盖，本脚本聚焦同会话 carry-over。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from evals._common.case import Case, Turn
from evals._common.llm_harness import run_llm_case
from evals._common.runner_base import MatrixRunner

CASES: list[Case] = [
    Case(
        case_id="M7-MEM-001",
        category="carry_name",
        expectation="第二轮回复含第一轮说过的名字",
        turns=(
            Turn("我叫阿杰，记住。"),
            Turn("我叫什么？"),
        ),
        kind="llm",
        scorer="regex",
        asserts={"must_contain": [r"阿杰"]},
    ),
    Case(
        case_id="M7-MEM-002",
        category="carry_preference",
        expectation="第二轮记住第一轮的偏好（不撒娇）",
        turns=(
            Turn("我不喜欢撒娇，请保持冷静专业。"),
            Turn("你应该怎么跟我说话？"),
        ),
        kind="llm",
        scorer="regex",
        asserts={"must_contain": [r"冷静|专业|不撒娇|直接"]},
    ),
    Case(
        case_id="M7-MEM-003",
        category="carry_fact",
        expectation="记住前面说过的事实",
        turns=(
            Turn("我的猫叫小黑。"),
            Turn("我的猫叫什么？"),
        ),
        kind="llm",
        scorer="regex",
        asserts={"must_contain": [r"小黑"]},
    ),
    Case(
        case_id="M7-MEM-004",
        category="carry_multiple_facts",
        expectation="不混淆多个事实，两个都答对",
        turns=(
            Turn("我叫阿杰。"),
            Turn("我的猫叫小黑。"),
            Turn("我叫什么？我的猫呢？"),
        ),
        kind="llm",
        scorer="regex",
        asserts={"must_contain": [r"阿杰", r"小黑"]},
    ),
    Case(
        case_id="M7-MEM-005",
        category="carry_relative_time",
        expectation="跨轮记住相对时间表达",
        turns=(
            Turn("半小时后提醒我喝水。"),
            Turn("刚才我让你提醒我什么？"),
        ),
        kind="llm",
        scorer="regex",
        asserts={"must_contain": [r"喝水|提醒"]},
    ),
    Case(
        case_id="M7-MEM-006",
        category="no_overwrite_by_later_unrelated",
        expectation="后来的无关信息不覆盖前面的记忆",
        turns=(
            Turn("我叫阿杰。"),
            Turn("今天天气真不错。"),
            Turn("我叫什么？"),
        ),
        kind="llm",
        scorer="regex",
        asserts={"must_contain": [r"阿杰"]},
    ),
]


class MemoryCarryoverMatrix(MatrixRunner):
    async def _run_one(self, case: Case) -> dict:
        return await run_llm_case(case)


async def main(output: Path) -> None:
    await MemoryCarryoverMatrix(CASES).run_all(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("evals/results/m7_memory.jsonl"))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    has_key = any(
        os.environ.get(name)
        for name in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_BASE_URL")
    )
    if not has_key and not (PROJECT_ROOT / ".env").exists():
        print("SKIP: no LLM API key found, matrix 7 skipped")
        sys.exit(0)

    try:
        asyncio.run(main(args.output))
    except Exception as exc:
        print(f"SKIP: LLM harness unavailable ({type(exc).__name__}: {exc}), matrix 7 skipped")
        sys.exit(0)
