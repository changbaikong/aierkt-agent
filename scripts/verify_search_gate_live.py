"""真实链路验收：搜索取证 → 回主循环 → 最终核查 → 结构化来源。

用法（在项目根目录）：
    venv/Scripts/python.exe scripts/verify_search_gate_live.py

验收点（对照维修计划 A1—A8）：
1. 搜索取到的证据以工具消息回到主循环，主回答保留角色/记忆/历史/图片/一次性要求；
2. 判定需要搜索时，系统经网关真实调用网页搜索（博查 + Bing），热梗补查 B站；
3. 读取最相关的原文（Jina Reader，走系统代理）；
4. 正文只在**最终核查通过后**发布一次，候选答案不以任何增量提前露出；
5. 来源走结构化字段（不是正文尾注），只保留最终答案实际用到的；
6. 闲聊那一轮不触发任何检索，也不调用证据核查；
7. 日志带触发原因、平台、证据链接、模型用量与分段耗时（search_gate logger），
   并聚合成触发率 / 平均独立来源数 / 单源回答率 / 追加搜索率。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.capability_runtime import build_runtime
from agent_core.contracts import AgentRequest, Scope
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.search_evidence import summarize_metrics
from agent_core.search_gate import build_search_gate
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider
from agent_core.memory_agent import MemoryAgentSession, PromptComposer  # noqa: F401 (组装一致性参考)


class _GateEventCollector(logging.Handler):
    """把 search_gate 的结构化事件收下来，供本脚本直接算观测指标。"""

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


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    collector = _GateEventCollector()
    logging.getLogger("agent_core").addHandler(collector)
    provider = OpenAICompatibleProvider.from_env()
    runtime = build_runtime(provider=provider, install_task_router=False)
    registry = runtime.registry

    bus = InMemoryEventBus()
    deltas: list[str] = []

    async def sink(event: Event) -> None:
        if event.type == EventType.CHAT_DELTA:
            deltas.append(str(event.data.get("delta") or ""))

    bus.subscribe(EventType.CHAT_DELTA, sink)

    gate = build_search_gate(provider)
    if gate is None:
        print("搜索门未开启（SEARCH_GATE_ENABLED）")
        return 2

    # 生产同款系统提示词：人格 + 工具规则 + 安全边界 + 运行时尾段
    from agent_core.prompts import PromptComposer, PromptContext

    scope = Scope("local-user", "web", "search-gate-live", "companion")
    system_prompt = PromptComposer().compose(PromptContext(
        scope=scope, profile="primary", registry=registry, output_language="zh",
    ))

    db = Path("logs/search_gate_live.db")
    db.parent.mkdir(exist_ok=True)
    loop = AgentLoop(
        provider,
        registry,
        scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        search_gate=gate,
        event_bus=bus,
        routing_mode="model",
        legacy_shadow_enabled=False,
    )

    from agent_core.contracts import AgentRequest

    outcome = await loop.run(AgentRequest(
        "梁文锋和梁文谷是什么", scope=scope, system_prompt=system_prompt,
    ))
    streamed = "".join(deltas)

    # 实际用到的检索后端从结构化事件里取（原始结果只用于追踪，不再写进 working）
    backends = [
        str(event.get("backend")) for event in collector.events
        if event.get("event") == "search" and event.get("backend")
    ]
    backends = list(dict.fromkeys(backends))

    print("\n===== 回复 =====")
    print(outcome.reply)
    print("===== 状态 =====")
    print("status:", outcome.status)
    print("route calls:", json.dumps(loop._route_calls, ensure_ascii=False))
    print("used tools:", sorted(loop._used_tools))
    print("backends:", backends or ["(no backend-tagged tool ran)"])
    print("search corrections:", loop._search_corrections)
    print("streamed chars:", len(streamed))

    # 第二轮：普通闲聊必须一次搜索都不发（普通聊天保护）。
    chat_scope = Scope("local-user", "web", "search-gate-live-chat", "companion")
    chat_loop = AgentLoop(
        provider, registry, chat_scope,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        search_gate=build_search_gate(provider),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )
    chat_outcome = await chat_loop.run(AgentRequest(
        "我今天上班好累啊", scope=chat_scope, system_prompt=system_prompt,
    ))
    print("\n===== 闲聊回合 =====")
    print("reply:", chat_outcome.reply[:200])
    print("tools:", sorted(chat_loop._used_tools), "corrections:", chat_loop._search_corrections)

    coverage = [event for event in collector.events if event.get("event") == "evidence_coverage"]
    independent = max((int(event.get("independent_source_count") or 0) for event in coverage), default=0)
    usage_events = [event for event in collector.events if event.get("event") == "turn_usage"]
    metrics = summarize_metrics(collector.events)
    print("\n===== 观测指标 =====")
    print(json.dumps(metrics, ensure_ascii=False))
    print("本轮证据覆盖:", json.dumps(coverage, ensure_ascii=False)[:600])
    print("本轮用量与首字:", json.dumps(usage_events[-1] if usage_events else {}, ensure_ascii=False)[:600])

    checks = {
        "回合完成": outcome.status == "completed",
        "搜索证据进入主回答（入口取证或模型自搜）":
            loop._search_bundle is not None or bool(loop._used_tools & {"search__web", "search__bilibili"}),
        "回复不含内部判定 JSON": "needs_search" not in outcome.reply,
        "回复不含未清洗的HTML标签": "<em" not in outcome.reply,
        "正文不拼接来源尾注": "来源：" not in outcome.reply and "http" not in outcome.reply,
        "来源走结构化字段或如实说明无法确认": (
            bool(outcome.sources) or "无法确认" in outcome.reply or "暂时无法" in outcome.reply
            or "没能查到" in outcome.reply or "没有查到" in outcome.reply
        ),
        "回答状态可解释": outcome.answer_status in {"answered", "partially_answered", "conflicted", "not_confirmed"},
        "至少两个独立来源或已如实说明只有单一来源": (
            independent >= 2 or outcome.answer_status in {"partially_answered", "conflicted", "not_confirmed"}
        ),
        "闲聊回合不触发检索也不核查": (
            not (chat_loop._used_tools & {"search__web", "search__bilibili"})
            and chat_loop._search_corrections == 0
        ),
        "本轮模型用量与首字时间已记账": bool(usage_events) and int(usage_events[-1].get("first_token_ms") or 0) > 0,
    }
    ok = True
    for name, passed in checks.items():
        print(("PASS " if passed else "FAIL ") + name)
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
