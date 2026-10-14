"""低风险持久动作的独立一致性复核。

【这个文件守五条】

1. **触发面是 `persistent` + `risk="low"`，不多不少。** 高风险那一档由用户点确认，
   `read` / `transient` 改错了重读即可 —— 复核跑到这两档上会变成无谓的拒绝。
2. **复核独立于被复核的工具。** 判据只在 `consistency_review.REQUIREMENTS` 这张表里，
   工具代码里一行复核逻辑都不该有。这条靠"表里加一条、工具代码不改也能生效"来验。
3. **表里没登记的持久动作放行（fail-open）**，但**覆盖面必须有守卫** ——
   否则新加一个低风险持久工具会静默漏过复核。守卫是最后那条快照用例：
   现存低风险持久工具必须逐个在判据表或豁免表里有名字。
4. **"不需要判据"要写下来。** 判据表 fail-open，于是"忘了加"和"决定不加"在代码里
   长得一样。`REVIEW_WAIVER` 是后者 —— 它让以后回头看的人能分清是遗漏还是决定。
5. **拒绝用稳定码，不是原文。** 下游按码分支，措辞改了不能让它失效。

【为什么复核要放在权限判定之后】

它不是权限检查。用户点了确认（risk=high）意味着"我知道要发生什么"；
但低风险那一档没人点确认，于是"模型有没有把要干什么说清楚"没人看。
复核补的就是这一格，判的是**描述完整度**，不是"准不准做"。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.actions import ActionGateway
from agent_core.consistency_review import (
    ERROR_MISSING_FIELD,
    ERROR_RELATIVE_PATH,
    REQUIREMENTS,
    REVIEW_WAIVER,
    ConsistencyRequirement,
    ConsistencyWaiver,
    review_persistent_action,
)
from agent_core.contracts import (
    EFFECT_PERSISTENT,
    EFFECT_READ,
    EFFECT_TRANSIENT,
    ActionRequest,
    Scope,
    ToolSpec,
)
from agent_core.tools import DefaultRiskPolicy, ToolRegistry

#: 复核表里提前登记的工具名。它属阶段 2，还不在生产注册表里 ——
#: 这里用**逐字同名**的假工具验闸门，所以这个名字写错就等于闸门没被验到。
CODE_AGENT = "code__agent"


def _spec(
    name: str,
    *,
    risk: str = "low",
    effect: str = EFFECT_PERSISTENT,
    handler: Any = None,
) -> ToolSpec:
    """造一个最小工具。

    不传"要不要确认"：`ToolSpec.requires_confirmation` 是派生属性（`risk == "high"`），
    不是字段 —— 传它会直接 TypeError。想造要确认的工具就写 `risk="high"`。
    """

    async def default_handler(arguments, scope):
        return {"status": "ok", "output": {"ran": True}}

    return ToolSpec(
        name=name,
        schema={
            "type": "function",
            "function": {"name": name, "parameters": {"type": "object", "properties": {}}},
        },
        handler=handler or default_handler,
        risk=risk,
        effect=effect,
    )


def _request(spec_name: str, arguments: dict[str, Any] | None = None) -> ActionRequest:
    return ActionRequest(
        action_id="act-1",
        name=spec_name,
        arguments=dict(arguments or {}),
        scope=Scope("u1", "web", "c1", "companion"),
        origin="chat_model",
    )


def _gateway(*specs: ToolSpec) -> ActionGateway:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return ActionGateway(
        registry=registry,
        risk_policy=DefaultRiskPolicy(),
        journal=None,
        approval_sink=None,
    )


class _AlwaysConfirmPolicy:
    """把任何动作都判成"要用户点头"。

    用来**人工造出"低风险 + 需要确认"这个组合** —— 生产上的
    `DefaultRiskPolicy` 只对 `risk="high"` 返 confirm，所以这个组合在默认策略下
    不会出现；但账号感知策略、以后的自定义策略都可能造出来，恢复口上的复核
    就是为那一天准备的。
    """

    def authorize(self, spec: ToolSpec, scope: Scope, arguments: dict) -> str:
        return "confirm"


# ──────────────────── 判据表本身的形状 ────────────────────


def test_requirement_table_is_namespaced_and_unique() -> None:
    """表里的名字必须带模块前缀且不重复。

    裸名字（没有 `module__`）在注册表里 resolve 不到 —— 那条判据会**静默不生效**，
    表现成"复核加了但没拦住"。重复的名字则意味着有一条永远不会被读到。
    两种情况都不会报错，所以只能在构造期和这里挡住。
    """
    seen: set[str] = set()
    for item in REQUIREMENTS:
        assert "__" in item.tool, f"{item.tool!r} 没带模块前缀，这条判据会静默失效"
        assert item.tool not in seen, f"{item.tool!r} 在判据表里出现了两次"
        seen.add(item.tool)
        assert item.required_arguments or item.absolute_path_arguments, (
            f"{item.tool!r} 一条判据都没有，那它不该出现在这张表里"
        )
        assert item.reason.strip(), f"{item.tool!r} 没写理由"


def test_requirement_rejects_a_bare_tool_name() -> None:
    """构造期就该炸，而不是等到运行期发现没生效。"""
    with pytest.raises(ValueError, match="模块前缀"):
        ConsistencyRequirement(tool="agent", required_arguments=("objective",), reason="x")


# ──────────────────── 触发面：只在 persistent + low 这一格 ────────────────────


async def test_code_agent_without_workspace_root_is_rejected_before_execution() -> None:
    """缺 `workspace_root` 的提交被拒，且**handler 一次都没跑**。

    "被拒"和"跑完才发现不对"是两件事：持久动作跑出去就留下东西了，
    只能在动手之前挡。
    """
    calls: list[dict] = []

    async def counting(arguments, scope):
        calls.append(dict(arguments))
        return {"status": "ok", "output": {"ran": True}}

    gateway = _gateway(_spec(CODE_AGENT, handler=counting))
    observation = await gateway.invoke(
        _request(CODE_AGENT, {"objective": "把日志里的报错归类"})
    )

    assert observation.status == "rejected"
    assert observation.error_kind == "permanent"
    assert observation.error == f"{ERROR_MISSING_FIELD}: workspace_root"
    assert calls == [], "复核必须在执行之前，handler 不该被跑到"


async def test_code_agent_with_blank_objective_is_rejected() -> None:
    """`objective` 只有空白等于没写 —— 字段"在"不等于说得清。"""
    gateway = _gateway(_spec(CODE_AGENT))
    observation = await gateway.invoke(
        _request(CODE_AGENT, {"workspace_root": "D:/work/repo", "objective": "   "})
    )

    assert observation.status == "rejected"
    assert observation.error == f"{ERROR_MISSING_FIELD}: objective"


async def test_code_agent_with_relative_workspace_root_is_rejected() -> None:
    """相对路径不算"明确" —— 它落成哪个目录取决于进程当时在哪。"""
    gateway = _gateway(_spec(CODE_AGENT))
    observation = await gateway.invoke(
        _request(CODE_AGENT, {"workspace_root": "./repo", "objective": "改一下 README"})
    )

    assert observation.status == "rejected"
    assert observation.error == f"{ERROR_RELATIVE_PATH}: workspace_root"


async def test_code_agent_with_declared_arguments_runs() -> None:
    """两个参数都齐、且根目录是绝对路径 → 放行，handler 真的跑。"""
    gateway = _gateway(_spec(CODE_AGENT))
    observation = await gateway.invoke(
        _request(
            CODE_AGENT,
            {"workspace_root": "D:/work/repo", "objective": "把 README 的安装段补全"},
        )
    )

    assert observation.status == "completed"
    assert observation.output == {"ran": True}


async def test_high_risk_persistent_tool_is_not_reviewed() -> None:
    """高风险那一档由用户点确认，复核不该再插一脚。"""
    spec = _spec(CODE_AGENT, risk="high")
    assert spec.requires_confirmation is True, "前提：risk=high 的工具本来就要用户点确认"
    assert review_persistent_action(_request(CODE_AGENT, {}), spec) == ""


@pytest.mark.parametrize("effect", [EFFECT_READ, EFFECT_TRANSIENT])
def test_read_and_transient_tools_are_not_reviewed(effect: str) -> None:
    """只读与瞬态改错了重读一次就有答案，不需要复核。

    用 `code__agent` 这个已经登记了判据的名字来验：**名字在表里也不代表会复核**，
    触发面是效果与风险两件事一起决定。
    """
    spec = _spec(CODE_AGENT, effect=effect)
    assert review_persistent_action(_request(CODE_AGENT, {}), spec) == ""


def test_unregistered_persistent_low_risk_tool_passes() -> None:
    """表里没登记的低风险持久工具放行。

    这是**刻意的 fail-open**：复核是新增的一层，把"没登记"当成拒绝
    会把现存那一批持久工具当场堵死 —— 那是生产事故，不是安全加固。
    覆盖面靠下面那条快照用例守，不靠这里拒绝。
    """
    spec = _spec("demo__persist")
    assert review_persistent_action(_request("demo__persist", {}), spec) == ""


def test_adding_a_requirement_does_not_touch_tool_code() -> None:
    """判据表是唯一政策点：加一条就生效，被复核的工具代码一行都不用改。

    做法是拿一个**故意没登记**的工具名，先确认它被放行，再临时把一条判据塞进表里，
    确认同一次调用会被拒。这验的是"复核真的读表"，而不是读到了别处写死的名单。
    """
    spec = _spec("demo__persist")
    request = _request("demo__persist", {"objective": "x"})
    assert review_persistent_action(request, spec) == ""

    extra = ConsistencyRequirement(
        tool="demo__persist",
        required_arguments=("workspace_root",),
        reason="临时判据，只在本用例里存在。",
    )
    import agent_core.consistency_review as module

    original = module.REQUIREMENTS
    module.REQUIREMENTS = (*original, extra)
    try:
        assert review_persistent_action(request, spec) == f"{ERROR_MISSING_FIELD}: workspace_root"
    finally:
        module.REQUIREMENTS = original

    assert review_persistent_action(request, spec) == "", "用例结束必须把表还原"


# ──────────────────── 恢复口也要装这一道 ────────────────────


async def test_resume_also_runs_the_consistency_review() -> None:
    """审批恢复也是一条落到执行的路径，复核在它上面同样要跑。

    现在复核只覆盖 `risk="low"`，而能走到"等审批"的动作都是 `risk="high"`，
    所以这条路径上复核**当前是空转**。空转不等于可以没有：恢复口上不装一道，
    将来出现"低风险 + 需要确认"的组合时就会安静地绕过复核 ——
    那正是这次要根治的形态。这里用一个"全都判要确认"的策略把那个组合造出来。
    """
    registry = ToolRegistry()
    registry.register(_spec(CODE_AGENT))
    gateway = ActionGateway(
        registry=registry,
        risk_policy=_AlwaysConfirmPolicy(),
        journal=None,
        approval_sink=lambda request, spec: "appr-1",
    )
    arguments = {"objective": "把日志里的报错归类"}

    suspended = await gateway.invoke(_request(CODE_AGENT, arguments))
    assert suspended.status == "waiting_approval", "前提：这一步必须先被挂起等审批"

    resumed = await gateway.resume(
        "appr-1", "confirmed", request=_request(CODE_AGENT, arguments)
    )

    assert resumed.status == "rejected", "用户点头了也不等于说清了要干什么"
    assert resumed.error == f"{ERROR_MISSING_FIELD}: workspace_root"


# ──────────────────── 豁免表：登记"看过、决定不加判据" ────────────────────


def test_waiver_table_names_are_namespaced_and_reasoned() -> None:
    """豁免表跟判据表一样要有形状要求：名字能 resolve 到、理由说得清。

    理由是给人看的：以后回头看的人只有一个问题 ——「这个工具是漏了，还是决定不加？」
    填个"不需要"回答不了这个问题，所以构造期就挡住空理由。
    """
    seen: set[str] = set()
    for item in REVIEW_WAIVER:
        assert "__" in item.tool, f"{item.tool!r} 没带模块前缀"
        assert item.tool not in seen, f"{item.tool!r} 在豁免表里出现了两次"
        seen.add(item.tool)
        assert item.reason.strip(), f"{item.tool!r} 没写理由"


def test_waiver_rejects_a_bare_tool_name() -> None:
    with pytest.raises(ValueError, match="模块前缀"):
        ConsistencyWaiver(tool="agent", reason="随便写点")


def test_waiver_rejects_a_blank_reason() -> None:
    with pytest.raises(ValueError, match="理由"):
        ConsistencyWaiver(tool="demo__tool", reason="   ")


def test_a_tool_is_either_reviewed_or_waived_never_both() -> None:
    """两张表互斥。同时出现两条命令式的说明，读到哪条取决于代码顺序 —— 那是最坏的形态。"""
    reviewed = {item.tool for item in REQUIREMENTS}
    waived = {item.tool for item in REVIEW_WAIVER}
    assert not (reviewed & waived), f"既登记了判据又登记了豁免：{sorted(reviewed & waived)}"


def test_the_waived_tools_are_exactly_the_four_reviewed_decisions() -> None:
    """豁免名单是**决定**，不是垃圾桶：这四条是逐个看过、并给出不该加判据的理由的。

    名单变了（少一条）就说明有人把这个决定撤销了 —— 那要么补上判据，
    要么把理由改对。加一条同样要在这里停一下，确认不是顺手塞进来的。
    """
    assert {item.tool for item in REVIEW_WAIVER} == {
        "media__submit_tts",
        "netease__login",
        "subagent__dispatch",
        "work__submit",
    }


# ──────────────────── 覆盖面守卫 ────────────────────


def test_every_low_risk_persistent_tool_has_an_explicit_decision() -> None:
    """生产注册表里"低风险 + 持久"的工具，每一个都要在两张表里有名字。

    **这条用例红了不代表代码坏了** —— 它代表有人新加了一个低风险持久工具
    （或者改了某个工具的 effect / risk）。那种改动会让这个新工具**静默漏过复核**，
    所以要在这里停一下，明确决定：给它加一条判据，还是确认它确实不需要复核、
    然后把它写进 `REVIEW_WAIVER` 并说清理由。

    "不用确认"不单独判：`ToolSpec.requires_confirmation` 就是 `risk == "high"`，
    写进条件里只是个永真的冗余项。真正决定复核触发面的是 risk 与 effect 两个字段。

    反向也要判：表里登记了、注册表里却没有的名字（工具删了、改过名了）会让那条
    判据**永远不会被读到**，而它不会报错。唯一允许这么存在的是提前登记的契约
    （阶段 2 的工具，上线即受管）。

    别为了让用例变绿直接照抄实际值 —— 那是把守卫关掉。
    """
    from agent_core.capability_runtime import build_runtime

    runtime = build_runtime()
    live = {
        spec.name
        for spec in runtime.registry
        if spec.effect == EFFECT_PERSISTENT and spec.risk == "low"
    }

    declared = {item.tool for item in REQUIREMENTS} | {
        item.tool for item in REVIEW_WAIVER
    }

    assert live <= declared, (
        "低风险持久工具集合变了，多出来的这些现在没有复核覆盖，"
        f"要么加判据、要么写进豁免表。多出来的是：{sorted(live - declared)}"
    )
    assert declared - live == {CODE_AGENT}, (
        "表里登记了但注册表里没有的名字 —— 那条判据永远不会被读到。"
        f"只允许提前登记的契约在这一档。多出来的是：{sorted(declared - live - {CODE_AGENT})}"
    )
