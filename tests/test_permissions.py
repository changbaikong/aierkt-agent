"""账号级权限模式（标准 / 完全访问）的边界测试。

这个文件守四条线：

1. **账号级而不是会话级**：同一 owner 的所有会话一起生效，换会话不改结论。
2. **两层闸门都要放**：聊天工具层（``risk_policy``）与后台 Host 层
   （``_requires_approval`` → ``waiting_approval``）是两条独立的通道，
   只放宽一条等于半残 —— 这里两条都验。
3. **排除表优先于模式**：``FULL_ACCESS_EXCLUSIONS`` 里的工具任何模式下都要人工确认。
   它挡的是"工具自己批自己"，不是"这个工具很危险"。
4. **越权写入仍然被拒**：``_requires_approval`` / ``approval_granted`` 这些 Host 自有字段
   出现在调用方 input/context 里，无论什么模式都 ValueError。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.contracts import EFFECT_READ, Scope, ToolSpec
from agent_core.permission_mode import (
    FULL_ACCESS_EXCLUSIONS,
    MODE_FULL_ACCESS,
    MODE_STANDARD,
    REASON_NON_LOOPBACK,
    AccountAwareRiskPolicy,
    AccountPermissionController,
    AccountPermissionStore,
    FullAccessCleanup,
    LookupFromStore,
    PermissionLayer,
    is_loopback_host,
)
from agent_core.tools import DefaultRiskPolicy, ToolRegistry
from agent_core.work_provider import WorkArtifact, WorkProviderCapabilities, WorkResult


OWNER = "local-user"
OTHER_OWNER = "somebody-else"


def _high_spec(name: str = "tasks__create") -> ToolSpec:
    return ToolSpec(
        name,
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}},
        lambda _args, _scope: {},
        risk="high",
        confirmation_title="do the thing",
    )


def _low_spec(name: str = "tasks__list") -> ToolSpec:
    return ToolSpec(
        name,
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}},
        lambda _args, _scope: {},
        risk="low",
        effect=EFFECT_READ,
    )


def _scope(owner: str = OWNER, conversation: str = "web-1") -> Scope:
    return Scope(owner, "web", conversation, "companion")


# --------------------------------------------------------------------------- #
# 存储与控制器
# --------------------------------------------------------------------------- #


def test_store_defaults_to_standard_for_unknown_account(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    assert store.mode_for(OWNER) == MODE_STANDARD
    assert store.mode_for("never-seen") == MODE_STANDARD


def test_store_round_trips_mode_per_account(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)

    assert store.mode_for(OWNER) == MODE_FULL_ACCESS
    # 账号级：另一个账号不受影响
    assert store.mode_for(OTHER_OWNER) == MODE_STANDARD


def test_store_switching_back_to_standard_restores_immediately(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    store.set_mode(OWNER, MODE_STANDARD)
    assert store.mode_for(OWNER) == MODE_STANDARD


def test_store_rejects_unknown_mode(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    with pytest.raises(ValueError):
        store.set_mode(OWNER, "yolo")


def test_controller_refuses_full_access_on_non_loopback_host(tmp_path):
    controller = AccountPermissionController(
        AccountPermissionStore(tmp_path / "core.db"), web_host="0.0.0.0"
    )
    snapshot = controller.set_mode(OWNER, MODE_FULL_ACCESS)

    assert snapshot["configured_mode"] == MODE_FULL_ACCESS
    assert snapshot["effective_mode"] == MODE_STANDARD
    assert snapshot["available"] is False
    assert snapshot["reason"] == REASON_NON_LOOPBACK


def test_controller_allows_full_access_on_loopback_host(tmp_path):
    for host in ("127.0.0.1", "localhost", "::1"):
        controller = AccountPermissionController(
            AccountPermissionStore(tmp_path / f"{host.replace(':', '_')}.db"), web_host=host
        )
        snapshot = controller.set_mode(OWNER, MODE_FULL_ACCESS)
        assert snapshot["available"] is True
        assert snapshot["effective_mode"] == MODE_FULL_ACCESS


def test_controller_downgrades_stored_full_access_when_host_is_not_loopback(tmp_path):
    """配置里写着全开也不算数：换到非回环监听后实际模式降级并给出原因。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    AccountPermissionController(store, web_host="127.0.0.1").set_mode(OWNER, MODE_FULL_ACCESS)

    snapshot = AccountPermissionController(store, web_host="0.0.0.0").snapshot(OWNER)
    assert snapshot["configured_mode"] == MODE_FULL_ACCESS
    assert snapshot["effective_mode"] == MODE_STANDARD
    assert snapshot["available"] is False


def test_is_loopback_host_accepts_only_loopback_names():
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert is_loopback_host("::1")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("192.168.1.10")
    assert not is_loopback_host("")


# --------------------------------------------------------------------------- #
# 闸门一：聊天工具层
# --------------------------------------------------------------------------- #


def test_standard_mode_still_pauses_high_risk_tool(tmp_path):
    policy = AccountAwareRiskPolicy(AccountPermissionStore(tmp_path / "core.db"))
    assert policy.authorize(_high_spec(), _scope(), {}) == "confirm"


def test_full_access_auto_allows_high_risk_tool_for_that_account(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    policy = AccountAwareRiskPolicy(store)

    assert policy.authorize(_high_spec(), _scope(), {}) == "allow"


def test_full_access_applies_to_every_conversation_of_the_account(tmp_path):
    """同一账号换会话不该改变结论 —— 这是"账号级"的定义。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    policy = AccountAwareRiskPolicy(store)

    for conversation in ("web-1", "web-2", "brand-new", "cli-main"):
        assert policy.authorize(_high_spec(), _scope(OWNER, conversation), {}) == "allow"
        assert policy.authorize(
            _high_spec(), Scope(OWNER, "cli", conversation, "companion"), {}
        ) == "allow"


def test_full_access_does_not_leak_to_other_accounts(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    policy = AccountAwareRiskPolicy(store)

    assert policy.authorize(_high_spec(), _scope(OTHER_OWNER), {}) == "confirm"


def test_anonymous_scope_is_denied_in_both_modes(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    policy = AccountAwareRiskPolicy(store)
    anonymous = Scope("anonymous:abc", "web", "web-1", "companion")

    assert policy.authorize(_high_spec(), anonymous, {}) == "deny"

    store.set_mode(OWNER, MODE_FULL_ACCESS)
    assert policy.authorize(_high_spec(), anonymous, {}) == "deny"


def test_exclusion_tools_still_confirm_under_full_access(tmp_path):
    """排除表优先于模式：表里的工具任何模式下都要人工确认。

    `work__approve` 2026-09-22 从这个表里出去了 —— 不是因为它变安全了，
    而是它整个从模型工具面删掉了（模型替用户批准高风险操作，会让"提交"与"审批"
    两道闸门互相抵消）。一个不存在的工具不需要被"排除"；
    这条断言因此收成单项，但仍然写成"逐项遍历"的形态：
    下次加进来一个排除项时，它会自动覆盖到。
    """
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    policy = AccountAwareRiskPolicy(store)

    assert FULL_ACCESS_EXCLUSIONS == frozenset({"work__plan_submit"})
    for name in sorted(FULL_ACCESS_EXCLUSIONS):
        assert policy.authorize(_high_spec(name), _scope(), {}) == "confirm"


def test_exclusion_only_narrows_never_widens(tmp_path):
    """排除表不给任何权限 —— 换成低风险注册它也不该被拦。"""
    policy = AccountAwareRiskPolicy(AccountPermissionStore(tmp_path / "core.db"))
    for name in sorted(FULL_ACCESS_EXCLUSIONS):
        assert policy.authorize(_low_spec(name), _scope(), {}) == "allow"


def test_low_risk_tools_are_unaffected_by_mode(tmp_path):
    policy = AccountAwareRiskPolicy(AccountPermissionStore(tmp_path / "core.db"))
    assert policy.authorize(_low_spec(), _scope(), {}) == "allow"


def test_policy_reads_mode_at_call_time_not_at_construction(tmp_path):
    """运行时不缓存模式：切换后立刻生效，不需要重建 policy。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    policy = AccountAwareRiskPolicy(store)

    assert policy.authorize(_high_spec(), _scope(), {}) == "confirm"
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    assert policy.authorize(_high_spec(), _scope(), {}) == "allow"
    store.set_mode(OWNER, MODE_STANDARD)
    assert policy.authorize(_high_spec(), _scope(), {}) == "confirm"


def test_policy_delegates_unknown_shapes_to_default_policy(tmp_path):
    """非高风险一律交回 DefaultRiskPolicy，别在这里重新实现一套规则。"""
    policy = AccountAwareRiskPolicy(AccountPermissionStore(tmp_path / "core.db"))
    fallback = DefaultRiskPolicy()
    spec = _low_spec()
    assert policy.authorize(spec, _scope(), {}) == fallback.authorize(spec, _scope(), {})


def test_tool_specs_keep_their_risk_markers(tmp_path):
    """放宽权限不许改 ToolSpec.risk —— 切回标准模式要能立即恢复。"""
    registry = ToolRegistry()
    registry.register(_high_spec("tasks__create"))

    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    AccountAwareRiskPolicy(store).authorize(registry.resolve("tasks__create"), _scope(), {})

    assert registry.resolve("tasks__create").risk == "high"
    assert registry.resolve("tasks__create").requires_confirmation is True


# --------------------------------------------------------------------------- #
# 闸门一的真实接入：AgentLoop 拿到的是同一个 account-aware 策略实例
# --------------------------------------------------------------------------- #


def _build_chat_loop(tmp_path, store: AccountPermissionStore, scope: Scope):
    """装一个真 registry + 真 AgentLoop，验证"策略装进去了"。

    不跑完整对话：这里要证的是**接线**（同一策略实例覆盖所有会话），
    而不是模型行为 —— 后者由 prompt 矩阵那套守。
    """
    from agent_core.loop import AgentLoop

    registry = ToolRegistry()
    registry.register(_high_spec("tasks__create"))

    class _NoopProvider:
        async def complete(self, *_args, **_kwargs):  # pragma: no cover - 不被调用
            raise AssertionError("no model call expected")

    loop = AgentLoop(
        _NoopProvider(),
        registry,
        scope,
        run_store=_tmp_run_store(tmp_path),
        confirmation_store=_tmp_confirmation_store(tmp_path),
        risk_policy=AccountAwareRiskPolicy(store),
    )
    return loop


def _tmp_run_store(tmp_path):
    from agent_core.persistence import SQLiteRunStore

    return SQLiteRunStore(tmp_path / "core.db")


def _tmp_confirmation_store(tmp_path):
    from agent_core.persistence import SQLiteConfirmationStore

    return SQLiteConfirmationStore(tmp_path / "core.db")


def test_chat_loop_pauses_high_risk_in_standard_mode(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    loop = _build_chat_loop(tmp_path, store, _scope("local-user", "web-1"))
    decision = loop.risk_policy.authorize(
        loop.registry.resolve("tasks__create"), loop.scope, {}
    )
    assert decision == "confirm"


def test_chat_loop_auto_allows_in_full_access_for_every_conversation(tmp_path):
    """账号级：同一策略实例对**所有**会话都给 allow，不是只对某一个。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode("local-user", MODE_FULL_ACCESS)
    policy = AccountAwareRiskPolicy(store)

    for conversation in ("web-1", "web-2", "brand-new-conversation"):
        assert policy.authorize(_high_spec(), _scope("local-user", conversation), {}) == "allow"


def test_chat_loop_policy_switches_without_rebuilding(tmp_path):
    """切换模式不需要重建 loop —— 这正是"账号级即时生效"的实现前提。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    loop = _build_chat_loop(tmp_path, store, _scope("local-user", "web-1"))
    spec = loop.registry.resolve("tasks__create")

    assert loop.risk_policy.authorize(spec, loop.scope, {}) == "confirm"
    store.set_mode("local-user", MODE_FULL_ACCESS)
    assert loop.risk_policy.authorize(spec, loop.scope, {}) == "allow"
    store.set_mode("local-user", MODE_STANDARD)
    assert loop.risk_policy.authorize(spec, loop.scope, {}) == "confirm"


def test_chat_loop_exclusions_still_confirm_under_full_access(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode("local-user", MODE_FULL_ACCESS)
    loop = _build_chat_loop(tmp_path, store, _scope("local-user", "web-1"))
    for name in sorted(FULL_ACCESS_EXCLUSIONS):
        assert loop.risk_policy.authorize(_high_spec(name), loop.scope, {}) == "confirm"


def test_chat_loop_other_accounts_keep_confirming(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode("local-user", MODE_FULL_ACCESS)
    loop = _build_chat_loop(tmp_path, store, _scope("someone-else", "web-1"))
    assert loop.risk_policy.authorize(loop.registry.resolve("tasks__create"), loop.scope, {}) == "confirm"


def test_permission_layer_wires_policy_controller_and_cleanup(tmp_path):
    """``PermissionLayer`` 是入口唯一该用的装配点：三件套口径一致。"""
    layer = PermissionLayer(db_path=tmp_path / "core.db", web_host="127.0.0.1")

    assert layer.effective_mode == MODE_STANDARD
    layer.controller.set_mode("local-user", MODE_FULL_ACCESS)
    assert layer.effective_mode == MODE_FULL_ACCESS
    assert layer.policy.authorize(_high_spec(), _scope(), {}) == "allow"
    assert layer.cleanup.confirmation_store is None

    # attach 之后清理链路才完整
    store = _tmp_confirmation_store(tmp_path)
    store.create(
        run_id="run-1", scope=Scope("local-user", "web", "web-1", "companion"),
        tool_call_id="c1", tool="tasks__create", arguments={}, title="t",
    )
    layer.attach(confirmation_store=store)
    result = layer.cleanup.run("local-user")
    assert result["rejected_confirmations"] == 1


def test_permission_layer_honours_non_loopback(tmp_path):
    layer = PermissionLayer(db_path=tmp_path / "core.db", web_host="0.0.0.0")
    layer.controller.set_mode("local-user", MODE_FULL_ACCESS)

    assert layer.effective_mode == MODE_STANDARD
    assert layer.policy.authorize(_high_spec(), _scope(), {}) == "confirm"


# --------------------------------------------------------------------------- #
# 闸门二：后台 Host 层
# --------------------------------------------------------------------------- #

from agent_core.work_host import WorkHost  # noqa: E402


class _Recorder:
    """最小 Provider：记录它到底跑了什么，用来判断有没有走进审批。"""

    capabilities = WorkProviderCapabilities(
        "recorder",
        ("file.list", "file.organize", "screen.capture", "system.volume.adjust"),
        supports_cancel=True,
        supports_resume=False,
    )

    def __init__(self, *, moves: list[dict] | None = None) -> None:
        self.executed: list[dict] = []
        self._moves = moves or [{"source": "a.txt", "target": "txt/a.txt"}]

    async def cancel(self, attempt_id: str) -> bool:
        return True

    async def execute(self, request, emit) -> WorkResult:
        self.executed.append({"task_type": request.task_type, "input": dict(request.task_input), "granted": request.approval_granted})
        if request.task_type == "screen.capture":
            return WorkResult("completed", output={"artifact_id": "a.png"})
        if request.task_type == "system.volume.adjust":
            return WorkResult("completed", output={"volume": 50})
        if request.task_type == "file.organize":
            return WorkResult("completed", output={"moves": self._moves, "applied": []})
        return WorkResult("completed", output={"files": []})


def _run(coro):
    return asyncio.run(coro)


class _ApprovalRecorder(_Recorder):
    """像真实 local-worker 一样：``_requires_approval`` 且未授权时返回 waiting_approval。

    ``_Recorder`` 一律 completed，没法造出"挂起的 Host 审批"这个状态；
    清理逻辑要验的正是这个状态，所以必须有一个诚实的 provider。
    闸门复刻自 ``LocalWorkerProvider.execute`` 的两行判据。
    """

    async def execute(self, request, emit) -> WorkResult:
        task_input = dict(request.task_input or {})
        self.executed.append(
            {"task_type": request.task_type, "input": task_input, "granted": request.approval_granted}
        )
        if (
            request.task_type in {"screen.capture", "system.volume.adjust"}
            and task_input.get("_requires_approval")
            and not request.approval_granted
        ):
            return WorkResult(
                "waiting_approval",
                output={"reason": "explicit approval required", "task_type": request.task_type},
            )
        return await super().execute(request, emit)


def _full_access_host(tmp_path, *, moves=None) -> tuple[WorkHost, _Recorder]:
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    provider = _Recorder(moves=moves)
    host = WorkHost(
        tmp_path / "work.db",
        provider=provider,
        permission_lookup=LookupFromStore(store),
    )
    return host, provider


async def _submit_and_drain(host: WorkHost, **kwargs) -> dict:
    """提交并用**真正启动的 Host** 跑完。

    必须走 ``start()``/``stop()``：``_run_worker`` 是 ``while not self._closed``
    的长驻循环，手搓 task 时 ``_closed`` 还是 True，worker 会在拿到 attempt 之前
    就退出 —— 表现为"队列空了但 provider 一次都没被调用"。
    start() 会自己起 worker 并置 ``_closed=False``，与生产完全同路径。
    """
    await host.start()
    try:
        result = await host.submit(**kwargs)
        await _wait_idle(host)
    finally:
        await host.stop()
    return result


async def _wait_idle(host: WorkHost, *, timeout: float = 10.0) -> None:
    """等队列排空并再稳一拍（最后一个 attempt 落库需要一点时间）。"""
    deadline = asyncio.get_running_loop().time() + timeout
    idle_rounds = 0
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        if host._queue.empty():
            idle_rounds += 1
            if idle_rounds >= 5:
                return
        else:
            idle_rounds = 0
    raise AssertionError("work host did not drain in time")


def test_standard_mode_screenshot_waits_for_approval(tmp_path):
    host = WorkHost(tmp_path / "work.db", provider=_Recorder())
    _run(host.submit(_scope(), "take a screenshot", task_type="screen.capture"))

    detail = host.list(_scope())[0]
    full = host.get(detail["work_item_id"], _scope())
    assert full["input"].get("_requires_approval") is True


def test_full_access_screenshot_skips_waiting_approval(tmp_path):
    """闸门二：完全访问下截屏不再需要逐项确认，直接真跑。"""
    host, provider = _full_access_host(tmp_path)
    _run(_submit_and_drain(host, scope=_scope(), objective="shot", task_type="screen.capture"))

    assert [item["task_type"] for item in provider.executed] == ["screen.capture"]
    assert provider.executed[0]["granted"] is True

    work_item_id = host.list(_scope())[0]["work_item_id"]
    detail = host.get(work_item_id, _scope())
    assert detail["status"] == "completed"
    assert detail["approvals"] == []


def test_full_access_volume_skips_waiting_approval(tmp_path):
    host, provider = _full_access_host(tmp_path)
    _run(
        _submit_and_drain(
            host,
            scope=_scope(),
            objective="louder",
            task_type="system.volume.adjust",
            input={"percent": 50},
        )
    )
    assert [item["task_type"] for item in provider.executed] == ["system.volume.adjust"]
    work_item_id = host.list(_scope())[0]["work_item_id"]
    assert host.get(work_item_id, _scope())["approvals"] == []


def test_full_access_file_organize_runs_the_preview_it_just_generated(tmp_path):
    """文件整理不许"跳过预览"——必须先生成清单，再按**同一份**清单执行。"""
    moves = [{"source": "a.txt", "target": "txt/a.txt"}, {"source": "b.txt", "target": "txt/b.txt"}]

    class _OrganizeRecorder(_Recorder):
        async def execute(self, request, emit) -> WorkResult:
            self.executed.append({"task_type": request.task_type, "manifest": list(request.approval_manifest), "granted": request.approval_granted})
            return WorkResult("completed", output={"moves": moves, "applied": moves})

    provider = _OrganizeRecorder(moves=moves)
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    host = WorkHost(tmp_path / "work.db", provider=provider, permission_lookup=LookupFromStore(store))

    directory = tmp_path / "docs"
    directory.mkdir()
    (directory / "a.txt").write_text("x", encoding="utf-8")

    _run(_submit_and_drain(host, scope=_scope(), objective="tidy", task_type="file.organize", input={"directory": str(directory)}))

    work_item_id = host.list(_scope())[0]["work_item_id"]
    detail = host.get(work_item_id, _scope())
    assert detail["status"] == "completed"
    assert detail["approvals"] == []


def test_full_access_does_not_leak_to_other_owner_in_host(tmp_path):
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    host = WorkHost(tmp_path / "work.db", provider=_Recorder(), permission_lookup=LookupFromStore(store))

    _run(host.submit(_scope(OTHER_OWNER), "shot", task_type="screen.capture"))
    full = host.get(host.list(_scope(OTHER_OWNER))[0]["work_item_id"], _scope(OTHER_OWNER))
    assert full["input"].get("_requires_approval") is True


def test_host_freezes_approval_flag_at_submit_time(tmp_path):
    """``_requires_approval`` 是提交时按 task_type 冻结的，不随后续模式翻转。"""
    host = WorkHost(tmp_path / "work.db", provider=_Recorder())
    _run(host.submit(_scope(), "take a screenshot", task_type="screen.capture"))
    full = host.get(host.list(_scope())[0]["work_item_id"], _scope())
    assert full["input"]["_requires_approval"] is True


def test_standard_mode_plan_screenshot_step_waits_for_approval(tmp_path):
    """标准模式下，计划里的截屏步骤照样停在 waiting_approval。"""
    host = WorkHost(tmp_path / "work.db", provider=_Recorder())
    _run(
        host.submit_plan(
            _scope(),
            "shoot then read",
            [
                {"step_id": "shot", "task_type": "screen.capture", "objective": "shot"},
                {"step_id": "read", "task_type": "file.list", "objective": "read", "depends_on": ["shot"]},
            ],
        )
    )
    steps = host.get_plan(host.list_plans(_scope())[0]["plan_id"], _scope())["steps"]
    shot = next(step for step in steps if step["step_id"] == "shot")
    assert shot["requires_approval"] is True


def test_full_access_plan_step_does_not_wait_for_approval(tmp_path):
    """完全访问下，计划里的截屏步骤不再产生 approval 记录。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    host = WorkHost(tmp_path / "work.db", provider=_Recorder(), permission_lookup=LookupFromStore(store))

    _run(
        host.submit_plan(
            _scope(),
            "shoot",
            [{"step_id": "shot", "task_type": "screen.capture", "objective": "shot"}],
        )
    )
    plan_id = host.list_plans(_scope())[0]["plan_id"]
    detail = host.get_plan(plan_id, _scope())
    assert detail["pending_approvals"] == []
    shot = detail["steps"][0]
    assert shot["input"].get("_requires_approval") is None


def test_plan_submit_tool_still_needs_confirmation_even_in_full_access(tmp_path):
    """``work__plan_submit`` 在闸门一被排除表挡住：计划**入口**仍要人点头；
    点头之后（进入 Host），其中的步骤才自动跑。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    policy = AccountAwareRiskPolicy(store)

    assert policy.authorize(_high_spec("work__plan_submit"), _scope(), {}) == "confirm"


# --------------------------------------------------------------------------- #
# 账号级清理的编排
# --------------------------------------------------------------------------- #


def _make_cleaner(tmp_path, *, web_host="127.0.0.1"):
    from agent_core.persistence import SQLiteConfirmationStore

    core_db = tmp_path / "core.db"
    store = AccountPermissionStore(core_db)
    controller = AccountPermissionController(store, web_host=web_host)
    confirmations = SQLiteConfirmationStore(core_db)
    return controller, confirmations


def test_cleanup_rejects_chat_confirmations_for_the_whole_account(tmp_path):
    controller, confirmations = _make_cleaner(tmp_path)
    for conversation in ("web-1", "web-2", "cli"):
        confirmations.create(
            run_id=f"run-{conversation}", scope=Scope(OWNER, "web", conversation, "companion"),
            tool_call_id=f"c-{conversation}", tool="tasks__create", arguments={}, title="t",
        )
    confirmations.create(
        run_id="other", scope=Scope(OTHER_OWNER, "web", "web-1", "companion"),
        tool_call_id="c-other", tool="tasks__create", arguments={}, title="t",
    )

    rejected = confirmations.reject_pending_for_owner(OWNER)

    assert rejected == 3
    for conversation in ("web-1", "web-2", "cli"):
        assert confirmations.pending_for_scope(Scope(OWNER, "web", conversation, "companion")) == []
    assert len(confirmations.pending_for_scope(Scope(OTHER_OWNER, "web", "web-1", "companion"))) == 1


def test_cleanup_covers_every_module_of_the_owner(tmp_path):
    """清理范围按 owner_id —— 不能只清当前 module_id。"""
    _, confirmations = _make_cleaner(tmp_path)
    for module in ("companion", "task", "media"):
        confirmations.create(
            run_id=f"run-{module}", scope=Scope(OWNER, "web", "web-1", module),
            tool_call_id=f"c-{module}", tool="tasks__create", arguments={}, title="t",
        )

    assert confirmations.reject_pending_for_owner(OWNER) == 3
    for module in ("companion", "task", "media"):
        assert confirmations.pending_for_scope(Scope(OWNER, "web", "web-1", module)) == []


def test_unknown_owner_cleanup_is_a_noop(tmp_path):
    _, confirmations = _make_cleaner(tmp_path)
    confirmations.create(
        run_id="run-1", scope=Scope(OWNER, "web", "web-1", "companion"),
        tool_call_id="c1", tool="tasks__create", arguments={}, title="t",
    )
    assert confirmations.reject_pending_for_owner("") == 0
    assert confirmations.reject_pending_for_owner("nobody") == 0
    assert len(confirmations.pending_for_scope(Scope(OWNER, "web", "web-1", "companion"))) == 1


def test_cleanup_rejects_host_approvals_across_plans(tmp_path):
    """Host 侧的 pending approval 也要按 owner 全清，等待中的计划步骤一并终止。

    场景：账号在**标准模式**下提交了一个需要审批的截图计划 → 步骤停在
    ``waiting_approval``、attempt 挂起 pending approval。此时切到完全访问，
    旧审批必须被拒、步骤必须被终结，且**不能**被顺手执行掉。
    """

    async def scenario() -> dict:
        provider = _ApprovalRecorder()
        host = WorkHost(tmp_path / "work.db", provider=provider)
        await host.start()
        try:
            await host.submit_plan(
                _scope(),
                "shoot",
                [{"step_id": "shot", "task_type": "screen.capture", "objective": "shot"}],
            )
            await _wait_idle(host)

            # 前提：标准模式下确实挂起了一个审批、步骤停在 waiting_approval
            pending = host.ledger.pending_approval_ids_for_owner(OWNER)
            assert len(pending) == 1, "标准模式下应有 1 个挂起的 Host 审批"
            assert len(host.ledger.waiting_plan_steps_for_owner(OWNER)) == 1
            assert provider.executed[0]["granted"] is False

            cleanup = await host.reject_pending_for_owner(OWNER)
            return {"cleanup": cleanup, "provider": provider, "host": host}
        finally:
            await host.stop()

    out = asyncio.run(scenario())
    cleanup = out["cleanup"]

    assert cleanup["rejected_approvals"] == 1
    assert cleanup["cancelled_plan_steps"] == 1
    assert cleanup["failed"] == []

    # 只跑过那一次（被挡住的），**没有**在清理时被顺手重跑
    assert [item["task_type"] for item in out["provider"].executed] == ["screen.capture"]

    # 审批不再 pending，步骤已被终结、plan 也收口
    assert out["host"].ledger.pending_approval_ids_for_owner(OWNER) == []
    assert out["host"].ledger.waiting_plan_steps_for_owner(OWNER) == []
    plan_id = out["host"].ledger.list_plans(_scope())[0].plan_id
    assert out["host"].ledger.get_plan_unscoped(plan_id).status == "cancelled"


def test_cleanup_does_not_touch_other_accounts(tmp_path):
    """清理按 owner_id 隔离，别人的审批和步骤不能被动。"""

    async def scenario() -> None:
        host = WorkHost(tmp_path / "work.db", provider=_ApprovalRecorder())
        await host.start()
        try:
            await host.submit_plan(
                Scope(OTHER_OWNER, "web", "web-9", "companion"),
                "shoot",
                [{"step_id": "shot", "task_type": "screen.capture", "objective": "shot"}],
            )
            await _wait_idle(host)

            result = await host.reject_pending_for_owner(OWNER)
            assert result["rejected_approvals"] == 0
            assert result["cancelled_plan_steps"] == 0

            # 别人的依然挂在那儿
            assert len(host.ledger.pending_approval_ids_for_owner(OTHER_OWNER)) == 1
            assert len(host.ledger.waiting_plan_steps_for_owner(OTHER_OWNER)) == 1
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_cleanup_on_empty_owner_is_noop(tmp_path):
    host = WorkHost(tmp_path / "work.db", provider=_ApprovalRecorder())
    assert _run(host.reject_pending_for_owner("")) == {
        "rejected_approvals": 0,
        "cancelled_plan_steps": 0,
        "failed": [],
    }
    assert _run(host.reject_pending_for_owner("nobody"))["rejected_approvals"] == 0


def test_cleanup_uses_no_conversation_or_module_filter(tmp_path):
    """跨会话、跨 module：同一个 owner 在不同 conversation / module 下的挂起审批一起清。"""

    async def scenario() -> dict:
        host = WorkHost(tmp_path / "work.db", provider=_ApprovalRecorder())
        await host.start()
        try:
            for conversation, module in (("web-1", "companion"), ("web-2", "task"), ("web-3", "media")):
                await host.submit_plan(
                    Scope(OWNER, "web", conversation, module),
                    "shoot",
                    [{"step_id": "shot", "task_type": "screen.capture", "objective": "shot"}],
                )
            await _wait_idle(host)
            assert len(host.ledger.pending_approval_ids_for_owner(OWNER)) == 3
            return {"cleanup": await host.reject_pending_for_owner(OWNER), "host": host}
        finally:
            await host.stop()

    out = asyncio.run(scenario())
    assert out["cleanup"]["rejected_approvals"] == 3
    assert out["cleanup"]["cancelled_plan_steps"] == 3
    assert out["host"].ledger.pending_approval_ids_for_owner(OWNER) == []


# --------------------------------------------------------------------------- #
# 伪造 Host 字段
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    ["_requires_approval", "approval_granted", "approved_moves", "authorization", "authorized", "web_confirmed"],
)
def test_forged_host_owned_fields_are_rejected_in_any_mode(tmp_path, field):
    host = WorkHost(tmp_path / "work.db", provider=_Recorder())
    with pytest.raises(ValueError):
        _run(host.submit(_scope(), "x", input={field: True}))
    with pytest.raises(ValueError):
        _run(host.submit(_scope(), "x", context={field: True}))


@pytest.mark.parametrize(
    "field",
    ["_requires_approval", "approval_granted", "approved_moves", "authorization", "authorized", "web_confirmed"],
)
def test_forged_host_owned_fields_are_rejected_even_under_full_access(tmp_path, field):
    """完全访问不降低"谁有资格写 Host 字段"这条线。"""
    store = AccountPermissionStore(tmp_path / "core.db")
    store.set_mode(OWNER, MODE_FULL_ACCESS)
    host = WorkHost(tmp_path / "work.db", provider=_Recorder(), permission_lookup=LookupFromStore(store))
    with pytest.raises(ValueError):
        _run(host.submit(_scope(), "x", input={field: True}))
    with pytest.raises(ValueError):
        _run(host.submit(_scope(), "x", context={field: True}))


def test_the_removed_web_exemption_cannot_be_smuggled_back_in(tmp_path):
    """`web_confirmed` 这条豁免已删，但连**别名**一起挡的那道线不能松。

    它曾经是 Web 端"我自己确认过了"的通行证，2026-09-22 随统一审批接口一起删掉。
    删掉一个参数不等于挡住了所有写法：把同一个词塞进 input / context
    是另一条路。上面那条 `test_forged_host_owned_fields_are_rejected_even_under_full_access`
    已经覆盖了这一点，这里再从"删除的那个能力"的角度钉一次 ——
    否则下次有人翻到旧代码，会以为这只是漏了一个参数。
    """
    host = WorkHost(tmp_path / "work.db", provider=_Recorder())
    with pytest.raises(TypeError):
        _run(host.submit(_scope(), "x", task_type="screen.capture", web_confirmed=True))
    with pytest.raises(ValueError):
        _run(host.submit(_scope(), "x", task_type="screen.capture", input={"web_confirmed": True}))


# --------------------------------------------------------------------------- #
# 账号级清理
# --------------------------------------------------------------------------- #


def test_full_access_cleanup_rejects_pending_confirmations_across_conversations(tmp_path):
    from agent_core.persistence import SQLiteConfirmationStore

    store = SQLiteConfirmationStore(tmp_path / "core.db")
    first = store.create(
        run_id="run-1", scope=Scope(OWNER, "web", "web-1", "companion"),
        tool_call_id="c1", tool="tasks__create", arguments={}, title="t",
    )
    second = store.create(
        run_id="run-2", scope=Scope(OWNER, "web", "web-2", "companion"),
        tool_call_id="c2", tool="tasks__create", arguments={}, title="t",
    )
    other = store.create(
        run_id="run-3", scope=Scope(OTHER_OWNER, "web", "web-1", "companion"),
        tool_call_id="c3", tool="tasks__create", arguments={}, title="t",
    )

    rejected = store.reject_pending_for_owner(OWNER)

    assert rejected == 2
    assert store.get(first.action_id, Scope(OWNER, "web", "web-1", "companion")).status != "pending"
    assert store.get(second.action_id, Scope(OWNER, "web", "web-2", "companion")).status != "pending"
    # 别的账号必须原样不动
    assert store.get(other.action_id, Scope(OTHER_OWNER, "web", "web-1", "companion")).status == "pending"


def test_full_access_cleanup_clears_locks_for_the_whole_owner(tmp_path):
    from agent_core.persistence import SQLiteConfirmationStore

    store = SQLiteConfirmationStore(tmp_path / "core.db")
    store.create(
        run_id="run-1", scope=Scope(OWNER, "web", "web-1", "companion"),
        tool_call_id="c1", tool="tasks__create", arguments={}, title="t",
    )
    store.create_lock(
        Scope(OWNER, "web", "web-1", "companion"),
        action_id="c1",
        reason="pending",
    )

    store.reject_pending_for_owner(OWNER)

    locks = [lock for lock in store.recover_locks() if lock.owner_id == OWNER]
    assert locks == []


def test_cleanup_does_not_run_the_old_action(tmp_path):
    """清理只能拒绝，不许补跑。"""
    from agent_core.persistence import SQLiteConfirmationStore

    store = SQLiteConfirmationStore(tmp_path / "core.db")
    request = store.create(
        run_id="run-1", scope=Scope(OWNER, "web", "web-1", "companion"),
        tool_call_id="c1", tool="tasks__create", arguments={}, title="t",
    )
    store.reject_pending_for_owner(OWNER)

    decided = store.get(request.action_id, Scope(OWNER, "web", "web-1", "companion"))
    assert decided.status == "rejected"
    assert decided.status != "confirmed"


# --------------------------------------------------------------------------- #
# 清理编排：聊天侧 + Host 侧一起清
# --------------------------------------------------------------------------- #


def test_cleanup_orchestrates_both_gates(tmp_path):
    """``FullAccessCleanup.run`` 一次把两条链路的陈旧挂起都收掉，并给出账目。"""
    from agent_core.persistence import SQLiteConfirmationStore

    core = SQLiteConfirmationStore(tmp_path / "core.db")
    for index, conversation in enumerate(("web-1", "web-2"), start=1):
        core.create(
            run_id=f"run-{index}", scope=Scope(OWNER, "web", conversation, "companion"),
            tool_call_id=f"c{index}", tool="tasks__create", arguments={}, title="t",
        )

    async def submit_pending(host: WorkHost) -> None:
        await host.start()
        try:
            await host.submit_plan(
                _scope(),
                "shoot",
                [{"step_id": "shot", "task_type": "screen.capture", "objective": "shot"}],
            )
            await _wait_idle(host)
        finally:
            await host.stop()

    host = WorkHost(tmp_path / "work.db", provider=_ApprovalRecorder())
    asyncio.run(submit_pending(host))

    cleanup = FullAccessCleanup(confirmation_store=core, work_host=host)
    result = asyncio.run(_run_cleanup(cleanup, OWNER))

    assert result["rejected_confirmations"] == 2
    assert result["rejected_work_approvals"] == 1
    assert result["cancelled_plan_steps"] == 1
    assert result["failed"] == []

    # 两条链路都真的落地了
    assert core.reject_pending_for_owner(OWNER) == 0  # 已经没有 pending 可拒
    assert host.ledger.pending_approval_ids_for_owner(OWNER) == []


async def _run_cleanup(cleanup: FullAccessCleanup, owner: str) -> dict:
    """``run`` 是同步的，但 Host 那半截是 async —— 这里等它真的跑完。

    ``FullAccessCleanup._run_maybe_async`` 在事件循环外会直接 ``asyncio.run``；
    本用例自己就在循环里，所以它走线程分支。为了断言不早于清理完成，
    这里让出一个 tick 再读。
    """
    result = cleanup.run(owner)
    await asyncio.sleep(0)
    return result


def test_cleanup_reports_failures_instead_of_raising(tmp_path):
    """清理里的异常要被记进 failed，不能把"开启完全访问"整个搞崩。"""
    class _BrokenStore:
        def reject_pending_for_owner(self, owner: str) -> int:
            raise RuntimeError("db is locked")

    class _BrokenHost:
        def reject_pending_for_owner(self, owner: str):
            raise RuntimeError("ledger gone")

    cleanup = FullAccessCleanup(confirmation_store=_BrokenStore(), work_host=_BrokenHost())
    result = cleanup.run(OWNER)

    assert result["rejected_confirmations"] == 0
    assert result["rejected_work_approvals"] == 0
    assert len(result["failed"]) == 2
    assert any("chat_confirmations" in item for item in result["failed"])
    assert any("work_approvals" in item for item in result["failed"])


def test_cleanup_without_owner_reports_and_does_nothing(tmp_path):
    result = FullAccessCleanup().run("")
    assert result["rejected_confirmations"] == 0
    assert result["failed"]
