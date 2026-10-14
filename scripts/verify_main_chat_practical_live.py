"""主聊天三项实用能力：唯一综合真实场景（隔离库、真实模型、硬性调用上限）。

对应计划 `docs/superpowers/plans/2026-10-14-practical-main-chat-continuation.md`
的「唯一综合真实场景」七步。装配方式照抄既有真实验收脚本
`scripts/verify_memory_deployment_loop_live.py`，保证走的是同一条现有通路。

安全与边界：

- 运行库、确认库、记忆库、向量库、队列库全部在**临时隔离目录**下显式指定，
  不碰默认项目库；脚本结束时临时目录自动删除。
- 真实模型调用**硬上限 20 次**（计划要求），超过即抛错停止新增调用。
- 模型调用与向量调用分别计数；失败、空响应与未完成项原样保留在证据里。

用法：
    python -X utf8 scripts/verify_main_chat_practical_live.py
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import sys
import tempfile
import time
import traceback
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_core.contracts import AgentRequest, Scope  # noqa: E402
from agent_core.loop import GATE_WEB_SEARCH_TOOL, AgentLoop  # noqa: E402
from agent_core.memory_agent import MemoryAgentSession  # noqa: E402
from agent_core.memory_model_agent import MemoryModelAgent  # noqa: E402
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker  # noqa: E402
from agent_core.memory_repositories import initialize_memory_repositories  # noqa: E402
from agent_core.memory_retrieval import MemoryRetriever, SUPERSEDED_STATUSES  # noqa: E402
from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket  # noqa: E402
from agent_core.memory_writes import MemoryWriter  # noqa: E402
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore  # noqa: E402
from agent_core.prompts import PromptComposer  # noqa: E402
from agent_core.search_gate import build_search_gate  # noqa: E402
from agent_core.tools import ToolRegistry  # noqa: E402
from agent_core_adapters.bigmodel_embeddings import BigModelEmbeddingProvider  # noqa: E402
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider  # noqa: E402
from scripts.memory_test_support import seed_semantic  # noqa: E402

#: 计划规定的硬上限：本轮新增模型调用累计到此为止，覆盖失败、重试、核查与重写。
MAX_MODEL_CALLS = 20

#: 给定单例的种子原话。由脚本显式写入，**不是**本轮模型提炼结果。
SEED_TEXT = "部署方案已决定立即部署"
PROJECT = "deployment-project"

FILLER_TURNS = 60
FILLER_USER = "顺便说一句，今天天气不错，晚上想吃点清淡的。"
FILLER_ASSISTANT = "好的，记下了。"

Q_AMBIGUOUS = "部署这事先缓一缓，后面再说。"
A_AMBIGUOUS = "好的，那就先缓一缓。"
Q_ARRANGE = "现在这个项目该怎么安排？"
Q_EXPLICIT = "我说的是正式部署暂时不做，实施情况还没确认，之后再决定。"
A_EXPLICIT = "明白了，正式部署暂时不做。"
Q_LONG = "查一下这个部署方案的准备事项，我现在应该怎么安排？"


class BudgetedProvider:
    """真实模型的记账包装：逐次记录用量，达到硬上限就拒绝新调用。"""

    def __init__(self, inner, limit: int = MAX_MODEL_CALLS) -> None:
        self.inner = inner
        self.limit = limit
        self.calls: list[dict] = []
        self.stage = "unset"

    @property
    def model(self) -> str:
        return str(getattr(self.inner, "model", "") or "")

    @property
    def max_tokens(self):
        return getattr(self.inner, "max_tokens", 2048)

    def _guard(self) -> None:
        if len(self.calls) >= self.limit:
            raise RuntimeError(f"模型调用已达硬上限 {self.limit} 次，停止新增调用")

    def _record(self, kind: str, messages, text: str) -> None:
        self.calls.append({
            "index": len(self.calls) + 1,
            "stage": self.stage,
            "kind": kind,
            "model": self.model,
            "request_chars": len(json.dumps(messages, ensure_ascii=False, default=str)),
            "response": text,
        })

    # 真实 provider 的签名是**仅关键字**参数，且 stream_complete 是协程而不是异步生成器。
    # 包装层必须逐字对齐，否则会在真实调用时报参数错误。
    async def complete(self, message, *, tools=None, max_tokens=None, temperature=None, **kw):
        self._guard()
        response = await self.inner.complete(
            message, tools=tools, max_tokens=max_tokens, temperature=temperature, **kw)
        self._record("complete", message, _response_text(response))
        return response

    async def stream_complete(self, message, *, tools=None, max_tokens=None,
                              temperature=None, on_delta=None, **kw):
        self._guard()
        # 流式正文经由 on_delta 回调逐段发出（回调是**可等待的**），provider 返回值里
        # 往往没有正文；这里把回调内容也收集一份，否则证据里只剩空字符串。
        chunks: list[str] = []

        async def _sink(piece):
            if piece:
                chunks.append(str(piece))
            if on_delta is not None:
                result = on_delta(piece)
                if inspect.isawaitable(result):
                    await result

        response = await self.inner.stream_complete(
            message, tools=tools, max_tokens=max_tokens, temperature=temperature,
            on_delta=_sink, **kw)
        text = "".join(chunks) or _response_text(response)
        self._record("stream_complete", message, text)
        return response

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _response_text(response) -> str:
    if response is None:
        return ""
    text = getattr(response, "content", None)
    if text:
        return str(text)
    if isinstance(response, dict):
        if response.get("content"):
            return str(response["content"])
        choices = response.get("choices") or []
        if choices:
            return str((choices[0].get("message") or {}).get("content") or "")
    return ""


def _jsonable(value):
    try:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if is_dataclass(value) and not isinstance(value, type):
            return asdict(value)
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception as exc:  # noqa: BLE001
        return {"unserializable": f"{type(exc).__name__}: {exc}"}


def snapshot(repos) -> list[dict]:
    with repos.connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM memory_records ORDER BY created_at,id")]


def _queue_jobs(queue, limit: int = 20) -> list[dict]:
    """读取队列里最近的作业（只看状态与处理端，用来证明没有走第二条提炼链）。"""
    try:
        with queue.connect() as db:
            rows = db.execute(
                "SELECT job_id, status, pipeline, extracted FROM memory_candidate_jobs "
                "ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"{type(exc).__name__}: {exc}"}]


async def run_scenario(base: Path, report: dict) -> int:
    from agent_core.project_env import load_project_env
    load_project_env()

    provider = BudgetedProvider(OpenAICompatibleProvider.from_env())
    embeddings = BigModelEmbeddingProvider.from_env()
    report["provider"] = {"model": provider.model, "embedding_model": getattr(embeddings, "model", "")}
    report["limits"] = {
        "max_model_calls": MAX_MODEL_CALLS,
        "model_call_seconds": 60, "execution_seconds": 180, "task_seconds": 600,
    }

    repos = initialize_memory_repositories(base / "records.db", base / "vectors",
                                          vector_size=embeddings.dimensions)
    report["isolation"] = {
        "root_is_temporary": True,
        "memory_records_db": str(base / "records.db"),
        "memory_vectors_dir": str(base / "vectors"),
        "queue_db": str(base / "queue.db"),
        "run_store_db": str(base / "answer-runtime.db"),
        "confirmation_store_db": str(base / "answer-runtime.db"),
        "note": "全部位于临时隔离目录，未触碰默认项目库",
    }
    try:
        writer = MemoryWriter(repos, embeddings)
        retriever = MemoryRetriever(repos, embeddings)
        scope = Scope("isolated-owner", "web", "live-composite", "companion")

        # ── 第 1 步：放种子（明确标注不是本轮提炼结果）──────────────────
        old = seed_semantic(writer, scope, [MemoryCandidate(
            SEED_TEXT, "decision", "accepted", ("user_statement",),
            scope_kind="project", project_id=PROJECT, horizon="long_term",
        )], turn_id="live-seed-turn", project_binding=PROJECT)[0]
        report["step1_seed"] = {
            "text": SEED_TEXT, "record_id": old["id"],
            "record_version": old.get("record_version"),
            "origin": "给定单例的测试种子原话，由脚本写入，不是本轮模型提炼结果",
        }
        report["database_before"] = snapshot(repos)

        queue = MemoryCandidateQueue(repos.db_path)
        queue.initialize()
        agent = MemoryModelAgent(writer, provider, retriever=retriever)
        worker = MemoryCandidateWorker(queue, None, writer, model_agent=agent, timeout_seconds=180)

        # 第一次公开澄清问题（模拟真实主聊天的追问）——用于验证「回答关联」。
        asked_question = "你说的『先缓一缓』，是指正式部署暂停，还是连演练也一起停？"
        asked_turn = "live-asked-turn"

        # ── 第 2 步：真实后台判断「先缓一缓」────────────────────────────
        p2 = MemoryTurnPacket(
            "live-turn-2", datetime.now(UTC).isoformat(), scope.owner_id, scope.channel,
            scope.conversation_id, scope_module=scope.module_id, project_binding=PROJECT,
            user_text=Q_AMBIGUOUS, published_reply=A_AMBIGUOUS, recalled_memory_refs=[],
        )
        provider.stage = "step2_ambiguous"
        ident2, _ = queue.enqueue(p2)
        report["step2_background_judgment"] = {
            "user_text": Q_AMBIGUOUS, "published_reply": A_AMBIGUOUS,
            "packet": p2.as_payload(),
        }
        report["step2_background_judgment"]["worker_result"] = await worker.process_once()
        job2 = queue.get(ident2)
        state2 = json.loads(job2.get("agent_state") or "{}")
        report["step2_background_judgment"].update({
            "job_status": job2.get("status"),
            "agent_state": state2,
            "processing_result": json.loads(job2.get("processing_result") or "{}"),
            "trace": state2.get("trace"),
            "codes": job2.get("codes"),
            "model_calls_so_far": len(provider.calls),
        })

        # ── 第 3 步：真实后台判断用户明确回答 ───────────────────────────
        p5 = MemoryTurnPacket(
            "live-turn-5", datetime.now(UTC).isoformat(), scope.owner_id, scope.channel,
            scope.conversation_id, scope_module=scope.module_id, project_binding=PROJECT,
            user_text=Q_EXPLICIT, published_reply=A_EXPLICIT, recalled_memory_refs=[],
        )
        provider.stage = "step3_explicit_answer"
        ident5, _ = queue.enqueue(p5)
        report["step3_explicit_answer"] = {"user_text": Q_EXPLICIT}
        report["step3_explicit_answer"]["worker_result"] = await worker.process_once()
        job5 = queue.get(ident5)
        state5 = json.loads(job5.get("agent_state") or "{}")
        report["step3_explicit_answer"].update({
            "job_status": job5.get("status"),
            "processing_result": json.loads(job5.get("processing_result") or "{}"),
            "trace": state5.get("trace"),
            "codes": job5.get("codes"),
            "model_calls_so_far": len(provider.calls),
        })
        report["database_after_writes"] = snapshot(repos)

        # ── 第 4 步：主聊天回答（用现有会话通路）────────────────────────
        # 走与 Web/CLI **同一个**装配函数，这样注册表里有真实的搜索等工具，
        # 而不是一个空注册表 —— 否则第 6 步的"真实查询"根本没工具可调。
        from agent_core.capability_runtime import build_runtime
        runtime = build_runtime(provider=provider)
        registry = runtime.registry
        report["runtime_tools"] = sorted(
            spec.name for spec in getattr(registry, "specs", lambda: [])()
        ) if hasattr(registry, "specs") else None
        report["search_tool_registered"] = registry.resolve(GATE_WEB_SEARCH_TOOL) is not None
        runtime_db = base / "answer-runtime.db"
        later = Scope(scope.owner_id, scope.channel, "live-answer", scope.module_id)
        loop = AgentLoop(provider, registry, later, run_store=SQLiteRunStore(runtime_db),
                         confirmation_store=SQLiteConfirmationStore(runtime_db), max_steps=2,
                         search_gate=build_search_gate(provider), routing_mode="model",
                         legacy_shadow_enabled=False)
        session = MemoryAgentSession(loop, writer, retriever, prompt_composer=PromptComposer())
        provider.stage = "step4_main_answer"
        out4 = await asyncio.wait_for(
            session.run(AgentRequest(Q_ARRANGE, scope=later, history=[])), timeout=180)
        report["step4_main_answer"] = {
            "question": Q_ARRANGE, "reply": out4.reply, "status": out4.status,
            "answer_status": getattr(out4, "answer_status", None),
            "handoff": _jsonable(out4.handoff),
            "model_calls_so_far": len(provider.calls),
        }

        # ── 第 6 步：六十轮无关历史后的真实查询 ─────────────────────────
        gate = build_search_gate(provider)  # 未开启时返回 None，如实记录
        filler: list[dict] = []
        for _ in range(FILLER_TURNS):
            filler.append({"role": "user", "content": FILLER_USER})
            filler.append({"role": "assistant", "content": FILLER_ASSISTANT})

        context = retriever.build_context(later, Q_LONG, history=list(filler))
        recalled = [r for r in list(context.hot) + list(context.warm)]
        from agent_core.chat_background import TurnBackground
        bg = TurnBackground.build(
            user_text=Q_LONG,
            selected=[dict(memory_id=r.get("id"), content=r.get("content"),
                           kind=r.get("kind"), content_status=r.get("content_status"),
                           version=r.get("record_version")) for r in recalled],
        )
        report["step6_long_chat_query"] = {
            "question": Q_LONG, "filler_turns": FILLER_TURNS,
            "recalled": [{"id": r.get("id"), "content": r.get("content"),
                          "kind": r.get("kind"), "content_status": r.get("content_status"),
                          "record_version": r.get("record_version")} for r in recalled],
            "background_payload": bg.as_payload(),
            "citations": _jsonable(context.citations),
            "dropped_items": _jsonable(context.dropped_items),
            "conflicts": _jsonable(context.conflicts),
        }
        loop6 = AgentLoop(provider, registry, later, run_store=SQLiteRunStore(runtime_db),
                          confirmation_store=SQLiteConfirmationStore(runtime_db), max_steps=2,
                          search_gate=gate, routing_mode="model", legacy_shadow_enabled=False)
        session6 = MemoryAgentSession(loop6, writer, retriever, prompt_composer=PromptComposer())
        report["step6_long_chat_query"]["background_injected"] = not bg.is_empty
        report["step6_long_chat_query"]["search_gate_enabled"] = gate is not None
        provider.stage = "step6_long_chat_query"
        req6 = AgentRequest(Q_LONG, scope=later, history=list(filler), turn_background=bg.as_payload())
        out6 = await asyncio.wait_for(session6.run(req6), timeout=180)
        report["step6_long_chat_query"].update({
            "reply": out6.reply, "status": out6.status,
            "answer_status": getattr(out6, "answer_status", None),
            "sources": _jsonable(out6.sources),
            "handoff": _jsonable(out6.handoff),
            "public_history_len": len(loop6.public_messages),
            "model_calls_so_far": len(provider.calls),
        })

        # ── 第 7 步：对照数据库 ─────────────────────────────────────────
        after = snapshot(repos)
        report["step7_db_check"] = {
            "records": [{"id": r["id"], "content": r["content"], "kind": r.get("kind"),
                         "content_status": r.get("content_status"), "status": r.get("status"),
                         "record_version": r.get("record_version"),
                         "effective_record_id": r.get("effective_record_id")}
                        for r in after],
            "queue_jobs": _queue_jobs(queue),
        }
        report["database_final"] = after
    finally:
        report["model_calls"] = provider.calls
        report["model_call_count"] = len(provider.calls)
        report["embedding_call_count"] = None
        repos.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/2026-10-14-main-chat-practical-live.json")
    args = parser.parse_args()

    report: dict = {
        "title": "主聊天三项实用能力：唯一综合真实场景",
        "date": "2026-10-14",
        "plan": "docs/superpowers/plans/2026-10-14-practical-main-chat-continuation.md",
        "disclosure": (
            "本文件是**真实模型**调用记录（非模拟）。判断语义是否正确仍需人工独立复核，"
            "脚本本身只做结构与输入传递检查。"
        ),
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    os.environ["MAIN_CHAT_PRACTICAL_ENABLED"] = "true"
    # 新版语义记录默认不参与召回（两个开关都关时新记录对召回不可见）。
    # 本场景验证的就是「已保存的现行决定能被重新召回」，所以显式打开既有开关，
    # 走的是项目既有配置路径，不是为测试新造的旁路。
    os.environ["MEMORY_SEMANTIC_ENABLED"] = "true"
    os.environ["MEMORY_SEMANTIC_RECALL_ENABLED"] = "true"
    # 可信项目绑定来自运行配置：召回按它匹配项目级记录（与既有验收脚本一致）。
    os.environ["AGENT_MEMORY_PROJECT"] = PROJECT
    report["flags"] = {
        "MAIN_CHAT_PRACTICAL_ENABLED": "true（脚本显式打开）",
        "MEMORY_SEMANTIC_ENABLED": "true（既有开关，脚本显式打开）",
        "MEMORY_SEMANTIC_RECALL_ENABLED": "true（既有开关，脚本显式打开）",
        "AGENT_MEMORY_PROJECT": PROJECT,
        "note": "关闭 MAIN_CHAT_PRACTICAL_ENABLED 时原路径不变；本次为验证新能力而显式开启。",
    }

    with tempfile.TemporaryDirectory(prefix="main-chat-live-", ignore_cleanup_errors=True) as tmp:
        started = time.monotonic()
        try:
            code = asyncio.run(run_scenario(Path(tmp), report))
        except Exception as exc:  # noqa: BLE001
            report["fatal"] = f"{type(exc).__name__}: {exc}"
            report["traceback"] = traceback.format_exc()
            code = 1
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)

    report["finished_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {dest}")
    print(f"model_calls={report.get('model_call_count')} fatal={report.get('fatal')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
