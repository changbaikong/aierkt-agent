"""套4 · 计划编排矩阵。

把 agent_core/control_plan.py 的 validate_plan_definition 全部校验项钉一遍，
每条 case 带 case_id（M4-PLAN-xxx）+ @pytest.mark.matrix。

分叉点对照（D 层计划，详见 plan）：
  D25  validate_plan_definition 校验项（按 control_plan.py L292-381 顺序）：
       ① objective 非空  ② ≥1 步  ③ ≤MAX_PLAN_STEPS(32)  ④ raw 是 Mapping
       ⑤ step_id 合法正则  ⑥ step_id 不重复  ⑦ task_type ∈ WORK_TASK_TYPES
       ⑧ task_type ∈ provider_task_types  ⑨ step_objective 非空  ⑩ input 是 Mapping
       ⑪ _scan_forbidden 扫 Host-owned/SCOPE/FORBIDDEN  ⑫ depends_on 不重复
       ⑬ 不自环  ⑭ requires_approval 只在 APPROVAL_ELIGIBLE  ⑮ failure_policy ∈ 三档
       ⑯ depends_on 引用存在  ⑰ _topological_order 检测环
       ⑱ ${step:x.output.y} 引用必须声明在 depends_on
  D26  failure_policy 三档 stop/retry/skip + DEFAULT_FAILURE_POLICY=stop
  D27/D29 waiting_approval 全流程 / plan step→WorkItem 转换：依赖 work_host/plan_scheduler
       集成，本矩阵聚焦 validate 契约层，集成留 test_work_host/test_control_plan。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.control_plan import (
    APPROVAL_ELIGIBLE_TASK_TYPES,
    DEFAULT_FAILURE_POLICY,
    FAILURE_POLICIES,
    MAX_PLAN_STEPS,
    PlanStepDefinition,
    PlanValidationError,
    validate_plan_definition,
)
from agent_core.work_provider import WORK_TASK_TYPES

pytestmark = pytest.mark.matrix


def _step(step_id: str = "s1", **kw: Any) -> dict:
    """合法的 file.list step，用 kw 覆盖单项。"""
    base = {
        "step_id": step_id,
        "task_type": "file.list",
        "objective": "列出目录文件",
        "input": {"directory": "D:\\Downloads"},
        "depends_on": (),
        "requires_approval": False,
        "failure_policy": "stop",
    }
    base.update(kw)
    return base


def _valid_plan(steps: list[dict] | None = None, **kw: Any) -> dict:
    """合法的 plan 参数包。"""
    plan = {"objective": "整理下载目录", "raw_steps": steps or [_step()]}
    plan.update(kw)
    return plan


# ──────────────────────── D25 合法计划通过 ────────────────────────


def test_M4_PLAN_001_valid_plan_returns_definitions() -> None:
    """D25 合法：单步 file.list → 返回 PlanStepDefinition tuple，字段保留。"""
    defs = validate_plan_definition(**_valid_plan())
    assert len(defs) == 1
    assert isinstance(defs[0], PlanStepDefinition)
    assert defs[0].step_id == "s1"
    assert defs[0].task_type == "file.list"
    assert defs[0].input == {"directory": "D:\\Downloads"}


def test_M4_PLAN_002_two_step_with_dependency_accepted() -> None:
    """D25 合法：两步 s1→s2 依赖，s2 引用 s1 输出且声明 depends_on → 通过。"""
    steps = [
        _step("s1"),
        _step("s2", task_type="file.organize", depends_on=("s1",),
              input={"directory": "${step:s1.output.directory}"}),
    ]
    defs = validate_plan_definition(**_valid_plan(steps))
    assert {d.step_id for d in defs} == {"s1", "s2"}


# ──────────────────────── D25 ①~④ objective / 步数 / Mapping ────────────────────────


def test_M4_PLAN_003_empty_objective_rejected() -> None:
    """①：objective 空 → PlanValidationError。"""
    with pytest.raises(PlanValidationError, match="objective is required"):
        validate_plan_definition(objective="  ", raw_steps=[_step()])


def test_M4_PLAN_004_no_steps_rejected() -> None:
    """②：raw_steps 空 → 至少一步。"""
    with pytest.raises(PlanValidationError, match="at least one step"):
        validate_plan_definition(objective="x", raw_steps=[])


def test_M4_PLAN_005_too_many_steps_rejected() -> None:
    """③：> MAX_PLAN_STEPS(32) → 拒绝。"""
    steps = [_step(f"s{i}") for i in range(MAX_PLAN_STEPS + 1)]
    with pytest.raises(PlanValidationError, match="at most"):
        validate_plan_definition(objective="x", raw_steps=steps)


def test_M4_PLAN_006_step_not_mapping_rejected() -> None:
    """④：raw 不是 Mapping → 拒绝。"""
    with pytest.raises(PlanValidationError, match="must be an object"):
        validate_plan_definition(objective="x", raw_steps=["not a dict"])


# ──────────────────────── D25 ⑤~⑥ step_id ────────────────────────


def test_M4_PLAN_007_invalid_step_id_rejected() -> None:
    """⑤：step_id 不合法（含空格/特殊字符）→ 拒绝。"""
    with pytest.raises(PlanValidationError, match="invalid step_id"):
        validate_plan_definition(objective="x", raw_steps=[_step("bad id")])


def test_M4_PLAN_008_empty_step_id_rejected() -> None:
    """⑤：step_id 空 → 拒绝。"""
    with pytest.raises(PlanValidationError, match="invalid step_id"):
        validate_plan_definition(objective="x", raw_steps=[_step("")])


def test_M4_PLAN_009_duplicate_step_id_rejected() -> None:
    """⑥：step_id 重复 → 拒绝。"""
    steps = [_step("s1"), _step("s1", task_type="file.organize")]
    with pytest.raises(PlanValidationError, match="duplicate step_id"):
        validate_plan_definition(objective="x", raw_steps=steps)


# ──────────────────────── D25 ⑦~⑧ task_type ────────────────────────


def test_M4_PLAN_010_unknown_task_type_rejected() -> None:
    """⑦：task_type 不在 WORK_TASK_TYPES → 拒绝。"""
    with pytest.raises(PlanValidationError, match="unknown task_type"):
        validate_plan_definition(objective="x", raw_steps=[_step(task_type="email.send")])


def test_M4_PLAN_011_unsupported_by_provider_rejected() -> None:
    """⑧：task_type 合法但 provider 不支持 → 拒绝（provider_task_types 限制）。"""
    with pytest.raises(PlanValidationError, match="does not support"):
        validate_plan_definition(
            objective="x",
            raw_steps=[_step(task_type="file.organize")],
            provider_task_types=["file.list"],
            provider_id="local",
        )


# ──────────────────────── D25 ⑨~⑪ objective/input ────────────────────────


def test_M4_PLAN_012_empty_step_objective_rejected() -> None:
    """⑨：step_objective 空 → 拒绝。"""
    with pytest.raises(PlanValidationError, match="requires an objective"):
        validate_plan_definition(objective="x", raw_steps=[_step(objective="")])


def test_M4_PLAN_013_input_not_mapping_rejected() -> None:
    """⑩：input 不是 Mapping → 拒绝。"""
    with pytest.raises(PlanValidationError, match="input must be an object"):
        validate_plan_definition(objective="x", raw_steps=[_step(input=["not", "a", "dict"])])


def test_M4_PLAN_014_input_with_host_owned_rejected() -> None:
    """⑪：input 含 approved_moves（Host-owned）→ _scan_forbidden 记 problem → 拒绝。"""
    with pytest.raises(PlanValidationError, match="Host-owned"):
        validate_plan_definition(
            objective="x",
            raw_steps=[_step(input={"directory": "D:\\A", "approved_moves": []})],
        )


def test_M4_PLAN_015_input_with_scope_key_rejected() -> None:
    """⑪：input 含 owner_id（SCOPE）→ 拒绝（step 不能改自己身份）。"""
    with pytest.raises(PlanValidationError, match="scope"):
        validate_plan_definition(
            objective="x",
            raw_steps=[_step(input={"directory": "D:\\A", "owner_id": "forged"})],
        )


# ──────────────────────── D25 ⑫~⑬ depends_on ────────────────────────


def test_M4_PLAN_016_duplicate_dependency_rejected() -> None:
    """⑫：depends_on 列同一依赖两次 → 拒绝。"""
    with pytest.raises(PlanValidationError, match="dependency twice"):
        validate_plan_definition(
            objective="x",
            raw_steps=[_step("s2", depends_on=("s1", "s1"))],
        )


def test_M4_PLAN_017_self_dependency_rejected() -> None:
    """⑬：depends_on 含自己 → 拒绝。"""
    with pytest.raises(PlanValidationError, match="cannot depend on itself"):
        validate_plan_definition(objective="x", raw_steps=[_step("s1", depends_on=("s1",))])


# ──────────────────────── D25 ⑭ requires_approval ────────────────────────


def test_M4_PLAN_018_approval_on_ineligible_task_rejected() -> None:
    """⑭：file.list 标 requires_approval=True 但不在 APPROVAL_ELIGIBLE → 拒绝。"""
    assert "file.list" not in APPROVAL_ELIGIBLE_TASK_TYPES
    with pytest.raises(PlanValidationError, match="cannot require approval"):
        validate_plan_definition(
            objective="x",
            raw_steps=[_step(requires_approval=True)],
        )


def test_M4_PLAN_019_approval_on_eligible_task_accepted() -> None:
    """⑭对照：file.organize 标 requires_approval=True（在 ELIGIBLE）→ 通过。"""
    assert "file.organize" in APPROVAL_ELIGIBLE_TASK_TYPES
    defs = validate_plan_definition(
        objective="x",
        raw_steps=[_step(task_type="file.organize", requires_approval=True)],
    )
    assert defs[0].requires_approval is True


# ──────────────────────── D25 ⑯~⑰ depends_on 引用 / 环 ────────────────────────


def test_M4_PLAN_020_dependency_on_unknown_step_rejected() -> None:
    """⑯：depends_on 引用不存在的 step → 拒绝。"""
    with pytest.raises(PlanValidationError, match="unknown step"):
        validate_plan_definition(objective="x", raw_steps=[_step("s1", depends_on=("ghost",))])


def test_M4_PLAN_021_dependency_cycle_rejected() -> None:
    """⑰：s1→s2→s1 环 → _topological_order 抛错。"""
    steps = [
        _step("s1", depends_on=("s2",)),
        _step("s2", task_type="file.organize", depends_on=("s1",)),
    ]
    with pytest.raises(PlanValidationError, match="cycle"):
        validate_plan_definition(objective="x", raw_steps=steps)


# ──────────────────────── D25 ⑱ ${step:x.output.y} 引用 ────────────────────────


def test_M4_PLAN_022_reference_without_dependency_rejected() -> None:
    """⑱：input 引用 ${step:s1.output.directory}，s1 在 plan 里但未声明 depends_on → 拒绝。"""
    steps = [
        _step("s1"),
        _step("s2", task_type="file.organize",
              input={"directory": "${step:s1.output.directory}"}),  # 故意不声明 depends_on
    ]
    with pytest.raises(PlanValidationError, match="without declaring"):
        validate_plan_definition(objective="x", raw_steps=steps)


def test_M4_PLAN_023_reference_with_dependency_accepted() -> None:
    """⑱对照：引用且声明在 depends_on → 通过。"""
    steps = [
        _step("s1"),
        _step("s2", task_type="file.organize", depends_on=("s1",),
              input={"directory": "${step:s1.output.directory}"}),
    ]
    defs = validate_plan_definition(objective="x", raw_steps=steps)
    assert len(defs) == 2


def test_M4_PLAN_024_reference_to_unknown_step_rejected() -> None:
    """⑱：引用不存在的 step → 拒绝。"""
    with pytest.raises(PlanValidationError, match="unknown step"):
        validate_plan_definition(
            objective="x",
            raw_steps=[_step("s1", depends_on=("ghost",),
                             input={"directory": "${step:ghost.output.directory}"})],
        )


# ──────────────────────── D26 failure_policy 三档 ────────────────────────


def test_M4_PLAN_025_all_failure_policies_accepted() -> None:
    """D26：stop/retry/skip 都合法。"""
    for policy in FAILURE_POLICIES:
        defs = validate_plan_definition(
            objective="x", raw_steps=[_step(failure_policy=policy)]
        )
        assert defs[0].failure_policy == policy


def test_M4_PLAN_026_invalid_failure_policy_rejected() -> None:
    """D26：非法 failure_policy → 拒绝。"""
    with pytest.raises(PlanValidationError, match="unknown failure_policy"):
        validate_plan_definition(objective="x", raw_steps=[_step(failure_policy="explode")])


def test_M4_PLAN_027_default_failure_policy_is_stop() -> None:
    """D26：DEFAULT_FAILURE_POLICY == 'stop'（缺省时用）。"""
    assert DEFAULT_FAILURE_POLICY == "stop"
    defs = validate_plan_definition(
        objective="x",
        raw_steps=[{"step_id": "s1", "task_type": "file.list", "objective": "x",
                    "input": {"directory": "D:\\A"}}],  # 不给 failure_policy
    )
    assert defs[0].failure_policy == "stop"
