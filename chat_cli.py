from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from agent_core import (
    AgentLoop,
    AgentRunner,
    AgentRequest,
    ConversationManager,
    MemoryAgentSession,
    ModuleRegistry,
    PromptComposer,
    Scope,
    ToolRegistry,
)
from agent_core.contracts import AccountTaskScope, AgentOutcome, ConfirmationRequest
from agent_core.events import Event, EventType, InMemoryEventBus
from agent_core.memory_repositories import MemoryRepositories, initialize_memory_repositories
from agent_core.memory_retrieval import MemoryRetriever
from agent_core.memory_writes import MemoryWriter
from agent_core.intent import SQLiteIntentStateStore
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.search_gate import build_search_gate
from agent_core.outbox import OutboxStore
from agent_core.steward import (
    STEWARD_PROGRESS_KIND,
    StewardNarrator,
    StewardReporter,
)
from agent_core.personas import ARcueid_PERSONA
from agent_core.permission_mode import PermissionLayer
from agent_core.capability_runtime import RuntimeBootstrapError, build_runtime
from agent_core.work_module import attach_code_agent_memory_retriever
from agent_core.prompts import DEFAULT_PRIMARY_PERSONA, build_relative_time_context
from agent_core.project_env import load_project_env
from agent_core_adapters.bigmodel_embeddings import BigModelEmbeddingProvider
from agent_core_adapters.openai_compatible import OpenAICompatibleProvider

try:
    # agent_tools/ 是可插拔包：目录的能力条目在 import 时才注册，
    # 拔掉整个目录就只是"没有工具"，CLI 照常运行。
    import agent_tools  # noqa: F401
except ImportError:
    agent_tools = None  # type: ignore[assignment]


load_project_env()


def _ensure_utf8_stdio() -> None:
    """Windows \u63a7\u5236\u53f0\u9ed8\u8ba4\u7528 GBK(cp936) \u8f93\u51fa\uff0c\u6253\u5370\u4e2d\u6587\u4f1a\u4e71\u7801\uff1b\u7edf\u4e00\u5f3a\u5236 UTF-8\u3002

    \u7528 errors="replace" \u515c\u5e95\uff1a\u4e07\u4e00\u9047\u5230\u65e0\u6cd5\u7f16\u7801\u7684\u5b57\u7b26\uff0c\u663e\u793a \ufffd \u800c\u4e0d\u662f\u5d29\u6e83\u3002
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


# 默认人设；可在 .env 里用 AGENT_SYSTEM_PROMPT 覆盖（不配就用网页版爱尔奎特）
DEFAULT_SYSTEM_PROMPT = ARcueid_PERSONA
PERSONA_OVERRIDE = os.environ.get("AGENT_SYSTEM_PROMPT", "").strip()
OUTPUT_MODE = os.environ.get("AGENT_OUTPUT_MODE", "text").strip().lower()
if OUTPUT_MODE not in {"text", "tts"}:
    print(f"[chat_cli] invalid AGENT_OUTPUT_MODE={OUTPUT_MODE!r}; using text")
    OUTPUT_MODE = "text"


def _confirmation_decision(text: str) -> str | None:
    """Return a decision only for explicit confirmation or rejection language."""
    normalized = text.strip().lower()
    if not normalized:
        return None
    if re.search(r"重置待确认|清除待确认|清理待确认|重置遗留确认", normalized):
        return "reset"
    if re.search(r"取消|拒绝|不用了|不设置|不需要|不要了|算了|不确认", normalized):
        return "rejected"
    if re.search(r"确认|确定|同意|批准", normalized):
        return "confirmed"
    return None


def _parse_confirmation_command(text: str) -> tuple[str | None, int | None]:
    """Parse deterministic local confirmation/task commands."""
    normalized = re.sub(r"[。！？!?,，、\s]+", "", text.strip().lower())
    if not normalized:
        return None, None
    if normalized in {"查看待确认", "有哪些待确认", "待确认有哪些", "查看遗留待确认"}:
        return "list_confirmations", None
    if normalized in {
        "查看当前任务", "当前任务", "任务列表", "有哪些任务", "查看任务",
        "帮我查看待处理任务", "现在有什么任务",
        "设置了吗", "定了吗", "提醒了吗", "闹钟设置了吗",
    }:
        return "list_tasks", None
    if normalized in {"查看记忆库", "记忆库有多少条", "记忆有多少", "当前记忆数量", "记忆统计"}:
        return "memory_stats", None
    if normalized in {"现在几点", "现在几点钟", "当前时间", "本地时间"}:
        return "local_time", None
    if normalized in {"重置待确认", "清除待确认", "清理待确认", "重置遗留确认"}:
        return "reset", None
    match = re.fullmatch(r"确认第(\d+)条", normalized) or re.fullmatch(r"确认(\d+)", normalized)
    if match:
        return "confirm", int(match.group(1))
    if normalized in {"确认", "确认设置", "明确确认", "确定", "同意", "批准"}:
        return "confirm", None

    # 第二层：关键词子串匹配（覆盖常见变体）
    # 顺序很重要：reset 要在 list_confirmations 之前，因为"清理待确认"是 reset 不是 list
    if "记忆" in normalized and any(k in normalized for k in ("统计", "多少", "几条", "数量", "看看", "查", "数据", "概况")):
        return "memory_stats", None
    if "任务" in normalized and any(k in normalized for k in ("列表", "查看", "有哪些", "现在", "什么")):
        return "list_tasks", None
    if ("重置" in normalized or "清除" in normalized or "清理" in normalized) and ("待确认" in normalized or "确认" in normalized):
        return "reset", None
    if "待确认" in normalized or ("确认" in normalized and any(k in normalized for k in ("查看", "哪些", "有"))):
        return "list_confirmations", None
    if "时间" in normalized or "几点" in normalized:
        return "local_time", None

    return None, None


def _confirmation_argument_summary(arguments: dict[str, Any]) -> str:
    """Render an allowlisted, bounded summary without arbitrary argument leakage."""
    safe_keys = {"title", "request_text", "schedule", "delivery_mode", "allowed_tools"}
    safe = {key: arguments[key] for key in safe_keys if key in arguments}
    if isinstance(safe.get("request_text"), str):
        safe["request_text"] = safe["request_text"][:80]
    rendered = json.dumps(safe, ensure_ascii=False, default=str, separators=(",", ":"))
    return rendered if len(rendered) <= 180 else rendered[:177] + "..."


def _format_pending_confirmations(pending: list[Any]) -> str:
    if not pending:
        return "当前没有待确认请求。"
    lines = ["待确认请求："]
    for index, request in enumerate(pending, 1):
        lines.extend(
            [
                f"[{index}] {request.title}",
                f"    工具：{request.tool}",
                f"    参数摘要：{_confirmation_argument_summary(request.arguments)}",
                f"    确认码：{request.code}",
            ]
        )
    if len(pending) > 1:
        lines.append("请回复“确认第1条”等明确编号；直接说“确认”不会默认选择。")
    else:
        lines.append("请回复“确认”继续，或回复“重置待确认”清理它。")
    return "\n".join(lines)


def _format_tasks(tasks: list[Any]) -> str:
    if not tasks:
        return "当前没有已创建的任务。"
    lines = ["当前任务："]
    for index, task in enumerate(tasks, 1):
        next_run = _format_task_time(task)
        line = f"[{index}] {task.title} | {task.delivery_mode} | {task.status} | 下一次：{next_run}"
        if task.last_error:
            line += f" | 错误：{str(task.last_error)[:120]}"
        lines.append(line)
    return "\n".join(lines)


def _format_condition_tasks(tasks: list[Any]) -> str:
    if not tasks:
        return "当前没有条件任务。"
    lines = ["条件任务："]
    for index, task in enumerate(tasks, 1):
        next_check = _format_condition_task_time(task)
        line = (
            f"[{index}] {task.title} | {getattr(task, 'city', '未知城市')} | "
            f"{getattr(task, 'condition', 'condition')} | {task.status} | 下次检查：{next_check}"
        )
        if getattr(task, "last_summary", None):
            line += f" | 最近结果：{str(task.last_summary)[:120]}"
        if getattr(task, "last_error", None):
            line += f" | 错误：{str(task.last_error)[:120]}"
        lines.append(line)
    return "\n".join(lines)


def _format_memory_stats(stats: dict[str, int]) -> str:
    return (
        "记忆库统计：\n"
        f"有效记忆：{int(stats.get('total', 0))} 条\n"
        f"hot：{int(stats.get('hot', 0))} 条\n"
        f"warm：{int(stats.get('warm', 0))} 条\n"
        f"cold：{int(stats.get('cold', 0))} 条\n"
        f"非有效记录：{int(stats.get('non_active', 0))} 条"
    )


def _format_task_time(task: Any) -> str:
    if not task.next_run_at:
        return "已触发/无下一次" if task.status in {"completed", "cancelled", "failed"} else "无"
    try:
        value = datetime.fromisoformat(str(task.next_run_at).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=ZoneInfo("UTC"))
        timezone_name = str((task.schedule or {}).get("timezone") or "UTC")
        value = value.astimezone(ZoneInfo(timezone_name))
        return value.isoformat(timespec="seconds")
    except Exception:
        return str(task.next_run_at)


def _format_condition_task_time(task: Any) -> str:
    value = getattr(task, "next_check_at", None)
    if not value:
        return "已触发/无下一次" if getattr(task, "status", "") in {"completed", "cancelled", "failed", "blocked"} else "无"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo(str(getattr(task, "timezone", "UTC")))).isoformat(timespec="seconds")
    except Exception:
        return str(value)


def _format_local_time_result(output: dict[str, Any]) -> str:
    return f"当前本机时间：{output.get('local_time', '未知')} | 时区：{output.get('timezone', '未知')} | 来源：{output.get('source', 'local')}"


_runtime_time_context = build_relative_time_context


def _request_with_runtime_time(
    user_text: str, scope: Scope, *, reader: Callable[[], dict[str, Any]] | None = None,
    delivery_target: str = "",
) -> AgentRequest:
    base_persona = PERSONA_OVERRIDE or DEFAULT_SYSTEM_PROMPT
    return AgentRequest(
        user_text=user_text,
        scope=scope,
        persona_override=f"{base_persona}\n\n{_runtime_time_context(reader)}",
        account_id=scope.owner_id,
        delivery_target=delivery_target,
    )


def _format_task_due_event(event: Event) -> str:
    return _format_task_due_events([event])


def _format_task_due_events(events: list[Event]) -> str:
    lines = ["\n[任务到点]"]
    for event in events:
        data = event.data
        city = f" | 城市：{data['city']}" if data.get("city") else ""
        summary = f" | 判定：{data['summary']}" if data.get("summary") else ""
        triggered = event.timestamp.astimezone().isoformat(timespec="seconds")
        lines.append(
            f"- {data.get('title') or '未命名任务'}：{data.get('request_text') or '无'}"
            f"{city}{summary} | 触发时间：{triggered}"
        )
    return "\n".join(lines) + "\n"


def _subscribe_task_due_output(event_bus: InMemoryEventBus, scope: Scope, *, outbox: OutboxStore | None = None, delivery_target: str = "unified") -> str:
    """Print only due events that belong to this CLI conversation."""
    pending: list[Event] = []
    scheduled = False

    def flush() -> None:
        nonlocal scheduled
        scheduled = False
        events = list(pending)
        pending.clear()
        if events:
            print(_format_task_due_events(events), flush=True)

    def output_due(event: Event) -> None:
        nonlocal scheduled
        if outbox is not None:
            outbox.enqueue(
                account_id=scope.owner_id,
                delivery_target=delivery_target,
                event_type=event.type.value,
                payload={"task_id": event.data.get("task_id"), "title": event.data.get("title"), "request_text": event.data.get("request_text"), "city": event.data.get("city"), "summary": event.data.get("summary"), "triggered_at": event.timestamp.isoformat()},
                dedupe_key=f"{event.data.get('task_id')}:{event.data.get('run_id') or event.timestamp.isoformat()}",
            )
        pending.append(event)
        if not scheduled:
            scheduled = True
            asyncio.get_running_loop().call_soon(flush)

    return event_bus.subscribe(
        EventType.TASK_DUE,
        output_due,
        owner_id=scope.owner_id,
        channel=scope.channel,
        conversation_id=scope.conversation_id,
        module_id=scope.module_id,
    )


def _subscribe_task_due_outbox(event_bus: InMemoryEventBus, account_id: str, module_id: str, outbox: OutboxStore, delivery_target: str) -> str:
    def enqueue(event: Event) -> None:
        outbox.enqueue(
            account_id=account_id,
            delivery_target=delivery_target,
            event_type=event.type.value,
            payload={"task_id": event.data.get("task_id"), "title": event.data.get("title"), "request_text": event.data.get("request_text"), "city": event.data.get("city"), "summary": event.data.get("summary"), "triggered_at": event.timestamp.isoformat()},
            dedupe_key=f"{event.data.get('task_id')}:{event.data.get('run_id') or event.timestamp.isoformat()}",
        )

    return event_bus.subscribe(EventType.TASK_DUE, enqueue, owner_id=account_id, module_id=module_id)


def _startup_messages() -> list[str]:
    """Return the fixed startup banner before durable recovery notices."""
    return [
        "伴生已就绪。输入 /exit 退出，/history 查看本会话历史，/clear 清空提示历史。",
        "回复生成期间你可以继续输入；多轮消息按顺序处理。",
    ]


def _format_tool_registration(tool_names: list[str]) -> str:
    """Render the user-facing capability banner without internal module terms."""
    return f"[chat_cli] 已注册 {len(tool_names)} 个工具：{', '.join(tool_names) if tool_names else '(无)'}"


def _format_unavailable_capability(capability: str) -> str:
    return f"{capability}功能当前不可用。"


def _format_outcome(scope: Scope, handle: Any, outcome: AgentOutcome) -> str:
    prefix = f"[{scope.conversation_id}][turn {handle.turn_id[:8]}]"
    if outcome.status == "completed":
        return f"{prefix} assistant: {outcome.reply}"
    if outcome.status == "paused":
        return (
            f"{prefix} paused: 该操作正在等待确认。"
            "输入“查看待确认”查看内容；只有一条时输入“确认”，多条时输入“确认第1条”。"
        )
    return f"{prefix} {outcome.status}: {outcome.reply}"


def _format_recovery_notice(notice: Any) -> str:
    """Render a recovery prompt without exposing raw persisted arguments."""
    return str(notice.message)


async def _generate_recovery_prompt(
    provider: Any,
    request: ConfirmationRequest,
    base_template: str,
    *,
    persona: str | None = None,
) -> str:
    """Ask the model to phrase a safe recovery question in the active persona."""
    active_persona = (persona or DEFAULT_PRIMARY_PERSONA).strip()
    response = await provider.complete(
        [
            {
                "role": "system",
                "content": (
                    f"当前人物设定：{active_persona}\n"
                    "请严格基于给定模板，用简短中文并符合人物设定自然改写恢复提示。"
                    "可以加入轻度人物语气，例如自然的‘喵~’，但必须保留尚未执行、明确确认和取消语义；"
                    "不要声称已执行，不要增加参数，不要提及未给出的参数，不要调用工具。"
                ),
            },
            {"role": "user", "content": base_template},
        ],
        tools=[],
        max_tokens=120,
        temperature=0.2,
    )
    content = (response.message or {}).get("content")
    return str(content or "").strip()


async def _watch_turn(scope: Scope, handle: Any) -> None:
    outcome = await handle.result
    print(_format_outcome(scope, handle, outcome), flush=True)


async def _shutdown_cli(
    manager: ConversationManager,
    repositories: MemoryRepositories,
    output_tasks: set[asyncio.Task[None]],
    task_service: Any = None,
    condition_task_service: Any = None,
    module_registry: ModuleRegistry | None = None,
) -> None:
    await manager.shutdown()
    if task_service is not None:
        await task_service.stop()
    if condition_task_service is not None:
        await condition_task_service.stop()
    if module_registry is not None:
        await module_registry.stop_all()
    if output_tasks:
        await asyncio.gather(*list(output_tasks), return_exceptions=True)
    repositories.close()


async def main() -> int:
    _ensure_utf8_stdio()
    try:
        provider = OpenAICompatibleProvider.from_env()
        embeddings = BigModelEmbeddingProvider.from_env()
    except RuntimeError as exc:
        print(f"配置错误：{exc}")
        return 1

    account_id = os.getenv("AGENT_ACCOUNT_ID", "local-user").strip()
    channel = os.getenv("AGENT_CHANNEL", "desktop").strip()
    conversation_id = os.getenv("AGENT_CONVERSATION_ID", channel).strip()
    delivery_target = os.getenv("AGENT_DELIVERY_TARGET", "unified").strip()
    if not account_id:
        print("配置错误：AGENT_ACCOUNT_ID 不能为空")
        return 1
    scope = Scope(owner_id=account_id, channel=channel, conversation_id=conversation_id)
    core_db = os.getenv("AGENT_CORE_DB", "agent_core.db")
    run_store = SQLiteRunStore(core_db)
    confirmation_store = SQLiteConfirmationStore(core_db)
    # 与 web_api 共用同一个 AGENT_CORE_DB，两边看到的意图状态是同一份。
    intent_state_store = SQLiteIntentStateStore(core_db)
    # 账号级权限层：与 web_api 同一份口径（AGENT_CORE_DB / WEB_HOST 同一批
    # 环境变量、同一张表）。它现在通过 BuildContext 显式传给各模块，
    # 不再靠模块级全局槽（那个槽谁在什么时候设的只能靠读调用顺序）。
    # CLI 没有 HTTP 监听，"本机限制"由 WEB_HOST 表达（默认 127.0.0.1）。
    event_bus = InMemoryEventBus()
    permission_layer = PermissionLayer(
        db_path=core_db,
        web_host=os.getenv("WEB_HOST", "127.0.0.1"),
        confirmation_store=confirmation_store,
    )
    # 装配必须与 Web 走**同一个**构建函数（agent_core.capability_runtime.build_runtime）。
    # 此前 CLI 在这里自己 ModuleRegistry() + tools_for()，于是拉出了一条不同用自己的路：
    # 没有 TaskRouter、任务库读的是另一批环境变量默认值 ——
    # 同一个目标在 Web 上走确定性路由，在 CLI 上交给模型。
    # TaskRouter 是内核组件不是外部可选服务：它装配失败时 build_runtime 会
    # 抛 RuntimeBootstrapError，CLI 直接报错退出，不降级成"没有路由"。
    try:
        runtime = build_runtime(
            provider=provider,
            event_bus=event_bus,
            permission_layer=permission_layer,
        )
    except RuntimeBootstrapError as exc:
        print(f"运行时初始化失败：{exc}")
        return 1
    modules = runtime.modules
    registry = runtime.registry
    task_service = runtime.task_service
    condition_task_service = runtime.condition_task_service
    task_router = runtime.task_router
    # 与 Web 同一个网关实例（同一个构建函数产出），审计表就不会被劈成两半。
    action_gateway = runtime.gateway
    await modules.start_all()
    if task_service is not None:
        task_service.registry_provider = lambda: registry
        await task_service.start()
    if condition_task_service is not None:
        condition_task_service.registry_provider = lambda: registry
        await condition_task_service.start()
    prompt_composer = PromptComposer()

    # 子代理派发工具**不在这里注册**了：唯一来源是能力目录（`build_runtime`），
    # 两个入口拿到同一份。以前两个入口各注册一遍、参数还不同（CLI 不传
    # run_store → 走默认库），`AGENT_CORE_DB` 一改就连到不同的库上。
    # 守卫见 tests/test_subagent_registration.py。

    tool_names = [spec.name for spec in registry]
    print(_format_tool_registration(tool_names))
    repositories = initialize_memory_repositories(vector_size=embeddings.dimensions)
    writer = MemoryWriter(repositories, embeddings)
    retriever = MemoryRetriever(repositories, embeddings)
    attach_code_agent_memory_retriever(runtime.work_host, retriever)

    def steward_sink(scope: Scope, message: dict[str, Any], stable_key: str) -> bool:
        """CLI 没有会话仓库，只把过程句挂进当前进程的历史（模型下一轮能看见）。"""
        content = str(message.get("content") or "").strip()
        if not content or manager is None:
            return False
        return bool(
            manager.append_message(
                scope,
                "assistant",
                content,
                stable_key=stable_key,
                kind=STEWARD_PROGRESS_KIND,
                phase=message.get("phase", ""),
            )
        )

    steward_reporter = StewardReporter(
        StewardNarrator(
            provider,
            persona=PERSONA_OVERRIDE or DEFAULT_SYSTEM_PROMPT,
            forbidden_terms={spec.name for spec in registry},
        ),
        sink=steward_sink,
        event_bus=event_bus,
    )
    # 回答前搜索门：与 Web 同一套开关（SEARCH_GATE_ENABLED），默认开启。
    search_gate = build_search_gate(provider)

    def session_factory(current_scope: Scope) -> MemoryAgentSession:
        loop = AgentLoop(
            provider=provider,
            registry=registry,
            scope=current_scope,
            run_store=run_store,
            confirmation_store=confirmation_store,
            # 与 Web 同一条闸门一：账号在完全访问模式下，高风险工具不再挂起。
            risk_policy=permission_layer.policy,
            event_bus=event_bus,
            intent_state_store=intent_state_store,
            steward=steward_reporter,
            # 与 Web 同一个路由器：确定性路由在 CLI 上也要生效，
            # 否则同一个目标在两个入口会得到不同的处理。
            router=task_router,
            routing_mode=runtime.context.routing_mode,
            legacy_shadow_enabled=runtime.context.legacy_shadow_enabled,
            gateway=action_gateway,
            search_gate=search_gate,
        )
        return MemoryAgentSession(
            loop,
            writer,
            retriever,
            prompt_composer=prompt_composer,
            persona_override=PERSONA_OVERRIDE,
            output_mode=OUTPUT_MODE,
            routing_mode=runtime.context.routing_mode,
        )

    manager = ConversationManager(session_factory, confirmation_store=confirmation_store, event_bus=event_bus)
    outbox = OutboxStore(os.getenv("AGENT_OUTBOX_DB", "agent_outbox.db"))
    runner = AgentRunner(
        manager,
        confirmation_store,
        outbox=outbox,
        delivery_target=delivery_target,
    )
    _subscribe_task_due_output(event_bus, scope)
    _subscribe_task_due_outbox(event_bus, scope.owner_id, scope.module_id, outbox, delivery_target)
    output_tasks: set[asyncio.Task[None]] = set()
    for message in _startup_messages():
        print(message)
    active_persona = PERSONA_OVERRIDE or DEFAULT_PRIMARY_PERSONA
    for notice in await runner.recover_with_language(
        scope,
        lambda request, base: _generate_recovery_prompt(provider, request, base, persona=active_persona),
    ):
        print(_format_recovery_notice(notice), flush=True)

    try:
        while True:
            try:
                user_input = (await asyncio.to_thread(input, "你: ")).strip()
            except EOFError:
                break

            if not user_input:
                continue
            command = user_input.lower()
            if command in {"/exit", "/quit"}:
                break
            if command == "/history":
                history = manager.history(scope)
                if not history:
                    print("暂无会话历史。")
                else:
                    for entry in history:
                        print(f"{entry.get('role')}: {entry.get('content')}")
                continue
            if command == "/clear":
                manager.clear_history(scope)
                print("提示历史已清空。")
                continue

            local_command, requested_index = _parse_confirmation_command(user_input)
            if local_command == "list_confirmations":
                pending_for_account = getattr(confirmation_store, "pending_for_account", None)
                pending = pending_for_account(AccountTaskScope(scope.owner_id, scope.module_id)) if callable(pending_for_account) else confirmation_store.pending_for_scope(scope)
                print(_format_pending_confirmations(pending), flush=True)
                continue
            if local_command == "list_tasks":
                if task_service is None and condition_task_service is None:
                    print(_format_unavailable_capability("任务"), flush=True)
                else:
                    parts: list[str] = []
                    if task_service is not None:
                        parts.append(_format_tasks(task_service.store.list_tasks(scope)))
                    if condition_task_service is not None:
                        parts.append(_format_condition_tasks(condition_task_service.store.list(scope)))
                    print("\n\n".join(parts), flush=True)
                continue
            if local_command == "memory_stats":
                print(_format_memory_stats(repositories.count_by_account(scope.owner_id, scope.module_id)), flush=True)
                continue
            if local_command == "local_time":
                try:
                    from agent_tools.system_info import local_time_info
                    time_result = local_time_info()
                except Exception as exc:
                    time_result = {"status": "failed", "error": f"local time unavailable: {type(exc).__name__}: {exc}"}
                if time_result.get("status") == "ok":
                    print(_format_local_time_result(dict(time_result.get("output") or {})), flush=True)
                else:
                    print(f"无法读取本机时间：{time_result.get('error') or '未知错误'}", flush=True)
                continue
            if local_command == "reset":
                count = await manager.reset_pending(scope)
                print(f"已重置 {count} 条待确认请求；没有创建新任务。", flush=True)
                continue
            if local_command == "confirm":
                pending_for_account = getattr(confirmation_store, "pending_for_account", None)
                pending = pending_for_account(AccountTaskScope(scope.owner_id, scope.module_id)) if callable(pending_for_account) else confirmation_store.pending_for_scope(scope)
                if not pending:
                    print("当前没有待确认请求。", flush=True)
                    continue
                if requested_index is None and len(pending) > 1:
                    print("当前有多条待确认请求，请回复“确认第1条”等明确编号。", flush=True)
                    continue
                selected_index = requested_index or 1
                if selected_index < 1 or selected_index > len(pending):
                    print(f"待确认编号无效，请选择 1 到 {len(pending)}。", flush=True)
                    continue
                selected = pending[selected_index - 1]
                outcome = await runner.resume(selected.action_id, scope, "confirmed")
                handle = type("ResumeHandle", (), {"turn_id": outcome.run_id or selected.run_id})()
                print(_format_outcome(scope, handle, outcome), flush=True)
                continue

            if _confirmation_decision(user_input) == "rejected":
                pending = runner.pending(scope)
                if len(pending) != 1:
                    print("当前没有唯一待确认请求；请先输入“查看待确认”或使用“重置待确认”。", flush=True)
                    continue
                outcome = await runner.resume(pending[0].action_id, scope, "rejected")
                handle = type("ResumeHandle", (), {"turn_id": outcome.run_id or pending[0].run_id})()
                print(_format_outcome(scope, handle, outcome), flush=True)
                continue

            handle = await runner.submit(_request_with_runtime_time(user_input, scope, delivery_target=delivery_target))
            if not handle.accepted:
                print(f"输入被拒绝：{handle.reason}")
                continue
            print(f"已入队第 {handle.position} 轮，turn {handle.turn_id[:8]}。", flush=True)
            task = asyncio.create_task(_watch_turn(scope, handle))
            output_tasks.add(task)
            task.add_done_callback(output_tasks.discard)
    except KeyboardInterrupt:
        print("\n正在停止……")
        return 130
    finally:
        await _shutdown_cli(manager, repositories, output_tasks, task_service, condition_task_service, modules)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
