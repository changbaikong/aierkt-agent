"""Task-level provider contract used by the Host control plane."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from .contracts import Scope, ToolArgumentViolation, is_blank


#: ``unknown_result`` is its own terminal status on purpose: it means "the task
#: stopped, and we cannot prove whether its side effects happened". Folding it
#: into ``failed`` would invite an automatic retry of an operation that may
#: already be half-applied — see ``WorkHost.retry``.
WORK_RESULT_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "interrupted", "waiting_approval", "unknown_result"}
)
LOCAL_WORK_TASK_TYPES = frozenset(
    {"file.list", "file.organize", "screen.capture", "system.volume.adjust"}
)
WORK_TASK_TYPES = LOCAL_WORK_TASK_TYPES | {"code.agent"}

#: 每个 task_type 必须在 ``input`` 里带上的字段。**这是 Work 领域的契约，唯一来源在这里。**
#:
#: 为什么它属于 Work 而不是路由：`input` 该带哪些字段，取决于这个 task_type
#: 到底要干什么 —— 那是 Work 的事。路由层以前自己留了一份（`routing._REQUIRED_INPUT`），
#: 于是"哪个类型要哪些输入"这件事在两个模块里各记一遍，改一处忘一处就开始漂移。
#:
#: 两处消费它，职责各不相同：
#:   1. ``work__submit`` 的 argument_validator —— 在进 WorkHost 之前拦住缺字段的提交；
#:   2. ``agent_core/routing.py`` —— **只读**，用来判断"这句话够不够格生成一条建议"。
#:      它读到的是事实，不是执行权：建议永远只是建议，没有执行路径。
#: 不经工具入口的兼容调用由具体 Provider 在真正执行前自行验证；Host 不能只看
#: ``input``，因为 ``WorkRequest.task_input`` 兼容 ``input or context``。
#:
#: 表里没有的 task_type 按"不需要额外输入"处理。显式写 ``()`` 而不省略，
#: 是为了让"想过之后确认不需要"和"忘了登记"能分开 —— 前者一眼看得见，
#: 后者会被 ``test_required_input_matches_known_task_types`` 抓住。
WORK_TASK_INPUT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "code.agent": ("project_root",),
    "file.list": ("directory",),
    "file.organize": ("directory",),
    "screen.capture": (),
    "system.volume.adjust": ("percent",),
}


def _input_missing(payload: Mapping[str, Any], field: str) -> bool:
    """"这一项等于没给吗"，按值的种类分开判。

    字符串用共享的 `is_blank`（空串、纯空白都算没给）；其余类型只判"在不在且不是
    None"。**不能对数字也套 `is_blank`**：`system.volume.adjust` 的 `percent=0`
    是"静音"，那是一个明确的意图，不是"没给"。`is_blank(0)` 为真，
    套上去会把静音请求当成缺参数拒掉 —— 这类误伤只有 0 这一档输入能触发，
    测试里很容易漏掉。
    """
    if field not in payload:
        return True
    value = payload[field]
    if isinstance(value, str):
        return is_blank(value)
    return value is None


def validate_work_submit_arguments(arguments: Mapping[str, Any]) -> ToolArgumentViolation | None:
    """`work__submit` 的结构校验：`input` 必须带齐这个 `task_type` 要的字段。

    通过返回 None，不通过返回带结构化细节的违规。**判据只来自上面那张表**，
    这里不另写一份 —— 两份判据迟早会给出不同答案，而症状是
    "网关放行了、Host 又拒了"（或者反过来，那个更糟）。

    为什么这个函数住在 `agent_core` 而不是 `agent_tools/work.py`：
    参数要求本身是 Work 领域知识，路由也要只读这张表；放在表和契约旁边，
    工具层不需要反过来成为领域规则的唯一来源。

    "空"的判据与网关那条必填字符串检查同源（`contracts.is_blank`），
    所以纯空白的 `directory` 在这里和在那里是同一个答案。

    `task_type` 自身的存在性与取值合法性归 `ToolSpec.schema` 的
    `required` / `enum` 管，这里不替它把同一件事报第二遍：
    走到这个函数时，它要么合法，要么是 schema 与这张表不同步
    （那种不同步由 `test_required_input_matches_known_task_types` 抓）。
    """
    task_type = arguments.get("task_type")
    if not isinstance(task_type, str) or task_type not in WORK_TASK_INPUT_REQUIREMENTS:
        return None
    payload = arguments.get("input")
    if not isinstance(payload, Mapping):
        payload = {}
    missing = [
        field
        for field in WORK_TASK_INPUT_REQUIREMENTS[task_type]
        if _input_missing(payload, field)
    ]
    if not missing:
        return None
    return ToolArgumentViolation(
        error="missing_task_input",
        output={"task_type": task_type, "missing": missing},
    )


def _event_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class WorkProviderCapabilities:
    provider_id: str
    task_types: tuple[str, ...] = ()
    supports_cancel: bool = False
    supports_resume: bool = False
    #: 允许这个 provider 与另一个 provider 共享同一个 task_type，并且**不夺取**
    #: 默认执行权：它只能被显式的 provider 标识选中。
    #:
    #: 通用注册表允许同类型的可选执行者，但生产代码任务只注册 DeepSeek Harness。
    #: 默认 False 表示独占；多个独占执行者争夺同一任务类型会直接报错。
    selectable: bool = False


@dataclass(frozen=True)
class TaskTypeCapabilities:
    """按 task_type 的能力报告，是 Host 匹配检查的唯一输入。

    为什么不用 ``WorkProviderCapabilities`` 的两个全局布尔：一个组合 provider
    汇总多个执行者，全局 ``any()`` 会把 A 的能力冒充给 B 的任务。这里的每一项
    都回答"**这个** task_type 交给**这个** provider，能不能做到"，做不做得到以
    可验证的执行契约为准，与模型强弱无关。
    """

    task_type: str
    #: 这个 provider 认领该 task_type（任务类型归属）。
    can_execute: bool = False
    #: 支持取消，并且取消后能给出"确实停了"的确认（给不出就进 unknown_result）。
    supports_cancel: bool = False
    cancel_confirmable: bool = False
    #: 支持恢复，且恢复凭据（session/快照检查点）可被 Host 校验。
    supports_resume: bool = False
    resume_has_checkpoint: bool = False
    #: 执行期间持续发送进度事件（Host 的活动投影依赖它）。
    emits_events: bool = False
    #: 接受 Host 建立的任务快照，只在工作副本上动手。
    accepts_snapshot: bool = False
    #: 结果能被 Host 独立验证（不信任 provider 自报的"完成"）。
    verifiable_result: bool = False
    #: 自动应用所需的 Host 验证链（验证器 + 回写协调器）已就位。
    host_verification_chain: bool = False
    #: 沙箱契约已确认（配置校验通过，且有逐 attempt 的 fail-closed 预检）。
    sandbox_confirmed: bool = False
    #: 给人看的补充说明（为什么缺某一档能力）。
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_type": self.task_type,
            "can_execute": self.can_execute,
            "supports_cancel": self.supports_cancel,
            "cancel_confirmable": self.cancel_confirmable,
            "supports_resume": self.supports_resume,
            "resume_has_checkpoint": self.resume_has_checkpoint,
            "emits_events": self.emits_events,
            "accepts_snapshot": self.accepts_snapshot,
            "verifiable_result": self.verifiable_result,
            "host_verification_chain": self.host_verification_chain,
            "sandbox_confirmed": self.sandbox_confirmed,
            "notes": list(self.notes),
        }


def capabilities_for_task_type(provider: Any, task_type: str) -> TaskTypeCapabilities:
    """读一个 provider 对某 task_type 的能力报告。

    provider 自己实现了 ``task_capabilities`` 就信它的报告；没实现（测试替身、
    老式 provider）就只从全局布尔推导能证明的那几项，其余一律 False ——
    "没有报告"不能被当成"有能力"。
    """
    method = getattr(provider, "task_capabilities", None)
    if callable(method):
        try:
            report = method(task_type)
        except Exception:
            report = None
        if isinstance(report, TaskTypeCapabilities) and report.task_type == task_type:
            return report
    capabilities = getattr(provider, "capabilities", None)
    owned = task_type in tuple(getattr(capabilities, "task_types", ()) or ())
    cancel = owned and bool(getattr(capabilities, "supports_cancel", False))
    resume = owned and bool(getattr(capabilities, "supports_resume", False))
    return TaskTypeCapabilities(
        task_type=task_type,
        can_execute=owned,
        supports_cancel=cancel,
        cancel_confirmable=cancel,
        supports_resume=resume,
        resume_has_checkpoint=resume,
        notes=() if owned else (f"provider does not own task_type {task_type}",),
    )


@dataclass(frozen=True)
class WorkRequest:
    work_item_id: str
    attempt_id: str
    scope: Scope
    objective: str
    context: Mapping[str, Any] = field(default_factory=dict)
    task_type: str = ""
    input: Mapping[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    allowed_roots: tuple[str, ...] = ()
    budget: Mapping[str, Any] = field(default_factory=dict)
    approval_manifest: tuple[Mapping[str, Any], ...] = ()
    approval_granted: bool = False
    #: Host 解析出来的**具体** provider 标识（例如 ``deepseek_harness``）。
    #: 空串表示"没指定，按 task_type 的默认 owner 走"。调用方传来的
    #: ``input.provider`` 由 Host 解析后写进这里，Provider 自己不该再读 input 猜。
    provider_id: str = ""

    def __post_init__(self) -> None:
        task_input = dict(self.input or self.context or {})
        if "approved_moves" in task_input:
            raise ValueError("approved_moves is Host-owned and cannot be supplied in a WorkRequest")
        if "authorization" in task_input or "authorized" in task_input:
            raise ValueError("authorization is Host-owned and cannot be supplied in a WorkRequest")
        if not self.input and self.context:
            object.__setattr__(self, "input", task_input)
        if not self.context and self.input:
            object.__setattr__(self, "context", task_input)
        if not self.task_type:
            # Read compatibility for pre-task_type ledger rows. New callers must
            # pass an explicit type; execution never infers from the objective.
            text = self.objective.casefold()
            inferred = "file.organize" if any(word in text for word in ("整理", "分类", "归类")) else "file.list"
            object.__setattr__(self, "task_type", inferred)

    @property
    def task_input(self) -> Mapping[str, Any]:
        return self.input or self.context

    @property
    def provider_selector(self) -> str:
        """显式 provider 标识：``provider_id`` 优先，其次 ``input/context.provider``。

        两个位置都认，是因为调用方来自两条路：工具/聊天把标识放在 ``input`` 里
        （那是它唯一的参数口袋），而 Host 恢复长任务时从账本重建请求，走的是
        ``provider_id`` 这个正统字段。
        """
        if str(self.provider_id).strip():
            return str(self.provider_id).strip()
        for source in (self.input, self.context):
            value = source.get("provider") if isinstance(source, Mapping) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""


@dataclass(frozen=True)
class WorkEvent:
    attempt_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=_event_id)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass(frozen=True)
class WorkArtifact:
    """Provider-created artifact metadata for Host persistence and downloads."""

    artifact_id: str
    attempt_id: str
    media_type: str
    size: int
    path: str
    expires_at: str | None = None

    @property
    def size_bytes(self) -> int:
        return self.size

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "attempt_id": self.attempt_id,
            "media_type": self.media_type,
            "size": self.size,
            "size_bytes": self.size,
            "path": self.path,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class WorkResult:
    status: str
    output: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    evidence: list[Mapping[str, Any]] = field(default_factory=list)
    artifacts: list[WorkArtifact] = field(default_factory=list)
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    approval: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in WORK_RESULT_STATUSES:
            raise ValueError(f"invalid work result status: {self.status}")


WorkEventEmitter = Callable[[WorkEvent], Awaitable[None]]


@runtime_checkable
class WorkProvider(Protocol):
    capabilities: WorkProviderCapabilities

    async def execute(self, request: WorkRequest, emit: WorkEventEmitter) -> WorkResult:
        """Execute a complete task attempt and emit progress events."""

    async def cancel(self, attempt_id: str) -> bool:
        """Request cancellation at the next provider execution boundary."""

    async def resume(
        self,
        request: WorkRequest,
        checkpoint: Mapping[str, Any],
        decision: str,
        emit: WorkEventEmitter,
    ) -> WorkResult:
        """Resume an interrupted or approval-paused attempt when supported."""
