"""开场白的联想线索：类型、频率闸门、冷却规则与使用记录。

【这个模块要解决什么】
"能从记忆里翻出旧事"和"偶尔自然地想起一件旧事"是两件事。
前者只需要一次检索；后者还要回答三个问题：

1. **现在该不该联想？** —— 上一句刚提过旧事，这一句再提就是复读；连着提三次，
   用户听到的就不是"她记得我"，而是"她在按表念我的档案"。
2. **哪件事还能提？** —— 上周刚提过的考试不该天天提，那是纠缠。
3. **这件事上次怎么说的？** —— 换个说法讲同一件事，用户立刻听得出这是"被记住了"。

前两条靠 :class:`OpeningRecallStore` 里的使用记录；第三条靠 ``cue_key``，
而它由**服务端**从选中线索的摘要算出（:func:`cue_dedupe_key`）—— 模型不参与判重键的
命名，"同一件事换个 id 再说一遍"就必然落到同一把键上。

【为什么单独一张表】
使用记录**不写聊天账本**（它不是要被读到的对话），也**不写 memory_records**
（它不是长期记忆，不参与记忆热度、过期与嵌入）。它只是一张"最近提过什么"的台账，
所以它有自己的生命周期：90 天前的记录顺手清掉，不需要后台任务。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .connection_pool import get_pool

LOGGER = logging.getLogger("agent_core.opening_recall")


#: 问候类型 —— 也是这张表唯一需要区分的三态。
GREETING_PLAIN = "plain"
GREETING_TOPIC = "topic"
GREETING_FACT = "fact"

#: 线索 id 的前缀。前缀既表达来源，也让"这是对话里的一句"与"这是账号事实"
#: 在日志和表里一眼可分。
TOPIC_CUE_PREFIX = "message:"
FACT_CUE_PREFIX = "fact:"

#: 冷却期。对话里的一句话 7 天不提，账号级事实 30 天不提 ——
#: 后者更"重"，也更是这个人的恒常属性，重复提起更像刻意。
TOPIC_COOLDOWN_DAYS = 7
FACT_COOLDOWN_DAYS = 30

#: 用过一次线索之后，至少要经过这么多次普通问候，才允许再次主动联想。
#: 1 → 至少隔开一次普通问候；具体线索仍受 7 / 30 天冷却与判重约束。
PLAIN_GREETINGS_BETWEEN_RECALLS = 1

#: 事件保留天数。到期记录在**写入时**顺手删掉 —— 不引入后台任务。
EVENT_RETENTION_DAYS = 90

#: 防重复查询的时间窗口。取最大冷却期（30 天）：窗口比冷却期短，冷却就会失效；
#: 条数上限只是兜底，防止极端高频进入把内存撑起来。
RECENT_EVENT_WINDOW_DAYS = 30
RECENT_EVENT_LIMIT = 200

#: ``cue_key`` 的长度约束。下限挡空串与单字（"事"这种大纲判不了重），
#: 上限挡住模型把整句话抄进来 —— 那会变成一条新的"记录"，而不是大纲。
CUE_KEY_MIN_CHARS = 2
CUE_KEY_MAX_CHARS = 40

_CUE_KEY_TRIM = "。，、；：！？…~～·-—.!,;:?\"'「」『』（）()"


@dataclass(frozen=True)
class OpeningCue:
    """一条可以拿来联想的线索。

    ``cue_id`` 是**稳定来源 id**（``message:123`` / ``fact:<uuid>``），不是内容 ——
    冷却与判重都靠它，靠文本比对会因标点或空格变化而失效。
    """

    cue_id: str
    source: str          # conversation / memory
    summary: str         # 原文摘要（给模型看的素材）
    occurred_at: str     # 发生时间（ISO 文本）
    category: str = ""   # 记忆类型；对话线索为 "topic"
    importance: float = 0.0

    @property
    def kind(self) -> str:
        return greeting_kind_for(self.cue_id)


@dataclass(frozen=True)
class GreetingDecision:
    """这次开场白的完整决策 —— 由**服务端**装配，不是模型申报。

    措辞模型只负责 ``greeting`` 那一个字段；``used_cue_id`` 是选题阶段**服务端选中**
    的那条线索，``cue_key`` 由该线索的摘要算出（:func:`cue_dedupe_key`）。
    让模型自己申报"我用了哪条、算哪件事"，等于把判重台账的写入权交给它 ——
    它编一个 id，我们就把一次编造记成"引用过的旧事"，之后几轮都按这件事防重复。
    """

    greeting: str
    used_cue_id: str | None = None
    cue_key: str | None = None

    @property
    def kind(self) -> str:
        return greeting_kind_for(self.used_cue_id)


@dataclass(frozen=True)
class CueSelection:
    """选题模型的输出：一个线索 id，或者明确"没有合适的"。

    它**只有选择权，没有措辞权，也没有否决台账的权力** —— 选中的 id 还要过一遍
    候选白名单（:func:`parse_cue_selection`）才算数。
    """

    selected_cue_id: str | None = None


def topic_cue_id(message_id: Any) -> str:
    return f"{TOPIC_CUE_PREFIX}{message_id}"


def fact_cue_id(record_id: Any) -> str:
    return f"{FACT_CUE_PREFIX}{record_id}"


def greeting_kind_for(cue_id: str | None) -> str:
    """线索 id → 问候类型。认不出的 id 一律按普通问候处理。"""
    value = str(cue_id or "").strip()
    if value.startswith(TOPIC_CUE_PREFIX):
        return GREETING_TOPIC
    if value.startswith(FACT_CUE_PREFIX):
        return GREETING_FACT
    return GREETING_PLAIN


def normalize_cue_key(value: str) -> str:
    """``cue_key`` 的规范化形式：去空白、去首尾标点、统一小写。

    目的是让"换种说法讲同一件事"被认出来 —— 模型两次生成的大纲用词会有小差异
    （``准备周五的考试`` / `` 准备周五的考试。 ``），规范化之后是同一个键。

    刻意只做**形状**上的规范化，不做同义改写：后者需要模型或词表，
    而这是一条每次进页面都会走的热路径，不能引入网络或歧义。
    """
    text = "".join(str(value or "").split())
    return text.strip(_CUE_KEY_TRIM).lower()


def usable_cue_key(value: str) -> bool:
    """这条大纲够不够格当判重键 —— 长度按**规范化之后**的值算。

    纯标点或纯空白（``。`` / ``...`` / 全空格）规范化之后是空串：拿它当键等于
    没有键，下次提同一件事时匹配不上，"不再重复提起"的保证就落空了。
    """
    normalized = normalize_cue_key(value)
    return CUE_KEY_MIN_CHARS <= len(normalized) <= CUE_KEY_MAX_CHARS


def cue_dedupe_key(cue: OpeningCue) -> str:
    """这条线索的判重键：**服务端**从摘要算出来，模型不参与。

    写台账和筛候选必须走这一个函数。两处各自手写一遍 ``normalize_cue_key`` 时，
    任何一处多截一段、少去一个标点，"上周记的键"和"这周算的键"就对不上，
    防重复当场失效 —— 而且失效是静默的，用户只会听到同一件事被讲两遍。
    """
    return normalize_cue_key(cue.summary)[:CUE_KEY_MAX_CHARS]


# ----------------------------------------------------------------------
# 频率闸门与冷却
# ----------------------------------------------------------------------
def should_allow_recall(
    events: Sequence[dict[str, Any]] | None,
    *,
    absence_tier: str = "medium",
) -> bool:
    """现在允许主动联想旧事吗。

    只剩一条规则：用过一次线索之后，必须经过 ``PLAIN_GREETINGS_BETWEEN_RECALLS``
    次普通问候才重新允许。没有这条，模型只要有素材就会次次都提。

    原来还有一条"``immediate`` 档永远不允许"（那个档位的意思是"可能只是刷新了
    页面"，提旧事等于把一次刷新说成一次重逢）。**它随档位一起停用了**
    （2026-09-18）："刷新不重演问候"这件事现在由前端进入槽负责 —— 同一次进入
    根本不会再问第二次，所以不需要再留一档来兜它。

    停用时另一条规则被证实仍然够用：冷却是按**线索**算的（7 天 / 30 天），
    "隔开一次普通问候"是按**进入**算的，两条都不依赖档位。

    ``absence_tier`` 参数保留只是为了不改调用方签名 —— 它不再影响结果。
    """
    if not events:
        # 还没有任何记录 = 这是第一次进入。首次可联想，不必先"攒"两次普通问候。
        return True
    plain_streak = 0
    for event in events:
        if str((event or {}).get("greeting_kind") or "") == GREETING_PLAIN:
            plain_streak += 1
            continue
        break
    return plain_streak >= PLAIN_GREETINGS_BETWEEN_RECALLS


def cooldown_blocklist(
    events: Sequence[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """冷却中的 ``(cue_id 集合, 规范化 cue_key 集合)``。

    两个集合都要查，缺一不可：``cue_id`` 挡住"同一句话"，``cue_key`` 挡住
    "同一件事换了一句话"（用户在另一个会话里又提了一次，message_id 变了，
    但说的是同一件事）。只查前者，用户会听到"上周的考试"被讲两遍。
    """
    current = now or datetime.now(UTC)
    blocked_ids: set[str] = set()
    blocked_keys: set[str] = set()
    for event in events or ():
        kind = str((event or {}).get("greeting_kind") or "")
        if kind not in (GREETING_TOPIC, GREETING_FACT):
            continue
        created = _parse_time((event or {}).get("created_at"))
        if created is None:
            # created_at 由本模块自己写，格式恒定；解析不了只可能是手工改过库。
            # 跳过而不是"永久冷却"：一条脏数据不该把一条线索永久屏蔽。
            continue
        cooldown = TOPIC_COOLDOWN_DAYS if kind == GREETING_TOPIC else FACT_COOLDOWN_DAYS
        if (current - created).total_seconds() / 86400 >= cooldown:
            continue
        cue_id = str((event or {}).get("cue_id") or "").strip()
        if cue_id:
            blocked_ids.add(cue_id)
        key = normalize_cue_key((event or {}).get("cue_key") or "")
        if key:
            blocked_keys.add(key)
    return frozenset(blocked_ids), frozenset(blocked_keys)


def available_cues(
    cues: Iterable[OpeningCue],
    events: Sequence[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> list[OpeningCue]:
    """从候选线索里滤掉正在冷却的那些，以及**定不出判重键**的那些。

    算不出一把可用的键，就等于提完之后无从追踪 —— 下次照样可能再提一遍。
    与其留下一条"提过但记不住"的线索，不如这次不提。
    """
    blocked_ids, blocked_keys = cooldown_blocklist(events, now=now)
    result: list[OpeningCue] = []
    for cue in cues:
        if cue.cue_id in blocked_ids:
            continue
        key = cue_dedupe_key(cue)
        if not usable_cue_key(key):
            continue
        if key in blocked_keys:
            continue
        result.append(cue)
    return result


# ----------------------------------------------------------------------
# 两段式输出解析
# ----------------------------------------------------------------------
def parse_cue_selection(text: str, *, allowed_cue_ids: Iterable[str] = ()) -> CueSelection | None:
    """解析选题模型的输出；**认不出的一律返回 ``None``**（由调用方按"没选"处理）。

    这一段的宽容度和措辞段刻意不同：

    - 选题段要求**整段就是一行裸 JSON**。带代码围栏、前面写一句"我的选择是"，
      在这里都算"没在按格式作答" —— 认不出就当没选，退成普通问候。
      选题没有"半对半错还能救"的中间态：它的输出只有一个 id，格式都不对，
      内容就更不可信。
    - 只认 ``selected_cue_id`` 这一个键，缺键即不合格（模型改写字段名说明它
      没在答题）。多带的键（``reason`` 之类）忽略 —— 那不影响 id 的可信度。
    - id 必须落在候选白名单里。放一个编造的 id 过去，台账就会把一次编造
      当成"引用过的旧事"，后面几轮的防重复全按错的事走。
    """
    value = str(text or "").strip()
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "selected_cue_id" not in payload:
        return None

    raw = payload["selected_cue_id"]
    if raw is None:
        # 明确选 null = "这些里没有适合开场的"。这是一个正常答案，不是失败。
        return CueSelection(None)
    if not isinstance(raw, str):
        # 数字、数组、true 都不是 id。布尔要单独挡住：Python 里 True == 1，
        # 不挡的话一个 ``true`` 能在候选是数字型 id 时蒙过去。
        return None
    cue_id = raw.strip()
    if not cue_id or cue_id not in {str(item) for item in allowed_cue_ids}:
        return None
    return CueSelection(cue_id)


def parse_narration(text: str) -> str | None:
    """从措辞模型的输出里取出 ``greeting``；取不到返回 ``None``。

    **只看 greeting 一个字段**，``used_cue_id`` 与 ``cue_key`` 一律不读 ——
    引用了哪条线索、算哪件事，由服务端在选题阶段就定好了（见 :class:`GreetingDecision`）。
    措辞模型多报回来的字段即使写得言之凿凿，也不参与任何判断。

    比选题段宽容：允许前后有杂字、允许代码围栏（:func:`_load_json_object`）。
    这句话本身还要过 :func:`~agent_core.opening_greeting.greeting_issue` 的内容判定，
    形状问题在那之前先收干净，免得一句好话因为带了引号被丢掉。
    """
    payload = _load_json_object(text)
    if payload is None:
        return None
    greeting = payload.get("greeting")
    return greeting if isinstance(greeting, str) else None


def _load_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里取出一个 JSON 对象；取不到返回 ``None``。

    允许前后有杂字（模型常写一句"好的，这是结果："再给 JSON），所以按第一个
    ``{`` 到最后一个 ``}`` 截取 —— 比要求"整段必须是合法 JSON"宽容，
    又不像"截到第一个 } 为止"那样容易截出半截对象。
    """
    value = str(text or "").strip()
    if not value:
        return None
    start = value.find("{")
    end = value.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(value[start:end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# ----------------------------------------------------------------------
# 使用记录
# ----------------------------------------------------------------------
class OpeningRecallStore:
    """「最近提过什么」的台账。**独立于聊天账本与记忆库。**

    表建在 ``agent_core.db``（与对话、确认记录同库），但只存稳定来源 id 与
    简短大纲 —— 不存完整聊天、完整提示词，也不存生成出来的那句话。
    存了那些，这张表就会变成一份"用户对话副本"，而它只是一张防重复的索引。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize()

    @property
    def _pool(self):
        return get_pool(str(self.path))

    def _initialize(self) -> None:
        with self._pool.get_connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS agent_core_opening_recall_events (
                    event_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    visit_id TEXT NOT NULL,
                    greeting_kind TEXT NOT NULL,
                    cue_id TEXT,
                    cue_key TEXT,
                    created_at TEXT NOT NULL
                )"""
            )
            # visit_id 参与唯一约束：React 重挂载、网络重试都会带着同一个 visit_id
            # 再来一次，只该留下一条记录 —— 否则"提过一次"会被记成三次，
            # 频率闸门当场失真。
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_opening_recall_visit
                ON agent_core_opening_recall_events
                (owner_id, channel, conversation_id, module_id, visit_id)"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_opening_recall_scope_created
                ON agent_core_opening_recall_events
                (owner_id, channel, module_id, created_at DESC)"""
            )

    @staticmethod
    def _timestamp(now: datetime | None = None) -> str:
        value = now or datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def recent_events(
        self,
        scope: Any,
        *,
        window_days: int = RECENT_EVENT_WINDOW_DAYS,
        limit: int = RECENT_EVENT_LIMIT,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """本账号在这个渠道与模块下最近的联想记录（时间倒序）。

        **刻意不按会话过滤**：用户切换会话之后再提起同一件事，听起来一样是复读。
        边界停在 ``(owner_id, channel, module_id)`` —— 别人的、别的渠道的记录
        不该影响这个用户的开场白。

        按**时间窗口**取而不是按条数取：窗口短于最大冷却期（30 天）冷却就会失效，
        而"一天进十次页面"的用户会在两天内就撑爆任何固定条数。
        """
        current = now or datetime.now(UTC)
        threshold = self._timestamp(current - timedelta(days=max(1, int(window_days))))
        with self._pool.get_connection() as db:
            rows = db.execute(
                """SELECT greeting_kind, cue_id, cue_key, created_at
                   FROM agent_core_opening_recall_events
                   WHERE owner_id=? AND channel=? AND module_id=? AND created_at>=?
                   ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (scope.owner_id, scope.channel, scope.module_id, threshold, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def record(
        self,
        scope: Any,
        *,
        visit_id: str,
        greeting_kind: str,
        cue_id: str | None = None,
        cue_key: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """写一条使用记录；返回"这次决策是否已经如实落在这条 visit_id 上"。

        撞唯一索引（同一个 visit_id 已经记过）**不自动算成功**：必须读回已有记录
        逐字段比对。同一次进入被重复请求时，模型完全可能给出不同的决策 ——
        若直接放行，用户看到的是新那句话，台账里存的却是旧那一句，
        下一轮判重就按错的记录走。宁可退回固定问候，也不能让台账与事实分叉。

        返回 False 表示"这次决策没能如实落库"：库异常、或撞上一条不一致的旧记录。
        """
        created_at = self._timestamp(now)
        try:
            with self._pool.get_connection() as db:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO agent_core_opening_recall_events
                       (event_id, owner_id, channel, conversation_id, module_id,
                        visit_id, greeting_kind, cue_id, cue_key, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        uuid.uuid4().hex,
                        scope.owner_id,
                        scope.channel,
                        scope.conversation_id,
                        scope.module_id,
                        str(visit_id),
                        greeting_kind,
                        cue_id,
                        cue_key,
                        created_at,
                    ),
                )
                if cursor.rowcount:
                    self._purge(db, now=now)
                    return True
                existing = db.execute(
                    """SELECT greeting_kind, cue_id, cue_key
                       FROM agent_core_opening_recall_events
                       WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?
                         AND visit_id=?""",
                    (
                        scope.owner_id,
                        scope.channel,
                        scope.conversation_id,
                        scope.module_id,
                        str(visit_id),
                    ),
                ).fetchone()
        except Exception:
            LOGGER.warning("开场白：联想使用记录写入失败", exc_info=True)
            return False
        return _same_decision(existing, greeting_kind=greeting_kind, cue_id=cue_id, cue_key=cue_key)

    def _purge(self, db: Any, *, now: datetime | None = None) -> int:
        """清掉保留期之前的记录。跟着写入走，不额外起后台任务。"""
        current = now or datetime.now(UTC)
        threshold = self._timestamp(current - timedelta(days=EVENT_RETENTION_DAYS))
        return db.execute(
            "DELETE FROM agent_core_opening_recall_events WHERE created_at < ?",
            (threshold,),
        ).rowcount


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _same_decision(
    row: Any,
    *,
    greeting_kind: str,
    cue_id: str | None,
    cue_key: str | None,
) -> bool:
    """已有记录与这次决策是不是同一件事。

    ``cue_key`` 按规范化后比对：同一件事的两次概括允许措辞有小抖动，
    不该因为多一个句号就判成"不一致"。
    """
    if row is None:
        # 既没插进去、也读不到：只可能是并发删除。按"没落库"处理 ——
        # 宁可这次降级，也不能把一句无法追踪的个性化问候交出去。
        return False
    if str(row["greeting_kind"] or "") != str(greeting_kind or ""):
        return False
    if str(row["cue_id"] or "") != str(cue_id or ""):
        return False
    return normalize_cue_key(row["cue_key"] or "") == normalize_cue_key(cue_key or "")


def delete_scope_events(db: Any, scope: Any) -> int:
    """删掉某个会话的联想记录（返回删除条数）。

    刻意接收**调用方的连接**而不自己开一条：会话清理必须与删消息在同一个事务里，
    否则中间会出现"气泡删了、派生记录还在"的状态，而下一次开场白正好读到它，
    就会提起一段用户刚刚清空的对话。

    表还没建（功能未启用、或首次启动）时返回 0 且不抛：一次"清空对话"
    不该因为一张可选表而失败。
    """
    try:
        return db.execute(
            """DELETE FROM agent_core_opening_recall_events
               WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?""",
            (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
        ).rowcount
    except sqlite3.OperationalError:
        return 0


def delete_orphan_events(db: Any) -> int:
    """删掉"父会话已经不在了"的联想记录。

    给自动清理（会话过期被删）用：那条路径按时间批量删会话行，没有逐个 Scope
    可传，所以用 NOT EXISTS 兜底扫一遍 —— 与展示游标的孤儿清理同一个套路。
    """
    try:
        return db.execute(
            """DELETE FROM agent_core_opening_recall_events
               WHERE NOT EXISTS (
                   SELECT 1 FROM agent_core_conversations AS c
                   WHERE c.owner_id=agent_core_opening_recall_events.owner_id
                     AND c.channel=agent_core_opening_recall_events.channel
                     AND c.conversation_id=agent_core_opening_recall_events.conversation_id
                     AND c.module_id=agent_core_opening_recall_events.module_id
               )"""
        ).rowcount
    except sqlite3.OperationalError:
        return 0
