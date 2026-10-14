"""上下文组装：把"这一回合要给模型看什么"拼成一个 AgentContext。

【在系统里的位置】
loop.py 决定"做什么"，这个文件决定"模型能看到什么"。它是提示词的唯一拼装点，
但**故意不含渠道策略**：微信/网页/CLI 各自的人设与渠道规则由入口传进来当参数，
而不是在这里 if-else 判断渠道。

【解耦构件的重新组装】
人格提示词（PersonaComposer）与工具调用提示词（ToolPromptComposer）各自独立生产，
以 PromptParts 交付到本模块，由 assemble_prompt 按硬约束拼回一份 system prompt：
安全段先于注入段、人格锁永远最后。换人格、换注册表都不影响拼装顺序。

【两种模式】
- build()      —— 完整模式：系统提示 + 记忆 + 渠道规则 + 最多 15 条干净历史。
                  用于正常对话，会给记忆检索留出时间。
- build_fast() —— 快速模式：只要渠道规则 + 最多 4 条历史，完全不碰记忆库。
                  用于"等不起检索"的轻量场景（前端快速回应）。

【为什么历史要"清洗"】
历史里混着带 tool_calls 的 assistant 消息和 system 消息。它们的格式合法性依赖
当时的工具上下文，直接重放会让模型看到一堆孤立的工具调用记录。所以这里按 role 白名单
重建，只保留 user/system 和"没有工具调用的 assistant"，并给每条挂上 created_at 供模型感知时间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .contracts import Scope
from .memory import MemoryAssembler, MemoryContext
from .prompts import PromptParts


@dataclass(frozen=True)
class AgentContext:
    """一次组装的结果。这就是"模型这一回合能看到的全部输入"。"""

    system_prompt: str  # 最终 system 消息（人设 + 记忆 + 渠道规则拼好的）
    history: list[dict[str, Any]]  # 清洗后的历史消息（不含工具调用）
    memory_text: str  # 单独留一份记忆文本，便于日志/监控台展示与调试
    memory_context: MemoryContext | None = None  # 记忆的完整结构（分层计数、引用等），快速模式下为 None
    ephemeral_instructions: str = ""  # 一次性指令，不写入长期记忆
    metadata: dict[str, Any] = field(default_factory=dict)  # 给日志/监控用的旁路信息


class ContextAssembler:
    def __init__(self, memory: MemoryAssembler) -> None:
        # 依赖注入：记忆装配器由外部传入，便于测试时换成假的。
        self.memory = memory

    @staticmethod
    def assemble_prompt(
        parts: PromptParts,
        *,
        injected_sections: Sequence[str] = (),
    ) -> str:
        """把解耦的人格构件、工具构件与注入段重新拼成一份 system prompt。

        这是解耦后的"重新组装"唯一入口，两条硬约束在这里兑现：
        1. 工具构件（含安全段）排在注入段之前——先立规矩，再读外部数据；
        2. 人格锁在 tail 里，永远是最后一段——离模型的下一句话最近，
           注入内容越权的影响最小。
        顺序不要在调用方手写，换人格、换注册表都从这里走。
        """
        sections = [parts.persona, parts.tool_rules]
        sections.extend(section.strip() for section in injected_sections if section and section.strip())
        sections.append(parts.tail)
        return "\n\n".join(sections)

    def build(
        self,
        scope: Scope,
        *,
        history: list[dict[str, Any]] | None = None,
        channel_rules: str = "",
        ephemeral: dict[str, str] | None = None,
        user_message: str = "",
        system_prompt: str = "",
        prompt_parts: PromptParts | None = None,
    ) -> AgentContext:
        # 1) 按作用域检索记忆。user_message 参与检索，所以"你现在忙什么"这类
        #    问题能召回相关记忆，而不是无脑塞最近几条。
        memory_context = self.memory.assemble(scope, user_message)
        # 2) system 消息的拼装：
        #    - 传入 prompt_parts（解耦模式）：人格/工具构件在本组装器内按硬约束
        #      重新拼装，记忆与渠道规则作为注入段夹在工具构件之后、人格锁之前；
        #    - 传入 system_prompt（整段模式）：行为与解耦前一致，三段直接拼接。
        #    渠道规则放最后（整段模式）或注入段末尾（解耦模式），让它对模型行为的约束优先级最高。
        if prompt_parts is not None:
            sections = [
                self.assemble_prompt(
                    prompt_parts,
                    injected_sections=(memory_context.injected_text, channel_rules),
                )
            ]
        else:
            sections = [part for part in (system_prompt, memory_context.injected_text, channel_rules) if part]
        # 3) 一次性指令包成 [键]\n值 的小节，拼在消息尾部而不是 system 里，
        #    这样它不会污染人设，也不会被写进长期记忆。
        ephemeral_text = "\n\n".join(
            f"[{key}]\n{value}" for key, value in (ephemeral or {}).items() if value
        )
        # 4) 历史清洗：
        #    - 保留 user / system；
        #    - assistant 只在"没有 tool_calls"时保留（带工具调用的历史重放会让模型看到孤立调用）；
        #    - content 强制转字符串，防止 None 掉进请求体；
        #    - created_at 有值才带上（给模型提供时间线索）；
        #    - 只取最近 15 条，控制 token。
        clean_history = [
            {
                "role": item.get("role"),
                "content": str(item.get("content") or ""),
                **({"created_at": str(item["created_at"])} if item.get("created_at") else {}),
            }
            for item in (history or [])
            if item.get("role") in {"system", "user"}
            or (item.get("role") == "assistant" and not item.get("tool_calls"))
        ][-15:]
        return AgentContext(
            system_prompt="\n\n".join(sections),
            history=clean_history,
            memory_text=memory_context.injected_text,
            memory_context=memory_context,
            ephemeral_instructions=ephemeral_text,
            metadata={
                "owner_id": scope.owner_id,
                "channel": scope.channel,
                "conversation_id": scope.conversation_id,
                "layer_counts": memory_context.metadata.get("layer_counts", {}),
            },
        )

    def build_fast(
        self,
        scope: Scope,
        *,
        history: list[dict[str, Any]] | None = None,
        channel_rules: str = "",
    ) -> AgentContext:
        """轻量组装：不检索记忆，历史只留最近 4 条。

        这里比 build() 更严格——连 system 角色都不收，只收 user/assistant 且内容非空，
        因为快速模式常用于闲聊式短回应，多余的消息只会让首字延迟变高。
        metadata.context_mode = "fast_chat" 是给监控台/日志用的标记，
        一眼就能看出这一回合走的是哪条路径。
        """
        clean_history = [
            {
                "role": item.get("role"),
                "content": str(item.get("content") or ""),
                **({"created_at": str(item["created_at"])} if item.get("created_at") else {}),
            }
            for item in (history or [])
            if item.get("role") in {"user", "assistant"} and str(item.get("content") or "").strip()
        ][-4:]
        return AgentContext(
            system_prompt=channel_rules,
            history=clean_history,
            memory_text="",
            memory_context=None,
            metadata={
                "owner_id": scope.owner_id,
                "channel": scope.channel,
                "conversation_id": scope.conversation_id,
                "context_mode": "fast_chat",
                "history_count": len(clean_history),
                "layer_counts": {"hot": 0, "warm": 0, "cold": 0},
            },
        )
