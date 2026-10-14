"""Task management tools backed by TaskService."""

from __future__ import annotations

from typing import Any

from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, AccountTaskScope, Scope, ToolSpec
from agent_core.task_service import TaskService
from agent_core.tools import classify_tool_error


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}}


_SCHEDULE_SCHEMA = {
    "type": "object",
    "description": (
        "Use exactly one schedule form: run_at (an ISO-8601 future timestamp with timezone offset), "
        "interval_seconds (at least 60), or a five-field cron expression. "
        "For Chinese users, always use timezone Asia/Shanghai and +08:00 offset. "
        "Example for tomorrow 3pm: {\"run_at\": \"2026-09-01T15:00:00+08:00\", \"timezone\": \"Asia/Shanghai\"}"
    ),
    "properties": {
        "run_at": {
            "type": "string",
            "description": (
                "Future ISO-8601 timestamp with timezone offset. "
                "Format: YYYY-MM-DDTHH:MM:SS+08:00 (for China/Asia). "
                "Example: 2026-08-31T14:30:00+08:00 for Aug 31 2026 at 2:30pm Beijing time."
            )
        },
        "interval_seconds": {"type": "integer", "minimum": 60, "description": "Repeat interval in seconds (minimum 60). Example: 3600 for hourly."},
        "cron": {"type": "string", "description": "Five-field cron expression (minute hour day month weekday). Example: '0 7 * * *' for daily 7am."},
        "timezone": {"type": "string", "description": "IANA timezone name. For Chinese users always use 'Asia/Shanghai'."},
    },
}


def make_task_tools(service: TaskService) -> list[ToolSpec]:
    def account_scope(scope: Scope) -> AccountTaskScope:
        return AccountTaskScope(scope.owner_id, scope.module_id)

    async def create(args: dict[str, Any], scope: Scope) -> dict[str, Any]:
        try:
            task = await service.create_task(
                scope=scope, title=str(args.get("title") or ""), request_text=str(args.get("request_text") or ""),
                delivery_mode=str(args.get("delivery_mode") or "notify"), schedule=dict(args.get("schedule") or {}),
                allowed_tools=list(args.get("allowed_tools") or []),
            )
            return {"status": "ok", "output": {"task_id": task.task_id, "status": task.status, "next_run_at": task.next_run_at}}
        except Exception as exc:
            return {"status": "failed", "error": str(exc), "error_kind": classify_tool_error(exc)}

    async def list_tasks(args: dict[str, Any], scope: Scope) -> dict[str, Any]:
        rows = service.store.list_tasks(account_scope(scope), str(args.get("status")) if args.get("status") else None, int(args.get("limit") or 50))
        return {"status": "ok", "output": {"tasks": [_task_dict(row) for row in rows]}}

    async def get(args: dict[str, Any], scope: Scope) -> dict[str, Any]:
        task_scope = account_scope(scope)
        task = service.store.get_task(str(args.get("task_id") or ""), task_scope)
        if task is None:
            return {"status": "ok", "output": {"found": False}}
        output = _task_dict(task)
        if args.get("include_runs"):
            output["runs"] = [run.__dict__ for run in service.store.runs(task.task_id, task_scope)]
        return {"status": "ok", "output": {"found": True, **output}}

    async def cancel(args: dict[str, Any], scope: Scope) -> dict[str, Any]:
        task_id = str(args.get("task_id") or "")
        changed = await service.cancel_task(task_id, account_scope(scope))
        return {"status": "ok", "output": {"cancelled": changed, "task_id": task_id}}

    async def run_now(args: dict[str, Any], scope: Scope) -> dict[str, Any]:
        task_id = str(args.get("task_id") or "")
        changed = await service.run_now(task_id, account_scope(scope))
        return {"status": "ok" if changed else "failed", "output": {"queued": changed, "task_id": task_id}, "error": "task not found or terminal" if not changed else "", "error_kind": "" if changed else "permanent"}

    return [
        ToolSpec(
            "create",
            _schema(
                "create",
                "Create a reminder or scheduled background task. Use this when user asks to be reminded or to schedule something.",
                {
                    "title": {"type": "string", "description": "Short task title, for example '提醒：开会' or '每日天气'."},
                    "request_text": {"type": "string", "description": "What to remind or what task to execute, for example '提醒我去开会' or '获取天气预报'."},
                    "delivery_mode": {"type": "string", "enum": ["notify", "execute"], "description": "Use 'notify' for simple reminders, 'execute' for background tasks."},
                    "schedule": _SCHEDULE_SCHEMA,
                    "allowed_tools": {"type": "array", "items": {"type": "string"}, "description": "Tools the background task can use (only needed for execute mode)."},
                },
                ["title", "request_text", "schedule"]
            ),
            create,
            risk="high",
            effect=EFFECT_PERSISTENT,
            confirmation_title="Create scheduled task"
        ),
        # ── 只读工具：可以走确定性路由（不经过模型直接回答）──
        # list 是"有哪些任务"这类问句的出口，答案有确定性、瞬间可得，
        # 不该为它调一次模型。标记必须打在**正式模块**上：只给测试里的
        # 手工 spec 打标记，真实链路上照样回落给模型（2026-09-14 的回归）。
        ToolSpec(
            "list",
            _schema("list", "List tasks in the current scope", {"status": {"type": "string"}, "limit": {"type": "integer"}}, []),
            list_tasks,
            risk="low",
            effect=EFFECT_READ,
            deterministic_route_allowed=True,
        ),
        # get 也读，但它**必须要 task_id**：只有模型从 list 结果里拿到 id
        # 之后才调得动，所以只标只读、不开放确定性路由。
        ToolSpec(
            "get",
            _schema("get", "Get a task and optional run history", {"task_id": {"type": "string"}, "include_runs": {"type": "boolean"}}, ["task_id"]),
            get,
            effect=EFFECT_READ,
        ),
        ToolSpec("cancel", _schema("cancel", "Cancel a scheduled task", {"task_id": {"type": "string"}}, ["task_id"]), cancel, risk="high", effect=EFFECT_PERSISTENT, confirmation_title="Cancel scheduled task"),
        ToolSpec("run_now", _schema("run_now", "Run a scheduled task immediately", {"task_id": {"type": "string"}}, ["task_id"]), run_now, risk="high", effect=EFFECT_PERSISTENT, confirmation_title="Run scheduled task now"),
    ]


def _task_dict(task: Any) -> dict[str, Any]:
    return {"task_id": task.task_id, "title": task.title, "request_text": task.request_text, "delivery_mode": task.delivery_mode, "schedule": task.schedule, "allowed_tools": task.allowed_tools, "status": task.status, "next_run_at": task.next_run_at, "last_error": task.last_error, "created_at": task.created_at, "updated_at": task.updated_at}
