import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from agent_core.contracts import Scope
from agent_core.memory_processing import MemoryReviewProcessor
from agent_core.memory_repositories import initialize_memory_repositories
from agent_core.memory_turn import HistoryItem, MemoryCandidate, MemoryUpdateSuggestion, turn_from_messages
from agent_core.memory_writes import MemoryWriter
from support.memory_seed import seed_semantic


class _Embeddings:
    def embed(self, text):
        return [0.1, 0.2, 0.3]


class _SupportedReview:
    model = "regression"

    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def complete(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(message={"content": json.dumps({"items": [self.row]}, ensure_ascii=False)})


def test_current_user_correction_overrides_old_negative_history(tmp_path: Path):
    repositories = initialize_memory_repositories(tmp_path / "records.db", tmp_path / "vectors", vector_size=3)
    writer = MemoryWriter(repositories, _Embeddings())
    scope = Scope("repair", "web", "gate", "companion")
    old = seed_semantic(
        writer,
        scope,
        [MemoryCandidate("我不喜欢蓝色", "preference", "explicit", ("user_statement",))],
        turn_id="seed",
    )[0]
    packet = turn_from_messages(
        turn_id="t",
        occurred_at=datetime.now(UTC).isoformat(),
        scope=scope,
        user_text="我喜欢蓝色",
        published_reply="收到",
    )
    packet = replace(packet, relevant_history=[HistoryItem("old", "user", "我不喜欢蓝色")])
    suggestion = MemoryUpdateSuggestion(
        "correct",
        content="我喜欢蓝色",
        kind="preference",
        content_status="explicit",
        target_id=old["id"],
        expected_version=1,
        source_types=("user_statement",),
        source_refs=("t",),
    )
    review = _SupportedReview(
        {
            "index": 0,
            "verdict": "supported",
            "content": "我喜欢蓝色",
            "subject": "颜色偏好",
            "reason": "当前用户明确纠正此前偏好",
            "same_subject": True,
            "preserves_negation": True,
            "preserves_conditions": True,
            "change_authorized": True,
            "quotes": [{"message_id": "t", "quote": "我喜欢蓝色"}],
            "lifetime": {"mode": "long_term", "basis": "明确偏好"},
        }
    )
    _, updates, result = asyncio.run(
        MemoryReviewProcessor(writer, review).prepare(
            packet,
            [],
            [suggestion],
            persist=lambda payload: (tmp_path / "review.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8"),
        )
    )
    assert review.calls == 1
    assert result.rejected == []
    assert len(updates) == 1
    assert updates[0].content == "我喜欢蓝色"
    outcome = writer.apply_semantic_update(scope, updates[0], target=old, packet=packet, turn_id="t")
    record = outcome["record"]
    fresh = repositories.records_by_ids([old["id"], record["id"]])
    assert fresh[old["id"]]["status"] == "archived"
    assert fresh[old["id"]]["effective_record_id"] == record["id"]
    assert fresh[record["id"]]["status"] == "active"
    assert fresh[record["id"]]["content"] == "我喜欢蓝色"
    assert fresh[record["id"]]["record_version"] == 2
    repositories.close()


def test_model_authority_cannot_override_a_newer_record_with_late_evidence(tmp_path):
    from agent_core.memory_maintenance import MemoryMaintenanceService
    from support.memory_review import FixedReviewProvider, supported_row, prepare_update
    repositories = initialize_memory_repositories(tmp_path / 'records.db', tmp_path / 'vectors', vector_size=3)
    writer = MemoryWriter(repositories, _Embeddings())
    scope = Scope('late', 'web', 'gate', 'companion')
    old = seed_semantic(writer, scope, [MemoryCandidate('预算五千', 'fact', 'reported', ('user_statement',))],
                        turn_id='seed', occurred_at='2026-10-12T12:00:00+00:00')[0]
    packet = turn_from_messages(turn_id='t', occurred_at='2026-10-01T12:00:00+00:00', scope=scope,
                               user_text='预算三千', published_reply='收到')
    item = MemoryUpdateSuggestion('correct', content='预算三千', kind='fact', content_status='reported',
        target_id=old['id'], expected_version=1, source_types=('user_statement',), source_refs=('t',))
    reviewed = asyncio.run(prepare_update(writer, packet, item, [supported_row('预算三千', [('t', '预算三千')])]))
    outcome = MemoryMaintenanceService(writer, repositories).apply(reviewed, scope=scope, packet=packet, turn_id='t')
    assert outcome['applied'] is False
    assert repositories.records_by_ids([old['id']])[old['id']]['status'] == 'active'
    with repositories.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM memory_records').fetchone()[0] == 1
    repositories.close()
