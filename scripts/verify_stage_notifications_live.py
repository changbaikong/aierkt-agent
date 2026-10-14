"""真实链路验收：统一阶段通知（普通查询 + 补查 + 原文读取）。

用法（在项目根目录）：
    venv/Scripts/python.exe scripts/verify_stage_notifications_live.py

它走的是**真实**的模型、搜索与原文读取（只读查询，不推送、不发布、不改外部数据），
只把过程气泡收进内存，不写用户的聊天库。验收点对照改造计划的任务 5：

1. 快速回合零气泡；慢回合才说，且说的是真实阶段；
2. 一次聊天回合最多一条首次通知 + 两条中段，相邻至少 4 秒；
3. 气泡非空、过公开校验，且没有查询词、网址、工具名、内部错误；
4. 措辞由模型现场生成，但调用有界（每个气泡最多重试一次），
   且主聊天模型不为措辞额外调用（后台任务 / 必要告知路径维持原样）；
5. 最终回答之后不再冒出旧进度（回合结束后静默观察一段时间）。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.capability_runtime import build_runtime
from agent_core.contracts import AgentRequest, Scope
from agent_core.events import EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.prompts import PromptComposer, PromptContext
from agent_core.search_gate import build_search_gate
from agent_core import steward as steward_module
from agent_core.steward import StewardNarrator, StewardReporter, steward_line_issue
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

#: 允许出现在气泡里的阶段。
PUBLIC_PHASES = {
    "searching", "materials_found", "reading_source", "source_read", "redirect", "conflict",
}
#: 观察"最终回答之后会不会冒旧进度"的静默时长（秒）。
QUIET_SECONDS = 6.0
SILENCE = 0.05  # 只影响断言精度，不人为拖慢业务
#: 气泡与用户问题最多允许重合的连续字数。取 4 是为了放过"是什么""现在"这类
#: 常用说法，只抓"把问题整句搬进气泡"这种真泄漏。
QUESTION_OVERLAP_LIMIT = 4
#: 比对这个重合度时先丢掉的标点（只留实义字符）。
_PUNCTUATION = "，。！？、,.!?~～…\"'“”‘’（）()《》【】[]{}:：;；-—"


class Recorder:
    """把落库的过程消息收进内存（真实 sink 会写库，这里只做验收）。"""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    def __call__(self, scope, message, stable_key):
        self.messages.append({
            "at": time.monotonic(),
            "phase": str(message.get("phase") or ""),
            "content": str(message.get("content") or ""),
            "key": stable_key,
        })
        return True


async def run_turn(provider, registry, scope, question: str, *, system_prompt: str, db: Path):
    """跑一个真实回合，返回观测结果。"""
    recorder = Recorder()
    bus = InMemoryEventBus()
    deltas: list[str] = []

    async def on_delta(event) -> None:
        if event.type == EventType.CHAT_DELTA:
            deltas.append(str(event.data.get("delta") or ""))

    bus.subscribe(EventType.CHAT_DELTA, on_delta)
    gate = build_search_gate(provider)
    loop = AgentLoop(
        provider,
        registry,
        scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        search_gate=gate,
        event_bus=bus,
        steward=StewardReporter(StewardNarrator(provider), sink=recorder),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )
    # 读原文由补查路径直接执行（不进 _used_tools），单独数一下才看得见。
    reads = {"n": 0}
    original_execute = loop._execute_deterministic_tool

    async def counting_execute(spec, call, working, **kwargs):
        if str(getattr(spec, "name", "")) in {"search__read", "search__bilibili_video"}:
            reads["n"] += 1
        return await original_execute(spec, call, working, **kwargs)

    loop._execute_deterministic_tool = counting_execute
    started = time.monotonic()
    outcome = await loop.run(AgentRequest(question, scope=scope, system_prompt=system_prompt))
    finished = time.monotonic()
    reply_at = finished
    await asyncio.sleep(QUIET_SECONDS)
    return {
        "question": question,
        "outcome": outcome,
        "reply": outcome.reply,
        "seconds": finished - started,
        "messages": recorder.messages,
        "late": [m for m in recorder.messages if m["at"] > reply_at],
        "streamed": "".join(deltas),
        "used_tools": sorted(loop._used_tools),
        "reads": reads["n"],
        "route_calls": list(loop._route_calls),
        "loop": loop,
    }


def _squeeze(text: str) -> str:
    """去掉空白和标点，只留实义字符 —— 用来比对"气泡是不是在复述问题"。"""
    return "".join(
        ch for ch in str(text or "") if not ch.isspace() and ch not in _PUNCTUATION
    )


def _longest_common_run(left: str, right: str) -> int:
    """两个字符串的最长公共连续片段长度（滚一行动态规划，输入都很短）。"""
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for row in range(1, len(left) + 1):
        current = [0] * (len(right) + 1)
        char = left[row - 1]
        for column in range(1, len(right) + 1):
            if char == right[column - 1]:
                current[column] = previous[column - 1] + 1
                if current[column] > best:
                    best = current[column]
        previous = current
    return best


def bubble_leaks(content: str, question: str, used_tools: list[str]) -> list[str]:
    """气泡里不该出现的东西；空列表 = 干净。返回中文原因，方便直接打印。"""
    text = str(content or "")
    lowered = text.lower()
    leaks: list[str] = []
    run = _longest_common_run(_squeeze(text), _squeeze(question))
    if run >= QUESTION_OVERLAP_LIMIT:
        leaks.append(f"复述用户问题（连续 {run} 字）")
    if "http" in lowered or "www." in lowered:
        leaks.append("网址")
    if "search__" in lowered or "tool" in lowered:
        leaks.append("工具名")
    for name in used_tools:
        value = str(name or "").strip().lower()
        if value and value in lowered:
            leaks.append(f"工具名 {name}")
    if "traceback" in lowered or "exception" in lowered:
        leaks.append("内部错误")
    return leaks


def check_bubbles(result: dict) -> dict[str, bool]:
    messages = result["messages"]
    phases = [m["phase"] for m in messages]
    gaps = [
        messages[index + 1]["at"] - messages[index]["at"]
        for index in range(len(messages) - 1)
    ]

    return {
        "阶段都是公开阶段": all(phase in PUBLIC_PHASES for phase in phases),
        "最多一条首次 + 两条中段": len(messages) <= 3,
        "中段不超过两条": len(messages) - (1 if messages else 0) <= 2,
        "相邻至少 4 秒": all(gap >= 4.0 - SILENCE for gap in gaps),
        # 文案是模型现场生成的，所以校验改成"非空 + 过公开校验器"：
        # has_evidence=False 一并卡住"还没有证据就说找到了"这类编造。
        "文案非空且通过公开校验": all(
            m["content"].strip()
            and steward_line_issue(
                m["content"], phase=m["phase"], has_evidence=False,
            ) is None
            for m in messages
        ),
        "没有查询词/网址/工具名": all(
            not bubble_leaks(m["content"], result["question"], result["used_tools"])
            for m in messages
        ),
        # 中段措辞**就是**模型写的，不能再断言"没调模型"；要卡的是调用有边界：
        # 每个气泡最多生成一次 + 重试一次（StagePhraseGenerator.attempts=2）。
        "措辞生成调用次数有界": 0 <= result["_compose_calls"] <= 2 * len(messages),
        # 主聊天模型只为后台任务 / 必要告知说话：这条路径的次数必须维持原样。
        "主聊天模型不为措辞额外调用": result["_narrator_calls"] == 0,
        "最终回答之后没有旧进度": result["late"] == [],
    }


async def main() -> int:
    provider = OpenAICompatibleProvider.from_env()
    runtime = build_runtime(provider=provider, install_task_router=False)
    registry = runtime.registry
    db = Path("logs/stage_notifications_live.db")
    db.parent.mkdir(exist_ok=True)

    narrator_calls = {"n": 0}
    original_narrate = StewardNarrator.narrate

    async def counting_narrate(self, context):  # noqa: ANN001
        narrator_calls["n"] += 1
        return await original_narrate(self, context)

    compose_calls = {"n": 0}
    compose_texts: list[str] = []
    original_compose = steward_module.StagePhraseGenerator.compose

    async def counting_compose(self, context, *, recent=()):  # noqa: ANN001
        compose_calls["n"] += 1
        text = await original_compose(self, context, recent=recent)
        # 把"打算说什么 / 为什么没说"记下来：空串 = 生成失败、超时、不合格或与近期重复。
        compose_texts.append(text or "")
        return text

    # 中段气泡的措辞现在是模型现场生成的（这是设计），所以要数的不是"有没有调"，
    # 而是"调得有没有边界"：StagePhraseGenerator 每个气泡最多重试一次。
    # StewardNarrator.narrate 仍然只服务后台任务 / 必要告知，不该被进度路径用到。
    StewardNarrator.narrate = counting_narrate
    steward_module.StagePhraseGenerator.compose = counting_compose
    try:
        scenarios = [
            ("快速闲聊", "我今天上班好累啊", False),
            ("普通事实查询", "梁文谷是谁", True),
            ("需要原文核对", "梁文锋和梁文谷是什么", True),
            # 模型凭记忆就能答、但属于"时效事实"：回答前补查会真的检索 + 读原文，
            # 这是观察"取得材料 / 正在读原文"中段阶段的关键场景。
            ("回答前补查（时效）", "现在 DeepSeek 最新的模型是哪个版本", True),
        ]
        overall_ok = True
        for index, (title, question, expect_bubble) in enumerate(scenarios, start=1):
            scope = Scope("local-user", "web", f"stage-notify-live-{index}", "companion")
            system_prompt = PromptComposer().compose(PromptContext(
                scope=scope, profile="primary", registry=registry, output_language="zh",
            ))
            before = narrator_calls["n"]
            before_compose = compose_calls["n"]
            before_texts = len(compose_texts)
            result = await run_turn(
                provider, registry, scope, question, system_prompt=system_prompt, db=db,
            )
            result["_narrator_calls"] = narrator_calls["n"] - before
            result["_compose_calls"] = compose_calls["n"] - before_compose
            result["_compose_texts"] = compose_texts[before_texts:]

            print(f"\n===== {title}：{question} =====")
            print(
                f"status: {result['outcome'].status}  耗时: {result['seconds']:.2f}s"
                f"  措辞生成调用: {result['_compose_calls']} 次"
            )
            print(f"实际用到的工具: {result['used_tools'] or ['(无)']}")
            print(
                "补查自身检索次数: "
                f"{getattr(result['loop'], '_gate_search_calls', 0)}"
                f"  实际读原文次数: {result['reads']}"
            )
            for generated in result["_compose_texts"]:
                # 空串 = 模型失败 / 超时 / 不合格 / 与近期重复 —— 这条按设计不发。
                print(f"  模型写出来: {generated or '（空：未通过校验或超时，按设计不发）'}")
            for message in result["messages"]:
                offset = message["at"] - (result["messages"][0]["at"] if result["messages"] else message["at"])
                print(f"  进度[{message['phase']}] +{offset:.2f}s  {message['content']}")
            if not result["messages"]:
                print("  （零过程气泡）")
            dirty = [
                leak
                for message in result["messages"]
                for leak in bubble_leaks(
                    message["content"], question, result["used_tools"],
                )
            ]
            if dirty:
                print("  气泡泄漏点: " + "；".join(dirty))
            print("回复：", result["reply"][:220].replace("\n", " "))

            checks = check_bubbles(result)
            checks["是否出现气泡符合预期"] = bool(result["messages"]) is expect_bubble
            for name, passed in checks.items():
                print(("  PASS " if passed else "  FAIL ") + name)
                overall_ok = overall_ok and passed
        return 0 if overall_ok else 1
    finally:
        StewardNarrator.narrate = original_narrate
        steward_module.StagePhraseGenerator.compose = original_compose


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
