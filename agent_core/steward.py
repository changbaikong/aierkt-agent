"""管家式过程表达（StewardNarrator）。

【它解决什么】
长任务进行中，聊天框里要么是空的、要么是一句"正在思考"，
用户不知道事情进行到哪一步了。本模块让主 Chat 用**自己的人物身份**
在关键节点说一两句人话：

    用户：你去看一下这篇论文。
    主 Chat：嗯，交给我吧。我先把这篇论文找出来，看看它到底在研究什么。
    主 Chat：找到了。它主要在讨论 XXX 和 XX，不过这里的关系还不够清楚，
             我再顺着它的方法和引用看下去。
    主 Chat：我看完了。简单说，这篇论文想说明的是……

【职责边界（重要）】
- **只负责措辞**。它不做任何任务决策，不调用工具，不推进计划。
- 输入只能是"已经确认的信息"（用户目标 / 当前阶段 / 下一步 / 已确认证据）。
  没有证据就不许说"找到了" —— 这条由 :func:`steward_line_issue` 强制。
- 生成失败（超时 / 不合格 / 没配模型）一律回退到固定角色化模板，
  **绝不阻塞原任务**：调用方拿不到话术也要继续跑。
- Provider（后台任务执行层）仍然只返回结构化结果，不负责说话。

【消息与事件】
:class:`StewardReporter` 负责落库 + 广播 ``steward.progress`` 事件。
落库交给注入的 sink（Web 侧是会话仓库），本模块不认识任何存储。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol

from .contracts import Scope
from .events import Event, EventBus, EventType
from .fastpath import SafeReadonlySummary, narrate as narrate_safe_summary
from .reply_guard import (
    mentions_internal_terms,
    reply_issue,
    strip_code_fences,
)
from .steward_progress import (
    STAGE_CONFLICT,
    STAGE_MATERIALS,
    STAGE_READ_DONE,
    STAGE_READING,
    STAGE_REDIRECT,
    STAGE_SEARCHING,
    STAGE_WORK_DELIVER,
    STAGE_WORK_EDIT,
    STAGE_WORK_INSPECT,
    STAGE_WORK_REPAIR,
    STAGE_WORK_VERIFY,
    StageReport,
    conversation_identity,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# 阶段常量
# ----------------------------------------------------------------------
# 只有"重要阶段"才有值。普通工具完成、分页、重试、内部调度都不在其中 ——
# 那些节点说话就会变成刷屏。
PHASE_ACCEPTED = "accepted"                      # 接手任务，准备第一次检索或执行
PHASE_EVIDENCE_FOUND = "evidence_found"          # 拿到关键材料，并且还有下一步
PHASE_REDIRECT = "redirect"                      # 方向变了 / 需要补查
PHASE_WAITING_APPROVAL = "waiting_approval"      # 等用户审批或补充信息
PHASE_COMPLETED = "completed"                    # 后台任务完成
PHASE_FAILED = "failed"                          # 失败
PHASE_CANCELLED = "cancelled"                    # 被取消（用户叫停 / 审批被拒）
PHASE_INTERRUPTED = "interrupted"                # 中断

# 聊天查询专用的阶段（取值与 steward_progress.STAGE_* 完全一致）。
# 它们描述的是"这一回合现在真实做到哪一步"，和上面的后台任务阶段共用一套
# 校验、去重、落库与广播；区别只在措辞来源：这几个阶段用本地短句，不调模型。
PHASE_SEARCHING = STAGE_SEARCHING                # 第一次查询正在执行
PHASE_MATERIALS = STAGE_MATERIALS                # 搜索确实返回了材料，还有下一步
PHASE_READING = STAGE_READING                    # 已开始读原文，正文还没回来
PHASE_READ_DONE = STAGE_READ_DONE                # 正文确实取得，还要读下一份
PHASE_CONFLICT = STAGE_CONFLICT                  # 已识别具体矛盾，且还会补查

# 代码长任务的阶段（跟随任务与执行尝试的生命周期，不跟随聊天回合）。
PHASE_WORK_INSPECT = STAGE_WORK_INSPECT          # 查看项目
PHASE_WORK_EDIT = STAGE_WORK_EDIT                # 修改代码
PHASE_WORK_VERIFY = STAGE_WORK_VERIFY            # 验证改动（宿主真的跑了测试）
PHASE_WORK_DELIVER = STAGE_WORK_DELIVER          # 准备交付（补丁确实生成）
PHASE_WORK_REPAIR = STAGE_WORK_REPAIR            # 失败后进入修复

#: 说出口的话会被写进对话历史并带上这个 kind。
#: 记忆提炼靠它把过程句挡在长期记忆之外（见 memory_extract）。
STEWARD_PROGRESS_KIND = "steward_progress"

#: "找到了 / 查到了" 这类结论词。没有证据时说出口就是**编造**，
#: 所以 evidence 为空的阶段（接手 / 改道 / 待审批）见到这些词直接判不合格。
_CLAIM_TERMS = ("找到了", "查到了", "看完了", "搜到了", "拿到了", "结果如下", "结论是", "已经确认")

#: 句末标点，用于"最多两句话"的截断。
_SENTENCE_END = re.compile(r"[。！？!?]")

#: 客服腔开头，一律不要。
_BANNED_PREFIXES = ("好的，我来帮你", "我来帮你", "好的，没问题", "为您", "请问", "亲")


@dataclass(frozen=True)
class StewardContext:
    """生成一句过程话术所需的**全部**输入。

    刻意没有"工具名 / 状态机 / 步骤编号"这类字段：
    它们一旦进来，就有被模型写进气泡的风险。
    """

    phase: str = PHASE_ACCEPTED
    goal: str = ""                       # 用户目标（原话，会被截断）
    stage: str = ""                      # 当前阶段（人话）
    next_action: str = ""                # 即将执行 / 下一步做什么（人话）
    evidence: tuple[str, ...] = ()       # 已经确认的结果摘要（只有真拿到才填）

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence)


# ----------------------------------------------------------------------
# 角色化兜底模板
# ----------------------------------------------------------------------
def _shorten(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _joined_evidence(evidence: Iterable[str]) -> str:
    items = [_shorten(item, 40) for item in evidence if str(item or "").strip()]
    return "；".join(items[:2])


def template_line(context: StewardContext) -> str:
    """模型不可用 / 超时 / 输出不合格时的固定话术。纯函数，方便单测。

    每条都自带人物语气，但**不含**任何未经确认的事实：
    拿不到证据就说"还在看"，拿到证据才说"找到了"。
    """
    goal = _shorten(context.goal, 24)
    evidence = _joined_evidence(context.evidence)
    if context.phase == PHASE_EVIDENCE_FOUND:
        if evidence:
            return f"找到了。{evidence}不过这里的关系还不够清楚，我再往下看看。"
        return "这部分还没有可靠结果。我再换个方向看看。"
    if context.phase == PHASE_REDIRECT:
        return "这条线索还不够清楚。我换个方式再查一次。"
    if context.phase == PHASE_WAITING_APPROVAL:
        return "我先停一下。真正动手之前，想让你先看一眼。"
    if context.phase == PHASE_COMPLETED:
        if evidence:
            return f"我看完了。{evidence}"
        return "我看完了，事情已经办好了。"
    if context.phase == PHASE_FAILED:
        return "这次没能把内容完整取回来。已经找到的部分我先留着，换一种方式再查会更合适。"
    if context.phase == PHASE_CANCELLED:
        return "好，这件事我停下来了。已经做完的部分都还在，你想接着做随时说。"
    if context.phase == PHASE_INTERRUPTED:
        return "刚才被打断了。已经查到的部分还在，我可以接着继续。"
    # PHASE_ACCEPTED
    if goal:
        return f"嗯，交给我吧。我先把「{goal}」这件事弄清楚，看看能查到什么。"
    return "嗯，交给我吧。我先去看看情况。"


# ----------------------------------------------------------------------
# 近期已发过程句（只用来避免重复，不是措辞库）
# ----------------------------------------------------------------------
class RecentPhraseMemory:
    """同一会话最近几句**已经发出去**的过程话术。

    它只服务两件事：① 生成时把"最近说过什么"告诉模型，避免复读；
    ② 生成结果与近期重复时判为不合格。它是措辞的**输入**，不是措辞本身 ——
    这里没有一句预置文案。记录按会话隔离、内存有界，重启后不恢复（不强求）。
    """

    def __init__(self, *, recent_limit: int = 3, max_conversations: int = 256) -> None:
        self.recent_limit = max(1, int(recent_limit))
        self.max_conversations = max(8, int(max_conversations))
        self._recent: dict[str, deque[str]] = {}

    def recent(self, key: str) -> tuple[str, ...]:
        return tuple(self._recent.get(str(key or ""), ()))

    def remember(self, key: str, line: str) -> None:
        value = str(line or "").strip()
        if not value:
            return
        bucket = str(key or "")
        if bucket not in self._recent and len(self._recent) >= self.max_conversations:
            # 有界：会话太多就整体清掉，本来就只服务"最近几句别重复"。
            self._recent.clear()
        entry = self._recent.get(bucket)
        if entry is None:
            entry = deque(maxlen=self.recent_limit)
            self._recent[bucket] = entry
        entry.append(value)


def _phrase_signature(text: str) -> str:
    """比较重复用的指纹：去掉标点、空白和语气词尾巴，只留实义字符。"""
    return re.sub(r"[\s，。！？、,.!?~～…\"'“”‘’（）()《》]+", "", str(text or ""))


def phrase_too_similar(line: str, recent: Iterable[str], *, threshold: float = 0.7) -> bool:
    """这句话是不是和最近说过的太像（重复检查）。

    判定保守但可解释：指纹相同、互相包含、或者字符二元组重合度 ≥ threshold。
    宁可换一句，也不要在同一段对话里复读同一句话。
    """
    candidate = _phrase_signature(line)
    if not candidate:
        return True
    for item in recent:
        existing = _phrase_signature(item)
        if not existing:
            continue
        if candidate == existing or candidate in existing or existing in candidate:
            return True
        left = {candidate[i:i + 2] for i in range(max(0, len(candidate) - 1))}
        right = {existing[i:i + 2] for i in range(max(0, len(existing) - 1))}
        if left and right:
            overlap = len(left & right) / len(left | right)
            if overlap >= threshold:
                return True
    return False


# ----------------------------------------------------------------------
# 动态措辞：模型负责怎么说，程序负责说什么
# ----------------------------------------------------------------------
_STAGE_SYSTEM = """你在给正在等消息的人报一句进度，不是做汇报，也不是做推理。

要求：
1. 只输出一到两句中文，不超过 60 个字，不要换行。
2. 使用第一人称，保持下面人物设定的语气，像人随口说一句。
3. 只说"已经确认的信息"里写了的内容；没有的东西不许补充、不许推断、不许下结论。
4. 还没拿到结果时，绝对不能说"找到了""查到了""看完了""结果如下"。
5. 不许出现工具名、文件路径、命令、网址、错误信息、步骤编号、JSON、代码块、异常信息。
6. 不复述用户的问题，不用"好的，我来帮你""还有什么需要帮助吗"这类客服腔。
7. 不要和"最近已经说过的话"重复或换汤不换药。
8. 直接输出这一句话本身：不要引号、不要前缀、不要解释、不要标题。"""


#: 重试时告诉模型"上一句为什么不能用"。它只影响措辞，不改变任何事实判断。
_REJECT_REASON_GUIDE = {
    "empty": "上一句是空的，或者生成超时了；请直接给一句话。",
    "premature": "还没拿到结果，不能说「找到了」「查到了」「看完了」这类话。",
    "long": "上一句太长了，压到 60 个字以内。",
    "protocol": "上一句带了 JSON、代码块或方括号标记，只许写自然中文。",
    "internal": "上一句出现了工具名、路径或内部词，全部去掉。",
    "tone": "上一句像客服腔，换成人物自己的随口语气。",
    "duplicate": "上一句和最近说过的话重复了，换个说法。",
    "other": "上一句不符合要求，换一种说法重写。",
}


class StagePhraseGenerator:
    """按阶段**动态生成**一句过程话术：模型负责措辞，程序负责判断进展。

    边界（与"后台任务的兜底话术"刻意分开）：

    - 输入只有安全的人类可读信息：目标摘要、当前阶段、确实会执行的下一步、
      可公开的事实摘要、近期已发过程句；工具名 / 路径 / 异常 / 正文全文 / 推理都进不来；
    - 生成失败、超时、输出不合格或与近期重复 → 返回空串，调用方**静默跳过**，
      不回退任何固定句（固定句会让"说话"变回模板，也会掩盖模型不可用）；
    - 完成 / 失败 / 取消 / 等待确认这些必要通知仍走 :class:`StewardNarrator`
      的现有兜底规则，不受这里影响。
    """

    def __init__(
        self,
        provider: Any = None,
        *,
        persona: str = "",
        timeout_seconds: float = 2.0,
        max_tokens: int = 96,
        temperature: float = 0.6,
        forbidden_terms: Iterable[str] = (),
        attempts: int = 2,
    ) -> None:
        self.provider = provider
        self.persona = str(persona or "")
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_tokens = max(32, int(max_tokens))
        self.temperature = temperature
        self.forbidden_terms = frozenset(str(term) for term in forbidden_terms if str(term).strip())
        self.attempts = max(1, int(attempts))

    @classmethod
    def from_narrator(cls, narrator: "StewardNarrator | None", **overrides: Any) -> "StagePhraseGenerator":
        """从已有的 StewardNarrator 复用模型、人设与禁用词 —— 不额外装配一套。"""
        if narrator is None:
            return cls(None, **overrides)
        options: dict[str, Any] = {
            "persona": narrator.persona,
            "forbidden_terms": narrator.forbidden_terms,
        }
        options.update(overrides)
        return cls(narrator.provider, **options)

    def _render(self, context: StewardContext, recent: Iterable[str]) -> str:
        lines = [f"- 用户在做的事：{_shorten(context.goal, 60) or '（对方刚开口，还不清楚）'}"]
        lines.append(f"- 当前阶段：{_PHASE_BRIEF.get(context.phase, context.phase)}")
        if context.stage:
            lines.append(f"- 具体在做什么：{_shorten(context.stage, 60)}")
        if context.evidence:
            lines.append(f"- 已经确认的事实：{_joined_evidence(context.evidence)}")
        else:
            lines.append("- 已经确认的事实：（还没有，一条都不要编）")
        lines.append(
            f"- 接下来确实会发生的事：{_shorten(context.next_action, 60) or '（不确定，不要承诺）'}"
        )
        recent_lines = [_shorten(item, 40) for item in recent if str(item or "").strip()]
        if recent_lines:
            lines.append("- 最近已经说过的话（不要重复）：" + " ／ ".join(recent_lines[-3:]))
        return "\n".join(lines)

    def _messages(
        self, context: StewardContext, recent: Iterable[str], *, feedback: str = ""
    ) -> list[dict[str, Any]]:
        system = _STAGE_SYSTEM
        if self.persona:
            system = f"{_STAGE_SYSTEM}\n\n【人物设定】\n{_shorten(self.persona, 600)}"
        user = f"【已经确认的信息】\n{self._render(context, recent)}"
        if feedback:
            user += f"\n\n【上一句为什么不能用】\n{feedback}\n请换一种说法，别犯同样的毛病。"
        user += "\n\n【你要说的那句话】"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    async def compose(
        self,
        context: StewardContext,
        *,
        recent: Iterable[str] = (),
    ) -> str:
        """生成一句；任何失败都返回空串（调用方据此静默跳过）。

        重试会把**上一次为什么不合格**告诉模型（"别提前说拿到了结果""别复读"），
        这样它有机会换一种说法；仍然不合格就按设计闭嘴，不回退固定句。
        """
        provider = self.provider
        if provider is None or not callable(getattr(provider, "complete", None)):
            return ""
        history = tuple(recent or ())
        feedback = ""
        for _ in range(self.attempts):
            line = await self._generate_once(context, history, feedback=feedback)
            if not line:
                feedback = _REJECT_REASON_GUIDE["empty"]
                continue
            issue = steward_line_issue(
                line,
                phase=context.phase,
                has_evidence=context.has_evidence,
                forbidden_terms=self.forbidden_terms,
            )
            if issue is not None:
                feedback = _REJECT_REASON_GUIDE.get(issue, _REJECT_REASON_GUIDE["other"])
                continue
            if phrase_too_similar(line, history):
                # 和最近说过的话撞了：再让模型换一个说法（attempts 用尽就跳过）。
                feedback = _REJECT_REASON_GUIDE["duplicate"]
                continue
            return line
        return ""

    async def _generate_once(
        self, context: StewardContext, recent: tuple[str, ...], *, feedback: str = ""
    ) -> str:
        try:
            response = await asyncio.wait_for(
                self.provider.complete(
                    self._messages(context, recent, feedback=feedback),
                    tools=None,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                ),
                timeout=self.timeout_seconds,
            )
        except Exception:
            logger.debug("stage phrase generation failed", exc_info=True)
            return ""
        raw = ""
        message = getattr(response, "message", None)
        if isinstance(message, dict):
            raw = str(message.get("content") or "")
        elif isinstance(response, dict):
            inner = response.get("message")
            raw = str((inner or {}).get("content") or "") if isinstance(inner, dict) else ""
        return normalize_steward_line(raw)


# ----------------------------------------------------------------------
# 输出校验
# ----------------------------------------------------------------------
def normalize_steward_line(text: str) -> str:
    """把模型输出收拾成一行：去围栏、去编号前缀、折成单行、最多两句话。"""
    value = strip_code_fences(text)
    value = value.replace("\r", " ").replace("\n", " ")
    value = re.sub(r"^\s*(?:\d+[\.、)]\s*|[-*•]\s*)+", "", value)
    value = re.sub(r"\s+", " ", value).strip().strip("\"'“”‘’ ")
    if not value:
        return ""
    sentences: list[str] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(value):
        sentences.append(value[cursor:match.end()].strip())
        cursor = match.end()
        if len(sentences) == 2:
            break
    if len(sentences) < 2 and cursor < len(value):
        tail = value[cursor:].strip()
        if tail:
            sentences.append(tail)
    joined = "".join(sentences).strip()
    return joined or value[:80]


def steward_line_issue(
    text: str,
    *,
    phase: str = PHASE_ACCEPTED,
    has_evidence: bool = False,
    forbidden_terms: Iterable[str] = (),
) -> str | None:
    """一句话能不能进气泡。返回 None 表示合格，否则返回问题类型。

    这是"原始 reasoning、工具协议、JSON 和异常栈无法进入气泡"的落点：
    - protocol / reasoning / completion 三类沿用 :func:`reply_guard.reply_issue`
      （和最终回复同一套判定，不是另写一版）；
    - internal：出现工具名或 Worker / Provider / Host 等内部词；
    - premature：还没有证据就宣称"找到了"；
    - long：超过 80 字（气泡里放不下，也说明模型在发挥）。
    """
    value = str(text or "").strip()
    if not value:
        return "empty"
    if len(value) > 80:
        return "long"
    if "```" in value or "{" in value or "}" in value:
        return "protocol"
    if re.search(r"\[[A-Za-z_]+\]|</?[a-zA-Z]", value):
        return "protocol"
    issue = reply_issue(value, tool_names=forbidden_terms)
    if issue is not None:
        return issue
    if mentions_internal_terms(value, extra_terms=forbidden_terms):
        return "internal"
    if not has_evidence and any(term in value for term in _CLAIM_TERMS):
        return "premature"
    if any(value.startswith(prefix) for prefix in _BANNED_PREFIXES):
        return "tone"
    return None


# ----------------------------------------------------------------------
# Narrator
# ----------------------------------------------------------------------
_NARRATOR_SYSTEM = """你在给正在等消息的人报一句进度，不是做汇报，也不是做推理。

要求：
1. 只输出一到两句中文，不超过 60 个字，不要换行。
2. 使用第一人称，保持下面人物设定的语气，像人随口说一句。
3. 只说"已经确认的信息"里写了的内容；没有的东西不许补充、不许推断、不许下结论。
4. 还没有结果时，绝对不能说"找到了""查到了""看完了""结果如下"。
5. 不许出现工具名、Worker、Provider、Host、步骤编号、JSON、代码块、异常信息、调用细节。
6. 不复述用户的问题，不用"好的，我来帮你""还有什么需要帮助吗"这类客服腔。
7. 直接输出这一句话本身：不要引号、不要前缀、不要解释、不要标题。"""

_PHASE_BRIEF = {
    PHASE_ACCEPTED: "刚刚接手，正准备开始查或做",
    PHASE_EVIDENCE_FOUND: "已经拿到一部分关键材料，还要继续往下看",
    PHASE_REDIRECT: "刚才那条路走不通或不够清楚，准备换个方式再查",
    PHASE_WAITING_APPROVAL: "停下来等对方确认，确认之前不会动手",
    PHASE_COMPLETED: "事情已经做完，正在把结果告诉对方",
    PHASE_FAILED: "这次没能做成，要说明情况但不暴露技术细节",
    PHASE_CANCELLED: "这件事被叫停了（对方取消或审批被拒），说明已经做完的部分还在",
    PHASE_INTERRUPTED: "进行中被打断了，说明现状",
    PHASE_SEARCHING: "第一次查询正在执行，还没有结果",
    PHASE_MATERIALS: "搜索确实返回了材料，但还要继续筛选或读取",
    PHASE_READING: "已经开始读某份原文，正文还没有拿回来",
    PHASE_READ_DONE: "一份原文已经成功取回，还要读下一份",
    PHASE_CONFLICT: "已经看出具体说法有出入，并且确实还会补查",
    PHASE_WORK_INSPECT: "已经在读项目里的文件或搜索内容，还没动手改",
    PHASE_WORK_EDIT: "已经开始改文件，改完还要检查",
    PHASE_WORK_VERIFY: "宿主真的开始跑验证了，结果还没出来",
    PHASE_WORK_DELIVER: "补丁确实已经生成，准备把改动和验证情况交付",
    PHASE_WORK_REPAIR: "上一版没通过，已经进入修复，还要再验一次",
}


class StewardNarrator:
    """把"已经确认的信息"翻译成一句角色化进度。

    不参与任务决策，不调用工具。模型不可用时退化为 :func:`template_line`。
    """

    def __init__(
        self,
        provider: Any = None,
        *,
        persona: str = "",
        timeout_seconds: float = 3.0,
        max_tokens: int = 96,
        temperature: float = 0.4,
        forbidden_terms: Iterable[str] = (),
    ) -> None:
        self.provider = provider
        self.persona = str(persona or "")
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.max_tokens = max(32, int(max_tokens))
        self.temperature = temperature
        self.forbidden_terms = frozenset(str(term) for term in forbidden_terms if str(term).strip())

    def _render(self, context: StewardContext) -> str:
        lines = [f"- 用户在做的事：{_shorten(context.goal, 60) or '（对方刚开口，还不清楚）'}"]
        lines.append(f"- 当前阶段：{_PHASE_BRIEF.get(context.phase, context.phase)}")
        if context.stage:
            lines.append(f"- 具体在做什么：{_shorten(context.stage, 60)}")
        if context.evidence:
            lines.append(f"- 已经确认的结果：{_joined_evidence(context.evidence)}")
        else:
            lines.append("- 已经确认的结果：（还没有，一条都不要编）")
        lines.append(f"- 下一步：{_shorten(context.next_action, 60) or '（继续手上的事）'}")
        return "\n".join(lines)

    def _messages(self, context: StewardContext) -> list[dict[str, Any]]:
        system = _NARRATOR_SYSTEM
        if self.persona:
            system = f"{_NARRATOR_SYSTEM}\n\n【人物设定】\n{_shorten(self.persona, 600)}"
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": f"【已经确认的信息】\n{self._render(context)}\n\n【你要说的那句话】"},
        ]

    async def narrate(self, context: StewardContext) -> str:
        """生成一句过程话术；任何情况下都返回一句能说出口的话。"""
        fallback = template_line(context)
        if self.provider is None:
            return fallback
        complete = getattr(self.provider, "complete", None)
        if not callable(complete):
            return fallback
        try:
            response = await asyncio.wait_for(
                complete(
                    self._messages(context),
                    tools=None,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                ),
                timeout=self.timeout_seconds,
            )
        except Exception:
            logger.debug("steward narration failed, using template", exc_info=True)
            return fallback
        raw = ""
        message = getattr(response, "message", None)
        if isinstance(message, dict):
            raw = str(message.get("content") or "")
        elif isinstance(response, dict):
            inner = response.get("message")
            raw = str((inner or {}).get("content") or "") if isinstance(inner, dict) else ""
        line = normalize_steward_line(raw)
        if steward_line_issue(
            line,
            phase=context.phase,
            has_evidence=context.has_evidence,
            forbidden_terms=self.forbidden_terms,
        ) is not None:
            return fallback
        return line

    def narrate_fast_result(self, summary: SafeReadonlySummary) -> str:
        """确定性快路径的最终回复：本地模板，**不调模型、不出过程气泡**。

        和 :meth:`narrate` 的分工：那个负责"事情进行中"的过程气泡（要调模型），
        这个负责"事情已经办完"的一句话结论 —— 瞬间完成的事，不值得为它
        调一次模型，也不值得为它产生一条进度气泡。

        契约：不访问工具、不读数据库、不调 LLM、永远返回非空中文字符串；
        失败也只能返回固定模板（见 :mod:`agent_core.fastpath`）。
        """
        try:
            from .fastpath_pool import get_character_fastpath_reply

            char_reply = get_character_fastpath_reply(summary) if "爱尔奎特" in self.persona else None
            if char_reply:
                return char_reply
            return narrate_safe_summary(summary)
        except Exception:
            logger.debug("fast path narration failed", exc_info=True)
            return "这次没查到。稍后再试一次会更合适。"


# ----------------------------------------------------------------------
# 落库 + 广播
# ----------------------------------------------------------------------
class ProgressSink(Protocol):
    """把一条过程消息写进某个对话。

    返回 False 表示"这条已经写过了"（stable_key 撞了），调用方据此跳过广播。
    """

    def __call__(self, scope: Scope, message: dict[str, Any], stable_key: str) -> bool: ...


def dedupe_key(
    scope: Scope,
    run_id: str = "",
    plan_id: str = "",
    step_id: str = "",
    phase: str = "",
) -> str:
    """稳定去重键：conversation_id + run_id/plan_id + step_id + phase。

    conversation_id 之外的身份三元组由存储层按 Scope 隔离，这里带上它是为了
    让**进程内**的 seen 集合也不会在不同对话之间串味。
    """
    return "|".join(
        (
            str(getattr(scope, "owner_id", "") or ""),
            str(getattr(scope, "channel", "") or ""),
            str(getattr(scope, "conversation_id", "") or ""),
            str(getattr(scope, "module_id", "") or ""),
            str(run_id or plan_id or ""),
            str(step_id or ""),
            str(phase or ""),
        )
    )


@dataclass
class StewardReporter:
    """在关键阶段说一句话：生成 → 落库 → 广播。

    用法::

        reporter = StewardReporter(narrator, sink=..., event_bus=...)
        await reporter.publish(scope, StewardContext(phase=PHASE_ACCEPTED, goal="看这篇论文"),
                               run_id=run_id, step_id="step-0", turn_id=turn_id)
    """

    narrator: StewardNarrator | None = None
    sink: ProgressSink | None = None
    event_bus: EventBus | None = None
    seen: set[str] = field(default_factory=set)

    async def publish(
        self,
        scope: Scope,
        context: StewardContext,
        *,
        run_id: str = "",
        plan_id: str = "",
        step_id: str = "",
        turn_id: str = "",
    ) -> str:
        """生成并写出一条过程消息。返回真正写出去的文本（被去重则为空串）。"""
        key = dedupe_key(scope, run_id, plan_id, step_id, context.phase)
        if key in self.seen:
            return ""
        narrator = self.narrator or StewardNarrator()
        text = await narrator.narrate(context)
        if not text:
            return ""
        return await self._deliver(
            scope,
            text,
            key=key,
            phase=context.phase,
            run_id=run_id,
            plan_id=plan_id,
            step_id=step_id,
            turn_id=turn_id,
        )

    async def publish_text(
        self,
        scope: Scope,
        text: str,
        *,
        phase: str = "",
        run_id: str = "",
        plan_id: str = "",
        step_id: str = "",
        turn_id: str = "",
    ) -> str:
        """写出一条**已经生成并通过校验**的过程消息。

        动态措辞（`StagePhraseGenerator`）走这条路：话由模型按当前阶段写，
        但"要不要发、什么时候发、还能说几句"仍由 TurnProgress 决定。
        去重、落库、广播与 :meth:`publish` 完全共用。
        """
        value = str(text or "").strip()
        if not value:
            return ""
        key = dedupe_key(scope, run_id, plan_id, step_id, phase)
        if key in self.seen:
            return ""
        return await self._deliver(
            scope,
            value,
            key=key,
            phase=phase,
            run_id=run_id,
            plan_id=plan_id,
            step_id=step_id,
            turn_id=turn_id,
        )

    async def _deliver(
        self,
        scope: Scope,
        text: str,
        *,
        key: str,
        phase: str,
        run_id: str,
        plan_id: str,
        step_id: str,
        turn_id: str,
    ) -> str:
        """落库 + 广播。返回真正写出去的文本（被去重 / 落库拒绝则为空串）。"""
        message = {
            "role": "assistant",
            "content": text,
            "kind": STEWARD_PROGRESS_KIND,
            "phase": phase,
            "run_id": str(run_id or ""),
            "plan_id": str(plan_id or ""),
            "step_id": str(step_id or ""),
        }
        accepted = True
        if self.sink is not None:
            try:
                accepted = bool(self.sink(scope, message, key))
            except Exception:
                logger.debug("steward sink failed", exc_info=True)
                return ""
        # 先记 seen 再广播：即使广播出问题，同一阶段也不会重试着又写一条。
        self.seen.add(key)
        if len(self.seen) > 256:
            self.seen.clear()
        if accepted and self.event_bus is not None:
            await self.event_bus.emit(
                Event(
                    type=EventType.STEWARD_PROGRESS,
                    timestamp=datetime.now(),
                    scope={
                        "owner_id": scope.owner_id,
                        "channel": scope.channel,
                        "conversation_id": scope.conversation_id,
                        "module_id": scope.module_id,
                    },
                    data={
                        "turn_id": str(turn_id or ""),
                        "run_id": str(run_id or ""),
                        "plan_id": str(plan_id or ""),
                        "step_id": str(step_id or ""),
                        "phase": phase,
                        "content": text,
                        "kind": STEWARD_PROGRESS_KIND,
                        "dedupe_key": key,
                    },
                )
            )
        return text if accepted else ""


class ChatStagePublisher:
    """把 :class:`~agent_core.steward_progress.StageReport` 变成一条过程消息。

    这是"动态措辞"的落点，分两段交给 TurnProgress 调用：

    - :meth:`compose`：把安全事实交给 :class:`StagePhraseGenerator` 现场生成一句话。
      模型不可用 / 超时 / 不合格 / 与近期重复 → 返回空串，这一条就不发（不回退固定句）；
    - :meth:`publish`：把生成结果落库 + 广播（复用 reporter 的去重与广播），
      成功之后才记进"近期已发过程句"。

    后台任务的模型措辞走 :meth:`StewardReporter.publish`，不受这里影响。
    """

    def __init__(
        self,
        reporter: StewardReporter | None,
        *,
        scope: Scope,
        goal: str = "",
        conversation_key: str = "",
        generator: StagePhraseGenerator | None = None,
        memory: RecentPhraseMemory | None = None,
        run_id: str = "",
        turn_id: str = "",
    ) -> None:
        self.reporter = reporter
        self.scope = scope
        self.goal = str(goal or "")
        self.conversation_key = str(conversation_key or "") or conversation_identity(scope)
        # 没显式给生成器就从 reporter 的 narrator 复用模型 / 人设 / 禁用词；
        # 没有模型时生成器返回空串 —— 过程气泡直接不发，而不是退回固定句。
        self.generator = generator or StagePhraseGenerator.from_narrator(
            getattr(reporter, "narrator", None)
        )
        self.memory = memory or RecentPhraseMemory()
        self.run_id = str(run_id or "")
        self.turn_id = str(turn_id or "")
        self.last_line = ""

    def context_for(self, report: StageReport) -> StewardContext:
        """阶段记录 → 交给措辞层的安全信息（工具名 / 路径 / 异常都进不来）。"""
        summary = str(getattr(report, "summary", "") or "").strip()
        return StewardContext(
            phase=str(getattr(report, "stage", "") or ""),
            goal=self.goal,
            next_action=str(getattr(report, "next_action", "") or ""),
            evidence=(summary,) if summary else (),
        )

    async def compose(self, report: StageReport) -> str:
        if self.reporter is None:
            return ""
        return await self.generator.compose(
            self.context_for(report),
            recent=self.memory.recent(self.conversation_key),
        )

    async def publish(self, report: StageReport, text: str) -> str:
        if self.reporter is None:
            return ""
        value = str(text or "").strip()
        if not value:
            return ""
        published = await self.reporter.publish_text(
            self.scope,
            value,
            phase=str(getattr(report, "stage", "") or ""),
            run_id=self.run_id,
            step_id=str(getattr(report, "node", "") or ""),
            turn_id=self.turn_id,
        )
        if published:
            self.memory.remember(self.conversation_key, published)
            self.last_line = published
        return published
