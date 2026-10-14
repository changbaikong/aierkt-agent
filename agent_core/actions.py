"""动作网关：唯一能把动作真正跑起来的地方。

【为什么需要它】

在它之前，"执行一个工具"这件事在系统里有**四份**实现，各自管一段：

    loop._execute_deterministic_tool  → 确定性只读路由
    loop.run 里的模型调用分支          → 模型自由调用
    loop.resume                        → 审批恢复
    condition_tasks._check             → 条件任务自己 `ToolExecutor().execute(...)`

四份的差别不是刻意的，是长出来的。后果是同一个问题（"这个动作该不该跑"）
要在四个地方各答一遍，答漏一处就留一个洞：条件任务那条路径
**根本没有权限判定**，它自己 resolve 出 spec 就直接执行了。
补丁打在某一处，另外三处不会跟着好。

网关把这件事收成一个人。它按固定顺序做七件事，顺序即正确性：

    ① 原子登记动作编号        —— 重复编号不重跑（防"重复点确认=双跑"）
    ② 来源限制                —— origin vs spec.allowed_origins
    ③ 参数校验（schema）      —— JSON Schema，畸形参数在动手前就挡掉
    ④ 结构校验（schema 之后） —— 必填字符串不能全是空白；跨字段关系由工具交出回调
    ⑤ 权限三态                —— allow / confirm / deny
    ⑥ 低风险持久动作复核      —— 见 consistency_review：risk=high 那一档由⑤让用户点确认，
                                  read / transient 改错了重读即可，只剩 persistent + low 没人看
    ⑦ 执行 + 超时 + 异常边界  —— 非只读超时记 outcome_unknown，不自动重放

【它不做什么】

- 不认识具体工具（不知道 search 和 weather 的区别）；
- 不决定"该不该给模型看这个工具"（那是 ToolSelector 的事）；
- 不自己造审批记录（由注入的 approval 回调落库，保持审批语义单一）。

【超时的诚实说明】

`asyncio.wait_for` 超时只是**停止等待**，不是停止执行。
同步 handler 跑在线程池里，Python 没有安全的方式把那个线程掐掉。
所以：

- 只读动作超时 → `failed` + `timeout`（重试没有副作用代价，可以试）；
- 非只读动作超时 → `outcome_unknown` + `indeterminate`（**禁止自动重放**），
  并且执行前已经写过 `running`，进程重启后由 `ActionJournal.reconcile_on_start`
  把遗留记录对账成同一个状态。

进程隔离与强制终止不在本轮范围内（见计划 §9）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Mapping, Protocol

from .action_journal import ActionJournal
from .consistency_review import review_persistent_action
from .contracts import (
    ACTION_STATUS_INVALID,
    ActionObservation,
    ActionRequest,
    Scope,
    ToolArgumentViolation,
    ToolResult,
    ToolSpec,
    is_blank,
    normalise_action_status,
)
from .exceptions import is_retryable

logger = logging.getLogger(__name__)

#: 动作失败时的稳定错误码。前端与日志只认这些，不认 handler 的原文。
ERROR_UNKNOWN_ACTION = "unknown_action"
ERROR_ORIGIN_NOT_ALLOWED = "origin_not_allowed"
ERROR_INVALID_ARGUMENTS = "invalid_arguments"
ERROR_INTERNAL = "internal_error"
#: 工具自己的结构校验回调本身跑不起来（抛异常，或返回值类型不对）。
#: 与"参数不合法"是两件事：这个是**工具坏了**，那个是**调用方给了坏参数**。
ERROR_ARGUMENT_VALIDATOR_FAILED = "argument_validator_failed"


class LateRegistry:
    """把"查工具"推迟到调用那一刻：包一个返回登记簿的 callable。

    为什么不能把登记簿对象提前固化：持有 raise 时点那一刻的注册表，
    等于假定"工具集合此后不再变化"。模块热插拔、`AGENT_DISABLED_MODULES`
    生效、测试里把 provider 换成空注册表 —— 这三种情况下都会 resolve 出一个
    **已经不该存在的工具**，而调用方看不出来。

    **不支持什么**：这里只转发网关真正用到的两个能力（`resolve` 与迭代）。
    别的属性访问会报错而不是静默返回 None —— 静默 None 会让"代理不支持这个操作"
    和"工具不存在"变得无法区分，那是两个完全不同的问题。
    """

    def __init__(self, provider: Any) -> None:
        self._provider = provider

    def _current(self) -> Any:
        provider = self._provider
        value = provider() if callable(provider) else provider
        return value

    def resolve(self, name: str) -> Any:
        target = self._current()
        return target.resolve(name) if target is not None else None

    def __iter__(self) -> Any:
        target = self._current()
        return iter(target) if target is not None else iter(())


class ApprovalSink(Protocol):
    """落审批记录的回调。

    网关**不自己建审批记录**：聊天确认（ConfirmationStore）与 Work 审批
    （WorkLedger）各有各的持久化语义，由注入的实现决定落哪一种。
    网关只要求它回答一件事：审批编号是多少。
    """

    def __call__(self, request: ActionRequest, spec: ToolSpec) -> str: ...


class ActionGateway:
    """动作的唯一执行入口。

    Attributes:
        registry: 工具登记簿。网关从它 resolve spec。
        risk_policy: 授权策略（`authorize` 返回 allow / confirm / deny）。
        journal: 审计表。为 None 时退化成"不记账"（测试与离线场景）。
        approval_sink: 高风险动作落审批的回调。为 None 时高风险动作只能被拒 ——
            因为"等待审批"必须有一个可恢复的审批编号，没有编号就没法恢复。
    """

    def __init__(
        self,
        *,
        registry: Any,
        risk_policy: Any,
        journal: ActionJournal | None = None,
        approval_sink: ApprovalSink | None = None,
    ) -> None:
        self.registry = registry
        self.risk_policy = risk_policy
        self.journal = journal
        self.approval_sink = approval_sink

    @classmethod
    def standalone(cls, registry: Any, *, risk_policy: Any = None, approval_sink: Any = None) -> ActionGateway:
        """给没有注入网关的嵌入式场景（单元测试、独立脚本）建一个最小网关。

        **它执行与其它网关完全相同的七步校验**（登记、来源、参数、结构、权限、一致性复核、执行），
        所以这不是一条绕过路径 —— 差异只在两样东西上没有：

        1. **审计账本**：不记账，于是"同一个动作编号不跑第二次"这条保护失效。
           嵌入式场景的动作编号本来也是临时生成的，这个代价落不到它头上。
        2. **审批回调**：没有 sink 时高风险动作只能被拒（`_wait_for_approval` 的行为）。
           不提供 fake 的"自动同意"，是因为假同意比拒绝危险得多。
           传了 `approval_sink` 就等于把审批语义交给调用方，网关不认识它 ——
           聊天确认与 Work 审批各有各的落库方式，这里不许二选一替它们决定。

        生产入口（`capability_runtime.build_runtime`）**一律不用它** ——
        那边要的是带账本、带账号感知风险策略的那一个。
        """
        from .tools import DefaultRiskPolicy

        return cls(
            registry=registry,
            risk_policy=risk_policy or DefaultRiskPolicy(),
            journal=None,
            approval_sink=approval_sink,
        )

    # ── 唯一公开入口 ────────────────────────────────────────────────

    async def invoke(self, request: ActionRequest) -> ActionObservation:
        """执行一个动作。**这是唯一允许触达 handler 的路径。**"""
        started = time.monotonic()

        # ⓪ 先解析动作名：未知动作连登记都不必（没有 spec 就没有来源限制可判）。
        spec = self.registry.resolve(request.name)
        if spec is None:
            return self._failed(request, ERROR_UNKNOWN_ACTION, request.name)

        # ① 原子登记。已经登记过的编号直接回既有观察，**绝不重跑**。
        if self.journal is not None:
            if not self.journal.register(request):
                return self._replay(request)

        # ② 来源限制
        if spec.allowed_origins is not None and request.origin not in spec.allowed_origins:
            return await self._finish(
                request,
                status="rejected",
                error=ERROR_ORIGIN_NOT_ALLOWED,
                error_kind="permission",
                started=started,
            )

        # ③ 参数校验：畸形参数在动手之前挡掉，错误信息指向调用方。
        problem = self._validate_arguments(request, spec)
        if problem:
            return await self._finish(
                request,
                status="rejected",
                error=problem,
                error_kind="permanent",
                started=started,
            )

        # ④ 结构校验：schema 判不了的两件事（见 _validate_structure）。
        violation = self._validate_structure(request, spec)
        if violation is not None:
            return await self._finish(
                request,
                status="rejected",
                error=violation.error,
                error_kind=violation.error_kind,
                output=violation.output,
                started=started,
            )

        # ⑤ 权限三态
        decision = self.risk_policy.authorize(spec, request.scope, request.arguments)
        if decision == "deny":
            return await self._finish(
                request,
                status="rejected",
                error="action is not authorized for this scope",
                error_kind="permission",
                started=started,
            )
        if decision == "confirm":
            return await self._wait_for_approval(request, spec, started)

        # ⑥ 低风险持久动作的独立一致性复核。
        # 放在权限判定之后、执行之前：**批准了也不等于描述清楚了** ——
        # 这一层判的不是"谁可以调"，是"这次调用说清了要干什么"。
        # 复核只看 effect=persistent 且 risk=low 这一格（见 consistency_review）。
        problem = review_persistent_action(request, spec)
        if problem:
            return await self._finish(
                request,
                status="rejected",
                error=problem,
                error_kind="permanent",
                started=started,
            )

        # ⑦ 真的执行
        return await self._execute(request, spec, started)

    async def resume(self, approval_id: str, decision: str, *, request: ActionRequest) -> ActionObservation:
        """审批恢复：用户点头之后再跑。

        `decision` 只认 `confirmed` / `rejected` 两个值。
        拒绝时**只记结果、绝不执行**。

        注意签名里显式要 `request`：审批记录里存的是"当初想做什么"，
        不能从调用方"顺便带来的参数"里重建 —— 那样等于给了一条
        绕过审批的旁路（拿审批编号配一组新参数）。
        """
        started = time.monotonic()
        if decision == "rejected":
            return await self._finish(
                request,
                status="rejected",
                error="user rejected or action expired",
                error_kind="permission",
                started=started,
            )
        if decision != "confirmed":
            return self._failed(request, ERROR_INVALID_ARGUMENTS, "invalid approval decision")

        spec = self.registry.resolve(request.name)
        if spec is None:
            return self._failed(request, ERROR_UNKNOWN_ACTION, request.name)

        # 恢复按 `invoke` 的同一顺序重跑，只少一步①：审批记录本身就是那次登记。
        # 恢复也走一遍来源与参数校验：审批期间 spec 可能已经变了
        # （模块重载、工具被移除），不能假定"当初合法现在也合法"。
        if spec.allowed_origins is not None and request.origin not in spec.allowed_origins:
            return await self._finish(
                request,
                status="rejected",
                error=ERROR_ORIGIN_NOT_ALLOWED,
                error_kind="permission",
                started=started,
            )
        problem = self._validate_arguments(request, spec)
        if problem:
            return await self._finish(
                request,
                status="rejected",
                error=problem,
                error_kind="permanent",
                started=started,
            )

        violation = self._validate_structure(request, spec)
        if violation is not None:
            return await self._finish(
                request,
                status="rejected",
                error=violation.error,
                error_kind=violation.error_kind,
                output=violation.output,
                started=started,
            )

        # 一致性复核也要重跑。现在它只覆盖 risk=low，而走到这里的动作必然是
        # "需要用户点头"那一档，所以此刻它是空转；但**恢复也是一条落到执行的路径**，
        # 不在这里放一道，将来"低风险 + 需要确认"的组合出现时就会绕过复核，
        # 而且绕得很安静。
        problem = review_persistent_action(request, spec)
        if problem:
            return await self._finish(
                request,
                status="rejected",
                error=problem,
                error_kind="permanent",
                started=started,
            )

        return await self._execute(request, spec, started)

    # ── 内部实现 ────────────────────────────────────────────────────

    def _validate_arguments(self, request: ActionRequest, spec: ToolSpec) -> str:
        """用 JSON Schema 校验参数；返回空串表示通过。

        校验的对象是 `schema["function"]["parameters"]`（OpenAI 工具格式）。
        拿不到这一段 schema 时**放行**而不是拒绝：有些工具（如 work 的计划工具）
        的 schema 是动态拼的，硬拒会把它们整个堵死。放行不代表不校验 ——
        handler 自己仍要挡住畸形输入，这一层只是尽量早失败。
        """
        parameters = ((spec.schema or {}).get("function") or {}).get("parameters")
        if not isinstance(parameters, dict):
            return ""
        try:
            import jsonschema
        except ImportError:  # pragma: no cover - 依赖已固定进 requirements
            logger.warning("jsonschema is unavailable; argument validation skipped")
            return ""
        try:
            jsonschema.validate(instance=request.arguments, schema=parameters)
        except jsonschema.ValidationError as exc:
            # 只回字段路径与原因，不回用户参数的原文。
            field = self._field_path(exc)
            reason = self._violation_reason(exc)
            return f"{ERROR_INVALID_ARGUMENTS}: {field or 'arguments'}: {reason}"
        except Exception as exc:  # schema 本身写坏了
            logger.warning("action schema is invalid (%s): %s", request.name, exc)
            return ""
        return ""

    @staticmethod
    def _field_path(exc: Any) -> str:
        """把 jsonschema 的报错定位成"哪个字段"。

        `required` 是个例外：报错的 `absolute_path` 指向**父对象**（空路径），
        缺的那个字段名藏在 `validator_value`（必填字段名的列表）与
        `message`（"'value' is a required property"）里。只回父路径的话，
        错误变成 `invalid_arguments: arguments: required` —— 用户看不出缺了哪个字段，
        排障只能去翻 schema 原文。

        这条路径是"参数错误要指向调用方"的具体落实：调用方要知道改哪个字段。
        """
        if exc.validator == "required":
            # validator_value 是必填字段名列表；schema 里可能声明了多个必填项。
            missing = exc.validator_value
            if isinstance(missing, (list, tuple)) and missing:
                names = [str(item) for item in missing]
            else:
                names = []
            if not names:
                # 兜底：从 message 里抠第一对引号包住的字段名（jsonschema 的措辞稳定）。
                import re

                found = re.findall(r"'([^']+)'", str(exc.message or ""))
                names = [found[0]] if found else []
            parent = ".".join(str(part) for part in exc.absolute_path)
            if names:
                return ".".join([parent] if parent else [""] + names).lstrip(".")
            return parent
        return ".".join(str(part) for part in exc.absolute_path) or str(exc.json_path or "")

    @staticmethod
    def _violation_reason(exc: Any) -> str:
        """违规定性词。`required` 类报错回缺失字段名，其余回 validator 名。

        回 validator 名（而不是 message 原文）是刻意的：message 会带上用户参数值，
        而审计与前端只该看到"哪一类问题"。
        """
        if exc.validator == "required":
            return "missing_required"
        return str(exc.validator or "invalid")

    def _validate_structure(self, request: ActionRequest, spec: ToolSpec) -> ToolArgumentViolation | None:
        """JSON Schema 之后的第二道参数检查。通过返回 None。

        补的是 schema 表达不了的两件事：

        1. **必填字符串不能全是空白。** `required` 只保证"字段在" ——
           `{"objective": "   "}` 是完全合法的。但对下游来说，"给了三个空格"
           和"一个字都没给"是同一件事，而 schema 判不出这个区别。
        2. **跨字段关系。** 最典型的是 `work__submit`：`input` 该带哪些字段，
           由 `task_type` 决定。JSON Schema 没有依赖表达，所以工具交出
           `spec.argument_validator`，网关在权限判定之前调它。

        第 2 条刻意交给工具自己，而不是在网关里写一张"所有工具的跨字段规则"表：
        跨字段规则是领域知识（哪个 task_type 要哪些输入，那是 Work 的事），
        堆在这里会让网关开始认识具体工具 —— 那正是这个类存在的反面。
        """
        arguments = request.arguments if isinstance(request.arguments, Mapping) else {}

        parameters = ((spec.schema or {}).get("function") or {}).get("parameters")
        if isinstance(parameters, dict):
            required = parameters.get("required")
            properties = parameters.get("properties")
            if isinstance(required, list) and isinstance(properties, dict):
                for field in required:
                    declared = properties.get(field)
                    if not isinstance(declared, dict) or declared.get("type") != "string":
                        continue
                    value = arguments.get(field)
                    # 只拦"是字符串、但全是空白"。字段缺失归 schema 的 required 管，
                    # 类型不对归 schema 的类型管 —— 这里不替它们把同一件事报第二遍。
                    # 判据用 `contracts.is_blank` 那一份共享实现，不在这里再写一遍。
                    if isinstance(value, str) and is_blank(value):
                        return ToolArgumentViolation(
                            error=f"{ERROR_INVALID_ARGUMENTS}: {field}: blank"
                        )

        validator = spec.argument_validator
        if validator is None:
            return None
        try:
            result = validator(arguments)
        except Exception as exc:
            # 回调跑不起来**不能当成通过了**。放行会让这次调用静默失去结构保护；
            # 拒绝是一次看得见的故障，会有人去修。异常原文只进日志（可能带路径），
            # 回给调用方的是稳定码。
            logger.warning(
                "argument validator raised (%s): %s: %s", spec.name, type(exc).__name__, exc
            )
            return ToolArgumentViolation(error=ERROR_ARGUMENT_VALIDATOR_FAILED)
        if result is None:
            return None
        if isinstance(result, ToolArgumentViolation):
            return result
        # 回调签名写错了（比如学着 _validate_arguments 返回空串表示通过）。
        # 这种"通过"是假的，必须当成失败 —— 否则一个返回 False 的回调
        # 会让所有参数都顺利过关，且谁都不会发现。
        logger.warning(
            "argument validator returned %s instead of ToolArgumentViolation (%s)",
            type(result).__name__,
            spec.name,
        )
        return ToolArgumentViolation(error=ERROR_ARGUMENT_VALIDATOR_FAILED)

    async def _execute(self, request: ActionRequest, spec: ToolSpec, started: float) -> ActionObservation:
        """真正的执行：超时边界 + 异常边界 + 结果归一化。"""

        async def call_handler() -> Any:
            # handler 允许是同步函数：同步的丢线程池跑，避免阻塞事件循环。
            # 线程池里拿到的仍可能是 awaitable（比如包了个协程），所以再判一次。
            if inspect.iscoroutinefunction(spec.handler):
                return await spec.handler(request.arguments, request.scope)
            value = await asyncio.to_thread(spec.handler, request.arguments, request.scope)
            if inspect.isawaitable(value):
                return await value
            return value

        try:
            value = await asyncio.wait_for(call_handler(), timeout=spec.timeout_seconds)
        except (asyncio.TimeoutError, TimeoutError):
            # 超时之后"副作用做到哪了"问不问得出来，决定这条记录该怎么写。
            # persistent：写到一半的东西没法靠再读一次状态判断完没完，只能记
            # outcome_unknown 并禁止重放。read / transient：没有副作用，或者重新读
            # 一次就知道现在在哪，所以算普通失败、可以重试。
            if not spec.unconfirmable_on_timeout:
                return await self._finish(
                    request,
                    status="failed",
                    error=f"action {spec.name} timed out",
                    error_kind="timeout",
                    started=started,
                )
            return await self._finish(
                request,
                status="outcome_unknown",
                error=f"action {spec.name} timed out; side effect state unknown",
                error_kind="indeterminate",
                started=started,
            )
        except Exception as exc:
            # 记录原始异常供排障，但**不回给调用方**（可能含路径等敏感信息）。
            logger.warning("action failed (%s): %s: %s", spec.name, type(exc).__name__, exc)
            return await self._finish(
                request,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                error_kind=self._classify(exc),
                started=started,
            )

        return await self._normalise(request, value, started)

    async def _normalise(self, request: ActionRequest, value: Any, started: float) -> ActionObservation:
        """把 handler 的返回值归一化成 ActionObservation。

        三条约定（与原先 ToolExecutor 保持一致，改成网关后不能变）：
        1. 返回 `ToolResult` 就是最终形态，不再解包；
        2. 字典同时含 `status` 和（`output` 或 `error`）→ 按已包装结构读；
        3. 其余字典整体当 output，状态视为 `ok`。
        """
        if isinstance(value, ToolResult):
            status, code = normalise_action_status(value.status)
            if code:
                return await self._finish(
                    request,
                    status=status,
                    error=code,
                    error_kind="permanent",
                    started=started,
                    output=dict(value.output),
                )
            return await self._finish(
                request,
                status=status,
                output=dict(value.output),
                error=value.error,
                error_kind=self._clean_error_kind(value.error_kind),
                started=started,
            )

        output = dict(value or {})
        if "status" in output and ("output" in output or "error" in output):
            raw_status = str(output.get("status") or "ok")
            status, code = normalise_action_status(raw_status)
            if code:
                # handler 用了词表外的状态词：不许静默当成功。
                return await self._finish(
                    request,
                    status=status,
                    error=f"{code}: {raw_status}",
                    error_kind="permanent",
                    started=started,
                    output=dict(output.get("output") or {}),
                )
            return await self._finish(
                request,
                status=status,
                output=dict(output.get("output") or {}),
                error=str(output.get("error") or ""),
                error_kind=self._clean_error_kind(output.get("error_kind")),
                started=started,
            )

        return await self._finish(request, status="completed", output=output, started=started)

    async def _wait_for_approval(self, request: ActionRequest, spec: ToolSpec, started: float) -> ActionObservation:
        """高风险动作：落审批、返回 waiting_approval、**不执行**。"""
        if self.approval_sink is None:
            return await self._finish(
                request,
                status="rejected",
                error="approval is required but no approval sink is configured",
                error_kind="permission",
                started=started,
            )
        try:
            approval_id = self.approval_sink(request, spec)
        except Exception as exc:
            logger.warning("approval sink failed (%s): %s", spec.name, exc)
            return await self._finish(
                request,
                status="failed",
                error="failed to record approval request",
                error_kind="permanent",
                started=started,
            )
        observation = await self._finish(
            request,
            status="waiting_approval",
            approval_id=approval_id,
            started=started,
        )
        # approval_code 保持与审批编号一致：前端按稳定码取本地化文案。
        return ActionObservation(
            action_id=observation.action_id,
            name=observation.name,
            status=observation.status,
            output=observation.output,
            approval_id=approval_id,
            approval_code=spec.confirmation_title or "",
            duration_ms=observation.duration_ms,
            origin=observation.origin,
            # 这一条是网关自己的闸门挂起的，不是 handler 带回来的结论。
            awaiting_approval=True,
        )

    def _replay(self, request: ActionRequest) -> ActionObservation:
        """重复编号：回放既有终态，不执行。

        三个必须讲清楚的点：

        1. **不重跑**。这是本方法存在的全部理由 —— 重复点确认、网络重发、
           审批恢复重入，都必须复用既有结果而不是再跑一遍副作用。

        2. **拿不回 output**。审计表刻意只存字段名与终态，不存返回内容
           （见 `action_journal` 模块头：参数值里有本机路径、坐标、用户原话）。
           所以这里回的 output 一定是空的，`replayed=True` 就是给调用方
           区分"这是我刚跑出来的结果"还是"这是我读到的历史终态"用的。
           把它伪装成一次正常完成（假装 output 是空的"成功"）会让调用方
           以为动作产出了空结果 —— 那是撒谎，不是复用。

        3. **挂着 `running` 的历史记录不重跑**。上一次没跑完就换了进程时，
           副作用的最终状态无法确认，按 `outcome_unknown` 回。
           重跑就是拿副作用赌第二次。
        """
        record = self.journal.get(request.action_id) if self.journal is not None else None
        if record is None:  # pragma: no cover - register 返回 False 时记录必然存在
            return self._failed(request, ERROR_INTERNAL, "action record disappeared")
        status = str(record.get("status") or "")
        if status == "running":
            status = "outcome_unknown"
            error_kind = "indeterminate"
        else:
            error_kind = str(record.get("error_kind") or "")
        return ActionObservation(
            action_id=request.action_id,
            name=str(record.get("name") or request.name),
            status=status,
            error_kind=error_kind,
            approval_id=str(record.get("approval_id") or ""),
            duration_ms=int(record.get("duration_ms") or 0),
            origin=str(record.get("origin") or request.origin),
            replayed=True,
        )

    async def _finish(
        self,
        request: ActionRequest,
        *,
        status: str,
        output: dict[str, Any] | None = None,
        error: str = "",
        error_kind: str = "",
        approval_id: str = "",
        started: float,
    ) -> ActionObservation:
        """记终态并组装观察结果。所有出口都走这里，保证审计不漏。"""
        duration_ms = int((time.monotonic() - started) * 1000)
        if self.journal is not None:
            try:
                self.journal.finish(
                    request.action_id,
                    status=status,
                    error_kind=error_kind,
                    approval_id=approval_id,
                    duration_ms=duration_ms,
                )
            except Exception:
                # 记账失败不能把一次已经完成的动作报成失败。
                logger.exception("action journal write failed (%s)", request.action_id)
        return ActionObservation(
            action_id=request.action_id,
            name=request.name,
            status=status,
            output=dict(output or {}),
            error=error,
            error_kind=error_kind,
            approval_id=approval_id,
            duration_ms=duration_ms,
            origin=request.origin,
        )

    def _failed(self, request: ActionRequest, kind: str, message: str = "") -> ActionObservation:
        """还没登记就失败的路径（未知动作、非法决定）用这个。

        `error` 一律写成 **`码: 说明`**：码是稳定的（下游按它分支），
        说明是给人看的。以前未知动作回的是 `unknown action: xxx` 这种纯文本，
        下游想判断"是不是工具不存在"只能做字符串包含 ——
        那种判断会在措辞被调整时静默失效，而且没人会想到去改它。
        """
        return ActionObservation(
            action_id=request.action_id,
            name=request.name,
            status="failed",
            error=f"{kind}: {message}" if message else kind,
            error_kind="permanent",
            origin=request.origin,
        )

    @staticmethod
    def _classify(exc: BaseException) -> str:
        """异常归类。顺序沿用 tools.classify_tool_error，不要调换。

        `PermissionError` 必须排在 `is_retryable` 之前：它是 OSError 的子类，
        否则会被误判成可重试，而权限问题重试无效，得人来授权。
        """
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, PermissionError):
            return "permission"
        if is_retryable(exc):
            return "transient"
        return "permanent"

    @staticmethod
    def _clean_error_kind(value: Any) -> str:
        """handler 自己声明的 error_kind 必须在词表里，否则丢弃。

        宁可留成"未分类"，也不能让拼错的字符串流到下游当判断依据。
        """
        from .contracts import ACTION_ERROR_KINDS

        kind = str(value or "")
        return kind if kind in ACTION_ERROR_KINDS else ""


__all__ = ["ActionGateway", "ApprovalSink", "LateRegistry", "ACTION_STATUS_INVALID", "Scope"]
