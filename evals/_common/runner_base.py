"""异步跑 case 的基类：子类实现 _run_one，基类负责写 JSONL + 调 Scorer + 汇总。

沿用 evals/web_workflow_matrix.py 的 run_case/main 模式：
- 每 case 跑一次，结果写一行 JSONL（含 provider_requests 全量重放证据）。
- 异常吞掉记成 error，不中断后续 case。
- 跑完调 pick_scorer 打分，把 score/auto/redline_broken 写进同一行。

套 5/6/7 的脚本继承这个基类，只实现 _run_one(case) -> dict 即可。
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .case import Case, case_to_record
from .scorer import pick_scorer


class MatrixRunner(ABC):
    """LLM evals 矩阵的通用骨架。"""

    def __init__(self, cases: list[Case], *, limit: int | None = None) -> None:
        self.cases = cases[:limit] if limit else list(cases)
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    @abstractmethod
    async def _run_one(self, case: Case) -> dict[str, Any]:
        """子类实现：跑一条 case，返回 result dict。

        result 至少含 status；多轮 case 还应含 reply/executions/responses/provider_requests。
        """

    async def run_all(self, output_path: Path) -> list[dict[str, Any]]:
        """跑全部 case，写 JSONL，返回记录列表（含评分）。"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        records: list[dict[str, Any]] = []
        with output_path.open("w", encoding="utf-8") as handle:
            for case in self.cases:
                record = await self._run_and_score(case)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                records.append(record)
                self._print_summary(record)
        return records

    async def _run_and_score(self, case: Case) -> dict[str, Any]:
        try:
            raw = await self._run_one(case)
        except Exception as exc:  # 不中断后续 case
            raw = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        record = case_to_record(case, result=raw)
        score = pick_scorer(case).evaluate(case, raw)
        record["score"] = score.score
        record["auto"] = score.auto
        record["score_reason"] = score.reason
        record["redline_broken"] = score.redline_broken
        return record

    @staticmethod
    def _print_summary(record: dict[str, Any]) -> None:
        keys = ("case_id", "category", "status", "score", "redline_broken", "error")
        line = json.dumps({k: record.get(k) for k in keys}, ensure_ascii=False)
        print(line, flush=True)
