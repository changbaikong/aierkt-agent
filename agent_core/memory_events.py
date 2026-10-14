"""业务结果的统一记忆入口（B 计划第五步）。

【解决什么问题】
后台任务、代码任务、管家播报原来都排除在自动提炼之外 —— 排除状态播报是对的，
但「这次到底产出了什么、测没测过、还差什么」没有另一条统一的写入路径，
于是主聊天下次要复用时什么都找不到。

【硬规矩】
- 进度播报不是完成结论，一条候选都不产生。
- 文件生成不等于测试通过：只有明确写了测试通过（或本就没有测试环节），
  才允许写「计划已完成」。
- 子代理口头自称成功算未核实来源，只能是 reported，不能是 verified。
- 失败要留下**具体条件**，不能只写「失败了」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from .memory_turn import EvidenceRef, MemoryCandidate, MemoryTurnPacket

#: 事件来源。进度类事件一律不产出候选。
PROGRESS_STATES = ("progress", "started", "running")
TERMINAL_STATES = ("completed", "failed", "cancelled")

#: 未核实来源：口头自称成功时用。
UNVERIFIED_SOURCES = ("assistant_suggestion",)


@dataclass(frozen=True)
class MemoryEvent:
    """一条业务结果事件。

    ``state`` 用 progress / completed / failed / cancelled；``tests_passed``
    为 ``None`` 表示**没有测试环节**（不是「测试通过」）。
    """

    event_id: str
    state: str
    goal: str = ""
    conclusion: str = ""
    artifacts: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    leftovers: tuple[str, ...] = ()
    failure_reason: str = ""
    failure_conditions: tuple[str, ...] = ()
    tests_passed: bool | None = None
    occurred_at: str = ""
    project_id: str = ""
    scope_kind: str = "conversation"
    claimed_by_subagent: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in (*PROGRESS_STATES, *TERMINAL_STATES):
            raise ValueError(f"未知事件状态 state={self.state!r}")
        if not str(self.event_id).strip():
            raise ValueError("业务结果事件必须带 event_id（幂等与去重都靠它）")


def candidates_from_event(event: MemoryEvent) -> list[MemoryCandidate]:
    """事件 → 候选列表。纯函数，不调模型、不碰数据库。

    一个事件可以同时产出一条以上的候选：产物、测试结果、剩余工作本来就是
    三件不同的事，合成一条会让「项目完成了吗」这种判断失去依据。
    """
    if event.state in PROGRESS_STATES:
        # 进度播报不是结论。写进去只会让下一次检索捞到一句「正在处理」。
        return []
    if event.state == "cancelled":
        return []

    candidates: list[MemoryCandidate] = []
    origin = "program"
    common = dict(
        occurred_at=event.occurred_at,
        project_id=event.project_id,
        scope_kind=event.scope_kind,
        source_refs=(event.event_id,),
        origin=origin,
    )

    if event.state == "failed":
        conditions = "；".join(event.failure_conditions) or "未记录触发条件"
        candidates.append(
            MemoryCandidate(
                content=f"任务未完成：{event.conclusion or event.goal or '未记录结论'}（条件：{conditions}）",
                kind="experience",
                content_status="observed",
                source_types=("tool_result",),
                reason=event.failure_reason,
                importance=0.8,
                **common,
            )
        )
        if event.leftovers:
            candidates.append(
                MemoryCandidate(
                    content="遗留待办：" + "；".join(event.leftovers),
                    kind="open_question",
                    content_status="open",
                    source_types=("tool_result",),
                    importance=0.7,
                    **common,
                )
            )
        return candidates

    # state == completed
    for artifact in event.artifacts:
        candidates.append(
            MemoryCandidate(
                content=f"已生成产物：{artifact}",
                kind="fact",
                # 有产物引用才是 verified；只说「做好了」而没有引用只能算 reported。
                content_status="verified" if event.evidence else "reported",
                source_types=("tool_result",) if event.evidence else ("assistant_suggestion",),
                importance=0.6,
                **common,
            )
        )
    if event.tests_passed is False:
        candidates.append(
            MemoryCandidate(
                content=(
                    "验证未通过："
                    + (event.conclusion or "产物已生成但测试失败")
                    + ("（条件：" + "；".join(event.failure_conditions) + "）" if event.failure_conditions else "")
                ),
                kind="experience",
                content_status="observed",
                reason=event.failure_reason,
                source_types=("tool_result",),
                importance=0.85,
                **common,
            )
        )
    if event.conclusion:
        # 「已完成」只在同时满足三条时成立：不是口头自称、有产物或证据可引用、
        # 且明确测试通过（或本就没有测试环节）。文件生成、后台执行成功都不等于目标达成。
        if event.claimed_by_subagent:
            candidates.append(
                MemoryCandidate(
                    content=f"自述结果（未核实）：{event.conclusion}",
                    kind="experience",
                    content_status="tentative",
                    source_types=UNVERIFIED_SOURCES,
                    importance=0.5,
                    **common,
                )
            )
        elif event.evidence and (event.tests_passed is True or event.tests_passed is None):
            candidates.append(
                MemoryCandidate(
                    content=f"完成结论：{event.conclusion}",
                    kind="plan",
                    content_status="completed",
                    source_types=("tool_result",),
                    importance=0.9,
                    **common,
                )
            )
        else:
            candidates.append(
                MemoryCandidate(
                    content=f"结果待核实：{event.conclusion}",
                    kind="experience",
                    content_status="observed",
                    source_types=("assistant_suggestion",),
                    importance=0.6,
                    **common,
                )
            )
    if event.leftovers:
        candidates.append(
            MemoryCandidate(
                content="遗留待办：" + "；".join(event.leftovers),
                kind="open_question",
                content_status="open",
                source_types=("tool_result",),
                importance=0.7,
                **common,
            )
        )
    return candidates


def event_from_task_result(payload: dict[str, Any], *, event_id: str = "") -> MemoryEvent:
    """从后台/代码任务的终态回执构造事件。

    只读这几个字段，缺的留空 —— 不猜、也不从回答文字里反推工具是否成功。
    """
    return MemoryEvent(
        event_id=event_id or str(payload.get("task_id") or payload.get("event_id") or ""),
        state=str(payload.get("status") or payload.get("state") or "completed"),
        goal=str(payload.get("goal") or payload.get("target") or ""),
        conclusion=str(payload.get("conclusion") or payload.get("summary") or ""),
        artifacts=tuple(str(item) for item in payload.get("artifacts") or ()),
        evidence=tuple(str(item) for item in payload.get("evidence") or ()),
        leftovers=tuple(str(item) for item in payload.get("leftovers") or ()),
        failure_reason=str(payload.get("failure_reason") or payload.get("error") or ""),
        failure_conditions=tuple(str(item) for item in payload.get("failure_conditions") or ()),
        tests_passed=payload.get("tests_passed"),
        occurred_at=str(payload.get("occurred_at") or ""),
        project_id=str(payload.get("project_id") or ""),
        claimed_by_subagent=bool(payload.get("claimed_by_subagent")),
    )


def record_event(
    writer: Any,
    scope: Any,
    event: MemoryEvent,
    *,
    known_ids: Iterable[str] | None = None,
) -> list[dict]:
    """把事件写进记忆库；进度类事件返回空列表。

    幂等：``turn_id`` 直接取事件号，同一条业务结果被反复推送也只落一份。
    """
    candidates = candidates_from_event(event)
    if not candidates:
        return []
    # This trusted application entry receives structured task results, not a
    # model-authored memory. Preserve the deterministic event rendering as tool
    # provenance; unverified assistant claims never become tool evidence.
    verified = [item for item in candidates if 'tool_result' in item.source_types
                and not event.claimed_by_subagent]
    packet = MemoryTurnPacket(
        turn_id=event.event_id,
        occurred_at=event.occurred_at or datetime.now(UTC).isoformat(),
        scope_owner=scope.owner_id, scope_channel=scope.channel,
        scope_conversation=scope.conversation_id, scope_module=scope.module_id,
        evidence_refs=[EvidenceRef(ref_id=event.event_id, outcome='completed', summary=item.content)
                       for item in verified],
    )
    return writer.save_semantic_candidates(
        scope,
        candidates,
        turn_id=event.event_id,
        occurred_at=event.occurred_at or None,
        known_ids=known_ids,
        packet=packet,
    )
