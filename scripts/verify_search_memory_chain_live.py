"""真实记忆链验收：召回 → 搜索 → 主回答 → 最终核查 → 发布。

与 verify_search_mainloop_live.py 的区别：这一条走**真实记忆会话**（临时记忆库 +
MemoryAgentSession 的召回与提示词组装），不是把记忆手工塞进 system 消息。
临时库落在系统临时目录，不碰生产记忆文件，也不启用提炼与云同步。

用法（项目根目录）：
    venv/Scripts/python.exe scripts/verify_search_memory_chain_live.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.capability_runtime import build_runtime
from agent_core.contracts import AgentRequest, Scope
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.memory_agent import MemoryAgentSession
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_writes import MemoryWriter
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.prompts import PromptComposer
from agent_core.search_gate import build_search_gate
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

#: 只有临时记忆库里有这条；答案里出现它，才说明"召回真的参与了搜索回答"。
RECALL_MARKER = "记忆标记-MEMCHAIN：用户在做本地聊天 Agent 项目，预算五千元，已选定方案 A。"


class NoEmbeddings:
    """不接向量服务：FTS/热区召回足够验证链路，避免验收依赖外部嵌入接口。"""

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("embeddings disabled for acceptance")


class RecordingProvider:
    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.calls: list[dict[str, Any]] = []

    async def complete(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": bool(kwargs.get("tools"))})
        return await self.inner.complete(messages, **kwargs)

    async def stream_complete(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": bool(kwargs.get("tools"))})
        return await self.inner.stream_complete(messages, **kwargs)

    def main_answer_calls(self) -> list[dict[str, Any]]:
        return [
            call for call in self.calls
            if call["tools"]
            and not any(str(m.get("content") or "").startswith("你是") for m in call["messages"] if m.get("role") == "system")
        ]

    def review_payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        for call in self.calls:
            systems = [str(m.get("content") or "") for m in call["messages"] if m.get("role") == "system"]
            if not any(text.startswith("你是最终核查器") for text in systems):
                continue
            for message in reversed(call["messages"]):
                if message.get("role") != "user":
                    continue
                try:
                    payloads.append(json.loads(str(message.get("content") or "{}")))
                except ValueError:
                    pass
                break
        return payloads


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("agent_core").setLevel(logging.ERROR)

    real_provider = OpenAICompatibleProvider.from_env()
    provider = RecordingProvider(real_provider)
    runtime = build_runtime(provider=provider, install_task_router=False)
    registry = runtime.registry
    gate = build_search_gate(provider)
    if gate is None:
        print("搜索门未开启（SEARCH_GATE_ENABLED=false），无法验收")
        return 2

    checks: dict[str, bool] = {}
    # Windows 上 SQLite/Qdrant 连接可能还在关闭路上，清理失败不该把验收判成失败。
    with tempfile.TemporaryDirectory(prefix="memory-chain-", ignore_cleanup_errors=True) as directory:
        tmp = Path(directory)
        repositories = initialize_memory_repositories(tmp / "memory.db", tmp / "vectors")
        writer = MemoryWriter(repositories, NoEmbeddings())
        retriever = MemoryRetriever(repositories, NoEmbeddings())

        scope = Scope("memory-accept", "web", f"memchain-{time.strftime('%H%M%S')}", "companion")
        # 记忆里刻意带上与问题重叠的 ASCII 词（budget/plan A）：FTS 对中文长句的
        # 分词口径不可靠，验收要测"召回进回答"，不该被分词器卡住。
        writer.save_fact(
            scope,
            f"{RECALL_MARKER} budget 五千元，plan A 已选定。",
            confidence=0.95,
            visibility="shared",
        )
        writer.append_message(
            scope, "user",
            f"背景补充 {RECALL_MARKER}（上一条是刚才说的项目情况）",
        )
        writer.append_message(scope, "assistant", "记下了：预算五千元、已选定 plan A。")
        question = "查一下 Ollama 本地部署大模型的最新方案，结合我的项目 budget 和 plan A 给出建议。"
        context = retriever.build_context(scope, question)

        bus = InMemoryEventBus()
        deltas: list[str] = []
        timeline: list[str] = []

        async def sink(event: Event) -> None:
            if event.type == EventType.CHAT_DELTA:
                deltas.append(str(event.data.get("delta") or ""))
                timeline.append("chat.delta")
            elif event.type == EventType.STEWARD_PROGRESS:
                timeline.append("steward.progress")

        bus.subscribe(EventType.CHAT_DELTA, sink)
        bus.subscribe(EventType.STEWARD_PROGRESS, sink)
        loop = AgentLoop(
            provider, registry, scope,
            run_store=SQLiteRunStore(tmp / "core.db"),
            confirmation_store=SQLiteConfirmationStore(tmp / "core.db"),
            search_gate=gate,
            event_bus=bus,
            routing_mode="model",
            legacy_shadow_enabled=False,
        )
        session = MemoryAgentSession(
            loop, writer, retriever,
            prompt_composer=PromptComposer(),
            output_language="zh",
            routing_mode="model",
        )

        started = time.monotonic()
        outcome = await session.run(AgentRequest(question, scope=scope))
        elapsed = round(time.monotonic() - started, 1)
        ledger = loop._turn_timer.ledger.snapshot()

        main_calls = provider.main_answer_calls()
        main_text = json.dumps(main_calls, ensure_ascii=False, default=str)
        review_payloads = provider.review_payloads()
        review_text = json.dumps(review_payloads, ensure_ascii=False, default=str)
        streamed = "".join(deltas)
        body_indexes = [index for index, item in enumerate(timeline) if item == "chat.delta"]
        progress_indexes = [index for index, item in enumerate(timeline) if item == "steward.progress"]

        checks["召回层拿到记忆"] = "MEMCHAIN" in str(context.injected_text)
        checks["召回进入主回答输入"] = "MEMCHAIN" in main_text
        checks["核查背景也拿到召回内容"] = bool(review_payloads) and "MEMCHAIN" in (
            review_payloads[-1].get("background") or ""
        )
        checks["确实联网取证"] = loop._gate_search_calls_total() > 0
        checks["有结构化来源"] = bool(outcome.sources)
        checks["正文只发布一次且与交付一致"] = bool(deltas) and streamed == outcome.reply
        checks["正文在进度之后发布"] = (not body_indexes) or (not progress_indexes) or min(body_indexes) > max(progress_indexes)
        checks["账本含整理/主回答/核查"] = {"coverage", "main_answer", "final_review"} <= set(ledger["stages"])
        checks["记忆写在临时库而不是生产库"] = (
            str(repositories.db_path).startswith(str(tmp)) if hasattr(repositories, "db_path") else True
        )
        checks["回答用了项目背景"] = any(
            token in outcome.reply
            for token in ("五千", "5000", "预算", "budget", "项目", "方案 A", "方案A", "plan A", "planA")
        )
        checks["回答没有否定已知背景"] = not any(
            phrase in outcome.reply
            for phrase in ("没有提供预算", "未提供预算", "预算未提供", "没有提到预算", "不知道你的预算")
        )

        report = {
            "checks": checks,
            "turn": {
                "status": outcome.status,
                "answer_status": outcome.answer_status,
                "reply": outcome.reply,
                "sources": [dict(source) for source in outcome.sources],
                "search_calls": loop._gate_search_calls_total(),
                "page_reads": loop._gate_read_calls,
                "elapsed_s": elapsed,
                "first_token_ms": int(loop._first_body_ms),
                "ledger": ledger,
                "main_answer_requests": len(main_calls),
                "review_passes": len(review_payloads),
            },
            "memory_db": str(getattr(repositories, "db_path", "unknown")),
        }
        report_path = Path("logs/search-memory-chain-report.json")
        report_path.parent.mkdir(exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(checks, ensure_ascii=False, indent=2))
    print("reply:", outcome.reply[:400])
    print("sources:", json.dumps([s.get("url") for s in outcome.sources], ensure_ascii=False))
    print("report:", report_path)
    ok = all(checks.values())
    print("结论:", "全部通过" if ok else "有未通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
