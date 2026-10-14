"""套3 · 工具授权矩阵。

把 agent_core/tools.py 的注册期校验 + agent_core/work_provider.py 的 WorkRequest Host-owned
防线 + agent_core/control_plan.py 的 _scan_forbidden 三道防线钉一遍。
每条 case 带 case_id（M3-AUTH-xxx）+ @pytest.mark.matrix，红线 case 加 -R 后缀。

分叉点对照（C 层工具授权，详见 plan）：
  C21  deterministic_route_allowed + requires_confirmation(risk=high) → 注册期 ValueError
  C22  deterministic_route_allowed + 非 read 效果 + 非 hard_routable → 注册期 ValueError
  +    deterministic_route_allowed + effect=read → 合法
  +    deterministic_route_allowed + hard_routable=True → 合法（work__submit 这条）
  C23  work__submit(low+hard_routable) 可硬拦 vs plan_submit(high) 永不可硬拦（语义对比）
  C24  同名重复注册 → ValueError
  Host-owned 注入攻击（三道防线）：
    ① WorkRequest.__post_init__ 拒 approved_moves/authorization/authorized
    ② _scan_forbidden 拒 plan step input 的 HOST_OWNED_INPUT_KEYS
    ③ _scan_forbidden 拒 SCOPE_INPUT_KEYS（owner_id/channel 等）
    ④ _scan_forbidden 拒 FORBIDDEN_INPUT_TOKEN（AGENT_WORK_ROOTS）
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, Scope, ToolSpec
from agent_core.control_plan import (
    FORBIDDEN_INPUT_TOKEN,
    HOST_OWNED_INPUT_KEYS,
    SCOPE_INPUT_KEYS,
    _scan_forbidden,
)
from agent_core.tools import ToolRegistry
from agent_core.work_provider import WorkRequest

pytestmark = pytest.mark.matrix

_SCOPE = Scope(owner_id="tester", channel="test", conversation_id="conv-1")


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }


async def _noop(arguments: dict, scope: Scope) -> dict:
    return {}


def _spec(
    name: str,
    *,
    risk: str = "low",
    effect: str = EFFECT_PERSISTENT,
    hard_routable: bool = False,
    deterministic_route_allowed: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        schema=_schema(name),
        handler=_noop,
        risk=risk,
        effect=effect,
        hard_routable=hard_routable,
        deterministic_route_allowed=deterministic_route_allowed,
    )


# ──────────────────────── C21 deterministic + high risk → ValueError ────────────────────────


def test_M3_AUTH_R001_deterministic_plus_high_risk_rejected() -> None:
    """C21 红线：deterministic_route_allowed + risk=high（requires_confirmation）→ 注册期 ValueError。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="non-confirmed tool"):
        registry.register(
            _spec("risky__route", risk="high", deterministic_route_allowed=True)
        )


# ──────────────────────── C22 deterministic 需 read 效果或 hard_routable ────────────────────────


def test_M3_AUTH_R002_deterministic_without_read_effect_or_hard_routable_rejected() -> None:
    """C22 红线：deterministic + 非 read 效果 + 非 hard_routable → ValueError。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="read-effect tool or an explicit hard_routable"):
        registry.register(
            _spec("bad__route", effect=EFFECT_PERSISTENT, hard_routable=False, deterministic_route_allowed=True)
        )


def test_M3_AUTH_003_deterministic_with_read_effect_accepted() -> None:
    """C22 合法：deterministic + effect=read → 注册成功。"""
    registry = ToolRegistry()
    registry.register(_spec("safe__ro", effect=EFFECT_READ, deterministic_route_allowed=True))
    assert registry.resolve("safe__ro") is not None


def test_M3_AUTH_004_deterministic_with_hard_routable_accepted() -> None:
    """C22 合法：deterministic + hard_routable=True（非只读但显式授权）→ 注册成功。
    这是 work__submit 的授权路径：有副作用但注册方明确允许运行时直接发起。"""
    registry = ToolRegistry()
    registry.register(_spec("work__submit", hard_routable=True, deterministic_route_allowed=True))
    assert registry.resolve("work__submit") is not None


# ──────────────────────── C23 work__submit 可硬拦 vs plan_submit 不可 ────────────────────────


def test_M3_AUTH_005_work_submit_is_hard_routable_not_high_risk() -> None:
    """C23：work__submit 合法形态 = low + hard_routable + deterministic → 可硬拦。"""
    registry = ToolRegistry()
    spec = _spec("work__submit", risk="low", hard_routable=True, deterministic_route_allowed=True)
    registry.register(spec)
    assert not spec.requires_confirmation  # low risk → 不需确认


def test_M3_AUTH_R006_plan_submit_high_risk_cannot_be_deterministic() -> None:
    """C23 红线：plan_submit 是 high risk，若标 deterministic_route_allowed → 注册期 ValueError。
    所以多步场景只能给 SOFT 建议，绝不能由运行时直接提交（绕过确认）。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(
            _spec("plan_submit", risk="high", deterministic_route_allowed=True)
        )


def test_M3_AUTH_007_plan_submit_without_deterministic_is_accepted() -> None:
    """C23 对照：plan_submit(high) 不标 deterministic_route_allowed → 注册成功（走主 loop 确认）。"""
    registry = ToolRegistry()
    spec = _spec("plan_submit", risk="high", deterministic_route_allowed=False)
    registry.register(spec)
    assert spec.requires_confirmation  # high risk → 必须确认


# ──────────────────────── C24 同名重复 / 空 name ────────────────────────


def test_M3_AUTH_008_duplicate_name_rejected() -> None:
    """C24：同名工具重复注册 → ValueError（绝不静默覆盖）。"""
    registry = ToolRegistry()
    registry.register(_spec("dup__tool"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_spec("dup__tool"))


def test_M3_AUTH_009_empty_name_rejected() -> None:
    """C24：空 name → ValueError。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError, match="tool name is required"):
        registry.register(_spec("   "))


# ──────────────────────── Host-owned 注入攻击 ① WorkRequest 防线 ────────────────────────


def test_M3_AUTH_R010_workrequest_with_approved_moves_rejected() -> None:
    """红线①：WorkRequest.input 含 approved_moves → __post_init__ ValueError。
    approved_moves 是 Host 在用户批准后才填的，LLM/plan 不能自己塞。"""
    with pytest.raises(ValueError, match="approved_moves is Host-owned"):
        WorkRequest(
            work_item_id="w1",
            attempt_id="a1",
            scope=_SCOPE,
            objective="整理",
            task_type="file.organize",
            input={"directory": "D:\\A", "approved_moves": [{"src": "x", "dst": "y"}]},
        )


def test_M3_AUTH_R011_workrequest_with_authorization_rejected() -> None:
    """红线①：WorkRequest.input 含 authorization → ValueError。"""
    with pytest.raises(ValueError, match="authorization is Host-owned"):
        WorkRequest(
            work_item_id="w1",
            attempt_id="a1",
            scope=_SCOPE,
            objective="整理",
            task_type="file.organize",
            input={"directory": "D:\\A", "authorization": "forged"},
        )


def test_M3_AUTH_R012_workrequest_with_authorized_rejected() -> None:
    """红线①：WorkRequest.input 含 authorized → ValueError（authorization 的变体）。"""
    with pytest.raises(ValueError, match="authorization is Host-owned"):
        WorkRequest(
            work_item_id="w1",
            attempt_id="a1",
            scope=_SCOPE,
            objective="整理",
            task_type="file.organize",
            input={"directory": "D:\\A", "authorized": True},
        )


def test_M3_AUTH_R013_workrequest_context_with_host_owned_rejected() -> None:
    """红线①：WorkRequest.context（不是 input）含 approved_moves → 同样 ValueError（两处都扫）。"""
    with pytest.raises(ValueError, match="approved_moves is Host-owned"):
        WorkRequest(
            work_item_id="w1",
            attempt_id="a1",
            scope=_SCOPE,
            objective="整理",
            task_type="file.organize",
            context={"approved_moves": []},
        )


def test_M3_AUTH_014_workrequest_clean_input_accepted() -> None:
    """对照：干净的 WorkRequest.input → 构造成功，task_input 可读。"""
    req = WorkRequest(
        work_item_id="w1",
        attempt_id="a1",
        scope=_SCOPE,
        objective="整理",
        task_type="file.organize",
        input={"directory": "D:\\A"},
    )
    assert req.task_input == {"directory": "D:\\A"}


# ──────────────────────── Host-owned 注入攻击 ② _scan_forbidden plan step 防线 ────────────────────────


def _scan(input: Any) -> list[str]:
    problems: list[str] = []
    _scan_forbidden(input, path="step.input", problems=problems)
    return problems


def test_M3_AUTH_R015_scan_rejects_host_owned_keys() -> None:
    """红线②：plan step input 含 HOST_OWNED_INPUT_KEYS（approved_moves/authorization/authorized/approval_granted）
    → _scan_forbidden 记 problem。"""
    for key in HOST_OWNED_INPUT_KEYS:
        problems = _scan({key: "forged"})
        assert any("Host-owned" in p for p in problems), f"{key} 未被拦"


def test_M3_AUTH_R016_scan_rejects_scope_keys() -> None:
    """③：plan step input 含 SCOPE_INPUT_KEYS（owner_id/channel/scope/allowed_roots 等）
    → _scan_forbidden 记 problem（step 不能自己改身份或沙箱）。"""
    for key in SCOPE_INPUT_KEYS:
        problems = _scan({key: "forged"})
        assert any("scope" in p.lower() or "Host scope" in p for p in problems), f"{key} 未被拦"


def test_M3_AUTH_R017_scan_rejects_agent_work_roots_token() -> None:
    """④：input 值含 AGENT_WORK_ROOTS 字符串 → problem（篡改运行时根）。"""
    problems = _scan({"note": f"set {FORBIDDEN_INPUT_TOKEN}=D:\\evil"})
    assert any(FORBIDDEN_INPUT_TOKEN in p for p in problems)


def test_M3_AUTH_R018_scan_rejects_agent_work_roots_in_key() -> None:
    """④：input 键名含 AGENT_WORK_ROOTS → problem。"""
    problems = _scan({f"my_{FORBIDDEN_INPUT_TOKEN}": 1})
    assert any(FORBIDDEN_INPUT_TOKEN in p for p in problems)


def test_M3_AUTH_019_scan_recurses_into_nested_dict() -> None:
    """②递归：嵌套 dict 里的 approved_moves 也要被扫到。"""
    problems = _scan({"metadata": {"inner": {"approved_moves": []}}})
    assert any("approved_moves" in p and "Host-owned" in p for p in problems)


def test_M3_AUTH_020_scan_recurses_into_list() -> None:
    """②递归：list 元素里的 scope 键也要被扫到。"""
    problems = _scan({"items": [{"owner_id": "forged"}]})
    assert any("owner_id" in p for p in problems)


def test_M3_AUTH_021_scan_accepts_clean_input() -> None:
    """对照：干净的 plan step input → 无 problem。"""
    problems = _scan({"directory": "D:\\A", "sort_by": "extension"})
    assert problems == []
