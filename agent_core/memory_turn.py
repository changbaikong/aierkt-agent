"""统一记忆候选与回合处理包的契约（B 计划第一步）。

【这个模块负责什么】
只负责「格式」和「校验」，不做任何写入、检索或模型调用：

1. 七类内容 ``kind`` 各自允许哪些 ``content_status``；
2. 一条候选记录必须带哪些字段（正文、类型、内容状态、来源类别、来源引用、
   发生时间、所属范围、语义版本）；
3. 哪些状态**只能由程序**给出（``verified`` / ``completed``），模型自己说
   「用户已经采纳」或「任务已经完成」一律拒收；
4. 回合处理包的字段与预算裁剪。

【为什么把「信任」和「进度」拆开】
旧模型里 ``confidence >= 0.85`` 就等于 ``status='confirmed'``，于是
「模型对这句话的理解很有把握」被当成了「这件事已经确认成立」。
本模块把四件事分成四个字段：

- ``confidence``：模型对句意理解的把握度（保留，只管排序用）
- ``source_types``：来源可信程度（用户明说 / 工具结果 / 助手建议 / 模型推断）
- ``content_status``：内容当前进展（建议 / 已采纳 / 已完成）
- 数据库 ``status``：记录本身是否还有效（active / archived / expired）

一个 0.95 的把握度，不能证明任何事情已经完成。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

#: 处理包结构版本。传输、存储、幂等键都记它，判断新旧格式靠它。
SCHEMA_VERSION = 1
#: 处理包字段版本。新增可选字段时 +1，旧包仍可读（缺字段按空默认值处理）。
PACKET_VERSION = 2
#: 提炼规则版本。规则一改就 +1，避免用新规则解释旧候选。
EXTRACTION_PROFILE_VERSION = 1
#: 记录语义版本。0 = 旧陪伴记忆（confidence/confirmed 那套），>=1 = 新语义记录。
SEMANTICS_VERSION = 1

#: 七类内容。旧记录的 kind 是 message/candidate/fact，靠 semantics_version 区分。
CONTENT_KINDS: tuple[str, ...] = (
    "fact",
    "decision",
    "plan",
    "preference",
    "experience",
    "open_question",
    "hypothesis",
)

#: 每类内容允许的进展状态。取用范围就是这里，别处不得自行放宽。
CONTENT_STATUSES: dict[str, tuple[str, ...]] = {
    "fact": ("reported", "verified", "disputed"),
    "decision": ("proposed", "accepted", "rejected", "superseded"),
    "plan": ("proposed", "accepted", "in_progress", "completed", "cancelled"),
    "preference": ("explicit", "tentative", "superseded"),
    "experience": ("observed", "tentative", "disputed"),
    "open_question": ("open", "resolved", "cancelled"),
    "hypothesis": ("untested", "supported", "refuted"),
}

#: 来源类别。同一条可以同时带上几种，逐项保留，不合并成「已确认」。
SOURCE_TYPES: tuple[str, ...] = (
    "user_statement",
    "tool_result",
    "assistant_suggestion",
    "model_inference",
)

#: 可见范围。conversation = 同会话，project = 同账号同项目，owner = 同账号。
SCOPE_KINDS: tuple[str, ...] = ("conversation", "project", "owner")

#: 记录生命周期（数据库 status 列）。与 content_status 正交。
LIFECYCLE_STATUSES: tuple[str, ...] = ("active", "archived", "expired")

#: 候选来源。程序构造的（工具完成事件）和模型产出的走不同信任路径。
CANDIDATE_ORIGINS: tuple[str, ...] = ("model_candidate", "program")

#: 只有程序能给出的进展状态 —— 模型自称一律拒绝。
PROGRAM_ONLY_STATUSES: frozenset[tuple[str, str]] = frozenset(
    {("fact", "verified"), ("plan", "completed")}
)

#: 需要用户明确表达采纳才能给的状态。
USER_ADOPTION_STATUSES: frozenset[tuple[str, str]] = frozenset(
    {("decision", "accepted"), ("plan", "accepted"), ("decision", "rejected")}
)

#: 处理包默认上限（模型计量单位）。
PACKET_UNIT_LIMIT = 8000
PACKET_SECTION_LIMITS: dict[str, tuple[int, int]] = {
    # 段落名 -> (最多条数, 最多单位)
    "relevant_history": (12, 2500),
    "recalled_memory_refs": (10, 1500),
    "evidence_and_tool_outcomes": (12, 1500),
}

#: 只在「解释历史」时出现的状态：已替代方案不能当成现行结论用。
SUPERSEDED_STATUSES: frozenset[str] = frozenset({"superseded", "rejected", "cancelled"})

_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")


class MemoryCandidateError(ValueError):
    """候选/处理包不合规。消息里带字段名与原因，便于测试断言。"""


def estimate_units(text: Any) -> int:
    """估算一段文本的模型计量单位。

    中文按**每字一个单位**算 —— 旧代码用 ``字数/4``，在中文上低估约四倍，
    结果「预算还剩很多」而模型实际已经吃满。缺计量接口时按这条保守值走。
    """
    raw = "" if text is None else str(text)
    cjk = len(_CJK.findall(raw))
    other = len(raw) - cjk
    return cjk + (other + 3) // 4


def item_units(item: Any) -> int:
    """处理包里一条背景材料的计量单位。

    三类材料记正文字段的字段名不同：历史消息是 ``content``，证据是 ``summary``，
    已用记忆是 ``excerpt``。以前这里只认前两个，遇到已用记忆就抛属性错误 ——
    于是「带已有记忆引用的包」一裁预算就崩，实际路径干脆不裁预算。
    """
    for name in ("content", "summary", "excerpt"):
        value = getattr(item, name, None)
        if isinstance(value, str):
            return estimate_units(value)
    return 0


def is_blank(value: Any) -> bool:
    """空串、纯空白、None 都算空。注意 ``0`` 和 ``False`` 不算空。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def normalize_text(value: str) -> str:
    """归一化正文：压掉空白、统一小写。只用于算稳定键，不改写正文本身。"""
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def semantic_key(kind: str, content: str, topics: Iterable[str] = ()) -> str:
    """一条候选的**稳定语义键**。

    刻意不含「模型输出的第几条」——换一次输出顺序不该重复造记录。
    键 = 类型 + 稳定主题 + 正文摘要。
    """
    topics_part = ",".join(sorted({normalize_text(t) for t in topics if normalize_text(t)}))
    digest = hashlib.sha1(normalize_text(content).encode("utf-8")).hexdigest()[:16]
    return f"{kind}|{topics_part}|{digest}"


def candidate_idempotency_key(
    *,
    scope_kind: str,
    scope_id: str,
    turn_or_event_id: str,
    source_locator: str,
    stable_key: str,
    profile_version: int = EXTRACTION_PROFILE_VERSION,
) -> str:
    """幂等键：账号/范围 + 回合或事件号 + 来源定位 + 稳定语义键 + 规则版本。

    重试时同样的输入算出同样的键，写入端靠 UNIQUE 约束挡住重复。
    """
    for name, value in (
        ("scope_kind", scope_kind),
        ("scope_id", scope_id),
        ("turn_or_event_id", turn_or_event_id),
        ("stable_key", stable_key),
    ):
        if is_blank(value):
            raise MemoryCandidateError(f"{name} 不能为空")
    return "|".join(
        [
            str(scope_kind),
            str(scope_id),
            str(turn_or_event_id),
            str(source_locator or "-"),
            str(stable_key),
            f"v{int(profile_version)}",
        ]
    )


@dataclass(frozen=True)
class MemoryCandidate:
    """一条待保存的记忆候选。

    ``origin`` 区分它是程序构造的还是模型产出的：模型产出的不允许自称
    「已验证」「已完成」「用户已采纳」。
    """

    content: str
    kind: str
    content_status: str
    source_types: tuple[str, ...]
    scope_kind: str = "conversation"
    occurred_at: str = ""
    reason: str = ""
    goal: str = ""
    project_id: str = ""
    project_label: str = ""
    topics: tuple[str, ...] = ()
    related_ids: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    evidence_level: str = ""
    confidence: float | None = None
    importance: float | None = None
    origin: str = "model_candidate"
    stable_key: str = ""
    profile_version: int = EXTRACTION_PROFILE_VERSION
    #: 这条记忆的时间尺度：``long_term``（长期事实/偏好/约束）或 ``short_term``
    #: （当下情绪/当前任务）。它决定写入侧的寿命政策 —— 只能按**声明**给，
    #: 不从正文用词倒推：长期原则里出现「今天的事情今天做完」很正常。
    horizon: str = ""
    # Internal service-issued proof. The model parser never accepts this field.
    review_receipt: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def resolved_stable_key(self) -> str:
        return self.stable_key or semantic_key(self.kind, self.content, self.topics)

    def referenced_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([*self.related_ids, *self.supersedes]))

    def as_metadata(self) -> dict[str, Any]:
        """落库时写进 ``metadata`` 的结构化语义（不含正文）。"""
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "semantics_version": SEMANTICS_VERSION,
            "profile_version": int(self.profile_version),
            "source_types": list(self.source_types),
            "source_refs": list(self.source_refs),
            "stable_key": self.resolved_stable_key(),
        }
        for name, value in (
            ("reason", self.reason),
            ("goal", self.goal),
            ("evidence_level", self.evidence_level),
            # 模型给出的项目名只当标签留痕：它不足以授项目级读取权限。
            ("project_label", self.project_label),
        ):
            if not is_blank(value):
                payload[name] = value
        if self.horizon:
            payload["horizon"] = self.horizon
        if self.topics:
            payload["topics"] = list(self.topics)
        if self.related_ids:
            payload["related_ids"] = list(self.related_ids)
        if self.supersedes:
            payload["supersedes"] = list(self.supersedes)
        return payload


def validate_candidate(candidate: MemoryCandidate, *, known_ids: Iterable[str] | None = None) -> None:
    """校验一条候选；不合规抛 :class:`MemoryCandidateError`。

    ``known_ids`` 传「输入里已经存在且有权限」的记忆 ID 集合时，关系引用必须
    落在其中 —— 关系不许指向凭空想出来的 ID。
    """
    if is_blank(candidate.content):
        raise MemoryCandidateError("content 不能为空")
    if candidate.kind not in CONTENT_KINDS:
        raise MemoryCandidateError(f"未知内容类型 kind={candidate.kind!r}")
    allowed = CONTENT_STATUSES[candidate.kind]
    if candidate.content_status not in allowed:
        raise MemoryCandidateError(
            f"{candidate.kind} 不允许内容状态 {candidate.content_status!r}，可选：{','.join(allowed)}"
        )
    if candidate.scope_kind not in SCOPE_KINDS:
        raise MemoryCandidateError(f"未知范围 scope_kind={candidate.scope_kind!r}")
    if candidate.origin not in CANDIDATE_ORIGINS:
        raise MemoryCandidateError(f"未知候选来源 origin={candidate.origin!r}")
    if not candidate.source_types:
        raise MemoryCandidateError("必须至少标一种来源类别 source_types")
    unknown_sources = [s for s in candidate.source_types if s not in SOURCE_TYPES]
    if unknown_sources:
        raise MemoryCandidateError(f"未知来源类别：{','.join(unknown_sources)}")
    pair = (candidate.kind, candidate.content_status)
    # 模型把握度证明不了事情已完成：这类状态只能由程序带着证据给出。
    if pair in PROGRAM_ONLY_STATUSES and candidate.origin != "program":
        raise MemoryCandidateError(
            f"{candidate.kind}/{candidate.content_status} 只能由程序依据证据给出，模型候选不得自称"
        )
    if pair in PROGRAM_ONLY_STATUSES and "tool_result" not in candidate.source_types:
        raise MemoryCandidateError(
            f"{candidate.kind}/{candidate.content_status} 需要 tool_result 来源"
        )
    # 建议可以存 proposed，采纳不行 —— 采纳要有用户明确陈述。
    if pair in USER_ADOPTION_STATUSES and "user_statement" not in candidate.source_types:
        raise MemoryCandidateError(
            f"{candidate.kind}/{candidate.content_status} 需要 user_statement 来源（用户明确表达）"
        )
    if candidate.confidence is not None:
        value = float(candidate.confidence)
        if not 0.0 <= value <= 1.0:
            raise MemoryCandidateError("confidence 必须在 0.0~1.0 之间")
    if candidate.scope_kind == "project" and is_blank(candidate.project_id):
        raise MemoryCandidateError("project 范围必须带 project_id")
    if candidate.scope_kind == "owner" and candidate.origin != "program" and pair not in {
        ("preference", "explicit"),
    }:
        # 账号级只放用户明确说过的稳定偏好；网页材料和模型推断不能自己升上来。
        raise MemoryCandidateError("owner 范围只允许用户明确陈述的稳定偏好")
    if known_ids is not None:
        known = set(known_ids)
        unknown_refs = [rid for rid in candidate.referenced_ids() if rid not in known]
        if unknown_refs:
            raise MemoryCandidateError(f"关系引用了输入中不存在的记忆 ID：{','.join(unknown_refs)}")


@dataclass(frozen=True)
class HistoryItem:
    message_id: str
    role: str
    content: str

    @property
    def units(self) -> int:
        return estimate_units(self.content)


@dataclass(frozen=True)
class MemoryRef:
    """已经用过的记忆：记 ID、版本、类型、状态和相关片段。"""

    memory_id: str
    kind: str
    content_status: str
    excerpt: str = ""
    version: int = SEMANTICS_VERSION
    layer: str = ""
    semantic_judgment: dict[str, Any] = field(default_factory=dict)

    @property
    def units(self) -> int:
        return estimate_units(self.excerpt) + estimate_units(json.dumps(self.semantic_judgment,ensure_ascii=False)) if self.semantic_judgment else estimate_units(self.excerpt)


@dataclass(frozen=True)
class EvidenceRef:
    """证据或工具终局状态。``outcome`` 取 success / failed / progress / unknown。"""

    ref_id: str
    summary: str
    outcome: str = "unknown"
    evidence_level: str = ""

    @property
    def units(self) -> int:
        return estimate_units(self.summary)


@dataclass
class MemoryTurnPacket:
    """一个已发布回合的处理包：入口、同步、widget 三处共用同一份。

    刻意**不**含全量历史、全库记忆和密钥：提炼是另一次后台调用，
    它只看到这里显式放进去的东西。
    """

    turn_id: str
    occurred_at: str
    scope_owner: str
    scope_channel: str
    scope_conversation: str
    scope_module: str = "companion"
    scope_kind: str = "conversation"
    project_binding: str = ""
    user_text: str = ""
    published_reply: str = ""
    outcome_status: str = "published"
    task_goal: str = ""
    relevant_history: list[HistoryItem] = field(default_factory=list)
    recalled_memory_refs: list[MemoryRef] = field(default_factory=list)
    evidence_refs: list[EvidenceRef] = field(default_factory=list)
    completeness: str = "complete"
    omitted_sections: tuple[str, ...] = ()
    extraction_profile_version: int = EXTRACTION_PROFILE_VERSION
    schema_version: int = SCHEMA_VERSION
    # 实施工作 B：可选的澄清关联（缺省空）。服务端填写的"可能关联"，不是可信确认。
    clarification_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.outcome_status not in ("published", "failed", "cancelled"):
            raise MemoryCandidateError(f"未知回合结果 outcome_status={self.outcome_status!r}")
        if self.scope_kind not in SCOPE_KINDS:
            raise MemoryCandidateError(f"未知范围 scope_kind={self.scope_kind!r}")
        if self.scope_kind == "project" and is_blank(self.project_binding):
            raise MemoryCandidateError("project 范围的回合必须带 project_binding")

    def is_published(self) -> bool:
        """只有正文真的发出去过，才谈得上「助手结论」。"""
        return self.outcome_status == "published" and not is_blank(self.published_reply)

    def sections(self) -> dict[str, list[Any]]:
        return {
            "relevant_history": list(self.relevant_history),
            "recalled_memory_refs": list(self.recalled_memory_refs),
            "evidence_and_tool_outcomes": list(self.evidence_refs),
        }

    def total_units(self) -> int:
        base = estimate_units(self.user_text) + estimate_units(self.published_reply)
        base += estimate_units(self.task_goal)
        return base + sum(
            item_units(item)
            for group in (self.relevant_history, self.recalled_memory_refs, self.evidence_refs)
            for item in group
        )

    def as_payload(self) -> dict[str, Any]:
        """序列化成可持久化、可跨端传输的字典（不含密钥）。"""
        return {
            "schema_version": self.schema_version,
            "extraction_profile_version": self.extraction_profile_version,
            "turn_id": self.turn_id,
            "occurred_at": self.occurred_at,
            "scope": {
                "owner_id": self.scope_owner,
                "channel": self.scope_channel,
                "conversation_id": self.scope_conversation,
                "module_id": self.scope_module,
                "kind": self.scope_kind,
                "project_id": self.project_binding,
            },
            "user_text": self.user_text,
            "published_reply": self.published_reply,
            "outcome_status": self.outcome_status,
            "task_goal": self.task_goal,
            "relevant_history": [
                {"message_id": item.message_id, "role": item.role, "content": item.content}
                for item in self.relevant_history
            ],
            "recalled_memory_refs": [
                {
                    "memory_id": item.memory_id,
                    "kind": item.kind,
                    "content_status": item.content_status,
                    "excerpt": item.excerpt,
                    "version": item.version,
                    "layer": item.layer,
                    "semantic_judgment": item.semantic_judgment,
                }
                for item in self.recalled_memory_refs
            ],
            "evidence_refs": [
                {
                    "ref_id": item.ref_id,
                    "summary": item.summary,
                    "outcome": item.outcome,
                    "evidence_level": item.evidence_level,
                }
                for item in self.evidence_refs
            ],
            "completeness": self.completeness,
            "omitted_sections": list(self.omitted_sections),
            "packet_version": PACKET_VERSION,
            # 可选字段：默认空字典，旧读法忽略它即可（向后兼容）。
            "clarification_context": dict(self.clarification_context or {}),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "MemoryTurnPacket":
        """把传输/存储里的字典还原成处理包。缺字段按旧版本兼容读取。"""
        scope = dict(payload.get("scope") or {})
        return cls(
            turn_id=str(payload.get("turn_id") or ""),
            occurred_at=str(payload.get("occurred_at") or ""),
            scope_owner=str(scope.get("owner_id") or ""),
            scope_channel=str(scope.get("channel") or ""),
            scope_conversation=str(scope.get("conversation_id") or ""),
            scope_module=str(scope.get("module_id") or "companion"),
            scope_kind=str(scope.get("kind") or "conversation"),
            project_binding=str(scope.get("project_id") or ""),
            user_text=str(payload.get("user_text") or ""),
            published_reply=str(payload.get("published_reply") or ""),
            outcome_status=str(payload.get("outcome_status") or "published"),
            task_goal=str(payload.get("task_goal") or ""),
            relevant_history=[
                HistoryItem(
                    message_id=str(item.get("message_id") or ""),
                    role=str(item.get("role") or ""),
                    content=str(item.get("content") or ""),
                )
                for item in (payload.get("relevant_history") or [])
            ],
            recalled_memory_refs=[
                MemoryRef(
                    memory_id=str(item.get("memory_id") or ""),
                    kind=str(item.get("kind") or ""),
                    content_status=str(item.get("content_status") or ""),
                    excerpt=str(item.get("excerpt") or ""),
                    version=int(item.get("version") or SEMANTICS_VERSION),
                    layer=str(item.get("layer") or ""),
                    semantic_judgment=item.get('semantic_judgment') if isinstance(item.get('semantic_judgment'),dict) else {},
                )
                for item in (payload.get("recalled_memory_refs") or [])
            ],
            evidence_refs=[
                EvidenceRef(
                    ref_id=str(item.get("ref_id") or ""),
                    summary=str(item.get("summary") or ""),
                    outcome=str(item.get("outcome") or "unknown"),
                    evidence_level=str(item.get("evidence_level") or ""),
                )
                for item in (payload.get("evidence_refs") or [])
            ],
            completeness=str(payload.get("completeness") or "complete"),
            omitted_sections=tuple(payload.get("omitted_sections") or ()),
            extraction_profile_version=int(
                payload.get("extraction_profile_version") or EXTRACTION_PROFILE_VERSION
            ),
            schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
            # 缺这个字段的旧包按空处理：旧任务继续可读，行为不变。
            clarification_context=(
                dict(payload.get("clarification_context"))
                if isinstance(payload.get("clarification_context"), dict) else {}
            ),
        )


def turn_from_messages(
    *,
    turn_id: str,
    occurred_at: str,
    scope: Any,
    user_text: str,
    published_reply: str,
    outcome_status: str = "published",
    task_goal: str = "",
    scope_kind: str = "conversation",
    project_binding: str = "",
    relevant_history: Iterable[Mapping[str, Any]] = (),
    recalled_memory_refs: Iterable[Mapping[str, Any]] = (),
    evidence_refs: Iterable[Mapping[str, Any]] = (),
    handoff: Mapping[str, Any] | None = None,
) -> MemoryTurnPacket:
    """从主回合的既有对象（``Scope`` + 消息列表 + 交接快照）拼一个处理包。

    只做搬运：主回合已经有的东西才放进来，这里不会再去查库或跑搜索。
    ``handoff`` 是主回合结束时留下的交接快照（本轮用过的记忆、外部证据、
    工具终局状态、可信项目绑定）。工具的终局状态只能来自它 ——
    从回答文字里反推"工具成没成功"就是猜。
    """
    history: list[HistoryItem] = []
    for item in relevant_history:
        kind = str(item.get("kind") or item.get("role") or "")
        if kind in ("task_result", "work_result", "steward_progress"):
            # 后台终态播报不是用户说过的话，进不了「必要历史」。
            continue
        history.append(
            HistoryItem(
                message_id=str(item.get("id") or item.get("message_id") or ""),
                role=str(item.get("role") or ""),
                content=str(item.get("content") or ""),
            )
        )
    snapshot = dict(handoff or {})
    recalled_source = list(snapshot.get("recalled_memory_refs") or recalled_memory_refs)
    evidence_source = [
        *(snapshot.get("evidence_refs") or ()),
        # 工具终局状态当成一类证据：成功/失败都是事实，按原状态记，不美化。
        *[
            {
                "ref_id": str(item.get("action_id") or item.get("name") or ""),
                "summary": f"工具 {item.get('name') or '未命名'} 终局：{item.get('status') or '未知'}",
                "outcome": str(item.get("status") or "unknown"),
            }
            for item in (snapshot.get("tool_outcomes") or ())
            if isinstance(item, Mapping)
        ],
        *evidence_refs,
    ]
    recalled = [
        MemoryRef(
            memory_id=str(item.get("memory_id") or item.get("id") or ""),
            kind=str(item.get("kind") or ""),
            content_status=str(item.get("content_status") or ""),
            excerpt=str(item.get("excerpt") or item.get("content") or ""),
            version=int(item.get("version") or item.get('record_version') or SEMANTICS_VERSION),
            layer=str(item.get("layer") or ""),
            semantic_judgment=item.get('semantic_judgment') if isinstance(item.get('semantic_judgment'),dict) else {},
        )
        for item in recalled_source
    ]
    evidence = [
        EvidenceRef(
            ref_id=str(item.get("ref_id") or item.get("id") or ""),
            summary=str(item.get("summary") or item.get("content") or ""),
            outcome=str(item.get("outcome") or "unknown"),
            evidence_level=str(item.get("evidence_level") or ""),
        )
        for item in evidence_source
    ]
    packet = MemoryTurnPacket(
        turn_id=turn_id,
        occurred_at=occurred_at,
        scope_owner=getattr(scope, "owner_id", ""),
        scope_channel=getattr(scope, "channel", ""),
        scope_conversation=getattr(scope, "conversation_id", ""),
        scope_module=getattr(scope, "module_id", "companion"),
        scope_kind=scope_kind,
        project_binding=project_binding or str(snapshot.get("project_binding") or ""),
        user_text=user_text,
        published_reply=published_reply,
        outcome_status=outcome_status,
        task_goal=task_goal,
        relevant_history=history,
        recalled_memory_refs=recalled,
        evidence_refs=evidence,
    )
    # 出厂就按预算裁一次：网页、本地队列和云端三条路拿到的是同一份。
    # 留给各入口自己裁，就变成「谁想起来谁裁」——实际结果是三条路都没裁。
    return apply_packet_budget(packet)[0]


def apply_packet_budget(
    packet: MemoryTurnPacket,
    *,
    unit_limit: int = PACKET_UNIT_LIMIT,
    section_limits: Mapping[str, tuple[int, int]] | None = None,
) -> tuple[MemoryTurnPacket, dict[str, Any]]:
    """按预算裁剪处理包，返回 (裁剪后的包, 报告)。

    裁剪顺序：先丢重复背景与低相关材料 —— **不截断结构化字符串**，
    也不因为裁掉了背景就推断「用户没有约束」。裁不下的当前问答标为需重试，
    绝不静默当成「已经完整理解」。
    """
    limits = dict(PACKET_SECTION_LIMITS)
    if section_limits:
        limits.update(dict(section_limits))
    omitted: list[str] = list(packet.omitted_sections)
    report: dict[str, Any] = {"dropped": {}, "unit_limit": unit_limit}

    def _trim(items: list[Any], name: str) -> list[Any]:
        max_items, max_units = limits.get(name, (len(items), 10**9))
        kept: list[Any] = []
        used = 0
        seen: set[str] = set()
        for item in items:
            key = normalize_text(getattr(item, "content", "") or getattr(item, "summary", "") or getattr(item, "excerpt", ""))
            if key and key in seen:
                report["dropped"][name] = report["dropped"].get(name, 0) + 1
                continue
            units = getattr(item, "units", estimate_units(key))
            if len(kept) >= max_items or used + units > max_units:
                report["dropped"][name] = report["dropped"].get(name, 0) + 1
                continue
            seen.add(key)
            kept.append(item)
            used += units
        return kept

    packet.relevant_history = _trim(list(packet.relevant_history), "relevant_history")
    packet.recalled_memory_refs = _trim(list(packet.recalled_memory_refs), "recalled_memory_refs")
    packet.evidence_refs = _trim(list(packet.evidence_refs), "evidence_and_tool_outcomes")

    if packet.total_units() > unit_limit:
        # 当前问答放不下：不静默截断，标出来让处理端重试或拆分。
        omitted.append("current_turn")
        packet.completeness = "partial"
        report["needs_retry"] = True
    if report["dropped"]:
        for name in report["dropped"]:
            if name not in omitted:
                omitted.append(name)
        packet.completeness = "partial"
    packet.omitted_sections = tuple(dict.fromkeys(omitted))
    report["units"] = packet.total_units()
    report["completeness"] = packet.completeness
    return packet, report


def serialize_candidates(candidates: Iterable[MemoryCandidate]) -> str:
    """候选列表 → JSON 文本（持久队列里存这个）。"""
    return json.dumps(
        [
            {
                "content": c.content,
                "kind": c.kind,
                "content_status": c.content_status,
                "source_types": list(c.source_types),
                "scope_kind": c.scope_kind,
                "occurred_at": c.occurred_at,
                "reason": c.reason,
                "goal": c.goal,
                "project_id": c.project_id,
                "project_label": c.project_label,
                "horizon": c.horizon,
                "topics": list(c.topics),
                "related_ids": list(c.related_ids),
                "supersedes": list(c.supersedes),
                "source_refs": list(c.source_refs),
                "evidence_level": c.evidence_level,
                "confidence": c.confidence,
                "importance": c.importance,
                "origin": c.origin,
                "stable_key": c.resolved_stable_key(),
                "profile_version": int(c.profile_version),
                "review_receipt": c.review_receipt,
            }
            for c in candidates
        ],
        ensure_ascii=False,
    )


def serialize_suggestions(suggestions: Iterable[MemoryUpdateSuggestion]) -> str:
    """更新建议列表 → JSON 文本（维护作业里存这个，重试时复用原建议）。

    重试必须复用**原来那条建议**：重跑一次模型会得出措辞略有不同的一版，
    然后「同一动作重复执行」就失去意义了 —— 幂等键算出来的不再是同一个。
    """
    return json.dumps(
        [
            {
                "action": s.action,
                "content": s.content,
                "kind": s.kind,
                "content_status": s.content_status,
                "source_types": list(s.source_types),
                "source_refs": list(s.source_refs),
                "target_id": s.target_id,
                "expected_version": s.expected_version,
                "conflict_ids": list(s.conflict_ids),
                "reason": s.reason,
                "horizon": s.horizon,
                "topics": list(s.topics),
                "value": bool(s.value),
                "confidence": s.confidence,
                "importance": s.importance,
                "origin": s.origin,
                "review_receipt": s.review_receipt,
            }
            for s in suggestions
        ],
        ensure_ascii=False,
    )


def decode_suggestions(raw: str | None) -> list[MemoryUpdateSuggestion]:
    """JSON 文本 → 更新建议列表；解析不了返回空（调用方据此重新提炼）。"""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    decoded: list[MemoryUpdateSuggestion] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        decoded.append(
            MemoryUpdateSuggestion(
                action=str(item.get("action") or ""),
                content=str(item.get("content") or ""),
                kind=str(item.get("kind") or ""),
                content_status=str(item.get("content_status") or ""),
                source_types=tuple(item.get("source_types") or ()),
                source_refs=tuple(item.get("source_refs") or ()),
                target_id=str(item.get("target_id") or ""),
                expected_version=item.get("expected_version"),
                conflict_ids=tuple(item.get("conflict_ids") or ()),
                reason=str(item.get("reason") or ""),
                horizon=str(item.get("horizon") or ""),
                topics=tuple(item.get("topics") or ()),
                value=bool(item.get("value", True)),
                confidence=item.get("confidence"),
                importance=item.get("importance"),
                origin=str(item.get("origin") or "model_candidate"),
                review_receipt=item.get('review_receipt') if isinstance(item.get('review_receipt'), dict) else {},
            )
        )
    return decoded


def candidate_from_payload(payload: Mapping[str, Any]) -> MemoryCandidate:
    """把持久化/传输里的候选字典还原成对象（重试时复用已保存的候选）。"""

    def _strings(name: str) -> tuple[str, ...]:
        value = payload.get(name)
        if isinstance(value, str):
            return (value,) if value else ()
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        return ()

    return MemoryCandidate(
        content=str(payload.get("content") or ""),
        kind=str(payload.get("kind") or ""),
        content_status=str(payload.get("content_status") or ""),
        source_types=_strings("source_types"),
        scope_kind=str(payload.get("scope_kind") or "conversation"),
        occurred_at=str(payload.get("occurred_at") or ""),
        reason=str(payload.get("reason") or ""),
        goal=str(payload.get("goal") or ""),
        project_id=str(payload.get("project_id") or ""),
        project_label=str(payload.get('project_label') or ''),
        horizon=str(payload.get('horizon') or ''),
        topics=_strings("topics"),
        related_ids=_strings("related_ids"),
        supersedes=_strings("supersedes"),
        source_refs=_strings("source_refs"),
        evidence_level=str(payload.get("evidence_level") or ""),
        confidence=payload.get("confidence"),
        importance=payload.get("importance"),
        origin=str(payload.get("origin") or "model_candidate"),
        stable_key=str(payload.get("stable_key") or ""),
        profile_version=int(payload.get("profile_version") or EXTRACTION_PROFILE_VERSION),
        review_receipt=payload.get('review_receipt') if isinstance(payload.get('review_receipt'), dict) else {},
    )


def decode_candidates(raw: str | None) -> list[MemoryCandidate]:
    """JSON 文本 → 候选列表；解析不了就返回空（重试时会重新跑提炼）。"""
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    decoded: list[MemoryCandidate] = []
    for item in items:
        if isinstance(item, Mapping):
            decoded.append(candidate_from_payload(item))
    return decoded


def scope_from_packet(packet: MemoryTurnPacket):
    """处理包 → :class:`Scope`（写入和记录访问都用这一个入口，避免各处自己拼）。"""
    from .contracts import Scope

    return Scope(
        owner_id=packet.scope_owner,
        channel=packet.scope_channel,
        conversation_id=packet.scope_conversation,
        module_id=packet.scope_module,
    )


#: 用户明确表达采纳/确认的原话特征。只做**加分**：命中不保证真的采纳，
#: 一次都没命中就不能给「已采纳」这个状态。刻意不放「可以」「行」这类
#: 单字常用词 —— 「执行」「运行」里都含「行」，会把无关句子判成采纳。
ADOPTION_MARKERS: tuple[str, ...] = (
    "就按", "就照", "按这个", "照这个", "就这么", "就这样", "按你说的",
    "同意", "赞成", "采纳", "采用", "批准", "确认", "定了", "定下来",
    "无异议", "没问题",
)
#: 明确否决或替代的原话特征。这些词本身就是否定的形态，直接算证据。
REJECTION_MARKERS: tuple[str, ...] = (
    "不用", "不要", "别用", "别做", "取消", "否决", "改用", "换成", "改为",
    "不采用", "先不",
)
#: 「这件事还没定／已经变了」的原话特征。本轮命中且与事项相关时，
#: 任何采纳表达都不作数：用户自己说没定，就不能记成已采纳。
UNSETTLED_MARKERS: tuple[str, ...] = (
    "未决定", "没决定", "还没定", "没定", "待定", "不确定", "未确定", "未确认",
    "还没变", "没有变", "没变化", "未变", "尚未", "还没",
)
#: 失效类动作（invalidate）需要的原话特征：用户自己说这件事不作数了。
INVALIDATION_MARKERS: tuple[str, ...] = (
    "不用", "不要", "别用", "别做", "取消", "否决", "改用", "换成", "改为",
    "不采用", "作废", "过期", "到期", "失效", "撤销", "不再", "结束", "先不",
)
#: 标记词附近出现这些字，说明那句话在提问或在否定，不算采纳。
_ADOPTION_NEGATIONS: tuple[str, ...] = ("不", "别", "否", "吗", "？", "?")
_ADOPTION_WINDOW = 3
#: 条件、假设、犹豫词：句子里出现这些，说明用户是在**设想**而不是表态。
#: 「如果同意的话就上云」「一旦定了就一律先落盘」都只是「有表态的样子」，
#: 事情并没有定下来。宁可退回建议，也不替用户拍板 —— 与提问、否定同一处理。
HEDGE_MARKERS: tuple[str, ...] = (
    "如果", "要是", "假如", "假若", "万一", "倘若", "若是", "一旦", "除非",
    "只要", "若", "假使", "就算", "哪怕", "即使",
    "看情况", "再说吧", "也许", "或许", "大概", "可能", "说不定",
    "考虑一下", "想想再", "以后再说", "待商量", "商量一下",
)
#: 「的话」单独判：它既可以是条件（「不累的话就…」），也可以只是「说的话／
#: 讲的话」里的普通词。只有后面紧接着「就／便／才／再」才算条件，免得把
#: 「就照我刚才说的话一律先落盘」这种明确决定也否掉。
_HEDGE_IF_SUFFIX = "的话"
_HEDGE_IF_FOLLOWERS: tuple[str, ...] = ("就", "便", "才", "再", "，", ",", "。")
_HEDGE_IF_WINDOW = 3


def is_hedged(text: Any) -> bool:
    """这段话是不是「有条件的设想」而不是已经定下来的表态。"""
    raw = str(text or "")
    if any(marker in raw for marker in HEDGE_MARKERS):
        return True
    start = 0
    while True:
        at = raw.find(_HEDGE_IF_SUFFIX, start)
        if at < 0:
            return False
        after = raw[at + len(_HEDGE_IF_SUFFIX) : at + len(_HEDGE_IF_SUFFIX) + _HEDGE_IF_WINDOW]
        if any(after.startswith(follower) for follower in _HEDGE_IF_FOLLOWERS):
            return True
        start = at + len(_HEDGE_IF_SUFFIX)

#: 条件判断的作用域标点：只有句末（句号/分号/叹号/问号/换行）才断开。
#: 逗号**不算**——「如果不累的话，就一律先落盘」是一个条件句被逗号切成两段，
#: 后半段自己不带条件词，按分句判就会漏过去。
_SENTENCE_SPLIT = re.compile(r"[。；;！!？?\n]+")


def sentences(text: Any) -> tuple[str, ...]:
    """按句末标点切句。条件、假设的作用域是**整句**，不是逗号分出来的小段。"""
    return tuple(part.strip() for part in _SENTENCE_SPLIT.split(str(text or "")) if part.strip())


def unhedged_clauses(text: Any) -> tuple[str, ...]:
    """把一段话切成「不带条件的那些分句」。

    先按句末标点分句，整句带条件（如果/要是/看情况…）就整句丢掉；剩下的句子
    再按逗号分成分句交给标记词匹配。这样「如果不累的话，就一律先落盘」不会
    漏，「如果累了就先歇会儿。另外日志一律先落盘」里的后半句也不会被误伤。
    """
    kept: list[str] = []
    for sentence in sentences(text) or (str(text or ""),):
        if is_hedged(sentence):
            continue
        kept.extend(clauses(sentence) or (sentence,))
    return tuple(kept)

#: 分句用的标点：采纳、失效、纠正都要落到**具体那一句**，不能跨句借证据。
_CLAUSE_SPLIT = re.compile(r"[。；;！!？?\n，、]+|(?<!\d),|,(?!\d)")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")
_LATIN_RUN = re.compile(r"[0-9A-Za-z]+")
#: 数字与中文数词。纠正「五千→三千」这类变化，判据就是这些值有没有出现在原话里。
_VALUE_RUN = re.compile(r"\d+(?:\.\d+)?|[零〇一二三四五六七八九十百千万亿两]+")
#: 千分位写法：`3,000`。中文里逗号是分句符，只有「三位一组」才当成同一个数。
#: 用空白边界排除半截匹配（`\d{1,3}` 会从 "3,000" 中间咬出 "000"）。
_GROUPED_NUMBER = re.compile(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d,])")
#: 数值量词。值要连单位一起认（「三千元」≠「三千人」），所以单位单独看得见。
#: 长的写法排在前面，免得「人民币」只匹配到「人」。
_VALUE_UNIT = re.compile(
    r"人民币|美元|欧元|日元|港币|公里|千米|千克|小时|分钟|"
    r"元|块|万|千|百|亿|个|人|天|年|月|周|日|秒|次|条|台|件|套|份|页|米|克|吨|升|%|％"
)
#: 拼数字用得上的汉字，以及它们各自的数值。
_CN_DIGITS: dict[str, int] = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_SMALL_UNITS: dict[str, int] = {"十": 10, "百": 100, "千": 1000}
_CN_BIG_UNITS: dict[str, int] = {"万": 10000, "亿": 100000000}
#: 「2026年」里的「年」是日期量词，不是数值词尾 —— 这种上下文不参与值比对，
#: 否则「2026年12月31日」会被切成一个值去跟原话比对。
_DATE_CONTEXT = re.compile(r"年|月|日|号|时|分|秒|点|周|星期|季度|期")
#: 这些二元组太泛，不足以证明「说的是同一件事」。
_GENERIC_UNITS = frozenset(
    {
        "用户", "我们", "他们", "你们", "这个", "那个", "一个", "可以", "已经",
        "就是", "不是", "什么", "因为", "所以", "如果", "现在", "以后", "这样",
        "那样", "还是", "只是", "然后", "但是", "而且",
    }
)


def clauses(text: Any) -> tuple[str, ...]:
    """把一段话切成短句（按标点）。证据绑定事项时要一句一句看。"""
    return tuple(part.strip() for part in _CLAUSE_SPLIT.split(str(text or "")) if part.strip())


def content_units(text: Any) -> set[str]:
    """一段文字的**实义片段**：中文二元组 + 两字以上的拉丁词/数字。

    用途是核对「两句说的是不是同一件事」：无关历史里的「同意」与本案正文
    没有任何共同片段，就借不到这个证据。
    """
    raw = str(text or "")
    units: set[str] = set()
    for run in _CJK_RUN.findall(raw):
        if len(run) == 1:
            units.add(run)
            continue
        units.update(run[index : index + 2] for index in range(len(run) - 1))
    units.update(
        match.group(0)
        for match in _LATIN_RUN.finditer(raw)
        if len(match.group(0)) > 1 or match.group(0).isdigit()
    )
    return {unit for unit in units if unit not in _GENERIC_UNITS}


def _chinese_number(text: str) -> int | None:
    """中文数词 → 整数。认不出（比如光一个「元」）就返回 None。

    支持口语里常见的写法：三千=3000、一万=10000、两千五=2500、两百=200。
    「万」以上的部分整体进位，所以「一亿三千万」也能算对。
    """
    total = 0
    section = 0
    number = 0
    seen = False
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            seen = True
        elif char in _CN_SMALL_UNITS:
            section += (number or 1) * _CN_SMALL_UNITS[char]
            number = 0
            seen = True
        elif char in _CN_BIG_UNITS:
            total += (section + (number or 1)) * _CN_BIG_UNITS[char]
            section = 0
            number = 0
            seen = True
        else:
            return None
    return total + section + number if seen else None


def _value_entry(raw: str, after: str = "") -> tuple[int, str] | None:
    """一个数值片段 → ``(数值, 量词)``；算不出的值不算数。

    ``raw`` 是数值本身，``after`` 是它紧跟的一小段上下文 —— 量词常常写在
    数字后面（「3000元」），也可能隔着一个「的」之类的助词（「5000 元的预算」），
    所以多给几个字让量词找得到。
    """
    raw = raw.replace(",", "")  # 千分位只是为了好读，比较的是数值本身
    if _DATE_CONTEXT.search(raw):
        # 「2026年12月31日」是日期，不是待比对的数值。
        return None
    digits = re.match(r"\d+(?:\.\d+)?", raw)
    if digits:
        raw_digits = digits.group(0)
        value = int(float(raw_digits)) if "." in raw_digits else int(raw_digits)
    else:
        parsed = _chinese_number(raw)
        if parsed is None:
            return None
        value = parsed
    tail = raw[digits.end() :] if digits else raw.lstrip("零〇一二三四五六七八九十百千万亿两")
    unit_match = _VALUE_UNIT.search(tail) or _VALUE_UNIT.search(after)
    return value, unit_match.group(0) if unit_match else ""


def _is_ordinary_word_numeral(raw: str, start: int, end: int) -> bool:
    """这个数词片段是不是**普通词里的一个汉字**，而不是一个数值。

    「一律」「统一」「一样」「唯一」里的「一」不是数值：整段只切得出一个「一」，
    但那是词的一部分。以前它会当成一个待核对的值，于是正常改写被要求
    「一」有出处，拒绝理由还写成「新值缺少原话依据」。

    判据收得很窄，只处理**单个汉字数词**：
    - 后面紧跟的是普通汉字（不是数词、不是量词）→ 是词的一部分；
    - 后面没有字或不是汉字（句末、标点）时，看前面是不是普通汉字（「唯一」）。
    多字数值（三千、一万）与带量词的数值（五人、五天）一律不受影响。
    """
    token = raw[start:end]
    if len(token) != 1 or token not in _CN_DIGITS:
        return False
    after = raw[end : end + 1]
    after_is_cjk = bool(after) and bool(_CJK_RUN.fullmatch(after))
    if after_is_cjk and not _VALUE_UNIT.match(raw[end:]):
        return True
    if not after_is_cjk:
        before = raw[start - 1 : start]
        if before and _CJK_RUN.fullmatch(before) and before not in _CN_DIGITS:
            return True
    return False


def _value_spans(raw: str) -> list[tuple[int, int, str]]:
    """一段文字里的数值片段：``(起点, 终点, 文本)``，按出现顺序排好。

    千分位写法（``3,000``）占掉的字符区间里，普通切法不再重复切 —— 否则
    ``3,000`` 会被 ``\\d+`` 咬成 ``3`` 和 ``000`` 两个假值，``3`` 又去后面
    借一个「千」变成三千。

    普通词里的单个数词（「一律」的「一」）不算数值，见
    :func:`_is_ordinary_word_numeral`。
    """
    grouped = list(_GROUPED_NUMBER.finditer(raw))
    spans = [(match.start(), match.end()) for match in grouped]

    def _inside(match: re.Match[str]) -> bool:
        return any(start <= match.start() and match.end() <= end for start, end in spans)

    simple = [
        match
        for match in _VALUE_RUN.finditer(raw)
        if not _inside(match)
        and not _is_ordinary_word_numeral(raw, match.start(), match.end())
    ]
    found = [(match.start(), match.end(), match.group(0)) for match in simple]
    found.extend((match.start(), match.end(), match.group(0)) for match in grouped)
    return sorted(found)


def value_entries(text: Any) -> tuple[tuple[int, str], ...]:
    """一段文字里可比较的数值：``(数值, 量词)``，按出现顺序去重。

    「三千元」「3,000元」「3000 元」都给出 ``(3000, "元")``。
    只用来做值比对，不改 ``value_tokens`` 的既有切分。
    """
    raw = str(text or "")
    entries: list[tuple[int, str]] = []
    for start, end, token in _value_spans(raw):
        entry = _value_entry(token, raw[end : end + 4])
        if entry is not None and entry not in entries:
            entries.append(entry)
    return tuple(entries)


def equivalent_values(left: Any, right: Any) -> bool:
    """两段文字里有没有「同一个值」——中文数词与阿拉伯数字算同一个。

    「三千元」和「3000元」是同一个值；「三千元」和「三千人」不是。
    两边都没写量词时按数值相等算（「三千」≈「3000」）。
    """
    return any(_values_match(one, other) for one in value_entries(left) for other in value_entries(right))


def _values_match(left: tuple[int, str], right: tuple[int, str]) -> bool:
    """两个值是不是同一个：数值相等，而且量词对得上。

    量词只在**两边都写了**的时候才挑：「三千人」和「3000元」是两回事，
    但「3000」和「3000元」是同一个数。钱币类量词彼此等价（元／块／人民币）。
    """
    if left[0] != right[0]:
        return False
    wanted, found = left[1], right[1]
    if not wanted or not found or wanted == found:
        return True
    return unit_is_money(wanted) and unit_is_money(found)


def unit_is_money(unit: str) -> bool:
    """这个量词是不是钱（元／块，以及「币」结尾的外币）。"""
    return unit in ("元", "块") or "币" in unit


def _same_value(entry: tuple[int, str], text: str) -> bool:
    """原话里有没有支撑这个值的**写法**。

    量词只在这段原话里**真的写了量词**时才挑：原话是「三千人」不能支撑
    「3000元」；原话是「预算是三千」（没写量词）时，数值对上就算数 ——
    用户本来就可能省略量词，程序不能因此否定他自己说过的数。
    """
    raw = str(text or "")
    if any(_values_match(entry, other) for other in value_entries(raw)):
        return True
    if entry[1] and unit_is_money(entry[1]):
        # 原话整段都在说钱，且原话里出现的是同一个数（自己没带量词）。
        plain = (entry[0], "")
        if _has_money_context(raw) and any(
            other[0] == entry[0] and not other[1] for other in value_entries(raw)
        ):
            return plain[0] == entry[0]
    return False


def _has_money_context(text: str) -> bool:
    """整段话是不是在说钱。原话省略量词时（「预算是三千」）靠它补上下文。"""
    return bool(re.search(r"钱|费用|预算|价格|价钱|成本|金额|报价|收费|工资|薪|花|付", text))


def value_evidenced(text: Any, value: Any) -> bool:
    """这段原话里有没有支撑这个值 —— 中文数词与阿拉伯数字算同一个值。

    「三千元」能支撑 ``"3000元"``、``"3000"``、``"三千"``；反过来 ``"3,000元"``
    也能支撑 ``"三千元"``。原话里根本没有这个数（哪怕是别的数）时返回 False。
    """
    raw = str(text or "")
    if not raw:
        return False
    entries = value_entries(value)
    if not entries:
        return str(value or "") in raw
    return any(_same_value(entry, raw) for entry in entries)


def mentions_item(text: Any, item_text: Any) -> bool:
    """这段话说的是不是这一条（按实义片段有交集判断）。"""
    units = content_units(item_text)
    if not units:
        return False
    raw = str(text or "")
    return any(unit in raw for unit in units)


def cites_item_subject(text: Any, item_text: Any, *, value: Any = "") -> bool:
    """这段原话是不是在说**这件事**（采纳、纠正绑事项时用）。

    比 :func:`mentions_item` 多认两种「明明在说同一件事却没共用字面」的情况：

    - 值相同但写法不同：原话说「预算三千」，记载写「预算为3000元」——
      实义片段对不上，但数值对得上，这就不能算「没提这件事」。
    - 原话只给了值、词面被改写：「不是五千，是三千」对记载「预算3000元」，
      数值对上了，就该认成同一件事。

    判定是从严的：先把事项自己的值找出来，只有**值真的对得上**才算候选；
    文字片段那条路仍然要求有共同实义片段。两样都不沾就返回 False。
    """
    if mentions_item(text, item_text):
        return True
    raw = str(text or "")
    if not raw:
        return False
    subject = value if str(value or "").strip() else item_text
    return any(value_evidenced(raw, token) for token in value_tokens(subject))


def value_tokens(text: Any) -> tuple[str, ...]:
    """一段文字里的数值片段（阿拉伯数字或中文数词）。"""
    tokens = [token for _, _, token in _value_spans(str(text or ""))]
    return tuple(dict.fromkeys(tokens))


def _value_text_key(text: str) -> tuple[tuple[int, str], ...]:
    """整段文字的「值指纹」：用来判断新旧内容里的值是不是同一批。"""
    return value_entries(text)


def _value_rewrites(new_text: Any, current_text: Any) -> set[str]:
    """新旧内容里**同一个值的不同写法**所带出的字面片段。

    只在「值全都没变、只是换了写法」时用：`五千`／`千元`／`算五` 都要当成写法，
    而真正的文字新增（「另外加急」）不含在内，仍然报得出来。
    """
    current_entries = value_entries(current_text)
    raw_new = str(new_text or "")
    rewritten: set[str] = set()
    covered_ranges: list[tuple[int, int]] = []
    for start, end, token in _value_spans(raw_new):
        entries = value_entries(token)
        if not entries:
            continue
        if not any(_values_match(entry, known) for entry in entries for known in current_entries):
            continue
        # 这个数值片段本身，以及它的量词尾巴（「五千」的「千元」）。
        rewritten.add(token)
        stop = min(end + 2, len(raw_new))
        rewritten.add(raw_new[start:stop])
        covered_ranges.append((start, stop))
    # 中文实义片段里凡是**和这些写法重了字**的，都是换写法带出来的残片。
    # 「预算五千元」里的「算五」横跨前缀和数值，不是包含关系，只能按重叠判。
    for run_index, run in enumerate(_CJK_RUN.finditer(raw_new)):
        for index in range(len(run.group(0)) - 1):
            piece_start = run.start() + index
            piece = raw_new[piece_start : piece_start + 2]
            if any(piece_start < stop and start < piece_start + 2 for start, stop in covered_ranges):
                rewritten.add(piece)
    return rewritten


def changed_value_tokens(new_text: Any, current_text: Any) -> tuple[str, ...]:
    """新内容相对旧内容**变了的值**：先比数字，没有数字再比新出现的实义片段。

    纠正「不是五千，是三千」时，要核对的就是「三千」这两个字有没有出现在
    用户原话里。模型自己说「用户纠正了预算」不算 —— 原话里没有新值，
    这次覆盖就没有依据。

    比的是**值**，不是写法：旧内容写「5000元」、新内容写「五千元」只是换写法，
    值这一层没有要核对的东西，不算「改了值」。
    """
    def _differs(token: str) -> bool:
        entries = value_entries(token)
        if not entries:
            return token not in current_tokens
        # 比的是值不是写法：旧内容「5000元」、新内容「五千元」算没变。
        return any(
            not any(_values_match(entry, known) for known in current_entries)
            for entry in entries
        )

    current_tokens = set(value_tokens(current_text))
    current_entries = _value_text_key(str(current_text or ""))
    new_tokens = value_tokens(new_text)
    changed = [token for token in new_tokens if _differs(token)]
    if changed:
        return tuple(changed)

    current_units = content_units(current_text)
    diff_units = set(content_units(new_text))
    diff_units.update(match.group(0) for match in _LATIN_RUN.finditer(str(new_text or "")))
    rewritten = _value_rewrites(new_text, current_text) if new_tokens else set()
    return tuple(
        sorted(
            unit
            for unit in diff_units
            if unit not in current_units
            and not any(unit in piece or piece in unit for piece in rewritten)
        )
    )


def value_evidence_gaps(
    new_text: Any, current_text: Any, evidence_texts: Iterable[Any]
) -> tuple[tuple[str, str], ...]:
    """**逐项**核对变化的值有没有依据，返回 ``((值, 所在原话), ...)``。

    调用方要的是「一条变化对应一条依据」，不是「命中任意一个就算整条有依据」：
    新内容把预算改成三千、又多出一个日期时，只有三千能在原话里找到出处，
    日期那一项必须单独报出来（见 B3-R3）。

    比的是值不是写法：原话说「三千元」，新内容写「3000元」算有依据（B3-R4）。
    值的写法对不上、原话里根本没有这个数的，这一项就留在结果里。
    """
    if not str(new_text or "").strip() or not str(current_text or "").strip():
        return ()
    evidence = [str(text or "") for text in (evidence_texts or ()) if str(text or "").strip()]
    gaps: list[tuple[str, str]] = []
    for token in changed_value_tokens(new_text, current_text):
        if not value_entries(token):
            # 不是数值（中文实义片段等），不在这里判，交给事项绑定的既有规则。
            continue
        # 一项一项找依据：某项找不到，就单独记这一项，不能拿别项的依据顶替。
        support = next((text for text in evidence if value_evidenced(text, token)), "")
        gaps.append((token, support))
    return tuple(gaps)


def value_evidence_missing(
    new_text: Any, current_text: Any, evidence_texts: Iterable[Any]
) -> tuple[str, ...]:
    """:func:`value_evidence_gaps` 的简版：只要那些**没有依据**的值。"""
    return tuple(
        token
        for token, support in value_evidence_gaps(new_text, current_text, evidence_texts)
        if not support
    )


def packet_source_roles(packet: MemoryTurnPacket) -> dict[str, tuple[str, ...]]:
    """处理包里**真实存在**的来源引用 → 这些引用的角色。

    合法引用只有两类：回合号本身、必要历史里的消息号。模型可以随手编一个
    「nonexistent-message」当出处 —— 写出来不等于它存在，所以引用要先在这里
    对得上，才轮得到「来源可信」这个判断。
    """
    roles: dict[str, set[str]] = {}
    if not is_blank(packet.turn_id):
        turn_roles: set[str] = set()
        if not is_blank(packet.user_text):
            turn_roles.add("user")
        if packet.is_published():
            turn_roles.add("assistant")
        roles[packet.turn_id] = turn_roles
    for item in packet.relevant_history:
        message_id = str(getattr(item, "message_id", "") or "")
        if message_id:
            roles.setdefault(message_id, set()).add(str(getattr(item, "role", "") or ""))
    return {ref: tuple(sorted(values)) for ref, values in roles.items()}


def user_authored_texts(packet: MemoryTurnPacket) -> tuple[str, ...]:
    """处理包里**用户自己说过**的话。助手文本不算 —— 助手建议不能当采纳依据。"""
    texts = [str(packet.user_text or "")]
    texts.extend(
        str(item.content or "")
        for item in packet.relevant_history
        if str(getattr(item, "role", "") or "") == "user"
    )
    return tuple(text for text in texts if text.strip())


def expresses_adoption(text: str, *, item_text: str = "") -> bool:
    """这段用户原话里有没有一段**没被否定**的采纳表达。

    判据刻意保守：标记词前后三个字内出现「不／别／否／是否／吗／？」就当疑问或
    否定处理。「先不要决定是否采用」里虽然写着「采用」，但它前面是「是否」——
    这条规则正是用来拦住这种伪造采纳的。判不出来就返回 False：宁可让这条候选
    退回「建议」，也不给它盖上「用户已采纳」的章。

    带条件的设想同样不算表态：「如果同意的话就上云」「一旦定了就一律先落盘」
    说的都是「假设成立才怎样」，事情并没有定下来，所以整句按未表态处理。
    条件的作用域是**整句**：逗号切出来的后半段自己不带条件词，也不能算数。

    给了 ``item_text`` 就逐**分句**绑定事项：只有「提到这件事的那一句」里的
    采纳表达才算数。否则一句无关的「同意」会被拿来给另一件事盖章。
    """
    for clause in unhedged_clauses(text):
        if item_text and not mentions_item(clause, item_text):
            continue
        for marker in ADOPTION_MARKERS:
            start = 0
            while True:
                at = clause.find(marker, start)
                if at < 0:
                    break
                window = clause[
                    max(0, at - _ADOPTION_WINDOW) : at + len(marker) + _ADOPTION_WINDOW
                ]
                if not any(negation in window for negation in _ADOPTION_NEGATIONS):
                    return True
                start = at + len(marker)
    return False


def _clause_has_marker(text: str, markers: tuple[str, ...], *, item_text: str = "") -> bool:
    """某一句里有没有这些标记词；给了事项就要求这一句提到它。"""
    for clause in clauses(text) or (str(text or ""),):
        if item_text and not mentions_item(clause, item_text):
            continue
        if any(marker in clause for marker in markers):
            return True
    return False


def cited_user_evidence(
    packet: MemoryTurnPacket, source_refs: Iterable[str] | None = None
) -> tuple[tuple[str, bool], ...]:
    """**被这条内容引用过的**用户原话，附带「是不是本轮」。

    以前这里把处理包里所有用户话都算上，于是模型只要在旧历史里找到一句「同意」，
    就能给本轮完全无关的方案盖上「用户已采纳」。引用范围才是这条内容的证据范围：
    没被引用的历史消息不参与判断。

    没有引用（程序构造的建议）时退回本轮用户原话 —— 本轮就是它唯一的出处。
    """
    wanted = {str(ref) for ref in (source_refs or ()) if str(ref or "").strip()}
    evidence: list[tuple[str, bool]] = []
    if not wanted or str(packet.turn_id) in wanted:
        text = str(packet.user_text or "")
        if text.strip():
            evidence.append((text, True))
    for item in packet.relevant_history:
        if str(getattr(item, "role", "") or "") != "user":
            continue
        message_id = str(getattr(item, "message_id", "") or "")
        if message_id and message_id in wanted:
            content = str(getattr(item, "content", "") or "")
            if content.strip():
                evidence.append((content, False))
    return tuple(evidence)


def current_turn_denies(text: str, item_text: str) -> bool:
    """本轮是否**明说**这件事还没定／没有变化。

    「部署方案还未决定」这类话必须优先于任何采纳表达：用户明明说没定，
    模型却拿历史里的「同意」当依据，这一条就是拦住它的地方。
    """
    return any(
        mentions_item(clause, item_text)
        and any(marker in clause for marker in UNSETTLED_MARKERS)
        for clause in clauses(text)
    )


def expresses_invalidation(text: str, *, item_text: str = "") -> bool:
    """这段原话里有没有「这件事不作数了」的表达。

    给了事项就要求同一句提到它：用户说「周一那件事改周二」不能授权去改预算。
    """
    return _clause_has_marker(text, INVALIDATION_MARKERS, item_text=item_text)


def adoption_evidenced(
    packet: MemoryTurnPacket,
    content_status: str,
    *,
    source_refs: Iterable[str] | None = None,
    item_text: str = "",
) -> bool:
    """「用户已采纳／已否决」这个状态，在**被引用的**用户原话里有没有对应证据。

    三条约束：只认引用到的用户消息；历史消息必须是在说同一件事；本轮明说
    「还没定／没变化」时一律不算。缺一条就退回 proposed，而不是丢内容。
    """
    if source_refs is None:
        evidence: tuple[tuple[str, bool], ...] = tuple(
            (text, False) for text in user_authored_texts(packet)
        )
    else:
        evidence = cited_user_evidence(packet, source_refs)
    for text, is_current in evidence:
        if is_current:
            if item_text and current_turn_denies(text, item_text):
                continue
            if content_status == "rejected":
                if any(marker in text for marker in REJECTION_MARKERS):
                    return True
            elif expresses_adoption(text):
                return True
            continue
        if content_status == "rejected":
            if _clause_has_marker(text, REJECTION_MARKERS, item_text=item_text):
                return True
        elif expresses_adoption(text, item_text=item_text):
            return True
    return False


# ---------------------------------------------------------------------------
# 内容维护：对**已存在**的记录提更新建议（B 计划的自动记忆维护闭环）
#
# 与上面「新建候选」是两件事，刻意分成两个结构：
# 候选说「要新建什么」，建议说「要对哪条已有记录做什么」。
# 合成一个结构，``add`` 就会带着目标编号到处跑，读的人分不清该不该看它。
# ---------------------------------------------------------------------------

#: 七种维护动作。与「内容状态」「是否值得保存」三者相互独立：
#: 动作 = 这次要做什么；内容状态 = 内容本身走到哪一步；保存价值 = 值不值得记。
MAINTENANCE_ACTIONS: tuple[str, ...] = (
    "add",
    "merge",
    "correct",
    "replace",
    "conflict",
    "invalidate",
    "ignore",
)

#: 动作的中文说法。日志、报告、界面共用这一份，不各写各的。
MAINTENANCE_ACTION_LABELS: dict[str, str] = {
    "add": "新增",
    "merge": "合并或补充",
    "correct": "纠正",
    "replace": "替代",
    "conflict": "标记冲突",
    "invalidate": "失效或待复核",
    "ignore": "保持或忽略",
}

#: 必须指向一条已知记录的动作。新建不需要目标。
ACTIONS_REQUIRING_TARGET: frozenset[str] = frozenset(
    {"merge", "correct", "replace", "conflict", "invalidate"}
)


class MemoryUpdateConflict(MemoryCandidateError):
    """目标记录在写入事务里已被别的变更替代（版本保护失败）。

    是**拒绝**而不是执行失败：这条建议的取证前提已经不成立，重试一万次也还是
    不成立 —— 必须重新取证，不能拿它去覆盖一条已经翻过页的记录。
    """

#: 内容的时间尺度：长期事实、偏好与约束，还是短期状态（当下情绪、当前任务）。
CONTENT_HORIZONS: tuple[str, ...] = ("long_term", "short_term")

#: 记录级复核状态。与内容状态正交：内容对不对是一回事，要不要再看一遍是另一回事。
REVIEW_STATES: tuple[str, ...] = ("none", "pending", "settled")


@dataclass(frozen=True)
class MemoryVersionRef:
    """一条记录 + 它的版本号。

    版本号由程序从库里读，模型不许自己补：模型说「这是第 3 版」不代表库里
    就是第 3 版，拿它当依据等于让模型决定覆盖哪一条。
    """

    memory_id: str
    version: int = 1
    content_status: str = ""


@dataclass(frozen=True)
class MemoryUpdateSuggestion:
    """模型提出的**一条内容更新建议**。

    模型能提的只有建议：目标记录、目标版本、动作能不能落地，全部由程序裁决。
    ``expected_version`` 是**前提条件**而不是授权依据：对不上就重新取证，
    不直接覆盖。
    """

    action: str
    content: str = ""
    kind: str = ""
    content_status: str = ""
    source_types: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    target_id: str = ""
    expected_version: int | None = None
    #: 「标记冲突」要指出的另一方（也必须是本次已授权输入里的记录）。
    #: 只指一边的话程序无法核对是哪两条互相矛盾，只能拒收。
    conflict_ids: tuple[str, ...] = ()
    reason: str = ""
    horizon: str = ""
    topics: tuple[str, ...] = ()
    value: bool = True
    confidence: float | None = None
    importance: float | None = None
    origin: str = "model_candidate"
    review_receipt: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def writes_a_record(self) -> bool:
        """这条建议会不会落成一条记录（「保持或忽略」不会）。"""
        return self.action != "ignore"


def validate_suggestion(
    suggestion: MemoryUpdateSuggestion,
    *,
    known_ids: Iterable[str] | None = None,
    known_versions: Mapping[str, int] | None = None,
) -> None:
    """校验一条更新建议；不合规抛 :class:`MemoryCandidateError`。

    ``known_ids`` 是本次输入里**已授权**的记录编号：目标必须落在其中。
    模型指着一条输入里没有的编号说「把它改掉」，程序无从核对，也不该照办。

    ``known_versions`` 是这些编号在库里的当前版本。对不上说明输入取好
    之后这条记录已经被改过 —— 按重试处理，不覆盖别人的更新。
    """
    if suggestion.action not in MAINTENANCE_ACTIONS:
        raise MemoryCandidateError(f"未知维护动作 action={suggestion.action!r}")
    if not suggestion.value and suggestion.action != "ignore":
        # 「不值得保存」只对应「保持或忽略」：其它动作都要真的写东西。
        raise MemoryCandidateError("value=False 只能配 action=ignore")
    if suggestion.origin not in CANDIDATE_ORIGINS:
        raise MemoryCandidateError(f"未知建议来源 origin={suggestion.origin!r}")
    if suggestion.action in ACTIONS_REQUIRING_TARGET:
        if is_blank(suggestion.target_id):
            raise MemoryCandidateError(f"{suggestion.action} 必须给出目标记录编号")
        if known_ids is not None and suggestion.target_id not in set(known_ids):
            raise MemoryCandidateError(f"目标记录不在本次已授权输入里：{suggestion.target_id}")
    if suggestion.action == "conflict":
        others = [item for item in suggestion.conflict_ids if item and item != suggestion.target_id]
        if not others:
            raise MemoryCandidateError("标记冲突必须给出另一方记录编号 conflict_ids")
        if known_ids is not None:
            outside = [item for item in others if item not in set(known_ids)]
            if outside:
                raise MemoryCandidateError(
                    f"冲突另一方不在本次已授权输入里：{','.join(outside)}"
                )
    if suggestion.kind:
        if suggestion.kind not in CONTENT_KINDS:
            raise MemoryCandidateError(f"未知内容类型 kind={suggestion.kind!r}")
        if suggestion.content_status:
            allowed = CONTENT_STATUSES[suggestion.kind]
            if suggestion.content_status not in allowed:
                raise MemoryCandidateError(
                    f"{suggestion.kind} 不允许内容状态 {suggestion.content_status!r}，"
                    f"可选：{','.join(allowed)}"
                )
    if suggestion.horizon and suggestion.horizon not in CONTENT_HORIZONS:
        raise MemoryCandidateError(f"未知时间尺度 horizon={suggestion.horizon!r}")
    unknown_sources = [s for s in suggestion.source_types if s not in SOURCE_TYPES]
    if unknown_sources:
        raise MemoryCandidateError(f"未知来源类别：{','.join(unknown_sources)}")
    if suggestion.confidence is not None:
        value = float(suggestion.confidence)
        if not 0.0 <= value <= 1.0:
            raise MemoryCandidateError("confidence 必须在 0.0~1.0 之间")
    if suggestion.action != "ignore" and is_blank(suggestion.content) and is_blank(suggestion.reason):
        raise MemoryCandidateError("更新建议要么给新内容，要么给原因，不能两者都空")
    if (
        suggestion.expected_version is not None
        and known_versions is not None
        and suggestion.action in ACTIONS_REQUIRING_TARGET
    ):
        current = known_versions.get(suggestion.target_id)
        if current is None:
            raise MemoryCandidateError(f"目标版本无法核对：{suggestion.target_id}")
        if int(suggestion.expected_version) != int(current):
            raise MemoryCandidateError(
                f"目标版本已变（建议基于第 {suggestion.expected_version} 版，库里已是第 {current} 版），需重新取证"
            )
