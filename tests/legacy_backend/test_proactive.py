import asyncio

from backend.agent_runtime import AgentEvent, AgentRuntime
from backend.channel_events import channel_event_hub
from backend.memory import MemoryScope
from backend.screen_mind import clear_pending_visual_note, get_pending_visual_note, set_pending_visual_note


def test_proactive_turn_uses_profile_and_consumes_visual_note(tmp_path, monkeypatch):
    from backend import proactive

    scope = MemoryScope("local-user", "desktop", "desktop")
    runtime = AgentRuntime(tmp_path / "agent.db")
    prompts = []

    class FakeLLM:
        def __init__(self, system_prompt: str):
            prompts.append(system_prompt)

        async def chat(self, _message: str) -> str:
            return "该歇会儿了。"

    async def fake_speak(_llm, _reply: str) -> str:
        return "少し休むがよい。"

    monkeypatch.setattr(proactive, "LLMClient", FakeLLM)
    monkeypatch.setattr(proactive, "make_speak_text", fake_speak)
    monkeypatch.setattr(proactive, "remember_speak_text", lambda _text: None)
    monkeypatch.setattr(proactive, "get_affection", lambda _scope: 80)
    clear_pending_visual_note()
    set_pending_visual_note("正在浏览代码项目页面。")
    subscriber = channel_event_hub.subscribe()

    async def exercise():
        await runtime.publish(AgentEvent("presence.updated", scope, {"idle_seconds": 3600}))
        return await proactive.emit_proactive_turn("基础系统提示", scope=scope, runtime=runtime)

    try:
        result = asyncio.run(exercise())
        assert result["status"] == "emitted"
        assert "[角色档案]" in prompts[0]
        assert "正在浏览代码项目页面。" in prompts[0]
        assert get_pending_visual_note() is None
        assert subscriber.get_nowait()["type"] == "speech"
    finally:
        channel_event_hub.unsubscribe(subscriber)
        clear_pending_visual_note()
