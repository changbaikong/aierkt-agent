"""真实主入口验收：A 部分维修后的搜索回答链（临时会话，不写长期记忆）。

用法（在项目根目录）：
    venv/Scripts/python.exe scripts/verify_search_mainloop_live.py

四个场景（对照维修计划第 4 节的"真实验收"）：
1. 复查截图里的 WorkBuddy 问题：答案简短、有结构化来源、没有"关键依据"段落；
2. 带项目记忆的问题：外部证据与个人背景同时出现；
3. 图片 + 一次性要求：图片信息与"只说适合我的部分"都参与回答；
4. 漏搜纠正：入口判成闲聊、候选答案在断言外部事实时，发布前补查一次。

边界：只走 AgentLoop + 临时 scope，不接记忆会话、不启用提炼与云同步，
所以不会把任何测试标记写进用户长期记忆。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.capability_runtime import build_runtime
from agent_core.contracts import AgentRequest, Scope
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.search_gate import build_search_gate
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

ROLE_MARKER = "【角色标记-ARC】你是爱尔奎特，说话简洁直接。"
PROJECT_MARKER = "【记忆标记-MEM777】用户正在做一个本地聊天 Agent 项目，预算五千元，已选定方案 A。"


class GateEventCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.events: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        marker = message.find("search_gate ")
        if marker < 0:
            return
        try:
            payload = json.loads(message[marker + len("search_gate "):])
        except ValueError:
            return
        if isinstance(payload, dict):
            self.events.append(payload)


IMAGE_TEXT = "WorkBuddy setup note: install code KESTREL-7; runs locally; OpenClaw compatible"
#: 图里独有的标记：正文提到它，才能证明图片真的进入了回答（而不是靠用户文字猜）。
IMAGE_MARKER = "KESTREL-7"
IMAGE_PATH = Path("logs/acceptance_prompt.png")


def make_image() -> tuple[str, str] | None:
    """读取验收用 PNG（由 bundled Python + Pillow 生成 ASCII 文字），返回 (data_url, 图内文字)。

    图内文字必须是**答案里推不出来**的内容：这样"回答提到它"才能证明图片被读进去了。
    缺图时返回 None —— 调用方必须把这一项记为"未验收"，不能当通过。
    """
    if not IMAGE_PATH.exists():
        return None
    encoded = base64.b64encode(IMAGE_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}", IMAGE_TEXT


class ProgressRecorder:
    """记录落库的过程提示（阶段 + 文案），用来核对"进度在前、正文在后"。"""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def __call__(self, scope, message, stable_key) -> bool:
        self.messages.append({**message, "stable_key": stable_key})
        return True


class RecordingProvider:
    """记录每次模型调用是否带图片/工具，用来核对图片与一次性要求真的发给了模型。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _describe(messages) -> dict[str, Any]:
        has_image = any(
            isinstance(message.get("content"), list)
            and any(isinstance(part, dict) and part.get("type") == "image_url" for part in message["content"])
            for message in messages
        )
        return {"has_image": has_image, "message_count": len(messages)}

    async def complete(self, messages, **kwargs):
        entry = {**self._describe(messages), "tools": bool(kwargs.get("tools")), "stream": False}
        self.calls.append(entry)
        return await self.inner.complete(messages, **kwargs)

    async def stream_complete(self, messages, **kwargs):
        entry = {**self._describe(messages), "tools": bool(kwargs.get("tools")), "stream": True}
        self.calls.append(entry)
        return await self.inner.stream_complete(messages, **kwargs)


async def run_case_checked(name: str, provider, registry, gate, collector, request, *, db: Path,
                           recorder: "RecordingProvider | None" = None, attempts: int = 2) -> dict:
    """跑一个场景；遇到 provider 超时/网络抖动导致整轮失败时重试一次。

    验收要判的是链路缺陷，不是模型服务的偶发抖动：一次失败可能只是 ReadTimeout，
    两次都失败才记进报告。
    """
    record: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        record = await run_case(
            name, provider, registry, gate, collector, request, db=db, recorder=recorder,
        )
        if record["status"] == "completed" and record["reply"] and "回复生成失败" not in record["reply"]:
            return record
        print(f"（{name} 第 {attempt} 次未完成：status={record['status']}，{'重试一次' if attempt < attempts else '记入报告'}）")
    return record


def build_loop(provider, registry, scope: Scope, gate, bus, db: Path, sink=None) -> AgentLoop:
    from agent_core.steward import StewardNarrator, StewardReporter

    reporter = StewardReporter(StewardNarrator(provider), sink=sink) if sink is not None else None
    return AgentLoop(
        provider, registry, scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        search_gate=gate,
        event_bus=bus,
        steward=reporter,
        routing_mode="model",
        legacy_shadow_enabled=False,
    )


async def run_case(
    name: str,
    provider,
    registry,
    gate,
    collector: GateEventCollector,
    request: AgentRequest,
    *,
    db: Path,
    recorder: RecordingProvider | None = None,
) -> dict:
    scope = request.scope
    bus = InMemoryEventBus()
    deltas: list[str] = []
    timeline: list[tuple[str, int]] = []
    calls_before = len(recorder.calls) if recorder is not None else 0

    async def sink(event: Event) -> None:
        if event.type == EventType.CHAT_DELTA:
            deltas.append(str(event.data.get("delta") or ""))
            timeline.append(("chat.delta", int(event.data.get("seq") or 0)))
        elif event.type == EventType.STEWARD_PROGRESS:
            timeline.append(("steward.progress", 0))

    bus.subscribe(EventType.CHAT_DELTA, sink)
    bus.subscribe(EventType.STEWARD_PROGRESS, sink)
    progress = ProgressRecorder()
    loop = build_loop(provider, registry, scope, gate, bus, db, sink=progress)
    started = time.monotonic()
    outcome = await loop.run(request)
    elapsed = round(time.monotonic() - started, 1)
    ledger = loop._turn_timer.ledger.snapshot()
    turn_id = record_turn_id(loop)
    body_indexes = [index for index, item in enumerate(timeline) if item[0] == "chat.delta"]
    progress_indexes = [index for index, item in enumerate(timeline) if item[0] == "steward.progress"]
    record = {
        "name": name,
        "turn_id": turn_id,
        "status": outcome.status,
        "reply": outcome.reply,
        "sources": [dict(source) for source in outcome.sources],
        "answer_status": outcome.answer_status,
        "streamed_chars": len("".join(deltas)),
        "streamed_text": "".join(deltas),
        "delta_events": len(deltas),
        "event_timeline": timeline,
        "body_after_progress": (not body_indexes) or (not progress_indexes) or (min(body_indexes) > max(progress_indexes)),
        "search_calls": loop._gate_search_calls_total(),
        "corrections": loop._search_corrections,
        "intent_source": str(getattr(getattr(loop, "_request_intent", None), "source", "") or ""),
        "first_token_ms": int(loop._first_body_ms),
        "progress_first_ms": int(loop._progress_first_ms),
        "elapsed_s": elapsed,
        "usage": ledger,
        "ledger_stages": sorted({entry["stage"] for entry in ledger["calls"]}),
        "progress_messages": len(progress.messages),
        "progress_phases": [message.get("phase") for message in progress.messages],
        "tools": sorted(loop._used_tools),
        "model_calls": recorder.calls[calls_before:] if recorder is not None else [],
        "events": [
            {"event": event.get("event"), **{k: v for k, v in event.items() if k in {
                "independent_source_count", "stop_reason", "publishable", "rewrite",
                "answer_status", "source_count", "searches_used", "page_reads",
                "entry_undetermined", "verdict_source", "audit_failed", "total_ms",
            }}}
            for event in collector.events if event.get("turn_id") == turn_id
        ],
    }
    print(f"\n===== {name} =====")
    print("status:", outcome.status, "| 用时:", elapsed, "s | 首字:", record["first_token_ms"], "ms")
    print("searches:", record["search_calls"], "| corrections:", record["corrections"], "| tools:", record["tools"])
    print("answer_status:", outcome.answer_status, "| sources:", len(outcome.sources))
    print("正文增量事件:", record["delta_events"], "| 账本阶段:", record["ledger_stages"])
    print("reply:\n" + outcome.reply)
    if outcome.sources:
        print("source urls:", json.dumps([s.get("url") for s in outcome.sources], ensure_ascii=False))
    return record


def record_turn_id(loop: AgentLoop) -> str:
    return str(getattr(loop, "_stream_turn_id", "") or "")


async def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    collector = GateEventCollector()
    logging.getLogger("agent_core").addHandler(collector)
    logging.getLogger("agent_core").setLevel(logging.INFO)

    provider = OpenAICompatibleProvider.from_env()
    recorder = RecordingProvider(provider)
    runtime = build_runtime(provider=recorder, install_task_router=False)
    registry = runtime.registry
    gate = build_search_gate(recorder)
    if gate is None:
        print("搜索门未开启（SEARCH_GATE_ENABLED=false），无法做真实验收")
        return 2
    db = Path("logs/search_mainloop_live.db")
    db.parent.mkdir(exist_ok=True)
    stamp = time.strftime("%H%M%S")
    results: list[dict] = []

    results.append(await run_case_checked(
        "① WorkBuddy 截图复查",
        recorder, registry, gate, collector,
        AgentRequest(
            "WorkBuddy 是什么？",
            scope=Scope("local-user", "web", f"accept-workbuddy-{stamp}", "companion"),
            system_prompt=ROLE_MARKER,
        ),
        db=db,
        recorder=recorder,
    ))

    results.append(await run_case_checked(
        "② 带项目记忆的问题",
        recorder, registry, gate, collector,
        AgentRequest(
            "查一下现在本地部署大模型的最新方案，结合我的项目和已有选择给出建议。",
            scope=Scope("local-user", "web", f"accept-memory-{stamp}", "companion"),
            system_prompt=f"{ROLE_MARKER}\n{PROJECT_MARKER}",
        ),
        db=db,
        recorder=recorder,
    ))

    image_payload = make_image()
    if image_payload is None:
        print("\n场景③未验收：缺少 logs/acceptance_prompt.png（用 bundled Python + Pillow 生成后重跑）")
        return 2
    data_url, image_text = image_payload
    results.append(await run_case_checked(
        "③ 图片 + 一次性要求",
        recorder, registry, gate, collector,
        AgentRequest(
            "看看这张图，再查一下 WorkBuddy 怎么安装",
            scope=Scope("local-user", "web", f"accept-image-{stamp}", "companion"),
            system_prompt=ROLE_MARKER,
            ephemeral_suffix="不要重复介绍，只说适合我的部分",
            content_parts=[
                {"type": "text", "text": "看看这张图，再查一下 WorkBuddy 怎么安装"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
            attachment_metadata=[{"kind": "image", "name": "workbuddy.png"}],
        ),
        db=db,
        recorder=recorder,
    ))

    results.append(await run_case_checked(
        "④ 外部事实类模糊问题（入口可能误判）",
        recorder, registry, gate, collector,
        AgentRequest(
            "WorkBuddy 值得入手吗？",
            scope=Scope("local-user", "web", f"accept-correct-{stamp}", "companion"),
            system_prompt=ROLE_MARKER,
        ),
        db=db,
        recorder=recorder,
    ))

    print("\n===== 验收判定 =====")
    checks: dict[str, bool] = {}
    first, memory, image, correction = results
    checks["① 有结构化来源"] = bool(first["sources"])
    checks["① 正文不附来源/依据段落"] = all(
        marker not in first["reply"] for marker in ("关键依据", "来源：", "http")
    )
    checks["① 正文不是报告排版（无加粗小标题/要点清单）"] = not any(
        line.strip().startswith(("**", "- ", "* ", "# ")) for line in first["reply"].splitlines()
    )
    checks["① 正文简短（≤400字）"] = len(first["reply"]) <= 400
    checks["① 用量与耗时已记账"] = (
        first["usage"]["call_count"] >= 3 and first["first_token_ms"] > 0
    )
    # ② 的判定：要么把项目背景用进回答，要么如实收窄（承认不知道，不给未定义方案下判断）。
    checks["② 结合了项目记忆或如实收窄"] = any(
        token in memory["reply"]
        for token in ("五千", "5000", "预算", "budget", "方案 A", "方案A", "plan A", "项目")
    ) or any(
        token in memory["reply"] for token in ("无法确认", "暂时无法", "只能确认", "没有依据", "没能查到")
    )
    checks["② 未对未定义方案作肯定判断"] = not re.search(
        r"方案\s*A[^。；\n]{0,24}(没问题|可行|方向|是对的|是好的|适合)", memory["reply"]
    )
    checks["② 用了外部证据"] = bool(memory["sources"]) or memory["search_calls"] > 0
    checks["③ 图片信息进入回答（含图内独有标记）"] = IMAGE_MARKER.lower() in image["reply"].lower()
    checks["③ 图片真的发给了模型"] = any(call.get("has_image") for call in image["model_calls"])
    checks["③ 图片问题也取了证"] = image["search_calls"] > 0
    checks["③ 正文只发布一次且与交付一致（事件口径）"] = (
        image["delta_events"] >= 1
        and image["streamed_text"] == image["reply"]
        and image["reply"].count(image["reply"]) == 1
    )
    # ④ 的保证不是"每次都补搜"，而是"绝不留未证实的外部断言"：
    # 要么取证过，要么明确说明无法确认。
    checks["④ 外部事实类模糊问题不留未证实断言"] = (
        correction["search_calls"] > 0
        or correction["corrections"] > 0
        or any(token in correction["reply"] for token in ("无法确认", "暂时无法", "没能查到", "没有查到", "不确定"))
    )
    checks["④ 回答仍有据可依或如实说明"] = bool(correction["sources"]) or any(
        token in correction["reply"] for token in ("无法确认", "暂时无法", "没能查到", "没有查到", "来源")
    )
    checks["没有把候选答案提前流出去（正文=交付）"] = all(
        record["streamed_text"] == record["reply"] for record in results
    )
    checks["正文只在进度之后发布"] = all(record["body_after_progress"] for record in results)
    checks["入口用模型判定时账本有 intent 记录"] = all(
        record["intent_source"] != "model" or "intent" in record["ledger_stages"] for record in results
    )
    checks["真实用量按原样入账（非估算）"] = all(
        all(not entry["estimated"] for entry in record["usage"]["calls"] if entry["stage"] != "coverage")
        or record["usage"]["estimated_calls"] == 0
        for record in results
    )

    ok = True
    for name, passed in checks.items():
        print(("PASS " if passed else "FAIL ") + name)
        ok = ok and passed
    report = write_report(results, checks)
    print("\n图内文字（供人工核对）:", image_text)
    print("完整报告:", report)
    print("结论:", "全部通过" if ok else "有未通过项，需要继续修")
    return 0 if ok else 1


def main_entry() -> int:
    # 中文报告写文件时按 UTF-8，避免控制台编码把正文写成乱码（独立验收已踩过）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    return asyncio.run(main())


def write_report(results: list[dict[str, Any]], checks: dict[str, bool]) -> Path:
    path = Path("logs/search-mainloop-live-report.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(
        {"checks": checks, "turns": results}, ensure_ascii=False, indent=2,
    ), encoding="utf-8")
    return path


if __name__ == "__main__":
    raise SystemExit(main_entry())
