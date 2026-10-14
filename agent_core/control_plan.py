"""Control Plan contracts: a durable, Host-validated DAG of work steps.

A ``ControlPlan`` is the user-visible objective. It owns a fixed set of
``PlanStep`` records that each map onto exactly one Host ``WorkItem``. The Host
persists the whole plan before creating any WorkItem, so a restart always sees a
complete, self-consistent plan.

The module deliberately holds no scheduling state: it only defines the contracts,
the submission-time validation rules (unknown task type, missing/unknown
dependency, cycle, forged approval fields, cross-scope input, Host-owned fields,
``AGENT_WORK_ROOTS`` tampering) and the ``${step:<id>.output.<path>}`` reference
resolver used by the Host right before a step becomes a WorkItem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .contracts import Scope
from .work_provider import WORK_TASK_TYPES

PLAN_STATUSES = frozenset(
    {"queued", "running", "waiting_approval", "completed", "failed", "cancelled", "interrupted"}
)
PLAN_IN_FLIGHT_STATUSES = frozenset({"queued", "running", "waiting_approval"})
PLAN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})

STEP_STATUSES = frozenset(
    {
        "pending",
        "ready",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "blocked",
        "cancelled",
        "interrupted",
    }
)
STEP_ACTIVE_STATUSES = frozenset({"ready", "running", "waiting_approval"})
STEP_TERMINAL_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled", "interrupted"})

FAILURE_POLICIES = frozenset({"stop", "retry", "skip"})
DEFAULT_FAILURE_POLICY = "stop"

# Cross-task orchestration events. ``plan.*`` describes the plan, ``step.*`` the step.
# ``step.retrying`` / ``step.cancelled`` / ``step.interrupted`` are Host extensions of
# the required vocabulary: they keep retry, rejection and restart observable without
# overloading ``step.failed``.
PLAN_EVENT_TYPES = frozenset(
    {
        "plan.created",
        "plan.started",
        "step.ready",
        "step.started",
        "step.waiting_approval",
        "step.completed",
        "step.failed",
        "step.blocked",
        "plan.completed",
        "plan.failed",
        "plan.cancelled",
        "plan.interrupted",
        "step.retrying",
        "step.cancelled",
        "step.interrupted",
    }
)

# Approval is a Host boundary: only steps that can change local state may demand it.
APPROVAL_ELIGIBLE_TASK_TYPES = frozenset({"file.organize", "screen.capture", "system.volume.adjust"})

# 这些 task_type **一律**要人工确认，计划自己说 requires_approval=false 也不算数。
# 候选计划可能是 LLM 生成的，"我要不要被审批"不能由候选者自己投票决定 ——
# 否则一条 system.volume.adjust 步骤只要不写 requires_approval 就能静默改音量。
MANDATORY_APPROVAL_TASK_TYPES = frozenset({"screen.capture", "system.volume.adjust"})

# Fields the Host owns. A plan (and therefore the LLM planner behind it) may never set them.
HOST_OWNED_INPUT_KEYS = frozenset({"approved_moves", "authorization", "authorized", "approval_granted"})
# A step cannot widen its own identity or the sandbox it runs inside.
SCOPE_INPUT_KEYS = frozenset(
    {"owner_id", "channel", "conversation_id", "module_id", "scope", "allowed_roots", "work_roots"}
)
FORBIDDEN_INPUT_TOKEN = "AGENT_WORK_ROOTS"

MAX_PLAN_STEPS = 32
MAX_STEP_ATTEMPTS = 3

_STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_REFERENCE_RE = re.compile(
    r"\$\{step:(?P<step>[A-Za-z0-9][A-Za-z0-9_.-]*)\.output(?P<path>(?:\.[A-Za-z0-9_-]+|\[\d+\])*)\}"
)
_REFERENCE_ONLY_RE = re.compile(
    r"^\$\{step:(?P<step>[A-Za-z0-9][A-Za-z0-9_.-]*)\.output(?P<path>(?:\.[A-Za-z0-9_-]+|\[\d+\])*)\}$"
)
_PATH_TOKEN_RE = re.compile(r"\.[A-Za-z0-9_-]+|\[\d+\]")


class PlanValidationError(ValueError):
    """Raised when a candidate plan violates a Host-owned plan boundary."""


@dataclass(frozen=True)
class ControlPlan:
    plan_id: str
    scope: Scope
    objective: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    plan_id: str
    task_type: str
    objective: str
    input: dict[str, Any]
    depends_on: tuple[str, ...]
    requires_approval: bool
    failure_policy: str
    status: str
    work_item_id: str | None = None
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class PlanStepDefinition:
    """A validated, not-yet-persisted step definition."""

    step_id: str
    task_type: str
    objective: str
    input: dict[str, Any]
    depends_on: tuple[str, ...]
    requires_approval: bool
    failure_policy: str


@dataclass(frozen=True)
class PlanEvent:
    event_id: str
    plan_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any]
    created_at: str
    step_id: str | None = None
    work_item_id: str | None = None
    attempt_id: str | None = None


# --------------------------------------------------------------------------- #
# Submission-time validation
# --------------------------------------------------------------------------- #


def _scan_forbidden(value: Any, *, path: str, problems: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if key_text in HOST_OWNED_INPUT_KEYS:
                problems.append(f"{path}.{key_text} is Host-owned and cannot be supplied by a plan")
            elif key_text in SCOPE_INPUT_KEYS:
                problems.append(f"{path}.{key_text} cannot override Host scope inside a plan step")
            elif FORBIDDEN_INPUT_TOKEN in key_text.upper():
                problems.append(f"{path}.{key_text} cannot modify {FORBIDDEN_INPUT_TOKEN}")
            _scan_forbidden(item, path=f"{path}.{key_text}", problems=problems)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan_forbidden(item, path=f"{path}[{index}]", problems=problems)
    elif isinstance(value, str):
        if FORBIDDEN_INPUT_TOKEN in value.upper():
            problems.append(f"{path} cannot modify {FORBIDDEN_INPUT_TOKEN}")


def _topological_order(dependencies: Mapping[str, tuple[str, ...]]) -> list[str]:
    """Return a deterministic topological order or raise on a cycle."""
    state: dict[str, int] = {}
    order: list[str] = []

    def visit(node: str, trail: list[str]) -> None:
        marker = state.get(node, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = " -> ".join([*trail, node])
            raise PlanValidationError(f"plan contains a dependency cycle: {cycle}")
        state[node] = 1
        for parent in dependencies.get(node, ()):  # parents terminate, node depends on them
            visit(parent, [*trail, node])
        state[node] = 2
        order.append(node)

    for node in sorted(dependencies):
        visit(node, [])
    return order


def dependency_closure(dependencies: Mapping[str, Sequence[str]]) -> dict[str, frozenset[str]]:
    """Map each step to the transitive set of steps it (indirectly) depends on."""
    closure: dict[str, frozenset[str]] = {}

    def resolve(node: str, trail: tuple[str, ...] = ()) -> frozenset[str]:
        if node in closure:
            return closure[node]
        if node in trail:
            raise PlanValidationError(f"plan contains a dependency cycle at step {node}")
        parents = tuple(dependencies.get(node, ()))
        found: set[str] = set(parents)
        for parent in parents:
            found |= resolve(parent, (*trail, node))
        closure[node] = frozenset(found)
        return closure[node]

    for node in dependencies:
        resolve(node)
    return closure


def iter_references(value: Any) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield ``(step_id, path)`` for every ``${step:x.output.y}`` reference found."""
    if isinstance(value, str):
        for match in _REFERENCE_RE.finditer(value):
            yield match.group("step"), _split_path(match.group("path") or "")
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from iter_references(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_references(item)


def _split_path(path: str) -> tuple[str, ...]:
    return tuple(token.lstrip(".") if token.startswith(".") else token for token in _PATH_TOKEN_RE.findall(path or ""))


def _lookup(output: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = output
    for token in path:
        if token.startswith("[") and token.endswith("]"):
            index = int(token[1:-1])
            try:
                current = current[index]
            except (IndexError, TypeError, KeyError) as exc:
                raise PlanValidationError(f"step output index not found: {token}") from exc
            continue
        if not isinstance(current, Mapping) or token not in current:
            raise PlanValidationError(f"step output field not found: {token}")
        current = current[token]
    return current


def resolve_references(value: Any, outputs: Mapping[str, Mapping[str, Any]]) -> Any:
    """Resolve ``${step:<id>.output.<path>}`` placeholders against predecessor outputs.

    A value that is exactly one reference keeps the referenced type (list, dict,
    number). Otherwise the resolved value is stringified inside the surrounding
    text. The Provider never sees a template: the Host resolves first.
    """
    if isinstance(value, str):
        whole = _REFERENCE_ONLY_RE.match(value)
        if whole:
            return _lookup(outputs.get(whole.group("step"), {}), _split_path(whole.group("path") or ""))
        return _REFERENCE_RE.sub(
            lambda match: str(_lookup(outputs.get(match.group("step"), {}), _split_path(match.group("path") or ""))),
            value,
        )
    if isinstance(value, Mapping):
        return {key: resolve_references(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item, outputs) for item in value]
    if isinstance(value, tuple):
        return tuple(resolve_references(item, outputs) for item in value)
    return value


def validate_plan_definition(
    objective: str,
    raw_steps: Iterable[Mapping[str, Any]],
    *,
    provider_task_types: Sequence[str] | None = None,
    provider_id: str = "",
) -> tuple[PlanStepDefinition, ...]:
    """Validate a candidate plan and return its normalized steps.

    The Host calls this before persisting anything: a plan the Host has not
    validated is never written to the ledger and never executed.
    """
    if not str(objective or "").strip():
        raise PlanValidationError("plan objective is required")

    raw_list = list(raw_steps or ())
    if not raw_list:
        raise PlanValidationError("a plan requires at least one step")
    if len(raw_list) > MAX_PLAN_STEPS:
        raise PlanValidationError(f"a plan supports at most {MAX_PLAN_STEPS} steps")

    supported = set(provider_task_types or WORK_TASK_TYPES)
    definitions: list[PlanStepDefinition] = []
    seen: set[str] = set()
    problems: list[str] = []

    for index, raw in enumerate(raw_list):
        if not isinstance(raw, Mapping):
            raise PlanValidationError(f"step #{index} must be an object")
        step_id = str(raw.get("step_id") or "").strip()
        if not _STEP_ID_RE.match(step_id):
            raise PlanValidationError(f"step #{index} has an invalid step_id")
        if step_id in seen:
            raise PlanValidationError(f"duplicate step_id: {step_id}")
        seen.add(step_id)

        task_type = str(raw.get("task_type") or "").strip()
        if task_type not in WORK_TASK_TYPES:
            raise PlanValidationError(f"unknown task_type: {task_type or '(missing)'}")
        if task_type not in supported:
            raise PlanValidationError(
                f"provider {provider_id or 'unknown'} does not support task_type: {task_type}"
            )

        step_objective = str(raw.get("objective") or "").strip()
        if not step_objective:
            raise PlanValidationError(f"step {step_id} requires an objective")

        step_input = raw.get("input") or {}
        if not isinstance(step_input, Mapping):
            raise PlanValidationError(f"step {step_id} input must be an object")
        step_input = dict(step_input)
        _scan_forbidden(step_input, path=f"step {step_id} input", problems=problems)

        depends_on = tuple(str(item) for item in (raw.get("depends_on") or ()))
        if len(set(depends_on)) != len(depends_on):
            raise PlanValidationError(f"step {step_id} lists a dependency twice")
        if step_id in depends_on:
            raise PlanValidationError(f"step {step_id} cannot depend on itself")

        requires_approval = bool(raw.get("requires_approval", False))
        if requires_approval and task_type not in APPROVAL_ELIGIBLE_TASK_TYPES:
            raise PlanValidationError(f"task_type {task_type} cannot require approval")
        # 该类的步骤由校验器**强制**打上确认标记：计划没写、甚至写了 false，
        # 都按"要确认"处理。审批是 Host 的边界，不是候选计划的选项。
        if task_type in MANDATORY_APPROVAL_TASK_TYPES:
            requires_approval = True

        failure_policy = str(raw.get("failure_policy") or DEFAULT_FAILURE_POLICY)
        if failure_policy not in FAILURE_POLICIES:
            raise PlanValidationError(f"unknown failure_policy: {failure_policy}")

        definitions.append(
            PlanStepDefinition(
                step_id=step_id,
                task_type=task_type,
                objective=step_objective,
                input=step_input,
                depends_on=depends_on,
                requires_approval=requires_approval,
                failure_policy=failure_policy,
            )
        )

    if problems:
        raise PlanValidationError("; ".join(dict.fromkeys(problems)))

    known = {definition.step_id for definition in definitions}
    for definition in definitions:
        for parent in definition.depends_on:
            if parent not in known:
                raise PlanValidationError(f"step {definition.step_id} depends on unknown step: {parent}")

    dependencies = {definition.step_id: definition.depends_on for definition in definitions}
    _topological_order(dependencies)
    closure = dependency_closure(dependencies)

    for definition in definitions:
        allowed = closure[definition.step_id]
        for referenced, _path in iter_references(definition.input):
            if referenced not in known:
                raise PlanValidationError(f"step {definition.step_id} references unknown step: {referenced}")
            if referenced not in allowed:
                raise PlanValidationError(
                    f"step {definition.step_id} references {referenced} without declaring it in depends_on"
                )

    return tuple(definitions)


def ready_step_ids(steps: Sequence[PlanStep], dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    """Return the ids of ``pending`` steps whose dependencies have all completed."""
    statuses = {step.step_id: step.status for step in steps}
    ready: list[str] = []
    for step in steps:
        if step.status != "pending":
            continue
        if all(statuses.get(parent) == "completed" for parent in dependencies.get(step.step_id, ())):
            ready.append(step.step_id)
    return ready


def step_outputs(steps: Sequence[PlanStep]) -> dict[str, dict[str, Any]]:
    """Collect completed step outputs for reference resolution."""
    return {step.step_id: dict(step.output or {}) for step in steps if step.status == "completed"}


def blockers(dependencies: Mapping[str, Sequence[str]], failed: str) -> set[str]:
    """Every step that transitively depends on ``failed``."""
    closure = dependency_closure(dependencies)
    return {node for node, parents in closure.items() if failed in parents}
