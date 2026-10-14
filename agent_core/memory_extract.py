"""记忆提炼器：把每一轮对话提炼成结构化的「陪伴属性」记忆（业务中性）。

独立模块，暂不接主循环。复用 ``OpenAICompatibleProvider`` 作提炼 LLM
（deepseek 等 OpenAI 兼容接口），不新建适配器类。

设计要点：
- 5 个桌宠陪伴槽位（conflict_key）映射到 memory_records.conflict_key 列
- LLM 只输出 JSON，``_parse`` 兜底链解析，任何异常都返回 []，绝不外抛
- 动态 token 分配：推理占满预算导致正文为空时，自动 ×2 加码重试（见 extract）
- duration 档位将来映射成 expires_at（本次不落地，仅记录在数据里）
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

from agent_core.contracts import ProviderResponse
from agent_core.project_env import PROJECT_ENV_PATH
from agent_core.task_result_bridge import TASK_RESULT_KIND
from agent_core.work_bridge import WORK_RESULT_KIND
from agent_core.steward import STEWARD_PROGRESS_KIND

from .memory_turn import (
    CONTENT_STATUSES,
    HEDGE_MARKERS,
    USER_ADOPTION_STATUSES,
    MemoryCandidate,
    MemoryCandidateError,
    MemoryUpdateSuggestion,
    adoption_evidenced,
    cited_user_evidence,
    cites_item_subject,
    clauses,
    content_units,
    expresses_adoption,
    is_hedged,
    mentions_item,
    packet_source_roles,
    REJECTION_MARKERS,
    sentences,
    unhedged_clauses,
    validate_candidate,
    validate_suggestion,
)

#: 留在对话上下文里、但不进长期记忆提炼的消息类型。
#: task_result 和 work_result 是同一类东西：后台任务的终态播报。
#: "「每晚总结」已经办好了"不是用户说过的事实，进记忆库只会污染检索。
NON_MEMORY_KINDS = frozenset({WORK_RESULT_KIND, STEWARD_PROGRESS_KIND, TASK_RESULT_KIND})

#: 内容的时间尺度，与 memory_turn.CONTENT_HORIZONS 同值。
#: 取值白名单由提炼侧把关，非法值直接拒收。
SHORT_TERM = "short_term"
LONG_TERM = "long_term"
CANDIDATE_HORIZONS: tuple[str, ...] = (LONG_TERM, SHORT_TERM)

#: ``MemoryCandidate`` 是否已经自带 horizon 字段。
#: lead 已把该字段派给写入侧；字段落地前，这里用子类把值带上，落地后子类自动
#: 让位（不再重复声明字段，避免 dataclass 字段顺序冲突）。两条路径对外行为一样：
#: ``as_metadata()`` 里出现 ``horizon``。
_CANDIDATE_HAS_HORIZON_FIELD = "horizon" in getattr(MemoryCandidate, "__dataclass_fields__", {})


if _CANDIDATE_HAS_HORIZON_FIELD:  # pragma: no cover - 取决于字段是否已落地
    HorizonCandidate = MemoryCandidate
else:

    @dataclass(frozen=True)
    class HorizonCandidate(MemoryCandidate):  # type: ignore[no-redef]
        """带上 ``horizon`` 的候选（字段未落地时的过渡实现）。

        ``MemoryCandidate`` 是 frozen 且没有 horizon 字段，而「这条记多久」必须
        由提炼侧交到写入侧（否则短期语义只能靠正文关键词猜）。子类只**加**一个
        带默认值的字段并覆写 ``as_metadata()``，因此：

        - ``validate_candidate(candidate)`` 照样通过 —— 它不检查类型，只读字段，
          新增字段带默认值，不改变 origin 等任何被校验的值；
        - ``dataclasses.replace()`` 会保留实际类型，退回 proposed 等改写不会丢 horizon；
        - 写入侧 metadata 通道（优先级最高）能读到它。

        没有声明 horizon 时行为与父类完全一致（metadata 里不出现该键）。
        """

        horizon: str = ""

        def as_metadata(self) -> dict[str, Any]:
            payload = super().as_metadata()
            if self.horizon:
                payload["horizon"] = self.horizon
            return payload

#: 「改用新值」的表达：用户明确把方案换成**这个**新说法。
#: 与 ADOPTION_MARKERS（同意/就按…）不同，这类话在语义上也等于「采纳了这个新值」，
#: 但它同时是 REJECTION_MARKERS 的一员 —— 所以只取「换成新值」这三个词，
#: **不取** 不用/不要/别用/取消/否决（那些是说旧的不作数，不等于采纳新值）。
#: 缺了这条，模型链路会两边打架：写入侧的替代保护认「改用」而放行，提炼侧却不认
#: 它是采纳，把状态降成 proposed，写入侧再因为 proposed 拒绝归档 —— 正当替代走不通。
SWITCH_TO_MARKERS: tuple[str, ...] = ("改用", "换成", "改为")

#: 「用户为某事项定下了结论」的措辞。与 ADOPTION_MARKERS（同意/就按…）是两回事：
#: 用户直接说「暂不部署」「一律先落盘」时，他已经在**决定**这件事，不需要再补一句
#: 「同意」。旧判据只认同意类词，于是这类明确决定被降成 proposed —— 这就是
#: 「直接表达决定不算决定」的老问题。
#:
#: 取值刻意只收「为事项定结论」的词，不收含糊语气词：
#: - 暂不/先不/不再：用户当下把这件事定成「不做/不做第二次」；
#: - 一律/统一/必须：用户定下一条规则（对某事项的固定做法）；
#: - 改成/改用/换成/改为：用户定下新的取值。
#: 「嗯」「好的」「可以」这类既不含结论、也指代不明，**不在**这里 ——
#: 含糊表态仍必须留在 proposed。
DECISION_MARKERS: tuple[str, ...] = (
    "暂不", "先不", "不再", "一律", "统一", "必须", "改成", "改用", "换成", "改为",
    "作废", "不作数",
    # 「决定用／决定使用／决定采用 X」同样是用户为这件事定下的结论。只收带宾语的
    # 短语，不收裸词「决定」：裸词会被「先不要决定是否采用」这类**收回**表态蹭到，
    # 把「还没定」错记成「已决定」。
    "决定用", "决定使用", "决定采用", "决定选用", "决定选", "决定改为", "决定换成",
)

#: 判「决定」时的疑问窗口词：标记附近出现这些字说明那句在**提问**而不是下结论
#: （「是不是要一律先落盘？」），不算决定。
_DECISION_NEGATIONS: tuple[str, ...] = ("是否", "吗", "？", "?")
_DECISION_WINDOW = 3
#: 分句会把句末问号切掉（「是不是要一律先落盘？」→「是不是要一律先落盘」），
#: 所以提问不能只看句末标点：句首出现这些词同样是在问，不是在下结论。
#: 「是否」可能离标记更远（「是不是**要**一律…」中间隔了字），故按整句句首判。
_DECISION_QUESTION_PREFIXES: tuple[str, ...] = (
    "是不是", "是否", "要不要", "能不能", "可不可以", "该不该", "用不用",
)
#: 标记后面紧接这些词时，说的是「先别急着决定」，恰恰是**没有**决定：
#: 「先不要决定是否采用」「先不确认」都不能当成用户已经定下结论。
_DECISION_WITHHOLD_MARKERS: tuple[str, ...] = (
    "要决定", "要定", "要确认", "急着决定", "急着定", "急着确认",
)
#: 条件、假设、犹豫词：句子里出现这些，说明用户是在**设想**，不是下结论。
#: 「如果不累的话就一律先落盘」「看情况吧，也许一律先落盘」都只是「有决定的
#: 样子」，事情并没有定下来。宁可退回建议，也不替用户拍板。
#: 词表与采纳判据共用同一份（``memory_turn.HEDGE_MARKERS``），避免两处判据打架。
_DECISION_HEDGE_MARKERS: tuple[str, ...] = HEDGE_MARKERS


def _strip_decision_markers(text: Any) -> str:
    """去掉句子里的决定标记词，只留事项本身。

    用途是**事项绑定**：「排期暂不调整」和「部署暂不实施」共有的那个「暂不」
    是「这句在作决定」的标记，不是两件事的共同点。拿它当共同片段，就会让
    两件不相干的事互相绑定。去掉标记后再比事项，才不会张冠李戴。
    """
    raw = str(text or "")
    for marker in DECISION_MARKERS:
        raw = raw.replace(marker, "")
    return raw


#: 只用**一个**普通词就能把两件不相干的事绑在一起，是不够的。实测：
#: 用户说「排期方案暂不调整」，模型输出「部署方案立即执行」却被记成已决定 ——
#: 两件事只共用「方案」这个词。要求至少两处共同实义片段，才认为说的是同一件事。
_SUBJECT_MIN_SHARED_UNITS = 2


def _binds_subject(clause: Any, content: Any, *, minimum: int = _SUBJECT_MIN_SHARED_UNITS) -> bool:
    """这一句说的是不是这条内容的**同一件事**（比 ``cites_item_subject`` 更严）。

    共同片段只数到一个（通常是个普通名词，如「方案」「项目」）时不认：那种
    巧合不足以证明两句话在谈同一件事，会被拿来给另一件事盖章。
    """
    bare_clause = _strip_decision_markers(clause)
    if not cites_item_subject(bare_clause, content):
        return False
    shared = content_units(bare_clause) & content_units(content)
    return len(shared) >= minimum


#: 续写分句的句首词：它接在「不是 A」之后，说的是这条结论的新取值。
_DECISION_CONTINUATION_LEADS: tuple[str, ...] = ("是", "就是", "而是", "就", "便", "则", "现在")


def _is_decision_continuation(clause: Any) -> bool:
    """这一句是不是紧接着上一句的**续写结论**（「是决定使用方案B」）。

    只认句首的肯定/更正引导词：它自己不带事项名，但指的是刚说过的那件事。
    别的句子（「另外…」「顺便…」）不在此列。
    """
    text = str(clause or "").strip()
    if not text:
        return False
    if any(marker in text for marker in ("另外", "顺便", "还有", "对了", "此外")):
        return False
    return text.startswith(_DECISION_CONTINUATION_LEADS)


def _expresses_explicit_decision(text: Any) -> bool:
    """这段原话里有没有「用户为某事项定下结论」的表达。

    只回答「有没有这类说法」，**不判断说的是哪件事** —— 事项绑定统一由
    ``_adoption_matches_subject`` 负责，避免这里出现第二套绑定规则。

    三类「有决定的样子、其实没定」都不算：提问（「是不是要一律先落盘？」）、
    明说先别定（「先不要决定是否采用」）、以及**有条件的设想**（「如果不累的
    话就一律先落盘」）。
    """
    raw = str(text or "").strip()
    if raw.startswith(_DECISION_QUESTION_PREFIXES):
        return False
    if any(hedge in raw for hedge in _DECISION_HEDGE_MARKERS):
        return False
    for marker in DECISION_MARKERS:
        start = 0
        while True:
            at = raw.find(marker, start)
            if at < 0:
                break
            after = raw[at + len(marker) : at + len(marker) + _DECISION_WINDOW + 2]
            if any(withhold in after for withhold in _DECISION_WITHHOLD_MARKERS):
                # 「先不要决定是否采用」：用户明说还没定，不算决定。
                start = at + len(marker)
                continue
            window = raw[max(0, at - _DECISION_WINDOW) : at + len(marker) + _DECISION_WINDOW]
            if not any(negation in window for negation in _DECISION_NEGATIONS):
                return True
            start = at + len(marker)
    return False

#: 不重复事项名称。候选侧核对事项绑定时，只有这一小类可以「不用提到事项名」，
#: 因为它指的就是在谈的这条。光写「同意」不算 ——「项目的排期同意下周开始」
#: 里的「同意」说的是排期，不是候选那条部署方案。
DEICTIC_ADOPTION_MARKERS: tuple[str, ...] = (
    "按这个", "照这个", "就按这个", "就照这个", "按你说的", "就这么", "就这样",
)


def _expresses_deictic_adoption(text: Any) -> bool:
    """这段原话里有没有一处**没被否定**的指代式采纳（「就按这个做」）。

    只认指向「当前讨论的那件事」的说法；把 AC 类判据仍交给 ``expresses_adoption``
    复算一遍，避免否定的窗口规则（「先不要按这个来」）在这里被绕过。
    """
    return expresses_adoption(str(text or "")) and any(
        marker in str(text or "") for marker in DEICTIC_ADOPTION_MARKERS
    )


from .memory_logging import configure_memory_logging

# 提炼层工作日志：与写入/检索/组装同级，输出到控制台（stderr）
LOGGER = logging.getLogger("agent_core.memory.extract")

# 提炼 LLM 默认配置（可用 EXTRACT_* 环境变量覆盖；.env 被 gitignore，不进仓库）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_TOKEN = 512          # 单次提炼的基础预算（推理+正文合计）
MAX_TOKEN_CEILING = 2048         # 预算不足时动态加码的上限
MAX_TOKEN_ESCALATIONS = 2        # 最多连续加码次数（512 → 1024 → 2048）

# 槽位定义：桌宠陪伴维度（元组：(conflict_key, 中文说明, duration 档位)）
DEFAULT_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("user.state.mood",       "即时情绪与压力",     "instant"),
    ("user.habit.schedule",   "作息与生活习惯",     "habit"),
    ("user.preference.likes", "情感倾向与好恶",     "long_term"),
    ("pet.relation.bond",     "与宠物的羁绊互动",   "long_term"),
    ("user.task.current",     "即时工作/任务目标",  "session"),
)
DURATION_OPTIONS: tuple[str, ...] = ("instant", "session", "habit", "long_term")
DEFAULT_DURATION = "long_term"  # 未知档位兜底
MAX_MEMORIES = 5                # 单次提炼上限
MAX_CONTENT_LENGTH = 500        # 单条 content 截断上限

# 提炼 LLM 的 system 提示（固定文案，无占位符）
SYSTEM_PROMPT = """你是桌宠的记忆提炼器，只提炼「陪伴属性」，不提炼业务事实。

规则：
1. 只输出一个 JSON 对象，不要输出任何其他文字、解释或 markdown。
2. 没有值得提炼的新信息时，输出一个空的 memories 数组。
3. 单次最多提炼 5 条。
4. 不提炼：密码、地址、真实姓名等隐私信息；一次性的闲聊客套。
5. 内容用简洁的中文陈述句，保留具体细节（时间、数字、名称），不要笼统。

confidence 置信度：用户明说的偏好/事实/目标给 0.85~1.0；
从语气、上下文推断出的情绪/状态给 0.4~0.7。

duration 时长（这条记忆多久之后还值得记住）：
- instant   瞬时状态（当下的情绪、压力），几小时内就过期
- session   一次任务/会话（当前目标），当天过期
- habit     生活习惯（作息、饮食），长期有效但会变
- long_term 稳定偏好（喜好、羁绊），几乎不变"""

# 槽位表（喂给 LLM，constrain conflict_key 的取值）
SLOT_TABLE = """可用槽位（conflict_key 必须且只能取以下之一）：
- user.state.mood       即时情绪与压力
- user.habit.schedule   作息与生活习惯
- user.preference.likes 情感倾向与好恶
- pet.relation.bond     与宠物的羁绊互动
- user.task.current     即时工作/任务目标"""

# 输出 schema 示例（放在 user 消息里，避免 system 文案里的花括号冲突）
OUTPUT_SCHEMA_EXAMPLE = (
    '{"memories": [{"content": "用户今天被老板批评了，心情很低落", '
    '"conflict_key": "user.state.mood", "confidence": 0.7, '
    '"importance": 0.8, "duration": "instant"}]}'
)


class ExtractionProvider(Protocol):
    """提炼后端契约：任何带 ``async complete`` 的对象都可作提炼 LLM。

    ``OpenAICompatibleProvider`` 天然满足；测试用 fake 对象注入。
    """

    async def complete(
        self,
        message: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse: ...


@dataclass(frozen=True)
class ExtractedMemory:
    """一条提炼出的记忆（对应将来写入 memory_records 的一行）。"""

    content: str
    conflict_key: str
    confidence: float          # 0.0~1.0
    importance: float          # 0.0~1.0
    duration: str              # instant/session/habit/long_term
    source_type: str = "llm_extracted"


class MemoryExtractor:
    """把一轮对话提炼成 ``list[ExtractedMemory]``；任何异常都降级为 []。"""

    def __init__(
        self,
        provider: ExtractionProvider,
        *,
        slots: tuple[tuple[str, str, str], ...] = DEFAULT_SLOTS,
        temperature: float = 0.2,
        max_token: int = DEFAULT_MAX_TOKEN,
        token_ceiling: int = MAX_TOKEN_CEILING,
    ) -> None:
        self.provider = provider
        self.slots = tuple(slots)
        self.temperature = temperature
        self.max_token = max_token          # 单次提炼的基础预算（推理+正文合计）
        self.token_ceiling = token_ceiling  # 预算不足时动态加码的上限
        # 提炼日志直接输出到控制台（stderr）；重复挂 handler 由 memory_logging 幂等处理
        configure_memory_logging()

    async def extract(
        self,
        user_text: str,
        *,
        assistant_reply: str = "",
        history: list[dict[str, Any]] | None = None,
    ) -> list[ExtractedMemory]:
        """调 provider → 解析 → 返回结构化结果；任何异常都返回 []，绝不外抛。

        动态 token 分配：预算从 ``self.max_token`` 起步，若正文为空且输出 token
        顶到本次预算（说明推理阶段把预算吃满、正文没地方写），就 ×2 加码重试，
        直到超出 ``self.token_ceiling`` 为止。常见轮次推理少，一次通过；只有
        推理爆发时才付出更大预算。
        """
        LOGGER.info(
            "提炼开始：用户输入 %d 字，历史 %d 条，助手回复 %d 字",
            len(user_text),
            len(history) if history else 0,
            len(assistant_reply),
        )
        try:
            messages = self._build_prompt(user_text, assistant_reply, history)
        except Exception as exc:
            LOGGER.warning("构建提炼提示失败，已降级为空结果：%s", exc)
            return []

        budget = self.max_token
        text = ""
        usage: dict[str, Any] = {}
        try:
            for _ in range(MAX_TOKEN_ESCALATIONS + 1):
                response = await self.provider.complete(
                    messages, temperature=self.temperature, max_tokens=budget
                )
                usage = response.usage or {}
                if isinstance(response.message, dict):
                    text = response.message.get("content") or ""
                if isinstance(text, str) and text.strip():
                    break  # 有正文，直接采用
                # 正文为空：若是预算被吃满（推理截断），加大预算重试；否则是真·空响应
                if not self._budget_exhausted(usage, budget) or budget >= self.token_ceiling:
                    break
                next_budget = min(budget * 2, self.token_ceiling)
                LOGGER.warning(
                    "提炼预算被推理占满（输出=%s 达到上限 %d），动态加大到 %d 重试",
                    self._completion_tokens(usage), budget, next_budget,
                )
                budget = next_budget
        except Exception as exc:
            LOGGER.warning("提炼调用失败，已降级为空结果：%s", exc)
            return []

        if not isinstance(text, str) or not text.strip():
            LOGGER.warning("提炼响应为空或无可读内容，未提取到记忆")
            return []
        memories = self._parse(text)
        LOGGER.info(
            "提炼完成：共 %d 条记忆 %s，token 消耗 %s",
            len(memories),
            self._slot_breakdown(memories),
            self._usage_text(usage),
        )
        return memories

    # ------------------------------------------------------------------
    # 日志辅助（纯静态，绝不抛异常）
    # ------------------------------------------------------------------
    @staticmethod
    def _slot_breakdown(memories: list[ExtractedMemory]) -> str:
        """把提炼结果按槽位聚合成可读分布，如「user.state.mood×1」；空结果返回「无」。"""
        if not memories:
            return "无"
        counts: dict[str, int] = {}
        for m in memories:
            counts[m.conflict_key] = counts.get(m.conflict_key, 0) + 1
        return " ".join(f"{k}×{v}" for k, v in sorted(counts.items()))

    @staticmethod
    def _usage_text(usage: dict[str, Any]) -> str:
        """把 provider 的 usage dict 转成中文标签；缺失的字段跳过，绝不抛异常。"""
        if not isinstance(usage, dict) or not usage:
            return "无"
        labels = {"prompt_tokens": "输入", "completion_tokens": "输出", "total_tokens": "总"}
        parts = [f"{labels.get(k, k)}={v}" for k, v in usage.items() if isinstance(v, (int, float))]
        return " ".join(parts) if parts else "无"

    @staticmethod
    def _completion_tokens(usage: dict[str, Any]) -> Any:
        """安全取输出 token 数；usage 缺失/非法返回 None，绝不抛异常。"""
        if isinstance(usage, dict):
            return usage.get("completion_tokens")
        return None

    @staticmethod
    def _budget_exhausted(usage: dict[str, Any], budget: int) -> bool:
        """截断信号：输出 token 达到（或超过）本次预算，说明预算被推理/生成吃满。

        推理模型先「想」后「答」，若推理阶段就把预算写满，正文会为空——这时
        加大预算重试才可能拿到正文。输出 token 没顶满却正文为空，则是真·空响应。
        """
        completion = MemoryExtractor._completion_tokens(usage)
        return isinstance(completion, (int, float)) and completion >= budget

    # ------------------------------------------------------------------
    # prompt 构造
    # ------------------------------------------------------------------
    def _build_prompt(
        self,
        user_text: str,
        assistant_reply: str,
        history: list[dict[str, Any]] | None,
    ) -> list[dict[str, str]]:
        lines = []
        if history:
            for entry in history[-6:]:  # 只带最近 6 条，控制 token 开销
                if not isinstance(entry, dict):
                    continue
                # work.result 桥写回的系统回执（"「X」完成了/失败了"）与管家过程句
                # （"我先把论文找出来看看"）都不进记忆提炼：前者是后台状态播报，
                # 后者是过程语气词，都不是用户的偏好或事实。让它们进提炼上下文会被
                # 提炼器当成"发生过的事实"记进长期记忆库，越用越脏。
                # 但两者都**保留在对话上下文里**：用户接着说"继续""批准"时，
                # 模型需要看见现在走到哪一步了。
                if entry.get("kind") in NON_MEMORY_KINDS:
                    continue
                role = entry.get("role")
                content = entry.get("content")
                if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                    lines.append(f"{role}: {content.strip()}")
        lines.append(f"user: {user_text.strip()}")
        if assistant_reply and assistant_reply.strip():
            lines.append(f"assistant: {assistant_reply.strip()}")

        user_message = "\n".join(
            [
                "【本轮对话】",
                "\n".join(lines),
                "",
                "【可用槽位】",
                SLOT_TABLE,
                "",
                "【输出格式】只输出下面这样的 JSON，不要输出其他文字：",
                OUTPUT_SCHEMA_EXAMPLE,
            ]
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]

    # ------------------------------------------------------------------
    # 解析（纯同步纯逻辑，绝不抛异常）
    # ------------------------------------------------------------------
    def _parse(self, text: str) -> list[ExtractedMemory]:
        """兜底解析链：剥代码块 → 取 JSON → 校验 → 过滤非法项 → []。"""
        data = self._load_json(text)
        if data is None:
            return []

        raw_items = data.get("memories")
        if not isinstance(raw_items, list):
            return []

        valid_keys = {slot[0] for slot in self.slots}
        memories: list[ExtractedMemory] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, str) or not content.strip():
                continue  # content 非空字符串，否则整条丢弃
            conflict_key = item.get("conflict_key")
            if conflict_key not in valid_keys:
                continue  # 槽位不在表内，丢弃（非法槽位）

            confidence = self._clamp_float(item.get("confidence"), 0.5)
            importance = self._clamp_float(item.get("importance"), 0.5)
            duration = item.get("duration", DEFAULT_DURATION)
            if duration not in DURATION_OPTIONS:
                duration = DEFAULT_DURATION  # 未知档位 → long_term

            memories.append(
                ExtractedMemory(
                    content=content.strip()[:MAX_CONTENT_LENGTH],
                    conflict_key=conflict_key,
                    confidence=confidence,
                    importance=importance,
                    duration=duration,
                )
            )
            if len(memories) >= MAX_MEMORIES:
                break
        return memories

    @staticmethod
    def _load_json(text: str) -> dict[str, Any] | None:
        """剥 ```json 围栏 → 取首 { 到尾 } → json.loads；失败返回 None。"""
        try:
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                return None
            data = json.loads(text[start : end + 1])
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _clamp_float(value: Any, default: float) -> float:
        """把任意值转成 0.0~1.0 的浮点；缺失/非法用 default 兜底。"""
        if isinstance(value, bool):
            return default
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return min(1.0, max(0.0, number))


def build_extractor_from_env(
    env_path: str | Path = PROJECT_ENV_PATH,
) -> MemoryExtractor | None:
    """从 .env 构建提炼器；缺 EXTRACT_API_KEY 时返回 None（优雅降级，不抛）。

    惰性 import ``OpenAICompatibleProvider``：避免 agent_core 反向依赖
    agent_core_adapters（与 web_search 的降级惯例一致）。
    """
    load_dotenv(dotenv_path=Path(env_path), override=False)
    api_key = os.environ.get("EXTRACT_API_KEY", "").strip()
    if not api_key:
        return None

    base_url = os.environ.get("EXTRACT_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
    model = os.environ.get("EXTRACT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        timeout = float(os.environ.get("EXTRACT_TIMEOUT", str(DEFAULT_TIMEOUT)) or DEFAULT_TIMEOUT)
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    try:
        max_token = int(os.environ.get("EXTRACT_MAX_TOKEN", str(DEFAULT_MAX_TOKEN)) or DEFAULT_MAX_TOKEN)
    except ValueError:
        max_token = DEFAULT_MAX_TOKEN

    from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

    provider = OpenAICompatibleProvider(
        base_url=base_url,
        api_key=api_key,
        model=model,
        max_token=max_token,
        timeout=timeout,
        # 提炼这条路必须显式开思考。默认是关的（面向用户的对话刻意关掉推理，
        # 见 test_openai_compatible 里钉住的行为），但强制思考的模型收到
        # "disabled" 会直接回 400；而且即使不报错，推理预算不够时正文会被
        # 截断成空串，表现成"什么都没提炼出来"——比报错更难查。
        thinking_enabled=True,
    )
    return MemoryExtractor(
        provider,
        max_token=max_token,
    )


# ---------------------------------------------------------------------------
# 统一候选提炼（B 计划第二步）
#
# 与上面的陪伴提炼器是**两条并存的路**：旧的那条只做兼容回退，新的这条覆盖
# 七类内容（含项目决策与原因）。两者共用一个 provider 协议，但提示词、输出
# 结构和落库方式都不同 —— 新的是「提交候选」，能不能存由程序校验说了算。
# ---------------------------------------------------------------------------

#: 状态表直接从契约模块生成，避免这里和 memory_turn 各写一份、慢慢对不上。
_KIND_TABLE = "\n".join(
    f"- {kind}：{'、'.join(statuses)}" for kind, statuses in CONTENT_STATUSES.items()
)

#: 单回合候选上限 = 七类内容各一条。上限由类型数量决定，不另外拍一个数字。
SEMANTIC_MAX_CANDIDATES = len(CONTENT_STATUSES)

#: 新版候选提炼的**独立**输出预算。
#: 刻意与旧的陪伴提炼器分开：旧的那条基础预算是 512，而新版一次要出七类
#: 结构化候选。推理和正文共用这 512 时，推理一爆发正文就被挤成空串，
#: 表现成「什么都没提炼出来」—— 比报错更难查（B-R11）。
SEMANTIC_MAX_TOKEN = 2048
SEMANTIC_TOKEN_CEILING = 4096
SEMANTIC_TOKEN_ESCALATIONS = 2


class MemoryExtractionError(RuntimeError):
    """提炼**失败**：连接错误、空正文、结构无效、额度耗尽。

    这和「提炼成功、但确实没有内容」是两件事。以前两者都返回空列表，处理
    工人分不出来，于是失败被记成 empty、永远不重试 —— 一次断网就等于这一轮
    永远没被记住（B-R2）。现在失败会抛出来，由队列按次数上限重试。
    """

SEMANTIC_SYSTEM_PROMPT = """你是记忆候选提炼器。你只**提交候选**，不决定最终存档。

规则：
1. 只输出一个 JSON 对象，不要输出任何其他文字或 markdown。
2. 没有值得记住的新信息时，输出 {"memories": []}。
3. 单次最多提炼 %d 条。""" % SEMANTIC_MAX_CANDIDATES + """
4. 不提炼密码、地址、真实姓名等隐私信息，也不提炼寒暄客套。
5. 内容用简洁的中文陈述句，保留具体细节（时间、数字、名称、文件或方案名）。

每条必须给出 kind 与 content_status，取值只能来自下面的表：

""" + _KIND_TABLE + """

content_status 只描述**内容当前走到哪一步**，不是「你有多确定」：
- 用户只是提出想法 → proposed；用户明确说「就按这个做」→ accepted；
- 计划只有真的完成证据才算 completed，你不要替它宣布；
- 事实里 reported 是用户自己说的，verified 必须由可靠工具结果支撑；
- 不要因为「用户语气很肯定」或「你理解得很清楚」就写 verified / completed。

source_types 可以多选，取值只能是 user_statement、tool_result、
assistant_suggestion、model_inference。外部网页材料属于证据，不是用户偏好。

reason 存「为什么这么选」；不知道原因就留空字符串，**不要编造用户动机**。
topics 用少量稳定标签（如 记忆、搜索、项目架构、运行部署、个人偏好）。
related_ids / supersedes 只能填输入里出现过的记忆 ID，不得虚构。
如果输入里说「方案 A 已定」但没有给出 A 的内容，写成 open_question 记录这个缺口，
不要替它补出定义。
horizon 只能填 long_term（长期事实/偏好/约束）或 short_term（当下情绪/当前任务）。

**已经存在的记忆要不要改**：不确定就写 action=add（新建）。
只有下面这些情况才写动作，而且**必须填 target_id**（只能填输入里出现过的记忆 ID）：

- merge：同一件事多了新原因、条件或出处，两个说法并不冲突；
- correct：用户明确指出旧的说法错了；
- replace：用户明确说「改用 B、不再用 A」这类替代；
- conflict：两条可信说法互相矛盾，时间和范围都解释不了；
- invalidate：明确到期、取消，或可靠新证据说明它不再适用；
- ignore：内容已经存在、变化没有价值、只是重复寒暄。

correct / replace / invalidate 要填 expected_version（那条记忆现在第几版，
输入里会写）。**不许自己编版本号**：对不上程序会拒绝这条建议。
不要因为「新消息更晚」就把旧结论改成错的；「以后再说」「先这样」这类
含糊表态只能写 proposed，不能写成 accepted。

confidence 是「你对句意的把握」（0~1），与上面所有状态无关。"""

SEMANTIC_SCHEMA_EXAMPLE = (
    '{"memories": [{"content": "决定让主回答模型在回答发布后参与候选记忆提炼", '
    '"kind": "decision", "content_status": "accepted", "source_types": ["user_statement"], '
    '"reason": "不想陪伴记忆和项目记忆分裂", "goal": "同一份上下文", '
    '"topics": ["记忆"], "related_ids": [], "supersedes": [], '
    '"confidence": 0.9, "importance": 0.8}]}'
)


def render_turn_packet(packet: Any) -> str:
    """把回合处理包渲染成提炼输入。

    只放处理包里已有的东西：这里**不会**再去查库、跑搜索或读全量历史 ——
    提炼看到什么，取决于调用方显式塞了什么。
    """
    lines: list[str] = ["【本轮对话】"]
    if packet.task_goal:
        lines.append(f"任务目标: {packet.task_goal}")
    for item in packet.relevant_history:
        if item.content.strip():
            lines.append(f"{item.role}: {item.content.strip()}")
    lines.append(f"user: {packet.user_text.strip()}")
    if packet.is_published():
        lines.append(f"assistant: {packet.published_reply.strip()}")
    else:
        lines.append("（本条助手回复未发布，不得作为助手结论使用）")

    if packet.recalled_memory_refs:
        lines.append("")
        lines.append("【本轮已用到的记忆（关系、目标与版本只能引用这里的 ID）】")
        for ref in packet.recalled_memory_refs:
            lines.append(
                f"- {ref.memory_id}（第 {int(ref.version)} 版）｜{ref.kind}/{ref.content_status}｜"
                f"{ref.excerpt.strip()}"
            )
            if ref.semantic_judgment:
                lines.append('  之前模型的判断与原文（可据新证据纠正，模型判断本身不是新证据）：' + json.dumps(ref.semantic_judgment,ensure_ascii=False))
    if packet.evidence_refs:
        lines.append("")
        lines.append("【外部证据与工具状态】")
        for ref in packet.evidence_refs:
            lines.append(f"- {ref.ref_id}｜{ref.outcome}｜{ref.summary.strip()}")
    if packet.omitted_sections or packet.completeness != "complete":
        lines.append("")
        lines.append(
            "【背景缺口】" + "、".join(packet.omitted_sections or ("current_turn",))
            + "：这些背景没有被提供，**不要因此推断「用户没有约束」**。"
        )
    lines.append("")
    lines.append("【输出格式】只输出下面这样的 JSON，不要输出其他文字：")
    lines.append(SEMANTIC_SCHEMA_EXAMPLE)
    return "\n".join(lines)


class SemanticMemoryExtractor:
    """把回合处理包提炼成 ``list[MemoryCandidate]``；任何异常都降级为 []。

    与陪伴提炼器同样不抛异常：提炼失败不该影响已经发布的回答。
    """

    def __init__(
        self,
        provider: ExtractionProvider,
        *,
        temperature: float = 0.2,
        max_token: int = SEMANTIC_MAX_TOKEN,
        token_ceiling: int = SEMANTIC_TOKEN_CEILING,
    ):
        self.provider = provider
        self.temperature = temperature
        # 输出预算是**独立**的：不复用旧陪伴提炼器的 512。见 SEMANTIC_MAX_TOKEN。
        self.max_token = max_token
        self.token_ceiling = token_ceiling
        configure_memory_logging()

    async def _complete(self, packet: Any) -> str:
        """调模型拿正文。

        失败（连接错误、空正文、结构无效）**抛 MemoryExtractionError**，
        不返回空串：返回空等于告诉队列「这轮没内容」，队列就会记成 empty
        并结束，失败从此无声无息。模型明确说「没有值得记的」才是真的没有内容，
        那种情况返回的是合法 JSON，走得到下面。
        """
        messages = [
            {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
            {"role": "user", "content": render_turn_packet(packet)},
        ]
        budget = self.max_token
        text = ""
        usage: dict[str, Any] = {}
        for _ in range(SEMANTIC_TOKEN_ESCALATIONS + 1):
            try:
                response = await self.provider.complete(
                    messages, temperature=self.temperature, max_tokens=budget
                )
            except Exception as exc:  # noqa: BLE001 - 抛给队列记账并按次数重试
                LOGGER.warning("候选提炼调用失败：%s", exc)
                raise MemoryExtractionError(f"provider_failed:{type(exc).__name__}") from exc
            usage = getattr(response, "usage", None) or {}
            text = response.message.get("content") if isinstance(response.message, dict) else ""
            if isinstance(text, str) and text.strip():
                break
            # 正文为空、输出 token 又顶满了本次预算 → 推理把预算吃掉了，加大重试。
            if not MemoryExtractor._budget_exhausted(usage, budget) or budget >= self.token_ceiling:
                break
            next_budget = min(budget * 2, self.token_ceiling)
            LOGGER.warning(
                "候选提炼预算被推理占满（输出=%s 达到上限 %d），加大到 %d 重试",
                MemoryExtractor._completion_tokens(usage), budget, next_budget,
            )
            budget = next_budget
        if not isinstance(text, str) or not text.strip():
            raise MemoryExtractionError("empty_response")
        return text

    async def extract(self, packet: Any, *, known_ids=None) -> list[MemoryCandidate]:
        """只取「新建候选」。内容更新建议由 :meth:`extract_maintenance` 取。"""
        text = await self._complete(packet)
        candidates, suggestions, rejected, item_count = self._split_items(
            text, packet=packet, known_ids=known_ids
        )
        self._raise_if_all_rejected(candidates, suggestions, rejected, item_count)
        if suggestions:
            LOGGER.warning(
                "本次提炼含 %d 条内容更新建议，当前入口尚未接入内容维护，先只记录不落库",
                len(suggestions),
            )
        LOGGER.info("候选提炼完成：候选=%d 拒绝=%d", len(candidates), len(rejected))
        return candidates

    async def extract_maintenance(
        self,
        packet: Any,
        *,
        known_ids=None,
        known_versions=None,
    ) -> tuple[list[MemoryCandidate], list[MemoryUpdateSuggestion]]:
        """一次调用同时读出「新建候选」和「对旧记录的更新建议」。

        合成一次调用是因为这两件事看的是同一段上下文：分成两次调模型，同一句话
        会得出两套略有差别的结论，然后谁也说不清哪套为准。
        """
        text = await self._complete(packet)
        candidates, suggestions, rejected, item_count = self._split_items(
            text, packet=packet, known_ids=known_ids, known_versions=known_versions
        )
        self._raise_if_all_rejected(candidates, suggestions, rejected, item_count)
        LOGGER.info(
            "提取完成：新建=%d 更新建议=%d 拒绝=%d %s",
            len(candidates),
            len(suggestions),
            len(rejected),
            " ".join(f"{s.action}/{s.kind or '-'}" for s in suggestions) or "无更新建议",
        )
        return candidates, suggestions

    #: 整份输出读不出来的两种情况。与「某一条不合规」不同：这两条是**结构**失败，
    #: 必须让上层看到失败并重试，而不是当成「模型说没有值得记的」。
    _STRUCTURAL_FAILURES = frozenset({"invalid_json", "missing_memories"})

    @staticmethod
    def _raise_if_all_rejected(candidates, suggestions, rejected, item_count: int) -> None:
        """模型给了条目却一条都没通过核对 → 按提炼失败处理，交队列重试。"""
        reasons = {str(item.get("reason")) for item in rejected}
        if reasons & SemanticMemoryExtractor._STRUCTURAL_FAILURES:
            LOGGER.warning("候选提炼输出结构无效，按失败处理")
            raise MemoryExtractionError("invalid_structure")
        if item_count and not candidates and not suggestions:
            reason = str(rejected[0].get("reason")) if rejected else "unknown"
            LOGGER.warning("候选全部被拒，按失败处理：%s", reason)
            raise MemoryExtractionError(f"all_candidates_rejected:{reason}")

    def parse(self, text: str, *, packet: Any, known_ids=None) -> tuple[list[MemoryCandidate], list[dict]]:
        """只取「新建候选」。

        带维护动作的条目由 :meth:`extract_maintenance` 取走；在这里如实标成一条
        记录，不静默吞掉 —— 否则模型一旦输出带动作的条目，这条入口看着就是
        「什么都没提炼出来」。
        """
        candidates, suggestions, rejected, _ = self._split_items(
            text, packet=packet, known_ids=known_ids
        )
        rejected.extend(
            {"reason": f"带维护动作 {item.action}，需走内容维护入口", "content": item.content[:40]}
            for item in suggestions
        )
        return candidates, rejected

    def parse_maintenance(
        self,
        text: str,
        *,
        packet: Any,
        known_ids=None,
        known_versions=None,
    ) -> tuple[list[MemoryCandidate], list[MemoryUpdateSuggestion], list[dict]]:
        """解析一次模型输出，返回 ``(新建候选, 更新建议, 拒绝记录)``。

        与 :meth:`parse` 用同一套核对：动作与目标先过，再看来源与状态。
        """
        candidates, suggestions, rejected, _ = self._split_items(
            text, packet=packet, known_ids=known_ids, known_versions=known_versions
        )
        return candidates, suggestions, rejected

    def _split_items(
        self,
        text: str,
        *,
        packet: Any,
        known_ids=None,
        known_versions=None,
    ) -> tuple[list[MemoryCandidate], list[MemoryUpdateSuggestion], list[dict], int]:
        """把模型输出拆成「新建候选」和「更新建议」，逐条做程序侧核对。

        三道核对，全部由程序做，不采信模型的自述：

        1. **结构**：正文必须是合法 JSON 且有 memories 列表，否则整份拒收；
        2. **来源**：source_refs 必须指向本回合真实存在的消息；标了「用户说的」
           却只挂助手来源的也拒收；更新建议的目标必须落在本次**已授权**输入里；
        3. **状态**：声称「用户已采纳／已否决」的，必须在用户原话里找到对应
           表达，找不到就退回 proposed —— 采纳是事实判断，不是标签。

        返回 ``(候选, 建议, 拒绝记录, 条目总数)``。条目总数给上层判断
        「模型给了东西但一条都没通过」算不算失败。
        """
        # 复用陪伴提炼器那套兜底解析链（剥围栏 → 取首尾花括号 → json.loads）。
        data = MemoryExtractor._load_json(text)
        if not isinstance(data, dict):
            return [], [], [{"reason": "invalid_json"}], 0
        raw_items = data.get("memories")
        if not isinstance(raw_items, list):
            return [], [], [{"reason": "missing_memories"}], 0

        merged_known = list(known_ids or ())
        if not merged_known:
            # 关系只能指向输入里出现过的记忆 ID。
            merged_known = [ref.memory_id for ref in packet.recalled_memory_refs]
        # 真实存在的来源引用 → 角色。模型编出来的引用在这一步就对不上。
        known_roles = packet_source_roles(packet)
        versions = dict(known_versions or {})
        candidates: list[MemoryCandidate] = []
        suggestions: list[MemoryUpdateSuggestion] = []
        rejected: list[dict] = []
        for item in raw_items:
            if not isinstance(item, dict):
                rejected.append({"reason": "not_an_object"})
                continue
            action = str(item.get("action") or "add").strip()
            if action not in ("add", "新增"):
                # 带动作的条目：内容更新建议，先去核对动作与目标，再核来源与状态。
                suggestion = self._suggestion_from_item(item, packet=packet, action=action)
                try:
                    validate_suggestion(
                        suggestion,
                        known_ids=merged_known,
                        known_versions=versions or None,
                    )
                except MemoryCandidateError as exc:
                    rejected.append({"reason": str(exc), "content": suggestion.content[:40]})
                    continue
                if not self._source_refs_ok(suggestion, known_roles, rejected):
                    continue
                suggestions.append(suggestion)
                continue
            try:
                candidate = self._candidate_from_item(item, packet)
            except MemoryCandidateError as exc:
                # 条目本身不合规（如 horizon 取值非法）：只拒这一条，不牵连整批。
                rejected.append(
                    {"reason": str(exc), "content": str(item.get("content") or "")[:40]}
                )
                continue
            if not self._source_refs_ok(candidate, known_roles, rejected):
                continue
            # Semantic status is checked by the reviewing memory agent. Parsing
            # validates structure and real source identities, not word lists.
            try:
                validate_candidate(candidate, known_ids=merged_known)
            except MemoryCandidateError as exc:
                rejected.append({"reason": str(exc), "content": candidate.content[:40]})
                continue
            candidates.append(candidate)
            if len(candidates) >= SEMANTIC_MAX_CANDIDATES:
                break
        return candidates, suggestions, rejected, len(raw_items)

    @staticmethod
    def _source_refs_ok(record: Any, known_roles: dict, rejected: list[dict]) -> bool:
        """来源引用必须真实存在，来源类别与引用角色要对得上。"""
        unknown_refs = [ref for ref in record.source_refs if ref not in known_roles]
        if unknown_refs:
            rejected.append(
                {
                    "reason": f"来源引用不存在：{','.join(unknown_refs)}",
                    "content": record.content[:40],
                }
            )
            return False
        if "user_statement" in record.source_types:
            roles = {role for ref in record.source_refs for role in known_roles.get(ref, ())}
            if "user" not in roles:
                rejected.append(
                    {
                        "reason": "声称用户来源，但引用里没有用户消息",
                        "content": record.content[:40],
                    }
                )
                return False
        return True

    @staticmethod
    def _target_excerpt(packet: Any, target_id: str) -> str:
        """本次输入里那条目标记忆的摘要 —— 用来核对证据说的是不是同一件事。"""
        for ref in getattr(packet, "recalled_memory_refs", ()) or ():
            if str(getattr(ref, "memory_id", "")) == str(target_id):
                return str(getattr(ref, "excerpt", "") or "")
        return ""

    @classmethod
    def _apply_status_evidence(cls, candidate: MemoryCandidate, packet: Any) -> MemoryCandidate:
        """声称「用户已采纳／已否决」却没有用户原话支撑时，退回 proposed。

        退回而不是丢弃：内容本身可能是有价值的建议，只是**状态**不能由模型
        自己盖章。这也符合「不把用户确认当成入库门槛」—— 东西照存，状态照实。

        两道关，都要过：

        1. 证据必须落在**被引用的**用户消息上，且本轮明说「还没定」时不算
           （``adoption_evidenced`` 的既有规则）；
        2. 该证据说的必须是**这条候选事项**（``_adoption_matches_subject``）。

        第 1 关额外放行「改用新值」这类替代措辞：写入侧的替代保护本来就认它
        （``_REPLACEMENT_MARKERS``），提炼侧若不认，「用户明确改用 B」会在
        这里被降成 proposed、写入侧再因 proposed 拒绝归档，正当替代反而走不通。
        """
        if (candidate.kind, candidate.content_status) not in USER_ADOPTION_STATUSES:
            return candidate
        if not cls._adoption_matches_subject(packet, candidate):
            return cls._downgrade(candidate)
        if (
            adoption_evidenced(
                packet,
                candidate.content_status,
                source_refs=candidate.source_refs,
                item_text=candidate.content,
            )
            or cls._switch_wording_evidenced(packet, candidate)
            or cls._decision_wording_evidenced(packet, candidate)
        ):
            return candidate
        return cls._downgrade(candidate)

    @staticmethod
    def _downgrade(candidate: MemoryCandidate) -> MemoryCandidate:
        LOGGER.info(
            "采纳状态缺少用户原话证据，退回 proposed：kind=%s 内容=%s",
            candidate.kind,
            candidate.content[:20],
        )
        return replace(candidate, content_status="proposed", evidence_level="adoption_unverified")

    @staticmethod
    def _decision_wording_evidenced(packet: Any, candidate: MemoryCandidate) -> bool:
        """用户在被引用的原话里为**这条候选事项**直接定下了结论。

        与 ``_switch_wording_evidenced``、``_adoption_matches_subject`` 共用同一套
        事项绑定：只有「同一句里既提到候选事项、又出现明确决定措辞」才算数，
        所以「排期暂不调整」证明不了「部署暂不实施」。

        为什么要单独一条：``ADOPTION_MARKERS`` 只收「同意／采纳／批准」这类
        应答词，而用户直接说「暂不部署」「一律先落盘」时，他已经在决定这件事，
        不该再被要求补一句「同意」—— 这正是「直接表达决定不算决定」的老问题。
        含糊表态（「嗯」「好的」）不在 ``DECISION_MARKERS`` 里，仍留在 proposed。
        """
        if candidate.content_status != "accepted":
            return False
        content = str(candidate.content or "")
        if not content.strip():
            return False
        # 绑定事项前先把决定措辞本身去掉：「排期暂不调整」与「部署暂不实施」
        # 共有的「暂不」是**判决定的标记**，不是共同事项，否则两件不相干的事
        # 会因为都用了「暂不」而互相绑定。
        bare_content = _strip_decision_markers(content)
        for text, _is_current in cited_user_evidence(packet, candidate.source_refs):
            for sentence in sentences(text) or (str(text or ""),):
                if is_hedged(sentence):
                    continue
                parts = clauses(sentence) or (sentence,)
                # 事项在**这一句**里出现过；决定措辞落在紧跟其后的续写分句上
                # （「回滚方案不是选择使用方案A，是决定使用方案B」）时认这一条 ——
                # 续写句本身不重复事项名，不能因此判成「说的是别的事」。
                anchored = any(_binds_subject(part, bare_content) for part in parts)
                for index, clause in enumerate(parts):
                    if not _expresses_explicit_decision(clause):
                        continue
                    if _binds_subject(clause, bare_content):
                        return True
                    if (
                        anchored
                        and index > 0
                        and _is_decision_continuation(clause)
                    ):
                        return True
        return False

    @staticmethod
    def _switch_wording_evidenced(packet: Any, candidate: MemoryCandidate) -> bool:
        """用户在被引用的原话里用「改用／换成／改为」明确换到**这条候选**的新值。

        与 ``_adoption_matches_subject`` 同一套事项绑定（同一句里既提到候选事项、
        又出现换值措辞），所以「排期改用周二」证明不了「预算改为三千」。
        只认换到新值的三个词，不认 不用/不要/取消/否决 —— 那些是说旧的不作数，
        不等于采纳新值。
        """
        if candidate.content_status != "accepted":
            return False
        content = str(candidate.content or "")
        if not content.strip():
            return False
        return any(
            any(marker in clause for marker in SWITCH_TO_MARKERS)
            for text, _is_current in cited_user_evidence(packet, candidate.source_refs)
            # 有条件的设想不算换值：「如果预算够，就改用方案B」只是设想。
            # 条件按整句判（逗号不算断句），否则后半句会漏过去。
            for clause in unhedged_clauses(text)
            if cites_item_subject(clause, content)
        )

    @staticmethod
    def _adoption_matches_subject(packet: Any, candidate: MemoryCandidate) -> bool:
        """被引用的用户原话里，采纳表达指的**是不是这个候选事项**。

        ``adoption_evidenced`` 对**本轮用户原话**不做事项绑定（只有历史消息才
        逐句绑定事项），所以本轮里任意一句「同意」都能被模型拿来给另一件事盖章：
        「同意A；B你再分析一下」会把 B 也盖成 accepted。这里补上绑定。

        规则分三种，覆盖「指代式采纳」「跨句指代确认」与「张冠李戴」：

        - 原话里**有一句**既带采纳表达、又提到候选事项 → 算绑定，认。
        - 原话里**提了**候选事项，而带采纳表达的是**指代式**句子（「那就按这个
          来吧」）→ 认。用户先交代事项、紧接着用「这个」确认，指的就是刚说的
          那件事，不能因为确认句没重复事项名就判成没绑定。
        - 原话里**提了**候选事项，但带采纳表达的是**点名了别的说法**的句子
          （「同意以后回答先说结论」）→ 采纳说的是别的事，**不认**。「同意A；
          云端部署的建议你再分析一下」对「云端部署」正是这一类。
        - 原话里**根本没提**候选事项 → 只有指代式采纳才放行（同上，既有用例
          「就按这个做，先别部署」对「统一记忆规则」属于这一类）。

        判据归纳：带采纳表达的那一句，要么自己提到事项，要么是**指代式**的
        （指代词指的就是对话里正在谈的那件事）。这样「改用／一律／暂不部署」
        这类不带「同意」字样的显式决定仍然由既有词表判定。

        ``rejected`` 走同一套绑定：判「否决」用既有否决词，但同样要求落在
        提到该事项的那一句上（「云端部署不用了」认，「排期不用了」不认）。
        """
        content = str(candidate.content or "")
        if not content.strip():
            return False
        rejected = candidate.content_status == "rejected"
        for text, _is_current in cited_user_evidence(packet, candidate.source_refs):
            body = str(text or "")
            subject_named = cites_item_subject(body, content)
            # 条件句整句不算表态（「如果不累的话，就一律先落盘」）。
            for clause in unhedged_clauses(body) or (body,):
                mentions = cites_item_subject(clause, content)
                if rejected:
                    if mentions and any(marker in clause for marker in REJECTION_MARKERS):
                        return True
                    continue
                if mentions and (
                    expresses_adoption(clause, item_text=content)
                    or any(marker in clause for marker in SWITCH_TO_MARKERS)
                    or _expresses_explicit_decision(clause)
                ):
                    return True
                # 跨句指代确认：事项在本段原话里出现过，这一句用「这个/这样」指它。
                if subject_named and mentions is False and _expresses_deictic_adoption(clause):
                    return True
            # 原话提到过这件事，却没找到能绑定的那一句 → 张冠李戴，不放行。
            if subject_named:
                continue
            # 原话完全没提这件事：只有**指代式**采纳才按「指的是当前讨论的事」放行
            # （「就按这个做」）。带具体别的说法的「同意」不算，否则一句无关的
            # 「项目的排期同意下周开始」就能给任何候选盖章。
            if _expresses_deictic_adoption(text):
                return True
        return False

    @classmethod
    def _apply_suggestion_status_evidence(
        cls, suggestion: MemoryUpdateSuggestion, packet: Any
    ) -> MemoryUpdateSuggestion:
        """更新建议里的「已采纳」同样要用户原话支撑，否则退回 proposed。

        判据与候选侧共用同一套：只认「同意类应答」不够 —— 用户直接说
        「改用方案B，不再用方案A」时他已经在决定这件事，这里若只认应答词，
        正当替代会被降成 proposed，写入侧再因 proposed 拒绝归档，整条更新
        就永远走不通（真实模型「改用方案B」三次都被拒就是这个原因）。
        ``_decision_wording_evidenced`` / ``_switch_wording_evidenced`` 与候选侧
        是同一对函数，同样要求证据落在**这条建议说的那件事**上。
        """
        if (suggestion.kind, suggestion.content_status) not in USER_ADOPTION_STATUSES:
            return suggestion
        if adoption_evidenced(
            packet,
            suggestion.content_status,
            source_refs=suggestion.source_refs,
            item_text=suggestion.content or cls._target_excerpt(packet, suggestion.target_id),
        ):
            return suggestion
        if cls._switch_wording_evidenced(packet, suggestion) or cls._decision_wording_evidenced(
            packet, suggestion
        ):
            return suggestion
        return replace(suggestion, content_status="proposed")

    @staticmethod
    def _suggestion_from_item(item: dict, *, packet: Any, action: str) -> MemoryUpdateSuggestion:
        def _strings(name: str) -> tuple[str, ...]:
            value = item.get(name)
            if isinstance(value, str):
                return (value.strip(),) if value.strip() else ()
            if isinstance(value, list):
                return tuple(str(v).strip() for v in value if str(v).strip())
            return ()

        raw_version = item.get("expected_version")
        try:
            expected_version = int(raw_version) if raw_version is not None else None
        except (TypeError, ValueError):
            expected_version = None
        worth = item.get("value")
        return MemoryUpdateSuggestion(
            action=action,
            content=str(item.get("content") or "").strip()[:MAX_CONTENT_LENGTH],
            kind=str(item.get("kind") or "").strip(),
            content_status=str(item.get("content_status") or "").strip(),
            source_types=_strings("source_types"),
            source_refs=tuple(
                dict.fromkeys([*(str(r) for r in _strings("source_refs")), packet.turn_id])
            ),
            target_id=str(item.get("target_id") or item.get("memory_id") or "").strip(),
            expected_version=expected_version,
            conflict_ids=_strings("conflict_ids") or _strings("conflicts_with"),
            reason=str(item.get("reason") or "").strip(),
            horizon=str(item.get("horizon") or "").strip(),
            topics=_strings("topics"),
            value=True if worth is None else bool(worth),
            confidence=SemanticMemoryExtractor._optional_float(item.get("confidence")),
            importance=SemanticMemoryExtractor._optional_float(item.get("importance")),
        )

    @staticmethod
    def _candidate_horizon(item: dict) -> str:
        """条目声明的 horizon；没声明返回空串，声明了非法值抛错拒收。

        取值只认 long_term / short_term（与 memory_turn.CONTENT_HORIZONS 同）。
        刻意不「非法就当没写」：那会让一个写错的值静默变成长期留存，
        与「短期语义要真的被到期政策处理」正好相反。
        """
        raw = str(item.get("horizon") or "").strip()
        if raw and raw not in CANDIDATE_HORIZONS:
            raise MemoryCandidateError(f"未知时间尺度 horizon={raw!r}")
        return raw

    @staticmethod
    def _candidate_from_item(item: dict, packet: Any) -> MemoryCandidate:
        def _strings(name: str) -> tuple[str, ...]:
            value = item.get(name)
            if isinstance(value, str):
                return (value.strip(),) if value.strip() else ()
            if isinstance(value, list):
                return tuple(str(v).strip() for v in value if str(v).strip())
            return ()

        return HorizonCandidate(
            content=str(item.get("content") or "").strip()[:MAX_CONTENT_LENGTH],
            kind=str(item.get("kind") or "").strip(),
            content_status=str(item.get("content_status") or "").strip(),
            source_types=_strings("source_types"),
            scope_kind=str(item.get("scope_kind") or packet.scope_kind or "conversation"),
            occurred_at=packet.occurred_at,
            reason=str(item.get("reason") or "").strip(),
            goal=str(item.get("goal") or "").strip(),
            project_id=str(item.get("project_id") or packet.project_binding or "").strip(),
            topics=_strings("topics"),
            related_ids=_strings("related_ids"),
            supersedes=_strings("supersedes"),
            source_refs=tuple(
                dict.fromkeys([*(str(r) for r in _strings("source_refs")), packet.turn_id])
            ),
            evidence_level=str(item.get("evidence_level") or "").strip(),
            confidence=SemanticMemoryExtractor._optional_float(item.get("confidence")),
            importance=SemanticMemoryExtractor._optional_float(item.get("importance")),
            origin="model_candidate",
            horizon=SemanticMemoryExtractor._candidate_horizon(item),
        )

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            return min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            return None


def semantic_max_token() -> int:
    """新版候选提炼的输出预算；``MEMORY_EXTRACT_MAX_TOKEN`` 可覆盖。

    单独一个环境变量、单独一个默认值，不与旧的 EXTRACT_MAX_TOKEN 混用：
    两条链路的输入结构完全不同，共用一个数字只会让改动互相牵连。
    """
    raw = os.environ.get("MEMORY_EXTRACT_MAX_TOKEN", "")
    try:
        value = int(str(raw).strip() or SEMANTIC_MAX_TOKEN)
    except (TypeError, ValueError):
        return SEMANTIC_MAX_TOKEN
    return value if value > 0 else SEMANTIC_MAX_TOKEN


def build_semantic_extractor_from_env(
    env_path: str | Path = PROJECT_ENV_PATH,
) -> SemanticMemoryExtractor | None:
    """按**主回答模型**的配置构建候选提炼器；配置缺失时返回 None。

    刻意不退回旧的陪伴提炼模型：静默换模型会让「同一个包两端结果不同」这类
    问题无迹可循。缺配置就是缺配置，由调用方记账并显示积压。
    """
    load_dotenv(dotenv_path=Path(env_path), override=False)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = os.environ.get("GOOGLE_GEMINI_BASE_URL", "").strip()
    model = os.environ.get("GEMINI_MODEL", "").strip()
    if not base_url or not model:
        return None
    from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

    max_token = semantic_max_token()
    return SemanticMemoryExtractor(
        OpenAICompatibleProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_token=max_token,
            timeout=DEFAULT_TIMEOUT,
            thinking_enabled=True,
        ),
        max_token=max_token,
    )
