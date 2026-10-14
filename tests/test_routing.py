"""主 Chat 路由层的单元测试。

这些用例的价值不在覆盖率，而在**钉住判定口径**：
"帮我看看这个文件夹" 和 "列出这个文件夹所有文件并按扩展名统计" 都含"文件夹"，
凭关键词命中必然误拦。判定必须看动词和产出，不是主题词。
"""

from __future__ import annotations

import json
import asyncio
from typing import Any, Iterable

import pytest

from agent_core.contracts import EFFECT_PERSISTENT, EFFECT_READ, ProviderResponse, Scope, ToolSpec
from agent_core.loop import AgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRouteStore, SQLiteRunStore
from agent_core.routing import (
    MAX_REASON_CHARS,
    PLAN_SUBMIT_TOOL,
    SUBAGENT_TOOL,
    WORK_SUBMIT_TOOL,
    RouteDecision,
    RoutePath,
    RouteRecord,
    RouteStrength,
    TaskRouter,
)
from agent_core.tools import ToolRegistry
from agent_core.work_provider import WORK_TASK_TYPES, validate_work_submit_arguments


class FakeRegistry:
    """最小 registry：只提供路由层用到的 `resolve`。"""

    def __init__(self, names: Iterable[str] = ()) -> None:
        self._names = set(names)

    def resolve(self, name: str) -> Any | None:
        return object() if name in self._names else None


def full_registry() -> FakeRegistry:
    return FakeRegistry([WORK_SUBMIT_TOOL, PLAN_SUBMIT_TOOL, SUBAGENT_TOOL])


# ──────────────────────── 规则层：不许误拦 ────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "帮我看看这个文件夹",
        "帮我看一下 D:\\Downloads 里有什么",
        "检查一下这个目录",
        "这个文件夹里是什么东西啊",
    ],
)
async def test_exploratory_requests_are_never_hard_routed(text: str) -> None:
    """探索性请求：动词是"看看/检查"，没有可枚举的产出，绝不能硬拦。"""
    decision = await TaskRouter(registry=full_registry()).decide(text)
    assert decision.strength is not RouteStrength.HARD
    assert decision.path is RoutePath.DIRECT


async def test_explore_verb_wins_over_topic_keyword() -> None:
    """核心用例：两句都含"文件夹"，结果必须不同 —— 证明判据不是关键词。"""
    router = TaskRouter(registry=full_registry())

    looking = await router.decide("帮我看看这个文件夹")
    listing = await router.decide("列出 D:\\Downloads 所有文件并按扩展名统计")

    assert looking.strength is RouteStrength.NONE
    # 2026-09-23 阶段 0：规则命中最多给 SOFT 建议，运行时不再替模型提交。
    assert listing.strength is RouteStrength.SOFT
    assert listing.task_type == "file.list"


async def test_capture_verb_is_not_swallowed_by_trailing_explore_word() -> None:
    """"截个图看看" 里有"看看"，但"截图"是强枚举动词 —— 顺序反了就会漏。

    截屏提交后必须用户确认，所以只给 SOFT，不再硬拦 —— 但仍要**命中**
    这条规则（SOFT 也带 task_type），不能被"看看"吞掉。
    """
    decision = await TaskRouter(registry=full_registry()).decide("截个图看看")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "screen.capture"
    assert decision.path is RoutePath.WORK_HOST


# ──────────────────────── 规则层：只建议、不执行 ────────────────────────
# 2026-09-23（code-agent 计划阶段 0）：`work__submit` 的确定性执行路径已删除。
# 规则命中现在一律 SOFT，参数放进建议文本让模型自己核对；
# 这里钉住的是"路由层再也交不出可执行参数"这条边界。


async def test_list_files_suggests_submit_with_extracted_parameters() -> None:
    decision = await TaskRouter(registry=full_registry()).decide(
        "列出 D:\\Downloads 所有文件并按扩展名统计"
    )
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST
    assert decision.task_type == "file.list"
    # 参数已经抽好，放进建议文本（而不是可执行参数）。
    # 注意是 JSON 形态，反斜杠在文本里被转义成 `\\`，所以只断言目录名与字段名。
    assert '"directory"' in decision.hint
    assert "Downloads" in decision.hint
    assert "work__submit" in decision.hint


async def test_organize_with_drive_letter_suggests_submit() -> None:
    """"整理 D 盘" 这类盘符写法要能补成合法根路径，并出现在建议里。"""
    decision = await TaskRouter(registry=full_registry()).decide("整理 D 盘")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "file.organize"
    assert "D:" in decision.hint.upper()


async def test_volume_adjustment_extracts_percent() -> None:
    decision = await TaskRouter(registry=full_registry()).decide("把音量调到 30")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "system.volume.adjust"
    # SOFT 不带执行参数；参数留给模型调 work__submit 时自己给
    assert not decision.arguments


@pytest.mark.parametrize(
    "text",
    ["把网易云音量调到 30%", "把云音乐的音量调到 30", "调低播放器音量到 20"],
)
async def test_app_volume_is_not_system_volume(text: str) -> None:
    """点名播放器的音量是**应用音量**，不是整机音量。

    两句都含"音量+调到+数字"，但走的是完全不同的工具：前者是
    netease__set_volume（要确认），后者是 WorkHost 的 system.volume.adjust。
    判成系统音量的话，用户会为了一个他没要的操作去点一次确认 ——
    确认一次改错地方，比不做更糟。
    """
    decision = await TaskRouter(registry=full_registry()).decide(text)

    assert decision.task_type != "system.volume.adjust"
    assert decision.strength is not RouteStrength.HARD


async def test_screenshot_needs_no_arguments() -> None:
    decision = await TaskRouter(registry=full_registry()).decide("截个图")
    # 截屏要用户确认：SOFT 建议，不硬拦
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST
    assert not decision.arguments


async def test_route_decision_never_carries_execution_arguments() -> None:
    """路由层不再产出可执行参数 —— `arguments` 恒空，这是"只建议不执行"的硬边界。

    以前 `_hard()` 会填 `arguments`，运行时照着直接调 `work__submit`。
    只要那个字段还能填上，就说明有一条绕过模型的执行路径又长回来了。
    """
    router = TaskRouter(registry=full_registry())
    for text in ("整理 D:\\Downloads", "列出 D:\\Downloads 所有文件", "截个图", "把音量调到 30"):
        decision = await router.decide(text)
        assert decision.arguments == {}, f"{text!r} 不应带可执行参数"
        assert decision.strength is not RouteStrength.HARD, f"{text!r} 不该产生 HARD"


# ──────────────────────── 缺参数只给"去问用户"的建议 ────────────────────────


async def test_missing_directory_only_suggests_asking_the_user() -> None:
    """"这个文件夹" 抽不出路径 → 不能给出可提交的建议，只能让模型先问清楚。"""
    decision = await TaskRouter(registry=full_registry()).decide(
        "列出这个文件夹所有文件并按扩展名统计"
    )
    assert decision.strength is not RouteStrength.HARD
    assert decision.arguments == {}


async def test_missing_directory_without_classifier_falls_back_cleanly() -> None:
    decision = await TaskRouter(registry=full_registry()).decide("整理这个目录")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "rule"


# ──────────────────────── 多步有依赖 → plan_submit（只建议） ────────────────────────


async def test_multi_step_with_dependency_suggests_plan_not_hard_route() -> None:
    """plan_submit 是高风险工具，必须走主 loop 确认，运行时不可以直接提交。"""
    decision = await TaskRouter(registry=full_registry()).decide(
        "先整理 D:\\Downloads，然后截个图给我"
    )
    assert decision.path is RoutePath.WORK_HOST
    assert decision.strength is RouteStrength.SOFT
    assert "plan_submit" in decision.hint


async def test_two_actions_without_sequence_marker_is_not_a_plan() -> None:
    """没有先后标记就不算编排，不该滥用计划。"""
    decision = await TaskRouter(registry=full_registry()).decide("整理 D 盘和截图")
    assert decision.strength is not RouteStrength.SOFT or "plan_submit" not in decision.hint


# ──────────────────────── subagent 软引导 ────────────────────────


async def test_research_request_suggests_subagent() -> None:
    decision = await TaskRouter(registry=full_registry()).decide("帮我分析一下这几个文件的差异")
    assert decision.path is RoutePath.SUBAGENT
    assert decision.strength is RouteStrength.SOFT
    assert SUBAGENT_TOOL in decision.hint


async def test_subagent_hint_absent_when_tool_missing() -> None:
    """工具不存在就别指一条走不通的路。"""
    router = TaskRouter(registry=FakeRegistry([WORK_SUBMIT_TOOL]))
    decision = await router.decide("帮我分析一下这几个文件的差异")
    assert decision.path is not RoutePath.SUBAGENT


# ──────────────────────── 降级行为 ────────────────────────


async def test_no_work_tools_disables_routing() -> None:
    decision = await TaskRouter(registry=FakeRegistry()).decide("整理 D:\\Downloads")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "fallback"


async def test_disabled_router_is_transparent() -> None:
    decision = await TaskRouter(registry=full_registry(), enabled=False).decide("整理 D:\\Downloads")
    assert decision.strength is RouteStrength.NONE


async def test_empty_input_is_safe() -> None:
    decision = await TaskRouter(registry=full_registry()).decide("")
    assert decision.strength is RouteStrength.NONE


async def test_router_never_raises_on_broken_registry() -> None:
    class Broken:
        def resolve(self, name: str) -> Any:
            raise RuntimeError("boom")

    decision = await TaskRouter(registry=Broken()).decide("整理 D:\\Downloads")
    assert decision.strength is RouteStrength.NONE


# ──────────────────────── LLM 后备 ────────────────────────


async def test_llm_classifier_can_complete_missing_parameters() -> None:
    """规则抽不出目录，LLM 补上了 → 建议里带上模型补的参数（仍是 SOFT）。"""

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "file.organize", "input": {"directory": "D:\\Downloads"}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is RouteStrength.SOFT
    assert decision.source == "llm"
    assert "Downloads" in decision.hint
    assert decision.arguments == {}


async def test_llm_classifier_can_reject() -> None:
    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": False}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "llm"


async def test_llm_classifier_without_parameters_only_suggests() -> None:
    """LLM 说是结构化任务但没给参数 → 仍然不能硬拦。"""
    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "file.organize", "input": {}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is RouteStrength.SOFT


async def test_llm_classifier_unknown_task_type_is_ignored() -> None:
    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "not.a.real.type", "input": {}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is RouteStrength.NONE


async def test_llm_classifier_timeout_degrades() -> None:
    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        await asyncio.sleep(5)
        return {"structured": True}

    router = TaskRouter(registry=full_registry(), classifier=classifier, timeout=0.01)
    decision = await router.decide("整理这个目录")
    assert decision.strength is not RouteStrength.HARD


async def test_llm_classifier_exception_degrades() -> None:
    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        raise RuntimeError("upstream unavailable")

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is not RouteStrength.HARD


async def test_llm_is_not_called_for_obvious_requests() -> None:
    """明确命中/明确无关的请求都不该打扰 LLM —— 否则每轮都多一次调用。"""
    calls: list[str] = []

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        calls.append(text)
        return {"structured": False}

    router = TaskRouter(registry=full_registry(), classifier=classifier)
    await router.decide("列出 D:\\Downloads 所有文件")   # 规则直接命中
    await router.decide("今天天气不错")                   # 明确无关
    assert calls == []


async def test_llm_cannot_upgrade_confirm_first_rule_to_hard() -> None:
    """调音量规则 hard=False：即使 LLM 补出 percent，也只能给 SOFT 确认，绝不硬拦。"""

    async def classifier(text, *, candidates):
        return {"structured": True, "task_type": "system.volume.adjust", "input": {"percent": 30}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("调一下音量")
    assert decision.task_type == "system.volume.adjust"
    assert decision.strength is RouteStrength.SOFT
    assert not decision.arguments


async def test_llm_completed_directory_is_rechecked_against_allowed_roots(tmp_path) -> None:
    """LLM 补出的目录同样要过 allowed_roots 预检，越权则降级 DIRECT/SOFT。"""
    outside = tmp_path.parent / "outside"

    async def classifier(text, *, candidates):
        return {"structured": True, "task_type": "file.organize", "input": {"directory": str(outside)}}

    decision = await TaskRouter(
        registry=full_registry(), classifier=classifier, allowed_roots=[tmp_path]
    ).decide("整理这个目录")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.DIRECT
    assert "不在允许范围" in decision.reason


async def test_llm_cannot_switch_uncertain_rule_to_another_task_type() -> None:
    """命中的是 file.organize，LLM 却返回 system.volume.adjust：不能偷偷换成别的类型。"""

    async def classifier(text, *, candidates):
        return {"structured": True, "task_type": "system.volume.adjust", "input": {"percent": 30}}

    decision = await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "llm"


# ──────────────────────── LLM 后备：候选范围与来源记录 ────────────────────────
# 这一组钉住两条口径：
#   ① 后备分类器只该看到**当前命中规则**的那一个 task_type，不该拿到全部任务清单
#     （拿到全部等于给它机会"换一条规则执行"，而它的职责只是补参数）。
#   ② 只要这一轮用过模型补参，最终 source 就必须是 `llm`——包括被判成
#     需确认（SOFT）和目录越权（SOFT/DIRECT）的降级结果。
#     否则统计上会把"模型补的参"记成"规则命中"，模型改坏了也没人看得出来。


async def test_classifier_candidates_are_limited_to_the_matched_rule() -> None:
    """分类器只接收当前规则对应的一个 task_type。"""
    seen: list[tuple[str, ...]] = []

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        seen.append(tuple(candidates))
        return {"structured": True, "task_type": "file.organize", "input": {"directory": "D:\\Downloads"}}

    await TaskRouter(registry=full_registry(), classifier=classifier).decide("整理这个目录")

    assert seen == [("file.organize",)], f"候选应只有命中规则，实际收到 {seen}"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("调一下音量", "system.volume.adjust"),
        ("列出这个文件夹里的文件", "file.list"),
    ],
)
async def test_classifier_candidates_track_each_matched_rule(text: str, expected: str) -> None:
    """不同规则命中时，候选跟着换成那一个。"""
    seen: list[tuple[str, ...]] = []

    async def classifier(user_text: str, *, candidates: Any) -> dict[str, Any]:
        seen.append(tuple(candidates))
        return {"structured": False}

    await TaskRouter(registry=full_registry(), classifier=classifier).decide(text)

    assert seen and seen[0] == (expected,), f"{text!r} 的候选应是 {(expected,)}，实际 {seen}"


async def test_llm_completed_confirm_first_rule_is_recorded_as_llm(tmp_path: Any) -> None:
    """模型补参数 → 即使因为"需要确认"降为 SOFT，来源也必须是 llm。"""

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "system.volume.adjust", "input": {"percent": 30}}

    router = TaskRouter(registry=full_registry(), classifier=classifier)
    decision = await router.decide("调一下音量")

    assert decision.strength is RouteStrength.SOFT
    assert decision.source == "llm", "模型补过参就该记 llm，不能记成 rule"


async def test_llm_out_of_roots_result_is_recorded_as_llm(tmp_path: Any) -> None:
    """模型补参数 → 即使目录越权而降级，来源也必须是 llm。"""
    outside = tmp_path.parent / "outside"

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "file.organize", "input": {"directory": str(outside)}}

    router = TaskRouter(registry=full_registry(), classifier=classifier, allowed_roots=[tmp_path])
    decision = await router.decide("整理这个目录")

    assert decision.path is RoutePath.DIRECT
    assert decision.strength is RouteStrength.SOFT
    assert decision.source == "llm", "越权降级也是模型补参的结果，来源必须记 llm"


async def test_llm_sources_land_in_the_route_store(tmp_path: Any) -> None:
    """落库记录里的 source 同样要是 llm —— 统计口径和内存决策必须一致。"""
    store = SQLiteRouteStore(tmp_path / "routes.db")

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "system.volume.adjust", "input": {"percent": 30}}

    router = TaskRouter(registry=full_registry(), classifier=classifier)
    decision = await router.decide("调一下音量")
    store.save(RouteRecord(
        run_id="run-llm",
        decided_path=decision.path.value,
        decided_strength=decision.strength.value,
        source=decision.source,
        task_type=decision.task_type,
    ))

    records = store.recent(limit=10)
    assert records, "记录应已落库"
    assert records[0]["source"] == "llm"


async def test_llm_completed_parameters_are_still_recorded_as_llm(tmp_path: Any) -> None:
    """参数补齐并形成提交建议时，来源同样是 llm。"""

    async def classifier(text: str, *, candidates: Any) -> dict[str, Any]:
        return {"structured": True, "task_type": "file.organize", "input": {"directory": "D:\\Downloads"}}

    router = TaskRouter(registry=full_registry(), classifier=classifier)
    decision = await router.decide("整理这个目录")

    assert decision.strength is RouteStrength.SOFT
    assert decision.source == "llm"


# ──────────────────────── 记录与误判 ────────────────────────


def test_route_record_detects_mismatch() -> None:
    record = RouteRecord(
        run_id="r1",
        decided_path=RoutePath.WORK_HOST.value,
        decided_strength=RouteStrength.SOFT.value,
        actual_path=RoutePath.DIRECT.value,
    )
    assert record.misrouted is True

    agreed = RouteRecord(
        run_id="r2",
        decided_path=RoutePath.WORK_HOST.value,
        decided_strength=RouteStrength.SOFT.value,
        actual_path=RoutePath.WORK_HOST.value,
    )
    assert agreed.misrouted is False


def test_route_record_ignores_none_strength() -> None:
    """NONE 表示"我没意见"，不算误判。"""
    record = RouteRecord(
        run_id="r3",
        decided_strength=RouteStrength.NONE.value,
        decided_path=RoutePath.DIRECT.value,
        actual_path=RoutePath.WORK_HOST.value,
    )
    assert record.misrouted is False


def test_decision_record_fields_are_serialisable() -> None:
    decision = RouteDecision(
        path=RoutePath.WORK_HOST,
        strength=RouteStrength.HARD,
        reason="x" * 500,
        task_type="file.list",
    )
    fields = decision.as_record_fields()
    assert fields["decided_path"] == "work_host"
    assert fields["decided_strength"] == "hard"
    assert len(fields["reason"]) <= MAX_REASON_CHARS


# ──────────────────────── 口径守卫 ────────────────────────


def test_required_input_matches_known_task_types() -> None:
    """必填输入表必须跟 Host 的 task_type 集合一一对上，防止漂移。

    这张表住在 `agent_core/work_provider.py`（Work 领域的契约，唯一来源），
    路由层只是读者 —— 所以守卫也要读**表本身**，不能再去 `routing` 里找它
    （routing 里那份 `_REQUIRED_INPUT` 已经删掉，省得同一件事有两个答案）。
    """
    from agent_core.work_provider import WORK_TASK_INPUT_REQUIREMENTS

    assert set(WORK_TASK_INPUT_REQUIREMENTS) == set(WORK_TASK_TYPES)


# ──────────────────────── loop 端到端 ────────────────────────
# 上面测的是 router 本身；这一段测的是"第五条分岔真的接进了主链路"。

_SUBMIT_SCHEMA = {
    "type": "function",
    "function": {
        "name": WORK_SUBMIT_TOOL,
        "description": "Submit a structured task to the background Host worker.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string"},
                "objective": {"type": "string"},
                "input": {"type": "object"},
            },
            "required": ["task_type", "objective", "input"],
        },
    },
}


class RecordingProvider:
    """假 provider：记录被调用次数和收到的消息。

    路由层只给建议，所以结构化请求**必须**走到模型这里；
    真正"没被调用"的是提交动作（`submitted` 列表为空）。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.messages: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ProviderResponse:
        self.calls += 1
        self.messages = list(messages)
        return ProviderResponse(
            message={"role": "assistant", "content": "好的"},
            usage={"completion_tokens": 4},
        )


def _build_loop(
    tmp_path: Any,
    provider: RecordingProvider,
    *,
    with_router: bool = True,
    deterministic: bool = True,
    with_plan_tool: bool = False,
) -> tuple[AgentLoop, list[dict[str, Any]], Any]:
    submitted: list[dict[str, Any]] = []

    async def submit(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        submitted.append(dict(arguments))
        return {"status": "queued", "work_item_id": "w-1", "attempt_id": "a-1"}

    async def plan_submit(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        submitted.append({"_plan": True, **arguments})
        return {"status": "ok"}

    registry = ToolRegistry()
    # `deterministic` 参数留着做对照：阶段 0 之后这个标记对运行时的行为
    # 没有任何影响（路由层再也没有可执行路径），两种取值都必须一样不提交。
    registry.register(
        ToolSpec(
            WORK_SUBMIT_TOOL,
            _SUBMIT_SCHEMA,
            submit,
            risk="low",
            effect=EFFECT_READ if deterministic else EFFECT_PERSISTENT,
            deterministic_route_allowed=deterministic,
        )
    )
    if with_plan_tool:
        # 名字必须用真实的命名空间形态（work__plan_submit），不能用常量本身 ——
        # 用常量会把常量错配喂回注册表，测成"常量与它自己一致"。
        # 守门：tests/test_routing_real_registry.py 用真实 Work 模块构建注册表。
        registry.register(
            ToolSpec(
                PLAN_SUBMIT_TOOL,
                {"type": "function", "function": {"name": PLAN_SUBMIT_TOOL, "parameters": {"type": "object"}}},
                plan_submit,
                risk="high",
                confirmation_title="确认执行计划",
            )
        )

    db = tmp_path / "core.db"
    loop = AgentLoop(
        provider,
        registry,
        Scope("u1", "web", "c1", "companion"),
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        router=TaskRouter(registry=registry) if with_router else None,
        route_store=SQLiteRouteStore(db),
    )
    return loop, submitted, db


async def test_loop_suggests_but_never_submits_for_the_model(tmp_path: Any) -> None:
    """结构化请求：运行时只注入建议，提交与否交给模型。

    2026-09-23（阶段 0）之前这条走的是硬拦：不调模型、直接提交。
    现在必须反过来 —— 模型被调用、一个任务都没提交、账本记 soft。
    """
    provider = RecordingProvider()
    loop, submitted, db = _build_loop(tmp_path, provider)

    outcome = await loop.run("列出 D:\\Downloads 所有文件")

    assert outcome.status == "completed"
    assert submitted == [], "运行时不许替模型提交后台任务"
    assert provider.calls >= 1, "建议要交给模型，模型必须被调用"
    hinted = [m for m in provider.messages if "work__submit" in str(m.get("content") or "")]
    assert hinted, "建议文本应该出现在发给模型的消息里"

    record = SQLiteRouteStore(db).recent()[0]
    assert record["decided_path"] == "work_host"
    assert record["decided_strength"] == "soft"
    assert record["actual_path"] == "direct", "模型自己没提交，实际路径就是 direct"
    assert not record["task_id"], "没有提交就不该有任务号"


async def test_loop_leaves_exploratory_request_to_the_model(tmp_path: Any) -> None:
    """"帮我看看这个文件夹" 必须落到模型手里，且回填为 direct（不是误判）。"""
    provider = RecordingProvider()
    loop, submitted, db = _build_loop(tmp_path, provider)

    await loop.run("帮我看看这个文件夹")

    assert submitted == []
    assert provider.calls >= 1
    record = SQLiteRouteStore(db).recent()[0]
    assert record["actual_path"] == "direct"
    assert record["decided_strength"] == "none"


async def test_loop_without_router_behaves_exactly_as_before(tmp_path: Any) -> None:
    """没有注入 router 时不能有任何路由行为 —— 这是可回滚的保证。"""
    provider = RecordingProvider()
    loop, submitted, _ = _build_loop(tmp_path, provider, with_router=False)

    await loop.run("列出 D:\\Downloads 所有文件")

    assert submitted == []
    assert provider.calls >= 1


async def test_loop_never_executes_regardless_of_tool_flags(tmp_path: Any) -> None:
    """工具标了什么授权标记都不影响：路由层不再有可执行路径。

    这条替代了原来的"工具没开确定性路由时降级"用例 —— 那个前提
    （存在一条会检查 `deterministic_route_allowed` 的执行路径）已经不存在，
    留着会让人以为还有这么一条链。
    """
    provider = RecordingProvider()
    loop, submitted, _ = _build_loop(tmp_path, provider, deterministic=False)

    outcome = await loop.run("列出 D:\\Downloads 所有文件")

    assert submitted == []
    assert provider.calls >= 1
    assert outcome.status == "completed"


async def test_loop_injects_soft_hint_without_executing(tmp_path: Any) -> None:
    """多步有依赖 → 只注入建议，绝不直接提交（plan_submit 必须走用户确认）。"""
    provider = RecordingProvider()
    loop, submitted, db = _build_loop(tmp_path, provider, with_plan_tool=True)

    await loop.run("先整理 D:\\Downloads，然后截个图给我")

    assert submitted == [], "SOFT 档绝不能由运行时直接执行"
    assert provider.calls >= 1
    hinted = [m for m in provider.messages if PLAN_SUBMIT_TOOL in str(m.get("content") or "")]
    assert hinted, "建议文本应该出现在发给模型的消息里"
    record = SQLiteRouteStore(db).recent()[0]
    assert record["decided_strength"] == "soft"
    assert record["actual_path"] == "direct"


async def test_model_mode_records_legacy_shadow_without_injecting_hint(tmp_path: Any) -> None:
    provider = RecordingProvider()
    loop, submitted, db = _build_loop(tmp_path, provider)
    loop.routing_mode = "model"
    loop.legacy_shadow_enabled = True

    outcome = await loop.run("列出 D:\\Downloads 所有文件")

    assert outcome.status == "completed"
    assert submitted == []
    assert not any("[路由建议]" in str(message.get("content") or "") for message in provider.messages)
    record = SQLiteRouteStore(db).recent()[0]
    assert record["decided_path"] == "work_host"
    assert record["actual_path"] == "direct"


async def test_model_shadow_toggle_does_not_change_messages_seen_by_model(tmp_path: Any) -> None:
    (tmp_path / "shadow").mkdir()
    (tmp_path / "plain").mkdir()
    shadow_provider = RecordingProvider()
    shadow_loop, _, _ = _build_loop(tmp_path / "shadow", shadow_provider)
    shadow_loop.routing_mode = "model"
    shadow_loop.legacy_shadow_enabled = True

    plain_provider = RecordingProvider()
    plain_loop, _, _ = _build_loop(tmp_path / "plain", plain_provider)
    plain_loop.routing_mode = "model"
    plain_loop.legacy_shadow_enabled = False

    await shadow_loop.run("列出 D:\\Downloads 所有文件")
    await plain_loop.run("列出 D:\\Downloads 所有文件")

    assert shadow_provider.messages == plain_provider.messages


async def test_model_mode_with_shadow_disabled_does_not_call_legacy_router(tmp_path: Any) -> None:
    provider = RecordingProvider()
    loop, submitted, db = _build_loop(tmp_path, provider)
    loop.routing_mode = "model"
    loop.legacy_shadow_enabled = False

    async def forbidden(_text: str) -> Any:
        raise AssertionError("legacy router must not run when shadow is disabled")

    loop.router.decide = forbidden
    outcome = await loop.run("列出 D:\\Downloads 所有文件")

    assert outcome.status == "completed"
    assert submitted == []
    record = SQLiteRouteStore(db).recent()[0]
    assert record["routing_mode"] == "model"
    assert record["source"] == "disabled"
    assert record["actual_route_kind"] == "direct"
    assert record["valid_turn"] == 1


class ToolChoosingProvider:
    def __init__(self) -> None:
        self.responses = iter(
            [
                ProviderResponse(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-code",
                                "type": "function",
                                "function": {
                                    "name": WORK_SUBMIT_TOOL,
                                    "arguments": json.dumps(
                                        {
                                            "task_type": "code.agent",
                                            "objective": "修复项目",
                                            "input": {"project_root": "C:/secret-project"},
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            }
                        ],
                    },
                    usage={"completion_tokens": 8},
                ),
                ProviderResponse(
                    message={"role": "assistant", "content": "任务已提交。"},
                    usage={"completion_tokens": 4},
                ),
            ]
        )

    async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
        return next(self.responses)


async def test_model_route_record_classifies_code_agent_without_storing_arguments(tmp_path: Any) -> None:
    submitted: list[dict[str, Any]] = []

    async def submit(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        submitted.append(dict(arguments))
        return {"status": "queued", "work_item_id": "w-code"}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            WORK_SUBMIT_TOOL,
            _SUBMIT_SCHEMA,
            submit,
            risk="low",
            effect=EFFECT_PERSISTENT,
        )
    )
    db = tmp_path / "route-model.db"
    loop = AgentLoop(
        ToolChoosingProvider(),
        registry,
        Scope("u1", "web", "c1", "companion"),
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        route_store=SQLiteRouteStore(db),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )

    outcome = await loop.run("请修复这个代码项目")

    assert outcome.status == "completed"
    assert submitted and submitted[0]["task_type"] == "code.agent"
    record = SQLiteRouteStore(db).recent()[0]
    assert record["actual_route_kind"] == "code_agent"
    assert record["requested_task_type"] == "code.agent"
    assert record["task_id"] == "w-code"
    assert json.loads(record["requested_tools"]) == [WORK_SUBMIT_TOOL]
    assert json.loads(record["tool_statuses"])[0]["status"] == "ok"
    assert "secret-project" not in record["requested_tools"]
    assert "secret-project" not in record["tool_statuses"]


async def test_model_route_record_uses_only_successful_work_task_id(tmp_path: Any) -> None:
    class RetryProvider:
        def __init__(self) -> None:
            self.responses = iter([
                ProviderResponse(message={"role": "assistant", "content": "", "tool_calls": [{
                    "id": "bad", "type": "function", "function": {
                        "name": WORK_SUBMIT_TOOL,
                        "arguments": json.dumps({"task_type": "code.agent", "objective": "修复"}),
                    },
                }]}, usage={"completion_tokens": 4}),
                ProviderResponse(message={"role": "assistant", "content": "", "tool_calls": [{
                    "id": "good", "type": "function", "function": {
                        "name": WORK_SUBMIT_TOOL,
                        "arguments": json.dumps({
                            "task_type": "code.agent", "objective": "修复",
                            "input": {"project_root": "C:/project"},
                        }),
                    },
                }]}, usage={"completion_tokens": 4}),
                ProviderResponse(message={"role": "assistant", "content": "已提交。"}, usage={"completion_tokens": 4}),
            ])

        async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
            return next(self.responses)

    async def submit(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        return {"status": "queued", "work_item_id": "work-final"}

    registry = ToolRegistry()
    registry.register(ToolSpec(
        WORK_SUBMIT_TOOL, _SUBMIT_SCHEMA, submit, risk="low", effect=EFFECT_PERSISTENT,
        argument_validator=validate_work_submit_arguments,
    ))
    db = tmp_path / "route-task-id.db"
    loop = AgentLoop(
        RetryProvider(), registry, Scope("u1", "web", "c1", "companion"),
        run_store=SQLiteRunStore(db), confirmation_store=SQLiteConfirmationStore(db),
        route_store=SQLiteRouteStore(db), routing_mode="model", legacy_shadow_enabled=False,
    )

    outcome = await loop.run("修复项目")

    assert outcome.status == "completed"
    record = SQLiteRouteStore(db).recent()[0]
    assert record["task_id"] == "work-final"
    statuses = json.loads(record["tool_statuses"])
    assert [row["status"] for row in statuses] == ["rejected", "ok"]
    assert all("task_id" not in row for row in statuses)


class ToolListProvider(RecordingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.tool_names: list[str] = []

    async def complete(self, messages: list[dict[str, Any]], *, tools: Any = None, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        self.messages = list(messages)
        self.tool_names = [item["function"]["name"] for item in (tools or [])]
        return ProviderResponse(message={"role": "assistant", "content": "好的"}, usage={"completion_tokens": 4})


async def test_model_mode_bypasses_legacy_intent_gate_and_keeps_full_tool_list(tmp_path: Any) -> None:
    class ExplodingIntentStore:
        def load(self, scope: Scope) -> Any:
            raise AssertionError("legacy intent gate must not run in model mode")

    provider = ToolListProvider()
    loop, submitted, _ = _build_loop(tmp_path, provider, with_router=False)
    loop.routing_mode = "model"
    loop.legacy_shadow_enabled = False
    loop.intent_state_store = ExplodingIntentStore()

    outcome = await loop.run("帮我处理这个请求")

    assert outcome.status == "completed"
    assert submitted == []
    assert WORK_SUBMIT_TOOL in provider.tool_names


async def test_model_mode_readonly_fastpath_is_not_counted_as_model_route(tmp_path: Any) -> None:
    provider = RecordingProvider()
    loop, _, db = _build_loop(tmp_path, provider)
    async def local_info(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        return {"local_time": "2026-09-25T12:00:00+08:00"}

    loop.registry.register(
        ToolSpec(
            "system__local_info",
            {
                "type": "function",
                "function": {
                    "name": "system__local_info",
                    "parameters": {"type": "object"},
                },
            },
            local_info,
            effect=EFFECT_READ,
            deterministic_route_allowed=True,
        )
    )
    loop.routing_mode = "model"
    loop.legacy_shadow_enabled = False

    outcome = await loop.run("现在几点")

    assert outcome.status == "completed"
    assert provider.calls == 0
    assert SQLiteRouteStore(db).recent() == []


class ApprovalAlternativeProvider:
    def __init__(self) -> None:
        self.responses = iter(
            [
                ProviderResponse(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "danger-1",
                                "type": "function",
                                "function": {
                                    "name": "danger__delete",
                                    "arguments": json.dumps({"target": "demo"}),
                                },
                            }
                        ],
                    },
                    usage={"completion_tokens": 4},
                ),
                ProviderResponse(
                    message={"role": "assistant", "content": "没有执行删除，我可以改为只读检查。"},
                    usage={"completion_tokens": 6},
                ),
            ]
        )

    async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
        return next(self.responses)


async def test_rejected_approval_returns_to_model_and_updates_route_status(tmp_path: Any) -> None:
    executions: list[dict[str, Any]] = []

    async def delete(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        executions.append(dict(arguments))
        return {"deleted": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "danger__delete",
            {
                "type": "function",
                "function": {
                    "name": "danger__delete",
                    "description": "Delete a target.",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                },
            },
            delete,
            risk="high",
            effect=EFFECT_PERSISTENT,
        )
    )
    db = tmp_path / "approval-route.db"
    loop = AgentLoop(
        ApprovalAlternativeProvider(),
        registry,
        Scope("u1", "web", "c1", "companion"),
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        route_store=SQLiteRouteStore(db),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )

    paused = await loop.run("删除目标")
    completed = await loop.resume(paused.action_id, "rejected")

    assert paused.status == "paused"
    assert completed.status == "completed"
    assert "没有执行删除" in completed.reply
    assert executions == []
    record = SQLiteRouteStore(db).recent()[0]
    assert json.loads(record["tool_statuses"])[0]["status"] == "rejected"


async def test_rejected_approval_after_restart_updates_route_status(tmp_path: Any) -> None:
    executions: list[dict[str, Any]] = []

    async def delete(arguments: dict[str, Any], scope: Scope) -> dict[str, Any]:
        executions.append(dict(arguments))
        return {"deleted": True}

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "danger__delete",
            {
                "type": "function",
                "function": {
                    "name": "danger__delete",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                },
            },
            delete,
            risk="high",
            effect=EFFECT_PERSISTENT,
        )
    )
    db = tmp_path / "approval-restart-route.db"
    scope = Scope("u1", "web", "c1", "companion")
    run_id = "restart-route-run"
    first = AgentLoop(
        ApprovalAlternativeProvider(),
        registry,
        scope,
        run_id=run_id,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        route_store=SQLiteRouteStore(db),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )
    paused = await first.run("删除目标")
    resumed_provider = RecordingProvider()
    second = AgentLoop(
        resumed_provider,
        registry,
        scope,
        run_id=run_id,
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        route_store=SQLiteRouteStore(db),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )

    completed = await second.resume(paused.action_id, "rejected")

    assert completed.status == "completed"
    assert executions == []
    record = SQLiteRouteStore(db).recent()[0]
    assert json.loads(record["tool_statuses"])[0]["status"] == "rejected"


class FailedToolAlternativeProvider:
    def __init__(self) -> None:
        self.responses = iter(
            [
                ProviderResponse(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "fail-1",
                                "type": "function",
                                "function": {
                                    "name": "demo__fails",
                                    "arguments": json.dumps({"value": "demo"}),
                                },
                            }
                        ],
                    },
                    usage={"completion_tokens": 4},
                ),
                ProviderResponse(
                    message={"role": "assistant", "content": "工具失败了，我没有重复执行。"},
                    usage={"completion_tokens": 6},
                ),
            ]
        )
        self.second_messages: list[dict[str, Any]] = []

    async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
        response = next(self.responses)
        if any(item.get("role") == "tool" for item in messages):
            self.second_messages = list(messages)
        return response


async def test_model_tool_failure_is_structured_and_recorded(tmp_path: Any) -> None:
    async def fail(_arguments: dict[str, Any], _scope: Scope) -> dict[str, Any]:
        raise RuntimeError("controlled failure")

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "demo__fails",
            {
                "type": "function",
                "function": {
                    "name": "demo__fails",
                    "description": "Always fails for the test.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                        "required": ["value"],
                    },
                },
            },
            fail,
            effect=EFFECT_READ,
        )
    )
    provider = FailedToolAlternativeProvider()
    db = tmp_path / "failed-route.db"
    loop = AgentLoop(
        provider,
        registry,
        Scope("u1", "web", "c1", "companion"),
        run_store=SQLiteRunStore(db),
        confirmation_store=SQLiteConfirmationStore(db),
        route_store=SQLiteRouteStore(db),
        routing_mode="model",
        legacy_shadow_enabled=False,
    )

    outcome = await loop.run("运行这个只读检查")

    assert outcome.status == "completed"
    tool_message = next(item for item in provider.second_messages if item.get("role") == "tool")
    observation = json.loads(tool_message["content"])
    assert observation["status"] == "failed"
    assert "controlled failure" in observation["error"]
    record = SQLiteRouteStore(db).recent()[0]
    assert json.loads(record["tool_statuses"])[0]["status"] == "failed"
