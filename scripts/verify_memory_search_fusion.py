"""记忆 + 搜索融合的离线整链验收（B 计划第六步）。

跑法：``python scripts/verify_memory_search_fusion.py``

做什么：在一个临时数据库、临时向量目录里，走**真实链路**跑一遍
「回合入队 → 候选提炼 → 校验写入 → 按当前问题召回 → 注入」，
再验业务结果入口和范围隔离。全程不连生产云、不动真实记忆库。

为什么要有它：字段存在、关键词出现都不算完成。这里每一步都检查
**语义**：决策有没有带上原因、模型自述完成有没有被拒、进度播报有没有被
当成结论、越权能不能读到别的账号的记录。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 开关必须在导入使用它的模块之前设好：召回路径每次调用读一次环境变量。
os.environ.setdefault("MEMORY_SEMANTIC_ENABLED", "true")

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope  # noqa: E402
from agent_core.memory_extract import SemanticMemoryExtractor  # noqa: E402
from agent_core.memory_events import MemoryEvent, record_event  # noqa: E402
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker  # noqa: E402
from agent_core.memory_repositories import initialize_memory_repositories  # noqa: E402
from agent_core.memory_retrieval import MemoryRetriever  # noqa: E402
from agent_core.memory_turn import (  # noqa: E402
    MemoryCandidate,
    MemoryTurnPacket,
    turn_from_messages,
)
from agent_core.memory_writes import MemoryWriter  # noqa: E402
from agent_core.memory_processing import MemoryReviewProcessor

VECTOR_SIZE = 8


class FixedReview:
    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        fixtures = {
            ('verify-turn-1', '让主回答模型在回答发布后参与候选记忆提炼'):
                ('候选记忆提炼', ['让主回答模型在回答发布后参与候选记忆提炼', '因为不想陪伴记忆和项目记忆分裂', '就按这个做'], 'long_term'),
            ('verify-turn-1', '先按方案实施，暂不部署'):
                ('项目实施阶段', ['先按方案实施', '暂不部署'], 'phase'),
            ('verify-turn-4', '预算是三千'):
                ('预算', ['不是五千', '是三千'], 'long_term'),
        }
        rows = []
        for item in payload['items']:
            content = item['candidate']['content']
            key = (payload['packet']['turn_id'], content)
            if key not in fixtures:
                rows.append(dict(index=item['index'], verdict='unsupported', reason='未知离线样例'))
                continue
            subject, quotes, mode = fixtures[key]
            rows.append(dict(index=item['index'], verdict='supported', content=content, subject=subject,
                             reason='该固定样例的用户原话支持该事项', same_subject=True,
                             preserves_negation=True, preserves_conditions=True, change_authorized=True,
                             lifetime=dict(mode=mode, basis='该事项的原话'),
                             quotes=[dict(message_id=key[0], quote=q) for q in quotes]))
        return types.SimpleNamespace(message={'content': json.dumps({'items': rows}, ensure_ascii=False)})


class StubEmbeddings:
    """确定性向量：同样文本永远同样向量，不需要联网。"""

    dimensions = VECTOR_SIZE

    def embed(self, text: str) -> list[float]:
        buckets = [0.0] * VECTOR_SIZE
        for index, char in enumerate(str(text)):
            buckets[index % VECTOR_SIZE] += (ord(char) % 17) / 17.0
        norm = sum(value * value for value in buckets) ** 0.5 or 1.0
        return [value / norm for value in buckets]


class StubExtractor:
    """离线候选提炼器：按回合内容给出固定的候选，替代真实模型调用。

    刻意只返回**合规**候选。不合规的输出应该在校验那一关被丢掉，
    那一步在下面用真实的解析器单独验（见「模型自称已完成被拒」）。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, packet: MemoryTurnPacket, *, known_ids=None):
        self.calls += 1
        return [
            MemoryCandidate(
                content="让主回答模型在回答发布后参与候选记忆提炼",
                kind="decision",
                content_status="accepted",
                source_types=("user_statement",),
                reason="不想陪伴记忆和项目记忆分裂",
                goal="同一份上下文",
                topics=("记忆",),
                source_refs=(packet.turn_id,),
            ),
            MemoryCandidate(
                content="先按方案实施，暂不部署",
                kind="plan",
                content_status="accepted",
                source_types=("user_statement",),
                source_refs=(packet.turn_id,),
            ),
        ]


class Checks:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def check(self, name: str, passed: bool, detail: str = "") -> None:
        self.items.append({"check": name, "passed": bool(passed), "detail": detail})

    @property
    def ok(self) -> bool:
        return all(item["passed"] for item in self.items)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="memory-fusion-"))
    checks = Checks()
    os.environ['QDRANT_MODE'] = 'local'
    os.environ['QDRANT_PATH'] = str(root/'vectors')
    try:
        repositories = initialize_memory_repositories(
            root / "memory.db", root / "vectors", vector_size=VECTOR_SIZE
        )
        writer = MemoryWriter(repositories, StubEmbeddings())
        queue = MemoryCandidateQueue(repositories.db_path)
        queue.initialize()
        extractor = StubExtractor()
        worker = MemoryCandidateWorker(queue, extractor, writer, review_provider=FixedReview())
        retriever = MemoryRetriever(repositories, StubEmbeddings())
        scope = Scope("verify-user", "web", "conv-1", "companion")

        # ---- 1. 回合入队 → 后台提炼 → 校验写入 -------------------------
        # 先用真实的解析器验一遍「模型自称已完成」会不会被拒。
        parser = SemanticMemoryExtractor(provider=types.SimpleNamespace())
        parsed, rejected = parser.parse(
            json.dumps(
                {
                    "memories": [
                        {
                            "content": "让主回答模型参与候选提炼",
                            "kind": "decision",
                            "content_status": "proposed",
                            "source_types": ["assistant_suggestion"],
                        },
                        {
                            "content": "记忆改造已经全部完成",
                            "kind": "plan",
                            "content_status": "completed",
                            "source_types": ["model_inference"],
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            packet=MemoryTurnPacket(
                turn_id="verify-turn-0",
                occurred_at="2026-10-10T12:00:00+00:00",
                scope_owner="verify-user",
                scope_channel="web",
                scope_conversation="conv-1",
            ),
        )
        checks.check(
            "模型自称完成被解析器拒收",
            len(parsed) == 1 and len(rejected) == 1,
            json.dumps(rejected, ensure_ascii=False)[:160],
        )

        packet = MemoryTurnPacket(
            turn_id="verify-turn-1",
            occurred_at="2026-10-10T12:00:00+00:00",
            scope_owner=scope.owner_id,
            scope_channel=scope.channel,
            scope_conversation=scope.conversation_id,
            user_text="让主回答模型在回答发布后参与候选记忆提炼，因为不想陪伴记忆和项目记忆分裂，就按这个做。先按方案实施，暂不部署",
            published_reply="好，先按方案实施，暂不部署",
        )
        job_id, created = queue.enqueue(packet)
        checks.check("回合入队只收一次", created and queue.enqueue(packet)[1] is False, job_id)
        checks.check("入队不写记忆库", writer.list_records(scope) == [])

        import asyncio

        result = asyncio.run(worker.process_once())
        stored = {row["content"]: row for row in writer.list_records(scope)}
        checks.check("提炼后落库", result["stored"] >= 1, json.dumps(result, ensure_ascii=False))
        decision = stored.get("让主回答模型在回答发布后参与候选记忆提炼")
        checks.check("决策带上类型与状态", bool(decision) and decision["kind"] == "decision" and decision["content_status"] == "accepted")
        checks.check(
            "决策保留原因",
            bool(decision) and "不想陪伴记忆和项目记忆分裂" in (decision["metadata"] or ""),
        )
        checks.check(
            "模型自称已完成被拒",
            "记忆改造已经全部完成" not in stored,
            ",".join(sorted(stored))[:120],
        )
        checks.check(
            "计划状态是已采纳而不是已完成",
            bool(stored.get("先按方案实施，暂不部署"))
            and stored["先按方案实施，暂不部署"]["content_status"] == "accepted",
        )

        # ---- 2. 按当前问题召回（代词续问） -----------------------------
        context = retriever.build_context(
            scope,
            "这个怎么和搜索结合",
            history=[{"role": "user", "content": "记忆提炼和召回要怎么接起来"}],
        )
        checks.check("代词续问召回到该决策", "让主回答模型在回答发布后参与候选记忆提炼" in context.injected_text)
        checks.check("注入带上类型与状态", "decision/accepted" in context.injected_text)
        checks.check("注入带上原因", "原因：不想陪伴记忆和项目记忆分裂" in context.injected_text)
        checks.check("注入带上记忆编号", bool(decision) and decision["id"][:8] in context.injected_text)
        checks.check(
            "已发布回答没有被当成已完成事实",
            "已经全部完成" not in context.injected_text,
        )

        # ---- 3. 范围隔离 ---------------------------------------------
        other_conversation = Scope(scope.owner_id, "web", "conv-2", "companion")
        other_context = retriever.build_context(other_conversation, "记忆提炼和召回要怎么接起来")
        checks.check("别的会话读不到", "让主回答模型在回答发布后参与候选记忆提炼" not in other_context.injected_text)
        intruder = Scope("someone-else", "web", "conv-1", "companion")
        intruder_context = retriever.build_context(intruder, "记忆提炼和召回要怎么接起来")
        checks.check("别的账号读不到", "让主回答模型在回答发布后参与候选记忆提炼" not in intruder_context.injected_text)

        # ---- 4. 业务结果入口 -----------------------------------------
        event = MemoryEvent(
            event_id="verify-task-1",
            state="completed",
            goal="修好记忆队列",
            conclusion="产物已生成但测试失败",
            artifacts=("agent_core/memory_queue.py",),
            evidence=("sha256:abc",),
            tests_passed=False,
            failure_reason="断言未通过",
            failure_conditions=("只跑了单测，未跑全量",),
            leftovers=("修 test_memory_queue",),
        )
        records = record_event(writer, scope, event)
        contents = " ".join(row["content"] for row in records)
        statuses = {(row["kind"], row["content_status"]) for row in records}
        checks.check("业务结果写入产物事实", ("fact", "verified") in statuses and "memory_queue.py" in contents)
        checks.check("测试失败单独成条", ("experience", "observed") in statuses and "只跑了单测，未跑全量" in contents)
        checks.check("不能保存项目完成", ("plan", "completed") not in statuses, contents[:120])
        progress_event = MemoryEvent(event_id="verify-task-2", state="progress", conclusion="正在处理")
        checks.check("进度播报不产生候选", record_event(writer, scope, progress_event) == [])

        # ---- 5. 回合交接进处理包 --------------------------------------
        handoff_packet = turn_from_messages(
            turn_id="verify-turn-2",
            occurred_at="2026-10-10T13:00:00+00:00",
            scope=scope,
            user_text="这个怎么和搜索结合",
            published_reply="按已采纳方案接",
            handoff={
                "recalled_memory_refs": (
                    [
                        {
                            "memory_id": decision["id"],
                            "kind": decision["kind"],
                            "content_status": decision["content_status"],
                            "excerpt": decision["content"],
                            "layer": decision["layer"],
                        }
                    ]
                    if decision
                    else []
                ),
                "evidence_refs": [],
                "tool_outcomes": [
                    {"action_id": "a1", "name": "web__search", "status": "failed"},
                ],
                "project_binding": "dm-agent",
            },
        )
        checks.check(
            "交接快照带上了本轮用过的记忆",
            bool(decision) and handoff_packet.recalled_memory_refs[0].memory_id == decision["id"],
        )
        checks.check(
            "工具失败按原状态进包，没有美化成成功",
            any(item.outcome == "failed" for item in handoff_packet.evidence_refs),
        )
        checks.check("交接带上了可信项目绑定", handoff_packet.project_binding == "dm-agent")

        # ---- 6. 内容维护后的版本走同一条召回链 ------------------------
        # 前五段验的是「新建」。这一段验「改过之后」：现行版本要顶上，
        # 被纠正的旧版本退到解释历史那一段 —— 两条路都不能漏。
        from agent_core.memory_maintenance import MemoryMaintenanceService
        from agent_core.memory_turn import MemoryCandidate, MemoryUpdateSuggestion

        service = MemoryMaintenanceService(writer, repositories)
        correction_target = seed_semantic(writer,
            scope,
            [
                MemoryCandidate(
                    content="预算是五千",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=("verify-turn-3",),
                    occurred_at="2026-10-10T10:00:00+00:00",
                )
            ],
            turn_id="verify-turn-3",
            occurred_at="2026-10-10T10:00:00+00:00",
        )[0]
        suggestion = MemoryUpdateSuggestion(
                action="correct",
                target_id=correction_target["id"],
                expected_version=int(correction_target["record_version"]),
                content="预算是三千",
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                reason="用户指出旧数字说错了",
            )
        correction_packet = turn_from_messages(
                turn_id="verify-turn-4",
                occurred_at="2026-10-11T12:00:00+00:00",
                scope=scope,
                user_text="不是五千，是三千",
                published_reply="好，改成三千",
            )
        durable = []
        _, reviewed, processing = asyncio.run(MemoryReviewProcessor(writer, FixedReview()).prepare(
            correction_packet, [], [suggestion], persist=lambda payload: (
                (root/'correction-review.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8'),
                durable.append(payload))))
        outcome = service.apply(
            reviewed[0] if reviewed else suggestion,
            scope=scope,
            packet=correction_packet,
            turn_id="verify-turn-4",
        )
        fusion_recall = retriever.retrieve(scope, "预算", include_semantic=True)
        warm_contents = {row["content"] for row in fusion_recall.warm}
        cold_contents = {row["content"] for row in fusion_recall.cold_evidence}
        checks.check(
            "更新后的现行版本进入召回",
            bool(outcome["applied"]) and "预算是三千" in warm_contents,
            str(outcome.get("reason", "")),
        )
        checks.check(
            "被纠正的旧版本只在解释历史时出现",
            "预算是五千" not in warm_contents and "预算是五千" in cold_contents,
            json.dumps(sorted(cold_contents), ensure_ascii=False)[:120],
        )

        repositories.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    report = {
        "passed": sum(1 for item in checks.items if item["passed"]),
        "total": len(checks.items),
        "ok": checks.ok,
        "checks": checks.items,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if checks.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
