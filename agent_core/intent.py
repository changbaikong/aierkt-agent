"""意图判定与轻量会话状态 —— "这句话到底是不是在要工具"。

【在系统里的位置】
loop.py 的确定性路由（音乐搜索、事实查询）是纯规则的：命中就调工具，不问上下文。
代价是"我昨天去天气局上班啦"之后的"天气查询"会被当成查天气。这个文件补上缺失的那一层：

    用户输入 → RuleIntentClassifier（判意图 + 抽参数 + 算置信度）
             → IntentGate（按置信度决定 调工具 / 追问 / 普通聊天）

【为什么置信度由规则算而不是模型给】
模型给分不可复现、不可断言，还会为了"显得确定"而给高分。
规则评分器的每个加减项都写在 SCORE_* 常量里，测试可以逐项核对。
本版**不引入独立分类模型**：模型只在"规则判定为歧义"时负责把追问说得自然一点，
它输出的不是决定，只是措辞。

【三档阈值】
    >= 0.85  明确工具请求 → 调工具
    0.55~0.85 语义有冲突或缺参数 → 追问
    < 0.55   普通聊天 → 不调工具（连工具清单都不给模型，见 loop 的 block_all）

【状态的生命周期】
ConversationIntentState 短命、可有可无：丢了最坏是重新追问一次，不会做出错误动作。
所以存储做成 Protocol + 两种实现（SQLite 生产 / 内存测试），TTL 与轮数双重衰减。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from .connection_pool import get_pool
from .contracts import (
    CONVERSATION_INTENT,
    ConversationIntentState,
    IntentDecision,
    IntentPolicy,
    Scope,
)

# ── 评分常量：每一项都可以在测试里单独核对 ──
SCORE_BASE = 0.10          # 任何输入都有的基线，保证空输入不会是 0
SCORE_ACTION = 0.35        # 命中动作词（查 / 搜索 / 播放…）
SCORE_NO_ACTION = -0.05    # 没有动作词 → 多半是陈述句
SCORE_OBJECT = 0.30        # 命中对象词（天气 / 歌曲…）
SCORE_SLOTS_FULL = 0.23    # 必填参数齐全
SCORE_SLOTS_MISSING = -0.10  # 必填参数缺失
CONFLICT_PENALTY = -0.20   # 与当前话题冲突

# 冲突判定后夹取的区间：下限 0.55 保证"宁可追问也不要直接放弃"，
# 上限 0.84 保证"再高也不许绕过追问直接调工具"。
CONFLICT_CLAMP_LOW = 0.55
CONFLICT_CLAMP_HIGH = 0.84

CLARIFY_THRESHOLD = 0.55   # 低于它按普通聊天处理

# 全局动作词。IntentPolicy.action_terms 没声明时用它兜底。
DEFAULT_ACTION_TERMS: tuple[str, ...] = (
    "帮我查", "帮我搜", "查一下", "查查", "查询", "查",
    "搜一下", "搜索一下", "搜索", "搜搜", "搜", "查找", "找一下",
    "来首", "放首", "播放", "听首", "点首",
)

# 陈述语气词 / 疑问词：只用于给 conversation 意图打分（区分"在聊"还是"在问"）
DECLARATIVE_TERMS: tuple[str, ...] = ("啦", "了", "过", "呢", "吧", "啊", "呀", "嘛")
INTERROGATIVE_TERMS: tuple[str, ...] = ("什么", "怎么", "为什么", "干什么的", "做什么的", "是谁", "哪些", "哪")

CONVERSATION_SCORE_DECLARATIVE = 0.98
CONVERSATION_SCORE_INTERROGATIVE = 0.96
CONVERSATION_SCORE_NEUTRAL = 0.70

# 槽位提取用的正则
_LOCATION_PATTERN = re.compile(r"([\u4e00-\u9fa5]{2,10}?)(?:的)?(?:今天|明天|今日|明日|这周|当前)?(?:的)?天气")
_TRAILING_MUSIC_TERMS = re.compile(r"(?:这首歌|这首|歌曲|音乐|歌|歌手|专辑|歌单|网易云音乐?)$")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def strip_action_prefix(text: str, action_terms: Sequence[str] = DEFAULT_ACTION_TERMS) -> str:
    """剥掉开头的动作词，剩下才是真正的查询内容。

    "查询佛山的天气" → "佛山的天气"；槽位提取必须先做这一步，
    否则会抽出一个"查询佛山"这种带着动词的脏参数。
    """
    value = str(text or "").strip()
    for term in sorted(action_terms, key=len, reverse=True):
        if value.startswith(term):
            return value[len(term):].strip(" 的:：,，")
    return value


# ─────────────────── 追问后"只答参数"的保守续接 ───────────────────
# 只有当上一轮已经存下 pending_intent 时，这一句短回答才有意义：
# 它不重新走分类器、不产生新的置信度，仅仅因为"待确认意图已经定义了它在回答什么"。

# ── 撤回 / 换话题：**只认整句或明确句式**，不认"句中含某个子串" ──
# 为什么改口径："不是北京，是上海"含"不是"、"不用北京，查上海"含"不用"，
# 但它们都是**改口补参数**，不是放弃。用子串匹配会把一次正常回答误伤成取消，
# 用户被迫把整句话重说一遍 —— 这正是本组常量要修掉的行为。
_PENDING_DECLINE_EXACT: frozenset[str] = frozenset({
    "算了", "算了吧", "算了算了", "取消", "取消吧", "不必", "不需要",
    "不用", "不用了", "不要了", "不查了", "不搜了", "不放了", "不听了",
    "不看了", "不弄了", "继续聊", "接着聊", "不聊了",
})
# "算了…"开头的整句：还要在后半句里看到明确的否定/取消，才承认是撤回。
# 否则"算了，还是查北京吧"会被误判成放弃。
_PENDING_DECLINE_LEADER = "算了"
_PENDING_DECLINE_TAIL_MARKERS: tuple[str, ...] = ("不", "别", "取消", "没必要")

# 明确的换话题句式。只在"这句话没能补上任何参数"的前提下才生效（见 loop._route_by_intent）：
# "我说的是北京"和"我说的是上班的事"前缀一样，区分它们的是**有没有抠出可用的参数**。
_PENDING_TOPIC_SWITCH_PREFIX: tuple[str, ...] = (
    "我说的是", "我是说", "我说的不是", "我不是说", "我说的是不是",
)
_PENDING_TOPIC_SWITCH_TAIL: tuple[str, ...] = (
    "换个话题", "换话题", "聊点别的", "聊别的", "说点别的", "不聊这个", "别聊这个",
)
# 句尾的语气词与标点：判"整句是不是就是这个词"之前先削掉。
_PENDING_TRAILING_PUNCT = re.compile(r"[\s。．.!！?？~～、,，;；:：]+$")
_PENDING_TAIL_PARTICLES = re.compile(r"[吧呗啊呀哦嘛嗯啦]+$")

# 听得出是"没在回答"的敷衍句：既不填参数，也不撤销，保持追问。
_PENDING_NON_ANSWERS: tuple[str, ...] = (
    "不知道", "不清楚", "不确定", "不晓得", "忘了", "忘记", "随便", "都行",
    "都可以", "无所谓", "你猜", "没想好", "看着办", "都行吧",
)

# ── 改口句式：把"新值"从句子里捞出来 ──
# 支持三类：① 否定在前（不是X，是Y / 不用X，查Y）② 否定在后（X，不是Y）③ 重申前缀（我说的是Y）。
_CORRECTION_SEGMENT_SPLIT = re.compile(r"[，,；;、。]+")
_CORRECTION_NEGATOR = re.compile(r"^(?:我)?(?:不(?:是|用|要|查|搜|看|听|放|想)|别(?:说|放|查|搜))")
_CORRECTION_VALUE_MARKER = re.compile(r"^(?:而|就)?(?:是|要|查|搜|看|听|放|播|用|来|换|改|想)")
_PENDING_RESTATE_PREFIX = re.compile(r"^(?:我)?(?:说的?是|是说)")

# 地点前缀里的礼貌/动作壳，剥掉后才是地名本身。
_LOCATION_WRAPPER = re.compile(
    r"^(?:那|就|还是|然后|另外|对了|帮我|给我|请|麻烦|我要|我想|继续聊|接着聊|去|在)+"
)
# 尾部"天气"及常见收尾：去掉后剩下的才是城市名。
_TRAILING_WEATHER = re.compile(
    r"(?:的)?(?:今天|明天|今日|明日|现在|这周)?(?:的)?天气(?:怎么样|如何|的情况|情况)?[?？。!！]?$"
)
# 合法地名：2~32 个中文 / 拉丁字母 / 数字 / 空格 / 间隔号 / 连字符。
_LOCATION_SHAPE = re.compile(r"^[一-鿿A-Za-z0-9 ·\-]{2,32}$")
# 明显是别的工具动作的词，出现在"地名"里说明这根本不是地名。
_LOCATION_ACTION_BLOCK = re.compile(
    r"(?:播放|搜索|查一下|查询|查查|查|搜|看看|看下|看|听|整理|列出|统计|截图|打开|关闭|音量|天气|歌曲|音乐|歌|文件|目录)"
)
# 明显不是地名的"内容词"：出现在地名位置只说明用户在聊别的事。
_LOCATION_TOPIC_BLOCK: tuple[str, ...] = (
    "事情", "话题", "工作", "上班", "下班", "聊天", "闲聊", "聊点",
    "东西", "问题", "意思", "什么", "怎么", "为什么", "这个", "那个",
    "别的", "其他", "刚才", "之前", "事",
)


def _normalize_utterance(text: str) -> str:
    """判"整句语义"之前的归一化：去空白、去句尾标点。"""
    return _PENDING_TRAILING_PUNCT.sub("", _compact(text)).strip()


def is_pending_decline(user_text: str) -> bool:
    """是否是明确的"撤回/取消"。只在存在 pending_intent 时调用。

    判据是**整句**或**明确句式**，不再看"句中是否含某个子串"：
    "不是北京，是上海"含"不是"，但它是改口补参数；用子串判会把正常回答误伤成取消。
    """
    compact = _normalize_utterance(user_text)
    if not compact:
        return False
    for candidate in (compact, _PENDING_TAIL_PARTICLES.sub("", compact)):
        if candidate in _PENDING_DECLINE_EXACT:
            return True
    if compact.startswith(_PENDING_DECLINE_LEADER):
        rest = compact[len(_PENDING_DECLINE_LEADER):]
        if any(marker in rest for marker in _PENDING_DECLINE_TAIL_MARKERS):
            return True
    return False


def is_pending_topic_switch(user_text: str) -> bool:
    """是否是明确的"换话题"。

    只在**这句没能补上任何参数**时才该被调用：`我说的是北京` 与 `我说的是上班的事`
    前缀相同，区分它们的是"有没有抠出可用的槽位值"，而不是句式本身。
    """
    compact = _normalize_utterance(user_text)
    if not compact:
        return False
    if any(compact.startswith(prefix) for prefix in _PENDING_TOPIC_SWITCH_PREFIX):
        return True
    return any(tail in compact for tail in _PENDING_TOPIC_SWITCH_TAIL)


def is_pending_correction(user_text: str) -> bool:
    """这句是不是在**改口**（而不是单纯补一个参数）。

    带"否定旧值"或"重申"标记的句子才允许覆盖上一轮已经存下的参数；
    普通短回答（"北京"）只能填当前缺的槽位。
    """
    compact = _compact(user_text)
    if not compact:
        return False
    return bool(_CORRECTION_NEGATOR.match(compact) or _PENDING_RESTATE_PREFIX.match(compact))


def _correction_value(user_text: str) -> str | None:
    """从改口句里捞出"新的那个值"。

    ① 否定在前：`不是北京，是上海` / `不用北京，查上海` → 取最后一段，剥掉取值标记。
    ② 否定在后：`北京，不是上海` → 取第一段（用户先说了对的那个）。
    其它情况返回 None，交给普通提取器。
    """
    value = _normalize_utterance(user_text)
    if not value:
        return None
    segments = [item.strip() for item in _CORRECTION_SEGMENT_SPLIT.split(value) if item.strip()]
    if len(segments) < 2:
        return None
    first, last = segments[0], segments[-1]
    if _CORRECTION_NEGATOR.match(first):
        tail = _PENDING_RESTATE_PREFIX.sub("", last, count=1)
        tail = _CORRECTION_VALUE_MARKER.sub("", tail, count=1)
        return tail.strip() or None
    if _CORRECTION_NEGATOR.match(last):
        return first or None
    return None


def _correction_candidates(user_text: str) -> tuple[str, ...]:
    """提取参数时按顺序尝试的文本：改口后的新值优先，原句兜底。"""
    value = _correction_value(user_text)
    raw = str(user_text or "").strip()
    if value and value != raw:
        return (value, raw)
    return (raw,)


def _is_non_answer(compact: str) -> bool:
    return any(_compact(term) in compact for term in _PENDING_NON_ANSWERS)


def _location_from_candidate(text: str) -> str | None:
    """把一段文本当"地名候选"来清洗与校验，站不住脚就返回 None。"""
    value = str(text or "").strip()
    if not value or is_pending_decline(value) or _is_non_answer(_compact(value)):
        return None
    while True:
        stripped = _LOCATION_WRAPPER.sub("", value, count=1).strip()
        stripped = _PENDING_RESTATE_PREFIX.sub("", stripped, count=1).strip()
        stripped = _TRAILING_WEATHER.sub("", stripped).strip()
        stripped = stripped.strip(" 的:：,，")
        if stripped == value:
            break
        value = stripped
        if not value:
            return None
    if any(mark in value for mark in ("?", "？", "/", "\\", ":", "：", "http", "www")):
        return None
    if not _LOCATION_SHAPE.match(value) or _LOCATION_ACTION_BLOCK.search(value):
        return None
    # 仍以否定词开头的"地名"几乎一定是句子片段（"不是上海"），不是地名。
    if _CORRECTION_NEGATOR.match(_compact(value)):
        return None
    # 明显在聊别的东西的词（事情/话题/工作/上班/聊天…）：这类词出现在地名位置
    # 只可能是用户在换话题或敷衍，拿它去查天气会拼出"上班 天气"这种胡话。
    if any(word in value for word in _LOCATION_TOPIC_BLOCK):
        return None
    return value


def _extract_location_answer(user_text: str) -> str | None:
    for candidate in _correction_candidates(user_text):
        value = _location_from_candidate(candidate)
        if value:
            return value
    return None


def _query_from_candidate(raw: str, policy: IntentPolicy) -> str | None:
    if not raw or is_pending_decline(raw) or _is_non_answer(_compact(raw)):
        return None
    value = strip_action_prefix(raw, policy.action_terms or DEFAULT_ACTION_TERMS)
    value = _PENDING_RESTATE_PREFIX.sub("", value, count=1).strip()
    value = _TRAILING_MUSIC_TERMS.sub("", value).strip(" 的：:，,。！？!? ")
    if not (1 <= len(value) <= 80):
        return None
    if any(mark in value for mark in ("?", "？", "\n", "\t")):
        return None
    # 仍带着否定词的"查询词"是句子片段（"不用五月天"），不是歌手/歌名。
    if _CORRECTION_NEGATOR.match(_compact(value)):
        return None
    return value or None


def _extract_query_answer(user_text: str, policy: IntentPolicy) -> str | None:
    for candidate in _correction_candidates(user_text):
        value = _query_from_candidate(candidate, policy)
        if value:
            return value
    return None


def extract_pending_slots(
    user_text: str,
    policy: IntentPolicy,
    missing_slots: Sequence[str],
) -> dict[str, str]:
    """把"只答参数"的短回答填进**当前缺失**的槽位。

    认不出的槽位名一律不猜（返回里不带它），让调用方据此继续追问。
    """
    slots: dict[str, str] = {}
    if policy is None:
        return slots
    for slot in missing_slots:
        if slot == "location":
            value = _extract_location_answer(user_text)
        elif slot == "query":
            value = _extract_query_answer(user_text, policy)
        else:
            value = None
        if value:
            slots[slot] = value
    return slots


# ─────────────────────────── 状态存储 ───────────────────────────


class IntentStateStore(Protocol):
    """会话语义状态的存储接口。

    故意做得和 RuntimeStore 一样窄（load / save / clear），
    这样内存实现和 SQLite 实现能同签名替换，测试不需要碰数据库。
    """

    def load(self, scope: Scope) -> ConversationIntentState | None: ...
    def save(self, scope: Scope, state: ConversationIntentState) -> None: ...
    def clear(self, scope: Scope) -> bool: ...


def _state_is_fresh(state: ConversationIntentState, *, ttl_seconds: int, max_turns: int, now: datetime) -> bool:
    """TTL 与轮数双重衰减，任一超限即作废。

    只看时间不够：连续聊十几轮后，"上一轮的话题"早就不该再影响当前判定。
    """
    if int(state.turns) >= int(max_turns):
        return False
    raw = str(state.updated_at or "").strip()
    if not raw:
        return False
    try:
        updated = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return (now - updated) <= timedelta(seconds=int(ttl_seconds))


class InMemoryIntentStateStore:
    """进程内实现：状态和 LocationStore 一样只活在内存里，进程一退就没。

    给测试和单进程 CLI 用。丢状态的后果只是"重新追问一次"，不是数据丢失。
    """

    def __init__(self, *, ttl_seconds: int = 900, max_turns: int = 5, now=None) -> None:
        self.ttl_seconds = int(ttl_seconds)
        self.max_turns = int(max_turns)
        self._now = now or (lambda: datetime.now())
        self._values: dict[tuple[str, str, str, str], ConversationIntentState] = {}

    @staticmethod
    def _key(scope: Scope) -> tuple[str, str, str, str]:
        return (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id)

    def load(self, scope: Scope) -> ConversationIntentState | None:
        state = self._values.get(self._key(scope))
        if state is None:
            return None
        if not _state_is_fresh(state, ttl_seconds=self.ttl_seconds, max_turns=self.max_turns, now=self._now()):
            self._values.pop(self._key(scope), None)
            return None
        return state

    def save(self, scope: Scope, state: ConversationIntentState) -> None:
        self._values[self._key(scope)] = state

    def clear(self, scope: Scope) -> bool:
        return self._values.pop(self._key(scope), None) is not None


class SQLiteIntentStateStore:
    """SQLite 实现：复用 agent_core.db 与连接池，web / chat_cli 两个入口自动共享。

    两个入口读的是同一个 AGENT_CORE_DB，所以状态天然跨入口一致；
    进程重启后"正在追问"的那一半对话也不会凭空消失。
    """

    TABLE = "agent_core_intent_state"

    def __init__(self, path: Path | str, *, ttl_seconds: int = 900, max_turns: int = 5, now=None) -> None:
        self.path = Path(path)
        self.ttl_seconds = int(ttl_seconds)
        self.max_turns = int(max_turns)
        self._now = now or (lambda: datetime.now())
        # 读缓存：绝大多数对话是在同一个进程里连续进行的，每次 load 都去
        # 连接池取一次连接要 ~2.7ms（实测几乎全是连接获取/归还的开销，不是 SQL），
        # 而读出来的东西刚才是自己写进去的。所以读走缓存、写仍然落库。
        # 代价：另一进程改了同一会话时，本进程最多晚一轮看到。
        # 这个状态只是"上轮话题"的提示，晚一轮的后果顶多是重新追问一次。
        self._cache: dict[tuple[str, str, str, str], ConversationIntentState] = {}
        self._initialize()

    def _initialize(self) -> None:
        with get_pool(str(self.path)).get_connection() as db:
            db.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    owner_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    module_id TEXT NOT NULL DEFAULT 'companion',
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, channel, conversation_id, module_id)
                )"""
            )

    @staticmethod
    def _key(scope: Scope) -> tuple[str, str, str, str]:
        return (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id)

    def load(self, scope: Scope) -> ConversationIntentState | None:
        key = self._key(scope)
        cached = self._cache.get(key)
        if cached is not None:
            if _state_is_fresh(cached, ttl_seconds=self.ttl_seconds, max_turns=self.max_turns, now=self._now()):
                return cached
            self._cache.pop(key, None)
            self.clear(scope)
            return None
        with get_pool(str(self.path)).get_connection() as db:
            row = db.execute(
                f"SELECT state_json, updated_at FROM {self.TABLE} "
                "WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["state_json"])
        except (TypeError, ValueError):
            self.clear(scope)
            return None
        state = _state_from_payload(payload)
        if not _state_is_fresh(state, ttl_seconds=self.ttl_seconds, max_turns=self.max_turns, now=self._now()):
            self.clear(scope)
            return None
        self._cache[key] = state
        return state

    def save(self, scope: Scope, state: ConversationIntentState) -> None:
        self._cache[self._key(scope)] = state
        payload = json.dumps(_state_to_payload(state), ensure_ascii=False)
        with get_pool(str(self.path)).get_connection() as db:
            db.execute(
                f"""INSERT INTO {self.TABLE}
                (owner_id, channel, conversation_id, module_id, state_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, channel, conversation_id, module_id)
                DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (
                    scope.owner_id, scope.channel, scope.conversation_id, scope.module_id,
                    payload, _now_iso(),
                ),
            )

    def clear(self, scope: Scope) -> bool:
        self._cache.pop(self._key(scope), None)
        with get_pool(str(self.path)).get_connection() as db:
            updated = db.execute(
                f"DELETE FROM {self.TABLE} WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id),
            )
        return bool(updated.rowcount)

    def clear_orphans(self) -> int:
        """清掉"父会话已经不存在"的意图状态，返回删除条数。

        为什么需要它：删会话有两条路 —— HTTP 接口显式删，和 ``cleanup()`` 按
        保留期删。前者会顺手调 ``manager.forget``，后者根本不知道意图表存在。
        没有这道兜底，保留期删掉的会话会把状态留成孤儿行：既没人读、也没人清。

        归档会话**不算孤儿**：它的父行还在（只是 ``status='archived'``），
        恢复回来时那句"刚才正要帮你查天气，还查吗？"仍然应该有效。

        先查后删而不是直接 ``DELETE ... WHERE NOT EXISTS``：要删的键必须和
        要清出 ``_cache`` 的键**一模一样**。缓存里留着一条已删的状态，比库里
        留着更糟 —— 本进程下一轮还会拿它接着追问，而那份数据其实已经没人管了。
        """
        clause = (
            f"SELECT owner_id, channel, conversation_id, module_id FROM {self.TABLE} AS s"
            " WHERE NOT EXISTS ("
            "  SELECT 1 FROM agent_core_conversations AS c"
            "  WHERE c.owner_id=s.owner_id AND c.channel=s.channel"
            "    AND c.conversation_id=s.conversation_id AND c.module_id=s.module_id)"
        )
        with get_pool(str(self.path)).get_connection() as db:
            rows = db.execute(clause).fetchall()
            for row in rows:
                db.execute(
                    f"DELETE FROM {self.TABLE} WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
                    (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]),
                )
        for row in rows:
            self._cache.pop(
                (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]), None
            )
        return len(rows)


def _state_to_payload(state: ConversationIntentState) -> dict[str, Any]:
    payload = asdict(state)
    payload["topic_terms"] = list(state.topic_terms or ())
    return payload


def _state_from_payload(payload: dict[str, Any]) -> ConversationIntentState:
    data = dict(payload or {})
    terms = data.get("topic_terms") or []
    return ConversationIntentState(
        topic=data.get("topic"),
        topic_terms=tuple(str(item) for item in terms),
        active_intent=data.get("active_intent"),
        slots=dict(data.get("slots") or {}),
        pending_intent=data.get("pending_intent"),
        pending_question=data.get("pending_question"),
        confidence=float(data.get("confidence") or 0.0),
        updated_at=data.get("updated_at"),
        turns=int(data.get("turns") or 0),
    )


# ─────────────────────────── 规则分类器 ───────────────────────────


class RuleIntentClassifier:
    """规则评分器：判意图、抽参数、算置信度。

    评分公式（每一项都对应 SCORE_* 常量）：

        base 0.10
        + 0.35  命中动作词            （"查"/"搜索"/"播放"）
        - 0.05  没有动作词            （多半是陈述句）
        + 0.30  命中对象词            （"天气"/"歌曲"）
        + 0.23  必填参数齐全 / -0.10 缺失

    例子：
        "查询佛山的天气" = 0.10 + 0.35 + 0.30 + 0.23 = 0.98 → 直接调用
        "天气查询"       = 0.10 + 0.35 + 0.30 - 0.10       = 0.65 → 追问（缺地点）
        "我昨天去天气局上班啦" = 0.10 - 0.05（"天气局"被 negative 排除，对象词不算）
                                                            = 0.05 → 聊天

    话题冲突（上一轮在闲聊、这一轮撞上同一个对象词）会额外扣 CONFLICT_PENALTY，
    并把结果夹进 [0.55, 0.84]：既不放弃（不落到聊天档），也不许绕过追问。
    """

    def __init__(self, policies: Iterable[IntentPolicy]) -> None:
        self.policies = tuple(policies)

    def classify(
        self,
        user_text: str,
        *,
        state: ConversationIntentState | None = None,
        assistant_text: str = "",
    ) -> IntentDecision:
        text = _compact(user_text)
        if not text or not self.policies:
            return IntentDecision(CONVERSATION_INTENT, CONVERSATION_SCORE_NEUTRAL, reason="无输入或无门控工具")

        candidates: list[tuple[float, IntentPolicy, dict[str, str], bool, bool]] = []
        for policy in self.policies:
            score, slots, object_hit, action_hit = self._score_policy(text, policy)
            candidates.append((score, policy, slots, object_hit, action_hit))

        related_intents = tuple(
            policy.intent_name
            for _, policy, _, object_hit, _ in candidates
            if object_hit or any(term in text for term in policy.negative_terms)
        )

        # 只有"对象词 + 动作词"同时命中才算在请求工具；
        # 只有一个，说明只是话题相关（聊天气局）或只是个动词（查一下东西）。
        actionable = [item for item in candidates if item[3] and item[4]]
        if not actionable:
            return self._conversation_decision(text, related_intents=related_intents)

        score, policy, slots, _, _ = max(actionable, key=lambda item: item[0])
        ambiguous = False
        reason_parts: list[str] = ["动作词+对象词"]

        if policy.required_slots:
            missing = [slot for slot in policy.required_slots if not str(slots.get(slot) or "").strip()]
            if missing:
                ambiguous = True
                reason_parts.append(f"缺参数 {'/'.join(missing)}")
            else:
                reason_parts.append("参数齐全")

        if self._conflicts_with_topic(text, policy, state, assistant_text=assistant_text):
            score = max(CONFLICT_CLAMP_LOW, min(score + CONFLICT_PENALTY, CONFLICT_CLAMP_HIGH))
            ambiguous = True
            topic = str(state.topic or "").strip() if state else ""
            reason_parts.append(f"与当前话题「{topic}」冲突")

        confidence = max(0.0, min(1.0, round(score, 4)))
        return IntentDecision(
            policy.intent_name,
            confidence,
            slots,
            "；".join(reason_parts),
            ambiguous,
            related_intents,
        )

    def _score_policy(self, text: str, policy: IntentPolicy) -> tuple[float, dict[str, str], bool, bool]:
        negative_hit = any(term in text for term in policy.negative_terms)
        object_hit = (not negative_hit) and any(term in text for term in policy.object_terms)
        action_terms = policy.action_terms or DEFAULT_ACTION_TERMS
        action_hit = any(term in text for term in action_terms)

        score = SCORE_BASE
        score += SCORE_ACTION if action_hit else SCORE_NO_ACTION
        if object_hit:
            score += SCORE_OBJECT

        slots = self.extract_slots(text, policy)
        if policy.required_slots:
            missing = [slot for slot in policy.required_slots if not str(slots.get(slot) or "").strip()]
            score += SCORE_SLOTS_MISSING if missing else SCORE_SLOTS_FULL
        return score, slots, object_hit, action_hit

    @staticmethod
    def _conflicts_with_topic(
        text: str,
        policy: IntentPolicy,
        state: ConversationIntentState | None,
        *,
        assistant_text: str = "",
    ) -> bool:
        """上一轮在闲聊、这一轮撞上同一个对象词 → 语义冲突。

        注意这里**故意忽略 negative_terms**："天气局"和"天气"都含"天气"，
        正因为字面接近，才需要追问来分辨，而不是直接当成天气查询。
        """
        if state is None or state.active_intent != CONVERSATION_INTENT:
            return False
        current = {term for term in policy.object_terms if term in text}
        previous = set(state.topic_terms or ())
        if not current & previous:
            return False
        if not str(assistant_text or "").strip():
            return True
        assistant = _compact(assistant_text)
        asks_question = any(mark in assistant for mark in ("？", "?", "吗", "呢", "什么", "怎么", "为什么", "哪"))
        return asks_question and any(term in assistant for term in policy.object_terms)

    @staticmethod
    def extract_slots(text: str, policy: IntentPolicy) -> dict[str, str]:
        """按意图抽参数。抽不到就留空，由置信度去决定要不要追问。"""
        slots: dict[str, str] = {}
        body = strip_action_prefix(text)
        for slot in policy.required_slots:
            if slot == "location":
                match = _LOCATION_PATTERN.search(body) or _LOCATION_PATTERN.search(text)
                if match:
                    slots["location"] = match.group(1).strip(" 的")
            elif slot == "query":
                # 顺序有讲究：先去标点 → 再去尾部音乐词 → 再去残留的"的"，
                # 否则"周杰伦的歌"会抽成一个带着"的"的脏 query。
                value = body.strip(" ：:，,。！？!? ")
                value = _TRAILING_MUSIC_TERMS.sub("", value).strip(" 的：:，,。！？!? ")
                slots["query"] = value
        return slots

    @staticmethod
    def _conversation_decision(
        text: str,
        *,
        related_intents: tuple[str, ...] = (),
    ) -> IntentDecision:
        if any(term in text for term in DECLARATIVE_TERMS):
            score, reason = CONVERSATION_SCORE_DECLARATIVE, "陈述句"
        elif any(term in text for term in INTERROGATIVE_TERMS):
            score, reason = CONVERSATION_SCORE_INTERROGATIVE, "疑问句"
        else:
            score, reason = CONVERSATION_SCORE_NEUTRAL, "无明确工具请求"
        return IntentDecision(CONVERSATION_INTENT, score, reason=reason, related_intents=related_intents)

    @staticmethod
    def topic_terms_for(text: str, policies: Iterable[IntentPolicy]) -> tuple[str, ...]:
        """抽取"这一句在聊什么"的对象词。

        和 _score_policy 不同：这里**不排除** negative_terms。
        "我昨天去天气局上班啦"聊的确实是"天气"这个话题，
        只是它不是天气查询——话题该记下来，动作才该被拦住。
        """
        compact = _compact(text)
        terms = {
            term
            for policy in policies
            for term in policy.object_terms
            if term in compact
        }
        return tuple(sorted(terms))


# ─────────────────────────── 闸门 ───────────────────────────

CALL = "call"
CLARIFY = "clarify"
CHAT = "chat"


@dataclass(frozen=True)
class GateDecision:
    """闸门的一次裁决：做什么 + 附带约束。

    Attributes:
        action: call（调工具）/ clarify（追问）/ chat（普通聊天）。
        decision: 分类器的原始判定，留给日志和测试核对。
        blocked_intents: 本轮要屏蔽的意图；运行时会映射为对应的工具名。
        block_all: 为 True 时本轮连工具清单都不给模型（置信度过低，纯聊天）。
        clarify_hint: 交给主模型生成追问时用的约束说明。
    """

    action: str
    decision: IntentDecision
    blocked_intents: tuple[str, ...] = ()
    block_all: bool = False
    clarify_hint: str = ""


# 意图的中文说法，只用于生成追问文案与状态摘要（给模型/人看，不参与判定）
INTENT_LABELS: dict[str, str] = {
    "weather_query": "查询实时天气",
    "search_query": "搜索网页资料",
    "music_query": "搜索歌曲",
}


class IntentGate:
    """把 IntentDecision 翻译成"接下来做什么"。

    最终是否调用工具由这里决定——模型即使参与，也只能提供措辞，不能提供决定。
    """

    def __init__(self, policies: Iterable[IntentPolicy]) -> None:
        self.policies = tuple(policies)
        self._by_name = {policy.intent_name: policy for policy in self.policies}

    def decide(self, decision: IntentDecision) -> GateDecision:
        if decision.intent == CONVERSATION_INTENT:
            # 判成聊天：不自动调工具，但工具清单照给模型——
            # "天气局是做什么的"这种问句，模型自己去搜是**正确**行为，
            # 要拦的只是"规则把'天气局'当成天气查询"这条误触发路径。
            return GateDecision(CHAT, decision, blocked_intents=decision.related_intents)

        policy = self._by_name.get(decision.intent)
        threshold = policy.min_confidence if policy else 0.85
        if decision.confidence >= threshold and not decision.ambiguous:
            return GateDecision(CALL, decision)

        if decision.confidence >= CLARIFY_THRESHOLD:
            return GateDecision(CLARIFY, decision, clarify_hint=self._hint(decision))

        # 置信度过低：连工具都不给，这轮只当聊天。
        return GateDecision(CHAT, decision, block_all=True)

    @staticmethod
    def _hint(decision: IntentDecision) -> str:
        label = INTENT_LABELS.get(decision.intent, decision.intent)
        slots = "、".join(f"{key}={value}" for key, value in decision.slots.items() if value)
        parts = [f"候选动作：{label}"]
        if slots:
            parts.append(f"已识别：{slots}")
        if decision.reason:
            parts.append(f"判定依据：{decision.reason}")
        parts.append("存在多种合理解释，需要先问清用户到底想要哪一个")
        return "；".join(parts)


def render_state_summary(state: ConversationIntentState | None) -> str:
    """把内部状态渲染成给模型看的中文摘要。

    刻意不把 JSON 原样丢给模型：模型会把字段名当成事实去复述，
    而且内部字段一旦变动，提示词就得跟着改。摘要只保留"够用"的三行。
    状态是提示，不是事实来源；最新输入永远优先。
    """
    if state is None:
        return ""
    lines: list[str] = []
    if state.topic:
        lines.append(f"当前话题：{state.topic}")
    if state.pending_intent:
        label = INTENT_LABELS.get(state.pending_intent, state.pending_intent)
        tail = f"聊「{state.topic}」" if state.topic else "闲聊"
        lines.append(f"待确认意图：用户是在请求{label}，还是继续{tail}")
    for key, value in (state.slots or {}).items():
        if value:
            lines.append(f"{key}：{value}")
    if state.pending_intent:
        lines.append("边界不清时先询问，不要擅自调用工具")
    return "\n".join(lines)
