from __future__ import annotations

from backend import memory


def test_memory_candidates_are_scoped_and_promotable(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_DIR", tmp_path)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory, "EMBEDDINGS_DISABLED", True)

    alice = memory.MemoryScope("alice", "wechat", "alice-chat")
    bob = memory.MemoryScope("bob", "wechat", "bob-chat")
    candidate_id = memory.save_memory_candidate("Alice likes tea", scope=alice, confidence=0.7)

    assert candidate_id is not None
    assert len(memory.list_memory_candidates(scope=alice)) == 1
    assert memory.list_memory_candidates(scope=bob) == []
    assert memory.promote_memory_candidate(candidate_id, bob) is False
    assert memory.promote_memory_candidate(candidate_id, alice) is True
    assert "Alice likes tea" in memory.build_memory_context(alice)
    assert "Alice likes tea" not in memory.build_memory_context(bob)


def test_memory_candidate_rejection_is_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_DIR", tmp_path)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory, "EMBEDDINGS_DISABLED", True)

    scope = memory.MemoryScope("user", "desktop", "desktop")
    candidate_id = memory.save_memory_candidate("temporary preference", scope=scope)
    assert candidate_id is not None
    assert memory.reject_memory_candidate(candidate_id, scope) is True
    assert memory.reject_memory_candidate(candidate_id, scope) is False
    assert memory.list_memory_candidates(scope=scope) == []


def test_assembly_injects_hot_and_confirmed_warm_but_not_cold(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_DIR", tmp_path)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory, "EMBEDDINGS_DISABLED", True)

    scope = memory.MemoryScope("user", "desktop", "desktop")
    memory.save_raw_message("user", "我现在在上海", scope)
    memory.save_fact("用户住在上海", scope=scope)
    context = memory.assemble_memory_context(scope, "上海")

    assert context.hot
    assert any(item["layer"] == memory.MemoryLayer.WARM for item in context.warm)
    assert context.cold_evidence == []
    assert "[USER_CONFIRMED_FACT]" in context.injected_text
    assert "[RECENT_DIALOGUE]" in context.injected_text


def test_legacy_fact_is_labeled_as_unconfirmed(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_DIR", tmp_path)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory, "EMBEDDINGS_DISABLED", True)
    scope = memory.MemoryScope("user", "desktop", "desktop")
    with memory._get_db() as db:
        db.execute(
            "INSERT INTO facts (content, owner_id, channel, conversation_id) VALUES (?, ?, ?, ?)",
            ("旧历史偏好", scope.owner_id, scope.channel, scope.conversation_id),
        )
        db.commit()
    context = memory.assemble_memory_context(scope)
    assert "[UNCONFIRMED_CANDIDATE]" in context.injected_text
