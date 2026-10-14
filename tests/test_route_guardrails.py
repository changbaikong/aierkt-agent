"""路由护栏与回传桥的单元测试。

覆盖六件事：
  1. hard_routable 独立标记的注册校验；
  2. 越权目录在提交前降级，而不是等 provider 执行期炸；
  3. 需要确认的任务（截屏/音量）只给 SOFT；
  4. 路径抽取的中文噪声清理；
  5. WorkResultBridge 把终态写回对话（scope 映射 + stable_key 幂等）；
  6. RouteStore.stats 的误判率聚合。
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_core.contracts import Scope, ToolSpec
from agent_core.events import EventType, InMemoryEventBus
from agent_core.persistence import SQLiteRouteStore
from agent_core.routing import RoutePath, RouteStrength, TaskRouter, _extract_directory
from agent_core.tools import ToolRegistry
from agent_core.work_bridge import REPORTABLE_STATUSES, WorkResultBridge, render_work_reply
from agent_core.work_host import WorkHost
from agent_core.work_provider import (
    WORK_TASK_TYPES,
    WorkProviderCapabilities,
    WorkResult,
)

ROOT = r"C:\allowed\root"


class FakeRegistry:
    def __init__(self, names: tuple[str, ...] = ()) -> None:
        self.names = set(names)

    def resolve(self, name: str) -> Any | None:
        return {"name": name} if name in self.names else None


# 只摘掉 `work__approve`（它已从模型工具面删除）。
# 注意这里保留原样的 `"plan_submit"`：它没带 `work__` 前缀，所以
# `TaskRouter._has_tool(PLAN_SUBMIT_TOOL)` 一直探不到它 —— 那是这个替身
# 自己的历史写法，动它会改变下面几条用例的走向，不属于这一批。
FULL = FakeRegistry(("work__submit", "plan_submit", "subagent__dispatch"))


# ──────────────────────── hard_routable 独立标记 ────────────────────────


def _schema(name: str) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def test_hard_routable_allows_non_readonly_deterministic_tool() -> None:
    """有副作用但显式授权（hard_routable=True）的工具可以走确定性路由。"""
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "work__submit",
            _schema("work__submit"),
            lambda args, scope: None,
            risk="low",
            hard_routable=True,
            deterministic_route_allowed=True,
        )
    )
    assert registry.resolve("work__submit") is not None


def test_deterministic_route_still_requires_confirmation_free_tool() -> None:
    """硬拦红线不放松：高风险工具永远不能被运行时直接执行。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(
            ToolSpec(
                "evil",
                _schema("evil"),
                lambda args, scope: None,
                risk="high",
                hard_routable=True,
                deterministic_route_allowed=True,
            )
        )


def test_deterministic_route_requires_readonly_or_hard_routable() -> None:
    """两条授权线都不满足时，注册必须当场炸掉（启动期报错，不留给用户）。"""
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        registry.register(
            ToolSpec(
                "sneaky",
                _schema("sneaky"),
                lambda args, scope: None,
                risk="low",
                deterministic_route_allowed=True,
            )
        )


# ──────────────────────── 越权预检 ────────────────────────


async def test_out_of_root_directory_is_downgraded_before_submit() -> None:
    """目录不在 allowed_roots：降级 + 明确告诉模型别提交。"""
    router = TaskRouter(registry=FULL, allowed_roots=[ROOT])
    decision = await router.decide(r"列出 D:\Downloads 所有文件")

    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.DIRECT
    assert decision.arguments == {}
    assert "不要调用 work__submit" in decision.hint
    assert "D:\\Downloads" in decision.hint


async def test_in_root_directory_suggests_submit_without_executing() -> None:
    """范围内目录：给出带参数的提交建议，但仍不执行（阶段 0 起没有执行路径）。"""
    router = TaskRouter(registry=FULL, allowed_roots=[ROOT])
    decision = await router.decide(rf"列出 {ROOT}\sub 所有文件")

    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "file.list"
    assert decision.arguments == {}
    assert "work__submit" in decision.hint


async def test_no_allowed_roots_keeps_previous_behavior() -> None:
    """调用方没给 roots 就不预检 —— 建议照样给，执行照样不归路由层。"""
    router = TaskRouter(registry=FULL)
    decision = await router.decide(r"列出 D:\Downloads 所有文件")

    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "file.list"


async def test_symlinked_directory_is_rejected() -> None:
    """与 FileWorkService._safe_path 对齐：symlink 直接拒。"""
    link = Path(tempfile.gettempdir()) / "route-guard-link"
    if link.exists() or link.is_symlink():
        link.unlink()
    try:
        link.symlink_to(Path(ROOT), target_is_directory=True)
    except OSError:
        pytest.skip("symlink unavailable on this host")
    try:
        router = TaskRouter(registry=FULL, allowed_roots=[ROOT])
        decision = await router.decide(f"列出 {link} 所有文件")
        assert decision.strength is not RouteStrength.HARD
    finally:
        if link.is_symlink() or link.exists():
            link.unlink()


# ──────────────────────── 需要确认的任务 ────────────────────────


async def test_confirm_first_asks_the_user_before_submitting() -> None:
    """截屏/音量：先向用户确认，再提交。

    提示里**不许**再出现"调用某个工具来批准"。以前这里会让模型
    "提交完再调 `work__approve` 请求确认"，那个工具 2026-09-22 从模型面删掉了：
    模型能替用户批准高风险操作，会让"提交"与"审批"两道闸门互相抵消。
    现在提示只能说"先问一句，再提交"，审批是用户的事。
    """
    decision = await TaskRouter(registry=FULL).decide("截个图")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST
    assert "work__approve" not in decision.hint
    assert "确认" in decision.hint
    assert "work__submit" in decision.hint


async def test_confirm_first_hint_never_names_an_approval_tool() -> None:
    """工具集怎么变，提示里都不许出现"调工具来批准"这条路。

    以前这条守的是"能力探测降级"：`work__approve` 在不在决定提示词怎么写。
    现在那个工具根本不存在了，所以判据反过来了 ——
    **不管注册表长什么样，提示都不该提它**。这条留着的意义是防止
    有人以后又给模型加回一个"批准"动作（换名字也算）。
    """
    router = TaskRouter(registry=FakeRegistry(("work__submit", "plan_submit")))
    decision = await router.decide("把音量调到 30")
    assert decision.strength is RouteStrength.SOFT
    assert "work__approve" not in decision.hint
    assert "approve" not in decision.hint.lower()


# ──────────────────────── 路径噪声清理 ────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (r"列出 C:\a\b 里的文件", r"C:\a\b"),
        (r"列出 D:\Downloads 所有文件", r"D:\Downloads"),
        (r"C:\用户\文档", r"C:\用户\文档"),
        (r"C:\Program Files\App", r"C:\Program Files\App"),
    ],
)
def test_extract_directory_strips_trailing_noise(text: str, expected: str) -> None:
    assert _extract_directory(text) == expected


# ──────────────────────── WorkResultBridge ────────────────────────


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[Scope, dict[str, Any], str | None, dict[str, Any]]] = []

    def append_message(self, scope: Scope, message: dict[str, Any], *, stable_key: str | None = None, now: Any = None, **kwargs: Any) -> bool:
        if any(key == stable_key for _, _, key, _ in self.calls):
            return False
        self.calls.append((scope, message, stable_key, dict(kwargs)))
        return True


class FakeEventBus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.emitted: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.emitted.append(event)
        await super().emit(event)


def _event(status: str, **overrides: Any) -> dict[str, Any]:
    base = {
        "type": status,
        "work_item_id": "w-1",
        "attempt_id": "a-1",
        "owner_id": "u1",
        "channel": "web",
        "conversation_id": "c1",
        "objective": "整理下载目录",
        "task_type": "file.list",
        "payload": {"total": 3},
    }
    base.update(overrides)
    return base


def _bridge(repo: FakeRepository | None = None, bus: FakeEventBus | None = None) -> tuple[WorkResultBridge, FakeRepository, FakeEventBus]:
    repo = repo or FakeRepository()
    bus = bus or FakeEventBus()
    return WorkResultBridge(object(), repo, bus), repo, bus


def test_render_work_reply_covers_statuses() -> None:
    assert "共 3 项" in render_work_reply("completed", "列出文件", "file.list", {"total": 3})
    assert "共移动 2 个文件" in render_work_reply("completed", "整理", "file.organize", {"applied": [1, 2]})
    assert "文件保存在" in render_work_reply("completed", "截屏", "screen.capture", {"path": "x.png"})
    assert "音量" in render_work_reply("completed", "调音量", "system.volume.adjust", {"percent": 30})
    assert "失败了" in render_work_reply("failed", "任务", "file.list", {}, "boom")
    assert "已取消" in render_work_reply("cancelled", "任务", "file.list", {})
    assert "确认" in render_work_reply("waiting_approval", "任务", "screen.capture", {})
    assert render_work_reply("queued", "任务", "file.list", {}) == ""


def test_code_agent_completion_reports_verified_patch_without_claiming_original_changed() -> None:
    reply = render_work_reply("completed", "修复运算", "code.agent", {
        "verification_status": "verified", "patch": "--- a/app.py\n+++ b/app.py",
        "apply_status": "patch_only",
    })
    assert "测试通过" in reply
    assert "补丁" in reply
    assert "原项目未改动" in reply


async def test_code_agent_read_only_completion_carries_the_answer() -> None:
    """只读检查任务：回执必须带上结论，且不能说成"没生成补丁"。"""
    reply = render_work_reply(
        "completed",
        "检查 README",
        "code.agent",
        {
            "verification_status": "not_run",
            "apply_status": "patch_only",
            "patch": "",
            "message": "README 写着：启动方式 python app.py。",
        },
    )

    assert "README 写着" in reply
    assert "没有生成补丁" not in reply
    assert "未通过" not in reply
    assert "没有改动任何文件" in reply


def test_code_agent_patch_completion_keeps_verification_wording() -> None:
    reply = render_work_reply(
        "completed",
        "修复运算",
        "code.agent",
        {
            "verification_status": "not_run",
            "apply_status": "patch_only",
            "patch": "--- a/app.py\n+++ b/app.py",
            "summary": "把减法改成加法",
        },
    )

    assert "补丁已生成" in reply
    assert "未配置可信测试" in reply
    assert "把减法改成加法" in reply


async def test_bridge_writes_to_chat_scope_and_emits_event() -> None:
    bridge, repo, bus = _bridge()
    await bridge._handle(_event("completed"))

    assert len(repo.calls) == 1
    scope, message, stable_key, kwargs = repo.calls[0]
    assert scope.module_id == "companion", "必须写回发起对话，不是 work 模块自己的 scope"
    assert scope.conversation_id == "c1"
    assert message["role"] == "assistant"
    assert "共 3 项" in message["content"]
    assert stable_key == "work-result:a-1:completed"
    # 系统回执不算用户活动：不能把没人说话的对话顶到会话列表最前
    assert kwargs.get("touch_activity") is False
    assert len(bus.emitted) == 1
    assert bus.emitted[0].type is EventType.WORK_RESULT
    assert bus.emitted[0].scope["module_id"] == "companion"


async def test_bridge_reads_code_agent_result_from_nested_output() -> None:
    bridge, repo, _ = _bridge()
    await bridge._handle(
        _event(
            "completed",
            task_type="code.agent",
            payload={
                "output": {
                    "verification_status": "verified",
                    "patch": "--- a/app.py\n+++ b/app.py",
                    "apply_status": "patch_only",
                }
            },
        )
    )

    content = repo.calls[0][1]["content"]
    assert "测试通过" in content
    assert "补丁已生成" in content
    assert "原项目未改动" in content


async def test_bridge_is_idempotent_on_replayed_events() -> None:
    """stable_key 撞了 = 已经通知过，不能再发一次事件。"""
    bridge, repo, bus = _bridge()
    await bridge._handle(_event("completed"))
    await bridge._handle(_event("completed"))

    assert len(repo.calls) == 1
    assert len(bus.emitted) == 1


async def test_bridge_skips_events_without_conversation_metadata() -> None:
    """反查不到发起对话时宁可少通知，也不能写错地方。"""
    bridge, repo, bus = _bridge()
    await bridge._handle(_event("completed", conversation_id="", owner_id=""))
    await bridge._handle(_event("started"))
    assert repo.calls == []
    assert bus.emitted == []
    assert "completed" in REPORTABLE_STATUSES


async def test_bridge_reports_waiting_approval() -> None:
    """waiting_approval 必须回传，否则"完成后告诉你"是永久悬空。"""
    bridge, repo, bus = _bridge()
    await bridge._handle(_event("waiting_approval"))
    assert len(repo.calls) == 1
    assert "确认" in repo.calls[0][1]["content"]


def test_memory_extraction_skips_work_result_receipts() -> None:
    """桥写回的系统回执必须被记忆提炼过滤，否则"「X」失败了"会被当事实记住。"""
    from agent_core.memory_extract import MemoryExtractor

    extractor = MemoryExtractor(provider=object())
    history = [
        {"role": "user", "content": "整理下载目录"},
        {"role": "assistant", "content": "「整理下载目录」完成了，共移动 2 个文件。", "kind": "work_result"},
        {"role": "assistant", "content": "好的，交给我"},
    ]
    body = extractor._build_prompt("批准", "好", history)[1]["content"]
    assert "共移动 2 个文件" not in body, "work_result 回执不该进提炼上下文"
    assert "整理下载目录" in body, "用户原话必须保留"
    assert "好的，交给我" in body, "普通 assistant 回复必须保留"


# ──────────────────────── WorkHost._attempt_meta ────────────────────────


class _FakeProvider:
    capabilities = WorkProviderCapabilities("fake", tuple(sorted(WORK_TASK_TYPES)))

    async def execute(self, request: Any, emit: Any) -> WorkResult:
        return WorkResult("completed", output={"total": 0})

    async def cancel(self, attempt_id: str) -> bool:
        return True


def test_attempt_meta_carries_scope_and_objective(tmp_path: Any) -> None:
    """广播里必须带完整的发起对话四元组 —— 这是桥能写回对话的前提。"""
    host = WorkHost(tmp_path / "work.db", provider=_FakeProvider())
    scope = Scope("u1", "web", "c1", "work")
    result = asyncio.run(host.submit(scope, "列出文件", task_type="file.list", input={"directory": ROOT}))
    meta = host._attempt_meta(result["attempt_id"])
    assert meta["owner_id"] == "u1"
    assert meta["channel"] == "web"
    assert meta["conversation_id"] == "c1"
    assert meta["module_id"] == "work"
    assert meta["objective"] == "列出文件"
    assert meta["task_type"] == "file.list"


# ──────────────────────── RouteStore.stats ────────────────────────


def test_route_stats_aggregates_misrouted(tmp_path: Any) -> None:
    store = SQLiteRouteStore(tmp_path / "route.db")
    from agent_core.routing import RouteRecord

    # HARD work_host，实际 work_host：不误判
    store.save(RouteRecord("r1", "c1", "列出 D:\\x", "work_host", "hard"))
    store.finish("r1", actual_path="work_host", final_status="completed")
    # SOFT work_host 建议，实际 direct：误判
    store.save(RouteRecord("r2", "c1", "截个图", "work_host", "soft"))
    store.finish("r2", actual_path="direct", final_status="completed")
    # NONE：没有建议，不参与误判
    store.save(RouteRecord("r3", "c1", "看看", "direct", "none"))
    store.finish("r3", actual_path="direct", final_status="completed")

    stats = store.stats()
    assert stats["total"] == 3
    assert stats["by_decided_strength"] == {"hard": 1, "soft": 1, "none": 1}
    assert stats["evaluated"] == 2
    assert stats["misrouted"] == 1
    assert stats["misrouted_rate"] == 0.5
    assert len(stats["recent_misrouted"]) == 1
    assert stats["recent_misrouted"][0]["run_id"] == "r2"


def test_route_store_migrates_old_database_without_losing_rows(tmp_path: Any) -> None:
    database = tmp_path / "old-route.db"
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE agent_core_route_decisions (
                run_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL DEFAULT '',
                user_text TEXT NOT NULL DEFAULT '',
                decided_path TEXT NOT NULL,
                decided_strength TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'rule',
                task_type TEXT DEFAULT '',
                task_id TEXT DEFAULT '',
                actual_path TEXT DEFAULT '',
                final_status TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """INSERT INTO agent_core_route_decisions VALUES
            ('old', 'c1', '看看', 'direct', 'none', '', 'rule', '', '', 'direct',
             'completed', '2026-09-01T00:00:00', '2026-09-01T00:00:00')"""
        )

    store = SQLiteRouteStore(database)
    rows = store.recent()

    assert rows[0]["run_id"] == "old"
    assert rows[0]["routing_mode"] == "legacy"
    assert rows[0]["valid_turn"] == 0


def test_model_route_stats_report_shadow_disagreement_and_time_gate(tmp_path: Any) -> None:
    from agent_core.routing import RouteRecord

    database = tmp_path / "model-route.db"
    store = SQLiteRouteStore(database)
    store.save(
        RouteRecord(
            run_id="m1",
            conversation_id="c1",
            user_text="修复代码",
            decided_path="direct",
            decided_strength="soft",
            routing_mode="model",
        )
    )
    store.finish(
        "m1",
        actual_path="work_host",
        actual_route_kind="code_agent",
        requested_tools=("work__submit",),
        requested_task_type="code.agent",
        tool_statuses=({"tool": "work__submit", "status": "ok", "task_type": "code.agent"},),
        valid_turn=True,
        model_called=True,
        final_status="completed",
    )
    started = datetime(2026, 9, 1, 8, 0, 0)
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_route_decisions SET created_at=?, updated_at=? WHERE run_id='m1'",
            (started.isoformat(timespec="seconds"), started.isoformat(timespec="seconds")),
        )
    for hours in range(0, 7 * 24 + 1, 6):
        store.record_observation_heartbeat(
            routing_mode="model",
            legacy_shadow_enabled=True,
            service_status="degraded",
            now=started + timedelta(hours=hours),
        )

    stats = store.stats(now=started + timedelta(days=7, seconds=1))

    assert stats["by_routing_mode"] == {"model": 1}
    assert stats["valid_model_turns"] == 1
    assert stats["model_route_kinds"] == {"code_agent": 1}
    assert stats["legacy_shadow_evaluated"] == 1
    assert stats["legacy_shadow_disagreed"] == 1
    assert stats["evaluated"] == 0
    assert stats["misrouted"] == 0
    assert stats["recent_misrouted"] == []
    assert stats["observation_gate"]["time_ready"] is True
    assert stats["observation_gate"]["volume_ready"] is False
    assert stats["observation_gate"]["eligible_for_manual_review"] is True


def test_model_route_time_gate_resets_after_heartbeat_gap(tmp_path: Any) -> None:
    from agent_core.routing import RouteRecord

    database = tmp_path / "routes-gap.db"
    store = SQLiteRouteStore(database)
    started = datetime(2026, 9, 1, 8, 0, 0)
    store.save(RouteRecord(
        run_id="m1", conversation_id="c1", user_text="hello",
        decided_path="direct", decided_strength="none", routing_mode="model",
    ))
    store.finish(
        "m1", actual_path="direct", actual_route_kind="direct",
        valid_turn=True, model_called=True, final_status="completed",
    )
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_route_decisions SET created_at=?, updated_at=? WHERE run_id='m1'",
            (started.isoformat(timespec="seconds"), started.isoformat(timespec="seconds")),
        )
    for hours in (0, 6, 12, 18):
        store.record_observation_heartbeat(
            routing_mode="model", legacy_shadow_enabled=True,
            service_status="healthy", now=started + timedelta(hours=hours),
        )
    resumed = started + timedelta(days=6)
    for hours in range(0, 25, 6):
        store.record_observation_heartbeat(
            routing_mode="model", legacy_shadow_enabled=True,
            service_status="healthy", now=resumed + timedelta(hours=hours),
        )

    stats = store.stats(now=started + timedelta(days=7, hours=1))

    assert stats["observation_gate"]["time_ready"] is False
    assert stats["observation_gate"]["continuous_days"] < 2
    assert stats["observation_gate"]["heartbeat_gap_hours"] == 12


def test_model_route_time_gate_requires_current_model_shadow_heartbeat(tmp_path: Any) -> None:
    from agent_core.routing import RouteRecord

    database = tmp_path / "routes-mode-break.db"
    store = SQLiteRouteStore(database)
    started = datetime(2026, 9, 1, 8, 0, 0)
    store.save(RouteRecord(
        run_id="m1", conversation_id="c1", user_text="hello",
        decided_path="direct", decided_strength="none", routing_mode="model",
    ))
    store.finish("m1", actual_path="direct", actual_route_kind="direct",
                 valid_turn=True, model_called=True, final_status="completed")
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE agent_core_route_decisions SET created_at=?, updated_at=? WHERE run_id='m1'",
            (started.isoformat(timespec="seconds"), started.isoformat(timespec="seconds")),
        )
    store.record_observation_heartbeat(
        routing_mode="model", legacy_shadow_enabled=True,
        service_status="healthy", now=started,
    )
    store.record_observation_heartbeat(
        routing_mode="legacy", legacy_shadow_enabled=True,
        service_status="healthy", now=started + timedelta(hours=6),
    )

    stats = store.stats(now=started + timedelta(hours=7))

    assert stats["observation_gate"]["time_ready"] is False
    assert stats["observation_gate"]["current_heartbeat_valid"] is False


def test_route_store_volume_gate_opens_at_one_thousand_valid_model_turns(tmp_path: Any) -> None:
    from agent_core.routing import RouteRecord

    store = SQLiteRouteStore(tmp_path / "routes-volume.db")
    for index in range(1000):
        run_id = f"model-{index}"
        store.save(
            RouteRecord(
                run_id=run_id,
                conversation_id="c1",
                user_text="直接回答",
                decided_path="direct",
                decided_strength="none",
                routing_mode="model",
            )
        )
        store.finish(
            run_id,
            actual_path="direct",
            actual_route_kind="direct",
            valid_turn=True,
            model_called=True,
            final_status="completed",
        )

    stats = store.stats()

    assert stats["valid_model_turns"] == 1000
    assert stats["observation_gate"]["volume_ready"] is True
    assert stats["observation_gate"]["eligible_for_manual_review"] is True
