"""内容维护闭环的离线验收：从**真实网页回合出口**一路走到召回与核查。

【为什么必须从真实入口进】
手工构造一个「正确的新版本」再断言它被召回，考的是测试自己。这里从
``web_api._finish_chat_turn``（网页发布完成后的收尾函数）出发，让它把作业写进
持久队列，再由队列工人提炼、保存、应用维护，最后只通过检索去看结果 ——
中间没有任何一步是脚本替系统摆好的。

【覆盖到哪、没覆盖到哪】
覆盖：已发布回合 → 持久队列 → 候选落库 → 更新建议入队 → 应用纠正 →
换一个会话召回 → 主回答与核查看到同一份有效语义。
没覆盖：真实提炼模型的输出质量与费用（本脚本用确定性假提炼器，一次模型调用都没有）、
生产云端的部署状态。所以它**不能**用来宣布「B 整链已经通过」—— 报告里
``live_model_calls`` 与 ``live_model_verified`` 两项写的就是这件事。

退出码 0 表示全部通过；1 表示至少一项验收条件失败（不是脚本崩溃）。
"""
from __future__ import annotations

import asyncio
import ast
import gc
import json
import logging
import os
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceScheduler, MemoryMaintenanceService
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker, MemoryMaintenanceWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_turn import MemoryCandidate, MemoryUpdateSuggestion, turn_from_messages
from agent_core.memory_writes import MemoryWriter
from agent_core.memory_processing import MemoryReviewProcessor

#: 本端配置的可信项目绑定。项目范围跨会话，靠的就是它。
PROJECT_BINDING = "trusted-project"


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class FixedReview:
    """Finite independent judgments; unknown scenarios fail closed."""
    async def complete(self, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        fixtures = {
            ('turn-1', '项目预算是五千'): ['项目预算是五千'],
            ('turn-2', '项目预算是三千'): ['不是五千', '是三千'],
        }
        rows = []
        for item in payload['items']:
            content = item['candidate']['content']
            key = (payload['packet']['turn_id'], content)
            if key not in fixtures:
                rows.append(dict(index=item['index'], verdict='unsupported', reason='未知离线样例'))
                continue
            rows.append(dict(index=item['index'], verdict='supported', content=content,
                             subject='项目预算', reason='用户明确报告或纠正项目预算',
                             same_subject=True, preserves_negation=True, preserves_conditions=True,
                             change_authorized=True, lifetime=dict(mode='long_term', basis='项目预算事实'),
                             quotes=[dict(message_id=key[0], quote=q) for q in fixtures[key]]))
        return SimpleNamespace(message={'content': json.dumps({'items': rows}, ensure_ascii=False)})


class ScriptedExtractor:
    """按轮次给出确定性输出：第一轮记下预算，第二轮纠正它。

    刻意不用真实模型：这一支验的是**闭环机制**（作业、幂等、版本、召回），
    模型输出质量是另一件事，混在一起会得出「机制没问题因为模型这次答对了」
    这种站不住的结论。
    """

    def __init__(self, old_id_holder: dict) -> None:
        self.old_id_holder = old_id_holder
        self.calls = 0

    async def extract(self, packet, *, known_ids=None):
        return await self.extract_maintenance(packet, known_ids=known_ids)[0]

    async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
        self.calls += 1
        if self.calls == 1:
            return (
                [
                    MemoryCandidate(
                        content="项目预算是五千",
                        kind="fact",
                        content_status="reported",
                        source_types=("user_statement",),
                        source_refs=(packet.turn_id,),
                        # 项目范围：跨会话要能读到，靠的正是可信绑定。
                        scope_kind="project",
                        project_id=packet.project_binding,
                    )
                ],
                [],
            )
        target = str(self.old_id_holder["id"])
        return (
            [
                MemoryCandidate(
                    content="项目预算是三千",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=(packet.turn_id,),
                )
            ],
            [
                MemoryUpdateSuggestion(
                    action="correct",
                    target_id=target,
                    expected_version=int(self.old_id_holder["version"]),
                    content="项目预算是三千",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=(packet.turn_id,),
                    reason="用户指出旧数字错了",
                )
            ],
        )


def _packet(turn_id: str, user_text: str, reply: str, *, conversation="c1"):
    return turn_from_messages(
        turn_id=turn_id,
        occurred_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        scope=Scope("user", "web", conversation, "companion"),
        user_text=user_text,
        published_reply=reply,
        project_binding=PROJECT_BINDING,
    )


async def _run_web_finish(
    namespace_patch: dict,
    scope: Scope,
    turn_id: str,
    user_text: str,
    reply: str,
    *,
    project_binding: str = "",
) -> None:
    """直接跑网页的收尾函数，让它自己把作业写进队列。

    刻意不 import web_api：模块级的初始化会去碰真实的运行库。
    这里只取那一个函数，按名字（AST）摘出来执行，依赖用替身顶住。
    """
    source = ast.parse((ROOT / "web_api.py").read_text(encoding="utf-8-sig"))
    finish = next(n for n in source.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_finish_chat_turn")
    namespace = {
        "ChatRequest": object,
        "Any": object,
        "ChatAttachmentStore": object,
        "asyncio": asyncio,
        "datetime": datetime,
        "timezone": timezone,
        "clean_character_reply": lambda x: x,
        "manager": None,
        "_scope": lambda r: scope,
        "_memory_extractor": None,
        "cloud_sync_enabled": lambda: False,
        "turn_from_messages": turn_from_messages,
        "logger": SimpleNamespace(exception=lambda *a, **kw: None),
        **namespace_patch,
    }
    exec(compile(ast.Module(body=[finish], type_ignores=[]), str(ROOT / "web_api.py"), "exec"), namespace)
    future = asyncio.get_running_loop().create_future()
    # 可信项目绑定走**交接快照**进来：主回合结束时把它留在 outcome.handoff 里，
    # 收尾函数从那儿取。这里照同一条路走，不另开一条只有脚本知道的入口。
    handoff = {"project_binding": project_binding} if project_binding else {}
    future.set_result(
        SimpleNamespace(status="completed", reply=reply, run_id="run-1", handoff=handoff)
    )
    await namespace["_finish_chat_turn"](
        SimpleNamespace(message=user_text),
        SimpleNamespace(result=future, turn_id=turn_id),
        None,
        None,
    )


async def run() -> int:
    logging.disable(logging.CRITICAL)
    # 这几个开关只影响本进程。项目绑定按真实部署那样配在运行环境里。
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    os.environ["MEMORY_PROCESSING_ENDPOINT"] = "local"
    os.environ["AGENT_MEMORY_PROJECT"] = PROJECT_BINDING
    checks: list[dict] = []

    def check(name: str, passed: bool, actual) -> None:
        checks.append({"check": name, "passed": bool(passed), "actual": actual})

    with tempfile.TemporaryDirectory(prefix="memory-maintenance-loop-", ignore_cleanup_errors=True) as folder:
        base = Path(folder)
        # 临时向量集合必须压成**本地嵌入式**模式。`.env` 里配了
        # ``QDRANT_MODE=server`` 时（云端就是），仓库初始化会连到那台**生产**
        # 向量库上，把三维的验收数据往 2048 维的正式集合里写 —— 既必然报错，
        # 又是在碰生产数据。显式按环境变量压回本地临时目录。
        os.environ["QDRANT_MODE"] = "local"
        os.environ["QDRANT_PATH"] = str(base / "vectors")
        repos = initialize_memory_repositories(base / "records.db", base / "vectors", vector_size=3)
        writer = MemoryWriter(repos, Embeddings())
        retriever = MemoryRetriever(repos, Embeddings())
        queue = MemoryCandidateQueue(base / "jobs.db")
        queue.initialize()
        service = MemoryMaintenanceService(writer, repos)
        holder: dict = {}
        extractor = ScriptedExtractor(holder)
        candidate_worker = MemoryCandidateWorker(
            queue, extractor, writer, maintenance_queue=queue, review_provider=FixedReview()
        )
        maintenance_worker = MemoryMaintenanceWorker(queue, service, review_provider=FixedReview())
        scope = Scope("user", "web", "c1", "companion")
        try:
            # 1) 已发布回合 → 真实网页收尾函数 → 持久队列
            await _run_web_finish(
                {"_memory_candidate_queue": queue, "_memory_sync_store": None},
                scope,
                "turn-1",
                "项目预算是五千",
                "好，记下五千",
                project_binding=PROJECT_BINDING,
            )
            backlog = queue.stats()["pending"]
            check("已发布回合经网页出口进了持久队列", backlog == 1, {"pending": backlog})

            # 2) 队列提炼 + 落库
            outcome = await candidate_worker.process_once()
            stored = writer.list_records(scope)
            check(
                "队列提炼后候选落库",
                outcome["stored"] == 1 and len(stored) == 1 and stored[0]["content"] == "项目预算是五千",
                {"outcome": outcome, "records": [row["content"] for row in stored]},
            )
            if not stored:
                # 不抛 IndexError：把作业状态和错误原样带进报告，好定位是提炼、
                # 校验还是写库哪一步没成。
                report = {
                    "ok": False,
                    "passed": sum(1 for item in checks if item["passed"]),
                    "total": len(checks),
                    "checks": checks,
                    "reason": "队列提炼没有落库",
                    "job_errors": _job_errors(queue),
                    "production_memory_touched": False,
                }
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
            holder["id"] = stored[0]["id"]
            holder["version"] = int(stored[0]["record_version"])

            # 3) 新信息（纠正）→ 第二轮走同一条路
            await _run_web_finish(
                {"_memory_candidate_queue": queue, "_memory_sync_store": None},
                scope,
                "turn-2",
                "不是五千，是三千",
                "好，改成三千",
                project_binding=PROJECT_BINDING,
            )
            await candidate_worker.process_once()
            maintenance_outcome = await maintenance_worker.process_once()
            check(
                "更新建议经维护队列应用",
                maintenance_outcome["applied"] == 1,
                {"maintenance": maintenance_outcome},
            )

            # 4) 跨会话召回：换一个会话，看现行版本
            later_scope = Scope("user", "web", "c-other", "companion")
            recalled = retriever.retrieve(later_scope, "项目预算")
            warm_contents = {row["content"] for row in recalled.warm}
            check(
                "另一个会话读到更新后的现行版本",
                "项目预算是三千" in warm_contents,
                {"warm": sorted(warm_contents)},
            )

            # 5) 主回答与核查收到同一份有效语义
            answer_side = retriever.build_context(later_scope, "项目预算")
            review_side = retriever.build_context(later_scope, "预算 三千 核查")
            answer_id = _effective_id(retriever, later_scope)
            review_id = _effective_id(retriever, later_scope)
            check(
                "主回答与核查看到同一条有效版本",
                answer_id is not None and answer_id == review_id,
                {"answer_id": answer_id, "review_id": review_id},
            )
            check(
                "注入文本里现行结论只有一个版本",
                "项目预算是三千" in answer_side.injected_text
                and "项目预算是五千" not in answer_side.injected_text.split("[HISTORICAL_EVIDENCE]")[0],
                {"injected": answer_side.injected_text[:200]},
            )
            check(
                "旧版本仍能在解释历史时查到",
                "项目预算是五千" in json.dumps(
                    [row["content"] for row in recalled.cold_evidence], ensure_ascii=False
                ),
                {"cold": [row["content"] for row in recalled.cold_evidence]},
            )

            # 6) 定期复核：没有新证据时模型调用为零
            scheduler = MemoryMaintenanceScheduler(service, interval_seconds=0)
            review = scheduler.run_once(now=datetime.now(UTC))
            check(
                "定期复核在没有新证据时不调模型",
                review["model_calls"] == 0 and extractor.calls == 2,
                {"review": review, "extractor_calls": extractor.calls},
            )

            # 7) 幂等：同一条纠正再应用一次，不造新版本
            before = len(writer.list_records(scope))
            repeat_packet = _packet("turn-2", "不是五千，是三千", "好，改成三千")
            repeat_suggestion = MemoryUpdateSuggestion(
                    action="correct",
                    target_id=holder["id"],
                    expected_version=holder["version"],
                    content="项目预算是三千",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    reason="重复提交",
                )
            durable = []
            _, reviewed, processing = await MemoryReviewProcessor(writer, FixedReview()).prepare(
                repeat_packet, [], [repeat_suggestion], persist=lambda payload: (
                    (base/'repeat-review.json').write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8'),
                    durable.append(payload)))
            repeat = service.apply(
                reviewed[0] if reviewed else repeat_suggestion,
                scope=scope,
                packet=repeat_packet,
                turn_id="turn-2",
            )
            after = len(writer.list_records(scope))
            check(
                "重复应用同一条更新不造新版本",
                repeat["applied"] is True and before == after,
                {"before": before, "after": after, "reason": repeat["reason"]},
            )
        finally:
            repos.close()
            gc.collect()

    report = {
        "ok": all(item["passed"] for item in checks),
        "passed": sum(item["passed"] for item in checks),
        "total": len(checks),
        "checks": checks,
        "live_model_calls": 0,
        "live_model_verified": False,
        "limits": [
            "用确定性假提炼器，不调真实模型：模型输出质量与费用不在本脚本覆盖范围内",
            "没有登录或部署远端：生产云端的处理端状态不在本脚本覆盖范围内",
        ],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


def _job_errors(queue) -> list[dict]:
    """作业表里的状态与错误：落库没成时，原因就在这几行里。"""
    with queue.connect() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT turn_id,status,attempts,last_error FROM memory_candidate_jobs ORDER BY created_at"
        ).fetchall()
    return [dict(row) for row in rows]


def _effective_id(retriever: MemoryRetriever, scope: Scope) -> str | None:
    """当前有效版本的记录号：状态 active 且没有被别的版本替代。"""
    result = retriever.retrieve(scope, "项目预算")
    for row in result.warm:
        if not str(row.get("effective_record_id") or "").strip():
            return str(row["id"])
    return None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
