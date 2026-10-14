"""后台任务不许占住 event loop —— 用"故意阻塞的假存储"把这件事钉死。

【为什么需要这一组】
存储层是同步 SQLite，而它现在被三类后台循环调用：开场白、定时任务调度、
媒体任务 worker，外加能力探测与会话清理。任何一处直接在 ``async def`` 里调它，
都会让同一个 loop 上跑的 SSE 推送、聊天请求、开场白**一起**停住 ——
症状是"整个页面卡一秒"，而不是"某一行报错"，所以它极难从失败里被看见。

判据是**心跳**：存储阻塞时，一个每 20ms 醒一次的 asyncio 任务仍在 100ms 内
跑完若干次。如果那个阻塞调用落在 loop 上，心跳会被按暂停键 ——
不管它阻塞多久，心跳的间隔就等于那次阻塞的时长。

为什么不用"断言耗时"而要用心跳：耗时会被调度抖动影响，而"心跳是否停摆"
是一个二值事实 —— 它要么被卡住，要么没有中间态。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_core.contracts import AccountTaskScope, Scope

BLOCK_SECONDS = 0.5
HEARTBEAT_MS = 20
#: 心跳判据的上限。远小于 BLOCK_SECONDS，又给 CI 留出抖动余量。
HEARTBEAT_LIMIT_MS = 100


class _Heartbeat:
    """每 ``HEARTBEAT_MS`` 醒一次并记下间隔，用来证明 loop 没被占住。"""

    def __init__(self) -> None:
        self.stamps: list[float] = []
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_MS / 1000)
            self.stamps.append(time.monotonic())

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def worst_gap_ms(self) -> float:
        if len(self.stamps) < 2:
            return 0.0
        return max((b - a) * 1000 for a, b in zip(self.stamps, self.stamps[1:]))


@contextlib.asynccontextmanager
async def heartbeats() -> AsyncIterator[_Heartbeat]:
    """在**已经开始跑**的 event loop 上起心跳，退出时收掉。

    这里刻意不用 pytest fixture：fixture 的同步段没有 running loop，
    ``asyncio.create_task`` 会直接 RuntimeError；而这一类断言的全部意义
    就是"被测的那个 loop 还在不在跳"，所以心跳必须和被测协程同一个 loop。
    """
    hb = _Heartbeat()
    hb.start()
    try:
        yield hb
    finally:
        await hb.stop()


def _assert_responsive(hb: _Heartbeat, label: str) -> None:
    assert hb.stamps, f"{label}：心跳一次都没跑 → loop 从头到尾被占住"
    assert hb.worst_gap_ms < HEARTBEAT_LIMIT_MS, (
        f"{label}：存储阻塞期间心跳停了 {hb.worst_gap_ms:.0f}ms，"
        f"说明那次阻塞发生在 event loop 上（阈值 {HEARTBEAT_LIMIT_MS}ms）"
    )


# ─────────────────────── 开场白 ───────────────────────


class _BlockingOpeningStore:
    """开场白用到的两种同步存储都阻塞。"""

    def recent_events(self, scope, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return []

    def record(self, scope, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return True


class _BlockingConversationRepository:
    def recent_user_messages(self, scope, **kwargs):
        time.sleep(BLOCK_SECONDS)
        return []


@pytest.mark.asyncio
async def test_opening_material_read_does_not_block_the_loop() -> None:
    """开场素材读取走工作线程 → 它阻塞时心跳照跑。"""
    from agent_core.opening_greeting import OpeningGreetingService

    service = OpeningGreetingService(
        None,  # 没有 provider：素材读完之后就停下了，不影响这条断言
        conversation_repository=_BlockingConversationRepository(),
    )
    scope = Scope("acct", "web", "conv", "companion")

    async with heartbeats() as hb:
        await service.generate(scope, visit_id="visit-abcdefgh", history=None)

    _assert_responsive(hb, "开场素材读取")


@pytest.mark.asyncio
async def test_opening_recall_read_does_not_block_the_loop() -> None:
    """联想台账读取走工作线程 → 它阻塞时心跳照跑。"""
    from agent_core.opening_greeting import OpeningGreetingService

    service = OpeningGreetingService(None, recall_store=_BlockingOpeningStore())
    scope = Scope("acct", "web", "conv", "companion")

    async with heartbeats() as hb:
        await service.generate(
            scope,
            visit_id="visit-abcdefgh",
            history=[{"role": "user", "content": "我下周三去拔牙"}],
        )

    _assert_responsive(hb, "联想台账读取")


# ─────────────────────── 能力检查 ───────────────────────


@pytest.mark.asyncio
async def test_capability_database_probe_does_not_block_the_loop(monkeypatch) -> None:
    """`PRAGMA quick_check` 是同步磁盘扫描 → 必须走工作线程。

    **这条只测一件事**：`_database_health` 有没有离开 event loop。
    所以另外两样要一起挡掉，否则测的就变成"整个运行时初始化会不会卡"：

    - `_initialize_runtime` 换成空操作。真跑它会拉 Qdrant、嵌入模型、记忆仓库、
      Work 队列 —— 既慢又依赖外部状态（Qdrant 的本地锁被别的进程占着时，
      这条用例会以一个与本主题无关的原因失败）；
    - `_module_registry` 置 None，跳过模块探测那一大段。

    症状上的区别很具体：不隔离的话，失败信息看起来像"心跳被占了"，
    但真凶在初始化里，而这两件事的修法完全不同。
    """
    import web_api

    def _slow() -> dict:
        time.sleep(BLOCK_SECONDS)
        return {"status": "healthy", "writable": True, "code": "", "reason": ""}

    monkeypatch.setattr(web_api, "_database_health", _slow)
    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)
    # 只跑到数据库这一步就够：把后面的模块探测跳过去。
    monkeypatch.setattr(web_api, "_module_registry", None)

    async with heartbeats() as hb:
        await web_api._build_capability_snapshot(force=True)

    _assert_responsive(hb, "能力探测的数据库健康检查")


# ─────────────────────── 会话维护 ───────────────────────


class _BlockingConversationRepository2:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def cleanup(self) -> int:
        self._calls.append("cleanup")
        time.sleep(BLOCK_SECONDS)
        return 0


class _BlockingIntentStore:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def clear_orphans(self) -> int:
        self._calls.append("clear_orphans")
        time.sleep(BLOCK_SECONDS)
        return 0


@pytest.mark.asyncio
async def test_conversation_maintenance_moves_storage_off_the_loop(monkeypatch) -> None:
    """会话清理与孤儿意图清理都在工作线程，且**顺序仍是先会话后孤儿**。"""
    import web_api

    calls: list[str] = []
    monkeypatch.setattr(web_api, "conversation_repository", _BlockingConversationRepository2(calls))
    monkeypatch.setattr(web_api, "intent_state_store", _BlockingIntentStore(calls))
    monkeypatch.setattr(web_api, "manager", None)
    # 每轮别真的睡 30 分钟：把间隔压成"不睡"，让循环在一轮里跑完两处清理。
    monkeypatch.setattr(web_api, "_conversation_cleanup_interval", lambda: 0)

    loop = asyncio.get_running_loop()
    task = loop.create_task(web_api._conversation_maintenance_loop())

    async with heartbeats() as hb:
        # 两处清理各阻塞 BLOCK_SECONDS：等到它们都进过一遍就够了。
        deadline = time.monotonic() + BLOCK_SECONDS * 6
        while len(calls) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert calls[:2] == ["cleanup", "clear_orphans"], "顺序：先删会话，再删孤儿状态"
    _assert_responsive(hb, "会话定期清理")


# ─────────────────────── 任务调度 ───────────────────────


class _BlockingTaskStore:
    """定时任务存储：所有读都阻塞。"""

    def __init__(self) -> None:
        self.due_calls = 0

    def due_tasks(self, now: str):
        self.due_calls += 1
        time.sleep(BLOCK_SECONDS)
        return []

    def get_task(self, task_id, scope):
        time.sleep(BLOCK_SECONDS)
        return None

    def cancel_task(self, task_id, scope):
        time.sleep(BLOCK_SECONDS)
        return False

    def set_due_now(self, task_id, scope, when):
        time.sleep(BLOCK_SECONDS)
        return False

    def claim_due_task(self, *args, **kwargs):  # pragma: no cover - 无到期任务
        raise AssertionError("没有到期任务，不该被调到")

    def finish_run(self, *args, **kwargs):  # pragma: no cover - 无到期任务
        raise AssertionError("没有到期任务，不该被调到")


@pytest.mark.asyncio
async def test_task_tick_does_not_block_the_loop() -> None:
    """调度器 tick 扫表走工作线程 → 扫表期间心跳照跑。"""
    from agent_core.task_service import TaskService

    # 没有到期任务，所以后面的 claim / finish 都不会被走到。
    service = TaskService(provider=None, registry_provider=lambda: None, store=_BlockingTaskStore())

    async with heartbeats() as hb:
        processed = await service.tick()

    assert processed == 0
    _assert_responsive(hb, "定时任务 tick")


@pytest.mark.asyncio
async def test_task_run_now_does_not_block_the_loop(monkeypatch) -> None:
    """`run_now` 的存储读也走工作线程。"""
    from agent_core.task_service import TaskService

    service = TaskService(provider=None, registry_provider=lambda: None, store=_BlockingTaskStore())
    monkeypatch.setattr(service, "registry_provider", lambda: None)
    # 任务不存在 → run_now 会提前返回；这里把 tick 钉死，证明它没被调到。
    async def _never_called() -> int:  # pragma: no cover - 任务不存在时不该走到
        raise AssertionError("任务不存在时不该 tick")

    monkeypatch.setattr(service, "tick", _never_called)

    async with heartbeats() as hb:
        queued = await service.run_now("task-1", AccountTaskScope("acct", "companion"))

    assert queued is False
    _assert_responsive(hb, "任务 run_now")


# ─────────────────────── 条件任务 ───────────────────────


class _BlockingConditionStore:
    def list(self, scope):
        time.sleep(BLOCK_SECONDS)
        return []

    def cancel(self, task_id, scope):
        time.sleep(BLOCK_SECONDS)
        return False


@pytest.mark.asyncio
async def test_condition_task_listing_does_not_block_the_loop(monkeypatch) -> None:
    """`/api/tasks` 里的条件任务列表走工作线程。"""
    import web_api

    class _Svc:
        def __init__(self) -> None:
            self.store = _BlockingConditionStore()

    monkeypatch.setattr(web_api, "_initialize_runtime", lambda: None)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(web_api, "_start_task_services", _noop)
    # 只留条件任务这一路：定时任务那路按"服务不可用"跳过。
    monkeypatch.setattr(web_api, "_task_service", lambda: None)
    monkeypatch.setattr(web_api, "_condition_task_service", lambda: _Svc())

    async with heartbeats() as hb:
        payload = await web_api.list_all_tasks(account_id="acct")

    assert payload == {"tasks": []}
    _assert_responsive(hb, "条件任务列表")


# ─────────────────────── 媒体任务 ───────────────────────


class _BlockingMediaStore:
    def recover_expired_claims(self):
        time.sleep(BLOCK_SECONDS)
        return 0

    def list_jobs_by_status(self, status, *, limit=10):
        time.sleep(BLOCK_SECONDS)
        return []

    def claim_queued_job(self, job_id, scope):  # pragma: no cover - 队列为空
        raise AssertionError("队列为空，不该抢占")


@pytest.mark.asyncio
async def test_media_worker_cycle_does_not_block_the_loop(monkeypatch, tmp_path) -> None:
    """媒体 worker 的每轮扫描（recover + list）都在工作线程。"""
    import agent_core.media_service as media_mod
    from agent_core.media_service import MediaTaskService

    service = MediaTaskService(store=_BlockingMediaStore(), artifact_dir=Path(tmp_path))

    # 只跑一轮：轮询间隔改成"立刻再次循环"，第二轮开头直接退出循环。
    rounds = 0
    real_sleep = media_mod.asyncio.sleep

    async def _sleep(seconds: float) -> None:
        nonlocal rounds
        if seconds >= 1:  # 只拦轮询间隔，不拦其他等待
            rounds += 1
            if rounds >= 2:
                raise asyncio.CancelledError()
            return None
        await real_sleep(seconds)

    monkeypatch.setattr(media_mod.asyncio, "sleep", _sleep)

    async with heartbeats() as hb:
        with contextlib.suppress(asyncio.CancelledError):
            await service._worker_loop()

    assert rounds >= 1, "worker 一轮都没跑完 → 测的不是要测的东西"
    _assert_responsive(hb, "媒体 worker 轮询")
