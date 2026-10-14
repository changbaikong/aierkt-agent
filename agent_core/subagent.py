"""子代理派发工具：主模型调用后新建独立 AgentLoop 处理复杂子任务。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .contracts import EFFECT_PERSISTENT, AgentRequest, ProviderResponse, Scope, ToolSpec
from .events import EventBus
from .tools import classify_tool_error
from .loop import AgentLoop
from .runner import AgentRunner
from .persistence import ConfirmationStore, RunStore, SQLiteConfirmationStore, SQLiteRunStore
from .provider import LLMProvider
from .prompts import DEFAULT_SUBAGENT_PERSONA, PromptComposer, PromptContext
from .tools import DefaultRiskPolicy, RiskPolicy, ToolRegistry


# 默认配置
MAX_DEPTH = 2
CHILD_MAX_STEPS = 10
# 12000 装不下真实的多来源网络研究：预算估算对中文按「1 字 1 token」计，
# 单条搜索结果最多回填 4800 字，系统提示 + 任务书 + 两三条结果就把预算吃光，
# 子代理还没交出结论就被自己的安全闸拦下（2026-09-30 验收第 14/15 批同型复现）。
# 48000 在估算口径下对 64k 上下文的对话模型仍留足余量（中文估算约为真实占用的 1.7 倍）。
CHILD_MAX_TOTAL_TOKENS = 48000
DEFAULT_TIMEOUT_SECONDS = 180.0
TIMEOUT_CAP = 600.0
SUBAGENT_MAX_REPLY_CHARS = 4000

DEFAULT_SUBAGENT_SYSTEM_PROMPT = DEFAULT_SUBAGENT_PERSONA


def _depth(scope: Scope) -> int:
    """从 scope.module_id 解析当前子代理深度。"""
    return scope.module_id.count("/sub")


def _child_scope(scope: Scope, suffix: str) -> Scope:
    """构造子代理的隔离 scope（conversation_id 加后缀、module_id 加 /sub）。"""
    return replace(
        scope,
        conversation_id=f"{scope.conversation_id}#s{suffix}",
        module_id=f"{scope.module_id}/sub{suffix}",
    )


def _validated_args(
    arguments: dict[str, Any],
    *,
    default_steps: int = CHILD_MAX_STEPS,
    default_tokens: int = CHILD_MAX_TOTAL_TOKENS,
    default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    timeout_cap: float = TIMEOUT_CAP,
) -> tuple[str, str | None, int, int, float]:
    """校验并归一化工具参数。非法输入抛 ValueError（由 handler 转成 failed 结果）。

    三个默认值必须能由注册方覆盖：之前它们被写死成模块常量，
    导致 `register_subagent_tool(child_max_steps=3)` 这类调用**静默失效**——
    参数收下了，却从来没传到这里。`timeout_cap` 同理。
    """
    # task（必填）
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task is required and must be a non-empty string")
    task = task.strip()

    # system_prompt（可选）
    system_prompt = arguments.get("system_prompt")
    if system_prompt is not None:
        if not isinstance(system_prompt, str):
            raise ValueError("system_prompt must be a string")
        system_prompt = system_prompt.strip() or None

    # max_steps（可选，1..20）
    max_steps = arguments.get("max_steps", default_steps)
    if not isinstance(max_steps, bool) and isinstance(max_steps, int):
        max_steps = min(20, max(1, max_steps))
    else:
        raise ValueError("max_steps must be an integer")

    # max_total_tokens（可选，0..100000）
    max_total_tokens = arguments.get("max_total_tokens", default_tokens)
    if not isinstance(max_total_tokens, bool) and isinstance(max_total_tokens, int):
        max_total_tokens = min(100000, max(0, max_total_tokens))
    else:
        raise ValueError("max_total_tokens must be an integer")

    # timeout_seconds（可选，5..600）
    timeout_seconds = arguments.get("timeout_seconds", default_timeout)
    if isinstance(timeout_seconds, (int, float)) and not isinstance(timeout_seconds, bool):
        timeout_seconds = float(min(timeout_cap, max(5.0, timeout_seconds)))
    else:
        raise ValueError("timeout_seconds must be a number")

    return task, system_prompt, max_steps, max_total_tokens, timeout_seconds


SUBAGENT_DISPATCH_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "subagent__dispatch",
        "description": (
            "Delegate a task to a sub-agent with independent "
            f"{CHILD_MAX_STEPS}-step/{CHILD_MAX_TOTAL_TOKENS}-token budget. "
            "Use this when: "
            "1) Task requires multiple tool calls (>3 steps, e.g. researching multiple sources); "
            "2) Task involves reading/analyzing multiple files; "
            "3) Task needs iterative work (generate code + write tests + verify); "
            "4) You sense the main loop's 5-step limit might be exceeded. "
            "The sub-agent returns only the final result, keeping the main conversation clean."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The task to delegate to the sub-agent. Be specific and complete.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Optional system prompt override for the sub-agent. If not provided, uses default sub-agent instructions.",
                },
                "max_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": CHILD_MAX_STEPS,
                    "description": f"Maximum steps for the sub-agent (default: {CHILD_MAX_STEPS}).",
                },
                "max_total_tokens": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100000,
                    "default": CHILD_MAX_TOTAL_TOKENS,
                    "description": f"Maximum total tokens for the sub-agent (default: {CHILD_MAX_TOTAL_TOKENS}).",
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 5,
                    "maximum": TIMEOUT_CAP,
                    "default": DEFAULT_TIMEOUT_SECONDS,
                    "description": f"Timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
                },
            },
            "required": ["task"],
            "additionalProperties": False,
        },
    },
}


#: 子代理默认看不到的工具。
#:
#: 派发工具自己在名单里：子代理再派一次就是递归。以前只靠 `max_depth` 在
#: **跑起来之后**挡一刀，而模型是看得见它的 —— 看得见但跑不动比看不见更糟，
#: 模型会一轮轮试着调它，每轮撞一次墙，把子代理那点步数预算（默认 10 步）
#: 全烧在明知不可能成功的事情上。
CHILD_HIDDEN_TOOLS = frozenset({"subagent__dispatch"})


def child_tool_registry(
    registry: ToolRegistry,
    *,
    hidden: frozenset[str] = CHILD_HIDDEN_TOOLS,
) -> ToolRegistry:
    """给子 loop 一份**属于它自己**的工具表。

    为什么不能直接把父 registry 那个对象传下去（2026-09-22 之前正是这么做的，
    见 `subagent.py` 里构造子 loop 那一段）：子 loop 与主 loop 共用同一个对象，
    等于子代理看到的工具和主 Chat 一模一样。现在子代理有自己的一张表 ——
    这不是"防坏人的墙"，而是**视野本来就该更窄**：委派目标只有一件事，
    给它全套工具没有意义，给它派发工具更是自找递归。

    权限上限不在这里改：子代理仍然继承同一个 `child_risk_policy`，
    该停的高风险操作照样停（见 `register_subagent_tool` 的说明）。
    """
    child = ToolRegistry()
    for spec in registry:
        if spec.name in hidden:
            continue
        child.register(spec)
    return child


async def _dispatch(
    arguments: dict[str, Any],
    scope: Scope,
    *,
    provider: LLMProvider,
    registry: ToolRegistry,
    event_bus: EventBus | None,
    max_depth: int,
    default_system_prompt: str,
    run_store: RunStore,
    confirmation_store: ConfirmationStore,
    child_risk_policy: RiskPolicy,
    timeout_cap: float,
    prompt_composer: PromptComposer | None,
    output_mode: str,
    runner: AgentRunner | None,
    child_max_steps: int = CHILD_MAX_STEPS,
    child_max_total_tokens: int = CHILD_MAX_TOTAL_TOKENS,
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """子代理派发 handler：建独立 AgentLoop、跑完、返回观察。"""
    # ① 校验参数
    try:
        task, sp_override, steps, tokens, timeout = _validated_args(
            arguments,
            default_steps=child_max_steps,
            default_tokens=child_max_total_tokens,
            default_timeout=default_timeout_seconds,
            timeout_cap=timeout_cap,
        )
    except ValueError as exc:
        return {"status": "failed", "error": f"invalid arguments: {exc}", "error_kind": "permanent"}

    # ② 深度防护
    if _depth(scope) >= max_depth:
        return {"status": "failed", "error": f"sub-agent depth exceeded (max_depth={max_depth})", "error_kind": "permanent"}

    # ③ 构造子 scope + 子 loop
    child_scope = _child_scope(scope, uuid.uuid4().hex[:4])
    # 子代理用自己的一张表（不含派发工具），不是主 Chat 那张。
    # prompt 里列出的工具也用它 —— 说得出却调不动的工具比不说更糟。
    child_registry = child_tool_registry(registry)
    loop = AgentLoop(
        provider=provider,
        registry=child_registry,
        scope=child_scope,
        run_store=run_store,
        confirmation_store=confirmation_store,
        risk_policy=child_risk_policy,
        max_steps=steps,
        max_total_tokens=tokens,
        event_bus=event_bus,
    )

    # ④ 跑子 loop
    child_prompt = sp_override or default_system_prompt
    if prompt_composer is not None:
        child_prompt = prompt_composer.compose(
            PromptContext(
                scope=child_scope,
                profile="subagent",
                registry=child_registry,
                persona_override=child_prompt,
                output_mode=output_mode,  # type: ignore[arg-type]
            )
        )
    req = AgentRequest(
        user_text=task,
        scope=child_scope,
        system_prompt=child_prompt,
    )
    try:
        executor = runner or AgentRunner(None, None)
        outcome = await asyncio.wait_for(executor.run_internal(loop, req), timeout=timeout)
    except asyncio.TimeoutError:
        return {"status": "failed", "error": f"sub-agent timed out after {timeout:.0f}s", "error_kind": "timeout"}
    except Exception as exc:
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}

    # ⑤ 读快照获取 steps/tokens
    snap = run_store.load(loop.run_id, child_scope)
    meta = {
        "sub_status": outcome.status,
        "steps": snap.steps if snap else 0,
        "tokens": snap.tokens if snap else 0,
    }

    # ⑥ 归一化输出
    if outcome.status == "completed" and outcome.reply:
        return {
            "status": "ok",
            "output": {
                **meta,
                "reply": outcome.reply[:SUBAGENT_MAX_REPLY_CHARS],
            },
        }
    if outcome.status == "paused":
        return {
            "status": "paused",
            "output": {
                **meta,
                "action_id": outcome.action_id,
                "note": "sub-agent requested confirmation; relay to user",
            },
        }
    # failed / cancelled / 其他
    return {
        "status": "failed",
        "error": outcome.reply or "sub-agent failed",
        "output": meta,
    }


def make_subagent_tool(
    provider: LLMProvider,
    *,
    registry_provider: Callable[[], ToolRegistry],
    event_bus: EventBus | None = None,
    max_depth: int = MAX_DEPTH,
    child_max_steps: int = CHILD_MAX_STEPS,
    child_max_total_tokens: int = CHILD_MAX_TOTAL_TOKENS,
    default_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    timeout_cap: float = TIMEOUT_CAP,
    risk: str = "low",
    run_store: RunStore | None = None,
    confirmation_store: ConfirmationStore | None = None,
    child_risk_policy: RiskPolicy | None = None,
    system_prompt: str = DEFAULT_SUBAGENT_SYSTEM_PROMPT,
    prompt_composer: PromptComposer | None = None,
    output_mode: str = "text",
    runner: AgentRunner | None = None,
    name: str = "dispatch",
) -> ToolSpec:
    """**只造**子代理派发工具，不注册。

    `name` 默认是**本地名** `dispatch`：走能力目录时由 `ModuleRegistry` 统一拼上
    `subagent__` 前缀（跟别的模块一个规矩）。直接往某张表注册的场景
    （`register_subagent_tool`）会显式传完整名 `subagent__dispatch`。

    与 `register_subagent_tool` 的关系：那个是"造完顺手塞进某张表"的便利入口，
    这个是给能力目录用的 —— 目录自己决定往哪张表塞、什么时候塞。

    Args:
        registry_provider: 取**当前**工具表的 callable。子代理要用它给子 loop
            造一张自己的表（见 `child_tool_registry`）。

            为什么是 callable 而不是直接给一张表：目录装配模块时，全仓那张注册表
            **还没建出来**（工具表是从模块产出拼出来的）。此刻固化一个表对象，
            等于让子代理拿着一张空表去干活。
        provider: LLM provider（主子共享）
        event_bus: 事件总线（可选，主子共享）
        max_depth: 最大嵌套深度（默认 2）
        child_max_steps: 子代理默认步数预算（默认 10）
        child_max_total_tokens: 子代理默认 token 预算（默认 CHILD_MAX_TOTAL_TOKENS）
        default_timeout_seconds: 默认超时秒数（默认 180）
        timeout_cap: 超时上限（默认 600）
        risk: 工具自身风险等级（默认 "low"，可配 "high" 让父级先确认）
        run_store: 持久化存储（可选，默认 SQLite）
        confirmation_store: 确认存储（可选，默认 SQLite）
        child_risk_policy: 子代理风险策略（默认 DefaultRiskPolicy）

        注意：默认**不是** AllowAllRiskPolicy —— 委派不授予额外权限，
        子代理里的高风险操作照样要停下来等确认。
        system_prompt: 子代理默认 system prompt

    Returns:
        注册后的 ToolSpec
    """
    # 默认存储
    if run_store is None:
        run_store = SQLiteRunStore(Path("agent_core.db"))
    if confirmation_store is None:
        confirmation_store = SQLiteConfirmationStore(Path("agent_core.db"))
    if child_risk_policy is None:
        # Delegation does not grant extra authority. High-risk child actions
        # must follow the same confirmation boundary as parent actions.
        child_risk_policy = DefaultRiskPolicy()

    # 闭包捕获所有依赖
    async def handler(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        return await _dispatch(
            arguments,
            scope,
            provider=provider,
            registry=registry_provider(),
            event_bus=event_bus,
            max_depth=max_depth,
            default_system_prompt=system_prompt,
            run_store=run_store,
            confirmation_store=confirmation_store,
            child_risk_policy=child_risk_policy,
            timeout_cap=timeout_cap,
            prompt_composer=prompt_composer,
            output_mode=output_mode,
            runner=runner,
            child_max_steps=child_max_steps,
            child_max_total_tokens=child_max_total_tokens,
            default_timeout_seconds=default_timeout_seconds,
        )

    return ToolSpec(
        name=name,
        schema=SUBAGENT_DISPATCH_SCHEMA,
        handler=handler,
        risk=risk,
        effect=EFFECT_PERSISTENT,
        timeout_seconds=timeout_cap,
        confirmation_title="Delegate task to sub-agent",
    )


def register_subagent_tool(
    registry: ToolRegistry,
    provider: LLMProvider,
    **kwargs: Any,
) -> ToolSpec:
    """造一个子代理派发工具并**当场注册**进 `registry`。

    这是给"手上已经有一张表"的场景用的（大批单测、独立脚本）。
    生产入口**不许用它**：那会让同一件事出现第二个注册来源
    （见 `tests/test_subagent_registration.py`）。
    """
    spec = make_subagent_tool(
        provider,
        registry_provider=lambda: registry,
        name="subagent__dispatch",
        **kwargs,
    )
    registry.register(spec)
    return spec
