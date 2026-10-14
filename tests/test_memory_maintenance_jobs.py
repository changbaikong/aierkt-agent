"""内容维护作业：入队、认领、应用、重启恢复（B 计划第四步）。

钉五件事：
1. 一次提炼同时产出候选与更新建议，建议走**同一个队列的另一个类别**；
2. 正文发布不等后台处理（入队是本地事务，模型调用发生在工人里）；
3. 同一个回合不会双端重复入队；
4. 租约过期后能被重新认领（重启与超时恢复）；
5. 定期复核到点但没有新证据时**不调模型**，只把复核时间往后推。

对应的验收样例是 M9 / M11 / M13。
"""
from __future__ import annotations

from scripts.memory_test_support import seed_semantic
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from agent_core.contracts import Scope
from agent_core.memory_maintenance import MemoryMaintenanceScheduler, MemoryMaintenanceService
from agent_core.memory_queue import MemoryCandidateQueue, MemoryCandidateWorker, MemoryMaintenanceWorker
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import MemoryCandidate, MemoryTurnPacket, MemoryUpdateSuggestion
from agent_core.memory_writes import MemoryWriter
from support.memory_review import FixedReviewProvider, supported_row


class _Embeddings:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _Extractor:
    """一次调用同时给出候选和更新建议；记下调用次数以便验证「什么时候真的调了模型」。"""

    def __init__(self, *candidates, suggestions=()) -> None:
        self.candidates = list(candidates)
        self.suggestions = list(suggestions)
        self.calls = 0
        self.provider = FixedReviewProvider([
            supported_row('预算三千', [('turn-1', '预算不是五千'), ('turn-1', '是三千')], index=0),
            supported_row('预算三千', [('turn-1', '预算不是五千'), ('turn-1', '是三千')], index=1),
        ])

    async def extract(self, packet, *, known_ids=None):
        self.calls += 1
        return list(self.candidates)

    async def extract_maintenance(self, packet, *, known_ids=None, known_versions=None):
        self.calls += 1
        return list(self.candidates), list(self.suggestions)


def _packet(turn_id: str = "turn-1", **overrides) -> MemoryTurnPacket:
    base = dict(
        turn_id=turn_id,
        occurred_at="2026-10-11T12:00:00+00:00",
        scope_owner="user",
        scope_channel="web",
        scope_conversation="c1",
        user_text="不是五千，是三千",
        published_reply="好，记成三千",
    )
    base.update(overrides)
    return MemoryTurnPacket(**base)


def _scope() -> Scope:
    return Scope("user", "web", "c1", "companion")


def _stack(tmp_path, *, candidates=(), suggestions=()):
    repos = initialize_memory_repositories(
        tmp_path / "records.db", tmp_path / "vectors", vector_size=3
    )
    writer = MemoryWriter(repos, _Embeddings())
    queue = MemoryCandidateQueue(tmp_path / "jobs.db")
    queue.initialize()
    service = MemoryMaintenanceService(writer, repos)
    extractor = _Extractor(*candidates, suggestions=suggestions)
    worker = MemoryCandidateWorker(queue, extractor, writer, maintenance_queue=queue)
    return dict(
        repos=repos,
        writer=writer,
        queue=queue,
        service=service,
        extractor=extractor,
        worker=worker,
        maintenance=MemoryMaintenanceWorker(queue, service, review_provider=FixedReviewProvider([
            supported_row('预算三千', [('turn-1', '不是五千'), ('turn-1', '是三千')])
        ])),
        scheduler=MemoryMaintenanceScheduler(service, interval_seconds=0),
    )


def _seed(writer, scope, content: str, *, version: int = 1):
    return seed_semantic(writer,
        scope,
        [
            MemoryCandidate(
                content=content,
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=("turn-0",),
                occurred_at="2026-10-11T10:00:00+00:00",
            )
        ],
        turn_id="turn-0",
        occurred_at="2026-10-11T10:00:00+00:00",
    )[0]


def test_one_extraction_feeds_both_the_candidate_and_the_maintenance_queue(tmp_path) -> None:
    stack = _stack(
        tmp_path,
        candidates=[
            MemoryCandidate(
                content="预算三千",
                kind="fact",
                content_status="reported",
                source_types=("user_statement",),
                source_refs=("turn-1",),
            )
        ],
    )
    scope = _scope()
    old = _seed(stack["writer"], scope, "预算五千")
    stack["extractor"].suggestions = [
        MemoryUpdateSuggestion(
            action="correct",
            target_id=old["id"],
            expected_version=1,
            content="预算三千",
            kind="fact",
            content_status="reported",
            source_types=("user_statement",),
            source_refs=("turn-1",),
            reason="用户说旧的数字错了",
        )
    ]
    stack["queue"].enqueue(_packet(user_text='预算不是五千，是三千'))
    asyncio.run(stack["worker"].process_once())

    stats = stack["queue"].kind_stats()
    assert stats["candidates"]["completed"] == 1
    assert stats["maintenance"]["pending"] == 1
    assert len(stack["writer"].list_records(scope)) == 2  # 旧记录 + 新候选
    stack["repos"].close()


def test_publishing_does_not_wait_for_the_background_extraction(tmp_path) -> None:
    """入队只是本地事务：模型调用发生在工人里，不是发布那一刻。"""
    stack = _stack(tmp_path)
    stack["queue"].enqueue(_packet())
    assert stack["extractor"].calls == 0

    asyncio.run(stack["worker"].process_once())
    assert stack["extractor"].calls == 1
    stack["repos"].close()


def test_a_maintenance_job_is_applied_and_the_old_version_is_kept(tmp_path) -> None:
    stack = _stack(tmp_path)
    scope = _scope()
    old = _seed(stack["writer"], scope, "预算五千")
    suggestion = MemoryUpdateSuggestion(
        action="correct",
        target_id=old["id"],
        expected_version=1,
        content="预算三千",
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        source_refs=("turn-1",),
        reason="用户说旧的数字错了",
    )
    stack["queue"].enqueue_maintenance(_packet(), [suggestion])

    result = asyncio.run(stack["maintenance"].process_once())

    assert result["applied"] == 1
    fresh_old = stack["repos"].records_by_ids([old["id"]])[old["id"]]
    assert fresh_old["status"] == "archived"
    assert fresh_old["effective_record_id"]
    stack["repos"].close()


def test_the_same_turn_only_enqueues_one_maintenance_job(tmp_path) -> None:
    stack = _stack(tmp_path)
    scope = _scope()
    old = _seed(stack["writer"], scope, "预算五千")
    suggestion = MemoryUpdateSuggestion(
        action="invalidate", target_id=old["id"], expected_version=1, reason="已到期"
    )
    first, created_first = stack["queue"].enqueue_maintenance(_packet(), [suggestion])
    second, created_second = stack["queue"].enqueue_maintenance(_packet(), [suggestion])

    assert created_first is True and created_second is False
    assert first == second
    stack["repos"].close()


def test_a_changed_payload_for_the_same_turn_is_rejected(tmp_path) -> None:
    stack = _stack(tmp_path)
    scope = _scope()
    old = _seed(stack["writer"], scope, "预算五千")
    stack["queue"].enqueue_maintenance(
        _packet(), [MemoryUpdateSuggestion(action="invalidate", target_id=old["id"], reason="到期")]
    )
    with pytest.raises(ValueError):
        stack["queue"].enqueue_maintenance(
            _packet(user_text="另一次完全不同的对话"),
            [MemoryUpdateSuggestion(action="invalidate", target_id=old["id"], reason="到期")],
        )
    stack["repos"].close()


def test_a_failed_apply_keeps_the_original_suggestion_for_retry(tmp_path) -> None:
    """重试必须复用**原建议**：重跑模型会得出一版措辞不同的建议，幂等键就变了。"""
    stack = _stack(tmp_path)
    scope = _scope()
    old = _seed(stack["writer"], scope, "预算五千")
    suggestion = MemoryUpdateSuggestion(
        action="correct",
        target_id=old["id"],
        expected_version=1,
        content="预算三千",
        kind="fact",
        content_status="reported",
        source_types=("user_statement",),
        reason="用户说旧的数字错了",
    )
    job_id, _ = stack["queue"].enqueue_maintenance(_packet(), [suggestion])

    def boom(*args, **kwargs):
        raise RuntimeError("数据库中途失败")

    original = stack["service"].apply
    stack["service"].apply = boom
    result = asyncio.run(stack["maintenance"].process_once())
    assert result["failed"] == 1
    stack["service"].apply = original

    job = stack["queue"].get(job_id)
    assert job["status"] == "failed"
    assert job["attempts"] == 1
    stored = stack["queue"].stored_suggestions(job)
    assert [item.target_id for item in stored] == [old["id"]]
    assert stack["extractor"].calls == 0  # 失败重试没有再调模型

    # 退避窗口过了以后重跑：复用原建议，直接把它应用下去。
    later = datetime.now(UTC) + timedelta(minutes=5)
    retry = asyncio.run(stack["maintenance"].process_once(now=later))
    assert retry["applied"] == 1
    assert stack["extractor"].calls == 0
    assert stack["queue"].get(job_id)["status"] == "completed"
    stack["repos"].close()


def test_an_expired_lease_can_be_claimed_again_after_a_restart(tmp_path) -> None:
    """进程被杀在「处理中」：租约到期后别的处理端能重新认领，作业不丢。"""
    stack = _stack(tmp_path)
    stack["queue"].enqueue(_packet())
    first = stack["queue"].claim(limit=1)
    assert first and first[0]["status"] == "processing"

    # 重启后立刻认领：租约还没过，不该抢到别人正在处理的那条。
    assert stack["queue"].claim(limit=1) == []
    recovered = stack["queue"].claim(limit=1, now=datetime.now(UTC) + timedelta(minutes=10))
    assert [item["job_id"] for item in recovered] == [first[0]["job_id"]]
    stack["repos"].close()


def test_content_maintenance_is_its_own_switch(monkeypatch) -> None:
    """内容维护是独立开关，默认关闭；关掉它只停维护，不影响候选链路。"""
    from agent_core import memory_switches

    monkeypatch.delenv("MEMORY_CONTENT_MAINTENANCE_ENABLED", raising=False)
    assert memory_switches.content_maintenance_enabled() is False
    monkeypatch.setenv("MEMORY_CONTENT_MAINTENANCE_ENABLED", "true")
    assert memory_switches.content_maintenance_enabled() is True
    monkeypatch.setenv("MEMORY_CONTENT_MAINTENANCE_ENABLED", "false")
    assert memory_switches.content_maintenance_enabled() is False


def test_the_review_pass_defers_due_records_without_calling_the_model(tmp_path) -> None:
    """M11：定期到点但没有新证据 → 只把复核时间往后推，模型调用为零。"""
    stack = _stack(tmp_path)
    scope = _scope()
    first = _seed(stack["writer"], scope, "预算是五千")
    second = _seed(stack["writer"], scope, "预算是三千")
    stack["service"].apply(
        MemoryUpdateSuggestion(
            action="conflict",
            target_id=first["id"],
            expected_version=1,
            conflict_ids=(second["id"],),
            reason="两条说法对不上",
            source_types=("user_statement",),
            source_refs=("turn-1",),
        ),
        scope=scope,
        packet=_packet(user_text="不记得了"),
        turn_id="turn-1",
    )
    assert stack["scheduler"].run_once() == {"due": 0, "deferred": 0, "model_calls": 0}

    later = datetime.now(UTC) + timedelta(days=2)
    report = stack["scheduler"].run_once(now=later)
    assert report["due"] == 2
    assert report["model_calls"] == 0
    assert stack["extractor"].calls == 0
    rows = {row["id"]: row for row in stack["writer"].list_records(scope)}
    assert rows[first["id"]]["status"] == "active"  # 只是排下一次，不擅自失效
    stack["repos"].close()
