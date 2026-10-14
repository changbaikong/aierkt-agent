"""舞台播放投影与已读游标的回归用例。

对应的三条产品语义：
1. 两条管家消息前后脚到达时，后一条**不能**抢掉前一条；
2. 已读游标只能向前，且只能被本 Scope 的助手消息推动；
3. "还在处理" / "已中断" 必须分开显示 —— 前者让用户等，后者让用户放心。
"""

from __future__ import annotations

import sqlite3

from agent_core.contracts import Scope
from agent_core.conversation_repository import (
    ADVANCE_ADVANCED,
    ADVANCE_BLOCKED,
    ADVANCE_UNCHANGED,
    ConversationRepository,
)
from agent_core.presentation import (
    ACTIVITY_IDLE,
    ACTIVITY_INTERRUPTED,
    ACTIVITY_PAUSED,
    ACTIVITY_RUNNING,
    background_summary,
    is_waiting,
    normalize_activity,
    pending_assistant_messages,
    project_presentation,
    visible_messages,
)


def _scope(conversation_id: str = "stage", *, owner_id: str = "owner", module_id: str = "companion") -> Scope:
    return Scope(owner_id, "web", conversation_id, module_id)


def _messages(*specs):
    """``(role, content, message_id)`` 三段式 → 账本消息字典。"""
    return [
        {"role": role, "content": content, "message_id": message_id}
        for role, content, message_id in specs
    ]


# ----------------------------------------------------------------------
# 队列顺序：不抢占
# ----------------------------------------------------------------------
def test_second_steward_message_does_not_preempt_the_first() -> None:
    messages = _messages(
        ("user", "帮我看看那篇论文", 1),
        ("assistant", "嗯，交给我吧。", 2),
        ("assistant", "找到了。它主要在讨论这个。", 3),
    )

    snapshot = project_presentation(messages, acknowledged_message_id=1)

    assert snapshot.current_message_id == 2
    assert snapshot.pending_message_ids == [2, 3]
    assert snapshot.pending_count == 2
    # 队列里排第二的那条**不出现**在履历里 —— 提前出现就等于剧透，
    # 用户会在履历里看到自己还没确认过的话。
    assert [item["message_id"] for item in snapshot.messages] == [1, 2]


def test_unplayed_messages_never_leak_into_visible_history() -> None:
    messages = _messages(
        ("user", "问题", 1),
        ("assistant", "已经读过的回复", 2),
        ("assistant", "刚到的进度", 3),
        ("assistant", "最终回答", 4),
    )

    visible = visible_messages(messages, acknowledged_message_id=2, current_message_id=3)

    assert [item["message_id"] for item in visible] == [1, 2, 3]


def test_pending_ignores_messages_without_identity() -> None:
    """没有 message_id 的助手消息不能占住队列 —— 它永远确认不掉，会堵死后面所有消息。"""
    messages = [
        {"role": "user", "content": "问题", "message_id": 1},
        {"role": "assistant", "content": "内存里的临时条"},
        {"role": "assistant", "content": "有身份的进度", "message_id": 2},
    ]

    assert [item["message_id"] for item in pending_assistant_messages(messages, 1)] == [2]


def test_user_messages_are_never_queued() -> None:
    messages = _messages(
        ("assistant", "回复", 1),
        ("user", "下一句", 2),
        ("user", "再一句", 3),
    )

    assert pending_assistant_messages(messages, 1) == []
    assert project_presentation(messages, acknowledged_message_id=1).current_message_id is None


# ----------------------------------------------------------------------
# 活动状态
# ----------------------------------------------------------------------
def test_interrupted_never_looks_like_still_working() -> None:
    """中断和"还在处理"必须是两件事，否则用户会一直等一件不会完成的事。"""
    assert normalize_activity("interrupted") == ACTIVITY_INTERRUPTED
    assert is_waiting(ACTIVITY_INTERRUPTED) is False
    assert is_waiting(ACTIVITY_RUNNING) is True
    assert is_waiting(ACTIVITY_PAUSED) is False
    assert is_waiting(ACTIVITY_IDLE) is False


def test_live_background_task_outranks_a_stale_interrupted_mark() -> None:
    """进程重启会把上一轮标成 interrupted；此刻确有后台任务在跑就得说"在跑"。"""
    assert normalize_activity("interrupted", background_active=True) == ACTIVITY_RUNNING


def test_background_summary_counts_only_active_states() -> None:
    summary = background_summary(
        [{"status": "running"}, {"status": "completed"}, {"status": "queued"}],
        plans=[{"status": "waiting_approval"}, {"status": "failed"}],
    )

    assert summary == {"active": True, "active_count": 3}
    assert background_summary([]) == {"active": False, "active_count": 0}


def test_waiting_flag_survives_after_the_bubble_finishes() -> None:
    """气泡说完了、后台还在跑：waiting 仍为 True，舞台继续显示"我还在处理"。"""
    messages = _messages(("user", "跑个后台任务", 1), ("assistant", "交给我吧。", 2))

    snapshot = project_presentation(
        messages,
        acknowledged_message_id=2,
        runtime_state="idle",
        background={"active": True, "active_count": 1},
    )

    assert snapshot.pending_count == 0
    assert snapshot.waiting is True
    assert snapshot.activity == ACTIVITY_RUNNING


# ----------------------------------------------------------------------
# 已读游标（存储层）
# ----------------------------------------------------------------------
def test_new_conversation_starts_at_zero_instead_of_swallowing_unread(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("fresh")
    message_id = repository.insert_message(scope, {"role": "assistant", "content": "后台跑完了"})

    assert message_id is not None
    assert repository.presentation_state(scope) == 0
    snapshot = project_presentation(
        repository.history(scope),
        acknowledged_message_id=repository.presentation_state(scope),
    )
    assert snapshot.pending_message_ids == [message_id]


def test_advance_moves_the_cursor_forward_only(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("forward")
    first = repository.insert_message(scope, {"role": "assistant", "content": "第一句"})
    second = repository.insert_message(scope, {"role": "assistant", "content": "第二句"})

    assert repository.advance_presentation(scope, first).acknowledged_message_id == first
    assert repository.advance_presentation(scope, second).acknowledged_message_id == second
    # 倒着确认：游标不动，也不报错（重复点击、乱序到达的确认都不该把已读拉回去）
    repeated = repository.advance_presentation(scope, first)
    assert repeated.status == ADVANCE_UNCHANGED
    assert repeated.acknowledged_message_id == second


def test_cursor_ignores_other_scope_and_user_messages(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("mine")
    other = _scope("theirs")
    mine = repository.insert_message(scope, {"role": "user", "content": "我问的"})
    theirs = repository.insert_message(other, {"role": "assistant", "content": "别的会话的回复"})
    reply = repository.insert_message(scope, {"role": "assistant", "content": "我答的"})

    # 用户消息、别的会话的 id：都不该推进游标，也都不是错误
    assert repository.advance_presentation(scope, mine).status == ADVANCE_UNCHANGED
    assert repository.advance_presentation(scope, theirs).status == ADVANCE_UNCHANGED
    assert repository.advance_presentation(scope, mine).acknowledged_message_id == 0
    assert repository.advance_presentation(scope, reply).status == ADVANCE_ADVANCED
    assert repository.presentation_state(other) == 0  # 隔离：对方游标不受影响


def test_advance_refuses_to_skip_the_head_of_the_queue(tmp_path) -> None:
    """只能确认队首那条未读助手消息 —— 这是这轮收紧的核心契约。"""
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("queue")
    first = repository.insert_message(scope, {"role": "assistant", "content": "第一句"})
    second = repository.insert_message(scope, {"role": "assistant", "content": "第二句"})
    third = repository.insert_message(scope, {"role": "assistant", "content": "第三句"})

    skipped = repository.advance_presentation(scope, third)
    assert skipped.status == ADVANCE_BLOCKED
    assert skipped.acknowledged_message_id == 0
    assert skipped.expected_message_id == first      # 告诉调用方该确认谁
    assert repository.presentation_state(scope) == 0  # 状态一点没动

    assert repository.advance_presentation(scope, first).status == ADVANCE_ADVANCED
    # 跳过中间那句依然被拒
    assert repository.advance_presentation(scope, third).status == ADVANCE_BLOCKED
    assert repository.presentation_state(scope) == first
    assert repository.advance_presentation(scope, second).status == ADVANCE_ADVANCED
    assert repository.advance_presentation(scope, third).status == ADVANCE_ADVANCED


def test_advance_is_idempotent_when_nothing_is_left(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("done")
    only = repository.insert_message(scope, {"role": "assistant", "content": "就这一句"})

    assert repository.advance_presentation(scope, only).status == ADVANCE_ADVANCED
    again = repository.advance_presentation(scope, only)
    assert again.status == ADVANCE_UNCHANGED
    assert again.acknowledged_message_id == only


def test_cursor_rejects_non_positive_identifiers(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")

    for invalid in (0, -1):
        try:
            repository.advance_presentation(_scope("bad"), invalid)
        except ValueError:
            continue
        raise AssertionError(f"message_id={invalid} 应当被拒绝")


def test_existing_history_is_marked_read_when_the_cursor_table_is_created(tmp_path) -> None:
    """老库升级：建表那一刻把每个会话的游标推到当前最后一条，避免上线后重播全部历史。"""
    database = tmp_path / "core.db"
    repository = ConversationRepository(database)
    scope = _scope("legacy")
    repository.insert_message(scope, {"role": "user", "content": "很久以前的提问"})
    legacy_reply = repository.insert_message(scope, {"role": "assistant", "content": "很久以前的回答"})

    # 模拟"升级前"：账本里已经有历史，但还没有展示游标这张表。
    with sqlite3.connect(database) as raw:
        raw.execute("DROP TABLE agent_core_conversation_presentation")

    upgraded = ConversationRepository(database)

    assert upgraded.presentation_state(scope) == legacy_reply
    assert project_presentation(
        upgraded.history(scope),
        acknowledged_message_id=upgraded.presentation_state(scope),
    ).pending_count == 0


def test_history_exposes_stable_message_identity(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("identity")
    message_id = repository.insert_message(scope, {"role": "user", "content": "hi", "kind": "chat"})

    entry = repository.history(scope)[0]

    assert entry["message_id"] == message_id
    assert entry["kind"] == "chat"
    # 身份不能从 metadata 混进来（那样会出现两个同名键，谁赢看展开顺序）
    assert entry["message_id"] == repository.history(scope)[0]["message_id"]


def test_deleting_a_conversation_clears_its_cursor(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("rebuilt")
    reply = repository.insert_message(scope, {"role": "assistant", "content": "旧会话的回复"})
    repository.advance_presentation(scope, reply)

    assert repository.delete(scope) is True
    assert repository.presentation_state(scope) == 0


# ----------------------------------------------------------------------
# 游标生命周期
# ----------------------------------------------------------------------
def test_clearing_messages_clears_the_cursor_too(tmp_path) -> None:
    """清空消息必须连游标一起清。

    留着游标就等于留着"读到第 2015 条了"这个位置，而第 2015 条已经不存在 ——
    下一次算队列时会拿一个悬空位置去比较，表现是刚写入的未读消息凭空消失。
    """
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("cleared")
    reply = repository.insert_message(scope, {"role": "assistant", "content": "会被清掉"})
    repository.advance_presentation(scope, reply)
    assert repository.presentation_state(scope) == reply

    assert repository.clear(scope) is True

    assert repository.presentation_state(scope) == 0
    assert repository.history(scope) == []


def test_recreating_the_same_conversation_id_starts_from_zero(tmp_path) -> None:
    """同一个 conversation_id 被重新创建时，不能继承上一个会话的播放位置。"""
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("reborn")
    old = repository.insert_message(scope, {"role": "assistant", "content": "上一个会话的回复"})
    repository.advance_presentation(scope, old)
    repository.delete(scope)

    fresh = repository.insert_message(scope, {"role": "assistant", "content": "新会话的第一句"})

    assert repository.presentation_state(scope) == 0
    snapshot = project_presentation(
        repository.history(scope),
        acknowledged_message_id=repository.presentation_state(scope),
    )
    assert snapshot.pending_message_ids == [fresh]


def test_cleanup_purges_orphaned_cursors(tmp_path) -> None:
    """定期清理要收掉"父会话已经不在了"的游标行。"""
    database = tmp_path / "core.db"
    repository = ConversationRepository(database)
    stale = _scope("stale")
    repository.insert_message(stale, {"role": "assistant", "content": "旧会话"})
    repository.presentation_state(stale)

    # 直接把会话行删掉，模拟"升级前删过会话、游标残留"的老库
    with sqlite3.connect(database) as raw:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DELETE FROM agent_core_conversations WHERE conversation_id='stale'")

    repository.cleanup()

    with repository._pool.get_connection() as db:
        leftover = db.execute(
            "SELECT COUNT(*) AS n FROM agent_core_conversation_presentation WHERE conversation_id='stale'"
        ).fetchone()["n"]
    assert leftover == 0


def test_presentation_state_does_not_create_a_conversation(tmp_path) -> None:
    """查一个还不存在的会话：返回 0，且不顺手建出一个空会话（它会出现在会话列表里）。"""
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("ghost")

    assert repository.presentation_state(scope) == 0
    assert repository.get(scope) is None


def test_presentation_table_has_a_scoped_cascade_foreign_key(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")

    with repository._pool.get_connection() as db:
        keys = db.execute("PRAGMA foreign_key_list(agent_core_conversation_presentation)").fetchall()

    # 复合外键在 PRAGMA 里是"每个列一行"，所以比的是四行合起来说明的那件事：
    # 四个键一起指向会话表，且级联删除。
    assert {row["table"] for row in keys} == {"agent_core_conversations"}
    assert {row["on_delete"] for row in keys} == {"CASCADE"}
    assert {row["from"] for row in keys} == {"owner_id", "channel", "conversation_id", "module_id"}
    assert len({row["id"] for row in keys}) == 1  # 同一个外键，不是四个


def test_legacy_cursor_table_is_rebuilt_with_the_foreign_key(tmp_path) -> None:
    """老库升级：无外键的旧表被重建，正常数据保留、孤儿行被滤掉。"""
    database = tmp_path / "core.db"
    repository = ConversationRepository(database)
    kept = _scope("kept")
    kept_reply = repository.insert_message(kept, {"role": "assistant", "content": "保留"})
    repository.advance_presentation(kept, kept_reply)

    # 退回"有游标表但没外键"的老形态，并塞进一行孤儿
    raw = sqlite3.connect(database)
    try:
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DROP TABLE agent_core_conversation_presentation")
        raw.execute(
            """CREATE TABLE agent_core_conversation_presentation (
                owner_id TEXT NOT NULL, channel TEXT NOT NULL, conversation_id TEXT NOT NULL,
                module_id TEXT NOT NULL, acknowledged_message_id INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, channel, conversation_id, module_id))"""
        )
        raw.execute(
            "INSERT INTO agent_core_conversation_presentation VALUES ('owner','web','kept','companion',?, 'x')",
            (kept_reply,),
        )
        raw.execute(
            "INSERT INTO agent_core_conversation_presentation VALUES ('owner','web','ghost','companion',7,'x')"
        )
        raw.commit()
    finally:
        raw.close()

    upgraded = ConversationRepository(database)

    assert upgraded.presentation_state(kept) == kept_reply  # 正常数据保留
    with upgraded._pool.get_connection() as db:
        assert db.execute("PRAGMA foreign_key_list(agent_core_conversation_presentation)").fetchall()
        orphans = db.execute(
            "SELECT COUNT(*) AS n FROM agent_core_conversation_presentation WHERE conversation_id='ghost'"
        ).fetchone()["n"]
    assert orphans == 0  # 孤儿行没有跟着搬过去
