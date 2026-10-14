"""统一 work 工具工厂：模型侧**只有四个**入口。

【这一批收掉的是什么】

以前 work 对模型暴露 9 个工具：

    submit / list / get / cancel / approve
    plan_submit / plan_get / plan_list / plan_cancel

两档（单步任务、多步计划）各长出一套状态查询与取消，于是同一件事在模型面前
有两个名字（"看看任务"要 list 还是 get？"取消"要 cancel 还是 plan_cancel？），
而 `approve` 更麻烦 —— 它让**模型可以替用户批准一个高风险操作**。
审批本该由人做：`work__submit` 当时带 hard_routable，运行时可以直接提交；
可一旦模型手里有 `approve`，它就能自己把刚提交的高风险任务批掉 ——
两道闸门互相抵消。

现在收成四个：`submit` / `plan_submit` / `status` / `cancel`。
`status` 与 `cancel` 用 `kind` 分派到任务或计划，`id` 决定"查单项还是列一批"。
`approve` 整个从模型面消失，后台任务审批只能由 Web/CLI 用户走统一审批控制面
（`agent_core/approval_coordinator.py` + `/api/approvals`）。
`submit` 的硬路由授权也已摘掉（2026-09-23，阶段 0）：提交后台任务是持久动作，
只有模型能发起。

本文件钉住五件事：
  1. **精确**只有四个工具（多一个少一个都算失败）；
  2. `status` 的分派行为（kind × 有无 id × limit）；
  3. `cancel` 的分派行为与"缺 id 就失败"；
  4. 授权元数据（submit 不给硬路由、status 只读、cancel/plan_submit 高风险）；
  5. 遗留的 `work__approve` 待确认记录**不可能被执行**。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.modules import ModuleRegistry, ModuleSpec
from agent_core.tools import ToolCall, ToolExecutor
from agent_tools.work import _work_scope, make_work_tools

CHAT_SCOPE = Scope("alice", "web", "conv-1", "companion")
WORK_SCOPE = Scope("alice", "web", "conv-1", "work")

#: 模型能看到的**全部** work 入口。这是一个"精确相等"的判据，不是"包含"。
ALL_TOOL_NAMES = {"submit", "plan_submit", "status", "cancel"}


class FakeHost:
    """记录调用参数的最小 Host 替身（不碰 sqlite、不起 worker）。"""

    def __init__(self, *, raises: BaseException | None = None, work_detail: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises = raises
        self.work_detail = work_detail or {"work_item_id": "w-1", "status": "waiting_approval", "approvals": []}

    def _record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))
        if self.raises is not None:
            raise self.raises

    async def submit(self, scope: Scope, objective: str, *, task_type: str, input: dict[str, Any]) -> dict[str, Any]:
        self._record("submit", scope, objective, task_type, input)
        return {"status": "queued", "work_item_id": "w-1", "attempt_id": "a-1"}

    def list(self, scope: Scope) -> list[dict[str, Any]]:
        self._record("list", scope)
        return [{"work_item_id": "w-1", "status": "queued"}]

    def get(self, work_item_id: str, scope: Scope) -> dict[str, Any]:
        self._record("get", work_item_id, scope)
        if work_item_id == "missing":
            raise KeyError(work_item_id)
        return dict(self.work_detail)

    async def cancel(self, work_item_id: str, scope: Scope) -> bool:
        self._record("cancel", work_item_id, scope)
        if work_item_id == "missing":
            raise KeyError(work_item_id)
        return True

    async def decide_approval(self, approval_id: str, scope: Scope, decision: str) -> dict[str, Any]:
        # 这个替身**不该**被任何工具调到：模型面已经没有 approve 了。
        # 留着它是为了让"偷偷调到了"这件事在断言里看得见。
        self._record("decide_approval", approval_id, scope, decision)
        return {"status": "queued", "approval_id": approval_id}

    async def submit_plan(self, scope: Scope, objective: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        self._record("submit_plan", scope, objective, steps)
        return {"status": "queued", "plan_id": "p-1"}

    def get_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        self._record("get_plan", plan_id, scope)
        if plan_id == "missing":
            raise KeyError(plan_id)
        return {"plan_id": plan_id, "status": "queued"}

    def list_plans(self, scope: Scope, *, limit: int = 20) -> list[dict[str, Any]]:
        self._record("list_plans", scope, limit)
        return [{"plan_id": "p-1"}]

    async def cancel_plan(self, plan_id: str, scope: Scope) -> dict[str, Any]:
        self._record("cancel_plan", plan_id, scope)
        if plan_id == "missing":
            raise KeyError(plan_id)
        return {"cancelled": True}


def _tool(host: Any, local_name: str) -> ToolSpec:
    tools = {spec.name: spec for spec in make_work_tools(host)}
    assert local_name in tools, f"make_work_tools 少了 {local_name}，实际：{sorted(tools)}"
    return tools[local_name]


def _build_module(host: Any) -> ModuleRegistry:
    """把工厂塞进一个真的 work 模块，验证命名空间后的对外工具名。"""
    modules = ModuleRegistry()
    modules.register(
        ModuleSpec(
            "work",
            lambda: make_work_tools(host),
            description="后台工作计划编排",
            declared_tools=("submit", "plan_submit", "status", "cancel"),
        )
    )
    return modules


# ──────────────────────── 1. 模型面精确只有四个 ────────────────────────


def test_the_model_face_is_exactly_four_tools() -> None:
    """**精确相等**：多一个少一个都失败。

    用 `==` 而不是 `>=`：这一批的全部意义就是"数量收窄到四"，
    一个只判包含的断言对"又多长出一个工具"完全无感。
    """
    names = [spec.name for spec in make_work_tools(FakeHost())]

    assert set(names) == ALL_TOOL_NAMES
    assert len(names) == len(set(names)), "统一工厂不能产出重名工具"


def test_the_model_face_has_no_approve() -> None:
    """`approve` 从模型面消失。

    它存在时，模型可以替用户批准一个高风险操作 —— 当时 `work__submit` 带
    hard_routable，运行时能直接提交；模型手里有 approve 就能把刚提交的
    高风险任务自己批掉，两道闸门互相抵消。
    审批只能由 Web/CLI 用户走统一审批控制面。
    """
    names = {spec.name for spec in make_work_tools(FakeHost())}

    assert "approve" not in names
    assert not any("approve" in name for name in names), names


def test_make_work_plan_tools_is_gone() -> None:
    """只留一个公开构建入口 `make_work_tools()`。

    上一个"兼容子集"入口（`make_work_plan_tools`）活了下来，而它的存在让
    "谁在注册工具"这个问题重新变成两个答案。需要拆分实现时用私有辅助函数，
    不能再形成第二个公开入口。
    """
    import agent_tools.work as work

    assert not hasattr(work, "make_work_plan_tools")


def test_namespaced_schema_names_match_the_public_tool_names() -> None:
    """本地名提交给模块，模块包装成 work__* 后 model 看到的名字也是 work__*。"""
    registry = _build_module(FakeHost()).tools_for({"work"})

    for local_name in ALL_TOOL_NAMES:
        spec = registry.resolve(f"work__{local_name}")
        assert spec is not None, f"registry 缺 work__{local_name}"
        assert spec.schema["function"]["name"] == f"work__{local_name}"


def test_local_schema_names_stay_unprefixed_before_namespacing() -> None:
    for spec in make_work_tools(FakeHost()):
        assert spec.schema["function"]["name"] == spec.name


# ──────────────────────── 1b. 工具名单只有一份 ────────────────────────


def test_work_tool_names_constant_is_gone() -> None:
    """运行时代码里不许再有第二份工具名单。

    `WORK_TOOL_NAMES` 是"模块自己维护的名字清单"，能力目录里的
    `declared_tools` 是"目录维护的名字清单"，`make_work_tools()` 是真实产出 ——
    三份东西里有两份是人手写的，就一定会漂移（这个项目已经踩过一次：
    静态清单漏了 `weather`，而报"某模块不可用"的代码正好去查它 → `KeyError`）。

    收尾之后只留**一份人手写的**：能力目录里的那条声明。
    模块声明从真实产出提取，真实产出对不上目录时由
    `tests/test_capability_catalog.py` 的对账用例报错。
    """
    import agent_tools.work as work

    assert not hasattr(work, "WORK_TOOL_NAMES"), (
        "agent_tools.work 又导出了工具名单常量 —— 那是能力目录之外的第二份真相"
    )


def test_the_module_declaration_is_extracted_from_the_real_output(tmp_path: Any) -> None:
    """模块的 `declared_tools` 必须**等于** `build_tools()` 的真实产出。

    这条测的不是"名字对不对"（那由目录对账用例管），而是**声明是怎么来的**：
    手写一份名单也能碰巧对得上，但下一次改工具时它就会漂移。
    """
    from agent_core.work_module import build_work_module

    spec, _host = build_work_module(db_path=tmp_path / "work.db", allowed_roots=[tmp_path])

    assert spec.declared_tools == tuple(item.name for item in spec.build_tools())


def test_the_catalog_holds_the_only_expected_declaration() -> None:
    """唯一一份**人手写**的预期声明在能力目录里，且就是这四个。

    它允许与真实产出不一致 —— 那种不一致由对账用例报错。
    这里把它钉死，是为了让"有人以为改目录比改代码快"这件事立刻可见。
    """
    from agent_core.capability_catalog import build_catalog

    entry = build_catalog().entry("work")

    assert entry is not None
    assert entry.declared_tools == ("submit", "plan_submit", "status", "cancel")


def test_namespacing_preserves_work_authorization_metadata() -> None:
    """模块包装必须原样搬走授权字段，否则确认闸门会静默失效。

    `work__submit` 自 2026-09-23（阶段 0）起**不带**硬路由授权：
    提交后台任务是持久动作，只有模型能发起，运行时不许替用户提交。
    """
    registry = _build_module(FakeHost()).tools_for({"work"})

    submit = registry.resolve("work__submit")
    assert submit is not None
    assert submit.risk == "low"
    assert submit.hard_routable is False, "提交后台任务不许被运行时直接发起"
    assert submit.deterministic_route_allowed is False, "确定性路由不再能提交任务"

    status = registry.resolve("work__status")
    assert status is not None
    assert status.effect == EFFECT_READ, "work__status 是纯查询，必须只读"
    assert status.risk == "low"

    # cancel 要按高风险处理：它真的会停掉一个在跑的任务。
    for name in ("work__cancel", "work__plan_submit"):
        spec = registry.resolve(name)
        assert spec is not None
        assert spec.requires_confirmation, f"{name} 必须走高风险确认"
        assert spec.deterministic_route_allowed is False, f"{name} 永远不能被确定性路由直调"


# ──────────────────────── 2. 统一状态查询的分派 ────────────────────────


@pytest.mark.asyncio
async def test_status_lists_tasks_when_no_id_is_given() -> None:
    host = FakeHost()
    result = await _tool(host, "status").handler({"kind": "task"}, CHAT_SCOPE)

    assert ("list", (WORK_SCOPE,)) in host.calls
    assert result["status"] == "ok"
    assert [item["work_item_id"] for item in result["output"]["items"]] == ["w-1"]


@pytest.mark.asyncio
async def test_status_reads_one_task_when_id_is_given() -> None:
    host = FakeHost()
    result = await _tool(host, "status").handler({"kind": "task", "id": "w-1"}, CHAT_SCOPE)

    assert ("get", ("w-1", WORK_SCOPE)) in host.calls
    assert result["status"] == "ok"
    assert result["output"]["found"] is True
    assert result["output"]["item"]["work_item_id"] == "w-1"


@pytest.mark.asyncio
async def test_status_reports_a_missing_task_instead_of_raising() -> None:
    result = await _tool(FakeHost(), "status").handler({"kind": "task", "id": "missing"}, CHAT_SCOPE)

    assert result["status"] == "ok"
    assert result["output"]["found"] is False


@pytest.mark.asyncio
async def test_status_lists_plans_and_passes_limit_through() -> None:
    host = FakeHost()
    result = await _tool(host, "status").handler({"kind": "plan", "limit": 5}, CHAT_SCOPE)

    assert ("list_plans", (WORK_SCOPE, 5)) in host.calls
    assert result["status"] == "ok"
    assert [item["plan_id"] for item in result["output"]["items"]] == ["p-1"]


@pytest.mark.asyncio
async def test_status_reads_one_plan_when_id_is_given() -> None:
    host = FakeHost()
    result = await _tool(host, "status").handler({"kind": "plan", "id": "p-1"}, CHAT_SCOPE)

    assert ("get_plan", ("p-1", WORK_SCOPE)) in host.calls
    assert result["status"] == "ok"
    assert result["output"]["item"]["plan_id"] == "p-1"


@pytest.mark.asyncio
async def test_status_rejects_an_unknown_kind() -> None:
    """kind 拼错时必须**明确失败**，不能默默当成 task 或 plan。

    当成 task 的后果是"查了另一个东西还以为查到了"；当成 plan 同理。
    这种错误在模型侧很难自查，所以要在返回里说清楚它错在哪。
    """
    for kind in ("", "tasks", "task_list", "whatever", "计划"):
        result = await _tool(FakeHost(), "status").handler({"kind": kind}, CHAT_SCOPE)
        assert result["status"] == "failed", kind
        assert result["error_kind"] == "permanent", kind
        assert "kind" in result["error"], result["error"]


@pytest.mark.asyncio
async def test_kind_tolerates_case_and_surrounding_space() -> None:
    """大小写与首尾空白吃掉 —— 模型给 "Task" / " plan " 是常事。

    这不是放松校验：归一化之后仍然是**精确匹配**那两个值，
    拼错的词照样被上面那条挡住。
    """
    host = FakeHost()
    for raw, expect in (("Task", "list"), (" plan ", "list_plans")):
        result = await _tool(host, "status").handler({"kind": raw}, CHAT_SCOPE)
        assert result["status"] == "ok", (raw, result)
        assert expect in {call[0] for call in host.calls}, (raw, host.calls)


# ──────────────────────── 3. 统一取消的分派 ────────────────────────


@pytest.mark.asyncio
async def test_cancel_stops_a_task() -> None:
    host = FakeHost()
    result = await _tool(host, "cancel").handler({"kind": "task", "id": "w-1"}, CHAT_SCOPE)

    assert ("cancel", ("w-1", WORK_SCOPE)) in host.calls
    assert result["status"] == "ok"
    assert result["output"]["cancelled"] is True


@pytest.mark.asyncio
async def test_cancel_stops_a_plan() -> None:
    host = FakeHost()
    result = await _tool(host, "cancel").handler({"kind": "plan", "id": "p-1"}, CHAT_SCOPE)

    assert ("cancel_plan", ("p-1", WORK_SCOPE)) in host.calls
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_cancel_requires_an_id() -> None:
    """取消必须有编号：没有编号时不许猜"最近那个"。

    猜的后果是用户说一句"取消"就停掉了他没提的那个任务。
    """
    for kind in ("task", "plan"):
        host = FakeHost()
        result = await _tool(host, "cancel").handler({"kind": kind}, CHAT_SCOPE)
        assert result["status"] == "failed", kind
        assert result["error_kind"] == "permanent", kind
        assert host.calls == [], f"{kind}: 缺 id 时不该碰 Host"


@pytest.mark.asyncio
async def test_cancel_reports_a_missing_target_without_leaking_a_keyerror() -> None:
    for kind, target in (("task", "cancel"), ("plan", "cancel_plan")):
        host = FakeHost()
        result = await _tool(host, "cancel").handler({"kind": kind, "id": "missing"}, CHAT_SCOPE)
        assert result["status"] == "failed", kind
        assert result["error_kind"] == "permanent", kind
        assert "missing" not in result["error"], "不该把内部 KeyError 原文抛给模型"
        assert target in {call[0] for call in host.calls}


# ──────────────────────── 4. Scope 投影 ────────────────────────


def test_work_scope_pins_module_id_and_keeps_chat_identity() -> None:
    projected = _work_scope(CHAT_SCOPE)
    assert projected == WORK_SCOPE
    assert projected.module_id == "work"
    assert (projected.owner_id, projected.channel, projected.conversation_id) == ("alice", "web", "conv-1")


def test_work_scope_overwrites_a_foreign_module_id() -> None:
    assert _work_scope(Scope("bob", "desktop", "c9", "netease")).module_id == "work"


@pytest.mark.asyncio
async def test_every_tool_projects_scope_to_work() -> None:
    """四个入口都落在同一个 work Scope —— 否则聊天建的任务在 Web 工作页看不到。"""
    host = FakeHost()
    for name, arguments in (
        ("submit", {"task_type": "file.list", "objective": "列出文件", "input": {"directory": "D:/x"}}),
        ("plan_submit", {"objective": "整理目录", "steps": [{"step_id": "s1", "task_type": "file.list", "objective": "列"}]}),
        ("status", {"kind": "task"}),
        ("cancel", {"kind": "task", "id": "w-1"}),
    ):
        await _tool(host, name).handler(arguments, CHAT_SCOPE)

    assert len(host.calls) == 4, host.calls
    for _name, args in host.calls:
        scopes = [value for value in args if isinstance(value, Scope)]
        assert scopes and all(scope == WORK_SCOPE for scope in scopes)


# ──────────────────────── 5. 参数透传与错误分类 ────────────────────────


@pytest.mark.asyncio
async def test_submit_passes_task_type_objective_and_input_through() -> None:
    host = FakeHost()
    result = await _tool(host, "submit").handler(
        {"task_type": "file.list", "objective": "列出 D:/x 的文件", "input": {"directory": "D:/x", "sort_by": "name"}},
        CHAT_SCOPE,
    )

    assert ("submit", (WORK_SCOPE, "列出 D:/x 的文件", "file.list", {"directory": "D:/x", "sort_by": "name"})) in host.calls
    assert result == {"status": "queued", "work_item_id": "w-1", "attempt_id": "a-1"}


@pytest.mark.asyncio
async def test_submit_tolerates_a_missing_input_block() -> None:
    """input 缺失时按空对象透传，而不是 KeyError —— 必填校验是 Host 的职责。"""
    host = FakeHost()
    await _tool(host, "submit").handler({"task_type": "screen.capture", "objective": "截图"}, CHAT_SCOPE)

    assert ("submit", (WORK_SCOPE, "截图", "screen.capture", {})) in host.calls


@pytest.mark.asyncio
async def test_submit_classifies_permission_errors() -> None:
    host = FakeHost(raises=PermissionError("目录不在允许范围内"))
    result = await _tool(host, "submit").handler(
        {"task_type": "file.list", "objective": "列出", "input": {"directory": "D:/secret"}}, CHAT_SCOPE
    )

    assert result["status"] == "failed"
    assert result["error_kind"] == "permission"
    assert "PermissionError" in result["error"]


@pytest.mark.asyncio
async def test_submit_classifies_permanent_errors() -> None:
    host = FakeHost(raises=ValueError("unsupported task_type: shell.exec"))
    result = await _tool(host, "submit").handler(
        {"task_type": "shell.exec", "objective": "越界", "input": {}}, CHAT_SCOPE
    )

    assert result["status"] == "failed"
    assert result["error_kind"] == "permanent"


@pytest.mark.asyncio
async def test_plan_submit_surfaces_host_validation_errors() -> None:
    host = FakeHost(raises=ValueError("plan step references an unknown dependency"))
    result = await _tool(host, "plan_submit").handler(
        {"objective": "整理", "steps": [{"step_id": "s1", "task_type": "file.list", "objective": "列"}]},
        CHAT_SCOPE,
    )

    assert result["status"] == "failed"
    assert result["error_kind"] == "permanent"


@pytest.mark.asyncio
async def test_submit_handler_survives_the_tool_executor() -> None:
    """走一遍真正的执行器：handler 是 async、超时与异常边界仍由执行器兜底。"""
    host = FakeHost()
    executor = ToolExecutor()
    result = await executor.execute(
        _tool(host, "submit"),
        ToolCall("call-1", "work__submit", {"task_type": "file.list", "objective": "列出", "input": {"directory": "D:/x"}}),
        CHAT_SCOPE,
    )

    assert result.status == "ok"
    assert result.output["work_item_id"] == "w-1"


def test_tools_are_cheap_and_side_effect_free_to_build() -> None:
    """build_tools 每次请求都会重跑，所以它只允许拼 Spec，不许碰 Host。"""
    host = FakeHost()
    first = make_work_tools(host)
    second = make_work_tools(host)

    assert [spec.name for spec in first] == [spec.name for spec in second]
    assert host.calls == []


# ──────────────────────── 6. 遗留 `work__approve` 待确认记录 ────────────────────────


@pytest.mark.asyncio
async def test_a_legacy_approve_confirmation_cannot_execute_anything(tmp_path: Any) -> None:
    """工具删了，**积压的待确认记录**也不能变成一条执行路径。

    老库里可能躺着一条 `tool="work__approve"` 的 ConfirmationRequest
    （用户当时没点）。删掉工具之后，那条记录还在，用户仍然可能点它。
    必须保证：

    1. 它**不会执行**（工具不存在，网关回 unknown_action）；
    2. 它**不会自动转换成新的审批**（不许偷偷调 host.decide_approval）；
    3. 模型拿到的那条工具回执里带**稳定错误码** `unknown_action` ——
       没有这条，"为什么没做成"只能靠模型看措辞猜，而这个动作以后会越来越少见，
       措辞早晚会被改。

    **不要求回合以失败收场**：一个已经不存在的高风险动作被拒之后，
    对话继续走完是正常的（模型会告诉用户"这一步已经不支持了"）。
    要守的是"没有副作用、没有偷换审批"，不是"必须报错"。
    """
    import json

    from agent_core.contracts import ProviderResponse, RunSnapshot
    from agent_core.loop import AgentLoop
    from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore

    class _Provider:
        def __init__(self) -> None:
            self.requests: list[list[dict[str, Any]]] = []

        async def complete(self, messages: Any, **_kwargs: Any) -> Any:
            self.requests.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return ProviderResponse(message={"role": "assistant", "content": "不做。"}, usage={})

    provider = _Provider()

    host = FakeHost()
    registry = _build_module(host).tools_for({"work"})
    run_store = SQLiteRunStore(tmp_path / "core.db")
    confirmations = SQLiteConfirmationStore(tmp_path / "core.db")
    loop = AgentLoop(
        provider, registry, CHAT_SCOPE,
        run_store=run_store, confirmation_store=confirmations,
    )

    # 造一条遗留记录：工具名是一个已经不存在的动作。
    legacy = confirmations.create(
        run_id=loop.run_id, scope=CHAT_SCOPE, tool_call_id="call-legacy",
        tool="work__approve", arguments={"work_item_id": "w-1"}, title="批准后台任务",
    )
    run_store.save(
        RunSnapshot(
            loop.run_id, CHAT_SCOPE,
            [
                {"role": "user", "content": "帮我截个图"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{
                        "id": "call-legacy", "type": "function",
                        "function": {"name": "work__approve", "arguments": '{"work_item_id": "w-1"}'},
                    }],
                },
            ],
            1, 0, "paused",
        )
    )

    outcome = await loop.resume(legacy.action_id, "confirmed")

    assert host.calls == [], f"遗留审批触发了真实动作：{host.calls}"
    assert not any(call[0] == "decide_approval" for call in host.calls), "遗留审批被自动转换成了新的审批"
    assert outcome.status != "paused", "遗留审批不该被换成一个新的待审批"

    receipts = [
        json.loads(item["content"])
        for request in provider.requests for item in request
        if item.get("role") == "tool" and item.get("content")
    ]
    assert receipts, "模型一条工具回执都没收到 —— 失败被静默吞掉了"
    assert any("unknown_action" in str(item.get("error") or "") for item in receipts), receipts


# ──────────────────────── 7. 跨入口共用同一 work Scope（真 Host） ────────────────────────


def _real_work_module(tmp_path: Any):
    """真的 work 模块 + 真的 WorkHost（只建不 start：本组用例不跑 provider）。"""
    from agent_core.work_module import build_work_module

    spec, host = build_work_module(db_path=tmp_path / "work.db", allowed_roots=[tmp_path])
    modules = ModuleRegistry()
    modules.register(spec)
    return host, modules.tools_for({"work"})


async def _submit(registry: Any, scope: Scope, directory: str) -> str:
    result = await registry.resolve("work__submit").handler(
        {"task_type": "file.list", "objective": "列出文件", "input": {"directory": directory}}, scope
    )
    assert result["status"] == "queued", result
    return str(result["work_item_id"])


@pytest.mark.asyncio
async def test_chat_submission_is_visible_to_the_web_work_page(tmp_path: Any) -> None:
    """聊天工具提交 → Web 工作页那条口径（module_id="work"）必须能读到同一个任务。"""
    host, registry = _real_work_module(tmp_path)
    work_item_id = await _submit(registry, CHAT_SCOPE, str(tmp_path))

    web_scope = Scope("alice", "web", "conv-1", "work")
    assert host.get(work_item_id, web_scope)["work_item_id"] == work_item_id
    assert [item["work_item_id"] for item in host.list(web_scope)] == [work_item_id]
    # 统一状态入口读到的也是它。
    detail = await registry.resolve("work__status").handler({"kind": "task", "id": work_item_id}, CHAT_SCOPE)
    assert detail["output"]["item"]["work_item_id"] == work_item_id


@pytest.mark.asyncio
async def test_work_scope_isolates_owner_and_conversation(tmp_path: Any) -> None:
    """CLI 与 Web 的 owner/channel/conversation 三段必须互不串线。"""
    host, registry = _real_work_module(tmp_path)
    alice_web = await _submit(registry, Scope("alice", "web", "conv-1", "companion"), str(tmp_path))
    bob_cli = await _submit(registry, Scope("bob", "desktop", "cli-9", "companion"), str(tmp_path))

    alice_items = [item["work_item_id"] for item in host.list(Scope("alice", "web", "conv-1", "work"))]
    bob_items = [item["work_item_id"] for item in host.list(Scope("bob", "desktop", "cli-9", "work"))]

    assert alice_items == [alice_web]
    assert bob_items == [bob_cli]
    # 换个 owner / 会话 / 渠道都读不到别人的任务。
    assert host.list(Scope("bob", "web", "conv-1", "work")) == []
    assert host.list(Scope("alice", "web", "conv-2", "work")) == []
    assert host.list(Scope("alice", "desktop", "conv-1", "work")) == []
    # 用聊天 scope（module_id="companion"）也读不到 —— 任务只存在于 work 分区。
    assert host.list(CHAT_SCOPE) == []


@pytest.mark.asyncio
async def test_plan_submitted_from_chat_uses_the_same_work_scope(tmp_path: Any) -> None:
    host, registry = _real_work_module(tmp_path)
    plan_tool = registry.resolve("work__plan_submit")

    result = await plan_tool.handler(
        {
            "objective": "先列出再整理",
            "steps": [
                {"step_id": "list", "task_type": "file.list", "objective": "列出", "input": {"directory": str(tmp_path)}},
                {"step_id": "org", "task_type": "file.organize", "objective": "整理", "input": {"directory": str(tmp_path)}, "depends_on": ["list"]},
            ],
        },
        CHAT_SCOPE,
    )

    assert result["status"] == "ok", result
    plan_id = result["output"]["plan_id"]
    detail = host.get_plan(plan_id, Scope("alice", "web", "conv-1", "work"))
    assert detail["plan_id"] == plan_id
    with pytest.raises(KeyError):
        host.get_plan(plan_id, CHAT_SCOPE)

    # 统一状态入口读计划也走同一条口径。
    listed = await registry.resolve("work__status").handler({"kind": "plan"}, CHAT_SCOPE)
    assert [item["plan_id"] for item in listed["output"]["items"]] == [plan_id]


def test_web_route_scope_matches_the_chat_projection() -> None:
    """HTTP 路由的 `_work_scope(account, conversation)` 与聊天侧的投影口径必须一致，
    否则"同一个 work Scope"就只是巧合，改一处就会两边分叉。
    """
    import web_api

    assert web_api._work_scope("alice", "conv-1") == Scope("alice", "web", "conv-1", "work")


# ──────────────────────── 8. 提交参数的跨字段契约 ────────────────────────
#
# 这一节钉的是同一份契约的两个落点：
#
#     `WORK_TASK_INPUT_REQUIREMENTS`（Work 领域里唯一一份表）
#         ├─ 工具注册时挂上的 `argument_validator`：进 WorkHost **之前**拒
#         └─ 路由层只读它（影子观察，不产生任何执行效果）
#
# 为什么非要这一层：Schema 只能说"字段在不在"，说不出"提交 file.organize
# 必须带 directory" —— 这种契约是条件式的，还得认识任务类型的含义。
#
# 【为什么 WorkHost.submit 里**不**装这一道】
#
# 加过一次，被测试打回来了，错在两处，都写下来免得有人再顺手加：
#
#   1. 进到 provider 的 `task_input` 取的是 `input or context`
#      （见 `tests/test_work_host.py` 里那条绕过通道用例的说明）。只判 `input`
#      会把"目录从 context 带进来"这种正在被支持的调用判成缺参数。
#   2. 审批的判据只有一个：这个 task_type 要不要改本机状态，**与参数齐不齐无关**。
#      `test_chat_volume_submit_without_input_also_waits` 钉的就是"参数没给全也照样
#      走到审批那一步"。在提交口按参数完整性拒绝，等于让"能不能问用户"
#      取决于参数齐不齐 —— 那是两件不相干的事被绑在一起。
#
# 真正的参数拒绝在工具那一层：拦在模型动手之前，那时候还没有任何审批被挂起。


def test_input_requirements_table_is_the_single_source() -> None:
    """路由层读的必须就是这张表，不是另一份抄本。

    抄本的问题不是"多写一遍"，是**两边会分叉**：路由按"必须有 directory"
    降级追问，工具层按别的判据放行或拒绝，症状是"问了用户又没用上"。
    """
    from agent_core.routing import _required_input
    from agent_core.work_provider import WORK_TASK_INPUT_REQUIREMENTS

    for task_type, fields in WORK_TASK_INPUT_REQUIREMENTS.items():
        assert _required_input(task_type) == fields, task_type
    assert _required_input("no.such.task") == (), "未知任务类型不该凭空要求参数"


def test_validate_work_submit_arguments_accepts_a_complete_submission() -> None:
    from agent_core.work_provider import validate_work_submit_arguments

    assert (
        validate_work_submit_arguments(
            {"task_type": "file.organize", "input": {"directory": "D:/x"}}
        )
        is None
    )
    # 不要求参数的任务类型，input 整个缺失也算齐。
    assert validate_work_submit_arguments({"task_type": "screen.capture"}) is None
    # 表里没有的任务类型不归它管（"不认识的任务类型"是 Host 的拒绝理由）。
    assert validate_work_submit_arguments({"task_type": "shell.exec", "input": {}}) is None


def test_validate_work_submit_arguments_counts_blank_as_missing() -> None:
    """全空白的 directory 等于没给 —— 与网关那条统一检查同一个判据。

    纯空白是最容易漏测的一档：它有长度，`required` 与 `minLength` 都拦不住它，
    只有那个共享的"非空白"判据能拦。
    """
    from agent_core.work_provider import validate_work_submit_arguments

    for payload in ({"directory": ""}, {"directory": "   "}, {}):
        violation = validate_work_submit_arguments(
            {"task_type": "file.organize", "input": payload}
        )
        assert violation is not None, payload
        assert violation.error == "missing_task_input"
        assert violation.output == {"task_type": "file.organize", "missing": ["directory"]}


def test_validate_work_submit_arguments_accepts_a_zero_percent() -> None:
    """`percent=0` 是"静音"，不是"没给"。

    数字项**不能**套那个非空白判据：`is_blank(0)` 为真，套上去会把静音请求
    当成缺参数拒掉。这类误伤只有 0 这一档输入能触发，不专门写一条就会漏。
    """
    from agent_core.work_provider import validate_work_submit_arguments

    assert (
        validate_work_submit_arguments(
            {"task_type": "system.volume.adjust", "input": {"percent": 0}}
        )
        is None
    )
    # 真缺了还是要拦。
    violation = validate_work_submit_arguments(
        {"task_type": "system.volume.adjust", "input": {}}
    )
    assert violation is not None
    assert violation.output["missing"] == ["percent"]


@pytest.mark.asyncio
async def test_the_registered_submit_tool_carries_the_validator() -> None:
    """注册出来的 `work__submit` 必须**真的挂上**那份校验。

    这条才是守卫：少了这一步，前面几条单测全绿而生产上没有任何拦截 ——
    "校验函数写好了但没接上"是本项目出过的那类事故。
    包装层漏搬字段会造成一模一样的静默失效，所以裸 spec 与命名空间后的都查。
    """
    from agent_core.work_provider import validate_work_submit_arguments

    host = FakeHost()
    assert _tool(host, "submit").argument_validator is validate_work_submit_arguments

    namespaced = _build_module(host).tools_for({"work"}).resolve("work__submit")
    assert namespaced is not None
    assert namespaced.argument_validator is validate_work_submit_arguments, (
        "模块包装层漏搬 argument_validator —— 装上去了但生产路径上没有"
    )


@pytest.mark.asyncio
async def test_gateway_rejects_an_incomplete_submission_before_creating_anything(tmp_path: Any) -> None:
    """整条链走一遍：参数不齐的提交在网关那一步被拒，**没有任务被建出来**。

    走网关而不是直接调 handler：生产上模型只能通过网关触达 handler，
    直接调 handler 的用例证明不了拦截装在生产路径上。
    """
    from agent_core.actions import ActionGateway
    from agent_core.contracts import ActionRequest
    from agent_core.tools import DefaultRiskPolicy

    host, registry = _real_work_module(tmp_path)
    gateway = ActionGateway(
        registry=registry,
        risk_policy=DefaultRiskPolicy(),
        journal=None,
        approval_sink=None,
    )

    observation = await gateway.invoke(
        ActionRequest(
            action_id="act-1",
            name="work__submit",
            arguments={
                "task_type": "file.organize",
                "objective": "把下载目录整理一下",
                "input": {},
            },
            scope=CHAT_SCOPE,
            origin="chat_model",
        )
    )

    assert observation.status == "rejected", observation.error
    assert observation.error == "missing_task_input"
    assert observation.error_kind == "permanent"
    assert observation.output == {"task_type": "file.organize", "missing": ["directory"]}
    assert host.list(Scope("alice", "web", "conv-1", "work")) == [], "被拒的提交不该留下任务"


@pytest.mark.asyncio
async def test_gateway_lets_a_complete_submission_through(tmp_path: Any) -> None:
    """参数齐的提交照样放行 —— 免得把上面那条检查写成"一律拒绝"。"""
    from agent_core.actions import ActionGateway
    from agent_core.contracts import ActionRequest
    from agent_core.tools import DefaultRiskPolicy

    host, registry = _real_work_module(tmp_path)
    gateway = ActionGateway(
        registry=registry,
        risk_policy=DefaultRiskPolicy(),
        journal=None,
        approval_sink=None,
    )

    observation = await gateway.invoke(
        ActionRequest(
            action_id="act-2",
            name="work__submit",
            arguments={
                "task_type": "file.list",
                "objective": "列出下载目录",
                "input": {"directory": str(tmp_path)},
            },
            scope=CHAT_SCOPE,
            origin="chat_model",
        )
    )

    assert observation.status == "completed", observation.error
    assert observation.output["work_item_id"]
    assert len(host.list(Scope("alice", "web", "conv-1", "work"))) == 1


@pytest.mark.asyncio
async def test_work_host_does_not_reject_an_incomplete_submission(tmp_path: Any) -> None:
    """第二道防线**不在** Host 的提交口上，这条用例钉住这个决定。

    看起来"越早拒越好"，但提交口上拒会把两件不相干的事绑在一起：
    参数齐不齐，与"要不要先问用户一句"无关。参数没给全的提交照样该走到审批，
    让用户看见再决定（`test_chat_volume_submit_without_input_also_waits` 钉的是同一条）。

    另外 `task_input` 取的是 `input or context`，所以在提交口只看得见一半输入，
    判"缺参数"本来就会误伤。

    这条用例红了只有一种可能：有人在 Host 里加回了参数完整性检查。
    """
    host, _registry = _real_work_module(tmp_path)

    result = await host.submit(
        Scope("alice", "web", "conv-1", "work"),
        "整理下载目录",
        task_type="file.organize",
        input={},
    )

    assert result["status"], result
    assert len(host.list(Scope("alice", "web", "conv-1", "work"))) == 1
