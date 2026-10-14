"""寿命规则：一条记忆该留多久，由**用户原话**和内容性质一起定。

【为什么单独一个模块】
提炼、保存、更新、双端同步原来各算各的「这条记多久」：模型说 short_term 就一律
给一天有效期。于是「这个项目暂时先不部署，继续做本机验收」这种**阶段约束**
在两天后的维护里被清掉 —— 用户从没说过只限今天。

规则只有一条优先顺序，四处共用：

1. 用户明说了期限（「只限今天」「保留三天」）→ 按原话设到期时间；
2. 项目阶段约束（「暂不部署」「先设计，尚未实施」）→ 长期保留，等到明确替代、
   取消或结束条件成立为止，**不因为句子里出现「暂时」就一天后失效**；
3. 当天状态与情绪（「今天有点累」）→ 沿用现有短期政策（一天）；
4. 其余按内容自己声明的尺度，没声明就按长期 —— 宁可多留一条，也不能凭猜测
   让一条约束提前消失。

到了复核时间只表示「该看一眼」，不表示内容已失效：这里只负责**写下**寿命，
不做任何到期判定。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from .memory_turn import _chinese_number

LONG_TERM = "long_term"
SHORT_TERM = "short_term"
#: 短期语义的默认保质期：写下时算起，一天。落在「当日情绪」的尺度上。
DEFAULT_SHORT_RETENTION = timedelta(days=1)

#: 用户明说「只限今天」这一类。写了就按一天算。
_SAME_DAY_LIMITS = re.compile(r"(?:只限|仅限|只到|就到|限|仅)今天|今天(?:之内|内有效|有效)")
#: 「保留三天」「期限三天」「为期三天」「三天内」——数字与单位由下面的单位表换算。
_DURATION_WITH_LEAD = re.compile(
    r"(?:保留|留|期限|限|为期|有效期|维持)\s*([0-9零〇一二三四五六七八九十百千万亿两]+)\s*"
    r"(天|日|周|星期|礼拜|个月|月|年)"
)
_DURATION_WITH_TAIL = re.compile(
    r"([0-9零〇一二三四五六七八九十百千万亿两]+)\s*(天|日|周|星期|礼拜|个月|月|年)\s*(?:之内|内|以内)"
)
_UNIT_DAYS = {
    "天": 1, "日": 1, "周": 7, "星期": 7, "礼拜": 7, "个月": 30, "月": 30, "年": 365,
}
#: 阶段约束的措辞：说的是「现在到某个节点之前先这样」，不是「今天怎么样」。
_PHASE_MARKERS: tuple[str, ...] = (
    "暂不", "暂时", "先不", "尚未", "还没", "还没有", "现阶段", "当前阶段",
    "眼下", "暂时先", "先", "继续", "往后", "接下来",
)
#: 阶段约束说的**那件事**。没有这类事项名词就不是项目阶段约束。
_MATTER_NOUNS: tuple[str, ...] = (
    "项目", "部署", "上线", "实施", "设计", "开发", "发布", "投产", "验收",
    "排期", "迁移", "改造", "方案", "计划", "任务", "进度", "工程", "测试",
    "联调", "评审", "版本", "架构", "日志", "流程", "规则", "策略", "原则",
)
#: 当天状态的措辞（情绪、身体感受、今天的当下情况）。这些照旧按短期处理。
#: 刻意不放「现在」「刚才」：它们在长期原则里同样常见，按词推断会把长期偏好误杀。
_SAME_DAY_MARKERS: tuple[str, ...] = (
    "今天", "今日", "今晚", "这会儿", "心情", "有点累", "很累",
    "累了", "困", "饿", "烦", "难过", "开心", "高兴", "生气", "焦虑", "烦躁",
    "头疼", "不舒服", "情绪",
)


@dataclass(frozen=True)
class Lifetime:
    """一条记忆的寿命判定结果。"""

    horizon: str
    expires_in: timedelta | None
    basis: str

    @property
    def is_short(self) -> bool:
        return self.horizon == SHORT_TERM

    def as_metadata(self) -> dict[str, str]:
        return {"horizon": self.horizon, "lifetime_basis": self.basis}


def explicit_duration(text: str) -> tuple[timedelta, str] | None:
    """用户原话里明说的期限。读不出返回 ``None``。"""
    raw = str(text or "")
    if not raw:
        return None
    if _SAME_DAY_LIMITS.search(raw):
        return DEFAULT_SHORT_RETENTION, "用户明说只限今天"
    for pattern in (_DURATION_WITH_LEAD, _DURATION_WITH_TAIL):
        for match in pattern.finditer(raw):
            amount = _parse_amount(match.group(1))
            unit = _UNIT_DAYS.get(match.group(2))
            if amount is None or not unit or amount <= 0:
                continue
            return timedelta(days=amount * unit), f"用户明说期限：{match.group(0).strip()}"
    return None


def _parse_amount(raw: str) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    return _chinese_number(text)


def is_phase_constraint(text: str) -> bool:
    """这条内容是不是**阶段约束**：说到某件事现在先怎么样。

    两个条件同时成立才算：讲的是某件事（项目、部署、实施…），而且用了阶段性措辞
    （暂不、尚未、先…）。「今天有点累」没有事项名词，「天气不错」也没有，
    都不会被误判成阶段约束。
    """
    raw = str(text or "")
    if not raw:
        return False
    if not any(noun in raw for noun in _MATTER_NOUNS):
        return False
    from .memory_extract import _expresses_explicit_decision
    if not _expresses_explicit_decision(raw) and not re.search(r'暂时不|先(?:设计|开发|验收|测试)|继续(?:做|本机验收|联调|验收)', raw):
        return False
    return any(marker in raw for marker in _PHASE_MARKERS)


def is_same_day_state(text: str) -> bool:
    """这条内容是不是**当天状态**（情绪、身体感受、今天的心情）。"""
    raw = str(text or "")
    if not raw:
        return False
    if any(marker in raw for marker in _SAME_DAY_MARKERS):
        return True
    # 情绪词不带「今天」时也按当天状态：短期政策的对象就是这一类。
    return any(word in raw for word in ("心情", "情绪", "很累", "有点累"))


def resolve_lifetime(
    content: str,
    *,
    kind: str = "",
    declared: str = "",
    source_text: str = "",
    default_short: timedelta = DEFAULT_SHORT_RETENTION,
) -> Lifetime:
    """一条内容该留多久。``source_text`` 是用户自己的原话（有则优先看它）。"""
    body = str(content or "")
    said = str(source_text or "")
    # 1. 用户明说的期限最优先：他说三天就三天，说只限今天就一天。
    #    优先看用户原话；原话没有时看内容本身（模型可能把期限写进了正文）。
    for text, where in ((said, '用户原话'),) if said else ((body, '程序内容'),):
        found = explicit_duration(text)
        if found is not None:
            duration, basis = found
            return Lifetime(SHORT_TERM, duration, f"{basis}（{where}）")
    # 2. 阶段约束长期保留：不能因为「暂时」就一天后失效，也不能因为同一条内容
    #    里还提了一句当天状态（「项目先不部署，继续联调，今天有点累」）就降级成
    #    当天状态 —— 那样阶段约束会被顺带清掉。用户明说「只限今天」由第 1 条管，
    #    这里的先后顺序就是规则文档写的顺序。
    if is_phase_constraint(said or body):
        return Lifetime(LONG_TERM, None, "项目阶段约束，保留到被明确替代或取消")
    if said and re.search(r'分析一下|再分析|帮我分析|看一下|查一下|解释一下|比较一下|建议.{0,8}分析', said):
        return Lifetime(SHORT_TERM, default_short, '一次分析或查询请求')
    if is_same_day_state(said or body) and not re.search(r'习惯|喜欢|偏好|一律|原则|长期|每次|总是', said or body):
        return Lifetime(SHORT_TERM, default_short, '当天状态，按一天保留')
    # 3. 内容自己声明长期时按长期：长期原则里出现「今天的事情今天做完」很正常，
    #    按关键词推断会把一条原则误杀成当日状态。
    if str(declared or "").strip() == LONG_TERM:
        return Lifetime(LONG_TERM, None, "内容自己声明为长期")
    # 4. 当天状态照旧短期。
    if is_same_day_state(body):
        return Lifetime(SHORT_TERM, default_short, "当天状态，按现有短期政策处理")
    # 5. 其余按声明；没声明就长期。
    if str(declared or "").strip() == SHORT_TERM:
        return Lifetime(SHORT_TERM, default_short, "内容自己声明为短期")
    return Lifetime(LONG_TERM, None, "没有明确期限，按长期保留")
