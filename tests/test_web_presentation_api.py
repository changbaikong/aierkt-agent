"""``/presentation`` 两个接口的真实 HTTP 用例。

走 TestClient 而不是直调仓库方法，是因为这一层要负责三件仓库不知道的事：
状态码（200 / 409 / 422）、Scope 从路径与请求体两个来源的组装、
以及"刷新恢复时快照长什么样"。这些只有真发一次请求才测得到。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_core.contracts import Scope
from agent_core.conversation_repository import ConversationRepository


ACCOUNT = "acct"
CONVERSATION = "web-stage"


def _scope(account: str = ACCOUNT, conversation: str = CONVERSATION) -> Scope:
    return Scope(account, "web", conversation, "companion")


@pytest.fixture()
def repository(tmp_path) -> ConversationRepository:
    return ConversationRepository(tmp_path / "core.db")


@pytest.fixture()
def client(repository, monkeypatch) -> TestClient:
    import web_api

    monkeypatch.setattr(web_api, "conversation_repository", repository)
    # manager 为 None：路由退回仓库直读 —— 正是"刷新后恢复"走的那条路径。
    monkeypatch.setattr(web_api, "manager", None)
    monkeypatch.setattr(web_api, "work_host", None)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    return TestClient(web_api.app)


def _seed(repository: ConversationRepository, *, account: str = ACCOUNT, conversation: str = CONVERSATION, runtime_state: str = "idle"):
    """一个用户提问 + 两句助手回复。返回 (scope, 用户消息 id, 队首 id, 第二条 id)。"""
    scope = _scope(account, conversation)
    user = repository.insert_message(scope, {"role": "user", "content": "帮我看下这个文件"})
    head = repository.insert_message(scope, {"role": "assistant", "content": "嗯，交给我吧。"})
    tail = repository.insert_message(scope, {"role": "assistant", "content": "看完了，一共三处改动。"})
    if runtime_state != "idle":
        repository.mark_runtime(scope, runtime_state)
    return scope, user, head, tail


def _advance_url(conversation: str = CONVERSATION) -> str:
    return f"/api/conversations/{conversation}/presentation/advance"


def _presentation(client: TestClient, *, account: str = ACCOUNT, conversation: str = CONVERSATION) -> dict:
    response = client.get(f"/api/conversations/{conversation}/presentation?account_id={account}")
    assert response.status_code == 200
    return response.json()


def test_presentation_exposes_only_the_head_of_the_queue(client, repository) -> None:
    _, user, head, tail = _seed(repository)

    payload = _presentation(client)

    assert payload["acknowledged_message_id"] == 0
    assert payload["current_message_id"] == head
    assert payload["pending_count"] == 2
    # 排在队首之后的那条**不在**可见消息里：提前给出去用户就"读到"了
    assert [item["message_id"] for item in payload["messages"]] == [user, head]
    assert tail not in [item["message_id"] for item in payload["messages"]]


def test_presentation_of_an_unknown_conversation_returns_an_empty_snapshot(client, repository) -> None:
    payload = _presentation(client, conversation="nobody")

    assert payload["messages"] == []
    assert payload["pending_count"] == 0
    assert payload["current_message_id"] is None
    # 查快照不该顺手建出一个空会话（它会出现在会话列表里）
    assert repository.get(_scope(conversation="nobody")) is None


def test_advance_confirms_the_head_and_returns_the_next_snapshot(client, repository) -> None:
    _, _, head, tail = _seed(repository)

    response = client.post(_advance_url(), json={"account_id": ACCOUNT, "message_id": head})

    assert response.status_code == 200
    payload = response.json()
    assert payload["acknowledged_message_id"] == head
    assert payload["current_message_id"] == tail
    assert payload["pending_count"] == 1


def test_advance_refuses_to_skip_the_head_with_409(client, repository) -> None:
    _, _, head, tail = _seed(repository)

    response = client.post(_advance_url(), json={"account_id": ACCOUNT, "message_id": tail})

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "not_head_of_queue"
    assert detail["expected_message_id"] == head
    assert repository.presentation_state(_scope()) == 0  # 一步都没动


def test_advance_is_idempotent_for_repeats_user_and_foreign_ids(client, repository) -> None:
    _, user, head, _ = _seed(repository)
    other = _seed(repository, conversation="elsewhere")[2]
    url = _advance_url()

    assert client.post(url, json={"account_id": ACCOUNT, "message_id": head}).status_code == 200
    repeat = client.post(url, json={"account_id": ACCOUNT, "message_id": head})
    assert repeat.status_code == 200
    assert repeat.json()["acknowledged_message_id"] == head

    # 用户消息、别的会话的助手消息：都是幂等的"什么都不做"，不是错误
    assert client.post(url, json={"account_id": ACCOUNT, "message_id": user}).status_code == 200
    assert client.post(url, json={"account_id": ACCOUNT, "message_id": other}).status_code == 200
    assert repository.presentation_state(_scope()) == head


def test_advance_rejects_invalid_identifiers_with_422(client, repository) -> None:
    _seed(repository)
    url = _advance_url()

    for bad in (0, -3, "abc", None):
        response = client.post(url, json={"account_id": ACCOUNT, "message_id": bad})
        assert response.status_code == 422, f"message_id={bad!r} 应当被拒绝"


def test_advance_keeps_same_conversation_id_isolated_per_account(client, repository) -> None:
    _, _, head, _ = _seed(repository)                    # acct
    other_scope, _, other_head, _ = _seed(repository, account="other")  # other，同一个 conversation_id

    response = client.post(_advance_url(), json={"account_id": "other", "message_id": other_head})

    assert response.status_code == 200
    assert response.json()["acknowledged_message_id"] == other_head
    assert repository.presentation_state(_scope()) == 0          # acct 的游标没被带跑
    assert repository.presentation_state(other_scope) == other_head
    assert head != other_head


def test_presentation_reports_running_and_interrupted_separately(client, repository) -> None:
    scope = _seed(repository, runtime_state="running")[0]

    running = _presentation(client)
    assert running["activity"] == "running"
    assert running["waiting"] is True

    repository.mark_runtime(scope, "interrupted")
    broken = _presentation(client)
    assert broken["activity"] == "interrupted"
    # 中断必须与"还在处理"分开：否则用户会一直等一件不会完成的事
    assert broken["waiting"] is False
