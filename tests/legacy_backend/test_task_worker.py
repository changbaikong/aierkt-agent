"""跨轮次任务执行器测试：拆解 / 执行 / 步骤状态推进 / CAS / worker 集成。"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.agent_runtime import AgentRuntime
from backend.agent_loop import AgentOutcome
from backend.channel_events import channel_event_hub
from backend.memory import MemoryScope
from backend.task_worker import (
    plan_work_item,
    execute_plan_step,
    _process_work_item,
    _parse_plan_json,
)


def _make_llm() -> MagicMock:
    llm = MagicMock()
    llm.judge = AsyncMock(return_value='{"steps": ["搜索ESP32功耗", "分析结果"]}')
    return llm


# ---------- 拆解 ----------

def test_parse_plan_json_direct():
    assert _parse_plan_json('{"steps": ["a", "b"]}') == {"steps": ["a", "b"]}


def test_parse_plan_json_markdown_fence():
    assert _parse_plan_json('```json\n{"steps": ["x"]}\n```') == {"steps": ["x"]}


def test_parse_plan_json_bad_returns_none():
    assert _parse_plan_json("很抱歉我无法完成这个请求。") is None


class PlanWorkItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_steps(self):
        llm = _make_llm()
        steps = await plan_work_item(llm, "帮我分析ESP32的功耗问题")
        self.assertEqual(steps, ["搜索ESP32功耗", "分析结果"])

    async def test_degrades_on_bad_json(self):
        llm = _make_llm()
        llm.judge.return_value = "很抱歉我无法完成这个请求。"
        steps = await plan_work_item(llm, "帮我写一份周报")
        self.assertEqual(steps, ["帮我写一份周报"])

    async def test_caps_at_five(self):
        llm = _make_llm()
        llm.judge.return_value = json.dumps({"steps": [f"步骤{i}" for i in range(8)]})
        steps = await plan_work_item(llm, "帮我做一个大项目")
        self.assertEqual(len(steps), 5)

    async def test_execute_passes_prior_context(self):
        llm = _make_llm()
        step = {"instruction": "分析结果"}
        with patch("backend.task_worker.AgentLoop") as loop_type:
            loop_type.return_value.run = AsyncMock(return_value=AgentOutcome("completed", "run-1", "完成"))
            outcome = await execute_plan_step(llm, step, prior_context="前序：功耗30uA")
        self.assertEqual(outcome, "完成")
        user_text = loop_type.return_value.run.call_args[0][0]
        self.assertIn("前序：功耗30uA", user_text)
        self.assertIn("分析结果", user_text)


# ---------- DB：步骤状态推进 + CAS ----------

def test_steps_progress_through_statuses(tmp_path):
    runtime = AgentRuntime(tmp_path / "agent.db")
    scope = MemoryScope("alice", "wechat", "alice")
    item = runtime.create_work_item(scope, "任务", "帮我写周报")

    step_ids = runtime.save_plan_steps(item["work_item_id"], ["第一步", "第二步"])
    assert len(step_ids) == 2

    steps = runtime.get_steps(item["work_item_id"])
    assert [s["seq"] for s in steps] == [1, 2]
    assert all(s["status"] == "pending" for s in steps)

    runtime.update_step_status(item["work_item_id"], step_ids[0], "running")
    runtime.update_step_status(item["work_item_id"], step_ids[0], "completed", result="结果一")
    assert runtime.next_pending_step(item["work_item_id"])["step_id"] == step_ids[1]

    runtime.update_step_status(item["work_item_id"], step_ids[1], "running")
    runtime.update_step_status(item["work_item_id"], step_ids[1], "failed", error="boom")
    final = runtime.get_steps(item["work_item_id"])
    assert [s["status"] for s in final] == ["completed", "failed"]


def test_claim_work_item_is_cas(tmp_path):
    runtime = AgentRuntime(tmp_path / "agent.db")
    scope = MemoryScope("alice", "wechat", "alice")
    item = runtime.create_work_item(scope, "任务", "帮我做件事")

    assert runtime.claim_work_item(item["work_item_id"]) is True
    assert runtime.claim_work_item(item["work_item_id"]) is False


def test_mark_work_item_terminal_whitelist(tmp_path):
    runtime = AgentRuntime(tmp_path / "agent.db")
    scope = MemoryScope("alice", "wechat", "alice")
    item = runtime.create_work_item(scope, "任务", "帮我做件事")

    runtime.mark_work_item_terminal(item["work_item_id"], "completed")
    assert runtime.work_item(item["work_item_id"])["item"]["status"] == "completed"


# ---------- worker 集成 ----------

def test_worker_processes_one_queued_item_to_terminal(tmp_path, monkeypatch):
    runtime = AgentRuntime(tmp_path / "agent.db")
    scope = MemoryScope("alice", "wechat", "alice")
    item = runtime.create_work_item(scope, "任务", "帮我分析ESP32功耗")

    # 单独给 worker 用的 runtime（_process_work_item 内部用 get_agent_runtime 单例）
    monkeypatch.setattr("backend.task_worker.get_agent_runtime", lambda: runtime)
    llm = _make_llm()
    llm.judge.return_value = json.dumps({"steps": ["搜索", "分析"]})
    llm.chat_with_tools.return_value = "结果OK"

    queue = channel_event_hub.subscribe()
    try:
        asyncio.run(_process_work_item(llm, item))

        assert runtime.work_item(item["work_item_id"])["item"]["status"] == "completed"
        steps = runtime.get_steps(item["work_item_id"])
        assert [s["status"] for s in steps] == ["completed", "completed"]
        assert steps[0]["result"] == "结果OK"

        # 收到 task.completed 事件
        while True:
            evt = queue.get_nowait()
            if evt.get("type") == "task.completed":
                assert evt["work_item_id"] == item["work_item_id"]
                break
    finally:
        channel_event_hub.unsubscribe(queue)


def test_worker_step_failure_blocks_item(tmp_path, monkeypatch):
    runtime = AgentRuntime(tmp_path / "agent.db")
    scope = MemoryScope("alice", "wechat", "alice")
    item = runtime.create_work_item(scope, "任务", "帮我分析")

    monkeypatch.setattr("backend.task_worker.get_agent_runtime", lambda: runtime)
    llm = _make_llm()
    llm.judge.return_value = json.dumps({"steps": ["第一步", "第二步"]})
    llm.chat_with_tools.side_effect = RuntimeError("api down")

    asyncio.run(_process_work_item(llm, item))

    assert runtime.work_item(item["work_item_id"])["item"]["status"] == "blocked"
    steps = runtime.get_steps(item["work_item_id"])
    assert steps[0]["status"] == "failed"
    assert "api down" in steps[0]["error"]
