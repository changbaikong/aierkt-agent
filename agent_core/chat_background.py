"""本轮可复用的「关键背景」对象（实施工作 A）。

它只做三件搬运类的事，不做判断、不改记忆、不调模型：

1. 把一轮里**已经存在**的背景收进一个结构（当前用户原话、已召回的记忆及其引用、
   相关内容状态、待澄清事项、必要原话片段、背景缺口）；
2. 保证同一个背景在「主回答 → 核查 → 重写 → 预算收尾」之间是**同一份**，
   而不是各段落各自去 system 提示里重切一次；
3. 输入放不下时，按完整对话组从最近往前删历史，但关键背景与当前请求始终保留。

设计边界（写清楚，避免被当成新能力宣传）：

- 它不是长期记忆库，也不做语义摘要。旧历史不生成摘要，只有**已经召回并带引用**
  的决定原文会被留在关键背景里。
- 不扩大检索候选、不重排、不承诺恢复从未召回或入口已丢失的信息。
- 背景本身放不下时，明确交出缺口（``gaps``），不静默删掉约束后假装回答完整。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

#: 背景对象的结构版本。新增字段时提升它，便于旧记录识别。
BACKGROUND_VERSION = 1

#: 背景对象在对话消息里的标记，用于同一轮内去重（不重复注入同一份）。
BACKGROUND_MARKER = "【本轮关键背景】"

#: 单条原话片段最多留多少字。片段是「原话」，不是摘要，所以只做长度边界。
QUOTE_LIMIT = 400

#: 背景整体在提示里的字数上限。超出后按重要性从低到高丢：先丢缺口说明之外
#: 的软字段，**绝不丢「已召回决定原文」与「当前用户原话」**。
DEFAULT_BACKGROUND_LIMIT = 4000


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        return str(value.get("content") or value.get("text") or "").strip()
    return str(value).strip()


def _safe_rows(value: Any) -> tuple[Any, ...]:
    """只接受"一串字典"；其他形状（字符串、数字、字典）一律当空。

    坏数据的方向必须是"少一份背景"，不是"整轮回答报错"。
    """
    if isinstance(value, Mapping):
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _safe_strs(value: Any) -> tuple[str, ...]:
    """只接受"一串字符串"；单个字符串按一条处理，其余当空。"""
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if isinstance(item, str) and item.strip())


@dataclass(frozen=True)
class BackgroundMemory:
    """已经在召回里选中、并带引用的记忆。正文保留原话，不做改写。"""

    memory_id: str
    content: str
    kind: str = ""
    content_status: str = ""
    version: int = 1
    layer: str = ""
    semantic_judgment: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "content": self.content,
            "kind": self.kind,
            "content_status": self.content_status,
            "version": self.version,
            "layer": self.layer,
            "semantic_judgment": dict(self.semantic_judgment),
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "BackgroundMemory":
        return cls(
            memory_id=str(row.get("memory_id") or row.get("id") or ""),
            content=_text(row.get("content") or row.get("excerpt")),
            kind=str(row.get("kind") or ""),
            content_status=str(row.get("content_status") or ""),
            version=int(row.get("version") or row.get("record_version") or 1),
            layer=str(row.get("layer") or ""),
            semantic_judgment=dict(row.get("semantic_judgment") or {}),
        )


@dataclass(frozen=True)
class BackgroundQuote:
    """一条必要原话：谁说的、说的是什么。

    ``locator`` 只用来**本轮定位**。有现成的消息编号就用；没有就留一个本轮
    内部编号（``turn-local:...``），绝不编造数据库编号。
    """

    role: str
    text: str
    locator: str = ""
    source_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "locator": self.locator,
            "source_id": self.source_id,
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "BackgroundQuote":
        return cls(
            role=str(row.get("role") or ""),
            text=_text(row.get("text") or row.get("content")),
            locator=str(row.get("locator") or ""),
            source_id=str(row.get("source_id") or ""),
        )


@dataclass(frozen=True)
class PendingClarification:
    """待澄清事项的展示形态（由服务端授权后给出，背景只负责携带）。"""

    candidate_id: str
    job_id: str = ""
    item_path: str = ""
    quote: str = ""
    reason: str = ""
    suggested_question: str = ""
    target_id: str = ""
    target_version: int = 0
    deadline: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "job_id": self.job_id,
            "item_path": self.item_path,
            "quote": self.quote,
            "reason": self.reason,
            "suggested_question": self.suggested_question,
            "target_id": self.target_id,
            "target_version": self.target_version,
            "deadline": self.deadline,
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "PendingClarification":
        return cls(
            candidate_id=str(row.get("candidate_id") or ""),
            job_id=str(row.get("job_id") or ""),
            item_path=str(row.get("item_path") or ""),
            quote=_text(row.get("quote")),
            reason=str(row.get("reason") or ""),
            suggested_question=str(row.get("suggested_question") or ""),
            target_id=str(row.get("target_id") or ""),
            target_version=int(row.get("target_version") or 0),
            deadline=str(row.get("deadline") or ""),
        )


@dataclass(frozen=True)
class TurnBackground:
    """本轮的关键背景。默认空对象 —— 旧调用不带它时行为完全不变。"""

    user_text: str = ""
    selected: tuple[BackgroundMemory, ...] = ()
    quotes: tuple[BackgroundQuote, ...] = ()
    pending: tuple[PendingClarification, ...] = ()
    gaps: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    version: int = BACKGROUND_VERSION

    # ------------------------------------------------------------------
    # 构造
    # ------------------------------------------------------------------
    @classmethod
    def empty(cls) -> "TurnBackground":
        return cls()

    @classmethod
    def build(
        cls,
        *,
        user_text: str = "",
        selected: Iterable[Any] = (),
        quotes: Iterable[Any] = (),
        pending: Iterable[Any] = (),
        gaps: Iterable[str] = (),
        notes: Iterable[str] = (),
    ) -> "TurnBackground":
        memories = tuple(
            item if isinstance(item, BackgroundMemory) else BackgroundMemory.from_mapping(item)
            for item in selected
            if item is not None
        )
        quote_rows = tuple(
            item if isinstance(item, BackgroundQuote) else BackgroundQuote.from_mapping(item)
            for item in quotes
            if item is not None
        )
        pending_rows = tuple(
            item if isinstance(item, PendingClarification) else PendingClarification.from_mapping(item)
            for item in pending
            if item is not None
        )
        return cls(
            user_text=_text(user_text),
            selected=memories,
            quotes=quote_rows,
            pending=pending_rows,
            gaps=tuple(str(gap) for gap in gaps if str(gap or "").strip()),
            notes=tuple(str(note) for note in notes if str(note or "").strip()),
            version=BACKGROUND_VERSION,
        )

    # ------------------------------------------------------------------
    # 序列化：回合请求末尾用的就是这份 payload
    # ------------------------------------------------------------------
    def as_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "user_text": self.user_text,
            "selected": [item.as_dict() for item in self.selected],
            "quotes": [item.as_dict() for item in self.quotes],
            "pending": [item.as_dict() for item in self.pending],
            "gaps": list(self.gaps),
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "TurnBackground":
        """宽容解析：旧包、缺字段、非字典一律当作「没有背景」，不抛异常。

        这里**不能抛**：它读的是可能来自旧任务、旧存档或手写调试的数据。
        抛出去会连带把整轮回答打掉，而正确行为是"当成没有背景，照常回答"。
        所以每个列表字段都先确认是可迭代的序列，坏数据直接忽略。
        """
        if payload is None:
            return cls.empty()
        if isinstance(payload, TurnBackground):
            return payload
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                return cls.empty()
        if not isinstance(payload, Mapping):
            return cls.empty()
        return cls.build(
            user_text=_text(payload.get("user_text")),
            selected=_safe_rows(payload.get("selected")),
            quotes=_safe_rows(payload.get("quotes")),
            pending=_safe_rows(payload.get("pending")),
            gaps=_safe_strs(payload.get("gaps")),
            notes=_safe_strs(payload.get("notes")),
        )

    # ------------------------------------------------------------------
    # 可读性辅助
    # ------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        return not (self.user_text or self.selected or self.quotes or self.pending or self.gaps or self.notes)

    @property
    def current_decisions(self) -> tuple[BackgroundMemory, ...]:
        """现行决定（不含已被替代/被否决的）。供收尾判断「有没有现行约束」。"""
        return tuple(
            item for item in self.selected
            if item.content_status not in ("superseded", "rejected")
        )

    def dedupe_key(self) -> tuple[str, ...]:
        """同一轮的背景身份：用来保证同一记忆不被注入第二份。"""
        return tuple(sorted(
            [f"m:{item.memory_id}" for item in self.selected if item.memory_id]
            + [f"q:{item.role}:{item.text}" for item in self.quotes if item.text]
            + [f"c:{item.candidate_id}" for item in self.pending if item.candidate_id]
        ))

    def render(self, *, limit: int = DEFAULT_BACKGROUND_LIMIT) -> str:
        """渲染成给模型看的文本。关键项（当前原话、召回决定正文）永远排在最前。"""
        lines: list[str] = []
        if self.user_text:
            lines.append(f"当前用户原话：{self.user_text}")
        for item in self.selected:
            status = item.content_status or "未知状态"
            judgment = item.semantic_judgment or {}
            certainty = str(judgment.get("certainty") or "")
            head = f"- [{item.memory_id or '本轮记录'}]"
            if item.kind:
                head += f"（{item.kind}）"
            head += f" {item.content}"
            meta: list[str] = [f"状态={status}"]
            if certainty:
                meta.append(f"判断={certainty}")
            if item.version:
                meta.append(f"版本={item.version}")
            lines.append(f"{head}｜{'，'.join(meta)}")
        for item in self.pending:
            described = f"- 待澄清[{item.candidate_id}]：{item.quote or item.reason or '未提供原话'}"
            if item.reason and item.quote:
                described += f"（{item.reason}）"
            if item.suggested_question:
                described += f"｜现有建议问题：{item.suggested_question}"
            if item.target_version:
                described += f"｜目标版本={item.target_version}"
            lines.append(described)
        for item in self.quotes:
            who = {"user": "用户原话", "assistant": "助手原话"}.get(item.role, item.role or "原话")
            location = f"[{item.locator}]" if item.locator else ""
            lines.append(f"{who}{location}：{item.text[:QUOTE_LIMIT]}")
        for note in self.notes:
            lines.append(f"说明：{note}")
        if self.gaps:
            lines.append("背景缺口：" + "、".join(self.gaps) + "未纳入本轮（按预算省略）")
        body = "\n".join(lines)
        if len(body) <= limit:
            return body
        # 超限：只在**够不关键**的尾部截断，并如实标记；关键项已在最前。
        return body[:limit] + f"\n（背景超出 {limit} 字上限，后段已省略；关键决定与当前请求已保留）"

    def render_message(self) -> str:
        """包成一条 system 消息体（只在有内容时使用）。"""
        body = self.render()
        if not body:
            return ""
        return f"{BACKGROUND_MARKER}\n{body}"


def background_section(notes: Iterable[str] = ()) -> tuple[str, ...]:
    """固定说明：告诉模型背景的作用与边界，避免它把背景当新决定来源。"""
    base = (
        "本轮随请求附上「关键背景」：它包含当前用户原话、已经召回并带引用的相关记忆、"
        "相关内容状态、待澄清事项与背景缺口。",
        "背景是已经收到的事实搬运，不是新的决定来源：不得据此声称动作已经执行、正在执行或已经停止。",
    )
    return tuple(base) + tuple(str(note) for note in notes if str(note or "").strip())


def trim_messages(
    messages: Sequence[Mapping[str, Any]],
    *,
    budget_tokens: int,
    estimate: Any,
    protected: Sequence[Mapping[str, Any]] = (),
    min_keep: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按预算删减这次发给模型的**副本**，返回 ``(消息, 缺口说明)``。

    规则（对应计划的历史与预算处理）：

    - 放得下就原样返回，什么都不动；
    - 放不下时，**角色与权限（system）消息永远保留**，``protected`` 也一律保留，
      再从**最近的完整对话组**向前填历史；
    - 工具调用与其结果必须配对：宁可整组一起删，也不制造孤立工具消息；
    - 缺口如实报数（删了几组就报几组），不把没保住的系统提示悄悄吞掉；
    - 删减只作用于返回值，不碰传入的列表、公开历史或已保存记忆。
    """
    rows = [dict(message) for message in messages]
    protected_rows = [dict(message) for message in protected]
    estimate_fn = estimate if callable(estimate) else (lambda _rows: 0)

    def total(rows_in: Sequence[Mapping[str, Any]]) -> int:
        try:
            return int(estimate_fn(list(rows_in)) or 0)
        except Exception:  # noqa: BLE001 - 估算失败按"放得下"处理，不误删
            return 0

    if total(rows) <= budget_tokens:
        return rows, []

    # 角色与权限消息天然不可删：它承载人格、权限与工具说明，删掉等于换了一个助手。
    # 调用方额外指定的 protected 同样一律保留。
    #
    # 判定用**位置下标**而不是对象 id：分组时每条消息都会被复制成新对象，
    # 拿 id 比对永远匹配不上（这正是"系统提示被静默删掉"的原因）。
    protected_indexes = {
        index for index, message in enumerate(rows)
        if message.get("role") == "system" or _same_content(message, protected_rows)
    }

    groups = _message_groups(rows)
    kept_groups = [
        group for group in groups if all(index in protected_indexes for index in group)
    ]
    kept_indexes: set[int] = {index for group in kept_groups for index in group}
    kept_first = {group[0] for group in kept_groups if group}

    omitted: list[str] = []
    for group in reversed(groups):
        if group[0] in kept_first:
            continue
        candidate_indexes = kept_indexes | set(group)
        if (
            _history_pairs([rows[index] for index in kept_indexes]) >= min_keep
            and total([rows[index] for index in sorted(candidate_indexes)]) > budget_tokens
        ):
            omitted.append(_describe_group([rows[index] for index in group]))
            continue
        kept_indexes |= set(group)

    # 按原始顺序返回：保留的是"哪些消息"，不是"排成什么顺序"。
    # 顺序被重排（比如 system 挪到最后）会改变提示语义，那就不叫删减了。
    chosen = [rows[index] for index in sorted(kept_indexes)]

    gaps: list[str] = []
    if omitted:
        gaps.append(f"较早的 {len(omitted)} 组对话")
    return chosen, gaps


def _same_content(message: Mapping[str, Any], protected: Sequence[Mapping[str, Any]]) -> bool:
    """这条消息是否就是调用方指定的保护项（按角色＋内容比对，不比对象身份）。"""
    for item in protected:
        if message.get("role") == item.get("role") and message.get("content") == item.get("content"):
            return True
    return False


def _history_pairs(rows: Sequence[Mapping[str, Any]]) -> int:
    """已保留的对话组数（按 user 消息数算），用来判断是否够 ``min_keep``。"""
    return sum(1 for row in rows if row.get("role") == "user")


def _describe_group(group: Sequence[Mapping[str, Any]]) -> str:
    """给一组被删减的消息起个可读名（缺口说明用）。"""
    for message in group:
        if message.get("role") == "user":
            return _text(message.get("content"))[:40] or "一组对话"
    return "一组对话"


def _message_groups(messages: Sequence[Mapping[str, Any]]) -> list[list[int]]:
    """把消息切成完整对话**组的下标**。

    一组 = 一条 user 消息，加上它后面直到下一条 user 消息之前的全部内容
    （assistant 正文、工具调用、工具结果）。这样删减永远不会把工具调用和
    它的回执拆开。

    返回下标而不是消息对象：调用方要按下标回查原列表，才能在保留判定上
    与"哪一条"对齐（复制过的对象 id 比对不可靠，见 ``trim_messages``）。
    """
    groups: list[list[int]] = []
    current: list[int] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "system":
            # 系统消息自成一组：它属于"角色与权限"，永远优先保留。
            if current:
                groups.append(current)
                current = []
            groups.append([index])
            continue
        if role == "user" and current:
            groups.append(current)
            current = [index]
            continue
        current.append(index)
    if current:
        groups.append(current)
    return groups
