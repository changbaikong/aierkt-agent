"""单轮请求的分阶段耗时日志。

为什么不复用 ``metrics.py``：那是聚合指标（Prometheus 直方图），它能回答
"整体是不是变慢了"，但回答不了"慢在哪一段"。这里要的正是后者 ——
每一轮输出一条，用同一个 turn_id 串起来，才能看出是读位置慢了、
还是坐标转城市慢了。

三条纪律（方案第一部分）：

1. **不记用户坐标**；
2. **不记接口密钥**；
3. **不记第三方接口原文**。

防呆方式是"不提供自由文本入口"：调用方只能给一个 ``status``，而且它必须是
短标识（小写字母 / 数字 / ``_ . : -``），**纯数字被单独拒绝** ——
"31.2304" 这种坐标恰好能通过字符集检查。写不进去的东西，就不会漏出去。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

# 阶段名是固定枚举，不接受自由文本 —— 见模块文档。
STAGE_TURN_STARTED = "turn_started"
STAGE_INTENT_ROUTE = "intent_route"
STAGE_LOCATION_READ = "location_read"
STAGE_GEOCODE = "geocode"
STAGE_EXTERNAL_REQUEST = "external_request"
STAGE_TOOL_STARTED = "tool_started"
STAGE_TOOL_FINISHED = "tool_finished"
STAGE_STATUS_EMITTED = "status_emitted"
STAGE_REPLY_READY = "reply_ready"
#: 联网搜索的各段（A8：搜索耗时必须能分段看见，不能再只记一个总数）。
STAGE_SEARCH_STARTED = "search_started"
STAGE_SEARCH_FINISHED = "search_finished"
STAGE_FACTS_READY = "facts_ready"
STAGE_FINAL_REVIEW = "final_review"
STAGE_REWRITE = "rewrite"
STAGE_PUBLISH = "publish"

#: 模型调用账本的阶段名（与上面共用同一套短标识纪律）。
CALL_INTENT = "intent"
CALL_COVERAGE = "coverage"
CALL_MAIN_ANSWER = "main_answer"
CALL_FINAL_REVIEW = "final_review"
CALL_REWRITE = "rewrite"
CALL_CLAUSE = "clarify"
CALL_SUMMARY = "summary"

_SAFE_TOKEN = re.compile(r"^[a-z0-9_.:-]{1,32}$")


def safe_token(value: object) -> str:
    """把调用方给的标识压成安全形式；不像标识的一律变成 ``invalid``。

    纯数字被单独挡掉：``31.2304`` 恰好满足字符集，但它是坐标。
    """
    text = str(value or "").strip().lower()
    if not _SAFE_TOKEN.match(text):
        return "invalid"
    if text.replace(".", "").replace("-", "").replace(":", "").isdigit():
        return "invalid"
    return text


def estimate_tokens(value: Any) -> int:
    """模型没回用量时的估算口径（与主循环一致）：中日韩一字一 token，其余四字符一 token。

    估算是**兜底**，不是计费：账本会把它标成 ``estimated``，让读日志的人知道
    这个数字来自文本长度而不是接口回执。
    """
    text = value if isinstance(value, str) else str(value or "")
    cjk = sum("\u4e00" <= char <= "\u9fff" for char in text)
    ascii_chars = len(text) - cjk
    return max(0, cjk + (ascii_chars + 3) // 4)


def usage_tokens(usage: Mapping[str, Any] | None) -> tuple[int, int, bool]:
    """从 provider 用量里取 (输入, 输出, 是否来自接口)。

    两种字段名都要认：OpenAI 兼容层用 ``prompt_tokens`` / ``completion_tokens``，
    其他实现可能用 ``input_tokens`` / ``output_tokens``。两个都是 0 时返回
    ``estimated=True``，由调用方按文本长度补齐。
    """
    payload = dict(usage or {})
    prompt = payload.get("prompt_tokens", payload.get("input_tokens"))
    completion = payload.get("completion_tokens", payload.get("output_tokens"))
    try:
        prompt_tokens = int(prompt or 0)
        completion_tokens = int(completion or 0)
    except (TypeError, ValueError):
        return 0, 0, True
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return 0, 0, True
    return prompt_tokens, completion_tokens, False


@dataclass
class CallLedger:
    """本回合的模型调用账本：每一笔记阶段、用量、是否估算、开始结束时间。

    A8 的两个毛病都靠它修：辅助调用（入口判定、覆盖整理、核查、重写）过去
    完全不计费，"搜索回答"看起来不花钱；首字时间又从一个较晚的点起算。
    账本让每次调用都留下同口径的一笔，缺用量时用文本估算并明确标记。
    """

    started: Callable[[], float] = field(default=lambda: 0.0)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        stage: object,
        usage: Mapping[str, Any] | None = None,
        *,
        estimated_text: str = "",
        started_ms: float | None = None,
        finished_ms: float | None = None,
    ) -> dict[str, Any]:
        prompt, completion, estimated = usage_tokens(usage)
        if estimated:
            prompt = estimate_tokens(estimated_text) if estimated_text else 0
        entry = {
            "stage": safe_token(stage) if stage else "unknown",
            "input_tokens": prompt,
            "output_tokens": completion,
            "estimated": bool(estimated),
            "started_ms": round(float(started_ms if started_ms is not None else self.started()), 1),
            "finished_ms": round(float(finished_ms if finished_ms is not None else self.started()), 1),
        }
        self.calls.append(entry)
        return entry

    def snapshot(self) -> dict[str, Any]:
        stages: dict[str, dict[str, int]] = {}
        for entry in self.calls:
            bucket = stages.setdefault(entry["stage"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_calls": 0})
            bucket["calls"] += 1
            bucket["input_tokens"] += int(entry["input_tokens"])
            bucket["output_tokens"] += int(entry["output_tokens"])
            bucket["estimated_calls"] += 1 if entry["estimated"] else 0
        return {
            "calls": [dict(entry) for entry in self.calls],
            "stages": stages,
            "call_count": len(self.calls),
            "input_tokens": sum(int(entry["input_tokens"]) for entry in self.calls),
            "output_tokens": sum(int(entry["output_tokens"]) for entry in self.calls),
            "estimated_calls": sum(1 for entry in self.calls if entry["estimated"]),
        }


class TurnTimer:
    """一轮的分阶段计时器。

    ``elapsed_ms`` 记的是"从这一轮开始到现在"，不是相邻两点的差值 ——
    某一步没走到时，读日志的人也能看出进度停在哪。

    计时不参与任何业务判断：它只写日志，不改变控制流，失败了也不该影响这一轮。
    """

    def __init__(self, turn_id: object, *, clock: Callable[[], float] | None = None) -> None:
        self.turn_id = safe_token(turn_id) if turn_id else "unknown"
        self._clock = clock or time.perf_counter
        self._started = self._clock()
        self._stages: list[tuple[str, str, float]] = []
        self._finished = False
        self.ledger = CallLedger(started=self.elapsed_ms)

    def elapsed_ms(self) -> float:
        """从这一轮开始到现在的毫秒数（首字时间也用这个口径，A8）。"""
        return (self._clock() - self._started) * 1000

    def mark(self, stage: str, *, status: object = "") -> None:
        """记一个阶段。``stage`` 用上面那组常量，``status`` 只放短标识。"""
        if self._finished:
            return
        self._stages.append(
            (str(stage), safe_token(status) if status else "", self._clock() - self._started)
        )

    def marks(self) -> Sequence[tuple[str, str, float]]:
        return tuple(self._stages)

    def finish(self) -> dict:
        """收口：写一行日志，返回结构化结果（同一轮只生效一次）。"""
        if self._finished:
            return {}
        self._finished = True
        total_ms = round(self.elapsed_ms(), 1)
        chain = " ".join(
            f"{stage}={round(elapsed * 1000, 1)}ms" + (f"/{status}" if status else "")
            for stage, status, elapsed in self._stages
        )
        ledger = self.ledger.snapshot()
        logger.info(
            "turn.timing turn=%s total_ms=%s calls=%s in=%s out=%s estimated=%s %s",
            self.turn_id, total_ms, ledger["call_count"], ledger["input_tokens"],
            ledger["output_tokens"], ledger["estimated_calls"], chain,
        )
        return {
            "turn_id": self.turn_id,
            "total_ms": total_ms,
            "stages": [
                {"stage": stage, "status": status, "elapsed_ms": round(elapsed * 1000, 1)}
                for stage, status, elapsed in self._stages
            ],
            "usage": ledger,
        }
