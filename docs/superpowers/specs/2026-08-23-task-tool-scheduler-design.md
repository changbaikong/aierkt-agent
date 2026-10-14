# 任务工具与持久化调度器设计

## Summary

The task module adds durable reminders and scheduled LLM execution to the existing Agent architecture. Task definitions are independent of memory and are isolated by the complete `Scope` identity.

## Interfaces

- `tasks__create(title, request_text, delivery_mode, schedule, allowed_tools?)`
- `tasks__list(status?, limit?)`
- `tasks__get(task_id, include_runs?)`
- `tasks__cancel(task_id)`
- `tasks__run_now(task_id)`
- `system__local_info(kind?)`

Plans are normalized to one of `run_at`, `interval_seconds`, or five-field cron plus an IANA timezone. Repeating plans calculate the next UTC trigger after each successful run.

## Runtime

SQLite stores task definitions, execution history, and leases. The in-process scheduler claims due tasks with a conditional update. Notification tasks emit `task.due`; execution tasks build an Agent loop from a creation-time allowlist snapshot. Task management and high-risk tools are rejected for background execution.

The lifecycle emits `task.started`, `task.completed`, `task.failed`, and `task.cancelled`. Results remain queryable through `tasks__get` and are not coupled to a specific chat channel.

## Scope and safety

All task reads and writes require an exact owner/channel/conversation/module scope. Local machine information is read-only and reports `source=local`. The first version does not expose CPU, memory, disk, external queues, automatic unbounded retries, or network-time fallback.

## Verification

Tests cover schedule validation, scope isolation, atomic claims, notification events, tool allowlist enforcement, namespaced registration, and local information output. The full existing test suite remains the regression gate.
