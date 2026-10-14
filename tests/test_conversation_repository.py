from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_core.contracts import Scope
from agent_core.conversation_repository import ConversationRepository


def _scope(conversation_id: str, *, owner_id: str = "owner", channel: str = "web", module_id: str = "companion") -> Scope:
    return Scope(owner_id, channel, conversation_id, module_id)


def test_repository_keeps_same_conversation_id_isolated_by_full_scope(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    first = _scope("shared", owner_id="first")
    second = _scope("shared", owner_id="second")

    repository.append_message(first, {"role": "user", "content": "first private message"})
    repository.append_message(second, {"role": "user", "content": "second private message"})

    first_history = repository.history(first)
    second_history = repository.history(second)
    assert [item["content"] for item in first_history] == ["first private message"]
    assert [item["content"] for item in second_history] == ["second private message"]
    assert "created_at" in first_history[0]
    assert "created_at" in second_history[0]


def test_append_message_touches_activity_by_default(tmp_path) -> None:
    """默认行为不变：写入会刷新 last_activity_at，对话因此在列表里前置。"""
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("touch")
    before = repository.ensure(scope, now=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc))

    repository.append_message(
        scope,
        {"role": "user", "content": "hello"},
        now=datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc),
    )

    after = repository.get(scope)
    assert after.last_activity_at > before.last_activity_at


def test_append_message_without_touch_keeps_activity_stale(tmp_path) -> None:
    """touch_activity=False（系统回执）：消息照写，但 last_activity_at 不动。

    否则后台任务完成会把一个没人说话的对话顶到会话列表最前面。
    """
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("receipt")
    before = repository.ensure(scope, now=datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc))

    accepted = repository.append_message(
        scope,
        {"role": "assistant", "content": "「整理下载目录」完成了。", "kind": "work_result"},
        stable_key="work-result:a-1:completed",
        now=datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc),
        touch_activity=False,
    )

    after = repository.get(scope)
    assert accepted is True
    assert [item["content"] for item in repository.history(scope)] == ["「整理下载目录」完成了。"]
    assert after.last_activity_at == before.last_activity_at
    assert after.updated_at == before.updated_at


def test_repository_restores_messages_and_first_user_title_after_restart(tmp_path) -> None:
    database = tmp_path / "core.db"
    scope = _scope("restart")
    ConversationRepository(database).append_message(
        scope,
        {"role": "user", "content": "  这是   一条 会被规范化 的会话标题  "},
    )

    restored = ConversationRepository(database)

    restored_history = restored.history(scope)
    assert restored_history[0]["content"] == "  这是   一条 会被规范化 的会话标题  "
    assert "created_at" in restored_history[0]
    assert restored.get(scope).title == "这是 一条 会被规范化 的会话标题"


def test_repository_marks_only_running_conversations_interrupted_after_restart(tmp_path) -> None:
    database = tmp_path / "core.db"
    running = _scope("running")
    idle = _scope("idle")
    repository = ConversationRepository(database)
    repository.ensure(running)
    repository.ensure(idle)
    repository.mark_runtime(running, "running")

    restarted = ConversationRepository(database)

    assert restarted.mark_running_interrupted() == 1
    assert restarted.get(running).runtime_state == "interrupted"
    assert restarted.get(idle).runtime_state == "idle"


def test_repository_truncates_title_and_retains_latest_one_thousand_messages(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db", history_max_messages=1000)
    scope = _scope("long")

    repository.append_message(scope, {"role": "user", "content": "x" * 41})
    for index in range(1000):
        repository.append_message(scope, {"role": "assistant", "content": str(index)})

    history = repository.history(scope)

    assert len(history) == 1000
    assert history[0]["content"] == "0"
    assert history[-1]["content"] == "999"
    assert repository.get(scope).title == "x" * 40


def test_repository_archives_inactive_conversations_and_purges_old_archives(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    inactive = _scope("inactive")
    expired = _scope("expired")
    now = datetime.now()
    repository.ensure(inactive, now=now - timedelta(days=31))
    repository.ensure(expired, now=now - timedelta(days=91))
    repository.archive(expired, now=now - timedelta(days=91))

    result = repository.cleanup(now=now)

    assert result == {"archived": 1, "deleted": 1}
    assert repository.get(inactive).status == "archived"
    assert repository.get(expired) is None


def test_repository_stores_created_at_without_time_prefix_in_content(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    scope = _scope("timed")

    repository.append_message(
        scope,
        {
            "role": "user",
            "content": "[消息时间: 2026-08-29T10:00:00.000Z] [消息时间: 2026-08-28T09:00:00.000Z] 上周讨论的方案",
        },
        now=datetime(2026, 9, 10, 12, 0, 0),
    )

    history = repository.history(scope)
    assert history[0]["content"] == "上周讨论的方案"
    assert history[0]["created_at"]


def test_today_user_events_use_shanghai_day_and_exclude_current_conversation(tmp_path) -> None:
    repository = ConversationRepository(tmp_path / "core.db")
    current = _scope("current")
    other = _scope("other")
    now = datetime(2026, 9, 10, 9, 30, tzinfo=timezone.utc)  # 17:30 in Shanghai
    repository.append_message(other, {"role": "user", "content": "昨天的消息"}, now=datetime(2026, 9, 9, 15, 59, tzinfo=timezone.utc))
    repository.append_message(other, {"role": "user", "content": "16:30 我今天喝了酒"}, now=datetime(2026, 9, 10, 8, 30, tzinfo=timezone.utc))
    repository.append_message(current, {"role": "user", "content": "当前会话消息"}, now=datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc))

    rows = repository.today_user_events(current, now=now)

    assert [row["content"] for row in rows] == ["16:30 我今天喝了酒"]
