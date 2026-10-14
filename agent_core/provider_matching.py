"""Host 与 Provider 的能力匹配检查：matched / degraded / rejected。

Host 在把一个 task_type 交给一个 provider 之前，要能回答"凭什么能安全接入"。
答案只看**可验证的执行契约和运行状态**——取消能不能确认、恢复有没有检查点、
事件是否持续、快照是否被接受、结果能不能被 Host 独立验证、沙箱是否确认——
模型强弱不是匹配依据。

三档结论的边界（与设计方案一致）：

* ``matched`` —— 执行、取消、恢复、事件、快照和验证链全部可证明；
* ``degraded`` —— 可以运行，但验证链缺失，只能生成补丁、不能自动应用；
* ``rejected`` —— 缺少取消确认、恢复协议、沙箱确认或任务类型归属，不能接入。

匹配是静态评估加一次可选的 live health 探测；它不执行任务、不产生副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .work_provider import TaskTypeCapabilities, capabilities_for_task_type

MATCHED = "matched"
DEGRADED = "degraded"
REJECTED = "rejected"

#: code.agent 是长任务：可能中途被打断、可能在快照之外动手，所以恢复协议、
#: 事件流、快照和沙箱确认都是硬条件；验证链只决定 matched 还是 degraded。
CODE_AGENT_HARD_CHECKS = (
    "execute",
    "cancel",
    "cancel_confirm",
    "resume",
    "resume_checkpoint",
    "events",
    "snapshot",
    "sandbox",
)
#: 本地任务（file.list / file.organize / screen.capture / system.volume.adjust）
#: 是短任务：不建快照、不需要恢复协议；Host 审批链负责写之前的确认。
LOCAL_TASK_HARD_CHECKS = ("execute", "cancel", "cancel_confirm")

_HARD_CHECKS_BY_TASK_TYPE: dict[str, tuple[str, ...]] = {
    "code.agent": CODE_AGENT_HARD_CHECKS,
    **{task_type: LOCAL_TASK_HARD_CHECKS for task_type in (
        "file.list",
        "file.organize",
        "screen.capture",
        "system.volume.adjust",
    )},
}

#: 每个检查项对能力报告字段的映射，以及它失败时对人话解释的模板。
_CHECK_SPECS: dict[str, tuple[tuple[str, ...], str]] = {
    "execute": (("can_execute",), "provider 不认领 task_type {task_type}，没有任务类型归属"),
    "cancel": (("supports_cancel",), "provider 不支持取消该任务"),
    "cancel_confirm": (("cancel_confirmable",), "provider 无法确认取消是否生效（会进 unknown_result）"),
    "resume": (("supports_resume",), "provider 没有恢复协议，Host 重启后无法接续"),
    "resume_checkpoint": (("resume_has_checkpoint",), "provider 的恢复没有可校验的检查点（session/快照）"),
    "events": (("emits_events",), "provider 不持续发送进度事件，页面只会看到黑盒"),
    "snapshot": (("accepts_snapshot",), "provider 不接受 Host 建立的任务快照"),
    "sandbox": (("sandbox_confirmed",), "沙箱契约没有确认（配置或预检不通过）"),
}


@dataclass(frozen=True)
class MatchCheck:
    name: str
    passed: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "required": self.required,
        }


@dataclass(frozen=True)
class ProviderMatch:
    provider_id: str
    task_type: str
    verdict: str
    reasons: tuple[str, ...] = ()
    checks: tuple[MatchCheck, ...] = ()
    capabilities: TaskTypeCapabilities | None = None
    health: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "task_type": self.task_type,
            "verdict": self.verdict,
            "reasons": list(self.reasons),
            "checks": [check.as_dict() for check in self.checks],
            "capabilities": self.capabilities.as_dict() if self.capabilities else None,
            "health": dict(self.health),
        }


def _hard_checks_for(task_type: str) -> tuple[str, ...]:
    return _HARD_CHECKS_BY_TASK_TYPE.get(task_type, LOCAL_TASK_HARD_CHECKS)


def evaluate_provider_match(
    provider: Any,
    task_type: str,
    *,
    provider_id: str = "",
    health: Mapping[str, Any] | None = None,
) -> ProviderMatch:
    """评估"这个 task_type 能不能安全交给这个 provider"。

    ``health`` 是调用方拿到的 live 健康报告（没有探测就传 None）。provider 声称
    的能力与 live 探测**取更悲观的一个**：自报 matched 而探测失败时，结论必须是
    rejected，不允许"报告说行、运行起来不行"的错配。
    """
    capabilities = capabilities_for_task_type(provider, task_type)
    resolved_id = str(provider_id or getattr(capabilities, "provider_id", "") or getattr(
        getattr(provider, "capabilities", None), "provider_id", ""
    ))
    checks: list[MatchCheck] = []
    reasons: list[str] = []

    for name in _hard_checks_for(task_type):
        fields, reason_template = _CHECK_SPECS[name]
        passed = all(getattr(capabilities, field_name, False) for field_name in fields)
        checks.append(
            MatchCheck(name=name, passed=passed, detail=reason_template.format(task_type=task_type))
        )
        if not passed:
            reasons.append(reason_template.format(task_type=task_type))

    # live 健康探测：探测存在且明确不可用 → 无论自报什么都拒绝。
    # 分层报告里 ``executable`` 是更强的一票：配置、沙箱、无模型 ACP 握手
    # 全部通过才算可执行；握手失败而沙箱可用（available=True）时，旧规则
    # 会漏放行，新规则必须在 rejected 拦住。
    health_available: bool | None = None
    health_executable: bool | None = None
    health_payload: dict[str, Any] = dict(health or {})
    if health_payload:
        health_available = bool(health_payload.get("available", False))
        if "executable" in health_payload:
            health_executable = bool(health_payload.get("executable"))
        else:
            # 旧格式报告没有 executable 字段：退回 available 规则，不惩罚
            # 还没升级的 provider。
            health_executable = health_available
    if health_executable is False:
        stage = str(health_payload.get("failure_stage") or "")
        reason = str(health_payload.get("reason") or "provider 健康检查不可用")
        stage_note = f"（失败阶段：{stage}）" if stage else ""
        checks.append(MatchCheck(name="health", passed=False, detail=f"{reason}{stage_note}"))
        reasons.append(f"provider 当前不可执行{stage_note}：{reason}")
    elif health_available is False:
        reason = str(health_payload.get("reason") or "provider 健康检查不可用")
        checks.append(MatchCheck(name="health", passed=False, detail=reason))
        reasons.append(f"provider 当前不可用：{reason}")
    elif health_executable is True or health_available is True:
        checks.append(MatchCheck(name="health", passed=True, detail="健康检查通过"))

    verdict = REJECTED if reasons else _settle_non_rejected(
        task_type, capabilities, checks, reasons, health_payload
    )
    return ProviderMatch(
        provider_id=resolved_id,
        task_type=task_type,
        verdict=verdict,
        reasons=tuple(reasons),
        checks=tuple(checks),
        capabilities=capabilities,
        health=health_payload,
    )


def _settle_non_rejected(
    task_type: str,
    capabilities: TaskTypeCapabilities,
    checks: list[MatchCheck],
    reasons: list[str],
    health: Mapping[str, Any],
) -> str:
    """硬条件全过之后的分档：只差验证链 → degraded，否则 matched。"""
    if task_type != "code.agent":
        # 本地任务不走"验证后写回"的链路，验证链缺失不构成降级。
        return MATCHED
    if capabilities.host_verification_chain:
        return MATCHED
    # code.agent 的自动应用依赖 Host 验证链；链不在就是"能跑，但只出补丁"。
    chain_in_health = bool((health.get("host_chain") or {}).get("verifier")) if health else False
    if chain_in_health:
        # 报告与探测不一致，按悲观方向收：仍按 degraded 处理，但记录矛盾。
        checks.append(
            MatchCheck(
                name="verification",
                passed=False,
                detail="能力报告缺少验证链标记，按降级处理（自动应用关闭）",
            )
        )
    else:
        checks.append(
            MatchCheck(
                name="verification",
                passed=False,
                detail="Host 验证链不可用：只能生成补丁，不能自动应用",
            )
        )
    reasons.append("Host 验证链不可用：只能生成补丁，不能自动应用")
    return DEGRADED
