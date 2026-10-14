"""
核心数据契约 —— 业务中立的数据结构定义。

【这个文件在全系统里的位置】
它是整个 agent_core 的**词汇表**：所有模块之间传的都是这里定义的结构体。
同一份结构既被 CLI、Web 两个入口用，也被工具层、记忆层、任务层用，
所以这里**故意不含任何业务逻辑**（没有 HTTP、没有模型厂商、没有 SQL）。

【阅读顺序建议】
先读 Scope（身份边界）→ IngressContext（认证入口怎么产生 Scope）
→ ToolSpec/ToolCall/ToolResult（一次工具调用的三段式）
→ AgentRequest/AgentOutcome/RunSnapshot（一次对话回合的输入、输出、可恢复快照）
→ ConfirmationRequest（高风险操作的人工闸门）。

【为什么需要这些"容器"】
就像图书馆借书：
- Scope          = 借书卡（标识你是谁、在哪个分馆、借的是哪本书）
- RuntimeLock    = 借书记录（防止同一本书被重复借走）
- RunSnapshot    = 归还凭证（记录借书的完整过程，断线可续）
- ConfirmationRequest = 需要管理员盖章的特殊借阅
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping


# 工具处理函数的统一签名：收到 (参数字典, 作用域)，返回结果字典。
# 允许返回 awaitable，所以同步函数和 async 函数都能直接当 handler 注册。
ToolHandler = Callable[[dict[str, Any], "Scope"], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


@dataclass(frozen=True)
class Scope:
    """
    作用域 - 用户状态和 Agent 运行的身份边界

    Scope 用于隔离不同用户、不同对话的数据，确保：
    1. 用户 A 看不到用户 B 的对话历史
    2. Web 渠道的对话不会和 CLI 渠道混在一起
    3. 每个对话都有独立的记忆和上下文

    为什么需要 Scope？
    就像公司的文件柜：
    - owner_id = 部门名称（销售部的文件和技术部的文件分开）
    - channel = 文件柜编号（1号柜和2号柜分开）
    - conversation_id = 抽屉编号（每个抽屉存不同项目的文件）
    - module_id = 文件类型（合同文件和技术文档分开）

    四元组 (owner_id, channel, conversation_id, module_id) 是**完整身份**，
    全系统的隔离（runtime 状态、确认记录、hot 记忆、任务账本）都按它做。

    Attributes:
        owner_id: 所有者 ID（用户账号、租户 ID 等）
        channel: 渠道标识（"desktop", "web", "slack" 等）
        conversation_id: 对话 ID（唯一标识一次对话）
        module_id: 模块 ID（"companion", "task", "media" 等）

    注意：frozen=True 表示不可变，创建后不能修改（保证可安全跨协程传递、可做字典键）。
    """

    owner_id: str = "local-user"
    channel: str = "desktop"
    conversation_id: str = "desktop"
    module_id: str = "companion"


@dataclass(frozen=True)
class AccountTaskScope:
    """
    账户任务作用域 - 跨渠道的持久任务/确认身份

    与 Scope 的区别：
    - Scope：单次对话的作用域（channel + conversation_id 唯一）
    - AccountTaskScope：整个账户的作用域（跨渠道共享）

    为什么需要跨渠道？
    用户在网页上发起了一个需要确认的操作（比如删除文件），
    然后切换到手机 APP，应该能看到同样的待确认提示。
    AccountTaskScope 就是让这种跨渠道共享成为可能。

    注意：它**丢掉**了 channel 和 conversation_id，所以不要在需要"这一次对话"语义的地方用它。

    Attributes:
        account_id: 账户 ID（通常与 Scope.owner_id 一致）
        module_id: 模块 ID
    """

    account_id: str
    module_id: str = "companion"


@dataclass(frozen=True)
class RuntimeLock:
    """
    运行时锁 - 防止并发操作导致冲突

    为什么需要锁？
    想象你在超市买最后一瓶饮料：
    - 没有锁：你和另一个人同时拿起，发生冲突
    - 有锁：谁先拿到谁买走，另一个人只能等或放弃

    Agent 处理用户请求时也一样：
    - 如果同一个对话同时收到两条消息，不加锁会导致状态混乱
    - RuntimeLock 确保同一时刻只有一个操作在执行

    注意：这里只是**锁的描述**（持久化在账本里的那条记录），
    真正抢锁/放锁的动作在 persistence.py 里。

    Attributes:
        account_id: 账户 ID
        module_id: 模块 ID
        channel: 渠道
        conversation_id: 对话 ID
        action_id: 操作 ID（锁的唯一标识）
        reason: 加锁原因（如 "processing_message", "waiting_confirmation"）
        created_at: 创建时间（ISO 格式）
    """

    account_id: str
    module_id: str
    channel: str
    conversation_id: str
    action_id: str
    reason: str
    created_at: str


@dataclass(frozen=True)
class IngressContext:
    """
    入口上下文 - 已认证渠道适配器交给核心的元数据。

    生产入口必须由认证层构造它（不从用户文本里猜身份），
    README 里那几个 AGENT_ACCOUNT_ID / AGENT_CHANNEL 环境变量就是给 CLI 模拟这个用的。

    它是 Scope 的"来源"：`scope()` / `task_scope()` 两个方法把同一份入口信息
    分别投影成"这一次对话"和"这一个账户"两种身份。
    """

    account_id: str
    channel: str
    conversation_id: str
    module_id: str = "companion"
    delivery_target: str = "default"

    def scope(self) -> Scope:
        """投影成对话级作用域（含 channel + conversation_id）。"""
        return Scope(self.account_id, self.channel, self.conversation_id, self.module_id)

    def task_scope(self) -> AccountTaskScope:
        """投影成账户级作用域（跨渠道共享任务与确认）。"""
        return AccountTaskScope(self.account_id, self.module_id)


@dataclass(frozen=True)
class IntentPolicy:
    """工具的意图门控配置（挂在 ToolSpec 上，可选，默认 None = 不门控）。

    它回答的不是"这个工具危不危险"（那是 risk / effect 负责的事），
    而是"用户这句话到底是不是在要这个工具"。只有**聊天/工具双重含义明显**
    的工具才需要声明，比如"天气""搜索""音乐"——"天气局上班"和"查天气"
    字面接近但意图完全不同。

    Attributes:
        intent_name: 意图名（如 weather_query），分类器输出的 intent 就是它。
        object_terms: 对象词。命中只说明"话题相关"，不代表"在请求动作"。
        action_terms: 动作词。命中说明"在请求动作"。两者都命中才算明确请求。
        negative_terms: 排除词。文本里出现这些词时，对象词一律不作数
            （"天气局"不是天气查询）。
        required_slots: 必填参数槽位，缺一个就进不了最高置信度档。
        min_confidence: 该意图允许直接调用的下限，默认 0.85。
    """

    intent_name: str
    object_terms: tuple[str, ...] = ()
    action_terms: tuple[str, ...] = ()
    negative_terms: tuple[str, ...] = ()
    required_slots: tuple[str, ...] = ()
    min_confidence: float = 0.85


@dataclass(frozen=True)
class IntentDecision:
    """意图判定器的一次输出：判定意图、置信度、抽到的参数、判定理由。

    confidence 由 IntentClassifier 根据多项信号合成，**不是模型给的**。
    模型即使参与二次分类，也只能产出结构化的 intent/slots，不能直接决定是否调用工具。

    Attributes:
        intent: 意图名；"conversation" 是保留值，表示"这句是在聊天不是在要工具"。
        confidence: 0~1。对 conversation 而言表示"这是聊天的把握"。
        slots: 抽到的参数，如 {"location": "佛山"}。
        reason: 人可读的判定理由，进日志与测试断言。
        ambiguous: 规则无法给出高置信度结论（例如缺必填参数、与当前话题冲突）。
    """

    intent: str
    confidence: float
    slots: dict[str, str] = field(default_factory=dict)
    reason: str = ""
    ambiguous: bool = False
    related_intents: tuple[str, ...] = ()


CONVERSATION_INTENT = "conversation"


@dataclass
class ConversationIntentState:
    """一个会话的短生命周期语义状态（TTL + 轮数双重衰减）。

    它**不写进普通聊天历史**，只活在自己的存储里，用来回答一个问题：
    "上一轮的话题，会不会让这一轮的句子产生歧义？"

    Attributes:
        topic: 当前话题的可读摘要，如 "天气局工作"。
        topic_terms: 话题里命中的对象词（即便被 negative_terms 排除也算话题），
            用于判断这一轮是不是"撞上了旧话题"。
        active_intent: 上一轮的意图；"conversation" 表示上一轮在闲聊。
        slots: 已收集到的参数，跨轮复用（如追问后补的地点）。
        pending_intent: 正在等用户澄清的意图，非空表示"上一条是追问"。
        pending_question: 上一条追问的原话，便于日志与测试核对。
        confidence: 上一轮判定的置信度。
        updated_at: ISO 时间戳，TTL 判定用。
        turns: 已存活的轮数，超过上限就作废。
    """

    topic: str | None = None
    topic_terms: tuple[str, ...] = ()
    active_intent: str | None = None
    slots: dict[str, str] = field(default_factory=dict)
    pending_intent: str | None = None
    pending_question: str | None = None
    confidence: float = 0.0
    updated_at: str | None = None
    turns: int = 0


#: 工具效果三值 —— 回答"这个动作会在世界上留下什么"。
#:
#: 这三个值取代了原来的布尔 ``read_only``。布尔只能回答"有没有副作用"，
#: 而"有副作用"里混着两种性质完全不同的东西：调音量改错了再调回来就行，
#: 写文件改错了得真去把文件改回来。授权、超时对账、低风险回写复核
#: 都要按这个区别办事，所以它必须是一个**说得出区别**的字段，而不是一个位。
#:
#: * ``read``      —— 不改动任何东西，读多少次都一样。
#: * ``transient`` —— 改了，但改错了能改回来，或者重新读一次状态就知道现在在哪。
#: * ``persistent``—— 留下会活过这次调用的东西（文件、任务、提醒、歌单、会话）。
#:   这一类才需要独立复核与"超时后状态无法确认"的对账。
EFFECT_READ = "read"
EFFECT_TRANSIENT = "transient"
EFFECT_PERSISTENT = "persistent"
EFFECT_VALUES = (EFFECT_READ, EFFECT_TRANSIENT, EFFECT_PERSISTENT)


@dataclass(frozen=True)
class ToolArgumentViolation:
    """工具级结构校验不通过时，工具交回给网关的结构化结果。

    **为什么不是一句错误文案**：调用方（模型）要知道"改哪个字段"才能自己修好。
    一个字符串只装得下给人读的说明，装不下"缺哪几个字段"这种能被程序处理的信息；
    让下游去解析文案里的字段名，等于把措辞当接口 —— 措辞一改，判断就静默失效。

    Attributes:
        error: 稳定错误码，形如 ``missing_task_input``。下游按它分支，不解析别的。
        error_kind: 失败归类，取值见 TOOL_ERROR_KINDS。默认 permanent ——
            参数不对是调用方的问题，原样重试不会有不同结果。
        output: 结构化细节，会进 ``ActionObservation.output``。
            只放**字段名与枚举取值**，不放参数原文（那里可能有本机路径、用户原话）。
    """

    error: str
    error_kind: str = "permanent"
    output: dict[str, Any] = field(default_factory=dict)


# 工具级结构校验回调的统一签名：收到参数字典，通过返回 None、不通过返回违规结构。
#
# 它判的是"**这个工具**的参数在结构上自洽吗"。JSON Schema 判不了跨字段关系
# （最典型的：`input` 该带哪些字段，由 `task_type` 决定），这一层就是为此存在的。
# 单个字段的类型、必填、长度一律写进 schema，不要写进回调 ——
# 同一件事有两处判据时，这两处迟早会给出不同答案。
ToolArgumentValidator = Callable[[Mapping[str, Any]], ToolArgumentViolation | None]


def is_blank(value: Any) -> bool:
    """"这个值等于没给"的统一判据：非字符串、空串、纯空白都算。

    **只有一份实现，是刻意的。** 网关那条"必填字符串不能全是空白"的检查、
    一致性复核的判据表、Work 的任务输入契约 —— 三处都在问同一个问题。
    各写各的 `not value.strip()` 时，`"  "` 在 A 处算没给、在 B 处算给了，
    症状是"某条路径上校验不生效"，而且只在纯空白的输入上出现。
    纯空白是最容易被漏测的一档：它有长度，`required` 和 `minLength` 都拦不住。

    注意它对**非字符串**也返 True（含 None）。判据是"给不出内容"，
    `0`、`False` 这类会得到 True，所以别拿它当"值是否缺省"用 ——
    它的适用面就是"这个参数是不是一句话/一个名字"。
    """
    return not isinstance(value, str) or not value.strip()


@dataclass(frozen=True)
class ToolSpec:
    """
    一个可注册工具的全部元数据。

    这是"工具"在系统里的唯一真相：schema 给模型看（决定它能不能调、怎么调），
    handler 给网关调，其余字段决定**授权与路由策略**。

    Attributes:
        name: 工具名。模块内注册时会变成 "模块__工具"（如 media__submit_tts）。
        schema: OpenAI function-calling 格式的 JSON Schema。
        handler: 真正干活的函数，签名见 ToolHandler。
            **只允许 ActionGateway 调用**（见 actions.py）。别处直接调 handler
            都是绕过权限、审批、审计与结果归一化的旁路。
        risk: "low" / "high"。high 会触发人工确认（见 requires_confirmation）。
            **它管"要不要问人"，与 effect 是两条独立的线**：一个纯查询的动作也可能
            因为涉及隐私而要被确认，一个会写文件的动作也可能因为范围极小而不问。
        timeout_seconds: 单次执行超时，超时由网关统一转成 failed；
            persistent 工具在重启后被对账为 outcome_unknown（见 unconfirmable_on_timeout）。
        confirmation_title: 确认弹窗标题（给用户看的"我在问什么"）。
        effect: 这个动作会在世界上留下什么，取值见 EFFECT_VALUES。
            **每个工具都必须显式声明。** 默认值是 persistent（最严的一档）：
            忘了声明的工具按"会留下东西"处理，宁可多问一句，也不能被误放宽。
        hard_routable: 是否允许被**运行时**直接调用（绕过模型替用户发起）。
            它和 effect 是两条独立的线：effect 描述"会留下什么"，
            hard_routable 描述"有没有被显式授权绕过模型"。会留下东西的工具
            （比如会真的建后台任务的 work__submit）必须显式打开这个开关才能硬拦，
            不能靠谎报 effect="read" 来换取硬拦资格。
        deterministic_route_allowed: 是否允许走"确定性路由"（不经过模型直接调用）。
            注册时要求 effect=EFFECT_READ 或 hard_routable 至少满足一条，
            ToolRegistry.register 会强校验。
        query_extractor: 确定性路由时用哪个提取器从用户话里抠参数（如 "music"、"playback"）。
        intent_policies: 意图门控配置（可多个）。空元组表示语义明确、不需要过闸门。
            用复数是因为一个通用工具可以承载多个意图：search__web 既是
            "查天气"的出口，也是"搜资料"的出口，两者要分开判定、分开追问。
        capability_id: 这个工具属于哪个能力。空串表示从名字的 `模块__` 前缀推导。
        visibility: 谁能看见它，取值见 ACTION_VISIBILITY_VALUES。
        allowed_origins: 允许哪些来源发起；None 表示不限制来源。
        argument_validator: 这个工具自己的结构校验回调（可选）。
            网关在 JSON Schema 校验与"必填字符串非空白"校验之后、权限判定之前调用。
            **只有真正的跨字段关系才需要它**：单字段的类型/必填/长度都写在 schema 里，
            不写回调。返回 ToolArgumentViolation 表示拒绝，结构化细节进
            ActionObservation.output，让调用方知道该补什么。
    """

    name: str
    schema: dict[str, Any]
    handler: ToolHandler
    risk: str = "low"
    timeout_seconds: float = 15.0
    confirmation_title: str = "Confirmation required"
    effect: str = EFFECT_PERSISTENT
    hard_routable: bool = False
    deterministic_route_allowed: bool = False
    query_extractor: str | None = None
    intent_policies: tuple[IntentPolicy, ...] = ()
    capability_id: str = ""
    visibility: str = "model"
    allowed_origins: frozenset[str] | None = None
    argument_validator: ToolArgumentValidator | None = None

    def __post_init__(self) -> None:
        # 效果值写错必须在注册期就炸。拼错的值不会报错地退回默认的 persistent
        # （或更糟，被当成 read 放宽），两种情况都没人会注意到。
        if self.effect not in EFFECT_VALUES:
            raise ValueError(f"effect must be one of {list(EFFECT_VALUES)}, got {self.effect!r}")

    @property
    def requires_confirmation(self) -> bool:
        """高风险工具必须先经过人工确认才能执行。"""
        return self.risk == "high"

    @property
    def unconfirmable_on_timeout(self) -> bool:
        """超时之后，这个动作的副作用状态是不是**问不出来**。

        只有 persistent 是这样：写到一半的文件，重新读一次状态也判断不出写完没有。
        read 没有副作用；transient（音量、播放控制）重新读一次就知道现在到哪了，
        所以它们超时算普通失败、可以重试，不该被记成 outcome_unknown。
        """
        return self.effect == EFFECT_PERSISTENT

    @property
    def effective_capability_id(self) -> str:
        """工具归属的能力 id：显式声明优先，否则取 `模块__工具` 的前缀。"""
        if self.capability_id:
            return self.capability_id
        return self.name.split("__", 1)[0] if "__" in self.name else ""


@dataclass(frozen=True)
class ToolCall:
    """模型请求的一次工具调用：调哪个工具（name）+ 传什么参数（arguments）。

    call_id 是模型给的调用标识，用于把结果按 id 回填到同一个回合里。
    """

    call_id: str
    name: str
    arguments: dict[str, Any]


# 工具失败的分类词表，空串代表"未分类"（成功、或调用方没给分类）。
# 为什么要分类：failed 本身只说"没成"，没说"接下来该怎么办"。
# 分了类，下游才分得清"再试一次可能有救"（transient / timeout）、
# "重试也是白搭，是工具自己的 bug"（permanent）、"得人来点头"（permission）。
TOOL_ERROR_KINDS = frozenset({"", "timeout", "transient", "permanent", "permission"})


@dataclass(frozen=True)
class ToolResult:
    """工具执行的统一结果。

    约定：handler 返回的字典若同时含 "status" 和（"output" 或 "error"），
    就按这个结构理解；否则整个字典当作 output、status 视为 "ok"。
    这条归一化规则写在 actions.ActionGateway 里（tools.ToolExecutor 已降为网关内部实现）。

    **它只是"给模型回填消息"的适配结果，不再代表执行内核。**
    执行内核的唯一定义是 ActionObservation。

    Attributes:
        status: "ok" / "failed"（各模块也会用自己的状态词，如 "completed"）。
        output: 结构化产物。
        error: 失败原因，空串表示没有错误。
        error_kind: 失败分类，取值见 TOOL_ERROR_KINDS；空串表示未分类。
            异常路径由 tools.classify_tool_error 自动填，
            handler 也可以在返回字典里自己声明（值不在词表里会被丢弃）。
    """

    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""

    def as_dict(self) -> dict[str, Any]:
        """序列化给模型回填；error / error_kind 为空时不写这两个键，省 token。"""
        result: dict[str, Any] = {"status": self.status, "output": self.output}
        if self.error:
            result["error"] = self.error
        if self.error_kind:
            result["error_kind"] = self.error_kind
        return result


# ──────────────────────── 动作协议（执行内核的契约） ────────────────────────
#
# 为什么要独立于 ToolCall/ToolResult：
# 那两者是"模型消息的形状"——call_id 由模型给，结果回填给模型。
# 但真正要执行一个动作的来源远不止模型：确定性只读路由、定时任务、
# WorkHost、子代理都会发起。它们的共同点不是"模型说了什么"，
# 而是"谁、在什么作用域、以什么身份、要动哪个能力"。
# ActionRequest 描述后者，ActionObservation 描述结果；
# 网关是唯一把手伸进 handler 的地方。


#: 动作来源。每一种都是一个真实的发起路径，新增路径必须在这里登记。
#: 用途有二：① 网关按 `ToolSpec.allowed_origins` 做来源限制；
#:        ② 审计表按来源区分"谁发起的"，出问题时能定位到具体链路。
ACTION_ORIGINS = frozenset({
    "chat_model",     # 主 Chat：模型自由调用工具
    "chat_fastpath",  # 主 Chat：确定性只读路由（不经过模型）
    "subagent",       # 子代理：委派期间的动作
    "work_host",      # WorkHost：后台任务的内部动作
    "code_agent",     # 后台代码代理：快照、沙箱、知识与宿主网络动作
    "scheduler",      # 定时 / 条件任务
    "api",            # Web / CLI 直接发起的动作
})

#: `ToolSpec.visibility` 的合法取值。
#: - "model"    : 可以出现在模型工具清单里。
#: - "internal" : 永不进模型清单，只允许被 `allowed_origins` 里的来源调用。
#: 注意：两者都要经过网关。visibility 管"模型看不看得见"，
#: 权限（risk / allowed_origins）管"能不能真的跑"——两件事，别混。
ACTION_VISIBILITY_VALUES = frozenset({"model", "internal"})

#: 动作状态。这是**归一化之后**的唯一词表，前端只认这些值。
ACTION_STATUS_VALUES = frozenset({
    "completed",        # 正常完成
    "failed",           # 明确失败
    "rejected",         # 被权限 / 来源 / 参数校验拒绝
    "waiting_approval", # 高风险动作已落审批，等用户点头，尚未执行
    "cancelled",        # 用户主动取消
    "outcome_unknown",  # 副作用最终状态无法确认（见下）
})

#: **业务状态词**：handler 用它表达"这件事的领域结果"，不是系统层面的成败。
#:
#: 它们必须**原样透传**给调用方，不能归一化成 `failed`：
#:   - 位置工具用 `unavailable` 说"还没开位置共享"，用 `stale` 说"开过但太久" ——
#:     两句是两句不同的话，合成一句"失败"之后，用户会以为服务坏了而去重试。
#:   - 音乐工具用 `unhealthy` 说"播放器没起来"，同样不是一次"失败的动作"。
#:
#: 登记在这里（而不是让所有未知词都透传）是为了保住另一条规则：
#: **没登记过的词仍然降级为 failed**。否则拼错的状态词会一路流到前端
#: 当判断依据，而那正是这张表一开始要防的事。
ACTION_BUSINESS_STATUS_VALUES = frozenset({"unavailable", "stale", "unhealthy"})

#: 从"现有各方自己的状态词"到规范动作状态的映射。
#:
#: 为什么必须显式给出：handler 会返回 `ok`，media 模块返回 `completed`，
#: 快照里是 `paused` / `interrupted`…… 不做映射，前端会同时收到好几种
#: "成功"的说法，判断分支就写不完。**词表外的值一律降级为 failed +
#: invalid_action_status**，宁可报错也不让一个没见过的值流到下游当判断依据。
#:
#: 例外是上面那组业务词 —— 它们被原样透传，但仍需**登记在案**：
#: 新增一个业务词必须显式加进 `ACTION_BUSINESS_STATUS_VALUES`，
#: 加的时候就得回答"调用方看到这个词该说什么"，而不是让它悄悄流过去。
ACTION_STATUS_MAP: dict[str, str] = {
    "ok": "completed",
    "completed": "completed",
    "failed": "failed",
    "rejected": "rejected",
    "not_executed": "rejected",
    "paused": "waiting_approval",
    "waiting_approval": "waiting_approval",
    "cancelled": "cancelled",
    "interrupted": "outcome_unknown",
}

#: 状态映射失败的错误码：出现即说明某个 handler 返回了词表外的状态词。
ACTION_STATUS_INVALID = "invalid_action_status"

#: 动作错误的分类词表。在 TOOL_ERROR_KINDS 之上多一个 `indeterminate`：
#: 它专门表示"副作用可能已经发生，但我们不知道结果"。
#: 下游（尤其模型）**不得自动重试**这一类 —— 重试可能造成第二次副作用。
ACTION_ERROR_KINDS = frozenset({
    "", "timeout", "transient", "permanent", "permission",
    # 结果未知：副作用可能已经发生了。禁止自动重放。
    "indeterminate",
})


def normalise_action_status(raw: str) -> tuple[str, str]:
    """把任意状态词映射成规范动作状态。

    返回 ``(状态, 错误码)``：映射成功时错误码为空串；
    遇到词表外的值时返回 ``("failed", ACTION_STATUS_INVALID)``。

    这是纯函数，刻意不做 IO —— 它会被网关与测试同时依赖，
    必须能脱离任何存储单独验证。
    """
    if raw in ACTION_STATUS_MAP:
        return ACTION_STATUS_MAP[raw], ""
    if raw in ACTION_BUSINESS_STATUS_VALUES:
        return raw, ""
    return "failed", ACTION_STATUS_INVALID


@dataclass(frozen=True)
class ActionRequest:
    """一次动作发起的完整描述。

    这是"执行内核"的输入契约。它比 ToolCall 多了四样东西，每一样都有明确用途：

    - ``origin``：谁发起的。用于 `allowed_origins` 来源限制与审计。
    - ``scope``：完整作用域（owner / channel / conversation / module）。
      权限与数据隔离都按它判，不能靠调用方"顺手带上"。
    - ``identifiers``：run / turn / tool-call 三层标识，用于把动作挂回
      具体的回合与调用（审计、事件、重放对账都要）。
    - ``action_id``：动作编号。网关按它做**原子登记** —— 同一个编号
      不会被执行两次（重复的返回既有观察，不重跑）。

    注意：**参数里不得携带任何授权状态**。授权由网关按 origin + scope
    自己判；调用方说自己"已获批"一律无效（见 work_host 的
    `_HOST_OWNED_SUBMIT_KEYS`：连别名一起挡）。
    """

    action_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    scope: Scope = field(default_factory=Scope)
    origin: str = "chat_model"
    run_id: str = ""
    turn_id: str = ""
    tool_call_id: str = ""

    def __post_init__(self) -> None:
        # 来源必须是登记过的 —— 拼错的来源会让 allowed_origins 判断静默失效
        # （比对一个不存在的来源，结果取决于具体实现，这正是要避免的）。
        if self.origin not in ACTION_ORIGINS:
            raise ValueError(f"unknown action origin: {self.origin!r}")
        if not str(self.action_id).strip():
            raise ValueError("action_id is required")
        if not str(self.name).strip():
            raise ValueError("action name is required")


@dataclass(frozen=True)
class ActionObservation:
    """一次动作执行的结果。执行内核的**唯一**输出契约。

    Attributes:
        action_id: 对应 ActionRequest 的编号（审批恢复时按它找回原请求）。
        name: 动作名（工具名）。
        status: 规范动作状态，取值见 ACTION_STATUS_VALUES。
        output: 结构化产物。
        error: 失败原因（人可读的短句；敏感原文只进日志）。
        error_kind: 错误分类，取值见 ACTION_ERROR_KINDS。
        approval_id: status 为 waiting_approval 时，落库的审批编号；
            恢复时按它调用 `ActionGateway.resume`。
        approval_code: 审批的稳定码（前端按它取本地化文案）。
        duration_ms: 执行耗时，用于审计与超时分析。
        origin: 发起来源，回填自请求（审计用）。
        replayed: 本次结果是不是"读到的历史终态"而非"刚跑出来的结果"。
            审计表不存返回内容（只存字段名与终态），所以重放时 `output` 必然为空。
            调用方靠这个标志区分两种情况，避免把"没保留产物"误当成"动作产出了空结果"。
    """

    action_id: str
    name: str
    status: str
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_kind: str = ""
    approval_id: str = ""
    approval_code: str = ""
    duration_ms: int = 0
    origin: str = ""
    replayed: bool = False
    #: 「这次等待审批的人是我自己」——**只有网关的权限闸门会置它**。
    #:
    #: 为什么需要单独一个标志：`waiting_approval` 这个状态有两种来源，
    #: 语义完全不同，光看状态字段分不出来：
    #:
    #: 1. 网关判"高风险、要人点头" → 动作**根本没执行**，调用方必须挂起回合；
    #: 2. handler 自己返回 `status: "paused"` → 动作**已经执行完了**，
    #:    "paused" 是它带回来的业务结论（比如子代理在它自己那一层要确认），
    #:    调用方该做的是把这条结论回填给模型，而不是挂起自己的回合。
    #:
    #: 之前靠 `approval_id` 是否为空来区分，那是把"审批回调返回了什么"
    #: 当成判据 —— 回调换个实现就可能返回空串，然后整条回合被误挂起。
    awaiting_approval: bool = False

    @property
    def retryable(self) -> bool:
        """这个结果能不能被自动重试。

        `outcome_unknown` 永远不可重试：副作用可能已经发生，
        重放等于赌第二次副作用不会叠加。这是硬规则。
        """
        return self.status == "failed" and self.error_kind in {"timeout", "transient"}

    def as_tool_result(self) -> ToolResult:
        """适配成给模型回填的 ToolResult。

        **单向映射**：ActionObservation 是执行内核，ToolResult 只是消息适配。
        绝不允许反向用 ToolResult 当执行结果。
        """
        return ToolResult(self.status, dict(self.output), self.error, self.error_kind)



@dataclass(frozen=True)
class ProviderResponse:
    """一次模型调用的返回：message 是 assistant 消息体（可能含 tool_calls），usage 是 token 计量。"""

    message: dict[str, Any]
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRequest:
    """一次对话回合的输入打包。

    注意字段顺序：persona_override 之后才是后加的新字段，
    这是为了不改变既有位置参数调用的含义（注释见下）。
    """

    user_text: str
    scope: Scope | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    system_prompt: str = ""
    # 一次性附加指令（如"今天几号""当前场景"），不写入长期记忆。
    ephemeral_suffix: str = ""
    # Persona customization only. Keep this after legacy fields so existing
    # positional AgentRequest callers retain their meaning.
    # （仅人物卡定制用；放在旧字段之后，避免破坏既有的位置参数调用。）
    persona_override: str = ""
    account_id: str = ""
    delivery_target: str = ""
    # 多模态内容块与附件元数据（图片消息走这里，不进 user_text）。
    content_parts: list[dict[str, Any]] = field(default_factory=list)
    attachment_metadata: list[dict[str, Any]] = field(default_factory=list)
    turn_id: str = ""
    # 本轮可复用的关键背景（实施工作 A）。默认空：旧位置参数与旧调用完全不受影响。
    # 只承载「已经收到的事实搬运」——当前用户原话、已召回并带引用的记忆、
    # 相关内容状态、待澄清事项、必要原话片段与背景缺口；它不做记忆保存，
    # 也不是长期记忆库。payload 形态见 agent_core.chat_background.TurnBackground。
    turn_background: dict[str, Any] | None = None


@dataclass(frozen=True)
class ConfirmationRequest:
    """
    待确认操作 - 高风险工具的人工闸门。

    模型说"我要删文件"时，系统不直接执行，而是先落一条 ConfirmationRequest，
    回给用户一句"要不要执行"，等用户点确认后再用同一个 action_id 续跑。
    这样即使进程重启，那条待确认也还在（持久化在账本里）。
    """

    action_id: str
    run_id: str
    owner_id: str
    channel: str
    conversation_id: str
    tool_call_id: str
    tool: str
    arguments: dict[str, Any]
    code: str
    title: str
    expires_at: str
    status: str = "pending"
    module_id: str = "companion"
    module_spec_json: str = "{}"  # 存储模块配置，用于 resume 时重建 registry

    def as_dict(self) -> dict[str, Any]:
        """给前端/渠道适配器的展示用视图（故意不含 module_spec_json 这种内部细节）。"""
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "code": self.code,
            "tool": self.tool,
            "title": self.title,
            "arguments": self.arguments,
            "status": self.status,
            "expires_at": self.expires_at,
            "module_id": self.module_id,
        }


@dataclass(frozen=True)
class AgentOutcome:
    """一个回合的终局：正常回复 / 需要确认 / 出错，三选一。

    status 取值见 loop.py：正常回 "completed"，被闸门拦住回 "needs_confirmation"。
    confirmation 非空时，reply 通常是一句"要不要执行"的提示。
    """

    status: str
    run_id: str
    reply: str = ""
    action_id: str = ""
    confirmation_code: str = ""
    confirmation: ConfirmationRequest | None = None
    sources: tuple[dict[str, Any], ...] = ()
    answer_status: str = "answered"
    #: 回合交接：本轮**实际用到**的记忆引用、外部证据、工具终局状态和可信项目绑定。
    #: 只放已经发生的事实，供后台提炼读取；缺省 None 表示这一轮没收集（老路径）。
    handoff: dict[str, Any] | None = None


@dataclass(frozen=True)
class RunSnapshot:
    """运行快照 - Agent 断点续跑用（落在 agent_core.db 这类 SQLite 里）。

    steps / tokens 既是恢复依据，也是监控台 monitor_console.py 展示的运行健康度。
    """

    run_id: str
    scope: Scope
    messages: list[dict[str, Any]]
    steps: int
    tokens: int
    status: str
