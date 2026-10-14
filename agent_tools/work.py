"""Chat-side work tools: the single registration source for every ``work__*`` entry point.

【模型侧只有四个入口】

    work__submit        提交一个单步后台任务
    work__plan_submit   提交一份多步计划
    work__status        统一查询（任务 / 计划）
    work__cancel        统一取消（任务 / 计划）

**精确四个，不多不少。** 以前是九个，两档（单步任务、多步计划）各自长出一套
查询与取消，于是同一件事在模型面前有两个名字（"看看任务"用 list 还是 get？
"取消"用 cancel 还是 plan_cancel？），而 `approve` 更糟 ——
它让模型可以替用户批准一个高风险操作：

    work__submit 当时带 hard_routable，运行时可以直接提交；
    可模型手里一旦有 approve，它就能把刚提交的高风险任务自己批掉。
    两道闸门互相抵消。

审批因此整个从模型面消失。后台任务的审批只能由 Web / CLI 用户走统一审批控制面
（``agent_core/approval_coordinator.py`` + ``/api/approvals``）；
模型连"批准"这个动作都不该知道怎么表达。

【提交权只属于模型】（2026-09-23，code-agent 计划阶段 0）

`work__submit` 的 `hard_routable` 与 `deterministic_route_allowed` 已摘掉：
提交后台任务是持久动作，运行时替用户发起等于跳过了模型判断与用户意愿。
路由层现在最多给一条 SOFT 建议（见 ``agent_core/routing.py``）。

【为什么是 `kind` 而不是两个工具】

`status` / `cancel` 各自用一个 `kind`（`task` / `plan`）分派，`id` 决定
"查单项还是列一批"。这样"任务"与"计划"的差别落在**一个参数**上，
而不是落在两个工具名上 —— 模型不需要先判断"我这个是任务还是计划"再去挑工具名，
而它挑错名字的代价是调用失败（这个名字根本不在清单里）。

【只做"提交"，不做"执行"】

计划先过 Host 的 ``validate_plan_definition``，单步任务由 Host 决定审批语义。
工具层不许自己批准自己的步骤，也不许碰别人的 scope（见 :func:`_work_scope`）：
**owner / channel / conversation 原样透传，module_id 固定成 "work"**。
"""

from __future__ import annotations

from typing import Any

from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, Scope, ToolSpec
from agent_core.tools import classify_tool_error
from agent_core.work_provider import WORK_TASK_TYPES, validate_work_submit_arguments

#: `status` / `cancel` 都收这两个值。写成模块常量而不是各写一遍：
#: 两处各写一份的话，加一档时必然只改一处，而症状是"另一个入口说 kind 非法"。
_KINDS = ("task", "plan")

_MAX_LIMIT = 100
_DEFAULT_LIMIT = 20

#: 失败原因一律带**稳定错误码**（`error_kind`），模型按它决定"换个说法再说"
#: 还是"这条路走不通"。散文式的失败原因会让这种判断退化成字符串包含。
_PERMANENT = "permanent"


def _schema(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


def _work_scope(scope: Scope) -> Scope:
    """把聊天 scope 投影成 work scope。

    只改 ``module_id``，其余三段原样保留 —— 这是"两边看到同一份任务"的关键：
    * 固定 ``module_id="work"``，聊天和 Web 工作页落在同一张账本分区；
    * 保留 owner/channel/conversation，Host 广播时才能反查出是哪个对话发起的
      （``WorkResultBridge`` 靠它把结果写回原对话，不是写回 work 模块自己的 scope）。
    """
    return Scope(
        owner_id=scope.owner_id,
        channel=scope.channel,
        module_id="work",
        conversation_id=scope.conversation_id,
    )


def _kind(arguments: dict[str, Any]) -> str:
    """取并归一化 `kind`。大小写与首尾空白都吃掉 —— 模型给 "Task" 是常事。"""
    return str(arguments.get("kind") or "").strip().lower()


def _identifier(arguments: dict[str, Any]) -> str:
    return str(arguments.get("id") or "").strip()


def _limit(arguments: dict[str, Any]) -> int:
    try:
        value = int(arguments.get("limit") or _DEFAULT_LIMIT)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(_MAX_LIMIT, value))


def _bad_kind(kind: str) -> dict[str, Any]:
    """`kind` 拼错时**明确失败**，不默默按某一个当作默认。

    默认成 task 的后果是"查了另一个东西还以为查到了"。这种错误在模型侧
    几乎不可能自查，所以返回里要把合法值说清楚。
    """
    return {
        "status": "failed",
        "error": f"kind must be one of {list(_KINDS)}, got {kind!r}",
        "error_kind": _PERMANENT,
    }


_STEP_SCHEMA = {
    "type": "object",
    "description": "One step of the plan. Steps run as a DAG in dependency order.",
    "properties": {
        "step_id": {"type": "string", "description": "Short unique id inside this plan, e.g. 'organize'."},
        "task_type": {
            "type": "string",
            "enum": ["file.list", "file.organize", "screen.capture", "system.volume.adjust"],
            "description": "Host-supported task type. Anything else is rejected before execution.",
        },
        "objective": {"type": "string", "description": "What this step achieves, in the user's language."},
        "input": {
            "type": "object",
            "description": (
                "Structured step input, e.g. {\"directory\": \"D:/Downloads\"}. "
                "Reference an earlier step output with ${step:<step_id>.output.<field>}; "
                "the Host resolves it and the referenced step must appear in depends_on."
            ),
        },
        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Step ids that must complete first."},
        "requires_approval": {
            "type": "boolean",
            "description": "True when the user must approve this step before it runs.",
        },
        "failure_policy": {
            "type": "string",
            "enum": ["stop", "retry", "skip"],
            "description": "What happens when this step fails. Defaults to 'stop'.",
        },
    },
    "required": ["step_id", "task_type", "objective"],
}


# ──────────────────────────────── 私有处理器 ────────────────────────────────
#
# 分成四个私有函数是为了让 `make_work_tools` 读起来就是"四个入口"，
# 而不是一坨。**它们不是第二个公开入口** —— 没有 `make_work_plan_tools`
# 那样的兼容子集了（那种东西活下来就会让"谁在注册工具"重新变成两个答案）。


def _submit_tool(host: Any) -> ToolSpec:
    provider = getattr(host, "provider", None)
    capabilities = getattr(provider, "capabilities", None)
    supported_task_types = tuple(
        sorted(getattr(capabilities, "task_types", ()) or WORK_TASK_TYPES)
    )
    # 可显式选择的执行者。写进 input 的说明里，否则模型不知道 `deepseek_harness`
    # 这个名字存在 —— 它只认得 task_type，而 provider 选择是 input 里的一个字段。
    registry = getattr(host, "provider_registry", None)
    selectable_providers = tuple(
        str(item.get("provider_id") or "")
        for item in (registry() if callable(registry) else [])
        if item.get("selectable") and item.get("provider_id")
    )
    input_description = (
        'Structured step input, e.g. {"directory": "D:/Downloads"}.'
        + (
            " Optional field provider selects the executor "
            f"({', '.join(selectable_providers)}); omit it to use the default executor."
            if selectable_providers
            else ""
        )
    )

    async def submit(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        try:
            return await host.submit(
                _work_scope(scope),
                str(arguments.get("objective") or ""),
                task_type=str(arguments.get("task_type") or ""),
                input=dict(arguments.get("input") or {}),
            )
        except Exception as exc:
            # 在这里就把异常翻成带 error_kind 的失败：模型拿到的"接下来该怎么办"
            # 不再依赖执行器的兜底猜测（unsupported task_type → permanent，
            # 目录越权 → permission，这两种失败的处置方式完全不同）。
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}

    return ToolSpec(
        "submit",
        _schema(
            "submit",
            "Submit a structured task to the background Host worker.",
            {
                "task_type": {"type": "string", "enum": list(supported_task_types)},
                "objective": {"type": "string"},
                "input": {"type": "object", "description": input_description},
            },
            ["task_type", "objective", "input"],
        ),
        submit,
        # 2026-09-23（code-agent 计划阶段 0）：`hard_routable` 与
        # `deterministic_route_allowed` 都已摘掉 —— 提交后台任务是持久动作，
        # 不能由运行时替用户发起（那条路不经过模型，也不经过用户）。
        # 现在只有模型能调它，路由层最多给一条 SOFT 建议。
        risk="low",
        effect=EFFECT_PERSISTENT,
        timeout_seconds=30.0,
        # Schema 只能要求 `input` "是个对象"，说不出"这个 task_type 的 input
        # 得带哪些字段" —— 那是跨字段关系。整条链上唯一有资格判它的是
        # `WORK_TASK_INPUT_REQUIREMENTS`（Work 领域的契约），
        # 这里把它挂在工具上，让网关在进 Host 之前就拦住缺字段的提交。
        # 不经工具入口的兼容调用由具体 Provider 在执行前验证；Host 不能只看 input，
        # 因为 WorkRequest 还支持从 context 取得任务参数。
        argument_validator=validate_work_submit_arguments,
    )


def _plan_submit_tool(host: Any) -> ToolSpec:
    async def plan_submit(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        try:
            result = await host.submit_plan(
                _work_scope(scope),
                str(arguments.get("objective") or ""),
                list(arguments.get("steps") or []),
            )
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
        return {"status": "ok", "output": result}

    return ToolSpec(
        "plan_submit",
        _schema(
            "plan_submit",
            (
                "Submit a multi-step control plan for a user goal that needs several dependent tasks, "
                "for example: organize a folder, then screenshot, then lower the volume. "
                "Only submit a plan after the user has described the whole goal. "
                "The Host validates the plan; an invalid plan is rejected without executing anything."
            ),
            {
                "objective": {"type": "string", "description": "The user's goal in one sentence."},
                "steps": {"type": "array", "items": _STEP_SCHEMA, "description": "Ordered plan steps."},
            },
            ["objective", "steps"],
        ),
        plan_submit,
        risk="high",
        effect=EFFECT_PERSISTENT,
        timeout_seconds=30.0,
        confirmation_title="确认执行计划",
    )


def _status_tool(host: Any) -> ToolSpec:
    async def status(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        kind = _kind(arguments)
        if kind not in _KINDS:
            return _bad_kind(kind)
        work_scope = _work_scope(scope)
        item_id = _identifier(arguments)

        if kind == "task":
            if not item_id:
                return {"status": "ok", "output": {"kind": "task", "items": host.list(work_scope)}}
            try:
                detail = host.get(item_id, work_scope)
            except KeyError:
                # "没有这个任务"是**正常查询结果**，不是失败：模型据此告诉用户
                # "这个编号查不到"，比收到一个 failed 再猜为什么更好用。
                return {"status": "ok", "output": {"kind": "task", "found": False, "item": None}}
            return {"status": "ok", "output": {"kind": "task", "found": True, "item": detail}}

        if not item_id:
            return {
                "status": "ok",
                "output": {"kind": "plan", "items": host.list_plans(work_scope, limit=_limit(arguments))},
            }
        try:
            detail = host.get_plan(item_id, work_scope)
        except KeyError:
            return {"status": "ok", "output": {"kind": "plan", "found": False, "item": None}}
        return {"status": "ok", "output": {"kind": "plan", "found": True, "item": detail}}

    return ToolSpec(
        "status",
        _schema(
            "status",
            (
                "Read background work. kind='task' reads one submitted task (or lists them all when "
                "'id' is omitted); kind='plan' reads one control plan the same way. "
                "This operation is read-only."
            ),
            {
                "kind": {"type": "string", "enum": list(_KINDS), "description": "Which thing to read."},
                "id": {
                    "type": "string",
                    "description": "Task id or plan id. Omit it to list instead of reading one item.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                    "description": "Maximum number of plans when listing. Defaults to 20.",
                },
            },
            ["kind"],
        ),
        status,
        effect=EFFECT_READ,
        risk="low",
        timeout_seconds=15.0,
    )


def _cancel_tool(host: Any) -> ToolSpec:
    async def cancel(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        kind = _kind(arguments)
        if kind not in _KINDS:
            return _bad_kind(kind)
        item_id = _identifier(arguments)
        if not item_id:
            # 没有编号时**不许猜"最近那个"**：猜的后果是用户说一句"取消"，
            # 停掉了一个他根本没提的任务。
            return {
                "status": "failed",
                "error": "id is required: cancel needs the task id or plan id to stop",
                "error_kind": _PERMANENT,
            }
        work_scope = _work_scope(scope)
        try:
            if kind == "task":
                cancelled = await host.cancel(item_id, work_scope)
                return {"status": "ok", "output": {"kind": "task", "id": item_id, "cancelled": bool(cancelled)}}
            result = await host.cancel_plan(item_id, work_scope)
        except KeyError:
            # 不把内部 KeyError 原文丢给模型：对它来说"没这个编号"才是可行动的信息。
            return {"status": "failed", "error": f"{kind} not found", "error_kind": _PERMANENT}
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "error_kind": classify_tool_error(exc)}
        return {"status": "ok", "output": {"kind": "plan", "id": item_id, **dict(result or {})}}

    return ToolSpec(
        "cancel",
        _schema(
            "cancel",
            (
                "Cancel a background task or a control plan. Completed steps are kept; "
                "pending and running work is cancelled. The user is asked to confirm before "
                "this actually runs."
            ),
            {
                "kind": {"type": "string", "enum": list(_KINDS), "description": "Which thing to cancel."},
                "id": {
                    "type": "string",
                    "description": "Task id or plan id. Required — cancel never guesses the target.",
                },
            },
            ["kind", "id"],
        ),
        cancel,
        # 高风险：它真的会停掉一个在跑的东西。而且**不能被确定性路由直调** ——
        # 那条路不经过用户确认（deterministic_route_allowed 默认 False）。
        risk="high",
        effect=EFFECT_PERSISTENT,
        confirmation_title="取消后台任务",
        timeout_seconds=15.0,
    )


def make_work_tools(host: Any) -> list[ToolSpec]:
    """**唯一的**公开构建入口：产出四个聊天侧 work 工具。

    顺序固定（提交 → 计划 → 查询 → 取消），这样能力快照与测试的"精确四个"判据
    都能直接比对。`host` 由调用方（work 模块）注入并持有生命周期，工具只借它发起动作。
    """
    return [
        _submit_tool(host),
        _plan_submit_tool(host),
        _status_tool(host),
        _cancel_tool(host),
    ]


# 这里**没有**工具名清单常量，是刻意的。
#
# 曾经有过一个 `WORK_TOOL_NAMES`，于是"这套工具有哪些名字"有三份来源：
# 这个常量（模块维护）、能力目录的 `declared_tools`（目录维护）、
# `make_work_tools()` 的真实产出。三份里有两份是人手写的，就一定会漂移 ——
# 这个项目已经踩过同一个坑：旧的静态清单漏了 `weather`，而报"某模块不可用"
# 的代码正好去查它，`KeyError` 让诊断路径自己炸了。
#
# 现在只剩**一份人手写**的：`CapabilityCatalog` 里 work 条目的 `declared_tools`。
# 模块的声明由 `agent_core/work_module.py` 从真实产出提取；
# 两者不一致时由 `tests/test_capability_catalog.py` 的对账用例报错。
# 守门：`tests/test_work_tools.py::test_work_tool_names_constant_is_gone`。
