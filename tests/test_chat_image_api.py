from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from fastapi import HTTPException

from agent_core.chat_attachments import ChatAttachmentStore
from agent_core.contracts import AgentOutcome, Scope


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


def test_chat_attachment_upload_is_scope_bound(monkeypatch, tmp_path):
    import web_api

    store = ChatAttachmentStore(tmp_path / "db.sqlite", tmp_path / "files", ttl_seconds=60)
    monkeypatch.setattr(web_api, "_chat_attachment_store", store)
    response = TestClient(web_api.app).post(
        "/api/chat/attachments",
        data={"account_id": "alice", "conversation_id": "chat-1"},
        files={"file": ("photo.png", PNG, "image/png")},
    )

    assert response.status_code == 200
    attachment_id = response.json()["attachment_id"]
    assert store.get_for_scope(Scope("alice", "web", "chat-1", "companion"), attachment_id) is not None
    assert store.get_for_scope(Scope("bob", "web", "chat-1", "companion"), attachment_id) is None


def test_chat_accepts_image_only_and_sends_multimodal_content(monkeypatch, tmp_path):
    import web_api

    store = ChatAttachmentStore(tmp_path / "db.sqlite", tmp_path / "files", ttl_seconds=60)
    scope = Scope("alice", "web", "chat-1", "companion")
    attachment = store.upload(scope, "photo.png", "image/png", PNG)
    captured = {}

    class FakeManager:
        async def submit(self, request):
            captured["request"] = request
            future = asyncio.get_running_loop().create_future()
            future.set_result(AgentOutcome("completed", "run-1", reply="图里有一只猫。"))
            return SimpleNamespace(accepted=True, result=future, turn_id="turn-1", position=1)

        def history(self, _scope):
            return [{"role": "user", "content": ""}, {"role": "assistant", "content": "图里有一只猫。"}]

    monkeypatch.setattr(web_api, "_chat_attachment_store", store)
    monkeypatch.setattr(web_api, "manager", FakeManager())
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "_start_task_services", lambda: asyncio.sleep(0))
    monkeypatch.setattr(web_api, "_start_memory_workers", lambda: asyncio.sleep(0))

    response = asyncio.run(web_api.chat(web_api.ChatRequest(
        message="",
        attachment_id=attachment.attachment_id,
        account_id="alice",
        conversation_id="chat-1",
    )))

    assert response.status_code == 202
    parts = captured["request"].content_parts
    assert parts[0]["type"] == "image_url"
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "payload" not in str(captured["request"].attachment_metadata)
    asyncio.run(web_api._finish_chat_turn(web_api.ChatRequest(
        message="", attachment_id=attachment.attachment_id, account_id="alice", conversation_id="chat-1",
    ), SimpleNamespace(result=asyncio.sleep(0, result=AgentOutcome("completed", "run-1", reply="图里有一只猫。")), turn_id="turn-1"), attachment, store))
    assert store.get_for_scope(scope, attachment.attachment_id) is None


def test_chat_provider_failure_releases_attachment_for_retry(monkeypatch, tmp_path):
    import web_api

    store = ChatAttachmentStore(tmp_path / "db.sqlite", tmp_path / "files", ttl_seconds=60)
    scope = Scope("alice", "web", "chat-1", "companion")
    attachment = store.upload(scope, "photo.png", "image/png", PNG)

    class FailedManager:
        async def submit(self, _request):
            future = asyncio.get_running_loop().create_future()
            future.set_result(AgentOutcome("failed", "run-1", reply="provider failed: vision unavailable"))
            return SimpleNamespace(accepted=True, result=future, turn_id="turn-1", position=1)

        def history(self, _scope):
            return []

    monkeypatch.setattr(web_api, "_chat_attachment_store", store)
    monkeypatch.setattr(web_api, "manager", FailedManager())
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    monkeypatch.setattr(web_api, "_start_task_services", lambda: asyncio.sleep(0))
    monkeypatch.setattr(web_api, "_start_memory_workers", lambda: asyncio.sleep(0))

    response = asyncio.run(web_api.chat(web_api.ChatRequest(
        message="识别",
        attachment_id=attachment.attachment_id,
        account_id="alice",
        conversation_id="chat-1",
    )))

    assert response.status_code == 202
    asyncio.run(web_api._finish_chat_turn(web_api.ChatRequest(
        message="识别", attachment_id=attachment.attachment_id, account_id="alice", conversation_id="chat-1",
    ), SimpleNamespace(result=asyncio.sleep(0, result=AgentOutcome("failed", "run-1", reply="provider failed")), turn_id="turn-1"), attachment, store))
    assert store.get_for_scope(scope, attachment.attachment_id).status == "ready"


def test_chat_rejects_blank_message_without_attachment():
    import web_api

    with pytest.raises(HTTPException) as error:
        asyncio.run(web_api.chat(web_api.ChatRequest(message="")))

    assert error.value.status_code == 422
