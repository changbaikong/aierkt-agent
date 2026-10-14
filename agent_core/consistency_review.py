"""低风险持久动作的独立一致性复核。

【它补的是哪一格】

网关原有的三道检查各自漏掉同一格：

    参数校验（JSON Schema）  —— 只看类型，以及"字段在不在"，不看内容说不说得通
    权限三态（spec.risk）    —— 只看"谁可以调"，不看"这次调用描述清楚了没有"
    用户点确认（risk=high）  —— 只覆盖高风险那一档

于是"风险低、不需要确认、但会在世界上留下东西"这一格**没有人看**：模型可以不带
任何具体信息就把一个持久动作跑起来 —— 提交一个后台任务、派生一个子代理、发起一次登录。
留下的是活过这次调用的东西，事后却说不清它当初想干什么。

高风险那一档不需要复核（用户已经点过一次"我知道要发生什么"）；`read` 与
`transient` 也不需要（改错了重读一次就有答案）。**触发面就是 `persistent` + `risk="low"`。**

【它怎么判】

按工具名查一张声明式的判据表：这个工具的哪几个参数必须带上、哪几个必须是绝对路径。
答不上来就返回 `码: 字段` 形状的稳定说明，由网关把这次调用拒掉 —— 拒绝不是终点，
模型拿到码之后可以回去问用户要缺的信息。

**已经看过、决定不加判据的工具记进 `REVIEW_WAIVER`。** 这不是装饰：判据表是 fail-open 的，
"这个工具漏了"和"这个工具不需要"在代码里长得一模一样。豁免表把后者写下来，
以后回头看的人才能分清是遗漏还是决定。

【它不做什么】

- **不调模型。** 语义部分（"objective 是不是真的说清了要改什么"）它判不了；
  这里只判结构上说不说得清。别把"看起来像一句话"当成通过。
- **不认识任何工具本身。** 判据全在表里，被复核的工具里一行复核逻辑都没有 ——
  复核跟被复核的东西写在同一处，就等于没复核。
- **不改风险评级、不改审批流程。** 它只在"没人看"的那一格补上一个看的人。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Mapping

from .contracts import EFFECT_PERSISTENT, ActionRequest, ToolSpec, is_blank

#: 复核失败时的稳定错误码。**下游按码分支，不要去解析 error 原文。**
ERROR_MISSING_FIELD = "consistency_missing_field"
ERROR_RELATIVE_PATH = "consistency_relative_path"

#: 复核只覆盖这一格。改这两个值等于改复核的适用范围，不是普通重构。
REVIEWED_EFFECT = EFFECT_PERSISTENT
REVIEWED_RISK = "low"


@dataclass(frozen=True)
class ConsistencyRequirement:
    """一个工具的复核判据。

    声明式：这里只说"要什么"，不说"怎么判" —— 判的过程只有 `review_persistent_action`
    一份实现，加工具时不需要写代码。

    Attributes:
        tool: 工具全名，必须带模块前缀（`module__tool`），与注册表里的名字逐字一致。
        required_arguments: 必须存在、且去掉空白后非空的参数名。
        absolute_path_arguments: 必须是绝对路径的参数名（空值按 required 那边判，不重复报）。
        reason: 这条判据为什么存在（给人看，也是拒绝时让模型知道去哪问用户的依据）。
    """

    tool: str
    required_arguments: tuple[str, ...] = ()
    absolute_path_arguments: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        # 裸名字（没带 `module__` 前缀）在注册表里 resolve 不到，那张判据就**静默不生效**。
        # 这里直接报错，而不是留给运行期"看起来没出问题"。
        if "__" not in self.tool:
            raise ValueError(
                f"一致性复核表里的工具名必须带模块前缀（module__tool），got {self.tool!r}"
            )
        for field in (*self.required_arguments, *self.absolute_path_arguments):
            if not field or not field.strip():
                raise ValueError(f"{self.tool}: 判据里的参数名不能为空")
        if not self.reason.strip():
            raise ValueError(f"{self.tool}: 判据必须写清理由（拒绝时模型要靠它知道该问什么）")


#: **唯一的政策点。** 要放开或收紧某个工具，改这张表，别改代码。
#:
#: `code__agent` 是阶段 2 的工具，现在还不在注册表里 —— 这条是**提前登记的契约**：
#: 工具落地那一天起就自动受管，不给"先上线再补复核"留窗口。
REQUIREMENTS: tuple[ConsistencyRequirement, ...] = (
    ConsistencyRequirement(
        tool="code__agent",
        required_arguments=("workspace_root", "objective"),
        absolute_path_arguments=("workspace_root",),
        reason=(
            "提交代码代理任务必须说清在哪个根目录里改、以及要改什么。"
            "缺了就把请求退回模型，让它去问用户要这一项，而不是替用户挑一个根目录。"
        ),
    ),
)


@dataclass(frozen=True)
class ConsistencyWaiver:
    """「看过这个工具，决定不给它加判据」。

    存在的理由是那张判据表 fail-open：没登记就放行。于是「忘了加」和「决定不加」
    在代码里表现完全一样，事后没人分得清。豁免表把后者显式写下来。

    Attributes:
        tool: 工具全名，必须带模块前缀，与注册表里的名字逐字一致。
        reason: 为什么不需要判据 —— 是「参数形状已由别处覆盖」，还是「这件事没有可
            结构判定的维度」。写清楚，别写「不需要」三个字了事。
    """

    tool: str
    reason: str

    def __post_init__(self) -> None:
        if "__" not in self.tool:
            raise ValueError(
                f"豁免表里的工具名必须带模块前缀（module__tool），got {self.tool!r}"
            )
        if not self.reason.strip():
            raise ValueError(f"{self.tool}: 豁免必须写清理由，否则和「漏了」分不开")


#: 已复核、决定**不增加额外语义规则**的低风险持久工具。
#:
#: 这张表和 `REQUIREMENTS` 互斥、合起来覆盖全部现存低风险持久工具，
#: 由 `tests/test_consistency_review.py` 里那条覆盖面快照用例守着。
REVIEW_WAIVER: tuple[ConsistencyWaiver, ...] = (
    ConsistencyWaiver(
        tool="netease__login",
        reason=(
            "登录只需账号与渠道，两个参数都在 Schema 的 required 里、并由统一的"
            "非空白检查兜住。没有「少给一项就得退回问用户」的维度：账号错了是一次"
            "失败回执，不是一次说不清的持久动作。"
        ),
    ),
    ConsistencyWaiver(
        tool="media__submit_tts",
        reason=(
            "唯一必填项是 text，Schema 里已声明 required + minLength，"
            "纯空白由统一的非空白检查拦下。没有跨字段关系可判。"
        ),
    ),
    ConsistencyWaiver(
        tool="subagent__dispatch",
        reason=(
            "唯一必填项是 task，同上。这个工具**看起来**像缺一项就该拦，"
            "但它派出去的子代理自己会走一遍网关，子代理要调的工具在那里被复核 ——"
            "在这里再判一次是重复，不是补漏。"
        ),
    ),
    ConsistencyWaiver(
        tool="work__submit",
        reason=(
            "它确实有跨字段契约（哪种任务必须带哪些输入），但**不放在这张表里**："
            "契约按任务类型而变，属 Work 领域，见 `work_provider.WORK_TASK_INPUT_REQUIREMENTS`；"
            "执行由工具级 `ToolSpec.argument_validator` 在进 WorkHost 之前完成。"
            "放到这张通用表里就等于让复核器认识所有 Work 任务类型。"
        ),
    ),
)


def _table() -> dict[str, ConsistencyRequirement]:
    """把判据表摊成字典，顺带挡住重复的工具名。

    重复的名字意味着"两条判据里有一条永远不会被读到"，而读不到的那条不会报错 ——
    只会在某天有人发现复核没生效时，回头查到这里。
    """
    table: dict[str, ConsistencyRequirement] = {}
    for item in REQUIREMENTS:
        if item.tool in table:
            raise ValueError(f"一致性复核表里有重复的工具名：{item.tool}")
        table[item.tool] = item
    return table


def _blank(value: Any) -> bool:
    """空值判据：非字符串、空串、纯空白，都算"没给"。

    直接转发 `contracts.is_blank` —— 网关那条必填字符串检查用的是同一份实现。
    在这里另写一遍 `not value.strip()` 的后果是：`"  "` 在这张表里算没给、
    在网关那里算给了（或反过来），而两只在纯空白输入上分叉，最容易被漏测。
    """
    return is_blank(value)


def _is_absolute(value: str) -> bool:
    """绝对路径判据。

    用 `PurePath` 而不是 `Path`：这里只判语义，不碰文件系统（不 create、不 resolve、
    不 stat）—— 复核阶段去访问磁盘会把"参数问题"变成"环境问题"，也让这个函数不再纯粹。

    Windows 上 `PurePath(r"D:/a")` 与 `PurePath("//srv/share")` 为真，`PurePath("./a")`
    与 `PurePath("/a")` 为假（没有盘符）。最后一种正是要拦的：`/a` 看起来像绝对路径，
    在这台机器上却会被解释成当前盘根目录下的 `a`。
    """
    return PurePath(value).is_absolute()


def review_persistent_action(request: ActionRequest, spec: ToolSpec) -> str:
    """复核一次即将执行的调用。通过返回空串，不通过返回 `码: 字段`。

    返回空串表示"没意见"，与 `ActionGateway._validate_arguments` 的形状一致，
    网关那一处不用为它写第二套分支。
    """
    if spec.effect != REVIEWED_EFFECT or spec.risk != REVIEWED_RISK:
        return ""
    requirement = _table().get(spec.name)
    if requirement is None:
        # 表里没登记就得放行。**这里是 fail-open，是刻意的**：复核是新增的一层，
        # 让"没登记"变成拒绝，会把所有现存低风险持久工具当场堵死 ——
        # 那是一次静默的生产事故，不是一次安全加固。
        # 覆盖面的守卫不在这里，在 tests/test_consistency_review.py 里那条
        # "低风险持久工具恰好是这几个"的快照用例上：新工具出现时它会红，
        # 逼着人明确决定要不要给它加判据。
        return ""

    arguments: Mapping[str, Any] = (
        request.arguments if isinstance(request.arguments, Mapping) else {}
    )
    for field in requirement.required_arguments:
        if _blank(arguments.get(field)):
            return f"{ERROR_MISSING_FIELD}: {field}"
    for field in requirement.absolute_path_arguments:
        value = arguments.get(field)
        if not _blank(value) and not _is_absolute(value):
            return f"{ERROR_RELATIVE_PATH}: {field}"
    return ""
