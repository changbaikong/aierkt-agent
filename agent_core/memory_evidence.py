"""统一依据检查：一条内容里的**每一项实际信息**都要找得到出处。

【为什么单独一个模块】
新建、纠正、替代、合并，以及用新候选替代旧记录，原来是四段各自判断的代码。
判断迟早会不一致，而不一致的那一处就是错误事实被写进库的地方。这里只留一套
判据，四个入口都调它。

【什么算依据】
只有两类：用户自己说过的话（本轮原话或必要历史），和**成功**的工具回执。
助手建议、模型解释、反复自检都不算 —— 它们既不是「用户采纳了」，也不是
「这件事做完了」。

【程序先判，判不了才复核】
数值按值比（三千 = 3000）、普通词里的数词不算值（「一律」的「一」）、
否定与条件留在原句里一起看。程序确实分不清「只是换了个说法」还是「多了一件
新事实」时，结果记成 ``uncertain`` 交给有界复核；**两种结果都不落库**，
不能只凭模型自述把内容写进去。

【整条不应用，不切碎保存】
一条内容里只要有一项没出处，这一条就整体暂不应用，不自动删掉那一部分再存 ——
删改后的内容没人复核过，等于换个方式让错误事实进库。
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any, Iterable
from .memory_binding import bind_evidence, fingerprint, same_subject

from .memory_turn import (
    changed_value_tokens,
    clauses,
    content_units,
    value_entries,
    value_evidenced,
    value_tokens,
)

#: 结论类别：这些说的是「事情走到哪一步了」，与「用户选了哪个方案」是两件事。
REVIEW_RESULT = "review_result"
IMPLEMENTATION_RESULT = "implementation_result"
CONDITION = "condition"
REASON = "reason"
SCOPE = "scope"
VALUE = "value"
UNCERTAIN = "uncertain"

#: 有依据、无依据、不确定 —— 复核只允许返回这三种之一。
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNCERTAIN_VERDICT = "uncertain"

#: 规则版本：判据变了就 +1，好让旧回执能被识别为过期。
RULE_VERSION = "model-led-2"

#: 审批／评审结论的字面。
_APPROVAL_MARKERS: tuple[str, ...] = (
    "已获批准", "获得批准", "已获批复", "评审通过", "通过评审", "审核通过",
    "通过审核", "验收通过", "批准", "审批", "核准", "获准", "签批", "批复", "已通过",
)
#: 「名词 + 结果词」同句出现，也算一条审批／评审结论。
_APPROVAL_NOUNS: tuple[str, ...] = (
    "评审", "审批", "核准", "审核", "批准", "验收", "审计", "评估", "评审会",
)
_APPROVAL_RESULTS: tuple[str, ...] = (
    "通过", "批准", "核准", "获准", "同意", "放行", "签批", "批复", "签字",
)
#: 「取得式」措辞：它后面跟的「同意／认可」才是一条审批结论。
_APPROVAL_GAIN_MARKERS: tuple[str, ...] = (
    "已获", "获得", "得到", "征得", "已征得", "取得", "拿到", "经过", "经",
)
#: 能给出「同意／认可」的主体。
_APPROVAL_SOURCES: tuple[str, ...] = (
    "上级", "领导", "甲方", "客户", "对方", "公司", "老板", "评审会", "委员会",
    "董事会", "评审组", "验收组", "监理", "主管部门", "负责人",
)
#: 单独出现时只是语气词的应答词：要跟取得式或主体一起才算结论。
_CONSENT_WORDS: tuple[str, ...] = ("同意", "认可")
_CONSENT_WINDOW = 6
#: 实施／执行结果的字面。
_EXECUTION_NOUNS: tuple[str, ...] = (
    "实施", "执行", "上线", "部署", "投产", "演练", "回滚", "发布", "落地",
    "迁移", "开工", "交付", "启用", "验收", "施工", "改造",
)
_EXECUTION_RESULTS: tuple[str, ...] = (
    "完成", "成功", "已实施", "已经实施", "已上线", "已部署", "已投产",
    "已发布", "已落地", "已启用", "已迁移", "已开工", "已交付", "已验收",
    "做完", "搞定", "完工", "结束", "完毕", "就绪", "生效", "done",
)
#: 条件：带上它，结论只在那个前提下成立。
_CONDITION_MARKERS: tuple[str, ...] = (
    "如果", "若", "要是", "除非", "只有", "前提是", "条件是", "取决于",
    "不忙的话", "方便的话", "看情况", "一旦",
)
#: 原因：带上它，等于替用户说了一个动机。
_REASON_MARKERS: tuple[str, ...] = (
    "因为", "由于", "原因是", "之所以", "是为了", "为了",
)
#: 扩大适用范围的措辞：用户只说「今天这样」，内容里不能变成「以后全部」。
_SCOPE_EXPANSIONS: tuple[str, ...] = (
    "以后", "今后", "长期", "永久", "始终", "每次", "一直", "所有", "全部", "统统",
)
#: 否定词：紧挨在结论前面时说明「这件事没发生」，不能当依据。
_NEGATIONS: tuple[str, ...] = (
    "尚未", "还没", "还没有", "没有", "未获", "未予", "未能", "未曾", "未",
    "没", "不", "别", "非", "无", "取消", "否决", "放弃", "暂不", "先不",
    "不予", "拒绝", "驳回",
)
#: 否定窗口：结论词前面这么多个字里出现否定词就算被否定。
_NEGATION_WINDOW = 5

#: 「同一个选择的另一种说法」。换词不是新事实：模型把「用」写成「使用」、
#: 「选择」写成「决定」时，整条正当替代不该被拒。表里按同一件事分组，
#: 组内成员互相替换算改写；换了组（比如「改用 A」→「改用 B」）不算。
_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("改用", "改为", "换成", "变更为", "调整为", "切换为", "转用", "改用为"),
    (
        "用", "使用", "采用", "选用", "选择", "决定", "决策", "选定", "敲定",
        "定为", "取用", "使用方案",
    ),
    ("不再", "不用", "不要", "别用", "停止使用", "放弃使用", "取消"),
    ("继续", "仍旧", "依然", "照旧", "保持", "维持"),
    ("暂时", "临时", "当前阶段", "现阶段", "眼下"),
    ("尚未", "还没", "还没有", "未", "还没开始"),
    ("先", "首先", "暂时先", "优先"),
    ("本机", "本地", "机器上", "自己机器"),
    ("继续", "再"),
)

#: 中文数词。
_CN_NUMERAL_CHARS = frozenset("零〇一二三四五六七八九十百千万亿两")


@dataclass(frozen=True)
class EvidenceItem:
    """一条内容里的一项实际信息，以及它属于哪一类。"""

    category: str
    text: str
    clause: str
    verdict: str = UNSUPPORTED
    quote: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "item": self.text,
            "clause": self.clause,
            "verdict": self.verdict,
            "quote": self.quote,
            "reason": self.reason,
        }


def _negated(clause: str, marker: str, *, window: int = _NEGATION_WINDOW) -> bool:
    """结论词前面紧挨着否定词时，这一句说的是「没发生」。"""
    at = clause.find(marker)
    if at < 0:
        return False
    before = clause[max(0, at - window) : at]
    return any(word in before for word in _NEGATIONS)


def _approval_hit(clause: str) -> str:
    """这一句里的审批／评审结论片段；没有返回空串。

    三种说法都算：
    1. 固定字面（已获批准、评审通过、审核通过…）；
    2. 「审批名词 + 结果词」（验收通过、评审同意…）；
    3. **取得式 + 同意／认可**（已获上级同意、甲方认可、征得客户同意…）——
       「同意」单看是语气词，跟着「已获／征得」或一个主体名词时才是结论。
       第 3 条不能只看「同意」两个字：用户说「同意以后回答先说结论」是同一句
       里对**另一件事**的应答，把它当成「已获同意」的依据正是要防的张冠李戴。
    """
    for marker in _APPROVAL_MARKERS:
        if marker in clause and not _negated(clause, marker):
            return marker
    noun = next((item for item in _APPROVAL_NOUNS if item in clause), "")
    if noun:
        for result in _APPROVAL_RESULTS:
            if result in clause and not _negated(clause, result):
                return noun + result
    for consent in _CONSENT_WORDS:
        at = clause.find(consent)
        if at < 0 or _negated(clause, consent):
            continue
        before = clause[max(0, at - _CONSENT_WINDOW) : at]
        if any(gain in before for gain in _APPROVAL_GAIN_MARKERS) or any(
            source in clause for source in _APPROVAL_SOURCES
        ):
            return consent
    return ""


def _clause_states(category: str, clause: str) -> tuple[str, str]:
    """这一句有没有给出该类别的结论。返回 ``(结论片段, 空串)`` 或 ``("", "")``。"""
    if category == REVIEW_RESULT:
        hit = _approval_hit(clause)
        return (hit, "") if hit else ("", "")
    if category == IMPLEMENTATION_RESULT:
        markers, nouns, results = (), _EXECUTION_NOUNS, _EXECUTION_RESULTS
    elif category == CONDITION:
        markers, nouns, results = _CONDITION_MARKERS, (), ()
    elif category == REASON:
        markers, nouns, results = _REASON_MARKERS, (), ()
    elif category == SCOPE:
        markers, nouns, results = _SCOPE_EXPANSIONS, (), ()
    else:
        return "", ""
    for marker in markers:
        if marker in clause and not _negated(clause, marker):
            return marker, ""
    noun = next((item for item in nouns if item in clause), "")
    if not noun:
        return "", ""
    for result in results:
        if result in clause and not _negated(clause, result):
            return noun + result, ""
    return "", ""


def _shares_units(left: str, right: str) -> bool:
    """两句有没有共同实义片段 —— 用来确认依据说的是同一件事。"""
    if not left or not right:
        return False
    return bool(content_units(left) & content_units(right))


def evidence_text_of(item: EvidenceItem) -> str:
    return item.text or item.clause


def content_items(text: Any) -> tuple[EvidenceItem, ...]:
    """一段文字里的**结论类信息**：评审结果、实施结果、条件、原因、适用范围。"""
    found: list[EvidenceItem] = []
    seen: set[tuple[str, str]] = set()
    for clause in clauses(text):
        for category in (REVIEW_RESULT, IMPLEMENTATION_RESULT, CONDITION, REASON, SCOPE):
            marker, _ = _clause_states(category, clause)
            if not marker:
                continue
            key = (category, marker)
            if key in seen:
                continue
            seen.add(key)
            found.append(EvidenceItem(category=category, text=marker, clause=clause))
    return tuple(found)


def new_content_items(new_text: Any, current_text: Any) -> tuple[EvidenceItem, ...]:
    """新内容**多出来**的结论类信息：旧内容里已经有的那一项不用再核。

    只跳过**同一项**（同一类里同一个结论字面），不跳过整个类别：旧内容写着
    「验收通过」，新内容另加一句「评审通过」时，那是**另一件事的结论**，
    照样要各自找到出处。
    """
    current = str(current_text or "")
    found: list[EvidenceItem] = []
    for item in content_items(new_text):
        if item.text and item.text in current:
            continue
        # 同一句结论原样保留时不算新事实。
        if item.clause and item.clause in current:
            continue
        found.append(item)
    return tuple(found)


def evidence_gaps(
    new_text: Any,
    current_text: Any,
    *,
    evidence_clauses: Iterable[Any],
    tool_clauses: Iterable[Any] = (),
    item_text: str = "",
    check_values: bool = True,
    check_new_material: bool = True,
    context_texts: Iterable[Any] = (),
) -> tuple[EvidenceItem, ...]:
    """逐项核对：返回新内容里**没有出处**的那些项（空 = 每一项都有着落）。

    ``evidence_clauses`` 是**用户原话**（调用方已按事项绑定好的那几句）；
    ``tool_clauses`` 是成功工具回执里对得上这件事的部分。
    助手建议与模型推断不在这里出现 —— 它们本来就不该被当成依据。

    ``context_texts`` 是**这一轮用户说过的全部原话**，只用来判断「这个词用户
    到底说没说过」：用户先说事项、再补一句状态（「尚未实施，也没有评审结论」）时，
    那句话不在事项绑定范围内，但模型照抄它并不是编造 —— 依据范围与「说没说过」
    是两件事，各按各的判。

    核对顺序：先数值（按值比大小），再结论类信息（评审、实施、条件、原因、
    适用范围），最后是既不属于数值、也不属于已知类别、又不能确认只是换词的新
    材料 —— 这一类记 ``uncertain``，同样不落库。``check_new_material=False``
    用于**新建**记录：那条没有「旧内容」可比，逐字比对会把正常提炼出的新记录
    全判成新事实，这时只核数值与结论类信息。
    """
    raw_new = str(new_text or "")
    raw_old = str(current_text or "")
    if not raw_new.strip():
        return ()
    user_clauses = tuple(str(text or "") for text in (evidence_clauses or ()) if str(text or "").strip())
    tool_texts = tuple(str(text or "") for text in (tool_clauses or ()) if str(text or "").strip())
    context = tuple(str(text or "") for text in (context_texts or ()) if str(text or "").strip())
    all_evidence = (*user_clauses, *tool_texts)
    resolved_corrections = _quoted_corrections(user_clauses)
    all_evidence += resolved_corrections

    gaps: list[EvidenceItem] = []
    for source in user_clauses:
        if not same_subject(source, raw_new):
            continue
        condition = next((word for word in _CONDITION_MARKERS if word in source), '')
        if condition and not any(word in raw_new for word in _CONDITION_MARKERS):
            gaps.append(EvidenceItem(CONDITION, condition, source, reason='原文条件被遗漏'))
        for negative in re.finditer(r'(暂不|先不|不再|尚未|还没|没有|未|不)(?:在[^，。]{0,8})?(部署|实施|执行|上线|调整|发布|迁移|启用)', source):
            verb = negative.group(2)
            if verb in raw_new and not re.search(r'(暂不|先不|不再|尚未|还没|没有|未|不|暂缓|暂停)(?:在[^，。]{0,8})?' + verb, raw_new):
                gaps.append(EvidenceItem(IMPLEMENTATION_RESULT, negative.group(), source, reason='原文否定状态被遗漏或反转'))

    for claim in clauses(raw_new):
        sources = _claim_sources(claim, all_evidence)
        bare_correction = bool(raw_old and any(re.search(r'(不是|并非)[0-9零〇一二三四五六七八九十百千万亿两.,，]+(?:元|块|人)?$', s)
                                              and (same_subject(s, raw_old) or re.match(r'^(不是|并非)', s))
                                              and any(value_evidenced(raw_old, t) for t in value_tokens(s)) for s in user_clauses))
        if bare_correction:
            sources += tuple(s for s in user_clauses if re.fullmatch(r'(是|应该是|改成)[0-9零〇一二三四五六七八九十百千万亿两.,，]+(?:元|块|人)?', s))
        normal = _literal_claim(claim)
        # A negative cannot be removed just because all remaining letters occur
        # in the quote. Conversely, adding a negative reverses a positive fact.
        for source in sources:
            if _without_negations(normal) == _without_negations(_literal_claim(source)) and normal != _literal_claim(source):
                if bool(_NEGATIVE_PATTERN.search(normal)) != bool(_NEGATIVE_PATTERN.search(_literal_claim(source))):
                    gaps.append(EvidenceItem('polarity', claim, claim, reason='原文肯定或否定被反转'))
        if check_values:
            for token in value_tokens(claim):
                if not value_entries(token):
                    continue
                # An unchanged complete old claim may be carried forward. Old
                # values elsewhere never authorize a new field/value pairing.
                if any(normal == _literal_claim(old) for old in clauses(raw_old)):
                    continue
                if any(value_evidenced(text, token) for text in sources):
                    continue
                gaps.append(EvidenceItem(VALUE, token, claim, reason=f'数值「{token}」在该事项原话或成功回执里找不到'))

    for item in new_content_items(raw_new, raw_old):
        support = _find_support(item, user_clauses, tool_texts, item_text=item_text)
        if support:
            continue
        gaps.append(
            EvidenceItem(
                category=item.category,
                text=item.text,
                clause=item.clause,
                verdict=UNSUPPORTED,
                reason=_missing_reason(item),
            )
        )

    if check_new_material:
        gaps.extend(
            _uncertain_items(
                raw_new,
                raw_old,
                evidence_clauses=(*user_clauses, *resolved_corrections),
                context_texts=context,
                skip_clauses=frozenset(item.clause for item in gaps),
                skip_tokens=frozenset(item.text for item in gaps if item.category == VALUE),
            )
        )
    return tuple(gaps)


def _missing_reason(item: EvidenceItem) -> str:
    if item.category == REVIEW_RESULT:
        return f"「{item.text}」是评审／审批结论，用户原话和成功回执里都没有这件事"
    if item.category == IMPLEMENTATION_RESULT:
        return f"「{item.text}」是实施／执行结果，用户原话和成功回执里都没有这件事"
    if item.category == CONDITION:
        return f"「{item.text}」这个前提条件在用户原话里没有"
    if item.category == REASON:
        return f"「{item.text}」这个原因在用户原话里没有"
    if item.category == SCOPE:
        return f"「{item.text}」把适用范围放大了，用户原话里没有这个范围"
    return f"「{item.text}」在用户原话里找不到"


def _approval_kind(clause: str) -> str:
    """这一句说的审批是**哪一种**：评审／审批／验收…，或者「同意／认可」这一族。

    用途是防止「旧内容写着验收通过、新内容加一句评审通过」被判成有出处 ——
    两句只共用「通过」两个字，说的却是两件事。
    """
    noun = next((item for item in _APPROVAL_NOUNS if item in clause), "")
    if noun:
        return noun
    for consent in _CONSENT_WORDS:
        if consent in clause and not _negated(clause, consent):
            return consent
    return _approval_hit(clause)


def _subject_units(text: str, conclusion: str) -> set[str]:
    """一句话里**除去结论本身和给出结论的主体**之后，还剩下的实义片段。

    用途是把「说的是哪一件事」和「审批措辞」分开：用户说「客户提的那个报表
    格式我同意」时，剥掉「同意」和主体「客户」之后，剩下的「报表格式」才是
    这句话真正在说的事 —— 它跟「机房搬迁方案」没有共同片段，就不能给
    「已获客户同意」当依据。
    """
    stripped = str(text or "")
    for word in (conclusion, *_CONSENT_WORDS, *_APPROVAL_GAIN_MARKERS, *_APPROVAL_SOURCES):
        if word:
            stripped = stripped.replace(word, "")
    return content_units(stripped)


#: 指代当前这件事的说法：「这事甲方已经认可」里的事项名在上一句，指代词把它接过来。
_DEICTIC_SUBJECT: tuple[str, ...] = (
    "这事", "这件事", "此事", "该事", "该事项", "这项", "此项", "该方案",
    "这个方案", "该安排", "这个安排", "该决定",
)


def _find_support(
    item: EvidenceItem,
    user_clauses: tuple[str, ...],
    tool_texts: tuple[str, ...],
    *,
    item_text: str = "",
) -> str:
    """这一项的出处（原话或回执原文）；空串表示没有。"""
    claim_marker, _ = _clause_states(item.category, item.clause)
    claim_subject = _subject_units(item.clause, claim_marker)
    everything = (*user_clauses, *tool_texts)
    turn_mentions_item = bool(
        item_text
        and any(content_units(clause) & content_units(item_text) for clause in everything)
    )
    for clause in everything:
        marker, _ = _clause_states(item.category, clause)
        if not marker:
            continue
        if item.category == REVIEW_RESULT:
            kinds = {_approval_kind(clause), _approval_kind(item.clause)}
            explicit = {kind for kind in kinds if kind in _APPROVAL_NOUNS}
            if len(explicit) > 1:
                # 两边都写明了审批种类，而且不是同一种（评审 vs 验收）：
                # 都是结论，但不是同一件事，不能互为依据。
                continue
        if claim_subject:
            # 结论句自己说了是哪件事：依据句必须也说到**那件事**，
            # 光共用「同意／通过」两个字不算。
            if not (claim_subject & _subject_units(clause, marker)):
                continue
        elif item_text:
            # 裸结论（「甲方认可」只带主体，没带事项）：依据句要么自己提到这条
            # 记录说的事，要么用指代词（「这事甲方已经认可」）接着说同一件事 ——
            # 后者要求这一段原话里确实出现过这件事。
            mentions = bool(content_units(clause) & content_units(item_text))
            deictic = any(word in clause for word in _DEICTIC_SUBJECT)
            if not (mentions or (deictic and turn_mentions_item)):
                continue
        if _shares_units(clause, item.clause) or _shares_units(clause, item_text):
            return clause
    return ""


def _synonym_chars(text: str, known_texts: Iterable[str]) -> set[str]:
    """这段文字里**属于同一个选择的另一种说法**的字。

    只在「已知文本用过这一组里的某个说法、这段文字用了同组另一个说法」时成立：
    组内换词是改写，不是新事实（「用 B」→「使用 B」「选择 B」）。
    """
    known = [str(item or "") for item in known_texts]
    covered: set[str] = set()
    for group in _SYNONYM_GROUPS:
        if not any(member in text for member in group):
            continue
        if not any(member in one for member in group for one in known):
            continue
        for member in group:
            if member in text:
                covered.update(member)
    return covered


def _status_of(category: str, clause: str) -> str:
    """这一句对某个类别给出的状态：``yes``（已完成／已通过）、``no``（尚未）、空。

    否定也要认出来：「尚未实施」和「还没有实施」是同一个状态，只是换了说法；
    把它当成「没有依据的新事实」等于因为换词就拒绝一条正当记录。
    「同意／认可」这类结论同样算状态：用户说「已经征得客户同意」时，
    续写句带着这个状态，不能被判成「没提到结论」而漏出证据范围。
    """
    if category == REVIEW_RESULT:
        hit = _approval_hit(clause)
        if hit:
            return "yes"
        return ""
    if category != IMPLEMENTATION_RESULT:
        return ""
    nouns, results = _EXECUTION_NOUNS, _EXECUTION_RESULTS
    noun = next((item for item in nouns if item in clause), "")
    if not noun:
        return ""
    at = clause.find(noun)
    before = clause[max(0, at - 4) : at]
    if any(word in before for word in _NEGATIONS):
        return "no"
    if any(result in clause and not _negated(clause, result) for result in results):
        return "yes"
    return ""


def _status_matched(clause: str, evidence_clauses: Iterable[str]) -> bool:
    """这一句说的状态，依据里有没有同一个状态（同一个类别，同一种极性）。"""
    for category in (REVIEW_RESULT, IMPLEMENTATION_RESULT):
        status = _status_of(category, clause)
        if not status:
            continue
        if any(_status_of(category, other) == status for other in evidence_clauses):
            return True
    return False


def states_status(clause: str) -> bool:
    """这一句有没有说「已完成／尚未完成／已通过／尚未通过」这类状态。"""
    text = str(clause or "")
    return any(
        _status_of(category, text) for category in (REVIEW_RESULT, IMPLEMENTATION_RESULT)
    )


def _is_content_char(char: str) -> bool:
    """算不算「有实义的字」：汉字或拉丁字母。

    阿拉伯数字**不算**：数值由值比对那一层按「数值 + 量词」判断，这里再按字判
    会把「三千」和「3000」这种等值写法当成新内容。
    """
    return "\u3400" <= char <= "\u9fff" or char.isalpha()


#: 本身不承载新事实的字：助词、连词、介词、否定词与常见量词。
#: 「预算为三千元」里的「为」「元」不是新事实，「由 A 改为 B」里的「由」也不是。
#: 数词（一二三…十百千万亿）**不在**这一组里：单个数字也可能是一个被换掉的值。
_FUNCTION_CHARS = frozenset(
    "为是的了在和与及或也都就才还又会能要把被给对从到并且而等非无未不有"
    "上下里外前后中间个元人天次条件套份页米克吨升块"
    "由于将跟同向往让使令以自随按依着过再很更最太需该此"
)
#: 单个汉字数词也算「一个新值」：原话说「周一」、内容写「周三」必须报出来。
_CN_NUMERALS = frozenset("零〇一二三四五六七八九十百千万亿两")
#: 复述与评价用词：模型把「用户更正了此前的说法」写进正文时，这些字不是新事实。
_COMMENTARY_WORDS: tuple[str, ...] = (
    "助手", "要求", "同意",
    "用户", "更正", "修正", "此前", "之前", "之后", "原来", "原话", "所说",
    "说法", "表述", "记录", "表示", "说明", "描述", "明确", "指出", "提到",
    "认为", "强调", "确认", "需要", "应该", "已经", "正确", "错误", "不对",
    "依然", "仍然", "依旧", "当前", "目前", "本次", "这次", "后续", "原有",
    "旧值", "新值", "内容", "状态", "信息", "替代", "取代", "推翻", "覆盖",
    "实际", "事实", "情况", "其实", "依据", "判断", "标注",
)
_COMMENTARY_CHARS = frozenset("".join(_COMMENTARY_WORDS))


#: 「值位」引导词：跟在它后面的单个字是取值（「方案丙」「第 3 版」），
#: 换掉一个字母/序号就是换了一个新值，必须报出来。
_VALUE_POSITION_LEADS: tuple[str, ...] = (
    "方案", "计划", "版本", "编号", "选项", "标的", "型号", "第",
)


def _is_value_position(clause: str, index: int) -> bool:
    before = clause[max(0, index - 2) : index]
    return any(lead in before for lead in _VALUE_POSITION_LEADS)


def _unknown_runs(clause: str, known_chars: set[str]) -> list[str]:
    """这一句里**依据里完全没有的实义内容**。

    助词、连词与复述词不算（「预算为三千元」的「为」「元」、「用户更正」的
    「更正」）。剩下的一律记账：

    - 连成串的（「夜间低峰期」）直接算一件事；
    - 单独一个字的要分清：**取值位**上的那一个（「方案丙」的「丙」）、
      字母编号（「方案 C 的 C」）与数词都算换了一个新值；其余单个字
      （「改为」的「改」、「放在」的「放」）只是措辞，不单独报 ——
      否则正常改写会被一个动词字拦下。
    """
    for word in sorted(_COMMENTARY_WORDS, key=len, reverse=True):
        clause = clause.replace(word, ' ' * len(word))
    runs: list[str] = []
    current: list[str] = []
    start = 0
    for index, char in enumerate(clause):
        if _is_content_char(char) and char not in known_chars:
            if char not in _FUNCTION_CHARS:
                if not current:
                    start = index
                current.append(char)
                continue
        if current and _run_counts(clause, start, current):
            runs.append("".join(current))
        current = []
    if current and _run_counts(clause, start, current):
        runs.append("".join(current))
    return runs


def _run_counts(clause: str, start: int, run: list[str]) -> bool:
    """这一段生字算不算「原话里没有的内容」。"""
    if len(run) >= 2:
        return True
    char = run[0]
    if char in _CN_NUMERALS or (char.isascii() and char.isalpha()):
        return True
    # 换词表里的词（「改为」「决定」「使用」）本身是措辞：它们的首字不算新值。
    if any(member.startswith(char) and member in clause for group in _SYNONYM_GROUPS for member in group):
        return False
    return _is_value_position(clause, start)


_NEGATIVE_PATTERN = re.compile(r'尚未|还没有|还没|没有|暂不|先不|不再|不要|不用|不|未|没|别|无|非|拒绝|否决|取消|放弃')


def _without_negations(text):
    return _NEGATIVE_PATTERN.sub('', text)


def _quoted_corrections(quotes):
    """Resolve only adjacent explicitly named numeric corrections, no guessing.

    The returned text is used for comparison, never persisted as an original
    quote; receipts retain both complete original source clauses.
    """
    result = []
    numeral = r'[0-9零〇一二三四五六七八九十百千万亿两.,，]+(?:元|块|人)?'
    for before, after in zip(quotes, quotes[1:]):
        old = re.fullmatch(r'(.+?)(?:不是|并非)(' + numeral + r')', before)
        new = re.fullmatch(r'(?:是|应该是|改成)(' + numeral + r')', after)
        if old and new:
            result.append(old.group(1) + new.group(1))
    return tuple(result)


def _literal_claim(text):
    """A deliberately small equivalence vocabulary; preserves order and polarity.

    This is not a language inference engine. Unlisted paraphrases go to review,
    which can regenerate a faithful quote and must pass this gate again.
    """
    raw = str(text or '')
    for token in sorted(value_tokens(raw), key=len, reverse=True):
        entries = value_entries(token)
        if entries:
            raw = raw.replace(token, str(entries[0][0]))
    raw = re.sub(r'^(用户|我)(?:明确)?(?:表示|说|要求)?', '', raw)
    raw = re.sub(r'^(这个|该|这项)(?=项目|方案|计划|决定)', '', raw)
    raw = re.sub(r'^(这条约束|这条|该项)(?:保留|有效期)[0-9零〇一二三四五六七八九十百千万亿两]+天[:：]', '', raw)
    raw = re.sub(r'^(建议|请|麻烦)', '', raw)
    raw = re.sub(r'(可以吗|好吗|是否)$', '', raw)
    # Replace longest phrases together, never delete their individual letters.
    aliases = {
        '还没有':'尚未', '还没开始':'尚未', '还没':'尚未',
        '暂时不':'暂不', '暂时先不':'暂不', '先不':'暂不', '暂缓':'暂不',
        '暂时':'暂时', '临时':'暂时',
        '改用':'采用', '改为':'采用', '换成':'采用', '变更为':'采用', '调整为':'采用', '切换为':'采用',
        '使用方案':'采用方案', '使用':'采用', '选用':'采用', '选择':'采用', '决定':'采用', '用':'采用',
        '本地':'本机', '仍旧':'继续', '依然':'继续', '照旧':'继续', '保持':'继续', '维持':'继续',
        '人民币':'元', '块':'元',
    }
    pattern = '|'.join(map(re.escape, sorted(aliases, key=len, reverse=True)))
    raw = re.sub(pattern, lambda m: aliases[m.group()], raw)
    raw = re.sub(r'采用(?:采用)+', '采用', raw)
    raw = re.sub(r'^(目前|当前|已经)', '', raw)
    raw = raw.replace('已经', '已')
    return re.sub(r'[\s，,。；;！!？?、]|[为是的了]', '', raw)


def _claim_sources(claim, evidence):
    from .memory_binding import subject_text
    available = [s for text in evidence for s in clauses(text) if same_subject(s, claim)]
    scored = [(SequenceMatcher(None, subject_text(claim), subject_text(s)).ratio(), s) for s in available]
    best = max((score for score, _ in scored), default=0)
    return tuple(s for score, s in scored if score >= best - .05)


def _uncertain_items(
    new_text: str,
    current_text: str,
    *,
    evidence_clauses: tuple[str, ...],
    context_texts: tuple[str, ...] = (),
    skip_clauses: frozenset[str] = frozenset(),
    skip_tokens: frozenset[str] = frozenset(),
) -> tuple[EvidenceItem, ...]:
    """新内容里**没有任何出处、也不能确认只是换了个说法**的那些句子。

    这一类不一定错（可能只是换了说法），但程序判不了，一律记 ``uncertain``：
    不落库，等有界复核或下一条新证据，不靠猜。

    判定按**字**做，不按二元组：换说法会把边界上的二元组也改掉（「改用方案B」
    里「案改」「用方」这类），按字判既不会误杀改写，也不会放过真正的新内容。
    一句话算「有着落」需要满足其中一条：

    1. 所有实义字都在旧内容或依据里出现过（纯改写）；
    2. 没出现过的字全部落在这张「同一个选择的另一种说法」表覆盖的范围内；
    3. 这一句说的是「已完成／尚未完成」这类状态，而且依据里有同一个状态 ——
       「目前尚未实施」与「还没有实施」是同一个状态，不该因为换词被拒。

    判「新内容」看的是**连续两个以上的生字**：单个助词或量词（「为」「元」）
    不算一件事，连成串的实义字（「夜间低峰期」）才算。

    「生字」以**这一轮用户说过的全部原话**（``context_texts``）加旧内容为准：
    用户补在另一句里的话（「尚未实施，也没有评审结论」）也是他说过的，
    模型照抄不算编造。
    """
    if not new_text.strip():
        return ()
    known_texts = tuple(s for text in evidence_clauses for s in clauses(text)) + clauses(current_text)
    found: list[EvidenceItem] = []
    seen: set[str] = set()
    for clause in clauses(new_text):
        if clause in seen or clause in skip_clauses:
            continue
        if any(token and token in clause for token in skip_tokens):
            # 这一句的新内容就是那个已经报出来的值，不重复记一遍。
            continue
        seen.add(clause)
        normalized = _literal_claim(clause)
        # Equality, rather than unordered letter coverage, proves only the
        # explicitly supported spelling variants. Substring reductions require
        # review because they may discard a subject, negation or condition.
        if normalized and any(normalized == _literal_claim(source) for source in known_texts):
            continue
        # A recalled target may supply only the omitted subject of a short
        # correction. It cannot supply new actors or a reversed relationship.
        old_normal = _literal_claim(current_text)
        inherited = any(normalized.endswith(_literal_claim(source))
                        and normalized[:-len(_literal_claim(source))] in old_normal
                        and not _NEGATIVE_PATTERN.search(normalized[:-len(_literal_claim(source))])
                        for source in evidence_clauses if _literal_claim(source))
        choice_only = re.fullmatch(r'方案?\s*[A-Za-z]', clause) and any(
            re.fullmatch(r'采用(?:方案)?' + re.escape(clause.strip()[-1]), _literal_claim(s)) for s in evidence_clauses)
        correction = bool(current_text and value_entries(clause) and any(
            re.search(r'(不是|并非)[0-9零〇一二三四五六七八九十百千万亿两.,，]+(?:元|块|人)?$', s)
            and (same_subject(s, current_text) or re.match(r'^(不是|并非)', s))
            and any(value_evidenced(current_text, t) for t in value_tokens(s)) for s in evidence_clauses))
        if correction:
            old_shape, new_shape = _literal_claim(current_text), normalized
            for text in (current_text, clause):
                for token in value_tokens(text):
                    for value, _ in value_entries(token):
                        old_shape = old_shape.replace(str(value), '#')
                        new_shape = new_shape.replace(str(value), '#')
            correction = old_shape == new_shape and all(any(value_evidenced(s, t) for s in evidence_clauses
                if re.match(r'^(是|应该是|改成)', s)) for t in value_tokens(clause))
        if inherited or choice_only or correction:
            continue
        found.append(
            EvidenceItem(
                category=UNCERTAIN,
                text=clause,
                clause=clause,
                verdict=UNCERTAIN_VERDICT,
                # 理由里带上整句：只报一个片段（「机进」）看不出问题在哪，
                # 读的人得能对上原话才知道缺什么。
                reason=(
                    f"「{clause}」的语序、事项或含义不能由原话直接确认，需要复核后再落库"
                ),
            )
        )
    return tuple(found)


def gap_reason(gaps: Iterable[EvidenceItem], *, limit: int = 3) -> str:
    """把逐项结果写成一句拒绝理由：先说最硬的那几项，再说明整体不落库。

    只有数值缺依据时沿用既有说法（「新值缺少原话依据」），其余情况说明是哪一类
    信息没有出处 —— 理由要指向具体那一项，不能让调用方以为「整条都不对」。
    """
    items = [item for item in gaps if item is not None]
    if not items:
        return ""
    ordered = sorted(
        items,
        key=lambda item: (item.verdict != UNSUPPORTED, item.category == UNCERTAIN),
    )
    shown = ordered[:limit]
    values_only = all(item.category == VALUE for item in ordered)
    if values_only:
        return (
            "新值缺少原话依据：新内容里变的「"
            + "、".join(item.text for item in shown)
            + "」在用户原话里找不到，不能只凭模型判断覆盖这条记录"
        )
    detail = "；".join(item.reason or item.text for item in shown)
    prefix = "内容里的信息缺少依据" if ordered[0].verdict == UNSUPPORTED else "内容里的信息无法确认"
    return f"{prefix}，整条暂不应用：{detail}"


def support_verdicts(
    new_text: Any,
    current_text: Any,
    *,
    evidence_clauses: Iterable[Any],
    tool_clauses: Iterable[Any] = (),
    item_text: str = "",
) -> tuple[dict[str, Any], ...]:
    """逐项结果的可读形态：每项都是「有依据／无依据／不确定」加对应原文。"""
    return tuple(item.as_dict() for item in evidence_gaps(
        new_text,
        current_text,
        evidence_clauses=evidence_clauses,
        tool_clauses=tool_clauses,
        item_text=item_text,
    ))


def receipt(
    new_text: Any,
    current_text: Any,
    *,
    evidence_clauses: Iterable[Any],
    tool_clauses: Iterable[Any] = (),
    item_text: str = "",
    rule_version: str = "",
    target_version: Any = None,
    retention: str = "",
    check_new_material: bool = True,
    bindings: Iterable[Any] = (),
) -> dict[str, Any]:
    """核对回执：规则版本、内容与来源摘要、目标版本、逐项判断和依据引用。

    回执由**处理端**生成并随候选／更新建议持久保存；模型或外部请求自带的
    「检查通过」不算数 —— 这里重新算一遍，算不出来的项如实写成无依据／不确定。
    """
    evidence_clauses = tuple(evidence_clauses)
    tool_clauses = tuple(tool_clauses)
    bindings = list(bindings)
    items = evidence_gaps(
        new_text,
        current_text,
        evidence_clauses=evidence_clauses,
        tool_clauses=tool_clauses,
        item_text=item_text,
        check_new_material=check_new_material,
    )
    user_texts = [str(text or "") for text in (evidence_clauses or ())]
    tool_texts = [str(text or "") for text in (tool_clauses or ())]
    checked = tuple(item.as_dict() for item in items)
    return {
        "rule_version": rule_version or RULE_VERSION,
        "content_fingerprint": fingerprint(str(new_text or '')),
        "evidence_fingerprint": fingerprint([list(evidence_clauses), list(tool_clauses), bindings]),
        "subject_fingerprint": fingerprint(item_text),
        "bindings": bindings,
        "content_summary": str(new_text or "")[:80],
        "source_summary": [text[:80] for text in (*user_texts, *tool_texts)][:8],
        "target_version": target_version,
        "retention": retention,
        "items": checked,
        "unsupported": sum(1 for item in checked if item["verdict"] == UNSUPPORTED),
        "uncertain": sum(1 for item in checked if item["verdict"] == UNCERTAIN_VERDICT),
        "passed": not checked,
        "verdict": UNSUPPORTED if any(i['verdict'] == UNSUPPORTED for i in checked) else (UNCERTAIN_VERDICT if checked else SUPPORTED),
    }


def receipt_is_current(
    receipt: Any, *, content: Any, target_version: Any = None, rule_version: str = "",
    evidence_clauses=None, tool_clauses=(), bindings=(), item_text=None,
) -> bool:
    """这份已有回执还算不算数。

    内容、目标版本或规则版本变了一样，旧回执立即失效 —— 复用的前提是「同一件事、
    同一版目标、同一套判据」，否则等于拿旧结论给新内容盖章。
    """
    if not isinstance(receipt, dict):
        return False
    if str(receipt.get("rule_version") or "") != (rule_version or RULE_VERSION):
        return False
    if receipt.get('content_fingerprint') != fingerprint(str(content or '')):
        return False
    if evidence_clauses is not None and receipt.get('evidence_fingerprint') != fingerprint([list(evidence_clauses), list(tool_clauses), list(bindings)]):
        return False
    if item_text is not None and receipt.get('subject_fingerprint') != fingerprint(item_text):
        return False
    if target_version is None:
        return receipt.get("target_version") is None
    return str(receipt.get("target_version")) == str(target_version)


def persisted_receipt(
    receipt: Any, *, content: Any, target_version: Any = None
) -> dict[str, Any] | None:
    """把回执整理成可以写进记录附加信息的形态；不适用时返回 ``None``。"""
    if not receipt_is_current(receipt, content=content, target_version=target_version):
        return None
    return dict(receipt)


def check_memory_content(item, *, packet, target=None, lifetime=None, prior_receipt=None, review_authority=None):
    """The single content gate shared by candidates, corrections, replacements and merges."""
    from .memory_semantic import exact_bindings, original_sources, hard_gaps
    current = str((target or {}).get('content') or '')
    sources = original_sources(packet)
    proof = review_authority.verify(item, packet, target) if review_authority else None
    judgment = (proof or {}).get('semantic_judgment')
    # Any update of an existing record is a semantic operation. Even when the
    # text happens to equal an original clause, only the memory agent's
    # durable judgment may authorize changing/archiving the target; otherwise a
    # model or caller can retarget an unrelated record by quoting a new field.
    bindings = judgment.get('bindings', []) if judgment else (() if target else exact_bindings(item, packet))
    hard_sources = bindings if judgment else [s for s in sources if s['source_type'] in ('user_statement','tool_result')]
    gaps = hard_gaps(item.content, hard_sources, current)
    passed = bool(bindings) and not gaps
    verdict = UNSUPPORTED if gaps or packet is None else (SUPPORTED if passed else UNCERTAIN_VERDICT)
    if not passed and not gaps:
        gaps = [dict(category='semantic',item=item.content,clause=item.content,verdict=verdict,quote='',
                     reason='需要模型核对事项、关系、指代及含义' if packet else '缺少原始处理包')]
    checked = dict(rule_version=RULE_VERSION, content_fingerprint=fingerprint(item.content),
        evidence_fingerprint=fingerprint([sources,bindings]), subject_fingerprint=fingerprint((judgment or {}).get('subject') or item.content),
        bindings=bindings, content_summary=item.content[:80], source_summary=[b['quote'][:80] for b in bindings],
        target_version=(target or {}).get('record_version'),retention=(("short_term" if lifetime.is_short else "long_term") + ':' + lifetime.basis) if lifetime else '',
        items=gaps,unsupported=sum(g['verdict']==UNSUPPORTED for g in gaps),uncertain=sum(g['verdict']==UNCERTAIN_VERDICT for g in gaps),
        passed=passed and packet is not None,verdict=verdict)
    if judgment:
        checked.update(semantic_judgment=judgment,review_key=proof['review_key'],model_raw=proof['raw'])
    if not bindings:
        checked['binding_gap'] = 'missing_original_packet' if packet is None else ('semantic_review_required' if target else 'no_subject_bound_original_quote')
    if isinstance(prior_receipt, dict) and all(
        name in prior_receipt and prior_receipt[name] == checked.get(name)
        for name in ('rule_version', 'content_fingerprint', 'evidence_fingerprint',
                     'subject_fingerprint', 'target_version', 'passed', 'verdict')
    ) and prior_receipt.get('semantic_judgment') == checked.get('semantic_judgment'):
        # Rechecking integrity is cheap; never trust an old passed bit alone.
        checked['reused'] = True
    return checked
