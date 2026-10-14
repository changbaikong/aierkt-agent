"""Independent A acceptance probes; synthetic inputs and temporary databases only.

Exit 1 means confirmed acceptance defects remain. Never uses live memory or API keys.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("acceptance_helpers", ROOT / "tests/test_search_mainloop.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

from agent_core.answer_composer import AnswerAuditor, safe_closure
from agent_core.contracts import EFFECT_READ, ProviderResponse, ToolSpec
from agent_core.loop import AgentLoop
from agent_core.search_orchestrator import EvidenceBundle


def review_payloads(provider):
    return [json.loads(request[1]["content"]) for request in provider.requests
            if str(request[0].get("content", "")).startswith("你是最终核查器")]


class FollowupProvider(helpers.ScriptedProvider):
    def __init__(self):
        super().__init__(["FOLLOWUP_FACT：Nova 新版价格是 19 元，适合本地方案。"],
                         review={"publishable": True, "issue": "", "used_source_ids": []})
        self.requested = False

    async def complete(self, messages, **kwargs):
        if kwargs.get("tools") and not self.requested:
            self.requested = True
            self.requests.append([dict(m) for m in messages])
            return ProviderResponse(message={"role": "assistant", "content": "", "tool_calls": [
                {"id": "extra-search", "type": "function", "function": {
                    "name": "search__web", "arguments": json.dumps({"query": "Nova 新版价格"})}},
                {"id": "local-info", "type": "function", "function": {
                    "name": "project__constraints", "arguments": "{}"}},
            ]}, usage={"prompt_tokens": 30, "completion_tokens": 20})
        return await super().complete(messages, **kwargs)


async def main():
    results = {}
    with tempfile.TemporaryDirectory(prefix="a-acceptance-") as directory:
        tmp = Path(directory)
        calls = []

        def search(arguments, scope):
            calls.append(arguments)
            followup = len(calls) > 1
            return {"status": "ok", "output": {"query": arguments.get("query"), "result_count": 1,
                "results": [{"title": "Nova", "url": "https://new.example/price" if followup else "https://old.example/intro",
                "snippet": "FOLLOWUP_FACT：Nova 新版价格是 19 元。" if followup else helpers.FACT_TEXT}]}}

        search_tool = ToolSpec("search__web", {"type": "function", "function": {
            "name": "search__web", "parameters": {"type": "object"}}}, search,
            effect=EFFECT_READ, deterministic_route_allowed=True)
        local_tool = ToolSpec("project__constraints", {"type": "function", "function": {
            "name": "project__constraints", "parameters": {"type": "object"}}},
            lambda a, s: {"status": "ok", "output": {"detail": "LOCAL_TOOL_MARKER：只支持本地部署"}},
            effect=EFFECT_READ)
        provider = FollowupProvider()
        loop = helpers.build_loop(tmp, provider, gate=helpers.fact_gate(provider), tools=(search_tool, local_tool))
        outcome = await loop.run("Nova 是谁")
        review = review_payloads(provider)[-1]
        evidence_text = json.dumps({k: v for k, v in review.items() if k not in {"task", "answer", "background"}}, ensure_ascii=False)
        results["followup_evidence_missing"] = {
            "defect": len(calls) == 2 and "FOLLOWUP_FACT" not in evidence_text,
            "actual_search_calls": len(calls),
            "audit_urls": [s["url"] for s in review["sources"]],
            "delivered_urls": [s["url"] for s in outcome.sources],
        }
        results["non_search_tool_continues"] = {
            "defect": False,
            "tool_executed": any(c.get("tool") == "project__constraints" and c.get("status") == "ok" for c in loop._route_calls),
            "main_answer_sees_result": "LOCAL_TOOL_MARKER" in json.dumps(helpers.main_answer_request(provider), ensure_ascii=False),
            "audit_sees_result": "LOCAL_TOOL_MARKER" in json.dumps(review, ensure_ascii=False),
        }
        bundle = EvidenceBundle("Nova", "fact_search", materials=({"text": "RAW_SNIPPET_MARKER：宣传摘要而非已核实事实"},),
                                materials_unverified=True)
        fallback = safe_closure(bundle)
        results["unverified_material_published_by_fallback"] = {"defect": "RAW_SNIPPET_MARKER" in fallback, "reply": fallback}

        budget_provider = helpers.ScriptedProvider(["Nova 是研究机构。"])
        budget_loop = helpers.build_loop(tmp, budget_provider, gate=helpers.fact_gate(budget_provider),
                                        tools=(helpers.search_spec(),), max_total_tokens=1)
        await budget_loop.run("Nova 是谁")
        coverage_count = sum(any(str(m.get("content", "")).startswith("你是检索材料整理器") for m in r)
                             for r in budget_provider.requests)
        results["aux_call_runs_without_budget"] = {"defect": coverage_count > 0,
                                                   "total_budget": 1, "coverage_calls": coverage_count}

        usage_provider = helpers.ScriptedProvider(["可以先体验。"], verdict={"needs_search": False, "reason": "可先讨论"},
                                                   usage={"prompt_tokens": 123, "completion_tokens": 45})
        usage_loop = helpers.build_loop(tmp, usage_provider, gate=helpers.fact_gate(usage_provider), tools=(helpers.search_spec(),))
        await usage_loop.run("Nova 值得入手吗？")
        intent_entries = [c for c in usage_loop._turn_timer.ledger.calls if c["stage"] == "intent"]
        entry = intent_entries[0]
        results["actual_intent_usage_lost"] = {"defect": entry["input_tokens"] != 123 or entry["output_tokens"] != 45,
                                               "reported": {"input": 123, "output": 45}, "ledger": entry}

        tail_provider = helpers.ScriptedProvider()
        long_answer = "已知材料。" * 520 + "UNCHECKED_TAIL_MARKER：未经证据支持的尾段。"
        await AnswerAuditor(tail_provider).review("详细分析 Nova", long_answer, {"facts": [], "sources": []})
        checked = review_payloads(tail_provider)[-1]["answer"]
        results["answer_tail_not_reviewed"] = {"defect": "UNCHECKED_TAIL_MARKER" not in checked,
                                              "candidate_chars": len(long_answer), "reviewed_chars": len(checked)}
        background = AgentLoop._audit_background([
            {"role": "system", "content": "角色说明" * 300 + "MEMORY_TAIL_MARKER：方案仍待决定"},
            {"role": "user", "content": "HISTORY_MARKER：方案 A 只是一项建议"},
            {"role": "assistant", "content": "收到"},
            {"role": "user", "content": "Nova 值得入手吗？"},
        ])
        results["task_background_lost_at_audit"] = {"defect": "MEMORY_TAIL_MARKER" not in background and "HISTORY_MARKER" not in background}

    output = ROOT / "logs/search-mainloop-acceptance-edges.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if any(row["defect"] for row in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
