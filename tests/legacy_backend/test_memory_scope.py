from backend import memory


def test_user_memory_and_affect_state_are_owner_scoped(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_DIR", tmp_path)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(memory, "EMBEDDINGS_DISABLED", True)

    alice = memory.MemoryScope("wechat:alice", "wechat", "alice")
    bob = memory.MemoryScope("wechat:bob", "wechat", "bob")

    memory.save_raw_message("user", "ALICE_PRIVATE_MEMORY", alice)
    memory.save_raw_message("user", "BOB_PRIVATE_MEMORY", bob)

    assert "ALICE_PRIVATE_MEMORY" in memory.build_memory_context(alice)
    assert "BOB_PRIVATE_MEMORY" not in memory.build_memory_context(alice)
    assert "BOB_PRIVATE_MEMORY" in memory.build_memory_context(bob)
    assert "ALICE_PRIVATE_MEMORY" not in memory.build_memory_context(bob)

    with memory._get_db() as db:
        db.execute(
            "INSERT INTO facts (content, owner_id, channel, conversation_id) VALUES (?, ?, ?, ?)",
            ("ALICE_FACT", alice.owner_id, alice.channel, alice.conversation_id),
        )
        db.execute(
            "INSERT INTO summaries (content, owner_id, channel, conversation_id) VALUES (?, ?, ?, ?)",
            ("BOB_SUMMARY", bob.owner_id, bob.channel, bob.conversation_id),
        )
        db.commit()
    assert "ALICE_FACT" in memory.build_memory_context(alice)
    assert "BOB_SUMMARY" not in memory.build_memory_context(alice)

    memory.save_secret_diary("ALICE_PRIVATE_DIARY", scope=alice)
    memory.save_secret_diary("BOB_PRIVATE_DIARY", scope=bob)
    assert [item["content"] for item in memory.get_recent_diaries(scope=alice)] == ["ALICE_PRIVATE_DIARY"]

    memory.save_memory_dispute("ALICE_MEMORY_DISPUTE", scope=alice)
    memory.save_memory_dispute("BOB_MEMORY_DISPUTE", scope=bob)
    assert [item["claim"] for item in memory.get_recent_memory_disputes(scope=alice)] == ["ALICE_MEMORY_DISPUTE"]

    memory.adjust_affection(12, alice)
    assert memory.get_affection(alice) == 62
    assert memory.get_affection(bob) == 50

    alice_state = memory.update_mind_state_from_message("为什么会这样", alice)
    bob_state = memory.get_mind_state(bob)
    assert alice_state != bob_state
