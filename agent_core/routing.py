"""主 Chat 的任务形态路由：决定一条请求走 direct / subagent / work_host。

为什么要有这一层
------------------
在此之前，三条路径（`AgentLoop` 直接调工具 / `subagent__dispatch` / `work__submit`）
在 registry 里是平级的，选哪条完全靠模型读 prompt 自己猜。日志里查不到"为什么走了
这条路"，测试也没法断言。本模块把"该走哪条"变成一个可测试、可观测的纯函数。

判据不是关键词命中
------------------
最容易写错的做法是"句子里有『文件夹』就扔给 WorkHost"。那样"帮我看看这个文件夹"
会被硬拦——但用户只是在探索，硬拦会直接提交一个后台任务，这不是他想要的。

真正的判据是：**这句话是否明确描述了可枚举、可验证的结构化操作**。三个维度：

  ① 动词是枚举类（列出 / 统计 / 整理 / 截图 / 调音量），不是探索类（看看 / 检查一下）
  ② 动作有受力对象（哪怕只是指代词）
  ③ 产出可枚举，或动作本身参数化（"按扩展名统计" / "调到 30"）

并且 **先判有没有强枚举动词，再判是不是探索**——顺序反了会把"截个图看看"漏掉。

确定性执行的终点（2026-09-23，code-agent 计划阶段 0）
------------------------------------------------------
`work__submit` 的 `hard_routable` 标记已随确定性直接执行路径一并删除：
规则命中、LLM 补参，**最多只产生 SOFT 建议**，执行与否由主模型决定。
理由：自主代码代理上线后，"该不该进 WorkHost"是一个需要模型判断的决策
（direct / Work / 计划 / 子代理），不是一句正则能裁决的事；
而且硬拦路径不经过模型，等于替用户发起了持久动作（docs/code-agent-plan.md）。

`RouteStrength.HARD` 枚举值**保留**：路由账本里有存量 "hard" 记录，
删枚举会让读历史记录的代码炸掉；它只是不再被产生。

越权预检保留在建议路径上（`_out_of_roots`）：明知目录不在允许范围内，
还让模型去提交一次注定失败的任务，只会换来一句兑现不了的"交给后台了"。

同理，**提交后仍要用户确认**的任务（截屏 / 调音量）的建议会带"先确认再提交"
的措辞（`_confirm_first`）：提交本身就占了一个后台位，用户没点头之前
连"发起"都不该发生。

必填参数抽不出来就只给"缺参数"的建议：`file.list` / `file.organize`
没有 `input.directory` 会直接 failed，提示模型先补全或追问用户。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

# 工具名常量：路由层靠它们探测"这条路径当前到底存不存在"。
#
# 全部必须是**命名空间形态**：模块注册时会拼成 `模块__工具`，
# 少了前缀就永远 resolve 不到 —— 2026-09-21 修过这个 bug：
# PLAN_SUBMIT_TOOL 曾是 "plan_submit"，导致
# 「多步 + 依赖 → 建议计划编排」那条软路由静默失效，
# 带顺序依赖的多步目标被降级成单步提交。
# 守门：tests/test_routing_real_registry.py（用真实 Work 模块构建注册表）。
WORK_SUBMIT_TOOL = "work__submit"
PLAN_SUBMIT_TOOL = "work__plan_submit"
SUBAGENT_TOOL = "subagent__dispatch"

# 落库/展示用的字符串，与 RoutePath 一一对应。
MAX_REASON_CHARS = 200


class RoutePath(str, Enum):
    """三条执行路径。"""

    DIRECT = "direct"
    SUBAGENT = "subagent"
    WORK_HOST = "work_host"


class RouteStrength(str, Enum):
    """路由决定的强度。

    HARD: 运行时直接执行，不问模型。（**已停用**：2026-09-23 阶段 0 起不再产生，
        保留枚举值只为读账本里的存量 "hard" 记录。）
    SOFT: 只注入建议文本，模型保留全部工具，不会剥夺任何能力。
    NONE: 交模型自主决定，不注入任何建议。
    """

    HARD = "hard"
    SOFT = "soft"
    NONE = "none"


@dataclass(frozen=True)
class RouteDecision:
    """一次路由决策的完整结果。

    Attributes:
        path:       走哪条路径。
        strength:   SOFT / NONE（HARD 已停用，见 RouteStrength）。
        reason:     人类可读的选择原因，直接进日志和落库表，用于判断误判。
        source:     "rule"（规则命中）/ "llm"（后备分类器）/ "fallback"（降级）。
        confidence: 0~1，规则命中给高值，LLM 给的值按分类器的置信度填。
        task_type:  命中 WorkHost 任务类型时给出，取值来自 WORK_TASK_TYPES。
        arguments:  预留字段。确定性执行删除后恒为空；保留是因为账本结构
                    与 `as_record_fields` 的消费方仍在，阶段 4 清理旧路由时一并处理。
        hint:       SOFT 时注入 prompt 的建议文本。
    """

    path: RoutePath
    strength: RouteStrength
    reason: str
    source: str = "rule"
    confidence: float = 0.0
    task_type: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    hint: str = ""

    def as_record_fields(self) -> dict[str, Any]:
        """落成库表字段（不含 run_id / 回填字段，那些由调用方补）。"""
        return {
            "decided_path": self.path.value,
            "decided_strength": self.strength.value,
            "reason": self.reason[:MAX_REASON_CHARS],
            "source": self.source,
            "task_type": self.task_type,
        }


@dataclass(frozen=True)
class RouteRecord:
    """落库用的一条路由记录。

    `decided_*` 是路由层的判断，`actual_path` / `final_status` 是回合结束后回填的事实。
    两者不一致 = 一次误判，这是衡量路由质量的唯一硬指标。
    """

    run_id: str
    conversation_id: str = ""
    user_text: str = ""
    decided_path: str = RoutePath.DIRECT.value
    decided_strength: str = RouteStrength.NONE.value
    reason: str = ""
    source: str = "rule"
    task_type: str | None = None
    task_id: str | None = None
    actual_path: str | None = None
    final_status: str | None = None
    routing_mode: str = "legacy"

    @property
    def misrouted(self) -> bool:
        """建议路径与实际路径是否不一致（只对 SOFT 有意义）。

        HARD 的判断标准不同：它已经替模型做了执行，所以看的是最终状态是否失败。
        """
        if self.decided_strength == RouteStrength.NONE.value:
            return False
        if self.actual_path is None:
            return False
        return self.decided_path != self.actual_path


class RouteClassifier(Protocol):
    """LLM 后备分类器的接口。

    只在规则层给出 UNCERTAIN 时才调用——绝大多数请求不会走到这里，
    因此正常对话**不产生任何额外 LLM 调用**。
    """

    async def __call__(
        self,
        user_text: str,
        *,
        candidates: Sequence[str],
    ) -> Mapping[str, Any] | None:
        """返回 None 表示放弃；否则返回含 structured / task_type / input / reason 的映射。"""


# ────────────────────────────── 规则层 ──────────────────────────────
# 设计原则：先探索排除，再枚举命中。写反了会把"截个图看看"当成探索漏掉。

# 探索类动词：命中它们 = 用户在看，不是在要一个可枚举的产出。
_EXPLORE_PATTERN = re.compile(
    r"(?:帮我|给我|替我|麻烦|我想)?\s*"
    r"(?:看一下|看下|看看|瞧瞧|瞅瞅|瞅一下|打开看看|查看一下|检查一下|查查|查一下|瞅一眼|看一眼)"
)

# 研究/探索类：需要多步探查或连续推理，交给 subagent 更合适。
#
# 这里最忌讳写宽：`(?:...)?个?文件` 这种写法会把"这个文件夹"也匹配进来，
# 于是"帮我看看这个文件夹"被误判成研究任务。所以只认两种信号——
# 明确的研读动词，或者"这几个文件"这种**多文件**指代（单个"文件"不算）。
_RESEARCH_PATTERN = re.compile(
    r"(?:分析|研究|调研|对比|比较|总结|梳理|归纳|评估|review)"
    r"|(?:读|看)(?:一下|一遍|一通)?(?:这|那)?(?:几|多)个(?:文件|文档)"
    r"|(?:这|那)?(?:几|多)个(?:文件|文档).{0,12}(?:分析|对比|比较|总结|都|一起)"
)

# 序列标记：多步且带依赖的信号。
_SEQUENCE_MARKERS = ("先", "然后", "接着", "再", "之后", "随后", "最后", "做完", "完了", "紧接着")

# 枚举类动词（按 task_type 分组）。这些词本身自带"产出可枚举"的语义。
_LIST_VERBS = re.compile(r"(?:列出|列一下|列个|罗列|清单|统计|清点|数一下|数数|盘点|列出来)")
_ORGANIZE_VERBS = re.compile(r"(?:整理|整理一下|归类|归拢|收拾|分类整理|归置)")
_CAPTURE_VERBS = re.compile(r"(?:截个图|截屏|截图|截一张|截个屏|抓个屏|屏幕截图|截一下屏)")
_VOLUME_CONTEXT = re.compile(r"(?:音量|声音|系统音量|音量大小|声量)")
_VOLUME_ACTION = re.compile(r"(?:调到|调成|调为|调至|设置为|设为|设定为|设定|升到|降到|改到|改成|调)")

# "网易云音量" 和 "系统音量" 是**两个不同的东西**，走的是两条完全不同的工具：
# 前者是网易云应用自身音量（netease__set_volume），后者是整机音量（WorkHost 的
# system.volume.adjust）。句子点名了播放器还说 system.volume.adjust，
# 用户会为了一个他没要的操作去点一次确认 —— 确认一次改错地方，比不做更糟。
# 所以这类句子直接从系统音量规则里排除，留给模型按音乐工具处理。
_APP_VOLUME_CONTEXT = re.compile(r"(?:网易云|云音乐|播放器|音乐播放|音乐)")

# 路径抽取：Windows 盘符路径 / POSIX 绝对路径 / 引号包裹 / 单独的"D 盘"。
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/](?:[^\\/:*?\"<>|\r\n]+[\\/])*[^\\/:*?\"<>|\r\n]*")
_POSIX_PATH = re.compile(r"/(?:[^\s/]+/)*[^\s/]*")
# 路径后面的中文噪声："C:\a\b 里的文件" 会连着中文一起被上面的正则吃掉。
# 只砍"空格 + 非 ASCII"这一段——中文路径（"C:\用户\文档"）和带空格的英文路径
# （"C:\Program Files\x"）都不含这种尾巴，不会被误伤。
_TRAILING_NOISE = re.compile(r"\s+[^\x00-\x7F]*$")
_QUOTED = re.compile(r"[「『\"'“”]([^」』\"'“”]{1,200})[」』\"'“”]")
_DRIVE_ONLY = re.compile(r"\b([A-Za-z])\s*[盘驱动器]")

# 音量数值：0-100，允许 %/％ 后缀。
_VOLUME_VALUE = re.compile(r"(\d{1,3})\s*(?:%|％| percent)?")

def _required_input(task_type: str) -> tuple[str, ...]:
    """读 Work 领域的"这个 task_type 需要哪些 input 字段"。

    表本身住在 `agent_core/work_provider.py`（`WORK_TASK_INPUT_REQUIREMENTS`，
    唯一来源），路由层**只是读者**。它拿这个事实判断"这句话够不够格生成一条建议"，
    判出来的仍然只是建议 —— 路由层没有执行路径，"缺参数"在这里的后果是
    把建议降级并要求模型去问用户，不是拦下一次提交
    （拦提交是 `work__submit` 的结构校验与 `WorkHost.submit` 的事）。

    惰性导入的理由同 `_is_known_task_type`：不在模块导入期把两个模块的
    导入顺序绑在一起。
    """
    from .work_provider import WORK_TASK_INPUT_REQUIREMENTS

    return WORK_TASK_INPUT_REQUIREMENTS.get(task_type, ())


def _extract_directory(text: str) -> str | None:
    """从句子里抠出目录。拿不到就返回 None —— 调用方必须据此降级，不许猜。"""
    match = _WIN_PATH.search(text)
    if match:
        raw = match.group(0).strip()
        cleaned = _TRAILING_NOISE.sub("", raw).strip()
        return (cleaned.rstrip("\\/") or raw.rstrip("\\/") or raw)
    match = _POSIX_PATH.search(text)
    if match and len(match.group(0)) > 1:
        raw = match.group(0)
        cleaned = _TRAILING_NOISE.sub("", raw).strip()
        return (cleaned.rstrip("/") or raw.rstrip("/") or "/")
    match = _QUOTED.search(text)
    if match:
        return match.group(1).strip()
    match = _DRIVE_ONLY.search(text)
    if match:
        # "整理 D 盘" → "D:\"，补成合法根路径
        return f"{match.group(1).upper()}:\\"
    return None


def _extract_percent(text: str) -> float | None:
    """抠出音量百分比。超出 0-100 视为抽错，返回 None（不硬拦）。"""
    match = _VOLUME_VALUE.search(text)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= value <= 100.0:
        return None
    return value


class _RuleOutcome(str, Enum):
    """单条规则的判定结果。"""

    MISS = "miss"          # 这条规则跟本句无关
    UNCERTAIN = "uncertain"  # 像是这条规则，但缺必填参数 → 可能触发 LLM 后备
    MATCH = "match"        # 可以硬拦


@dataclass(frozen=True)
class _Rule:
    """一条结构化任务规则。

    `trigger` 用 callable 而不是直接存 `re.Pattern`：音量这类规则要同时满足
    "有音量上下文" 和 "有调整动作" 两个条件，正则塞不下，用函数更直白。
    """

    task_type: str
    trigger: Callable[[str], bool]
    extractor: Callable[[str], dict[str, Any] | None]
    note: str
    confirm_first: bool = False
    # confirm_first=True：建议里必须带"先向用户确认，再提交"的措辞。
    # 用在"提交后必须等用户确认"的任务上（截屏 / 调音量）—— 它们从 chat 来源
    # 提交时 Host 会打上 _requires_approval，替用户走完"发起"这一步
    # 等于把"批准"甩给一个对话里没有入口的用户。
    # （旧的 `hard` 字段随确定性执行路径一并删除：现在没有任何规则
    # 能让运行时替模型提交。）

    def evaluate(self, text: str) -> tuple[_RuleOutcome, dict[str, Any]]:
        if not self.trigger(text):
            return _RuleOutcome.MISS, {}
        values = self.extractor(text) or {}
        required = _required_input(self.task_type)
        if all(values.get(key) not in (None, "") for key in required):
            return _RuleOutcome.MATCH, values
        return _RuleOutcome.UNCERTAIN, values


def _volume_trigger(text: str) -> bool:
    """音量规则的特殊触发：必须有音量上下文 **且** 有调整动作。

    再排除"点名播放器"的句子（见 _APP_VOLUME_CONTEXT）：那是应用音量，
    不是整机音量。
    """
    if _APP_VOLUME_CONTEXT.search(text) is not None:
        return False
    return _VOLUME_CONTEXT.search(text) is not None and _VOLUME_ACTION.search(text) is not None


_RULES: tuple[_Rule, ...] = (
    _Rule(
        task_type="screen.capture",
        trigger=lambda text: _CAPTURE_VERBS.search(text) is not None,
        extractor=lambda text: {},
        note="截图：无必填参数，但提交后必须用户确认，建议里要求先问一句",
        confirm_first=True,
    ),
    _Rule(
        task_type="system.volume.adjust",
        trigger=_volume_trigger,
        extractor=lambda text: (
            {"percent": percent} if (percent := _extract_percent(text)) is not None else {}
        ),
        note="音量：需要音量上下文 + 调到/设为 + 0-100 的数值，确认后才会真的改",
        confirm_first=True,
    ),
    _Rule(
        task_type="file.organize",
        trigger=lambda text: _ORGANIZE_VERBS.search(text) is not None,
        extractor=lambda text: (
            {"directory": directory} if (directory := _extract_directory(text)) is not None else {}
        ),
        note="整理：动词命中就算相关，但缺目录只能给 UNCERTAIN",
    ),
    _Rule(
        task_type="file.list",
        trigger=lambda text: _LIST_VERBS.search(text) is not None,
        extractor=lambda text: (
            {"directory": directory} if (directory := _extract_directory(text)) is not None else {}
        ),
        note="列举：动词自带产出语义，但缺目录只能给 UNCERTAIN",
    ),
)


def _detect_task_types(text: str) -> tuple[str, ...]:
    """粗扫句子里出现了哪些 task_type 的信号（不看参数是否齐全）。"""
    found: list[str] = []
    if _CAPTURE_VERBS.search(text) is not None:
        found.append("screen.capture")
    if _volume_trigger(text):
        found.append("system.volume.adjust")
    if _ORGANIZE_VERBS.search(text) is not None:
        found.append("file.organize")
    if _LIST_VERBS.search(text) is not None:
        found.append("file.list")
    return tuple(found)


def _has_sequence_marker(text: str) -> bool:
    return any(marker in text for marker in _SEQUENCE_MARKERS)


def _is_exploratory(text: str) -> bool:
    """是否只是"看看"，没有要任何可枚举的产出。

    调用前必须确认本句**没有**命中任何强枚举动词，否则"截个图看看"会被误判成探索。
    """
    return _EXPLORE_PATTERN.search(text) is not None


def _none(reason: str, *, source: str = "rule") -> RouteDecision:
    return RouteDecision(
        path=RoutePath.DIRECT,
        strength=RouteStrength.NONE,
        reason=reason,
        source=source,
    )


def _is_known_task_type(value: Any) -> bool:
    from .work_provider import WORK_TASK_TYPES  # 惰性导入，避免模块级循环依赖

    return isinstance(value, str) and value in WORK_TASK_TYPES


# ────────────────────────────── 路由器 ──────────────────────────────


class TaskRouter:
    """把用户的一句话映射成一条执行路径。

    用法::

        router = TaskRouter(registry=registry)
        decision = await router.decide("整理 D:\\Downloads")
        if decision.hint:
            ...  # 把建议注入 prompt；执行与否由模型决定

    2026-09-23（阶段 0）起本模块**只建议、不执行**：确定性直接执行路径
    已删除，`decision.strength` 只会是 SOFT 或 NONE。

    整个 decide 不抛异常：任何异常都被吞掉并降级为 NONE，
    因为路由是**旁路**，它挂了不应该让主 Chat 挂掉。
    """

    def __init__(
        self,
        *,
        registry: Any = None,
        classifier: RouteClassifier | None = None,
        timeout: float = 3.0,
        enabled: bool = True,
        allowed_roots: Sequence[str | Path] | None = None,
    ) -> None:
        self.registry = registry
        self.classifier = classifier
        self.timeout = timeout
        self.enabled = enabled
        # 允许操作的目录根。空元组 = 不做预检（调用方没给，就不能据此拒绝）。
        # 有了它，"列出 D:\Downloads" 这种越权请求会在**提交之前**就被降级，
        # 而不是等 provider 在执行期抛 PermissionError 让任务静默失败。
        self.allowed_roots: tuple[Path, ...] = tuple(
            Path(str(item)).expanduser().resolve() for item in (allowed_roots or ())
        )

    def _directory_allowed(self, directory: Any) -> bool:
        """目录是否在允许的根内。规则与 FileWorkService._safe_path 保持一致。"""
        if not self.allowed_roots or not isinstance(directory, (str, Path)) or not str(directory).strip():
            return True
        try:
            candidate = Path(str(directory)).expanduser()
            if candidate.is_symlink():
                return False
            resolved = candidate.resolve(strict=False)
        except (OSError, ValueError):
            return False
        return any(root == resolved or root in resolved.parents for root in self.allowed_roots)

    async def decide(self, user_text: str) -> RouteDecision:
        """判定一句话该走哪条路。永远不会抛异常。"""
        try:
            return await self._decide(user_text)
        except Exception as exc:  # 路由是旁路，挂了必须静默降级
            logger.warning("route decision failed, falling back to direct: %s", exc)
            return _none(f"router error: {type(exc).__name__}", source="fallback")

    async def _decide(self, user_text: str) -> RouteDecision:
        text = str(user_text or "").strip()
        if not self.enabled:
            return _none("router disabled", source="fallback")
        if not text:
            return _none("empty input")

        # 能力探测：路径不存在就别建议它。这条保证"路由工具不可用"时行为明确，
        # 而不是给模型指一条走不通的路。
        has_work_submit = self._has_tool(WORK_SUBMIT_TOOL)
        has_plan_submit = self._has_tool(PLAN_SUBMIT_TOOL)
        has_subagent = self._has_tool(SUBAGENT_TOOL)
        if not has_work_submit and not has_plan_submit:
            return _none("WorkHost 工具未注册，路由降级", source="fallback")

        types = _detect_task_types(text)

        # ① 多步 + 依赖 → 建议 plan_submit。
        #    硬拦在这里是**禁止**的：plan_submit 是高风险工具，必须走主 loop 的确认。
        if len(types) >= 2 and _has_sequence_marker(text) and has_plan_submit:
            return RouteDecision(
                path=RoutePath.WORK_HOST,
                strength=RouteStrength.SOFT,
                reason=f"多步且带依赖（{'/'.join(types)}），需要跨步骤编排",
                confidence=0.7,
                hint=(
                    f"这个目标包含多个有先后顺序的步骤，用 {PLAN_SUBMIT_TOOL} 编排成计划，"
                    "不要拆成多次独立提交（计划会先给用户确认再执行）。"
                ),
            )

        # ② 完全没有结构化信号：探索类直接 NONE；研究类建议 subagent。
        if not types:
            if has_subagent and _RESEARCH_PATTERN.search(text) is not None:
                return RouteDecision(
                    path=RoutePath.SUBAGENT,
                    strength=RouteStrength.SOFT,
                    reason="需要多步探查或连续推理",
                    confidence=0.5,
                    hint=(
                        f"这个任务需要多步探查，交给 {SUBAGENT_TOOL} 跑，"
                        "只把结论带回主对话，避免来回试探刷屏。"
                    ),
                )
            if _is_exploratory(text):
                return _none("探索性请求，没有可枚举的产出，交给主 Chat")
            return _none("未命中任何结构化规则")

        # ③ 走到这里说明有强枚举动词（types 非空），直接逐条规则试。
        #    探索判断已经在 ② 做过了：types 为空才会被"看看"拦下，
        #    所以"截个图看看"不会被误判成探索——这正是顺序要对的原因。
        if not has_work_submit:
            return _none("work__submit 未注册，无法给出提交建议", source="fallback")

        uncertain: tuple[_Rule, dict[str, Any]] | None = None
        for rule in _RULES:
            outcome, values = rule.evaluate(text)
            if outcome is _RuleOutcome.MATCH:
                # 越权预检排在建议之前：明知会失败还建议模型提交一次，
                # 换来的只是用户在对话里看到一句已经承诺过的"交给后台了"。
                if not self._directory_allowed(values.get("directory")):
                    return self._out_of_roots(rule, values)
                return self._suggest(rule, values)
            if outcome is _RuleOutcome.UNCERTAIN and uncertain is None:
                uncertain = (rule, values)

        # ④ 规则拿不定 → 交给 LLM 后备。分类器不可用/超时/失败一律降级。
        if uncertain is not None and self.classifier is not None:
            llm = await self._ask_classifier(text, uncertain[0])
            if llm is not None:
                return llm
            return RouteDecision(
                path=RoutePath.WORK_HOST,
                strength=RouteStrength.SOFT,
                reason=f"疑似 {uncertain[0].task_type}，但缺少必填参数（{', '.join(_required_input(uncertain[0].task_type)) or '无'}）",
                confidence=0.4,
                task_type=uncertain[0].task_type,
                hint=(
                    "这像是结构化任务，但还缺关键信息（比如具体目录）。"
                    "先向用户确认缺失的信息，再调用 work__submit。"
                ),
            )

        if uncertain is not None:
            return _none(f"疑似 {uncertain[0].task_type} 但缺少必填参数，交模型补全")

        return _none("未命中任何结构化规则")

    # ── 内部实现 ──

    def _has_tool(self, name: str) -> bool:
        if self.registry is None:
            return False
        try:
            return self.registry.resolve(name) is not None
        except Exception:
            return False

    def _out_of_roots(self, rule: _Rule, values: dict[str, Any], *, source: str = "rule") -> RouteDecision:
        """抽到了目录但它在允许范围之外：不硬拦，也不让它白白提交一次。

        `source` 由调用方给：规则路径是 `rule`，模型补参路径是 `llm`。
        降级本身不该抹掉"这个参数是模型补出来的"这个事实——否则统计上会把
        模型改坏的结果记成规则命中，出了问题从账本上看不出来。
        """
        directory = str(values.get("directory") or "")
        allowed = ", ".join(str(root) for root in self.allowed_roots) or "（未配置）"
        return RouteDecision(
            path=RoutePath.DIRECT,
            strength=RouteStrength.SOFT,
            reason=f"目录不在允许范围内（task_type={rule.task_type}），已降级",
            source=source,
            confidence=0.95,
            task_type=rule.task_type,
            hint=(
                f"用户要操作的目录「{directory}」不在允许的文件根内（当前只允许：{allowed}）。\n"
                "不要调用 work__submit —— 它一定会失败。直接告诉用户这个访问不到，请他换一个目录。"
            ),
        )

    def _confirm_first(self, rule: _Rule, *, source: str = "rule") -> RouteDecision:
        """命中但需要用户确认才能执行：给建议，不替用户走完确认这一步。

        `source` 同上：模型补参路径传 `llm`。
        """
        # 提示词里**不许**再提"调用某个工具来批准"。
        #
        # 以前这里会探一下 `work__approve` 在不在，在的话就让模型
        # "提交完再调它请求确认"。那个工具 2026-09-22 从模型面删掉了 ——
        # 模型替用户批准高风险操作，等于让两道闸门互相抵消。
        # 审批现在只能由用户走统一审批控制面（`/api/approvals`），
        # 模型能做的只有"先问一句、拿到同意再提交"。
        hint = (
            "这个操作会真的改动系统状态，必须先拿到用户确认。\n"
            "先向用户确认，再调用 work__submit 提交。"
            "不要自己假定用户已经同意 —— 提交之后仍会停下来等他点头。"
        )
        return RouteDecision(
            path=RoutePath.WORK_HOST,
            strength=RouteStrength.SOFT,
            reason=f"{rule.task_type} 提交后仍需用户确认，交模型发起",
            source=source,
            confidence=0.8,
            task_type=rule.task_type,
            hint=hint,
        )

    def _suggest(
        self,
        rule: _Rule,
        values: dict[str, Any],
        *,
        source: str = "rule",
        confidence: float = 0.9,
    ) -> RouteDecision:
        """命中且参数齐全：给一条**带现成参数**的 SOFT 提交建议。

        抽好的参数放进建议文本（而不是 `arguments`）：模型看得见、可以核对，
        也可以推翻——最终调不调 `work__submit`、参数照不照抄，都是模型的判断。
        目录已经过 `allowed_roots` 预检，参数本身仍要过网关的 JSON Schema 校验。

        `confirm_first=True` 的规则走 `_confirm_first`：这类任务提交后
        Host 会挂审批，建议里必须先让模型问用户。
        """
        if rule.confirm_first:
            return self._confirm_first(rule, source=source)
        task_input = {key: value for key, value in values.items() if value not in (None, "")}
        params = json.dumps(task_input, ensure_ascii=False) if task_input else "（本任务无必填参数）"
        hint = (
            f"这看起来是结构化任务（task_type={rule.task_type}），参数已抽取：input={params}。\n"
            f"如果与用户意图一致，直接调用 work__submit 提交（objective 用用户的原话概括）；"
            f"参数有出入就先向用户确认，不要猜。"
        )
        return RouteDecision(
            path=RoutePath.WORK_HOST,
            strength=RouteStrength.SOFT,
            reason=f"{rule.note}（task_type={rule.task_type}），建议提交",
            source=source,
            confidence=confidence,
            task_type=rule.task_type,
            hint=hint,
        )

    async def _ask_classifier(self, text: str, rule: _Rule) -> RouteDecision | None:
        """调 LLM 后备分类器补**当前命中规则**缺的参数。任何失败都返回 None，由调用方降级。

        分类器只能补齐 `rule.task_type` 的入参，不能换任务类型、不能绕过 allowed_roots。
        三道检查（必填参数 → 目录范围 → 确认措辞）与规则命中路径完全一致，
        直接复用 `_out_of_roots` / `_suggest`（内含 `_confirm_first` 分支）。

        候选里**只放命中的那一个 task_type**：把全部任务清单交给它，等于请它挑一条
        别的规则执行，而它的职责只是补参数。范围收窄后，"换类型"的检查也从
        "事后不信任" 变成 "它根本没得选"。
        """
        candidates = (rule.task_type,)
        try:
            raw = await asyncio.wait_for(
                self.classifier(text, candidates=candidates),  # type: ignore[misc]
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("route classifier timed out after %.1fs", self.timeout)
            return None
        except Exception as exc:
            logger.warning("route classifier failed: %s", exc)
            return None
        if not raw:
            return None
        if not bool(raw.get("structured")):
            return _none("LLM 判定为非结构化任务", source="llm")

        task_type = raw.get("task_type") or rule.task_type
        if task_type != rule.task_type:
            # 分类器想换一条规则执行 —— 后备只能补参数，不能改写命中的任务类型。
            return _none(f"LLM 返回与命中规则不一致的 task_type：{task_type}，已忽略", source="llm")
        if not _is_known_task_type(task_type):
            # 分类器点了一个 Host 不认识的类型 —— 输出不可信，
            # 这时候连"建议"都不该给，直接当没说过。
            return _none(f"LLM 返回未知 task_type：{task_type}，已忽略", source="llm")
        values = dict(raw.get("input") or {})
        required = _required_input(task_type)
        if not all(values.get(key) not in (None, "") for key in required):
            # LLM 说是结构化任务，但参数还是抽不出来 —— 只能建议模型去问用户。
            return RouteDecision(
                path=RoutePath.WORK_HOST,
                strength=RouteStrength.SOFT,
                reason=f"LLM 判定为 {task_type}，但缺少必填参数",
                source="llm",
                confidence=0.5,
                task_type=task_type,
                hint=(
                    "这像是结构化任务，但还缺关键信息（比如具体目录）。"
                    "先向用户确认缺失的信息，再调用 work__submit。"
                ),
            )
        # 参数补齐后，仍要重跑规则命中路径的越权与确认预检：
        # LLM 补出的目录不享有豁免，confirm_first 的规则建议里照样要求先问用户。
        # 降级路径必须带 source="llm"：重要的是"这一轮的参数是模型补的"
        # 要如实记进账本，统计上才分得清规则命中与模型补参。
        if not self._directory_allowed(values.get("directory")):
            return self._out_of_roots(rule, values, source="llm")
        return self._suggest(
            rule, values, source="llm", confidence=float(raw.get("confidence") or 0.7)
        )
