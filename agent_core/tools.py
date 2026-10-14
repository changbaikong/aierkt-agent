"""工具注册、授权策略与执行归一化。

【在系统里的位置】
模型"想调工具"和"工具真的跑起来"之间隔了三道：
    模型输出 tool_calls
        → ToolRegistry.resolve 找到 ToolSpec
        → RiskPolicy.authorize 决定 放行 / 要人工确认 / 拒绝
        → ToolExecutor.execute 真正调用 handler，并把五花八门的返回归一化
这个文件就是后三道。它不认识任何具体工具，也不知道模型是谁。
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Protocol

from .contracts import EFFECT_READ, TOOL_ERROR_KINDS, IntentPolicy, Scope, ToolCall, ToolResult, ToolSpec
from .exceptions import is_retryable


class RiskPolicy(Protocol):
    """授权策略接口：只回答一个问题——这次调用该不该跑。"""

    def authorize(self, spec: ToolSpec, scope: Scope, arguments: dict[str, Any]) -> str:
        """Return ``allow``, ``confirm``, or ``deny``."""
        # allow   = 直接执行
        # confirm = 挂起，落一条 ConfirmationRequest 等用户点确认（HITL 闸门）
        # deny    = 直接拒绝，连问都不问


class DefaultRiskPolicy:
    """默认策略：只有"高风险"这一类会分流。

    规则很短，但每条都有明确理由：
    - 高风险 → confirm：写操作（发歌、改音量、动文件）必须用户点头。
    - 匿名身份 + 高风险 → deny：连确认的机会都不给。匿名会话没有可追责的账号，
      让它排队等一个人工确认没有意义，直接拒绝更安全。
    - 其余 → allow：只读查询不打扰用户。
    """

    def authorize(self, spec: ToolSpec, scope: Scope, arguments: dict[str, Any]) -> str:
        if spec.requires_confirmation:
            if scope.owner_id.startswith("anonymous:"):
                return "deny"
            return "confirm"
        return "allow"


class AllowAllRiskPolicy:
    """子代理专用：派发动作本身即授权边界，子 loop 内所有工具自动放行。"""

    def authorize(self, spec: ToolSpec, scope: Scope, arguments: dict[str, Any]) -> str:
        return "allow"


class ToolRegistry:
    """工具登记簿：按名字存放 ToolSpec，是"有哪些工具可用"的唯一真相。

    注册期就报错而不是运行时才发现，是本类的设计取向：
    工具重名、确定性路由配错，都在启动阶段炸掉，不留给用户去踩。
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name.strip():
            raise ValueError("tool name is required")
        # 确定性路由意味着"不经过模型直接调"，那就必须满足两条之一：
        #   ① effect="read" —— 什么都不改，绕过模型不会有后果；
        #   ② hard_routable —— 会留下东西，但注册方明确授权运行时可以直接发起。
        # ②是给"提交后台任务"这类工具准备的例外：它确实会留下东西，但过去为了拿到
        # 硬拦资格被迫标成只读，等于用一个假的语义去换权限。现在两个标记各管一件事，
        # 谁都可以被单独审计。
        if spec.deterministic_route_allowed and spec.requires_confirmation:
            raise ValueError("deterministic routes require a non-confirmed tool")
        if spec.deterministic_route_allowed and not (spec.effect == EFFECT_READ or spec.hard_routable):
            raise ValueError("deterministic routes require a read-effect tool or an explicit hard_routable=True")
        # 同名直接报错，绝不静默覆盖 —— 否则一个模块会悄悄把另一个模块的工具顶掉。
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def resolve(self, name: str) -> ToolSpec | None:
        """按名字取工具；找不到返回 None（调用方负责决定是报错还是降级）。"""
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """交给模型的工具清单（只有 schema，不含 handler 等内部字段）。"""
        return [spec.schema for spec in self._tools.values()]

    def intent_policies(self) -> tuple[IntentPolicy, ...]:
        """收集所有工具声明的意图门控配置，供 IntentClassifier 使用。

        顺序按工具注册顺序，分类器内部自己挑最高分，顺序不影响结果。
        没有工具声明时返回空元组——此时分类器会把一切都判成 conversation，
        闸门等于关闭，这正是"未启用意图门控"时的预期行为。
        """
        policies: list[IntentPolicy] = []
        for spec in self._tools.values():
            policies.extend(spec.intent_policies)
        return tuple(policies)

    def tools_for_intents(self, intent_names: tuple[str, ...]) -> tuple[str, ...]:
        """Return tools that declared at least one of the supplied intent policies."""
        wanted = set(intent_names)
        if not wanted:
            return ()
        return tuple(
            spec.name
            for spec in self._tools.values()
            if any(policy.intent_name in wanted for policy in spec.intent_policies)
        )

    def __iter__(self):
        return iter(self._tools.values())


def classify_tool_error(exc: BaseException) -> str:
    """把一次工具异常归进 TOOL_ERROR_KINDS 的某一类。

    判定顺序是刻意安排的，不要调换：

    1. ``TimeoutError`` —— asyncio 超时（Python 3.11+ 里 asyncio.TimeoutError
       就是内置 TimeoutError 本身）。单独成一类，因为它同时意味着
       "可能只是慢，再试一次有机会"和"任务可能还在后台跑"两件事。
    2. ``PermissionError`` —— 必须排在 is_retryable 之前：它是 OSError 的子类，
       否则会被下面那条 OSError 规则误判成可重试。权限问题重试无效，得人来授权。
    3. ``is_retryable(exc)`` —— 复用 exceptions 里已有的网络/限流判定
       （ConnectionError、TimeoutError、自定义 RetryableError 等）。
    4. 其余 —— 归 permanent。走到这里的多半是 KeyError / TypeError / AttributeError
       这类工具自身的实现缺陷，重试多少次结果都一样，越试越浪费预算。
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission"
    if is_retryable(exc):
        return "transient"
    return "permanent"


def _clean_error_kind(value: Any) -> str:
    """校验 handler 自己声明的 error_kind，不在词表里的值一律丢弃。

    宁可留成"未分类"，也不能让一个拼错的字符串流到下游去当判断依据——
    下游一旦按未知值分支，行为就不可预测了。
    """
    kind = str(value or "")
    return kind if kind in TOOL_ERROR_KINDS else ""


class _ToolExecutor:
    """**已降级为内部实现，不要在新代码里用。**

    它曾经是"执行一次工具调用"的公开入口，但那份职责已经整体搬进
    `agent_core.actions.ActionGateway`（见该模块头注释：执行这件事一度有四份实现）。
    现在网关一个人负责登记、来源限制、参数校验、权限、执行、
    超时、异常边界与状态归一化，这里只剩"调 handler + 归一化"这一小段，
    作为历史调用点的过渡实现保留。

    【为什么还要留着】
    还有四个调用点没迁完（`loop.py` 三处、`condition_tasks.py` 一处）。
    迁移会**逐个**做，每迁一个就把对应守门测试变绿（见计划 §3），
    所以这个类要活到最后一个调用点迁完为止 —— 一次性删掉会让中间几个
    阶段无法单独验证。全部迁完后 `tests/test_module_architecture.py`
    会加一条断言：`agent_core` 里除 `.actions` 外不得再出现这个类。

    【为什么改私有名字而不是加个 DeprecationWarning】
    加警告能提醒人，但提醒不了"新代码不小心 import 到它"——
    import 是合法的，警告又不报错。改私有名 + 从 `agent_core.__init__`
    摘掉导出，让误用在**写下的那一刻**就显眼。
    """

    async def execute(self, spec: ToolSpec, call: ToolCall, scope: Scope) -> ToolResult:
        try:
            async def invoke() -> Any:
                # handler 允许是同步函数：同步的丢线程池跑，避免阻塞事件循环；
                # 但线程池里返回的对象仍可能是 awaitable（比如包了个协程），所以再判一次。
                if inspect.iscoroutinefunction(spec.handler):
                    return await spec.handler(call.arguments, scope)
                value = await asyncio.to_thread(spec.handler, call.arguments, scope)
                if inspect.isawaitable(value):
                    return await value
                return value

            value = await asyncio.wait_for(invoke(), timeout=spec.timeout_seconds)
            # handler 也可以直接返回 ToolResult（media 模块就是这么写的），它已经是最终形态。
            # 这里必须短路：ToolResult 不是 mapping，继续走下面的 dict(value) 只会得到
            # TypeError，把一次本来成功的结果硬生生打成 failed。
            if isinstance(value, ToolResult):
                return value
            output = dict(value or {})
            # 归一化：handler 若已经返回 {status, output, error} 这种"已包装"结构，就照搬；
            # 否则把整个字典当 output，状态记为 ok。工具作者因此可以自由选择省事写法。
            # 这条路径没有异常对象可分类，所以分类由 handler 自己在 error_kind 里声明。
            if "status" in output and ("output" in output or "error" in output):
                return ToolResult(
                    str(output.get("status") or "ok"),
                    dict(output.get("output") or {}),
                    str(output.get("error") or ""),
                    _clean_error_kind(output.get("error_kind")),
                )
            return ToolResult("ok", output)
        except asyncio.TimeoutError:
            return ToolResult("failed", error=f"tool {spec.name} timed out", error_kind="timeout")
        except Exception as exc:
            return ToolResult(
                "failed",
                error=f"{type(exc).__name__}: {exc}",
                error_kind=classify_tool_error(exc),
            )


#: **过渡别名，等 §3 的调用点全部迁完就删。**
#: 五个测试文件（tests/test_tool_error_kind.py 等）还在按旧名字用它，
#: 那些文件在第 §6 步统一重写；在那之前旧名必须能 import，否则会
#: 把"测试还没重写"和"生产代码有洞"两种红混在一起，看不出哪个是真的。
ToolExecutor = _ToolExecutor

__all__ = [name for name in ("RiskPolicy", "DefaultRiskPolicy", "AllowAllRiskPolicy", "ToolRegistry")]
