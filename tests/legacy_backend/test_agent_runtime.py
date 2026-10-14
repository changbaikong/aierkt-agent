import asyncio

from backend.agent_runtime import AgentEvent, AgentRuntime, ActionRuntime, ToolAction
from backend.memory import MemoryScope


def test_agent_state_events_and_work_ledger_are_owner_scoped(tmp_path):
    runtime = AgentRuntime(tmp_path / "agent.db")
    alice = MemoryScope("alice", "wechat", "alice")
    bob = MemoryScope("bob", "wechat", "bob")

    async def exercise():
        turn_id = await runtime.begin_turn(alice, "wechat", "你好")
        await runtime.publish(AgentEvent("presence.updated", alice, {"idle_seconds": 1800}))
        await runtime.complete_turn(alice, turn_id)

    asyncio.run(exercise())
    assert runtime.state(alice).user_idle_seconds == 1800
    assert runtime.state(alice).phase == "idle"
    assert runtime.state(bob).user_idle_seconds == 0
    assert len(runtime.events(alice)) == 3
    assert [event["event_type"] for event in runtime.events(alice)] == [
        "turn.started",
        "presence.updated",
        "turn.completed",
    ]
    assert runtime.events(bob) == []

    work_item = runtime.create_work_item(alice, "查天气", "查广州天气")
    attempt = runtime.start_attempt(work_item["work_item_id"])
    runtime.finish_attempt(work_item["work_item_id"], attempt["attempt_id"], "completed", "done")
    artifact_id = runtime.add_artifact(work_item["work_item_id"], "result", "weather://guangzhou")
    assert artifact_id
    assert [item["work_item_id"] for item in runtime.work_items(alice)] == [work_item["work_item_id"]]
    assert runtime.work_items(bob) == []


def test_action_runtime_requires_explicit_confirmation():
    actions = ActionRuntime()
    actions.register("test.echo", lambda arguments: {"value": arguments["value"]})

    async def exercise():
        action = ToolAction("test.echo", {"value": "ok"}, requires_confirmation=True)
        pending = await actions.execute(action)
        completed = await actions.execute(action, confirmed=True)
        return pending, completed

    pending, completed = asyncio.run(exercise())
    assert pending.status == "pending_confirmation"
    assert completed.status == "completed"
    assert completed.output == {"value": "ok"}
