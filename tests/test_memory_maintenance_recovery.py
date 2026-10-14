"""内容维护的可靠性与来源保护：把 6 个新反例钉成回归用例。

这六条是独立复验里真实漏过的（BM-R1…BM-R6）：

- BM-R1 存储故障被当成业务拒绝，用户纠正没写进去也不会重试；
- BM-R2 更新建议交接入队失败只写日志，原作业照样完成，这次纠正永久丢失；
- BM-R3 旧版本已被替代后，迟到的同版本建议又能分叉出第二条「现行版本」；
- BM-R4 只要模型给个理由就能把长期原则归档；
- BM-R5 无关历史里的「同意」和助手引用被当成「用户已采纳」的证据；
- BM-R6 只要出现「不是」这类纠正词，就能改掉一条本轮完全没提过的记录。

脚本 ``scripts/verify_memory_maintenance_acceptance_edges.py`` 跑的是同一批判定；
这里补的是**恢复过程**：重试几次、调不调模型、最终是不是只生效一次。
"""
from __future__ import annotations

from support.memory_review import FixedReviewProvider
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from support.memory_review import supported_row
from agent_core.memory_processing import MemoryReviewProcessor
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_core.contracts import Scope
from agent_core.memory_extract import SemanticMemoryExtractor
from agent_core.memory_maintenance import MemoryMaintenanceScheduler, MemoryMaintenanceService
from agent_core.memory_queue import (
    MemoryCandidateQueue,
    MemoryCandidateWorker,
    MemoryMaintenanceWorker,
)
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import (
    HistoryItem,
    MemoryCandidate,
    MemoryRef,
    MemoryTurnPacket,
    MemoryUpdateSuggestion,
    turn_from_messages,
)
from agent_core.memory_writes import MemoryWriter


class _Embeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _scope(owner: str = "user") -> Scope:
    return Scope(owner, "web", "c1", "companion")


def _packet(turn_id: str, user_text: str, **overrides) -> MemoryTurnPacket:
    return turn_from_messages(
        turn_id=turn_id,
        occurred_at="2026-10-11T12:00:00+00:00",
        scope=_scope(),
        user_text=user_text,
        published_reply="好",
        **overrides,
    )


class _ExplicitReviewer:
    """Finite fixture judgments, independent of product bindings and check results."""
    calls = 0
    async def complete(self, messages, **kwargs):
        self.calls += 1
        data = json.loads(messages[-1]['content'])
        packet = data['packet']
        text, turn = packet['user_text'], packet['turn_id']
        rows = []
        for entry in data['items']:
            item, index = entry['candidate'], entry['index']
            content = item['content']
            allowed = {
                '不是五千，是三千': ('预算三千', '项目采购预算三千'),
                '不是五千，是四千': ('项目采购预算四千',),
                '项目采购预算不是五千，是三千': ('项目采购预算三千',),
                '项目采购预算不是五千，是四千': ('项目采购预算四千',),
                '设备采购预算不是五千，是三千': ('设备采购预算三千',),
                '预算不是五千，是三千；新候选：turn-off': ('预算三千', '新候选：turn-off'),
                '预算不是五千，是三千；新候选：turn-on': ('预算三千', '新候选：turn-on'),
            }
            if content in allowed.get(text, ()):
                if content.startswith('新候选'):
                    quotes = [(turn, content)]
                else:
                    prefix = text.split('，')[0]
                    quotes = [(turn, prefix), (turn, '是三千' if content.endswith('三千') else '是四千')]
                rows.append(supported_row(content, quotes, index=index))
            else:
                rows.append(dict(index=index, verdict='unsupported', reason='独立预期：该原话没有授权此变更',
                                 subject='预算', same_subject=False, preserves_negation=True,
                                 preserves_conditions=True, change_authorized=False))
        return SimpleNamespace(message={'content':json.dumps({'items':rows}, ensure_ascii=False)})


def _prepare_explicit(writer, packet, suggestion):
    _, prepared, _ = asyncio.run(MemoryReviewProcessor(writer, _ExplicitReviewer()).prepare(
        packet, [], [suggestion], persist=lambda _: None))
    return prepared[0] if prepared else suggestion


def _reviewed_apply(service, suggestion, **kwargs):
    suggestion = _prepare_explicit(service.writer, kwargs['packet'], suggestion)
    return service.apply(suggestion, **kwargs)


def seed_semantic(writer, scope, candidates, **kwargs):
    candidates = list(candidates)
    allowed = {'预算五千', '设备采购预算五千', '项目采购预算五千', '项目采购预算四千',
               '项目长期原则是本地优先', '旧方案到期作废', '服务器机房在广州',
               '会议日期是周一', '预算是五千', '预算是三千'}
    assert all(c.content in allowed for c in candidates)
    turn = kwargs['turn_id']
    packet = turn_from_messages(turn_id=turn, occurred_at=kwargs['occurred_at'], scope=scope,
                                user_text='。'.join(c.content for c in candidates), published_reply='',
                                project_binding=kwargs.get('project_binding', ''))
    rows = [supported_row(c.content, [(turn, c.content)], index=i, change=False)
            for i, c in enumerate(candidates)]
    prepared, _, result = asyncio.run(MemoryReviewProcessor(writer, FixedReviewProvider(rows)).prepare(
        packet, candidates, persist=lambda _: None))
    assert prepared and not result.pending_review
    return writer.save_semantic_candidates(scope, prepared, packet=packet, **kwargs)


def _stack(tmp_path):
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    writer = MemoryWriter(repos, _Embeddings())
    queue = MemoryCandidateQueue(tmp_path / "jobs.db")
    queue.initialize()
    service = MemoryMaintenanceService(writer, repos)
    return dict(
        repos=repos,
        writer=writer,
        queue=queue,
        service=service,
        maintenance=MemoryMaintenanceWorker(queue, service, review_provider=_ExplicitReviewer()),
    )


def _seed(stack, content: str, *, turn: str = "turn-0") -> dict:
    return seed_semantic(stack["writer"],
        _scope(),
        [
            MemoryCandidate(
                content=content,
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(turn,),
                occurred_at="2026-10-11T10:00:00+00:00",
            )
        ],
        turn_id=turn,
        occurred_at="2026-10-11T10:00:00+00:00",
    )[0]


def _correction(target: dict, content: str, *, turn: str = "turn-1") -> MemoryUpdateSuggestion:
    return MemoryUpdateSuggestion(
        action="correct",
        target_id=target["id"],
        expected_version=int(target["record_version"]),
        content=content,
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=(turn,),
        reason="用户纠正了这个数字",
    )


def _active_contents(stack) -> list[str]:
    rows = stack["writer"].list_records(_scope())
    return [row["content"] for row in rows if row["status"] == "active"]


# ---------------------------------------------------------------------------
# BM-R1：存储故障必须可恢复，恢复后只应用一次
# ---------------------------------------------------------------------------
def test_a_storage_outage_keeps_the_job_retryable_and_applies_exactly_once(tmp_path) -> None:
    stack = _stack(tmp_path)
    old = _seed(stack, "预算五千")
    job_id, _ = stack["queue"].enqueue_maintenance(
        _packet("turn-1", "不是五千，是三千"), [_correction(old, "预算三千")]
    )

    with patch.object(
        stack["writer"],
        "apply_semantic_update",
        side_effect=sqlite3.OperationalError("simulated storage outage"),
    ):
        result = asyncio.run(stack["maintenance"].process_once())

    job = stack["queue"].get(job_id)
    assert result["failed"] == 1, result
    assert job["status"] == "failed"          # 不是 completed
    assert job["attempts"] == 1                # 有界重试计数
    assert "OperationalError" in (job["last_error"] or "")
    # 建议还在作业里，恢复后直接复用，不用重新调模型。
    assert [item.target_id for item in stack["queue"].stored_suggestions(job)] == [old["id"]]
    assert _active_contents(stack) == ["预算五千"]   # 没写成就是没写成

    later = datetime.now(UTC) + timedelta(minutes=5)
    retry = asyncio.run(stack["maintenance"].process_once(now=later))
    assert retry["applied"] == 1, retry
    assert stack["queue"].get(job_id)["status"] == "completed"
    assert _active_contents(stack) == ["预算三千"]   # 只生效一次

    again = asyncio.run(stack["maintenance"].process_once(now=later + timedelta(minutes=5)))
    assert again["claimed"] == 0                      # 完成之后不再被认领
    assert _active_contents(stack) == ["预算三千"]
    stack["repos"].close()


def test_a_refusal_is_completed_not_retried(tmp_path) -> None:
    """证据不足是**拒绝**：重试多少次都一样，作业应当结束并留原因。"""
    stack = _stack(tmp_path)
    old = _seed(stack, "设备采购预算五千")
    suggestion = _correction(old, "设备采购预算三千")
    packet = _packet("turn-1", "不是周一，是周二开会；设备采购预算还没变化")
    job_id, _ = stack["queue"].enqueue_maintenance(packet, [suggestion])

    result = asyncio.run(stack["maintenance"].process_once())

    job = stack["queue"].get(job_id)
    assert result["refused"] == 1, result
    assert job["status"] == "refused"
    assert job["attempts"] == 0
    assert _active_contents(stack) == ["设备采购预算五千"]
    stack["repos"].close()


# ---------------------------------------------------------------------------
# BM-R2：提炼结果先落库；交接失败可恢复且不重复调模型
# ---------------------------------------------------------------------------
def test_a_failed_handoff_keeps_the_suggestion_and_never_repeats_the_model_call(tmp_path) -> None:
    class _Extractor:
        def __init__(self, suggestions) -> None:
            self.suggestions = list(suggestions)
            self.calls = 0
            self.provider = _ExplicitReviewer()

        async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
            self.calls += 1
            return [], list(self.suggestions)

    stack = _stack(tmp_path)
    old = _seed(stack, "预算五千")
    extractor = _Extractor([_correction(old, "预算三千")])
    packet = _packet("turn-1", "不是五千，是三千")
    job_id, _ = stack["queue"].enqueue(packet)
    worker = MemoryCandidateWorker(
        stack["queue"], extractor, stack["writer"], maintenance_queue=stack["queue"]
    )

    with patch.object(
        stack["queue"],
        "enqueue_maintenance",
        side_effect=sqlite3.OperationalError("simulated queue outage"),
    ):
        result = asyncio.run(worker.process_once())

    job = stack["queue"].get(job_id)
    assert result["failed"] == 1, result
    assert job["status"] == "failed" and job["attempts"] == 1
    assert "handoff" in (job["last_error"] or "")
    # 提炼结果已经落库：候选（空）与建议都在，重试不用再调模型。
    assert extractor.calls == 1
    assert [item.target_id for item in stack["queue"].stored_suggestions(job)] == [old["id"]]
    assert stack["queue"].kind_stats()["maintenance"]["pending"] == 0   # 一条都没交接出去

    later = datetime.now(UTC) + timedelta(minutes=5)
    retry = asyncio.run(worker.process_once(now=later))

    assert retry["empty"] == 1, retry                     # 候选本来就是空的
    assert extractor.calls == 1                           # 没有重复调模型
    assert stack["queue"].get(job_id)["status"] == "completed"
    assert stack["queue"].kind_stats()["maintenance"]["pending"] == 1   # 建议进了维护队列
    stack["repos"].close()


# ---------------------------------------------------------------------------
# BM-R3：写入事务里的版本保护
# ---------------------------------------------------------------------------
def test_a_late_suggestion_based_on_a_replaced_version_cannot_fork(tmp_path) -> None:
    stack = _stack(tmp_path)
    old = _seed(stack, "项目采购预算五千")
    late_packet = _packet('turn-2', '不是五千，是四千')
    late = _prepare_explicit(stack['writer'], late_packet, _correction(old, '项目采购预算四千', turn='turn-2'))
    first = _reviewed_apply(stack["service"],
        _correction(old, "项目采购预算三千", turn="turn-1"),
        scope=_scope(),
        packet=_packet("turn-1", "不是五千，是三千"),
        turn_id="turn-1",
    )
    assert first["applied"] is True, first["reason"]

    second = stack['service'].apply(
        late,
        scope=_scope(),
        packet=late_packet,
        turn_id="turn-2",
    )

    assert second["applied"] is False
    assert "替代" in second["reason"] or "版本" in second["reason"]
    assert _active_contents(stack) == ["项目采购预算三千"]     # 只有一条现行版本
    stack["repos"].close()


def test_the_same_change_is_still_replayable_after_the_target_was_replaced(tmp_path) -> None:
    """同一变更的重传／重启恢复必须照旧生效 —— 版本保护不能把幂等重放也挡掉。"""
    stack = _stack(tmp_path)
    old = _seed(stack, "预算五千")
    suggestion = _correction(old, "预算三千")
    packet = _packet("turn-1", "不是五千，是三千")
    first = _reviewed_apply(stack["service"],
        suggestion, scope=_scope(), packet=packet, turn_id="turn-1"
    )
    replay = _reviewed_apply(stack["service"],
        suggestion, scope=_scope(), packet=packet, turn_id="turn-1"
    )

    assert first["applied"] is True and replay["applied"] is True
    assert first["record"]["id"] == replay["record"]["id"]
    assert _active_contents(stack) == ["预算三千"]
    stack["repos"].close()


def test_two_concurrent_corrections_of_the_same_version_cannot_fork(tmp_path) -> None:
    """两个回合同时纠正同一条旧记录：只允许一个成为现行版本。

    版本保护写在**写入事务内**，所以并发时后到的那个会在事务里发现目标已经不是
    现行版本，只得拒绝；如果只在事务外读一次快照，两次纠正会各起一行，
    同时出现两个「现行版本」。
    """
    stack = _stack(tmp_path)
    old = _seed(stack, "项目采购预算五千")
    scope = _scope()
    outcomes: dict[str, dict] = {}
    gate = threading.Barrier(2)

    def apply(turn: str, content: str) -> None:
        packet = _packet(turn, f"项目采购预算不是五千，是{content[-2:]}")
        suggestion = _prepare_explicit(stack['writer'], packet, _correction(old, content, turn=turn))
        gate.wait(timeout=30)
        outcomes[turn] = stack['service'].apply(
            suggestion, scope=scope, packet=packet, turn_id=turn
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        for turn, content in (("turn-a", "项目采购预算三千"), ("turn-b", "项目采购预算四千")):
            pool.submit(apply, turn, content)
        pool.shutdown(wait=True)

    active = _active_contents(stack)
    applied = [turn for turn, outcome in outcomes.items() if outcome["applied"]]
    assert len(active) == 1, active
    assert len(applied) == 1, outcomes          # 至多一个成为现行版本
    for turn, outcome in outcomes.items():
        if not outcome["applied"]:
            # 另一个必须给出可核对的原因，不能静默丢掉
            assert outcome["reason"], (turn, outcome)
    stack["repos"].close()


# ---------------------------------------------------------------------------
# BM-R4：模型自拟理由不能归档长期原则
# ---------------------------------------------------------------------------
def test_a_model_reason_alone_marks_for_review_instead_of_archiving(tmp_path) -> None:
    stack = _stack(tmp_path)
    old = _seed(stack, "项目长期原则是本地优先")
    packet = _packet("turn-1", "今天天气不错")
    outcome = _reviewed_apply(stack["service"],
        MemoryUpdateSuggestion(
            action="invalidate",
            target_id=old["id"],
            expected_version=1,
            reason="模型认为这条原则太旧了",
            source_types=("model_inference",),
            source_refs=(packet.turn_id,),
        ),
        scope=_scope(),
        packet=packet,
        turn_id="turn-1",
    )

    fresh = stack["repos"].records_by_ids([old["id"]])[old["id"]]
    assert fresh["status"] == "active"                 # 有效状态没被动
    assert fresh["review_state"] == "pending"          # 只排了一次复核
    gap = json.loads(fresh["metadata"])["review_gap"]
    assert gap["missing"]                              # 缺哪条证据写清楚了
    assert gap["how_to_verify"]                        # 下次从哪里取证也写清楚了
    assert outcome["applied"] is True and outcome["review_only"] is True
    stack["repos"].close()


def test_user_words_bound_to_the_record_can_still_invalidate_it(tmp_path) -> None:
    stack = _stack(tmp_path)
    old = _seed(stack, "旧方案到期作废")
    packet = _packet("turn-1", "那个方案不用了")
    outcome = _reviewed_apply(stack["service"],
        MemoryUpdateSuggestion(
            action="invalidate",
            target_id=old["id"],
            expected_version=1,
            reason="用户明确不再用",
            source_types=("user_statement",),
            source_refs=(packet.turn_id,),
        ),
        scope=_scope(),
        packet=packet,
        turn_id="turn-1",
    )

    fresh = stack["repos"].records_by_ids([old["id"]])[old["id"]]
    assert outcome["applied"] is True and fresh["status"] == "archived"
    stack["repos"].close()


# ---------------------------------------------------------------------------
# BM-R5：采纳证据绑定被引用的事项与本轮原话
# ---------------------------------------------------------------------------
def test_an_unrelated_historical_agreement_is_not_adoption_evidence(tmp_path) -> None:
    packet = MemoryTurnPacket(
        turn_id="turn-1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner="user",
        scope_channel="web",
        scope_conversation="c1",
        user_text="部署方案还未决定",
        published_reply="好，先不定",
        relevant_history=[
            HistoryItem("style-choice", "user", "同意以后回答先说结论"),
            HistoryItem("cloud-proposal", "assistant", "建议把服务部署到云端"),
        ],
    )
    output = json.dumps(
        {
            "memories": [
                {
                    "content": "用户已采纳云端部署方案",
                    "kind": "decision",
                    "content_status": "accepted",
                    "source_types": ["user_statement"],
                    "source_refs": ["cloud-proposal"],
                }
            ]
        },
        ensure_ascii=False,
    )
    candidates, _ = SemanticMemoryExtractor(None).parse(output, packet=packet)

    assert len(candidates) == 1
    assert candidates[0].content_status == 'accepted'  # 解析只保留模型产物，最终采纳由复核判断。
    class RejectAdoption:
        async def complete(self, messages, **kwargs):
            return SimpleNamespace(message={'content':json.dumps({'items':[dict(index=0, verdict='unsupported',
                reason='用户只同意回答方式，云部署未决定', subject='云部署', same_subject=False,
                preserves_negation=True, preserves_conditions=True, change_authorized=False)]})})
    # 写入入口必须拒绝：解析后的状态不能充当采纳凭据。
    stack = _stack(tmp_path)
    prepared, _, result = asyncio.run(MemoryReviewProcessor(stack['writer'], RejectAdoption()).prepare(
        packet, candidates, persist=lambda _: None))
    assert not prepared and result.rejected
    stack['repos'].close()


def test_a_historical_agreement_about_the_same_item_still_counts(tmp_path) -> None:
    packet = MemoryTurnPacket(
        turn_id="turn-1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner="user",
        scope_channel="web",
        scope_conversation="c1",
        user_text="接着上次说的做吧",
        published_reply="好",
        relevant_history=[
            HistoryItem("pick", "user", "云部署方案我同意，就按这个来"),
        ],
    )
    output = json.dumps(
        {
            "memories": [
                {
                    "content": "用户已采纳云端部署方案",
                    "kind": "decision",
                    "content_status": "accepted",
                    "source_types": ["user_statement"],
                    "source_refs": ["pick"],
                }
            ]
        },
        ensure_ascii=False,
    )
    candidates, _ = SemanticMemoryExtractor(None).parse(output, packet=packet)

    assert candidates[0].content_status == "accepted"
    provider = FixedReviewProvider([supported_row('用户已采纳云端部署方案',
        [('pick', '云部署方案我同意'), ('pick', '就按这个来')], subject='云部署方案', change=False)])
    stack = _stack(tmp_path)
    prepared, _, result = asyncio.run(MemoryReviewProcessor(stack['writer'], provider).prepare(
        packet, candidates, persist=lambda _: None))
    assert provider.calls == 1 and prepared and not result.pending_review
    stack['repos'].close()


# ---------------------------------------------------------------------------
# BM-R6：纠正必须对应目标事项，并且原话里有新值
# ---------------------------------------------------------------------------
def test_an_unrelated_correction_word_cannot_rewrite_the_record(tmp_path) -> None:
    stack = _stack(tmp_path)
    old = _seed(stack, "设备采购预算五千")
    packet = _packet("turn-1", "不是周一，是周二开会；设备采购预算还没变化")
    outcome = _reviewed_apply(stack["service"],
        _correction(old, "设备采购预算三千"),
        scope=_scope(),
        packet=packet,
        turn_id="turn-1",
    )

    assert outcome["applied"] is False
    assert "新值缺少原话依据" in outcome["reason"]
    assert _active_contents(stack) == ["设备采购预算五千"]
    stack["repos"].close()


def test_a_correction_naming_the_new_value_in_the_users_words_is_applied(tmp_path) -> None:
    stack = _stack(tmp_path)
    old = _seed(stack, "设备采购预算五千")
    outcome = _reviewed_apply(stack["service"],
        _correction(old, "设备采购预算三千"),
        scope=_scope(),
        packet=_packet("turn-1", "设备采购预算不是五千，是三千"),
        turn_id="turn-1",
    )

    assert outcome["applied"] is True, outcome["reason"]
    assert _active_contents(stack) == ["设备采购预算三千"]
    stack["repos"].close()


def test_a_correction_about_a_record_the_user_never_mentioned_is_refused(tmp_path) -> None:
    """纠正词对上了、新内容也在原话里，但说的是**另一件事**：同样不许覆盖。"""
    stack = _stack(tmp_path)
    old = _seed(stack, "服务器机房在广州")
    outcome = _reviewed_apply(stack["service"],
        _correction(old, "会议日期是周三"),
        scope=_scope(),
        packet=_packet("turn-1", "会议不是周一，是周三"),
        turn_id="turn-1",
    )

    assert outcome["applied"] is False
    assert "目标事项" in outcome["reason"]
    assert _active_contents(stack) == ["服务器机房在广州"]
    stack["repos"].close()


def test_memory_refs_do_not_let_the_model_invent_the_value(tmp_path) -> None:
    """引用真实存在也不够：原话里没有的新值，引用到目标也不能当依据。"""
    stack = _stack(tmp_path)
    old = _seed(stack, "会议日期是周一")
    packet = MemoryTurnPacket(
        turn_id="turn-1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner="user",
        scope_channel="web",
        scope_conversation="c1",
        user_text="会议日期的事我再确认一下",
        published_reply="好",
        recalled_memory_refs=[
            MemoryRef(old["id"], "fact", "reported", old["content"], version=1)
        ],
    )
    outcome = _reviewed_apply(stack["service"],
        _correction(old, "会议日期是周三"),
        scope=_scope(),
        packet=packet,
        turn_id="turn-1",
    )

    assert outcome["applied"] is False
    assert _active_contents(stack) == ["会议日期是周一"]
    stack["repos"].close()


# ---------------------------------------------------------------------------
# M7：两个项目各有一条预算 —— 纠正与冲突都不许跨项目
# ---------------------------------------------------------------------------
def _seed_project(stack, content: str, project: str, *, turn: str) -> dict:
    return seed_semantic(stack["writer"],
        _scope(),
        [
            MemoryCandidate(
                content=content,
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=(turn,),
                scope_kind="project",
                project_id=project,
                occurred_at="2026-10-11T10:00:00+00:00",
            )
        ],
        turn_id=turn,
        occurred_at="2026-10-11T10:00:00+00:00",
        project_binding=project,
    )[0]


def test_a_correction_cannot_cross_project_boundaries(tmp_path) -> None:
    """M7：绑定在项目 A 的记录，项目 B 的回合改不动它。"""
    stack = _stack(tmp_path)
    record_a = _seed_project(stack, "项目采购预算五千", "project-a", turn="turn-a0")
    packet_b = turn_from_messages(
        turn_id="turn-b1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope=_scope(),
        user_text="项目采购预算不是五千，是三千",
        published_reply="好",
        project_binding="project-b",
    )

    crossed = _reviewed_apply(stack["service"],
        _correction(record_a, "项目采购预算三千", turn="turn-b1"),
        scope=_scope(),
        packet=packet_b,
        turn_id="turn-b1",
        project_id="project-b",
    )

    assert crossed["applied"] is False
    assert "不可见" in crossed["reason"]
    assert _active_contents(stack) == ["项目采购预算五千"]

    # 同一个项目里照常能改：拒绝的是范围，不是这个动作。
    packet_a = turn_from_messages(
        turn_id="turn-a1",
        occurred_at="2026-10-11T12:00:00+00:00",
        scope=_scope(),
        user_text="项目采购预算不是五千，是三千",
        published_reply="好",
        project_binding="project-a",
    )
    applied = _reviewed_apply(stack["service"],
        _correction(record_a, "项目采购预算三千", turn="turn-a1"),
        scope=_scope(),
        packet=packet_a,
        turn_id="turn-a1",
        project_id="project-a",
    )
    assert applied["applied"] is True, applied["reason"]
    stack["repos"].close()


def test_a_conflict_cannot_be_marked_across_projects(tmp_path) -> None:
    """M7：另一方的记录不在本次授权输入里 —— 冲突也标不上，只能留着待核对。"""
    stack = _stack(tmp_path)
    mine = _seed_project(stack, "项目采购预算五千", "project-a", turn="turn-a0")
    theirs = _seed_project(stack, "项目采购预算四千", "project-b", turn="turn-b0")
    outcome = _reviewed_apply(stack["service"],
        MemoryUpdateSuggestion(
            action="conflict",
            target_id=mine["id"],
            expected_version=int(mine["record_version"]),
            conflict_ids=(theirs["id"],),
            reason="两条说法对不上",
            source_types=("user_statement",),
            source_refs=("turn-a1",),
        ),
        scope=_scope(),
        packet=_packet("turn-a1", "不记得了", project_binding="project-a"),
        turn_id="turn-a1",
        project_id="project-a",
    )

    assert outcome["applied"] is False
    fresh = stack["repos"].records_by_ids([mine["id"], theirs["id"]])
    assert fresh[mine["id"]]["review_state"] == "none"      # 没被标成待复核
    assert fresh[theirs["id"]]["review_state"] == "none"
    assert all(row["status"] == "active" for row in fresh.values())
    stack["repos"].close()


# ---------------------------------------------------------------------------
# M11 + 长跑：定期任务反复跑，不自我循环、不新增版本、不留积压
# ---------------------------------------------------------------------------
def test_repeated_review_cycles_never_self_loop_or_grow_a_backlog(tmp_path) -> None:
    """到点但没有新证据：跑多少轮都只推复核时间，模型调用为零、版本不变。"""
    stack = _stack(tmp_path)
    first = _seed(stack, "预算是五千")
    second = _seed(stack, "预算是三千", turn="turn-0b")
    _reviewed_apply(stack["service"],
        MemoryUpdateSuggestion(
            action="conflict",
            target_id=first["id"],
            expected_version=int(first["record_version"]),
            conflict_ids=(second["id"],),
            reason="两条说法对不上",
            source_types=("user_statement",),
            source_refs=("turn-1",),
        ),
        scope=_scope(),
        packet=_packet("turn-1", "不记得了"),
        turn_id="turn-1",
    )
    before = {
        row["id"]: (row["record_version"], row["content"], row["status"])
        for row in stack["writer"].list_records(_scope())
    }
    scheduler = MemoryMaintenanceScheduler(stack["service"], interval_seconds=0, batch_size=20)

    moment = datetime.now(UTC)
    totals = {"due": 0, "model_calls": 0}
    for cycle in range(6):
        report = scheduler.run_once(now=moment + timedelta(days=2 * (cycle + 1)))
        totals["due"] += report["due"]
        totals["model_calls"] += report["model_calls"]
        assert report["deferred"] == report["due"]

    assert totals["due"] == 12          # 2 条 × 6 轮，每轮都到点
    assert totals["model_calls"] == 0   # 没有新证据就不调模型
    after = {
        row["id"]: (row["record_version"], row["content"], row["status"])
        for row in stack["writer"].list_records(_scope())
    }
    assert after == before              # 版本、正文、状态一个都没动
    assert stack["queue"].backlog() == 0
    assert sum(stack["queue"].kind_stats()["maintenance"].values()) == 0
    stack["repos"].close()


def test_a_replayed_suggestion_stays_one_version_over_many_retries(tmp_path) -> None:
    """同一份建议反复重放（重传、重启、多次重试）也只留一个版本。"""
    stack = _stack(tmp_path)
    old = _seed(stack, "预算五千")
    packet = _packet("turn-1", "不是五千，是三千")
    suggestion = _correction(old, "预算三千")
    job_id, _ = stack["queue"].enqueue_maintenance(packet, [suggestion])

    for _ in range(3):
        outcome = asyncio.run(stack["maintenance"].process_once())
        assert outcome["claimed"] in (0, 1)

    assert stack["queue"].get(job_id)["status"] == "completed"
    assert _active_contents(stack) == ["预算三千"]
    stack["repos"].close()


# ---------------------------------------------------------------------------
# 跨进程并发：网页进程与后台工人进程同时改同一条记录
# ---------------------------------------------------------------------------
#: 两个独立进程各自跑一遍真实维护服务；谁先写进事务谁说了算。
#: 向量目录按进程分开：Qdrant 本地模式不允许两个进程同时打开同一个目录
#: （生产用的是 Qdrant 服务端，这里测的是**记录库事务**这一层）。
CROSS_PROCESS_WORKER = """
import json, sys
sys.path.insert(0, sys.argv[7])
from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceService
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryUpdateSuggestion, turn_from_messages, decode_suggestions
from agent_core.memory_writes import MemoryWriter

db, vectors, target_id, version, content, turn_id, root = sys.argv[1:8]


class Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


repos = initialize_memory_repositories(db, vectors, vector_size=3)
writer = MemoryWriter(repos, Embeddings())
service = MemoryMaintenanceService(writer, repos)
scope = Scope("user", "web", "c1", "companion")
packet = turn_from_messages(
    turn_id=turn_id,
    occurred_at="2026-10-11T12:00:00+00:00",
    scope=scope,
    user_text="项目采购预算不是五千，是" + content[-2:],
    published_reply="好",
)
outcome = service.apply(
    decode_suggestions(sys.argv[8])[0],
    scope=scope,
    packet=packet,
    turn_id=turn_id,
)
print(json.dumps({"applied": outcome["applied"], "reason": outcome["reason"], "failure": bool(outcome.get("failure"))}))
repos.close()
"""


def test_two_processes_correcting_the_same_record_cannot_fork(tmp_path) -> None:
    """跨进程（网页进程 + 后台工人进程）同时纠正：只允许一个现行版本。

    生产上网页进程和记忆工人是**两个进程**，共用同一个记录库：版本保护必须
    在这层也成立，而不是只在同一进程的两个线程之间成立。
    """
    import subprocess
    import sys

    stack = _stack(tmp_path)
    old = _seed(stack, "项目采购预算五千")
    database = str(stack["repos"].db_path)
    root = str(Path(__file__).resolve().parents[1])
    from agent_core.memory_turn import serialize_suggestions
    signed = {}
    for turn, content in [('turn-proc-a', '项目采购预算三千'), ('turn-proc-b', '项目采购预算四千')]:
        packet = _packet(turn, f'项目采购预算不是五千，是{content[-2:]}')
        signed[turn] = serialize_suggestions([_prepare_explicit(stack['writer'], packet, _correction(old, content, turn=turn))])

    def spawn(turn: str, content: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                CROSS_PROCESS_WORKER,
                database,
                f"{database}-vectors-{turn}",
                old["id"],
                str(old["record_version"]),
                content,
                turn,
                root,
                signed[turn],
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )

    first = spawn("turn-proc-a", "项目采购预算三千")
    second = spawn("turn-proc-b", "项目采购预算四千")
    outcomes = []
    for process in (first, second):
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr[-800:]
        outcomes.append(json.loads(stdout.strip().splitlines()[-1]))

    active = _active_contents(stack)
    assert len(active) == 1, active
    assert sum(1 for item in outcomes if item["applied"]) == 1, outcomes
    for item in outcomes:
        if not item["applied"]:
            # 另一个要么被明确拒绝，要么是可重试的执行失败；两种都留了原因。
            assert item["reason"], item
    stack["repos"].close()


# ---------------------------------------------------------------------------
# 独立开关：关掉内容维护只停新任务，既不应用建议也不回退既有版本
# ---------------------------------------------------------------------------
class _CandidateAndSuggestionExtractor:
    """一次提炼同时产出候选和更新建议，用来验证开关关掉后各走哪条路。"""

    def __init__(self, suggestion) -> None:
        self.suggestion = suggestion
        self.calls = 0
        self.provider = _ExplicitReviewer()

    async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
        self.calls += 1
        return (
            [
                MemoryCandidate(
                    content=f"新候选：{packet.turn_id}",
                    kind="fact",
                    content_status="reported",
                    source_types=("user_statement",),
                    source_refs=(packet.turn_id,),
                )
            ],
            [self.suggestion],
        )


def test_switching_maintenance_off_stops_new_updates_without_rolling_back(tmp_path) -> None:
    """`MEMORY_CONTENT_MAINTENANCE_ENABLED=false` 时网页就是这么装配的：不给维护出口。

    候选照常落库；更新建议**不应用**（既有版本不倒退），也不新开维护任务。
    开关重新打开后，新的回合照常能改。
    """
    stack = _stack(tmp_path)
    old = _seed(stack, "预算五千")

    off_packet = _packet("turn-off", "预算不是五千，是三千；新候选：turn-off")
    off_extractor = _CandidateAndSuggestionExtractor(
        _correction(old, "预算三千", turn="turn-off")
    )
    off_worker = MemoryCandidateWorker(
        stack["queue"], off_extractor, stack["writer"], maintenance_queue=None
    )
    stack["queue"].enqueue(off_packet)
    result = asyncio.run(off_worker.process_once())

    assert result["stored"] == 1
    # 没有新维护任务（kind_stats 只按状态计数，全部为 0 就是没有）
    assert sum(stack["queue"].kind_stats()["maintenance"].values()) == 0
    assert sorted(_active_contents(stack)) == ["新候选：turn-off", "预算五千"]

    on_packet = _packet("turn-on", "预算不是五千，是三千；新候选：turn-on")
    on_extractor = _CandidateAndSuggestionExtractor(
        _correction(old, "预算三千", turn="turn-on")
    )
    on_worker = MemoryCandidateWorker(
        stack["queue"], on_extractor, stack["writer"], maintenance_queue=stack["queue"]
    )
    stack["queue"].enqueue(on_packet)
    asyncio.run(on_worker.process_once())
    assert stack["queue"].kind_stats()["maintenance"]["pending"] == 1

    # 还没执行时关掉维护：作业要原样留着（建议在、尝试数 0），不能被清掉。
    with stack["queue"].connect() as db:
        row = db.execute(
            "SELECT job_id FROM memory_candidate_jobs WHERE kind='maintenance' "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    pending_job = stack["queue"].get(str(row["job_id"]))
    assert pending_job["status"] == "pending" and int(pending_job["attempts"]) == 0
    assert len(stack["queue"].stored_suggestions(pending_job)) == 1

    applied = asyncio.run(stack["maintenance"].process_once())
    assert applied["applied"] == 1, applied
    assert "预算三千" in _active_contents(stack)
    stack["repos"].close()


def test_maintenance_logs_keep_references_not_the_whole_conversation(tmp_path) -> None:
    """运行日志只留引用与计数：整段用户原话不进日志。"""
    import logging

    secret = "这是一段很长很长的私人对话正文，用来检查日志里不会出现它的全文"
    stack = _stack(tmp_path)
    old = _seed(stack, "设备采购预算五千")
    packet = _packet("turn-log", f"不是周一，是周二开会；设备采购预算还没变化。{secret}")
    stack["queue"].enqueue_maintenance(packet, [_correction(old, "设备采购预算三千", turn="turn-log")])

    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record) -> None:
            captured.append(record.getMessage())

    logger = logging.getLogger("agent_core.memory")
    handler = _Capture()
    logger.addHandler(handler)
    try:
        outcome = asyncio.run(stack["maintenance"].process_once())
    finally:
        logger.removeHandler(handler)

    assert outcome["refused"] == 1            # 这条建议本来就会被拒（证据不足）
    text = "\n".join(captured)
    assert captured, "拒绝路径应当留下日志，否则这条断言没有意义"
    assert secret not in text                 # 正文不进日志
    assert len(text) < 400                    # 留下的是原因与计数，不是整段对话
    stack["repos"].close()


# ---------------------------------------------------------------------------
# M12：短期状态与长期事实要分得开；任务播报不做成长久事实
# ---------------------------------------------------------------------------
def test_temporal_scale_survives_the_queue_and_broadcasts_stay_out_of_memory() -> None:
    from agent_core.memory_extract import NON_MEMORY_KINDS
    from agent_core.memory_turn import (
        CONTENT_HORIZONS,
        MemoryCandidateError,
        decode_suggestions,
        serialize_suggestions,
        validate_suggestion,
    )
    from agent_core.steward import STEWARD_PROGRESS_KIND
    from agent_core.task_result_bridge import TASK_RESULT_KIND
    from agent_core.work_bridge import WORK_RESULT_KIND

    # 后台任务的终态播报不是用户说过的事实：三类都不许进长期记忆提炼。
    assert {TASK_RESULT_KIND, WORK_RESULT_KIND, STEWARD_PROGRESS_KIND} <= NON_MEMORY_KINDS
    assert set(CONTENT_HORIZONS) == {"long_term", "short_term"}

    short = MemoryUpdateSuggestion(
        action="add",
        content="用户今天有点累",
        kind="experience",
        content_status="observed",
        source_types=("user_statement",),
        horizon="short_term",
    )
    validate_suggestion(short)
    # 尺度要在队列里原样带下去：解码后变成空串，短期与长期就分不开了。
    stored = decode_suggestions(serialize_suggestions([short]))
    assert stored[0].horizon == "short_term"

    with pytest.raises(MemoryCandidateError):
        validate_suggestion(replace(short, horizon="forever"))
