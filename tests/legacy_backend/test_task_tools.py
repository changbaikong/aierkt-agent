"""任务工具 executor 测试：task_create / task_list / task_get / task_cancel。

覆盖：
- 已授权账号（桌面 local-user）可创建/取消任务
- 微信绑定主人（已授权）可创建/取消任务
- 未授权渠道被拒绝执行写操作，但可读（task_list / task_get）
"""

from __future__ import annotations

import unittest

from backend.memory import MemoryScope
from backend.task_tools import create_task_tool_executor
from backend.tool_authorization import authorize_owner, deauthorize_owner


class TaskToolExecutorTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        deauthorize_owner("wechat:user-1")

    async def test_local_user_can_create_task(self) -> None:
        executor = create_task_tool_executor(MemoryScope(owner_id="local-user"))
        result = await executor("task_create", {"title": "买水果", "request_text": "明天提醒我买水果"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("work_item_id", result["output"])

    async def test_authorized_wechat_owner_can_create_task(self) -> None:
        authorize_owner("wechat:user-1")
        executor = create_task_tool_executor(MemoryScope(owner_id="wechat:user-1", channel="wechat"))
        result = await executor("task_create", {"title": "写周报", "request_text": "帮我写一份周报"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("work_item_id", result["output"])

    async def test_unauthorized_channel_cannot_create_task(self) -> None:
        executor = create_task_tool_executor(MemoryScope(owner_id="wechat:stranger-9", channel="wechat"))
        result = await executor("task_create", {"title": "越权", "request_text": "偷偷建任务"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("权限", result["error"])

    async def test_unauthorized_channel_cannot_cancel_task(self) -> None:
        executor = create_task_tool_executor(MemoryScope(owner_id="wechat:stranger-9", channel="wechat"))
        result = await executor("task_cancel", {"work_item_id": "whatever"})
        self.assertEqual(result["status"], "failed")
        self.assertIn("权限", result["error"])

    async def test_unauthorized_channel_can_list_tasks(self) -> None:
        """只读操作对未授权渠道仍可用。"""
        executor = create_task_tool_executor(MemoryScope(owner_id="wechat:stranger-9", channel="wechat"))
        result = await executor("task_list", {})
        self.assertEqual(result["status"], "ok")
        self.assertIn("tasks", result["output"])

    async def test_unauthorized_channel_can_get_task(self) -> None:
        executor = create_task_tool_executor(MemoryScope(owner_id="wechat:stranger-9", channel="wechat"))
        result = await executor("task_get", {"work_item_id": "nonexistent"})
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["output"]["found"])

    async def test_cancel_non_cancellable_status(self) -> None:
        """已完成任务不能被取消（authorized）。"""
        executor = create_task_tool_executor(MemoryScope(owner_id="local-user"))
        created = await executor("task_create", {"title": "临时", "request_text": "临时任务"})
        work_item_id = created["output"]["work_item_id"]
        # 直接落库为 completed，模拟已结束任务
        from backend.agent_runtime import get_agent_runtime

        runtime = get_agent_runtime()
        runtime.mark_work_item_terminal(work_item_id, "completed")
        result = await executor("task_cancel", {"work_item_id": work_item_id})
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["output"]["cancelled"])
        self.assertIn("无法取消", result["output"]["message"])


if __name__ == "__main__":
    unittest.main()
