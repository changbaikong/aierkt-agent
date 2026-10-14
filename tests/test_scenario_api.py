import json
import asyncio
import base64
import io
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from agent_core.contracts import ProviderResponse
from agent_core.scenario_attachments import ScenarioAttachmentStore
from agent_core.scenario_api import make_scenario_router
from agent_core.scenario_runtime import ScenarioRuntime, ScenarioStore


class Provider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        self.calls.append(messages)
        reply = self.replies.pop(0)
        return ProviderResponse(message={"role": "assistant", "content": reply}, usage={})


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def test_start_endpoint_requires_a_ready_analyst_card(tmp_path):
    provider = Provider([
        _json({
            "reply": "卡片已经整理好，可以开始。",
            "scene_prompt": "角色：用户是研究员，对话者是守塔人。地点：冬季灯塔。事件：暴风雪封路。",
            "scene_summary": {"agent_role": "守塔人", "place": "冬季灯塔", "time_or_weather": "暴风雪", "event": "封路"},
            "status": "ready",
        }),
    ])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))

    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        assert client.post(f"/api/scenarios/{scenario_id}/start", json={}).status_code == 409
        response = client.post(
            f"/api/scenarios/{scenario_id}/messages",
            json={"text": "暴风雪中的灯塔，我是研究员，遇到独自守塔的人。", "client_turn_id": "setup-1"},
        )
        assert response.status_code == 202
        assert client.get(f"/api/scenarios/{scenario_id}").json()["phase"] == "ready"
        started = client.post(f"/api/scenarios/{scenario_id}/start", json={})
        assert started.status_code == 200
        assert started.json()["phase"] == "dialogue"


def test_mode_endpoint_saves_chatbot_mode_and_locks_after_start(tmp_path):
    provider = Provider([
        _json({
            "reply": "卡片已经整理好，可以开始。",
            "scene_prompt": "角色：用户与守塔人。地点：冬季灯塔。事件：暴风雪封路。",
            "scene_summary": {"agent_role": "守塔人", "place": "冬季灯塔", "time_or_weather": "暴风雪", "event": "封路"},
            "persona": {
                "identity": "聊天助手。", "personality": "温和。", "speaking_style": "自然。",
                "interaction_habits": "先回应。", "emotional_responses": "保持平静。",
                "forbidden_expressions": "不说客服话。", "examples": "在。",
            },
            "persona_preferences": {"reply_length": "balanced", "initiative": "balanced", "assistance": "balanced"},
            "status": "ready",
        }),
    ])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))

    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        changed = client.put(f"/api/scenarios/{scenario_id}/mode", json={"dialogue_mode": "chatbot"})
        assert changed.status_code == 200
        assert changed.json()["dialogue_mode"] == "chatbot"
        assert client.put(f"/api/scenarios/{scenario_id}/mode", json={"dialogue_mode": "unknown"}).status_code == 422

        client.post(
            f"/api/scenarios/{scenario_id}/messages",
            json={"text": "冬季灯塔里遇到守塔人", "client_turn_id": "setup-1"},
        )
        locked_during_setup = client.put(
            f"/api/scenarios/{scenario_id}/mode", json={"dialogue_mode": "roleplay"},
        )
        assert locked_during_setup.status_code == 409
        assert client.post(f"/api/scenarios/{scenario_id}/start", json={}).status_code == 200
        locked = client.put(f"/api/scenarios/{scenario_id}/mode", json={"dialogue_mode": "roleplay"})
        assert locked.status_code == 409


def test_api_keeps_prompt_private_and_scopes_sessions_to_owner(tmp_path):
    provider = Provider([
        _json({"reply": "我整理成了清晨车站的情景卡。", "scene_prompt": "地点：清晨的山城车站。角色：返乡旅客与站务员。事件：浓雾导致列车晚点。", "scene_summary": {"user_role": "返乡旅客", "agent_role": "站务员", "relationship": "初次见面", "place": "山城车站", "time_or_weather": "清晨浓雾", "current_state": "等待列车", "event": "列车晚点"}, "status": "ready"}),
        _json({"reply": "广播刚说还要晚点。", "emotion": "认真", "sticker_id": None}),
    ])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        created = client.post("/api/scenarios", json={"account_id": "alice"})
        assert created.status_code == 201
        scenario_id = created.json()["scenario_id"]
        assert created.json()["messages"] == []

        submitted = client.post(
            f"/api/scenarios/{scenario_id}/messages",
            json={"account_id": "alice", "text": "清晨浓雾，我在山城车站等晚点列车，遇到一位站务员。", "client_turn_id": "setup-1"},
        )
        assert submitted.status_code == 202
        duplicate = client.post(
            f"/api/scenarios/{scenario_id}/messages",
            json={"account_id": "alice", "text": "清晨浓雾，我在山城车站等晚点列车，遇到一位站务员。", "client_turn_id": "setup-1"},
        )
        assert duplicate.json() == {"turn_id": submitted.json()["turn_id"], "status": "accepted", "fresh": False}
        ready = client.get(f"/api/scenarios/{scenario_id}?account_id=alice").json()
        assert ready["phase"] == "ready"
        assert "scene_prompt" not in ready
        assert "山城车站" in json.dumps(ready, ensure_ascii=False)
        assert [item["role"] for item in ready["messages"]] == ["user", "assistant"]

        assert client.post(f"/api/scenarios/{scenario_id}/start", json={"account_id": "alice"}).status_code == 200

        client.post(
            f"/api/scenarios/{scenario_id}/messages",
            json={"account_id": "alice", "text": "还要等多久？", "client_turn_id": "chat-1"},
        )
        snapshot = client.get(f"/api/scenarios/{scenario_id}?account_id=alice").json()
        assert snapshot["messages"][-1]["content"] == "广播刚说还要晚点。"
        assert client.get(f"/api/scenarios/{scenario_id}?account_id=bob").status_code == 404
        assert client.get("/api/scenarios?account_id=bob").json()["scenarios"] == []


def test_failed_turn_can_retry_without_duplicating_user_message(tmp_path):
    provider = Provider([
        "not json",
        "still not json",
        _json({"reply": "卡片已经整理完成。", "scene_prompt": "地点：早晨车站。角色：两名等车的旅客。", "scene_summary": {"agent_role": "旅客", "relationship": "陌生人", "place": "车站", "time_or_weather": "早上", "current_state": "等车", "event": "等车"}, "status": "ready"}),
    ])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        response = client.post(
            f"/api/scenarios/{scenario_id}/messages",
            json={"text": "车站，早上，我跟路人等车", "client_turn_id": "setup-1"},
        )
        turn_id = response.json()["turn_id"]
        failed = client.get(f"/api/scenarios/{scenario_id}").json()
        assert failed["turn_status"] == "failed"
        assert failed["phase"] == "drafting"

        retried = client.post(f"/api/scenarios/{scenario_id}/turns/{turn_id}/retry", json={})
        assert retried.status_code == 202
        ready = client.get(f"/api/scenarios/{scenario_id}").json()
        assert ready["phase"] == "ready"
        assert len([m for m in ready["messages"] if m["role"] == "user"]) == 1


def test_sticker_endpoint_serves_only_catalogued_image(tmp_path):
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), Provider([]), tmp_path / "stickers")
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==")
    (tmp_path / "stickers" / "wave.png").write_bytes(image)
    (tmp_path / "stickers" / "fake.png").write_text("not an image")
    sticker_id = runtime.stickers.choices()[0]["id"]
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        served = client.get(f"/api/scenarios/stickers/{sticker_id}")
        assert served.status_code == 200
        assert served.headers["content-type"] == "image/png"
        assert served.content == image
        assert client.get("/api/scenarios/stickers/" + "0" * 24).status_code == 404


def test_web_api_exposes_scenarios_without_starting_main_chat(monkeypatch, tmp_path):
    import web_api

    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), Provider([]), tmp_path / "stickers")
    monkeypatch.setattr(web_api, "_scenario_instance", runtime, raising=False)
    monkeypatch.setattr(web_api, "manager", None)

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web_api.app), base_url="http://test") as client:
            response = await client.post("/api/scenarios", json={})
            assert response.status_code == 201
            scenario_id = response.json()["scenario_id"]
            detail = await client.get(f"/api/scenarios/{scenario_id}")
            assert detail.status_code == 200
            assert detail.json()["phase"] == "drafting"

    asyncio.run(check())
    assert web_api.manager is None


def test_prompt_cards_can_be_viewed_and_edited_only_in_their_own_scenario(tmp_path):
    provider = Provider([
        _json({"reply": "卡片已整理完成。", "scene_prompt": "地点：午夜旧书店。角色：用户与店主。事件：暴雨导致临时闭店。", "scene_summary": {"agent_role": "旧书店店主", "relationship": "初次见面", "place": "旧书店", "time_or_weather": "午夜暴雨", "current_state": "临时闭店", "event": "共同避雨"}, "status": "ready"}),
        _json({"reply": "晚上好。", "emotion": "平静", "sticker_id": None}),
    ])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        first_id = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        second_id = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        path = f"/api/scenarios/{first_id}/prompt-cards"
        initial = client.get(path, params={"account_id": "alice"})
        assert initial.status_code == 200
        assert initial.json()["style_prompt"] == ""
        assert initial.json()["base_style_prompt"] == ""
        assert initial.json()["scene_prompt"] == ""
        assert initial.json()["revision"] == 0

        early = client.put(path, json={"account_id": "alice", "revision": 0, "style_prompt": "轻快又好奇，短句聊天。", "scene_prompt": ""})
        assert early.status_code == 200
        assert early.json()["revision"] == 1
        setup = client.post(f"/api/scenarios/{first_id}/messages", json={"account_id": "alice", "text": "午夜暴雨，我在旧书店遇到准备闭店的店主。", "client_turn_id": "setup-1"})
        assert setup.status_code == 202
        prepared = client.get(path, params={"account_id": "alice"}).json()
        assert prepared["revision"] == 2
        assert "旧书店" in prepared["scene_prompt"]
        assert prepared["style_prompt"] == "轻快又好奇，短句聊天。"
        assert client.get(f"/api/scenarios/{first_id}", params={"account_id": "alice"}).json()["scene_prompt_customized"] is False

        edited = client.put(path, json={"account_id": "alice", "revision": 2, "style_prompt": "说话干脆，带一点笑意。", "scene_prompt": "午夜书店偶遇，只在这一段情景生效。"})
        assert edited.status_code == 200
        assert edited.json()["revision"] == 3
        assert edited.json()["base_style_prompt"] == ""
        assert client.put(path, json={"account_id": "alice", "revision": 2, "style_prompt": "旧内容", "scene_prompt": "旧内容"}).status_code == 409
        assert client.get(path, params={"account_id": "bob"}).status_code == 404
        assert client.put(path, json={"account_id": "bob", "revision": 3, "style_prompt": "越权", "scene_prompt": "越权"}).status_code == 404
        assert client.get(f"/api/scenarios/{first_id}", params={"account_id": "alice"}).json()["scene_prompt_customized"] is True
        other = client.get(f"/api/scenarios/{second_id}/prompt-cards", params={"account_id": "alice"}).json()
        assert other["scene_prompt"] == ""
        assert other["style_prompt"] == ""
        assert "scene_prompt" not in client.get(f"/api/scenarios/{first_id}", params={"account_id": "alice"}).json()

        assert client.post(f"/api/scenarios/{first_id}/start", json={"account_id": "alice"}).status_code == 200
        chat = client.post(f"/api/scenarios/{first_id}/messages", json={"account_id": "alice", "text": "你也刚到？", "client_turn_id": "chat-1"})
        assert chat.status_code == 202
        system = provider.calls[-1][0]["content"]
        assert "爱尔奎特式的语气" not in system
        card = provider.calls[-1][1]
        assert card["role"] == "user"
        assert "说话干脆，带一点笑意。" not in card["content"]
        assert "午夜书店偶遇，只在这一段情景生效。" in card["content"]
        assert "临时闭店" not in card["content"]


def test_duplicate_client_turn_id_with_different_text_is_rejected(tmp_path):
    provider = Provider([_json({"user_role": "用户本人", "agent_role": "路人", "relationship": "陌生人", "place": "车站", "time_or_weather": "早上", "current_state": "正在等车", "event": "等车"}), _json({})])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        url = f"/api/scenarios/{scenario_id}/messages"
        assert client.post(url, json={"text": "车站早上与陌生人等车", "client_turn_id": "one"}).status_code == 202
        assert client.post(url, json={"text": "完全不同的设定", "client_turn_id": "one"}).status_code == 409
        messages = client.get(f"/api/scenarios/{scenario_id}").json()["messages"]
        assert [item["content"] for item in messages] == ["车站早上与陌生人等车"]


def test_image_attachment_reaches_only_this_scenario_dialogue(tmp_path):
    provider = Provider([_json({"reply": "这张图里有一只猫。", "emotion": "好奇", "sticker_id": None})])
    attachment_store = ScenarioAttachmentStore(tmp_path / "attachments.db", tmp_path / "uploads")
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers", attachment_store=attachment_store)
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==")
    with TestClient(app) as client:
        own = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        other = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        scene = {"user_role": "用户本人", "agent_role": "观测员", "place": "山顶观测站", "time_or_weather": "深夜", "event": "排查信号"}
        runtime.store.set_scene("alice", own, scene, "观测站排查信号")
        runtime.store.set_scene("alice", other, scene, "另一个情景")
        upload = client.post(f"/api/scenarios/{own}/attachments", data={"account_id": "alice"}, files={"file": ("cat.png", image, "image/png")})
        assert upload.status_code == 201
        attachment_id = upload.json()["attachment_id"]
        assert client.post(f"/api/scenarios/{other}/messages", json={"account_id": "alice", "text": "看图", "attachment_id": attachment_id, "client_turn_id": "foreign"}).status_code == 409
        assert client.post(f"/api/scenarios/{own}/messages", json={"account_id": "bob", "text": "看图", "attachment_id": attachment_id, "client_turn_id": "foreign"}).status_code == 404
        sent = client.post(f"/api/scenarios/{own}/messages", json={"account_id": "alice", "text": "", "attachment_id": attachment_id, "client_turn_id": "image-only"})
        assert sent.status_code == 202
        snapshot = client.get(f"/api/scenarios/{own}", params={"account_id": "alice"}).json()
        assert snapshot["messages"][0]["attachment_id"] == attachment_id
        assert "base64" not in json.dumps(snapshot)
        content = provider.calls[-1][-1]["content"]
        assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,") for part in content)
        assert snapshot["messages"][-1]["content"] == "这张图里有一只猫。"


def test_image_in_setup_remains_useful_after_a_followup_question(tmp_path):
    provider = Provider([
        _json({"reply": "图片里的地点可以用，但还缺少发生时间。", "scene_prompt": "地点：图片中的旧书店。角色：用户与店主。事件：偶然遇见。", "scene_summary": {"agent_role": "店主", "relationship": "初次见面", "place": "图中的旧书店", "current_state": "正在看书", "event": "偶遇"}, "status": "discussing"}),
        _json({"reply": "已经补成深夜版本，可以开始。", "scene_prompt": "地点：深夜的旧书店。角色：用户与店主。事件：闭店前偶然遇见。", "scene_summary": {"agent_role": "店主", "relationship": "初次见面", "place": "图中的旧书店", "time_or_weather": "23:00", "current_state": "准备闭店", "event": "偶遇"}, "status": "ready"}),
    ])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers", attachment_store=ScenarioAttachmentStore(tmp_path / "attachments.db", tmp_path / "uploads"))
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==")
    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        upload = client.post(f"/api/scenarios/{scenario_id}/attachments", files={"file": ("bookshop.png", image, "image/png")})
        attachment_id = upload.json()["attachment_id"]
        first = client.post(f"/api/scenarios/{scenario_id}/messages", json={"text": "照这张图片设定偶遇", "attachment_id": attachment_id, "client_turn_id": "setup-image"})
        assert first.status_code == 202
        assert client.get(f"/api/scenarios/{scenario_id}").json()["phase"] == "drafting"
        assert any(part.get("type") == "image_url" for part in provider.calls[0][-1]["content"])
        second = client.post(f"/api/scenarios/{scenario_id}/messages", json={"text": "晚上十一点", "client_turn_id": "setup-time"})
        assert second.status_code == 202
        ready = client.get(f"/api/scenarios/{scenario_id}").json()
        assert ready["phase"] == "ready"
        assert ready["scene_summary"]["place"] == "图中的旧书店"


def test_failed_image_turn_keeps_attachment_for_retry(tmp_path):
    provider = Provider(["not json", "still not json", _json({"reply": "我看见了。", "emotion": "专注", "sticker_id": None})])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers", attachment_store=ScenarioAttachmentStore(tmp_path / "attachments.db", tmp_path / "uploads"))
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL/nwAAAABJRU5ErkJggg==")
    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        runtime.store.set_scene("local-user", scenario_id, {"place": "书店"}, "书店偶遇")
        upload = client.post(f"/api/scenarios/{scenario_id}/attachments", files={"file": ("bookshop.png", image, "image/png")})
        sent = client.post(f"/api/scenarios/{scenario_id}/messages", json={"text": "这是什么？", "attachment_id": upload.json()["attachment_id"], "client_turn_id": "vision-1"})
        turn_id = sent.json()["turn_id"]
        assert client.get(f"/api/scenarios/{scenario_id}").json()["turn_status"] == "failed"
        assert client.post(f"/api/scenarios/{scenario_id}/turns/{turn_id}/retry", json={}).status_code == 202
        assert client.get(f"/api/scenarios/{scenario_id}").json()["messages"][-1]["content"] == "我看见了。"
        assert all(any(
            isinstance(message.get("content"), list)
            and any(part.get("type") == "image_url" for part in message["content"])
            for message in call
        ) for call in provider.calls)


def test_document_attachment_text_reaches_only_the_current_scenario(tmp_path):
    provider = Provider([_json({"reply": "我看完这份资料了。", "emotion": "认真", "sticker_id": None})])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        own = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        other = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        runtime.store.set_scene("alice", own, {"place": "书店"}, "书店聊天")
        runtime.store.set_scene("alice", other, {"place": "车站"}, "车站聊天")
        upload = client.post(f"/api/scenarios/{own}/attachments", data={"account_id": "alice"}, files={"file": ("notes.md", "封面写着：只在书店见面。".encode(), "text/markdown")})
        assert upload.status_code == 201
        attachment_id = upload.json()["attachment_id"]
        assert client.post(f"/api/scenarios/{other}/messages", json={"account_id": "alice", "attachment_id": attachment_id, "client_turn_id": "wrong"}).status_code == 409
        sent = client.post(f"/api/scenarios/{own}/messages", json={"account_id": "alice", "attachment_id": attachment_id, "client_turn_id": "doc-only"})
        assert sent.status_code == 202
        assert "只在书店见面" in provider.calls[-1][-1]["content"]
        snapshot = client.get(f"/api/scenarios/{own}", params={"account_id": "alice"}).json()
        assert snapshot["messages"][0]["attachment_name"] == "notes.md"
        assert "封面写着" not in json.dumps(snapshot, ensure_ascii=False)
        assert client.get(f"/api/scenarios/{own}/attachments/{attachment_id}", params={"account_id": "alice"}).status_code == 404


def test_docx_text_is_read_and_blank_pdf_has_clear_error(tmp_path):
    from pypdf import PdfWriter

    store = ScenarioAttachmentStore(tmp_path / "attachments.db", tmp_path / "uploads")
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>夜间观测站</w:t></w:r></w:p></w:body></w:document>')
    word = store.upload("alice", "one", "scene.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", docx.getvalue())
    assert word.extracted_text == "夜间观测站"

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    pdf = io.BytesIO()
    writer.write(pdf)
    try:
        store.upload("alice", "one", "scan.pdf", "application/pdf", pdf.getvalue())
    except ValueError as exc:
        assert "没有可读取的文字" in str(exc)
    else:
        raise AssertionError("blank PDF should not be accepted")


def test_attachment_rejects_forged_content_and_cleans_unused_uploads(tmp_path, monkeypatch):
    from agent_core import scenario_attachments

    store = ScenarioAttachmentStore(tmp_path / "attachments.db", tmp_path / "uploads")
    for name, media_type, data in [
        ("fake.png", "image/png", b"not png"),
        ("fake.pdf", "application/pdf", b"not pdf"),
        ("huge.txt", "text/plain", b"a" * (store.max_bytes + 1)),
    ]:
        try:
            store.upload("alice", "one", name, media_type, data)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{name} must be rejected")
    item = store.upload("alice", "one", "note.txt", "text/plain", b"temporary")
    monkeypatch.setattr(scenario_attachments.time, "time", lambda: item.expires_at + 1)
    assert store.cleanup_expired() == 1
    assert not item.path.exists()
    assert store.get("alice", "one", item.attachment_id) is None


def test_persona_analysis_api_is_owner_scoped_and_saving_does_not_reanalyze(tmp_path):
    provider = Provider([_json({
        "persona": {
            "identity": "小莱，电子生命。", "personality": "温柔。", "speaking_style": "短句。",
            "interaction_habits": "认真回应明确求助。", "emotional_responses": "被调侃时轻轻反驳。",
            "forbidden_expressions": "不要客服腔。", "examples": "累了时说：别硬撑。",
        },
        "persona_preferences": {"reply_length": "short", "initiative": "balanced", "assistance": "reliable"},
        "warnings": [],
    })])
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), provider, tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))

    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={"account_id": "alice"}).json()["scenario_id"]
        path = f"/api/scenarios/{scenario_id}/persona/analyze"
        assert client.post(path, json={"account_id": "bob", "source_text": "一段人设"}).status_code == 404
        analyzed = client.post(path, json={"account_id": "alice", "source_text": "我叫小莱，说话短一点。"})
        assert analyzed.status_code == 200
        assert analyzed.json()["persona"]["identity"] == "小莱，电子生命。"
        assert client.get(f"/api/scenarios/{scenario_id}/prompt-cards", params={"account_id": "alice"}).json()["persona"]["identity"] == ""

        cards = client.get(f"/api/scenarios/{scenario_id}/prompt-cards", params={"account_id": "alice"}).json()
        saved = client.put(f"/api/scenarios/{scenario_id}/prompt-cards", json={
            "account_id": "alice", "revision": cards["revision"], "style_prompt": "",
            "scene_prompt": "", "persona": analyzed.json()["persona"],
            "persona_preferences": analyzed.json()["persona_preferences"],
        })
        assert saved.status_code == 200
        assert saved.json()["persona"]["identity"] == "小莱，电子生命。"
        assert len(provider.calls) == 1


def test_persona_analysis_api_rejects_blank_and_oversized_source(tmp_path):
    runtime = ScenarioRuntime(ScenarioStore(tmp_path / "scenario.db"), Provider([]), tmp_path / "stickers")
    app = FastAPI()
    app.include_router(make_scenario_router(lambda: runtime))
    with TestClient(app) as client:
        scenario_id = client.post("/api/scenarios", json={}).json()["scenario_id"]
        path = f"/api/scenarios/{scenario_id}/persona/analyze"
        assert client.post(path, json={"source_text": "   "}).status_code == 422
        assert client.post(path, json={"source_text": "x" * 12001}).status_code == 422
