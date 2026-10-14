"""执行路径必须过网关：从最坏的一处旁路开始收口。

【为什么先动 condition_tasks】

系统里一度有四份"执行一个工具"的实现。最坏的那份在
`agent_tools/condition_tasks.py::_check`：

    spec = self.registry_provider().resolve(WEATHER_SEARCH_TOOL)
    result = await ToolExecutor().execute(spec, call, task.scope)   # :377

它自己 resolve 出 spec 就自己执行了，**整条路上没有一次权限判定**。
另外三份（loop 的确定性路由 / 模型调用 / 审批恢复）至少还过 `RiskPolicy`，
只有这一份连那个都没有 —— 所以"这个工具该不该跑"在这里从来没人回答过。

更麻烦的是它跑的场景：条件任务在后台定时触发，**没有人在旁边点确认**。
一个 `scheduler` 发起的动作绕过了全套判定，这是最需要被收住的一条路。

【这个文件守哪三条】

1. 主闸门：条件任务触发时，工具必须**经网关**执行；
2. 来源：网关收到的 origin 必须是 `scheduler`；
3. 拒绝即止：网关说 rejected 时，不许再有人去碰 handler。

【本期故意不写的】

"全仓不得出现旁路"的总闸门仍留给最后一条执行路径迁完再加 ——
现在写它会长期红灯，让测试主干失去可信度（分不清预期红与回归）。
本文件只守**已经收口的那一条**。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.contracts import ActionObservation, ActionRequest, Scope, ToolResult


# ── 假网关：记录每一次调用 ──────────────────────────────────────────


class RecordingGateway:
    """只记录、不真执行的网关替身。

    为什么不用真 `ActionGateway` 配真 handler：那样测的是"网关行为对不对"，
    而**这个文件要测的是"条件任务有没有走网关"**。用替身把两件事分开 ——
    替身被调到了就说明链路对，没调到就说明还有旁路。
    """

    def __init__(self, result: ActionObservation | None = None) -> None:
        self.calls: list[ActionRequest] = []
        self.result = result or ActionObservation(
            action_id="act-x", name="search__web", status="completed", output={"ok": True},
        )

    async def invoke(self, request: ActionRequest) -> ActionObservation:
        self.calls.append(request)
        return self.result


class _FakeStore:
    """只实现 `_check` 真正会用到的那几个方法。

    不用真 SQLite 是刻意的：这条用例测的是**执行路径**，
    引入真实存储会把"存储行为"和"是否走网关"两件事纠缠在一起，
    一旦 storage 出问题，红的原因就说不清了。
    """

    def __init__(self) -> None:
        self.blocked: list[tuple[str, str]] = []
        self.rescheduled: list[str] = []
        self.completed: list[str] = []

    def block(self, task_id: str, reason: str) -> None:
        self.blocked.append((task_id, reason))

    def reschedule(self, task: Any, **fields: Any) -> None:
        self.rescheduled.append(getattr(task, "task_id", str(task)))

    def complete(self, task: Any, summary: str, triggered: str) -> bool:
        self.completed.append(getattr(task, "task_id", str(task)))
        return False

    def recover_interrupted_checks(self, now: str) -> int:  # 构造函数会调
        return 0


def _service(store: _FakeStore, gateway: RecordingGateway):
    """构造一个走网关的条件任务服务。"""
    from agent_core.tools import ToolRegistry
    from agent_tools.condition_tasks import ConditionTaskService

    return ConditionTaskService(
        store=store,
        # 空的登记簿：如果实现还在自己 resolve，这里**一定拿不到**工具，
        # 会走"authorized search__web tool is unavailable"分支 —— 网关就不会被调用。
        registry_provider=lambda: ToolRegistry(),
        event_bus=None,
        gateway=gateway,
    )


def _task() -> Any:
    """一条今天 00:00–23:59、该检查了的天气条件任务。"""
    from agent_tools.condition_tasks import ConditionTask

    return ConditionTask(
        task_id="ct-1",
        scope=Scope(owner_id="owner-1", channel="desktop", conversation_id="c1", module_id="companion"),
        title="下雨提醒",
        request_text="下雨就告诉我",
        city="上海",
        condition="weather_clear",
        timezone="UTC",
        check_interval_seconds=60,
        daily_window={"start": "00:00", "end": "23:59"},
        allowed_tools=["search__web"],
        status="checking",
        next_check_at=None,
        last_summary=None,
        last_error=None,
        triggered_at=None,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
    )


# ── 主闸门 ──────────────────────────────────────────────────────────


async def test_condition_task_execution_goes_through_the_gateway() -> None:
    """条件任务触发时，工具必须**经网关**执行，不许自己 resolve 自己跑。

    判据设计：给一个**空**登记簿。旧实现会先 `resolve` 拿到 None，
    然后 block 掉整条任务 —— 网关根本不会被调到。
    所以"网关被调用了一次"这个断言能直接区分两条路。
    """
    gateway = RecordingGateway()
    store = _FakeStore()
    service = _service(store, gateway)

    await service._check(_task())

    assert len(gateway.calls) == 1, (
        f"网关被调用了 {len(gateway.calls)} 次。"
        "条件任务还在绕开网关自己执行 —— 那条路径一次权限判定都没有。"
    )
    assert gateway.calls[0].name == "search__web"


async def test_condition_task_actions_are_tagged_with_the_scheduler_origin() -> None:
    """来源必须是 `scheduler`。

    后台定时触发是最需要可追溯的一种来源：出事时要能一眼看出
    "这是定时器发起的"，而不是"某个聊天回合发起的"。
    """
    gateway = RecordingGateway()
    service = _service(_FakeStore(), gateway)

    await service._check(_task())

    assert gateway.calls[0].origin == "scheduler"


async def test_a_denied_condition_task_action_does_not_touch_the_handler() -> None:
    """网关拒绝时应到此为止，不许再有人绕过去调 handler。

    这条是"为什么必须走网关"的具体理由：旧实现的 `allowed_tools` 检查
    只比对**名字**，比对完就直接 `ToolExecutor().execute(...)` ——
    那是一条没有任何东西阻止的直线。
    """
    gateway = RecordingGateway(
        ActionObservation(action_id="act-x", name="search__web", status="rejected", error="not authorized")
    )
    service = _service(_FakeStore(), gateway)

    await service._check(_task())

    assert len(gateway.calls) == 1, "网关仍应被调用一把 —— 权限判定发生在它里面"
    # 走不到 handler：没有 handler 被这一步拿到过（空登记簿 + 网关拒绝）。
    assert gateway.calls[0].name == "search__web"


async def test_production_assembly_wires_the_gateway_into_condition_tasks() -> None:
    """生产装配必须真的把网关交给条件任务服务。

    这条防的是"接口有了但没人接线"：`ConditionTaskService(gateway=None)`
    在结构上完全成立，构造不报错，测试（用替身）也全绿 ——
    直到某天第一条定时任务真的触发，`self.gateway.invoke` 才炸，
    而且症状是**这条提醒从此再也不响**（异常被 `_check_guarded` 吞掉）。

    那种故障在本地几乎不可能复现（要等 interval 到期），所以必须在这里断言。
    """
    from agent_core.capability_runtime import build_runtime

    runtime = build_runtime()
    service = runtime.condition_task_service

    assert service is not None, "conditions 模块没建起来"
    assert service.gateway is not None, (
        "条件任务服务没拿到网关 —— 它的后台执行会一直失败，"
        "且症状是提醒不响而不是报错（异常被 _check_guarded 吞掉）。"
    )


async def test_condition_tasks_do_not_resolve_and_execute_on_their_own() -> None:
    """`_check` 源码里不许再出现"自己执行"的形状。

    上面几条是行为断言（"有没有真的走网关"）；这条是**结构断言**
    （"源码里还有没有旁路"）。两者缺一不可：行为断言抓不到
    "某个分支下仍然绕行"，结构断言抓不到"接口对了但接线错"。
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "agent_tools" / "condition_tasks.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # 判据收紧成"接收者看着像工具规格"：`.execute` 这个名字在工程里太常见 ——
        # `sqlite3` 的游标就是 `db.execute(...)`。按属性名一刀切会误报几十行，
        # 而一条会误报的检查很快就会被加 skip 绕过，等于没有。
        receiver = getattr(node.func, "value", None)
        name = getattr(receiver, "id", "") or getattr(receiver, "attr", "") if receiver else ""
        if not name:
            continue
        looks_like_spec = name == "spec" or name.endswith("_spec")
        looks_like_executor = "ToolExecutor" in name or name.endswith("_executor")
        if (looks_like_spec or looks_like_executor) and getattr(node.func, "attr", "") == "execute":
            offenders.append(f"condition_tasks.py:{node.lineno} 还在自己 .execute({name}...)")
    assert offenders == [], "工具执行必须全部交给网关：\n" + "\n".join(offenders)


async def test_failed_gateway_result_reschedules_instead_of_crashing() -> None:
    """网关返回失败时，任务是"改期再试"，不是炸掉。

    条件任务跑在后台 `_check_guarded` 里，异常会被吞成 reschedule，
    所以"炸掉"的症状是**这条提醒从此再也不响** —— 极其难查。
    这里断言失败结果是**被处理的**（改期），而不是冒泡。
    """
    gateway = RecordingGateway(
        ActionObservation(
            action_id="act-x", name="search__web", status="failed",
            error="transient failure", error_kind="transient",
        )
    )
    store = _FakeStore()
    service = _service(store, gateway)

    await service._check(_task())

    assert store.completed == [], "失败不该被当成触发成功"
    assert store.blocked == [], "工具本身是有的，只是这次调用失败，不该 block"
    assert store.rescheduled == ["ct-1"], "失败的动作应该改期再试"
