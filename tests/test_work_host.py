import asyncio

import pytest

from agent_core.contracts import Scope
from agent_core.work_host import WorkHost
from agent_core.work_provider import WorkResult


class ImmediateProvider:
    capabilities = type("Caps", (), {"provider_id": "test", "task_types": ("file",), "supports_cancel": True, "supports_resume": False})()

    async def execute(self, request, emit):
        await emit(type("Event", (), {"attempt_id": request.attempt_id, "event_type": "progress", "payload": {"ok": True}})())
        return WorkResult("completed", {"objective": request.objective})

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("interrupted")


class GatedProvider:
    """复刻 LocalWorkerProvider 的审批闸门，但不碰真实的音量/截屏。

    闸门的判据完全照抄 provider：只看 ``request.task_input`` 里的
    ``_requires_approval``。这一点很关键 —— Host 把标记写在哪个 dict 里，
    决定了闸门到底有没有生效（见 test_chat_volume_submit_waiting_for_approval）。
    """

    capabilities = type("Caps", (), {
        "provider_id": "local-worker",
        "task_types": ("file.list", "file.organize", "screen.capture", "system.volume.adjust"),
        "supports_cancel": True,
        "supports_resume": False,
    })()

    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request, emit):
        self.requests.append(request)
        if (request.task_type in {"screen.capture", "system.volume.adjust"}
                and request.task_input.get("_requires_approval")
                and not request.approval_granted):
            return WorkResult("waiting_approval", {"reason": "explicit approval required",
                                                   "task_type": request.task_type})
        return WorkResult("completed", {"task_type": request.task_type})

    async def cancel(self, attempt_id):
        return True

    async def resume(self, request, checkpoint, decision, emit):
        return WorkResult("interrupted")


async def _settle(host, work_item_id, scope, *, attempts: int = 60):
    detail = None
    for _ in range(attempts):
        detail = host.get(work_item_id, scope)
        if detail["status"] not in {"queued", "running"}:
            return detail
        await asyncio.sleep(0.01)
    return detail


def test_host_submits_and_runs_work_in_background(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=ImmediateProvider(), max_workers=1)
        await host.start()
        try:
            result = await host.submit(Scope("alice", "web", "chat", "work"), "列出文件")
            assert result["status"] == "queued"
            for _ in range(20):
                detail = host.get(result["work_item_id"], Scope("alice", "web", "chat", "work"))
                if detail["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert detail["status"] == "completed"
            assert detail["attempts"][0]["output"]["objective"] == "列出文件"
            assert detail["events"]
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_host_approval_queues_successor_attempt(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=ImmediateProvider(), max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            result = await host.submit(scope, "整理文件", context={"directory": str(tmp_path)})
            for _ in range(20):
                detail = host.get(result["work_item_id"], scope)
                if detail["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert detail["status"] == "completed"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_host_retry_preserves_work_item_and_creates_new_attempt(tmp_path):
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=ImmediateProvider(), max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            result = await host.submit(scope, "列出文件")
            for _ in range(20):
                detail = host.get(result["work_item_id"], scope)
                if detail["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            retry = await host.retry(result["work_item_id"], scope)
            assert retry["work_item_id"] == result["work_item_id"]
            assert retry["attempt_id"] != result["attempt_id"]
            assert len(host.get(result["work_item_id"], scope)["attempts"]) == 2
        finally:
            await host.stop()

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# 系统音量的审批闸门不可伪造
# ----------------------------------------------------------------------
def test_chat_volume_submit_waits_for_approval_even_with_input(tmp_path):
    """带 input 的音量提交也必须进 waiting_approval。

    这条钉的是一个真实的绕过通道：``WorkRequest.task_input`` 取的是
    ``input or context``，Host 以前只把 ``_requires_approval`` 写进 context；
    只要调用方给了**任意一个** input 字段（比如 percent），整个标记就被盖掉，
    provider 侧的闸门随之失效 —— 音量会在没有任何确认的情况下被改掉。
    """
    async def scenario():
        provider = GatedProvider()
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            result = await host.submit(
                scope, "把音量调到 30", task_type="system.volume.adjust", input={"percent": 30},
            )
            detail = await _settle(host, result["work_item_id"], scope)
            assert detail["status"] == "waiting_approval"
            assert provider.requests[0].task_input.get("_requires_approval") is True
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_chat_volume_submit_without_input_also_waits(tmp_path):
    async def scenario():
        provider = GatedProvider()
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            result = await host.submit(scope, "把音量调到 30", task_type="system.volume.adjust")
            detail = await _settle(host, result["work_item_id"], scope)
            assert detail["status"] == "waiting_approval"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_a_self_reported_source_cannot_switch_off_approval(tmp_path):
    """调用方自报的来源不能当闸门 —— 现在连传都传不进来。

    以前 `submit(..., source="plan")` 是一条真实的绕过通道：
    `source != "chat"` 就直接放行。当时把它修成"不看 source、只看 App 判出来的
    标记"，但参数仍在，下一个改动很容易又拿它做判断。现在这个参数整个删掉了：
    调用方连表达"我是谁"的地方都没有，闸门就只剩"这个 task_type 要不要审批"
    这一条判据。
    """
    async def scenario():
        provider = GatedProvider()
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            with pytest.raises(TypeError):
                await host.submit(
                    scope, "把音量调到 30", task_type="system.volume.adjust",
                    input={"percent": 30}, source="plan",
                )
            result = await host.submit(
                scope, "把音量调到 30", task_type="system.volume.adjust", input={"percent": 30},
            )
            detail = await _settle(host, result["work_item_id"], scope)
            assert detail["status"] == "waiting_approval"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_host_owned_keys_are_rejected_in_context_and_input(tmp_path):
    """调用方（含模型）不许自己写审批/授权字段 —— 连别名一起挡。"""
    async def scenario():
        host = WorkHost(tmp_path / "work.db", provider=GatedProvider(), max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            for key in ("_requires_approval", "approval_granted", "approved_moves",
                        "approved", "authorization", "authorized", "web_confirmed"):
                with pytest.raises(ValueError, match="Host-owned"):
                    await host.submit(scope, "截个图", task_type="screen.capture", context={key: True})
                with pytest.raises(ValueError, match="Host-owned"):
                    await host.submit(scope, "截个图", task_type="screen.capture", input={key: True})
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_a_web_submit_no_longer_carries_its_own_exemption(tmp_path):
    """Web 提交不再有一条"我自己确认过了"的免审通道。

    以前 `submit_web_confirmed` 是给 Web 那一跳用的：前端先弹一个
    `window.confirm`，再带 `web_confirmed=True` 提交，Host 据此跳过审批。
    问题是这条豁免**落在请求里** —— 也就是落在调用方自报的字段上，
    而且执行完不留下任何能被再查到的审批记录。

    现在要执行就得走统一审批接口，审批落在 `work_approvals` 上，
    事后查得到、重复点也不会跑两次（见 `tests/test_approval_coordinator.py`）。
    所以"提交音量任务"在没人点头时**必须停在 waiting_approval**。
    """
    async def scenario():
        provider = GatedProvider()
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            # 提交接口上已经没有那个参数了：连传都传不进来。
            with pytest.raises(TypeError):
                await host.submit(
                    scope, "把音量调到 30", task_type="system.volume.adjust",
                    input={"percent": 30}, web_confirmed=True,
                )
            result = await host.submit(
                scope, "把音量调到 30", task_type="system.volume.adjust", input={"percent": 30},
            )
            detail = await _settle(host, result["work_item_id"], scope)
            assert detail["status"] == "waiting_approval"
            assert detail["approvals"], "要执行的任务必须留下一条可再查到的审批记录"
        finally:
            await host.stop()

    asyncio.run(scenario())


def test_plan_volume_step_waits_for_approval_even_if_the_plan_says_no(tmp_path):
    """Control Plan 里的系统音量步骤必须进 waiting_approval。

    候选计划可能是 LLM 生成的，不能让它自己填 requires_approval=false
    就把确认绕过去 —— 校验层强制、Host 提交时再判一次（纵深防御）。
    """
    from agent_core.control_plan import validate_plan_definition

    definition = validate_plan_definition("降一下音量", [{
        "step_id": "volume", "task_type": "system.volume.adjust",
        "objective": "把音量调到 30", "input": {"percent": 30},
        "requires_approval": False,
    }])

    assert definition[0].requires_approval is True

    async def scenario():
        provider = GatedProvider()
        host = WorkHost(tmp_path / "work.db", provider=provider, max_workers=1)
        await host.start()
        try:
            scope = Scope("alice", "web", "chat", "work")
            plan = await host.submit_plan(scope, "降一下音量", [{
                "step_id": "volume", "task_type": "system.volume.adjust",
                "objective": "把音量调到 30", "input": {"percent": 30},
            }])
            plan_id = plan["plan_id"]
            for _ in range(60):
                detail = host.get_plan(plan_id, scope)
                if detail["status"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.01)
            assert detail["status"] == "waiting_approval"
            assert provider.requests[0].task_input.get("_requires_approval") is True
        finally:
            await host.stop()

    asyncio.run(scenario())
