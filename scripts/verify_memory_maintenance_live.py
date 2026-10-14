"""内容维护链路的**真实模型**验收：关键样例各跑 3 次。

【跑的是什么】
用主回答模型（`.env` 里的 GEMINI_* 配置）真的调一次提炼，然后：
候选落库 → 更新建议入队 → 应用纠正 → 换一个会话召回。全程只用临时库和临时
向量集合，**不写生产记忆**。

【报的是什么】
每次调用的用量、耗时、候选与建议数量、拒绝原因；汇总给出完整结构输出率、
空响应数、失败数与状态错误数。命中率按样例分开数（真实模型有波动，
把「这次没提炼出来」和「提炼出来但状态错了」算成同一件事就没法查）。

【退出码】
0 = 没有失败、没有空响应、没有状态错误；1 = 出现过这三类里任意一类；
3 = 主回答模型配置缺失（缺配置不是通过）。

用法：``python scripts/verify_memory_maintenance_live.py [--repeat 3]``
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import (
    SemanticMemoryExtractor,
    build_semantic_extractor_from_env,
    semantic_max_token,
)
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_turn import HistoryItem, MemoryCandidate, MemoryRef, MemoryTurnPacket
from agent_core.memory_writes import MemoryWriter

PROJECT_BINDING = "live-acceptance-project"
#: 状态错误：模型把「还没定」写成了「用户已采纳」，或者替用户宣布已经完成。
BAD_STATUS_PAIRS = {("decision", "accepted"), ("plan", "completed"), ("fact", "verified")}


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


def _packet(turn_id, user_text, reply, *, history=(), refs=(), occurred=None):
    return MemoryTurnPacket(
        turn_id=turn_id,
        occurred_at=occurred or datetime.now(UTC).isoformat(timespec="milliseconds"),
        scope_owner="live-user",
        scope_channel="web",
        scope_conversation="live-conv",
        project_binding=PROJECT_BINDING,
        user_text=user_text,
        published_reply=reply,
        relevant_history=list(history),
        recalled_memory_refs=list(refs),
    )


#: 关键样例。每个样例给出「怎么问」和「必须看到什么」。
#: ``require_items`` 表示这一轮**必须**提炼出东西：像「嗯」和「先放着」这两轮，
#: 模型给空的正确答案就是空，不该当成失败；但纠正、替代、明确认同这几轮
#: 空手而归就是真的漏记。
def _cases():
    return [
        dict(
            name="M1_未说保存就自动形成候选",
            require_items=True,
            build=lambda refs: _packet("live-m1", "项目先不部署", "好，先不部署，其余照旧继续。"),
            forbidden={("plan", "completed")},
        ),
        dict(
            name="M2_含糊嗯不升级为已采纳",
            require_items=False,
            build=lambda refs: _packet(
                "live-m2",
                "嗯",
                "我建议三件事：统一记忆规则、先本地跑通再上云、把提炼放进后台队列。",
                history=[
                    HistoryItem(
                        "m2-a",
                        "assistant",
                        "我建议三件事：统一记忆规则、先本地跑通再上云、把提炼放进后台队列。",
                    )
                ],
            ),
            forbidden={("decision", "accepted"), ("plan", "accepted"), ("plan", "completed")},
        ),
        dict(
            name="M3_明确短句认同保留双方出处",
            require_items=True,
            build=lambda refs: _packet(
                "live-m3",
                "嗯，就这样",
                "好，以后回答先说结论。",
                history=[
                    HistoryItem("m3-a", "assistant", "以后回答先说结论可以吗"),
                    HistoryItem("m3-u", "user", "嗯，就这样"),
                ],
            ),
            forbidden={("plan", "completed"), ("fact", "verified")},
        ),
        dict(
            name="M4_纠正旧数字",
            require_items=True,
            build=lambda refs: _packet("live-m4", "不是五千，是三千", "好，改成三千。", refs=refs),
            forbidden={("plan", "completed"), ("fact", "verified")},
        ),
        dict(
            name="M5_明确替代",
            require_items=True,
            build=lambda refs: _packet("live-m5", "改用 B，不再用 A", "好，改成 B。", refs=refs),
            forbidden={("plan", "completed"), ("fact", "verified")},
        ),
        dict(
            name="M14_自述不算证据",
            require_items=False,
            build=lambda refs: _packet(
                "live-m14",
                "这个先放着",
                "我已经核对完毕，全部通过，可以直接上生产。",
            ),
            forbidden={("plan", "completed"), ("fact", "verified"), ("decision", "accepted")},
        ),
        dict(
            name="M12_寒暄情绪与长期原则分开",
            require_items=True,
            build=lambda refs: _packet(
                "live-m12",
                "早上好呀。今天有点累，先歇会儿。另外这个项目的日志一律先落盘再回传，别再改了。",
                "早上好。累就先歇会儿；日志那条我记下了：先落盘再回传。",
                history=[
                    HistoryItem("m12-u1", "user", "早上好呀"),
                    HistoryItem("m12-a1", "assistant", "早上好，今天状态怎么样？"),
                ],
            ),
            forbidden={("plan", "completed"), ("fact", "verified"), ("decision", "accepted")},
            expect=lambda candidates, suggestions, record: [
                problem
                for problem in (
                    # 长期原则要有明确依据地保留下来。
                    None
                    if any(
                        "日志" in candidate.content
                        for candidate in candidates
                        if candidate.kind in ("decision", "preference", "plan", "fact")
                    )
                    else "长期原则没提炼出来",
                    # 寒暄与当下情绪不能变成长期结论。
                    None
                    if not any(
                        any(marker in candidate.content for marker in ("早上好", "有点累"))
                        for candidate in candidates
                        if candidate.kind in ("decision", "preference", "plan")
                    )
                    else "寒暄或当下情绪被存成了长期结论",
                )
                if problem
            ],
        ),
    ]


async def _run_case(case, extractor, provider: "RecordingProvider", repeat, results):
    for index in range(repeat):
        with tempfile.TemporaryDirectory(
            prefix="memory-live-", ignore_cleanup_errors=True
        ) as folder:
            base = Path(folder)
            repos = initialize_memory_repositories(
                base / "records.db", base / "vectors", vector_size=3
            )
            writer = MemoryWriter(repos, Embeddings())
            retriever = MemoryRetriever(repos, Embeddings())
            service = MemoryMaintenanceService(writer, repos)
            scope = Scope("live-user", "web", "live-conv", "companion")
            record = {
                "case": case["name"],
                "repeat": index + 1,
                "ok": True,
                "problems": [],
                "actions": [],
                "candidates": [],
                "statuses": [],
                "usage": {},
                "latency_ms": 0,
                "script_calls": 0,
            }
            refs = []
            if case["name"].startswith(("M4", "M5")):
                seed_text = "项目预算是五千" if case["name"].startswith("M4") else "方案 A 已采纳"
                seed = seed_semantic(writer,
                    scope,
                    [
                        MemoryCandidate(
                            content=seed_text,
                            kind="fact" if case["name"].startswith("M4") else "decision",
                            content_status="reported" if case["name"].startswith("M4") else "accepted",
                            source_types=("user_statement",),
                            source_refs=("live-seed",),
                            scope_kind="project",
                            project_id=PROJECT_BINDING,
                            occurred_at="2026-10-10T09:00:00+00:00",
                        )
                    ],
                    turn_id="live-seed",
                    project_binding=PROJECT_BINDING,
                    occurred_at="2026-10-10T09:00:00+00:00",
                )[0]
                refs = [
                    MemoryRef(
                        seed["id"],
                        seed["kind"],
                        seed["content_status"],
                        seed["content"],
                        version=int(seed["record_version"]),
                    )
                ]
            packet = case["build"](refs)
            known_ids = [ref.memory_id for ref in refs]
            known_versions = {ref.memory_id: int(ref.version) for ref in refs}
            try:
                started = time.monotonic()
                candidates, suggestions = await extractor.extract_maintenance(
                    packet, known_ids=known_ids, known_versions=known_versions
                )
                record["latency_ms"] = int((time.monotonic() - started) * 1000)
            except Exception as exc:  # noqa: BLE001 - 失败要记进报告，不能吞
                record["ok"] = False
                record["problems"].append(f"提炼失败：{type(exc).__name__}")
                results.append(record)
                repos.close()
                continue

            record["script_calls"] = 1
            record["candidates"] = [f"{c.kind}/{c.content_status}" for c in candidates]
            record["statuses"] = [f"{c.kind}/{c.content_status}" for c in candidates]
            for suggestion in suggestions:
                record["actions"].append(
                    {
                        "action": suggestion.action,
                        "target": suggestion.target_id[:8],
                        "kind": suggestion.kind,
                        "content_status": suggestion.content_status,
                    }
                )
            if not candidates and not suggestions:
                record["empty"] = True
                if case["require_items"]:
                    # 这一轮本该提炼出东西却空手而归 —— 那是漏记，不是「确实没有」。
                    record["ok"] = False
                    record["problems"].append("空响应：这一轮本该提炼出内容却没提炼出来")
                else:
                    record["problems"].append("空响应（本样例允许为空）")

            for pair in case["forbidden"]:
                if any((c.kind, c.content_status) == pair for c in candidates):
                    record["ok"] = False
                    record["problems"].append(f"状态错误：模型给出 {pair[0]}/{pair[1]}")

            if case.get("expect"):
                # 逐样例的自定义断言（比如 M12 的「原则要留、寒暄不能留」）。
                problems = [item for item in case["expect"](candidates, suggestions, record) if item]
                if problems:
                    record["ok"] = False
                    record["problems"].extend(problems)

            # 真跑一遍应用：建议要能落库，纠正后的版本要能被召回看见。
            applied = []
            for suggestion in suggestions:
                outcome = service.apply(
                    suggestion,
                    scope=scope,
                    packet=packet,
                    turn_id=packet.turn_id,
                    project_id=PROJECT_BINDING,
                )
                applied.append(
                    {"action": suggestion.action, "applied": outcome["applied"], "reason": outcome["reason"]}
                )
            record["applied"] = applied
            if case["name"].startswith("M4") and suggestions:
                recalled = retriever.retrieve(
                    Scope("live-user", "web", "live-conv-2", "companion"),
                    "预算",
                    project_id=PROJECT_BINDING,
                )
                contents = {row["content"] for row in recalled.warm}
                record["recalled"] = sorted(contents)
                if applied and applied[0]["applied"] and not any("三千" in item for item in contents):
                    record["ok"] = False
                    record["problems"].append("纠正已应用，但召回里看不到三千")
            if case["name"].startswith("M5") and applied and not applied[0]["applied"]:
                record["problems"].append(f"替代未应用：{applied[0]['reason']}")
            record["usage"] = _usage(provider)
            results.append(record)
            repos.close()


class RecordingProvider:
    """把每次响应的用量抄下来。

    提炼器只把正文交给上层，用量留在响应对象上 —— 不抄一遍，「费用/额度」
    这件事在验收报告里就是空的，而计划要求它「可见」。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.usages: list[dict] = []

    async def complete(self, message, *, temperature=None, max_tokens=None):
        response = await self.inner.complete(
            message, temperature=temperature, max_tokens=max_tokens
        )
        usage = getattr(response, "usage", None)
        self.usages.append(dict(usage) if isinstance(usage, dict) else {})
        return response


def _usage(provider: RecordingProvider) -> dict:
    return provider.usages[-1] if provider.usages else {}


def _write_report(report: dict, out: str | Path | None) -> None:
    """报告打到 stdout，同时留一份到磁盘 —— 一次真实模型的用量与失败要能复核。"""
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if out:
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


async def main(repeat: int, out: str | Path | None = None) -> int:
    # 只影响本进程：新版语义记忆打开，否则新记录对召回本来就不可见，
    # 「纠正后能不能召回」会得到假阴性。
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    configured = build_semantic_extractor_from_env()
    if configured is None:
        print(json.dumps({"ok": False, "reason": "主回答模型配置缺失，未跑"}, ensure_ascii=False))
        return 3
    provider = RecordingProvider(configured.provider)
    extractor = SemanticMemoryExtractor(
        provider, temperature=configured.temperature, max_token=configured.max_token
    )
    results: list[dict] = []
    for case in _cases():
        await _run_case(case, extractor, provider, repeat, results)

    by_case: dict[str, dict] = {}
    for item in results:
        bucket = by_case.setdefault(item["case"], {"runs": 0, "ok": 0, "problems": []})
        bucket["runs"] += 1
        bucket["ok"] += 1 if item["ok"] else 0
        bucket["problems"].extend(item["problems"])
    failures = sum(1 for item in results if any(p.startswith("提炼失败") for p in item["problems"]))
    empties = sum(
        1
        for item in results
        if any(p.startswith("空响应：这一轮本该") for p in item["problems"])
    )
    optional_empties = sum(
        1 for item in results if any(p.startswith("空响应（本样例允许为空）") for p in item["problems"])
    )
    status_errors = sum(
        1 for item in results if any(p.startswith("状态错误") for p in item["problems"])
    )
    total_tokens = sum(
        int(item["usage"].get("total_tokens") or 0) for item in results
    )
    report = {
        "ok": failures == 0 and empties == 0 and status_errors == 0,
        "model": os.environ.get("GEMINI_MODEL", ""),
        "base_url": os.environ.get("GOOGLE_GEMINI_BASE_URL", ""),
        "output_budget_units": semantic_max_token(),
        "repeat": repeat,
        "calls": len(results),
        "transport_failures": failures,
        "empty_where_content_was_required": empties,
        "empty_where_empty_is_fine": optional_empties,
        "status_errors": status_errors,
        "total_tokens": total_tokens,
        "structural_success_rate": round(
            (len(results) - failures - empties) / len(results), 3
        )
        if results
        else 0.0,
        "by_case": {
            name: {"命中": bucket["ok"], "运行": bucket["runs"], "问题": bucket["problems"][:6]}
            for name, bucket in by_case.items()
        },
        "runs": results,
        "production_memory_touched": False,
    }
    _write_report(report, out)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--out", default=str(ROOT / "docs" / "2026-10-11-memory-maintenance-live-evidence.json"))
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.repeat, args.out)))
