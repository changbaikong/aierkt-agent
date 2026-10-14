from __future__ import annotations

import asyncio
import importlib
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from agent_core.contracts import AgentOutcome, Scope
from agent_core.events import Event, EventType, InMemoryEventBus
from chat_cli import (
    DEFAULT_SYSTEM_PROMPT,
    _confirmation_decision,
    _format_outcome,
    _format_pending_confirmations,
    _format_tasks,
    _format_condition_tasks,
    _parse_confirmation_command,
    _format_task_due_event,
    _format_local_time_result,
    _subscribe_task_due_output,
    _startup_messages,
    _shutdown_cli,
    _runtime_time_context,
    _request_with_runtime_time,
    _format_tool_registration,
    _format_unavailable_capability,
    _format_recovery_notice,
    _generate_recovery_prompt,
)


class ChatCliTests(unittest.IsolatedAsyncioTestCase):
    def test_cli_default_system_prompt_reuses_web_persona(self) -> None:
        from agent_core.personas import ARCUEID_PERSONA

        self.assertEqual(DEFAULT_SYSTEM_PROMPT, ARCUEID_PERSONA)

    async def test_recovery_prompt_passes_default_persona_to_llm(self) -> None:
        captured = {}

        class Provider:
            async def complete(self, messages, **kwargs):
                captured["messages"] = messages
                captured["kwargs"] = kwargs
                return SimpleNamespace(message={"content": "检测到未完成的操作喵~ 尚未执行，请回复确认或取消。"})

        result = await _generate_recovery_prompt(
            Provider(),
            SimpleNamespace(title="创建定时任务", tool="tasks__create"),
            "检测到未完成的高风险操作：创建定时任务。该操作尚未执行，请回复确认或取消。",
        )

        assert "爱尔奎特" in captured["messages"][0]["content"]
        assert "回复不要使用括号动作" in captured["messages"][0]["content"]
        assert "尚未执行" in captured["messages"][0]["content"]
        assert captured["messages"][1]["content"].startswith("检测到未完成")
        assert captured["kwargs"]["tools"] == []
        assert "喵" in result

    async def test_recovery_prompt_uses_custom_persona_when_supplied(self) -> None:
        captured = {}

        class Provider:
            async def complete(self, messages, **kwargs):
                captured["system"] = messages[0]["content"]
                return SimpleNamespace(message={"content": "操作尚未执行，请确认或取消。"})

        await _generate_recovery_prompt(
            Provider(),
            SimpleNamespace(title="删除文件", tool="file__delete"),
            "删除文件尚未执行，请回复确认或取消。",
            persona="你是一位沉稳的管家，语气克制。",
        )

        assert "沉稳的管家" in captured["system"]
        assert "猫娘" not in captured["system"]

    async def test_local_time_command_is_parsed_deterministically(self) -> None:
        self.assertEqual(_parse_confirmation_command("现在几点"), ("local_time", None))
        self.assertEqual(_parse_confirmation_command("当前时间"), ("local_time", None))
        self.assertEqual(_parse_confirmation_command("本地时间"), ("local_time", None))

    async def test_local_time_result_shows_local_source(self) -> None:
        output = _format_local_time_result({"local_time": "2030-01-01T20:00:00+08:00", "timezone": "Asia/Shanghai", "source": "local"})
        self.assertIn("2030-01-01T20:00:00+08:00", output)
        self.assertIn("Asia/Shanghai", output)
        self.assertIn("local", output)

    async def test_due_event_format_contains_title_and_request(self) -> None:
        event = Event(
            EventType.TASK_DUE,
            datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc),
            {"owner_id": "owner", "channel": "desktop", "conversation_id": "chat", "module_id": "companion"},
            {"task_id": "task-123", "title": "喝水提醒", "request_text": "请喝水"},
        )
        output = _format_task_due_event(event)
        self.assertIn("任务到点", output)
        self.assertIn("喝水提醒", output)
        self.assertIn("请喝水", output)

    async def test_due_subscription_only_prints_for_the_current_scope(self) -> None:
        bus = InMemoryEventBus()
        scope = Scope("owner", "desktop", "chat")
        with mock.patch("chat_cli.print") as output:
            _subscribe_task_due_output(bus, scope)
            await bus.emit(Event(EventType.TASK_DUE, datetime.now(timezone.utc), {
                "owner_id": "owner", "channel": "desktop", "conversation_id": "other", "module_id": "companion",
            }, {"title": "other", "request_text": "ignore"}))
            await bus.emit(Event(EventType.TASK_DUE, datetime.now(timezone.utc), {
                "owner_id": "owner", "channel": "desktop", "conversation_id": "chat", "module_id": "companion",
            }, {"title": "mine", "request_text": "show"}))
            await asyncio.sleep(0)

        self.assertEqual(output.call_count, 1)
        self.assertIn("mine", output.call_args.args[0])

    async def test_due_subscription_batches_events_from_one_loop_turn(self) -> None:
        bus = InMemoryEventBus()
        scope = Scope("owner", "desktop", "chat")
        with mock.patch("chat_cli.print") as output:
            _subscribe_task_due_output(bus, scope)
            for title in ("喝水", "会议"):
                await bus.emit(Event(EventType.TASK_DUE, datetime.now(timezone.utc), {
                    "owner_id": "owner", "channel": "desktop", "conversation_id": "chat", "module_id": "companion",
                }, {"title": title, "request_text": title}))
            await asyncio.sleep(0)

        self.assertEqual(output.call_count, 1)
        rendered = output.call_args.args[0]
        self.assertIn("喝水", rendered)
        self.assertIn("会议", rendered)

    async def test_runtime_time_context_uses_the_fresh_local_clock_result(self) -> None:
        output = _runtime_time_context(lambda: {
            "status": "ok",
            "output": {"local_time": "2030-01-01T20:00:00+08:00", "timezone": "Asia/Shanghai", "source": "local"},
        })

        self.assertIn("2030-01-01T20:00:00+08:00", output)
        self.assertIn("仅用该时间理解相对时间", output)

    async def test_request_attaches_runtime_time_without_changing_user_text(self) -> None:
        request = _request_with_runtime_time(
            "今晚提醒我", Scope("owner", "desktop", "chat"),
            reader=lambda: {"status": "ok", "output": {"local_time": "2030-01-01T20:00:00+08:00", "timezone": "Asia/Shanghai", "source": "local"}},
        )

        self.assertEqual(request.user_text, "今晚提醒我")
        self.assertIn("2030-01-01T20:00:00+08:00", request.persona_override)
        self.assertNotIn("2030-01-01T20:00:00+08:00", request.history)
    async def test_startup_messages_do_not_enumerate_pending_confirmations(self) -> None:
        output = "\n".join(_startup_messages())

        self.assertIn("伴生已就绪", output)
        self.assertNotIn("待确认", output)
        self.assertNotIn("检测到", output)

    async def test_recovery_notice_says_action_has_not_run(self) -> None:
        notice = SimpleNamespace(message="检测到未完成的高风险操作：创建任务。该操作尚未执行。")

        output = _format_recovery_notice(notice)

        self.assertIn("尚未执行", output)
        self.assertIn("创建任务", output)

    async def test_tool_banner_does_not_expose_internal_modules(self) -> None:
        output = _format_tool_registration(["search__web", "system__local_info"])

        self.assertIn("已注册 2 个工具", output)
        self.assertIn("search__web", output)
        self.assertNotIn("模块", output)

    async def test_unavailable_task_query_does_not_expose_internal_modules(self) -> None:
        # The user-facing fallback text is intentionally capability-oriented.
        output = _format_unavailable_capability("任务")
        self.assertNotIn("模块", output)

    async def test_local_command_parser_requires_an_index_for_multiple_confirmations(self) -> None:
        self.assertEqual(_parse_confirmation_command("查看待确认"), ("list_confirmations", None))
        self.assertEqual(_parse_confirmation_command("确认第1条"), ("confirm", 1))
        self.assertEqual(_parse_confirmation_command("确认 2"), ("confirm", 2))
        self.assertEqual(_parse_confirmation_command("确认"), ("confirm", None))
        self.assertEqual(_parse_confirmation_command("明确确认"), ("confirm", None))
        self.assertEqual(_parse_confirmation_command("重置待确认"), ("reset", None))
        self.assertEqual(_parse_confirmation_command("查看当前任务"), ("list_tasks", None))
        self.assertEqual(_parse_confirmation_command("帮我查看待处理任务"), ("list_tasks", None))
        self.assertEqual(_parse_confirmation_command("现在有什么任务"), ("list_tasks", None))
        self.assertEqual(_parse_confirmation_command("设置了吗"), ("list_tasks", None))
        self.assertEqual(_parse_confirmation_command("提醒了吗"), ("list_tasks", None))
        self.assertEqual(_parse_confirmation_command("查看记忆库"), ("memory_stats", None))
        self.assertEqual(_parse_confirmation_command("记忆库有多少条"), ("memory_stats", None))
        self.assertEqual(_parse_confirmation_command("记忆有多少"), ("memory_stats", None))
        self.assertEqual(_parse_confirmation_command("当前记忆数量"), ("memory_stats", None))
        self.assertEqual(_parse_confirmation_command("记忆统计"), ("memory_stats", None))

    async def test_pending_confirmation_format_redacts_sensitive_arguments(self) -> None:
        pending = [
            SimpleNamespace(
                tool="tasks__create",
                title="创建定时任务",
                arguments={"request_text": "提醒我休息", "secret": "do-not-display"},
                code="ABC12345",
            )
        ]

        output = _format_pending_confirmations(pending)

        self.assertIn("[1]", output)
        self.assertIn("tasks__create", output)
        self.assertIn("ABC12345", output)
        self.assertNotIn("do-not-display", output)

    async def test_task_format_contains_status_and_next_run(self) -> None:
        task = SimpleNamespace(
            title="晚上八点提醒", delivery_mode="notify", status="scheduled",
            next_run_at="2026-08-24T12:00:00+00:00", last_error=None,
            task_id="task-1234567890",
        )

        output = _format_tasks([task])

        self.assertIn("晚上八点提醒", output)
        self.assertIn("scheduled", output)
        self.assertIn("2026-08-24T12:00:00+00:00", output)

    async def test_condition_task_format_contains_city_status_and_next_check(self) -> None:
        task = SimpleNamespace(
            title="晒被子", city="上海", condition="weather_clear", status="scheduled",
            next_check_at="2026-08-24T12:00:00+00:00", last_summary="天气条件不明确", last_error=None,
            task_id="condition-1234567890",
        )

        output = _format_condition_tasks([task])

        self.assertIn("晒被子", output)
        self.assertIn("上海", output)
        self.assertIn("scheduled", output)
        self.assertIn("天气条件不明确", output)

    async def test_confirmation_decision_recognizes_explicit_confirm_and_reject_text(self) -> None:
        self.assertEqual(_confirmation_decision("确认设置。晚上八点吧"), "confirmed")
        self.assertEqual(_confirmation_decision("取消设置"), "rejected")
        self.assertEqual(_confirmation_decision("重置待确认"), "reset")
        self.assertIsNone(_confirmation_decision("设置了吗？"))

    async def test_output_contains_conversation_and_turn_identifiers(self) -> None:
        scope = Scope("owner", "desktop", "chat-a")
        handle = SimpleNamespace(turn_id="turn-123456789", result=asyncio.get_running_loop().create_future())
        handle.result.set_result(AgentOutcome("completed", "run-1", reply="hello"))

        output = _format_outcome(scope, handle, await handle.result)

        self.assertIn("chat-a", output)
        self.assertIn("turn-123", output)
        self.assertIn("hello", output)

    async def test_paused_outcome_explains_how_to_confirm_locally(self) -> None:
        scope = Scope("owner", "desktop", "chat-a")
        handle = SimpleNamespace(turn_id="turn-123456789")

        output = _format_outcome(scope, handle, AgentOutcome("paused", "run-1"))

        self.assertIn("查看待确认", output)
        self.assertIn("确认第1条", output)

    async def test_shutdown_closes_manager_before_repository(self) -> None:
        events: list[str] = []

        class Manager:
            async def shutdown(self) -> None:
                events.append("manager")

        class Repository:
            def close(self) -> None:
                events.append("repository")

        await _shutdown_cli(Manager(), Repository(), set())

        self.assertEqual(events, ["manager", "repository"])

    async def test_shutdown_stops_condition_service(self) -> None:
        events: list[str] = []

        class Manager:
            async def shutdown(self) -> None:
                events.append("manager")

        class Repository:
            def close(self) -> None:
                events.append("repository")

        class ConditionService:
            async def stop(self) -> None:
                events.append("conditions")

        await _shutdown_cli(Manager(), Repository(), set(), condition_task_service=ConditionService())

        self.assertEqual(events, ["manager", "conditions", "repository"])


class UnplugTests(unittest.TestCase):
    """拔插隔离：agent_tools/ 缺失时主体照样 import、照常运行（隔离的核心承诺）。"""

    def test_chat_cli_imports_when_agent_tools_missing(self) -> None:
        # sys.modules["agent_tools"] = None 会让 import agent_tools 抛 ImportError，
        # 等价于整个 agent_tools/ 目录被拔掉。
        #
        # **2026-09-21 改写**：原来断言的是 `module.register_tools is None`
        # （那个主动登记函数已随能力目录一起删掉）。
        # 被保证的承诺没变 —— **拔掉 agent_tools/ 时 CLI 照样能 import、不抛异常**，
        # 只是"无工具状态"现在由 `agent_tools is None` 表达，
        # 因为 CLI 不再 import 任何具体的登记函数，只 import 包本身。
        with mock.patch.dict(sys.modules, {"agent_tools": None}):
            module = importlib.reload(importlib.import_module("chat_cli"))
        self.assertIsNone(module.agent_tools), "拔包后 agent_tools 应为 None，而不是 import 抛错"

        # 恢复正常：让后续测试拿到真实的可插拔包
        restored = importlib.reload(importlib.import_module("chat_cli"))
        self.assertIsNotNone(restored.agent_tools)


if __name__ == "__main__":
    unittest.main()
