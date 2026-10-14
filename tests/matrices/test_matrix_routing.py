"""套1 · 路由与执行判定矩阵。

把 agent_core/routing.py 的 TaskRouter.decide() 全部分叉点系统化钉一遍，
每条 case 带 case_id（M1-ROUTE-xxx）+ @pytest.mark.matrix 标记，让统一 runner 能聚合。

分叉点对照（A 层路由判定，详见 plan）：
  A1  能力探测降级：work_submit+plan_submit 都没注册 → NONE(fallback)
  A2  多步+依赖 → plan_submit SOFT（不可硬拦，plan_submit 是 high risk）
  A3  研究类 → subagent SOFT
  A4  探索类（看看/检查）→ NONE
  A5  无任何信号 → NONE
  A6  MATCH 提交建议：file.list/file.organize + directory 合法 → SOFT（参数进 hint，
      **不再有 HARD 执行路径**，2026-09-23 阶段 0）
  A7  越权降级 SOFT：directory 在 allowed_roots 之外（本矩阵独有价值，test_routing 未覆盖）
  A8  confirm_first 降级 SOFT：screen.capture/system.volume.adjust 提交后仍要确认
  A9  UNCERTAIN + classifier 命中 + 参数齐 → SOFT（source=llm，参数进 hint）
  A10 UNCERTAIN + classifier 命中但缺参 → SOFT（source=llm）
  A11 UNCERTAIN + classifier 返回 None/超时/异常 → SOFT（source=rule，"缺少必填参数"）
  A12 UNCERTAIN + 无 classifier → NONE（交模型补全）
  A13 异常 → fallback NONE
  +   classifier 判非结构化 → NONE；classifier 未知 task_type → NONE
  +   顺序 pin："截个图看看" capture 不被 explore 吞（先枚举命中再探索排除）
  +   边界 pin：任何输入都不产生可执行参数（`arguments` 恒空）

B 层执行/集成（B13/14/15/16/20，loop 端到端）已在 tests/test_routing.py 覆盖，
本矩阵不重复，聚焦 TaskRouter 判定口径。
"""

from __future__ import annotations

import asyncio

import pytest

from agent_core.routing import (
    PLAN_SUBMIT_TOOL,
    SUBAGENT_TOOL,
    WORK_SUBMIT_TOOL,
    RoutePath,
    RouteStrength,
    TaskRouter,
)

from _common.fake_registry import FakeRegistry, full_registry

pytestmark = pytest.mark.matrix


# ── stub LLM 后备分类器（A9/A10/A11 用）──
# RouteClassifier Protocol: __call__(user_text, *, candidates) -> Mapping | None
# 返回含 structured/task_type/input/reason/confidence 的映射；None 表示放弃。


class StubClassifier:
    """可配置的 stub 分类器：返回固定响应 / 抛异常 / 延时触发超时。"""

    def __init__(self, *, response=None, exc=None, delay: float = 0.0) -> None:
        self.response = response
        self.exc = exc
        self.delay = delay
        self.calls = 0

    async def __call__(self, user_text: str, *, candidates):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return self.response


# ──────────────────────── A4 探索类 → NONE ────────────────────────


async def test_M1_ROUTE_001_exploratory_never_hard_routed() -> None:
    """A4：'帮我看看这个文件夹' 探索性，动词是'看看'，无可枚举产出 → NONE/DIRECT。"""
    decision = await TaskRouter(registry=full_registry()).decide("帮我看看这个文件夹")
    assert decision.strength is RouteStrength.NONE
    assert decision.path is RoutePath.DIRECT


async def test_M1_ROUTE_002_exploratory_with_path_still_none() -> None:
    """A4：带路径的探索'看下 D:\\Downloads 里有什么'仍 NONE，不因有路径就硬拦。"""
    decision = await TaskRouter(registry=full_registry()).decide("看下 D:\\Downloads 里有什么")
    assert decision.strength is not RouteStrength.HARD
    assert decision.path is RoutePath.DIRECT


# ──────────────────────── A6 MATCH → 提交建议（不执行）────────────────────────


async def test_M1_ROUTE_003_list_files_suggests_submit() -> None:
    """A6：'列出 D:\\Downloads 所有文件并按扩展名统计' 动词'列出'+ directory 合法
    → SOFT/file.list，参数进 hint；运行时不提交。"""
    decision = await TaskRouter(registry=full_registry()).decide(
        "列出 D:\\Downloads 所有文件并按扩展名统计"
    )
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST
    assert decision.task_type == "file.list"
    assert decision.arguments == {}, "路由层不再产出可执行参数"
    assert "work__submit" in decision.hint
    assert '"directory"' in decision.hint


async def test_M1_ROUTE_004_organize_suggests_submit() -> None:
    """A6：'整理 D:\\Downloads' 动词'整理'+ directory 合法 → SOFT/file.organize。"""
    decision = await TaskRouter(registry=full_registry()).decide("整理 D:\\Downloads")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "file.organize"
    assert decision.arguments == {}
    assert "work__submit" in decision.hint


async def test_M1_ROUTE_004b_organize_drive_letter_suggested() -> None:
    """A6：'整理 D 盘' 盘符补成 D:\\ → SOFT/file.organize，补出来的路径进 hint。"""
    decision = await TaskRouter(registry=full_registry()).decide("整理 D 盘")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "file.organize"
    assert "D:" in decision.hint.upper()


# ──────────────────────── A8 confirm_first 降级 SOFT ────────────────────────


async def test_M1_ROUTE_005_screenshot_soft_not_hard() -> None:
    """A8：'截个图' screen.capture confirm_first → SOFT（提交后仍要用户确认，不替用户走完）。"""
    decision = await TaskRouter(registry=full_registry()).decide("截个图")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST
    assert decision.task_type == "screen.capture"


async def test_M1_ROUTE_006_volume_with_percent_soft() -> None:
    """A8：'把音量调到 30' system.volume.adjust confirm_first → SOFT（参数齐也不替模型执行）。
    SOFT 决策只给 hint 不填 arguments。"""
    decision = await TaskRouter(registry=full_registry()).decide("把音量调到 30")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "system.volume.adjust"
    assert decision.hint  # SOFT 必须注入建议文本
    assert decision.arguments == {}


async def test_M1_ROUTE_007_volume_without_percent_uncertain() -> None:
    """A8→UNCERTAIN：'把音量调一下' 无数值 → UNCERTAIN（缺 percent），无 classifier → NONE。"""
    decision = await TaskRouter(registry=full_registry()).decide("把音量调一下")
    assert decision.strength is RouteStrength.NONE  # A12：无 classifier


# ──────────────────────── A7 越权降级 SOFT（本矩阵独有）────────────────────────


async def test_M1_ROUTE_008_out_of_roots_degrades_to_soft() -> None:
    """A7：'列出 E:\\Secret 所有文件' directory 在 allowed_roots 之外 → SOFT/DIRECT（不硬拦也不白提交）。"""
    router = TaskRouter(
        registry=full_registry(),
        allowed_roots=["D:\\Downloads", "D:\\Work"],
    )
    decision = await router.decide("列出 E:\\Secret 所有文件")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.DIRECT  # 越权降级走 DIRECT，不交给 work_host
    assert decision.task_type == "file.list"


async def test_M1_ROUTE_009_in_roots_still_suggests() -> None:
    """A7 对照：directory 在 allowed_roots 内 → 给出提交建议（越权预检不误伤合法目录）。"""
    router = TaskRouter(
        registry=full_registry(),
        allowed_roots=["D:\\Downloads"],
    )
    decision = await router.decide("列出 D:\\Downloads 所有文件")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST
    assert decision.task_type == "file.list"


# ──────────────────────── A2 多步+依赖 → plan_submit SOFT ────────────────────────


async def test_M1_ROUTE_010_multi_step_suggests_plan_soft() -> None:
    """A2：'先列出 D:\\A，然后整理 D:\\B' 多步+序列标记 → plan_submit SOFT（不可硬拦）。"""
    decision = await TaskRouter(registry=full_registry()).decide(
        "先列出 D:\\A 的文件，然后整理 D:\\B"
    )
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.WORK_HOST


async def test_M1_ROUTE_011_two_actions_no_sequence_not_plan() -> None:
    """A2 对照：两个动作但无序列标记 → 不走 plan（落到 file.list 硬拦或探索）。"""
    decision = await TaskRouter(registry=full_registry()).decide("列出整理 D:\\Downloads")
    assert decision.strength is not RouteStrength.SOFT or decision.path is not RoutePath.WORK_HOST or "plan" not in decision.reason


# ──────────────────────── A3 研究类 → subagent SOFT ────────────────────────


async def test_M1_ROUTE_012_research_suggests_subagent_soft() -> None:
    """A3：'分析这几个文档' 研究类 → subagent SOFT。"""
    decision = await TaskRouter(registry=full_registry()).decide("分析这几个文档")
    assert decision.strength is RouteStrength.SOFT
    assert decision.path is RoutePath.SUBAGENT


async def test_M1_ROUTE_013_subagent_absent_no_subagent_hint() -> None:
    """A3 对照：subagent 未注册时研究类不再建议 subagent（能力探测）。"""
    registry = FakeRegistry([WORK_SUBMIT_TOOL, PLAN_SUBMIT_TOOL])  # 无 subagent
    decision = await TaskRouter(registry=registry).decide("分析这几个文档")
    assert decision.path is not RoutePath.SUBAGENT


# ──────────────────────── A1 能力探测降级 / A5 无信号 / A13 异常 ────────────────────────


async def test_M1_ROUTE_014_no_work_tools_degrades_to_none() -> None:
    """A1：work_submit+plan_submit 都没注册 → NONE(fallback)。"""
    registry = FakeRegistry([SUBAGENT_TOOL])
    decision = await TaskRouter(registry=registry).decide("列出 D:\\Downloads 所有文件")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "fallback"


async def test_M1_ROUTE_015_plain_greeting_is_none() -> None:
    """A5：'你好' 无任何结构化信号 → NONE。"""
    decision = await TaskRouter(registry=full_registry()).decide("你好")
    assert decision.strength is RouteStrength.NONE


async def test_M1_ROUTE_016_empty_input_is_safe_none() -> None:
    """A5/A13：空输入 → NONE（不抛异常）。"""
    decision = await TaskRouter(registry=full_registry()).decide("")
    assert decision.strength is RouteStrength.NONE


async def test_M1_ROUTE_017_broken_registry_falls_back_to_none() -> None:
    """A13：registry 抛异常 → fallback NONE（路由是旁路，挂了静默降级）。"""

    class BrokenRegistry:
        def resolve(self, name):
            raise RuntimeError("registry broken")

    decision = await TaskRouter(registry=BrokenRegistry()).decide("列出 D:\\Downloads 所有文件")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "fallback"


# ──────────────────────── A12 UNCERTAIN + 无 classifier → NONE ────────────────────────


async def test_M1_ROUTE_018_missing_directory_no_classifier_is_none() -> None:
    """A12：'列出文件'缺 directory、无 classifier → NONE（交模型补全，不硬拦也不 SOFT）。"""
    decision = await TaskRouter(registry=full_registry()).decide("列出文件")
    assert decision.strength is RouteStrength.NONE


# ──────────────────────── A9/A10/A11 LLM 后备 ────────────────────────


async def test_M1_ROUTE_019_classifier_completes_params_to_soft_suggestion() -> None:
    """A9：UNCERTAIN + classifier 命中 + 参数齐 → SOFT(source=llm)，参数进 hint。"""
    classifier = StubClassifier(
        response={
            "structured": True,
            "task_type": "file.list",
            "input": {"directory": "D:\\Downloads"},
            "reason": "LLM 补全了目录",
            "confidence": 0.9,
        }
    )
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.SOFT
    assert decision.source == "llm"
    assert decision.task_type == "file.list"
    assert decision.arguments == {}
    assert "Downloads" in decision.hint
    assert classifier.calls == 1


async def test_M1_ROUTE_020_classifier_hits_but_missing_param_is_soft() -> None:
    """A10：UNCERTAIN + classifier 命中但缺参 → SOFT(source=llm)。"""
    classifier = StubClassifier(
        response={
            "structured": True,
            "task_type": "file.list",
            "input": {},  # 仍缺 directory
            "reason": "LLM 也抽不出目录",
        }
    )
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.SOFT
    assert decision.source == "llm"


async def test_M1_ROUTE_021_classifier_returns_none_is_soft() -> None:
    """A11：UNCERTAIN + classifier 返回 None → SOFT（'缺少必填参数'，降级建议）。"""
    classifier = StubClassifier(response=None)
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.SOFT


async def test_M1_ROUTE_022_classifier_timeout_is_soft() -> None:
    """A11：UNCERTAIN + classifier 超时 → SOFT（不阻塞主 loop）。"""
    classifier = StubClassifier(delay=0.2)
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
        timeout=0.05,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.SOFT


async def test_M1_ROUTE_023_classifier_exception_is_soft() -> None:
    """A11：UNCERTAIN + classifier 抛异常 → SOFT（任何失败都降级，不抛）。"""
    classifier = StubClassifier(exc=RuntimeError("classifier exploded"))
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.SOFT


async def test_M1_ROUTE_024_classifier_says_unstructured_is_none() -> None:
    """classifier 判非结构化 → NONE(source=llm)。"""
    classifier = StubClassifier(response={"structured": False, "reason": "不是结构化任务"})
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "llm"


async def test_M1_ROUTE_025_classifier_unknown_task_type_is_none() -> None:
    """classifier 返回未知 task_type → NONE（输出不可信，连建议都不给）。"""
    classifier = StubClassifier(
        response={"structured": True, "task_type": "unknown.type", "input": {}}
    )
    decision = await TaskRouter(
        registry=full_registry(),
        classifier=classifier,
    ).decide("列出文件")
    assert decision.strength is RouteStrength.NONE
    assert decision.source == "llm"


# ──────────────────────── 顺序 pin：capture 不被 explore 吞 ────────────────────────


async def test_M1_ROUTE_026_capture_with_explore_word_still_hits() -> None:
    """顺序 pin：'截个图看看' 含'看看'，但'截图'是强枚举动词 → 命中 screen.capture SOFT。
    顺序反了会把'截个图看看'误判成探索。这条钉住"先枚举命中再探索排除"。"""
    decision = await TaskRouter(registry=full_registry()).decide("截个图看看")
    assert decision.strength is RouteStrength.SOFT
    assert decision.task_type == "screen.capture"


# ──────────────────────── 边界 pin：路由层不产出可执行参数 ────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "列出 D:\\Downloads 所有文件",
        "整理 D:\\Downloads",
        "整理 D 盘",
        "截个图",
        "把音量调到 30",
        "先整理 D:\\Downloads，然后截个图给我",
    ],
)
async def test_M1_ROUTE_R027_no_executable_arguments_ever(text: str) -> None:
    """红线：任何输入都不许让路由层产出可执行参数，也不许产生 HARD 决策。

    阶段 0 删掉确定性执行路径之后，这条是"运行时不会替用户提交后台任务"的
    最后一道静态保证 —— 只要 `arguments` 还能被填上，就说明执行路径长回来了。
    """
    decision = await TaskRouter(registry=full_registry()).decide(text)
    assert decision.arguments == {}, f"{text!r} 不应带可执行参数"
    assert decision.strength is not RouteStrength.HARD, f"{text!r} 不该产生 HARD"
