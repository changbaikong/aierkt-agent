"""Independent B acceptance probes. Uses temporary stores; never production memory.

Exit 1 means at least one required boundary failed, not a script crash.
"""
from __future__ import annotations

import asyncio
import ast
import gc
import json
import logging
import os
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.memory_test_support import seed_semantic
from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_turn import MemoryCandidate, MemoryRef, MemoryTurnPacket, apply_packet_budget
from agent_core.memory_writes import MemoryWriter
from cloud_memory.store import CloudMemoryStore
from cloud_memory.worker import CloudMemoryWorker


class ProjectReviewProvider:
    model = 'project-independent-fixture'
    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        rows = []
        for item in payload['items']:
            if payload['packet']['turn_id'] != 'project-job':
                rows.append({'index':item['index'],'verdict':'uncertain','reason':'没有独立预期'})
                continue
            rows.append(dict(index=item['index'],verdict='supported',content='以后项目采用方案B',
                subject='项目方案选择',reason='用户明确决定采用B',same_subject=True,preserves_negation=True,
                preserves_conditions=True,change_authorized=False,
                lifetime={'mode':'long_term','basis':'长期方案选择无期限'},
                quotes=[{'message_id':'project-job','quote':'以后项目采用方案B'}]))
        return SimpleNamespace(message={'content':json.dumps({'items':rows},ensure_ascii=False)},usage={})


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class FailedProvider:
    async def complete(self, *args, **kwargs):
        raise ConnectionError("synthetic transport failure")


def packet(turn="probe", **kwargs):
    return MemoryTurnPacket(
        turn_id=turn, occurred_at=datetime.now(UTC).isoformat(),
        scope_owner="acceptance-user", scope_channel="web", scope_conversation="c1",
        user_text="先不要决定是否采用，也不要部署。", published_reply="建议采用统一记忆方案。",
        **kwargs,
    )


def candidate(content="记忆系统统一规则", **kwargs):
    data = dict(content=content, kind="decision", content_status="accepted",
                source_types=("user_statement",), source_refs=("probe",), topics=("记忆",))
    data.update(kwargs)
    return MemoryCandidate(**data)


async def run():
    logging.disable(logging.CRITICAL)
    # These switches only affect this isolated process.
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    os.environ.pop("AGENT_MEMORY_PROJECT", None)
    results = []

    def check(name, passed, actual):
        results.append(dict(check=name, passed=bool(passed), actual=actual))

    with tempfile.TemporaryDirectory(prefix="memory-b-acceptance-", ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        repos = initialize_memory_repositories(base / "memory.db", base / "vectors", vector_size=3)
        writer = MemoryWriter(repos, Embeddings())
        scope = Scope("acceptance-user", "web", "c1", "companion")
        retriever = MemoryRetriever(repos, Embeddings())
        parser = SemanticMemoryExtractor(SimpleNamespace())
        try:
            forged = json.dumps({"memories": [{
                "content": "用户已经采纳统一记忆方案", "kind": "decision", "content_status": "accepted",
                "source_types": ["user_statement"], "source_refs": ["nonexistent-message"],
            }]}, ensure_ascii=False)
            parsed, rejected = parser.parse(forged, packet=packet())
            rows = writer.save_semantic_candidates(scope, parsed, turn_id="probe",packet=packet())
            check("unaccepted_suggestion_and_fake_reference_rejected", not rows,
                  {"stored": len(rows), "statuses": [r["content_status"] for r in rows], "rejected": rejected})

            queue = MemoryCandidateQueue(base / "jobs.db")
            queue.initialize()
            jid, _ = queue.enqueue(packet("network-failure"))
            worker = MemoryCandidateWorker(queue, SemanticMemoryExtractor(FailedProvider()), writer)
            outcome = await worker.process_once()
            job = queue.get(jid)
            check("local_transport_failure_is_retryable", job["status"] == "failed" and job["attempts"] > 0,
                  {"outcome": outcome, "status": job["status"], "attempts": job["attempts"]})

            store = CloudMemoryStore(base / "cloud.db")
            store.initialize()
            cloud_packet = packet("cloud-network-failure")
            store.enqueue_turn({"turn_id": cloud_packet.turn_id, "owner_id": scope.owner_id,
                "channel": scope.channel, "conversation_id": scope.conversation_id, "module_id": scope.module_id,
                "user_text": cloud_packet.user_text, "assistant_reply": cloud_packet.published_reply,
                "occurred_at": cloud_packet.occurred_at, "packet": cloud_packet.as_payload()})
            cloud_worker = CloudMemoryWorker(repos, store, writer, None,
                semantic_extractor=SemanticMemoryExtractor(FailedProvider()))
            await cloud_worker.run_once()
            check("cloud_transport_failure_is_retryable", cloud_worker.stats["failed"] == 1,
                  {"stats": cloud_worker.stats, "queue": store.queue_counts()})

            with_ref = packet("with-memory-ref", recalled_memory_refs=[MemoryRef("old", "decision", "accepted", "已有决策")])
            try:
                _, report = apply_packet_budget(with_ref)
                check("packet_budget_handles_recalled_memory", True, report)
            except Exception as exc:
                check("packet_budget_handles_recalled_memory", False, {"error": type(exc).__name__, "message": str(exc)})

            changed = replace(packet("network-failure"), user_text="不同内容")
            try:
                queue.enqueue(changed)
                check("same_turn_with_changed_payload_rejected", False, "silently reused original job")
            except ValueError:
                check("same_turn_with_changed_payload_rejected", True, "rejected")

            first = seed_semantic(writer, scope, [candidate("两账号同项目同回合")], turn_id="shared-turn")[0]
            other = Scope("another-user", "web", "c1", "companion")
            second = seed_semantic(writer, other, [candidate("两账号同项目同回合")], turn_id="shared-turn")[0]
            check("idempotency_is_scoped_by_owner", second["owner_id"] == other.owner_id and first["id"] != second["id"],
                  {"returned_owner": second["owner_id"], "same_record": first["id"] == second["id"]})

            long_text = "记忆搜索背景说明。" * 100 + "失败条件：不允许固定把云端负责提炼、本地负责召回。"
            long_scope = Scope(scope.owner_id, "web", "long-record", "companion")
            long_row = seed_semantic(writer, long_scope, [candidate(long_text, topics=())], turn_id="long")[0]
            long_result = retriever.retrieve(long_scope, "失败条件", token_budget=5000)
            check("long_record_tail_survives_full_retrieval", long_row["id"] in {r["id"] for r in long_result.items},
                  {"chars": len(long_text), "recalled": long_row["id"] in {r["id"] for r in long_result.items}})

            ranking_scope = Scope(scope.owner_id, "web", "ranking", "companion")
            constraint = seed_semantic(writer, ranking_scope, [candidate("禁止固定把云端负责提炼而本地负责召回")], turn_id="constraint")[0]
            writer.append_message(ranking_scope, "user", "天气闲聊" * 15)
            ranked = retriever.retrieve(ranking_scope, "记忆架构", token_budget=65)
            check("decision_budget_precedes_unrelated_hot_chat", constraint["id"] in {r["id"] for r in ranked.items},
                  {"constraint_recalled": constraint["id"] in {r["id"] for r in ranked.items},
                   "selected_kinds": [r["kind"] for r in ranked.items], "units": ranked.metadata["estimated_tokens"]})

            rollback_scope = Scope(scope.owner_id, "web", "rollback", "companion")
            semantic = seed_semantic(writer, rollback_scope, [candidate("新版记录应当在关闭后隐藏")], turn_id="rollback")[0]
            writer.save_memory(rollback_scope, "旧格式记忆回滚锚点", conflict_key="rollback-anchor", confidence=.95,
                metadata={"related_ids": [semantic["id"]]}, duration="long_term")
            rolled = retriever.retrieve(rollback_scope, "回滚锚点", include_semantic=False)
            check("semantic_recall_off_blocks_relation_expansion", semantic["id"] not in {r["id"] for r in rolled.items},
                  {"semantic_recalled": semantic["id"] in {r["id"] for r in rolled.items}, "expanded": rolled.metadata["expanded"]})

            project_scope = Scope(scope.owner_id, "web", "project-packet", "companion")
            project_packet = replace(packet("project-job"), user_text="以后项目采用方案B", scope_conversation=project_scope.conversation_id, project_binding="trusted-project")
            class ProjectExtractor:
                provider = ProjectReviewProvider()
                async def extract(self, p, **kwargs):
                    return [candidate(p.user_text, source_refs=(p.turn_id,), scope_kind="project", project_id=p.project_binding)]
            q2 = MemoryCandidateQueue(base / "project-jobs.db")
            q2.initialize()
            q2.enqueue(project_packet)
            await MemoryCandidateWorker(q2, ProjectExtractor(), writer).process_once()
            project_rows = writer.list_records(project_scope)
            check("worker_honors_trusted_packet_project_binding", project_rows[0]["scope_kind"] == "project",
                  {"packet_project": project_packet.project_binding, "stored_scope": project_rows[0]["scope_kind"]})

            # Execute the actual web finishing function with capture-only adapters.
            # Avoid importing web_api: its global setup would touch live runtime stores.
            source = ast.parse((ROOT / "web_api.py").read_text(encoding="utf-8-sig"))
            finish = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_finish_chat_turn")
            local_packets, cloud_packets = [], []
            class LocalCapture:
                def enqueue(self, p): local_packets.append(p)
            class CloudCapture:
                def enqueue_turn(self, *args, **kwargs): cloud_packets.append(kwargs)
            from agent_core.memory_turn import turn_from_messages
            namespace = {"ChatRequest": object, "Any": object, "ChatAttachmentStore": object,
                "asyncio": asyncio, "datetime": datetime, "timezone": timezone,
                "clean_character_reply": lambda x: x, "manager": None, "_scope": lambda r: scope,
                "_memory_candidate_queue": LocalCapture(), "_memory_sync_store": CloudCapture(),
                "_memory_extractor": None, "cloud_sync_enabled": lambda: True,
                "turn_from_messages": turn_from_messages,
                "logger": SimpleNamespace(exception=lambda *a, **kw: None)}
            exec(compile(ast.Module(body=[finish], type_ignores=[]), str(ROOT / "web_api.py"), "exec"), namespace)
            future = asyncio.get_running_loop().create_future()
            future.set_result(SimpleNamespace(status="completed", reply="已发布的回答", run_id="r", handoff={}))
            await namespace["_finish_chat_turn"](SimpleNamespace(message="合成验收消息"),
                SimpleNamespace(result=future, turn_id="web-dual"), None, None)
            check("web_chooses_only_one_processing_endpoint", len(local_packets) + len(cloud_packets) == 1,
                  {"local_jobs": len(local_packets), "cloud_uploads": len(cloud_packets)})
        finally:
            repos.close()
            gc.collect()
    report = {"ok": all(r["passed"] for r in results), "passed": sum(r["passed"] for r in results),
              "total": len(results), "checks": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
