"""LLM case 运行 harness：套 5/6/7 复用。

基于 evals/web_workflow_matrix.py 的 run_case，但去掉 web_api 全局状态，
直接用 AgentLoop + MemoryAgentSession + 假 memory（_Retriever/_Writer）跑 case。
支持单轮（case.prompt）和多轮（case.turns）。

每 case 独立临时 SQLite + 独立 RecordingProvider（不跨 case 复用，见 plan 风险③）。
真实跑需 OpenAICompatibleProvider.from_env() 的 API key（.env 配置）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from agent_core import AgentLoop, MemoryAgentSession, PromptComposer
from agent_core.contracts import AgentRequest, Scope
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.personas import ARCUEID_PERSONA
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

from .case import Case, Turn
from .fake_tools import build_registry
from .recording_provider import RecordingProvider
from .runtime import fixed_runtime_time


class _MemoryContext:
    def __init__(self) -> None:
        self.injected_text = ""
        self.hot: list[dict[str, Any]] = []
        self.warm: list[dict[str, Any]] = []
        self.cold_evidence: list[dict[str, Any]] = []
        self.citations: list[dict[str, Any]] = []
        self.metadata = {"layer_counts": {"hot": 0, "warm": 0, "cold": 0}}


class _Retriever:
    def build_context(self, scope: Scope, user_text: str) -> _MemoryContext:
        return _MemoryContext()


class _Writer:
    def append_message(self, scope: Scope, role: str, content: str, **extra: Any) -> None:
        return None


async def run_llm_case(case: Case, *, model: str | None = None) -> dict[str, Any]:
    """跑一条 LLM case，返回 result dict（status/reply/executions/responses/provider_requests）。

    单轮 case 用 case.prompt；多轮 case 用 case.turns 逐轮跑。
    每 case 独立临时目录 + 独立 provider，不跨 case 复用。
    model 参数当前未透传（from_env 从 .env 读 GEMINI_MODEL/OPENAI_API_MODEL），
    保留签名供 runner --model 后续扩展。
    """
    executions: list[dict[str, Any]] = []
    registry = build_registry(executions)
    provider = RecordingProvider(OpenAICompatibleProvider.from_env())

    with tempfile.TemporaryDirectory(prefix="llm-matrix-") as directory:
        db_path = Path(directory) / "agent.db"
        run_store = SQLiteRunStore(db_path)
        confirmation_store = SQLiteConfirmationStore(db_path)
        composer = PromptComposer()
        scope = Scope("matrix-user", "web", f"llm-{case.case_id.lower()}")

        loop = AgentLoop(
            provider,
            registry,
            scope,
            run_store=run_store,
            confirmation_store=confirmation_store,
        )
        session = MemoryAgentSession(
            loop,
            _Writer(),
            _Retriever(),
            prompt_composer=composer,
            persona_override=ARCUEID_PERSONA,
        )

        turns = case.turns or ((Turn(case.prompt),) if case.prompt else ())
        responses: list[dict[str, Any]] = []
        for turn in turns:
            request = AgentRequest(
                user_text=turn.message,
                scope=scope,
                ephemeral_suffix=fixed_runtime_time(),
            )
            outcome = await session.run(request)
            responses.append(
                {
                    "endpoint": turn.endpoint,
                    "request": turn.message,
                    "reply": outcome.reply,
                    "status": outcome.status,
                }
            )

        return {
            "case_id": case.case_id,
            "status": responses[-1]["status"] if responses else "error",
            "reply": responses[-1]["reply"] if responses else "",
            "executions": executions,
            "responses": responses,
            "provider_requests": provider.requests,
            "provider_base_url": provider.inner.base_url,
            "provider_model": provider.inner.model,
        }
