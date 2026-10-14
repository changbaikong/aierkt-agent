"""Adapter that applies persistent memory around an AgentLoop turn."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

from .contracts import AgentOutcome, AgentRequest, Scope
from .chat_background import BackgroundMemory, BackgroundQuote, TurnBackground
from .context import ContextAssembler
from .loop import AgentLoop
from .memory_logging import configure_memory_logging
from .memory_retrieval import MemoryRetriever
from .memory_writes import MemoryWriter
from .memory_semantic import judgment_of
from .prompts import PromptComposer, PromptContext


LOGGER = logging.getLogger("agent_core.memory.agent")


class MemoryAgentSession:
    """Inject recalled memory and persist completed conversation turns."""

    def __init__(
        self,
        loop: AgentLoop,
        writer: MemoryWriter,
        retriever: MemoryRetriever,
        *,
        prompt_composer: PromptComposer | None = None,
        prompt_profile: str = "primary",
        persona_override: str = "",
        output_language: str = "zh",
        output_mode: str = "text",
        routing_mode: str = "legacy",
    ) -> None:
        configure_memory_logging()
        self.loop = loop
        self.writer = writer
        self.retriever = retriever
        self.prompt_composer = prompt_composer
        self.prompt_profile = prompt_profile
        self.persona_override = persona_override
        self.output_language = output_language
        self.output_mode = output_mode
        if routing_mode not in {"legacy", "model"}:
            raise ValueError(f"unsupported routing mode: {routing_mode}")
        self.routing_mode = routing_mode

    async def run(self, request: AgentRequest | str) -> AgentOutcome:
        if isinstance(request, str):
            request = AgentRequest(request, scope=self.loop.scope)
        scope = request.scope or self.loop.scope
        if scope != self.loop.scope:
            raise ValueError("memory session scope must match the AgentLoop scope")

        clean_history = [item for item in request.history if item.get("role") != "system"]
        # 召回要看得见本轮的必要历史：用户说的是「这个方案怎么落地」时，
        # 只有当前这句话的检索词什么都匹配不到。历史是**已经拿到的**，
        # 这里只做搬运，不会为召回再跑一次模型。
        context = await asyncio.to_thread(
            self.retriever.build_context,
            scope,
            request.user_text,
            history=clean_history,
        )
        if self.prompt_composer is None:
            system_prompt = "\n\n".join(
                part for part in (request.system_prompt, context.injected_text) if part
            )
        else:
            # 人格构件与工具构件由组装器拆开生产，再由上下文组装器按硬约束
            # （安全段先于注入段、人格锁最后）重新拼回一份 system prompt。
            parts = self.prompt_composer.parts(
                PromptContext(
                    scope=scope,
                    profile=self.prompt_profile,  # type: ignore[arg-type]
                    registry=self.loop.registry,
                    persona_override=(
                        request.persona_override
                        or request.system_prompt
                        or self.persona_override
                    ),
                    output_language=self.output_language,
                    output_mode=self.output_mode,  # type: ignore[arg-type]
                    routing_mode=self.routing_mode,  # type: ignore[arg-type]
                )
            )
            system_prompt = ContextAssembler.assemble_prompt(
                parts,
                injected_sections=(context.injected_text,),
            )
        user_metadata = {"attachments": request.attachment_metadata} if request.attachment_metadata else None
        await asyncio.to_thread(
            self.writer.append_message,
            scope,
            "user",
            request.user_text,
            metadata=user_metadata,
        )
        LOGGER.info(
            "组装上下文：hot=%d 条 warm=%d 条 cold=%d 条 引用=%d",
            len(context.hot), len(context.warm), len(context.cold_evidence), len(context.citations),
        )

        outcome = await self.loop.run(
            replace(
                request,
                scope=scope,
                history=clean_history,
                system_prompt=system_prompt,
                # 实施工作 A：用**现有召回结果**构造本轮关键背景，随请求一并交给循环。
                # 只搬运已经拿到的内容：这里不新增检索、不新增模型调用、不改提示顺序。
                turn_background=self._build_turn_background(request, context),
            )
        )
        outcome = replace(outcome, handoff=self._turn_handoff(context, outcome))
        if outcome.status == "completed" and outcome.reply:
            await asyncio.to_thread(self.writer.append_message, scope, "assistant", outcome.reply)
            LOGGER.info("助手回复已持久化 status=completed")
        return outcome

    def _build_turn_background(self, request: AgentRequest, context: MemoryContext) -> dict:
        """把这一轮的关键背景整理成可传递的 payload（实施工作 A）。

        范围严格限定在"已经收到的事实"：

        - 当前用户原话、已召回并带引用的记忆正文及状态、必要原话片段、背景缺口；
        - 待澄清事项**只在调用方显式授权时**才会出现在 ``request.turn_background`` 里，
          这里不自己去读后台疑点（那是实施工作 B 的授权入口）。
        - 调用方已经给了背景时，用它的待澄清事项与缺口，避免两处各拼一份。
        """
        pending = ()
        inherited_gaps: tuple[str, ...] = ()
        provided = TurnBackground.from_payload(request.turn_background)
        if not provided.is_empty:
            pending = provided.pending
            inherited_gaps = provided.gaps
        selected = [
            BackgroundMemory.from_mapping(item)
            for item in [*context.warm, *context.cold_evidence]
        ]
        quotes = [
            BackgroundQuote(role=item.get("role") or "", text=str(item.get("content") or ""))
            for item in (request.history or [])
            if item.get("role") in ("user", "assistant") and str(item.get("content") or "").strip()
        ][-4:]
        gaps: list[str] = list(inherited_gaps)
        if context.cold_evidence:
            gaps.append("较早的历史背景")
        if not selected:
            gaps.append("没有召回到相关记忆")
        background = TurnBackground.build(
            user_text=request.user_text,
            selected=selected,
            quotes=quotes,
            pending=pending,
            gaps=gaps,
        )
        LOGGER.info(
            "本轮关键背景：记忆=%d 原话=%d 待澄清=%d 缺口=%d",
            len(background.selected), len(background.quotes), len(background.pending), len(background.gaps),
        )
        return background.as_payload()

    def _turn_handoff(self, context: MemoryContext, outcome: AgentOutcome) -> dict:
        """本轮**实际用到**的东西：记忆引用、外部证据、工具终局状态、可信项目绑定。

        只搬运已经发生的事实：这里不会重新检索、也不会为提炼再跑一次工具 ——
        那等于给同一轮加一次副作用。工具终局只能来自执行内核（loop 记的），
        外层从回答文字里猜"工具成没成功"就是猜。
        """
        recalled = [
            {
                "memory_id": str(item.get("id") or ""),
                "kind": str(item.get("kind") or ""),
                "content_status": str(item.get("content_status") or ""),
                "excerpt": str(item.get("content") or "")[:200],
                "layer": str(item.get("layer") or ""),
                "version": int(item.get('record_version') or 1),
                "semantic_judgment": judgment_of(item),
            }
            for item in [*context.warm, *context.cold_evidence]
        ]
        evidence = [
            {
                "ref_id": str(item.get("url") or item.get("id") or ""),
                "summary": str(item.get("title") or item.get("snippet") or "")[:200],
                "outcome": "success",
                "evidence_level": str(item.get("evidence_level") or ""),
            }
            for item in (outcome.sources or ())
            if isinstance(item, dict)
        ]
        collector = getattr(self.loop, "turn_handoff", None)
        tool_data: dict = {}
        if callable(collector):
            try:
                tool_data = collector() or {}
            except Exception:  # noqa: BLE001 - 交接失败不影响本轮回答
                tool_data = {}
        return {
            "recalled_memory_refs": recalled,
            "evidence_refs": evidence,
            "tool_outcomes": list(tool_data.get("tool_outcomes") or ()),
            "project_binding": str(tool_data.get("project_binding") or ""),
            "completeness": "complete",
        }

    async def resume(self, action_id: str, decision: str) -> AgentOutcome:
        """Resume a confirmed paused loop and persist its final reply."""
        outcome = await self.loop.resume(action_id, decision)
        if outcome.status == "completed" and outcome.reply:
            await asyncio.to_thread(
                self.writer.append_message,
                self.loop.scope,
                "assistant",
                outcome.reply,
            )
            LOGGER.info("助手回复已持久化 status=completed")
        return outcome
