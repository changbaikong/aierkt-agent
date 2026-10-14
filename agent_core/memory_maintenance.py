"""内容维护：对**已存在**的记忆应用更新、保护版本、核对写入结果。

【为什么单独一个模块】
「挑要复核的记录」「校验更新建议」「原子应用变更」「核对写后结果」是四件
不同的事。散到队列、网页入口和云端工人里，就会变成三处各自判断「这条建议
能不能落地」—— 而它们的判断迟早不一致，不一致的那一处就是数据被改错的地方。

【模型只提建议】
动作、目标、版本、来源可信度全部由这里裁决：模型说「用户已经采纳」不算，
说「这是第 3 版」也不算。判据只有两种 —— 用户原话，和库里真实存在的记录。

【为什么必须原子】
一次纠正要同时做三件事：写新版本、让旧版本退出当前有效集合、记下纠正关系。
分几步提交时，中途失败会留下「旧的已经不生效、新的却没写上」的空洞：
那条记忆就此消失，而且没有任何痕迹指向它。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from . import memory_scope
from .memory_binding import bind_evidence
from .contracts import Scope
from .memory_evidence import check_memory_content, evidence_gaps, gap_reason, receipt, receipt_is_current, states_status
from .memory_horizon import resolve_lifetime
from .memory_turn import (
    ACTIONS_REQUIRING_TARGET,
    MemoryCandidateError,
    MemoryUpdateSuggestion,
    changed_value_tokens,
    cited_user_evidence,
    clauses,
    content_units,
    expresses_adoption,
    expresses_invalidation,
    mentions_item,
    sentences,
    user_authored_texts,
    validate_suggestion,
    value_evidenced,
    value_evidence_gaps,
    value_tokens,
)

LOGGER = logging.getLogger("agent_core.memory.maintenance")

#: 定期复核的默认间隔与单批上限。到点只表示「该看一眼」，不等于内容自动过期。
DEFAULT_REVIEW_INTERVAL = timedelta(hours=24)
DEFAULT_BATCH_SIZE = 20
#: 定期复核的轮询间隔（秒）：30 分钟一批。
DEFAULT_REVIEW_INTERVAL_SECONDS = 1800

#: 「明确替代」的原话特征。没有这类表达就不许覆盖现行结论 ——
#: 「新消息更晚」「模型更有把握」都不是替代依据。
#: 「不再」和「不再用／先不」必须收进来：用户说「改用 B，不再用 A」时，
#: 分句后「不再用 A」是独立一句，漏了它就等于正当替代被拦下。
_REPLACEMENT_MARKERS: tuple[str, ...] = (
    "改用", "换成", "改为", "不用", "不要", "别用", "取消", "否决",
    "不再", "先不", "不采用", "作废",
    "暂不", "暂缓", "停止",
)

#: 「明确指出旧说法错了」的原话特征。这一组是**标记**：某一句话里有它，
#: 说明用户在纠正某件事（纠的是哪件事由 :meth:`_subject_clauses` 绑定）。
_CORRECTION_MARKERS: tuple[str, ...] = (
    "不是", "错了", "纠正", "改成", "应该是", "并非", "说错了",
)

#: 纠正句里的「新结论引导词」。真正的纠正只有一个结论：
#: 「不是五千，是三千」里「是三千」才是新值。只凭标记词就去整轮原话里
#: 捞新值，会把「云端部署的建议你再分析一下」这种无关后续也算成依据。
_CORRECTION_LEAD_MARKERS: tuple[str, ...] = (
    "应该是", "改成", "改为", "换成", "更正为", "纠正为", "其实", "应当是",
    "现价", "现在", "新的", "新值", "调整为",
)
#: 纠正句里「旧说法」的引导词：它后面跟的是被否掉的旧值，不是新值。
_OLD_VALUE_MARKERS: tuple[str, ...] = ("不是", "并非", "原来", "以前", "之前", "旧")
#: 工具回执里「这件事变了／这件事不作数了」的措辞。回执只有读到现状时不够。
_TOOL_CHANGE_MARKERS: tuple[str, ...] = (
    "已作废", "已失效", "已取消", "已过期", "已删除", "已下线", "已停用",
    "不再", "不用", "无此", "不存在", "查无", "已变更", "已改为", "更正为",
    "错误", "不一致", "冲突", "推翻", "替代", "换成",
)
#: 分类动词：出现这些词说明说的是**结论/决定/方案**，不是数值。
_CATEGORY_MARKERS: tuple[str, ...] = (
    "决定", "结论", "方案", "建议", "原则", "政策", "做法", "方向", "想法",
    "观点", "说法", "定论", "选择",
)
#: 「还没定／没变化」的表达里**本身就自足**的那些：出现即表示这件事没定。
#: 刻意不放裸词「尚未/还没/还没有」—— 它们也能接**执行状态**（「尚未实施」
#: 「还没上线」），那是「事项已定、只是还没做」，不是「事项未定」（Q6）。
_UNDECIDED_MARKERS: tuple[str, ...] = (
    "未决定", "没决定", "还没定", "没定", "待定", "不确定", "未确定", "未确认",
    "未变", "没变", "没有变", "没变化",
)
#: 需要另接一个**未定语义词**才算未定的引导词：「尚未/还没/还没有 + 确定|决定…」。
#: 「尚未交付日期」这类不接未定语义词的，说明说的是执行进度，不算事项未定。
_UNDECIDED_LEADS: tuple[str, ...] = ("尚未", "还没", "还没有", "未曾", "仍未", "仍未定")
#: 「另起一件别的事」的引导词。续写判定里出现它就说明主语换了，不能当同一条的依据。
_NEW_SUBJECT_MARKERS: tuple[str, ...] = (
    "另外", "顺便", "还有", "对了", "此外", "至于", "其他", "别的", "再说", "另一个",
)
#: 未定语义词：跟在引导词后面才成立。
_UNDECIDED_QUALIFIERS: tuple[str, ...] = (
    "确定", "决定", "确认", "定", "定下来", "结论", "拍板", "敲定", "表态",
    "批准", "通过", "明确",
)
#: 执行状态词：出现这些说明讲的是「还没做」，不是「还没定」。
_EXECUTION_MARKERS: tuple[str, ...] = (
    "实施", "执行", "落地", "上线", "部署", "投产", "开工", "启动", "完成",
    "开始", "动工", "生效", "启用", "推广", "应用", "实行", "排期", "交付",
)
#: 字段名连写的写法：「交付时间为…」「预算为…」—— 字段名里可能带冒号，先去掉。
_FIELD_SEPARATOR = re.compile(r"[:：=]")
#: 数值里的千分位逗号，切分句前先复原，否则「3,000元」会被当成两句。
_GROUPED_DIGITS = re.compile(r"(?<=\d),(?=\d{3}\b)")
#: 连续汉字串。字段名只在这上面取，不把数字的二元组混进去。
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


def _is_value_bearing(token: str) -> bool:
    """这个片段是不是**带着一个值**（数字、中文数词，或字母数字编号）。

    逐项取证只针对这一类片段。换一种说法（「用」→「使用」、「选择」→
    「决定」）也会产生新片段，但那是措辞不是值：拿它去要求逐条有出处，
    正当的决定更新会被整条拒掉。

    判定直接复用 ``value_tokens``：整段刚好就是一个数值片段才算带值，
    所以「三千」「2026」算，「一律」「使用」不算（「一律」只切得出「一」，
    不等于整个词是个值）。
    """
    if not token:
        return False
    if token.isascii():
        # 字母/数字编号（方案 A、v2、2026）本身就是可核对的值。
        return True
    return token in value_tokens(token)


def _now_text() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class _NotificationReport:
    """这次写入期间，变更通知到底出去了没有。"""

    def __init__(self) -> None:
        self.calls = 0
        self.failures: list[str] = []

    @property
    def configured(self) -> bool:
        return self.calls > 0 or bool(self.failures)

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": list(self.failures),
        }


class _ProbeCallback:
    """包住真正的通知回调：转发调用，并把抛出来的异常如实记下来。

        只做转发与记账，不改变行为 —— 通知该失败还是失败，写入端该吞还是吞。
    """

    def __init__(self, callback: Any, report: _NotificationReport) -> None:
        self.callback = callback
        self.report = report

    def __call__(self, action: str, record: Any) -> Any:
        self.report.calls += 1
        try:
            return self.callback(action, record)
        except Exception as exc:  # noqa: BLE001 - 探针只记账，不改变写入端行为
            self.report.failures.append(f"{type(exc).__name__}: {exc}")
            raise


class MemoryMaintenanceService:
    """内容维护的执行入口。队列和网页只调它，不各自判规则。"""

    def __init__(
        self,
        writer: Any,
        repositories: Any,
        *,
        review_interval: timedelta = DEFAULT_REVIEW_INTERVAL,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.writer = writer
        self.repositories = repositories
        self.review_interval = review_interval
        self.batch_size = batch_size

    # ------------------------------------------------------------------
    # 挑选需复核的记录
    # ------------------------------------------------------------------
    def pick_due(self, *, now: datetime | None = None, limit: int | None = None) -> list[dict]:
        """到点该复核的记录（含还没解决的冲突）。

        没有待复核项时返回空列表，调用方据此**不调模型** —— 定期任务空转时
        一次模型调用都不该发生。
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="milliseconds")
        with self.repositories.connect() as db:
            rows = db.execute(
                "SELECT * FROM memory_records WHERE review_state='pending' AND status='active' "
                "AND (review_due_at IS NULL OR review_due_at<=?) "
                "ORDER BY review_due_at ASC LIMIT ?",
                (moment, int(limit or self.batch_size)),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_reviewed(self, record_id: str, *, settled: bool = True) -> None:
        """一次复核结束：要么定案，要么按间隔排下一次。"""
        with self.repositories.connect() as db:
            db.execute(
                "UPDATE memory_records SET review_state=?,review_due_at=?,updated_at=? WHERE id=?",
                (
                    "settled" if settled else "pending",
                    None if settled else (datetime.now(UTC) + self.review_interval).isoformat(timespec="milliseconds"),
                    _now_text(),
                    record_id,
                ),
            )

    # ------------------------------------------------------------------
    # 校验更新建议：目标必须重新授权
    # ------------------------------------------------------------------
    def read_targets(
        self,
        scope: Scope,
        ids: Any,
        *,
        project_id: str = "",
    ) -> tuple[list[dict], dict[str, int]]:
        """读目标记录，逐条**重新授权**，并给出当前版本号。

        模型看到的「旧记忆」是本端放进输入里的，但目标编号是它回填的 ——
        它可能填一条别的会话、甚至别的账号的记录。所以这里不采信：按账号和
        范围再过一遍授权，没权限的当它不存在。
        """
        wanted = [str(item) for item in ids if str(item or "").strip()]
        if not wanted:
            return [], {}
        records = self.repositories.records_by_ids(wanted)
        allowed: list[dict] = []
        for record_id in wanted:
            row = records.get(record_id)
            if row is None:
                continue
            if memory_scope.is_semantic(row):
                if not memory_scope.authorize(row, scope, project_id=project_id):
                    continue
            elif (
                str(row.get("owner_id")),
                str(row.get("channel")),
                str(row.get("conversation_id")),
                str(row.get("module_id")),
            ) != (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id):
                continue
            allowed.append(row)
        return allowed, {str(row["id"]): int(row.get("record_version") or 0) for row in allowed}

    @staticmethod
    def _lifetime_for(suggestion: MemoryUpdateSuggestion, packet: Any):
        """这条更新建议的新版本该留多久。

        与保存侧调用**同一个** :func:`resolve_lifetime`：用户明说的期限照原话，
        项目阶段约束长期保留，当天状态沿用短期政策。模型把「暂时先不部署」
        标成短期也不改变判定 —— 寿命由原话和内容性质决定，不由模型自述决定。
        """
        if packet is None and not str(getattr(suggestion, "horizon", "") or "").strip():
            return None
        source_text = ""
        if packet is not None:
            source_text = '。'.join(b['quote'] for b in bind_evidence(packet, suggestion.content, source_refs=suggestion.source_refs))
        return resolve_lifetime(
            str(suggestion.content or ""),
            kind=str(getattr(suggestion, "kind", "") or ""),
            declared=str(getattr(suggestion, "horizon", "") or ""),
            source_text=source_text,
        )

    def _evidence_receipt(
        self,
        suggestion: MemoryUpdateSuggestion,
        *,
        target: dict | None,
        packet: Any,
        lifetime: Any = None,
        prior_receipt: Any = None,
    ) -> dict | None:
        """这条更新建议的核对回执：逐项判断、依据引用、目标版本与保留方式。

        回执由**处理端**现算，不接受模型或外部请求自带的「检查通过」；旧作业没有
        回执时这一步就是「先补检查再应用」，不会因为缺字段默认为通过。
        作业里已经存着一份、而且内容与目标版本都没变时直接复用（重试不重复核对）。
        """
        return check_memory_content(suggestion, packet=packet, target=target,
                                    lifetime=lifetime, prior_receipt=prior_receipt,review_authority=self.writer.review_authority)

    def _refuse_reason(
        self,
        suggestion: MemoryUpdateSuggestion,
        *,
        target: dict | None,
        packet: Any,
    ) -> str:
        """程序侧的硬性拒绝条件。返回空串表示放行。

        这些条件模型判断不了，也不该由它判断：它看不到库里那条记录是哪天形成的，
        也无从知道用户到底有没有说过「改用 B」。

        **授权依据必须绑到同一件事上。** 本轮说「同意 A；B 再分析一下」时，
        A 那一句里的同意只能授权 A：把整轮原话拼成一大段去搜「改用／不是」，
        等于让一句无关的话给另一件事盖章（B3-R1）。所以下面一律按
        :meth:`_subject_clauses` 取「与本条建议同一件事的那几句」再判。
        """
        action = suggestion.action
        if action not in ("correct", "replace", "merge"):
            return ""
        if packet is None:
            return ""
        if target is not None:
            evidence_at = str(getattr(packet, 'occurred_at', '') or '')
            recorded_at = str(target.get('created_at') or '')
            if evidence_at and recorded_at and datetime.fromisoformat(evidence_at) < datetime.fromisoformat(recorded_at):
                return '证据早于现行版本，不覆盖'
        # A verified model decision owns semantic matching and change intent.
        # Permission/version/transaction guards below remain mandatory.
        if self.writer.review_authority.verify(suggestion,packet,target):
            return ""
        cited = self._cited_texts(suggestion, packet)
        item_text = self._item_text(suggestion, target)
        current_text = str((target or {}).get("content") or "")
        new_text = str(suggestion.content or "").strip()
        by_tool = self._has_tool_evidence(suggestion, packet, item_text=item_text)

        # 证据范围分两层，粒度都是「句子」：
        #   cited_subject —— 被这条建议**引用到**的原话里讲这件事的那几句；
        #   field_subject —— 本轮原话里讲这件事、或讲新字段的那几句。
        cited_subject = self._claims_about_item(suggestion, cited, item_text)
        # 逐项依据只看「讲这件事」的那几句。`field_subject` 另外还包含「讲了新字段
        # 名」的句子，用来判断“未定字段”，**不能**拿它当数值依据（见 V3）。
        target_subject = self._target_subject(packet, item_text, hint=new_text)
        # 本轮原话里所有**提到这件事**的分句都算证据范围：只收「带结论措辞」的
        # 那几句时，用户先说事项、再补一句状态（「尚未实施」）的写法会让后者
        # 落在范围外，正常的更新反而被判「没依据」。
        mentioned_clauses = self._subject_clauses(packet, item_text)
        field_subject = self._subject_clauses(
            packet, item_text, keywords=self._field_tokens(new_text, current_text)
        )

        if action == "replace":
            # 替代依据要绑到目标事项上：本轮说「同意 A；B 再分析一下」时，
            # A 那句里的同意不能给 B 盖章。
            #
            # 纠正措辞也算替代依据：用户说「不是用 A，是用 B」时，模型把动作
            # 标成 replace 而不是 correct 是常见写法，判据落在「这一句是不是
            # 在给这件事换结论」上，不落在模型动作的名字上。
            if not (
                self._subject_markers(packet, item_text, _REPLACEMENT_MARKERS)
                or self._subject_markers(packet, item_text, _CORRECTION_MARKERS)
                or self._cited_replacement_marker(cited, item_text)
                or self._subject_adoption(packet, suggestion, item_text)
            ):
                return "缺少明确替代依据（用户没有说改用或不再用）"
            if not (
                self._subject_decision_wording(packet, item_text)
                # 「不是用 A，是用 B」同样是把这件事的结论换掉：模型把动作标成
                # replace 时，这两道关必须用同一套判据，否则纠正措辞过了第一关、
                # 又被第二关拦下，正当替代还是走不通。
                or self._subject_markers(packet, item_text, _CORRECTION_MARKERS)
                or self._cited_replacement_marker(cited, item_text)
                or self._subject_adoption(packet, suggestion, item_text)
            ):
                return (
                    "替代依据没有落到目标事项上：本轮提到「"
                    f"{item_text[:20]}」的那句话里没有明确改用或采纳它"
                )
        # 证据时间先判：迟到的旧证据不能推翻现行结论，这条优先于「有没有纠正依据」，
        # 否则回执上的原因会误导成「用户没说错」，实际是证据本身太旧。
        if target is not None:
            evidence_at = str(getattr(packet, "occurred_at", "") or "")
            recorded_at = str(target.get("created_at") or "")
            if evidence_at and recorded_at and evidence_at < recorded_at:
                # 上传时间不等于发生时间。
                return "证据早于现行版本，不覆盖"
        if action == "correct":
            # 纠正依据必须落在「讲这件事」的原话上。用户纠的是别的字段
            # （「交付日期不是 12 月，是 1 月」）时，同一条新内容里的其他新值
            # 不算有依据 —— 那是另一件事的结论。
            if not (cited_subject or field_subject or by_tool):
                return "缺少纠正依据（用户没有指出旧说法错在哪，也没有工具结果）"
            if not (target is None or cited_subject or target_subject or by_tool):
                # 纠正词对上了、新值也在原话里，但说的是**另一件事**。
                return (
                    "纠正或替代没有落到目标事项上：引用原话里没有提到"
                    f"「{item_text[:20]}」，不能凭无关词覆盖这条记录"
                )
        if target is None:
            return ""
        undecided = self._undecided_fields(new_text, packet, item_text)
        if undecided:
            return (
                "未定字段不能填值：用户说「"
                + "、".join(undecided[:2])
                + "」还没确定，模型却在新内容里给它填了值"
            )
        # 统一依据检查：数值、评审结论、实施结果、条件、原因、适用范围逐项核。
        # 「用／使用／选择 B」这类换词不算新事实（见 memory_evidence 的换词表），
        # 但「已获批准／已经实施／演练成功」是另外的信息，必须各有各的出处。
        gaps = evidence_gaps(
            new_text,
            current_text,
            evidence_clauses=(
                ((cited_subject,) if cited_subject else ())
                + mentioned_clauses
                + target_subject
                # 明确替代时，用户那句话本身就是要改成的新结论：
                # 「改用 B，不再用 A」里 B 只在那一句出现，A 才是目标。
                # 纠正（correct）也一起算：模型把「改用 B」标成 correct 是常见
                # 写法，动作名字不该决定「这句话算不算依据」。
                + self._cited_subject_texts(cited)
            ),
            tool_clauses=self._tool_evidence_texts(packet, suggestion, item_text),
            item_text=item_text,
            # 「这个词用户说没说过」按整轮原话判：证据范围（按事项绑定）与
            # 「说没说过」是两件事，混在一起会把用户补在另一句里的话当成编造。
            context_texts=user_authored_texts(packet),
        )
        if gaps:
            return gap_reason(gaps)
        if action in ("correct", "replace") and cited and not by_tool:
            if not any(self._mentions_target(text, current_text) for text in cited):
                return (
                    "纠正或替代没有落到目标事项上：引用原话里没有提到"
                    f"「{current_text[:20]}」，不能凭无关词覆盖这条记录"
                )
        return ""

    @staticmethod
    def _target_subject(packet: Any, item_text: str, *, hint: str = "") -> tuple[str, ...]:
        """本轮原话里**只按目标事项**绑定的那几句（不含新字段名线索）。

        这是数值依据能用的范围：用户对这件事说的原话。新字段名出现在别的
        句子里时，那一句不属于这条记录的证据。

        「不是五千，是三千」这种纠正里，后半句「是三千」单看没有目标词，
        但它是前半句的**续写**，必须一起算作这件事的证据 —— 判定方式是：
        只要一段话里有句子绑上了目标事项，这段话里紧跟其后的结论句一并算数。

        续写分强弱两档：
        - **强续写**（「是三千」「改成八千」）跨句末标点也算，因为它自带结论引导词；
        - **弱续写**（「预算八千」这种字段名 + 值）只在**同一句之内**算，而且
          必须和**这次要写的新内容**共用至少一个实义片段 —— 否则同一句里
          另一件事的结论（「不是方案甲，是决定排期往后挪一周」）会被当成本条依据。

        ``hint`` 就是新内容；不给时弱续写不生效（宁可不收，也不认错依据）。
        """
        if packet is None:
            return ()
        wanted = str(item_text or "").strip()
        if not wanted:
            return ()
        picked: list[str] = []
        for text in user_authored_texts(packet):
            anchored = False
            same_sentence = False
            for sentence in sentences(text) or (str(text or ""),):
                # 跨到新句子后，「弱续写」（字段名 + 值）不再附着；
                # 强续写（「是三千」「还没有实施」）自带结论信号，仍可跨句末标点。
                same_sentence = False
                for clause in clauses(sentence) or (sentence,):
                    if MemoryMaintenanceService._mentions_target(clause, wanted):
                        anchored = True
                        same_sentence = True
                        picked.append(clause)
                    elif anchored and MemoryMaintenanceService._continues_claim(
                        clause, same_sentence=same_sentence, hint=hint
                    ):
                        # 续写的结论句（「是三千」「还没有实施」）：附着在刚绑上的
                        # 那句后面。接完这一句就不再往后接，避免把「另外……」那种
                        # 另一件事的话也吸进来。
                        picked.append(clause)
                    elif clause.strip():
                        anchored = False
                        same_sentence = False
        return tuple(picked)

    @staticmethod
    def _continues_claim(clause: str, *, same_sentence: bool = True, hint: str = "") -> bool:
        """紧邻的下一句是不是上一句的续写结论（「是三千」这种，不带目标词）。

        强续写：句首是肯定/更正式引导词、或带纠正标记、或说的是「已完成／尚未
        完成」这类状态 —— 自带「这是在给结论」的信号，跨句末标点也算。
        弱续写：「预算八千」这种字段名 + 值、没有新主语、也不长 —— 只有在
        **同一句之内**、而且和这次要写的新内容共用实义片段时才算，否则同一句里
        另一件事的结论会被当成本条的依据。
        """
        text = str(clause or "").strip()
        if not text:
            return False
        if any(marker in text for marker in _NEW_SUBJECT_MARKERS):
            # 「另外／顺便／还有」明确换了主语，不是续写。
            return False
        if states_status(text):
            # 「还没有实施」和「改用 B」是同一件事的两面：状态句是续写。
            return True
        if not value_tokens(text):
            return False
        if any(marker in text for marker in _CORRECTION_MARKERS):
            return True
        if text.startswith(("是", "为", "应", "应为", "现在")):
            return True
        if not (same_sentence and len(text) <= 12):
            return False
        return bool(hint) and mentions_item(text, hint)

    @staticmethod
    def _subject_clauses(
        packet: Any, item_text: str, *, keywords: tuple[str, ...] = ()
    ) -> tuple[str, ...]:
        """本轮原话里**在说这件事**的那几句。

        逐分句绑定是这里唯一说得通的粒度：一整轮里可能同时出现「同意 A」和
        「B 再看看」，只有提到 A 的那一句才是 A 的授权依据。
        没有任何一句提到这件事时返回空 —— 「没提到」不等于「整轮都算」。

        绑定走 :meth:`_mentions_target`：除了共同实义片段，也要认「改用 B，
        不再用 A」里那个用来区分版本的编号/字母，否则正当的替代会被当无关词拦下。

        ``keywords`` 是补充线索：用户可能对新字段另起一句
        （「交付日期尚未确定」），那一句和目标正文没有共同实义片段，
        但字段名本身对得上。
        """
        if packet is None:
            return ()
        wanted = str(item_text or "").strip()
        extra = tuple(str(word) for word in keywords if str(word or "").strip())
        if not wanted and not extra:
            return ()
        return tuple(
            clause
            for text in user_authored_texts(packet)
            for clause in clauses(text)
            if MemoryMaintenanceService._mentions_target(clause, wanted)
            or any(word in clause for word in extra)
        )

    @staticmethod
    def _subject_markers(packet: Any, item_text: str, markers: tuple[str, ...]) -> bool:
        """提到这件事的那几句里有没有这些标记词。"""
        return any(
            marker in clause
            for clause in MemoryMaintenanceService._subject_clauses(packet, item_text)
            for marker in markers
        )

    @staticmethod
    def _subject_decision_wording(packet: Any, item_text: str) -> bool:
        """用户对这件事有没有**明确的决策措辞**。

        判据不是「用户必须写同意」：改用、一律、取消、不再用都是决策。只要
        说这件事的那一句里出现决策/替代类字眼，就够授权本条建议。
        """
        return MemoryMaintenanceService._subject_markers(
            packet, item_text, _REPLACEMENT_MARKERS
        )

    @staticmethod
    def _subject_adoption(
        packet: Any, suggestion: MemoryUpdateSuggestion, item_text: str
    ) -> bool:
        """「用户已采纳」这件事有没有绑到同一事项上的用户原话支撑。

        复用 ``expresses_adoption`` 的逐句绑定与否定窗口：用本条建议**引用到**
        的原话，每条都要求「这一句同时提到这件事」。
        """
        if not expresses_adoption(str(suggestion.content or ""), item_text=item_text):
            for text, _ in cited_user_evidence(packet, suggestion.source_refs):
                if expresses_adoption(text, item_text=item_text):
                    return True
        return False

    @staticmethod
    def _claims_about_item(
        suggestion: MemoryUpdateSuggestion, cited: tuple[str, ...], item_text: str
    ) -> str:
        """引用原话里**在说这件事、并且给出结论**的那一部分。

        返回的是能当纠正依据的那些句子；空串表示用户这次没在说这件事。
        用途是给 B3-R3 定证据范围：只有「纠正这件事」的那几句才能证明
        这条新内容里的每一项新值。
        """
        wanted = str(item_text or "").strip()
        claimed: list[str] = []
        for text in cited:
            for clause in clauses(text):
                if MemoryMaintenanceService._mentions_target(
                    clause, wanted
                ) and MemoryMaintenanceService._states_claim(clause, item_text=wanted):
                    claimed.append(clause)
        return "".join(claimed)

    def _cited_replacement_marker(self, cited: tuple[str, ...], item_text: str) -> bool:
        """被引用原话里有没有**绑到目标事项**的替代表达。

        用户在引用到的那句话里说「改用 B，不再用 A」时替代依据成立；只说
        「改用 B」而目标根本不是 A 时不算 —— 这条建议引用的原话同样要落到
        目标事项上，否则就是拿另一件事的话来覆盖这条记录。
        """
        for text in cited:
            for clause in clauses(text):
                if not self._mentions_target(clause, item_text):
                    continue
                if any(marker in clause for marker in _REPLACEMENT_MARKERS):
                    return True
        return False

    def _cited_subject_texts(self, cited: tuple[str, ...]) -> tuple[str, ...]:
        """被引用原话里带替代措辞的那几句（明确替代时的新结论就在其中）。

        用户说「改用 B，不再用 A」时，新值 B 只出现在这句里，而目标事项写的是
        A —— 单按目标绑定会漏掉 B。所以明确替代时，这句原话本身也算证据；
        没有替代措辞的原话不在此列，避免把普通句子当依据。
        """
        picked: list[str] = []
        for text in cited:
            for clause in clauses(text):
                if any(marker in clause for marker in _REPLACEMENT_MARKERS):
                    picked.append(clause)
        return tuple(picked)

    def _item_text(self, suggestion: MemoryUpdateSuggestion, target: dict | None) -> str:
        """这条建议说的到底是哪件事：优先目标记录正文，其次建议自己的正文。

        目标记录才是「要改的那件事」。只拿建议正文去绑定，模型随口换个说法
        （「云端部署的建议」）就能把原话里提到过的另一件事算成依据。
        """
        target_text = str((target or {}).get("content") or "").strip()
        return target_text or str(suggestion.content or "").strip()

    @staticmethod
    def _mentions_target(text: str, target_text: str) -> bool:
        """这段原话是不是在说这条记录。

        两种提到方式：共同的实义片段（「设备采购预算」），或者目标里用来区分
        版本的编号/字母（「改用 B，不再用 A」里的 A）。后者也必须认 ——
        不认的话，正当的替代会被当成无关词拦下来。
        """
        if mentions_item(text, target_text):
            return True
        distinctive = changed_value_tokens(target_text, "")
        return bool(distinctive) and any(token in text for token in distinctive)

    @staticmethod
    def _cited_texts(suggestion: MemoryUpdateSuggestion, packet: Any) -> tuple[str, ...]:
        """被这条建议引用到的用户原话。没引用时退回本轮原话。"""
        if packet is None:
            return ()
        return tuple(text for text, _ in cited_user_evidence(packet, suggestion.source_refs))

    @staticmethod
    def _has_tool_evidence(
        suggestion: MemoryUpdateSuggestion, packet: Any, *, item_text: str = ""
    ) -> bool:
        """有没有**能当依据**的工具结果。

        三件事同时成立才算：来源类别说这是工具结果、这条回执不是失败、
        而且回执说的是这件事。以前只看「包里有没有任意一条回执」，于是
        「天气查询连接失败」也能授权去归档一条项目原则（B3-R2）。
        """
        if "tool_result" not in suggestion.source_types or packet is None:
            return False
        # 「查询成功」不等于「这条记录该作废」。工具结果要能当依据，除了不失败，
        # 还得**说到了要改的事**：回执里出现目标记录内容里的实义片段或数值，
        # 或者回执里带着用户明确要变更的表达。只报告现状（「当前为最近两版」）
        # 或查的是另一件事（「限行查询成功」）都不算。
        return bool(
            MemoryMaintenanceService._usable_tool_refs(
                packet, item_text=item_text, require_relevance=True, suggestion=suggestion
            )
        )

    @staticmethod
    def _tool_ref_reports_change(ref: Any, suggestion: Any) -> bool:
        """这条回执是不是**直接给出了要做的变更**，而不只是报告现状。

        「配置读取成功：回滚窗口当前为最近两版」这种回执只说明读到了现状；
        「核查失败：该原则已作废」这种才能支持把记录作废。判据是回执里
        有没有作废/更正/结论类措辞。
        """
        summary = str(getattr(ref, "summary", "") or "")
        return any(marker in summary for marker in _TOOL_CHANGE_MARKERS)

    @staticmethod
    def _tool_ref_addresses(ref: Any, item_text: str, suggestion: Any) -> bool:
        """回执说的是不是**这条变更要改的那件事**。

        要求回执和目标事项有共同实义片段/数值（事项绑定），或者回执本身就
        带着要变更的措辞。两者都不沾的话，这条回执与本次变更无关。
        """
        summary = str(getattr(ref, "summary", "") or "")
        if not summary.strip():
            return False
        if item_text and MemoryMaintenanceService._mentions_target(summary, item_text):
            return True
        return MemoryMaintenanceService._tool_ref_reports_change(ref, suggestion)

    @staticmethod
    def _usable_tool_refs(
        packet: Any,
        *,
        item_text: str = "",
        require_relevance: bool = False,
        suggestion: Any = None,
    ) -> tuple[Any, ...]:
        """包里可以用作依据的工具回执：没失败，而且（要求时）真的对得上这件事。"""
        usable: list[Any] = []
        for ref in getattr(packet, "evidence_refs", ()) or ():
            if MemoryMaintenanceService._tool_outcome(ref) == "failed":
                continue
            if item_text and not MemoryMaintenanceService._mentions_target(
                str(getattr(ref, "summary", "") or ""), item_text
            ) and not (
                require_relevance
                and MemoryMaintenanceService._tool_ref_reports_change(ref, suggestion)
            ):
                continue
            if require_relevance and not MemoryMaintenanceService._tool_ref_addresses(
                ref, item_text, suggestion
            ):
                continue
            usable.append(ref)
        return tuple(usable)

    @staticmethod
    def _tool_outcome(ref: Any) -> str:
        """一条工具回执的终局状态。没写状态时按「不知道」算，不按成功算。"""
        return str(getattr(ref, "outcome", "") or "").strip().lower() or "unknown"

    @staticmethod
    def _tool_evidence_scopes(
        packet: Any, suggestion: MemoryUpdateSuggestion, item_text: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """工具回执能证明的范围：``(有依据的数值, 有依据的实义片段)``。

        回执是**这条建议引用到的**那些，且必须对得上这件事。作用有两条：
        一是给新值逐项找依据，二是让「回执没说到的字段」不能跟着混进记录。
        """
        cited = set(str(ref) for ref in (suggestion.source_refs or ()) if ref)
        tokens: list[str] = []
        units: set[str] = set()
        for ref in MemoryMaintenanceService._usable_tool_refs(packet, item_text=item_text):
            ref_id = str(getattr(ref, "ref_id", "") or "")
            if cited and ref_id not in cited:
                continue
            summary = str(getattr(ref, "summary", "") or "")
            tokens.extend(value_tokens(summary))
            units.update(content_units(summary))
        return tuple(dict.fromkeys(tokens)), tuple(sorted(units))

    @staticmethod
    def _tool_evidence_texts(
        packet: Any, suggestion: MemoryUpdateSuggestion, item_text: str
    ) -> tuple[str, ...]:
        """成功工具回执里**能当依据**的那几段原文（没引用到的一条都不用）。"""
        if packet is None or "tool_result" not in (suggestion.source_types or ()):
            return ()
        cited = set(str(ref) for ref in (suggestion.source_refs or ()) if ref)
        texts: list[str] = []
        for ref in MemoryMaintenanceService._usable_tool_refs(packet, item_text=item_text):
            ref_id = str(getattr(ref, "ref_id", "") or "")
            if cited and ref_id not in cited:
                continue
            summary = str(getattr(ref, "summary", "") or "").strip()
            if summary:
                texts.append(summary)
        return tuple(dict.fromkeys(texts))

    def _value_evidence_gap(
        self,
        new_text: str,
        current_text: str,
        *,
        cited: tuple[str, ...],
        by_tool: bool,
        tool_scopes: tuple[tuple[str, ...], tuple[str, ...]] = ((), ()),
        rewording_expected: bool = False,
    ) -> str:
        """保留的**窄口**逐项核对：只对带值片段取证，供外部核验脚本调用。

        主判据已经统一到 :func:`agent_core.memory_evidence.evidence_gaps`
        （数值 + 评审结论 + 实施结果 + 条件 + 原因 + 适用范围一起核）。
        这个函数保留下来是因为 `verify_memory_final_falsification.py` 会直接
        调它做边界探针；产品链路不再用它。
        """
        if not new_text or not current_text:
            return ""
        by_user = tuple(
            token
            for token in changed_value_tokens(new_text, current_text)
            if token and (not rewording_expected or _is_value_bearing(token))
        )
        tool_values = tuple(tool_scopes[0] or ())
        unsupported: list[str] = []
        for token in dict.fromkeys(by_user):
            # 「三千」和「3000」是同一个值：比的是值，不是写法（B3-R4）。
            if any(value_evidenced(text, token) for text in cited):
                continue
            if by_tool and value_evidenced(" ".join(tool_values), token):
                continue
            unsupported.append(token)
        if not unsupported:
            return ""
        # 工具回执把范围钉死后，内容里多出来的实义片段同样要单独交代。
        if by_tool:
            covered_units = set(tool_scopes[1] or ()) | set(tool_values)
            extra = [
                unit
                for unit in content_units(new_text)
                if unit not in content_units(current_text)
                and unit not in covered_units
                and not any(unit in text for text in cited)
                and not any(unit in token for token in unsupported)
                and not any(token in unit for token in unsupported)
            ]
            unsupported.extend(sorted(extra)[:3])
        return (
            "新值缺少原话依据：新内容里变的「"
            + "、".join(unsupported[:3])
            + "」在用户原话里找不到，不能只凭模型判断覆盖这条记录"
        )

    @staticmethod
    def _states_claim(clause: str, *, item_text: str = "") -> bool:
        """这一句有没有「这件事该怎么算」的结论（纠正、采纳、决策措辞）。

        判的是**标记**，不带任何数值条件：「不是周一，是周二开会」和
        「不是五千，是三千」结构一样 —— 都是「旧说法错了 + 新结论」。
        哪一句是哪一件事由调用方按事项绑定，这里只负责分句。
        """
        if not clause:
            return False
        if any(marker in clause for marker in _CORRECTION_MARKERS):
            return True
        if any(marker in clause for marker in _REPLACEMENT_MARKERS):
            return True
        return False

    def _undecided_fields(self, new_text: str, packet: Any, item_text: str) -> tuple[str, ...]:
        """新内容里**用户明说未定**的字段。这些字段一律不许被填上。

        用户说「交付日期尚未确定」时，模型把日期写成 2026-12-31 也照样要拦：
        「没说」和「还等着定」是两件事，后者明确要求这一项留空。
        """
        if packet is None or not str(new_text or "").strip():
            return ()
        target_text = str(item_text or "").strip()
        undecided_clauses = MemoryMaintenanceService._subject_clauses(
            packet,
            target_text,
            keywords=self._field_tokens(new_text, target_text),
        )
        undecided_clauses = tuple(
            clause
            for clause in undecided_clauses
            if any(marker in clause for marker in _UNDECIDED_MARKERS)
        )
        if not undecided_clauses:
            return ()
        blocked: list[str] = []
        current_units = content_units(target_text)
        for field in self._new_fields(new_text, target_text):
            name = _FIELD_SEPARATOR.split(field, 1)[0].strip()
            if not name:
                continue
            probes = self._field_probe(name)
            # 字段名先得**真的对上用户那句话**，而且那句话讲的是「用户还没定的
            # 那一项」。用户说「设备采购预算还没变化」时，新内容里的「设备采购
            # 预算」和目标正文是同一个话题（不是新字段），不该按未定拦截 ——
            # 那是“值有没有原话依据”该管的事。
            if not any(probe in clause for probe in probes for clause in undecided_clauses):
                continue
            if any(probe in current_units for probe in probes):
                continue
            if any(probe in target_text for probe in probes):
                continue
            blocked.append(name)
        return tuple(dict.fromkeys(blocked))

    @staticmethod
    def _field_name(clause: str, old_units: set[str]) -> str:
        """一句话里的字段名：这句新出现的汉字，按「完整汉字串」取。

        走连续汉字串而不是二元组：标题「交付时间为」会同时给出「交付」「付时」
        「时间」「间为」，按二元组排序会得到「付时间」这种不存在的词。
        取整串（「交付时间为」）后再按用户原话做子串比对 —— 用户说的
        「交付日期尚未确定」与它对不上整串，但下面还会退回到逐段匹配。
        """
        names: list[str] = []
        for run in _CJK_RUN.findall(clause):
            fresh = [unit for unit in content_units(run) if unit not in old_units]
            if fresh:
                names.append(run)
        return names[0] if names else ""

    @staticmethod
    def _field_probe(name: str) -> tuple[str, ...]:
        """字段名的比对写法：整串，以及「前两字 + 后两字」这种常见别名。

        「交付时间为」比对「交付日期尚未确定」：整串对不上，但「交付」对得上。
        只取长度 ≥2 的探针，单字太泛。
        """
        if not name:
            return ()
        probes = {name}
        if len(name) >= 4:
            probes.add(name[:2])
            probes.add(name[-2:])
        return tuple(sorted(probe for probe in probes if len(probe) >= 2))

    @staticmethod
    def _field_tokens(new_text: str, current_text: str) -> tuple[str, ...]:
        """新内容里新增的字段名探针（「交付时」），用来找到用户说它的那一句。"""
        raw = _GROUPED_DIGITS.sub("", str(new_text or ""))
        old_units = content_units(current_text)
        tokens: list[str] = []
        for clause in clauses(raw):
            name = MemoryMaintenanceService._field_name(clause, old_units)
            tokens.extend(MemoryMaintenanceService._field_probe(name))
        return tuple(dict.fromkeys(tokens))

    @staticmethod
    def _new_fields(new_text: str, current_text: str) -> tuple[str, ...]:
        """新内容里相对旧内容**新增的字段**：「交付时间为」「预算三」这类片段。

        字段名用来核对用户原话里有没有说这一项，例如「交付日期尚未确定」
        里的「交付日期」。带上同一个句子里的第一个数，是为了同时覆盖
        「预算三千」这种写法。
        """
        if not str(new_text or "").strip():
            return ()
        raw = _GROUPED_DIGITS.sub("", str(new_text))
        old_units = content_units(current_text)
        fields: list[str] = []
        for clause in clauses(raw):
            name = MemoryMaintenanceService._field_name(clause, old_units)
            if not name:
                continue
            numbers = value_tokens(clause)
            fields.append(f"{name}{numbers[0]}" if numbers else name)
        return tuple(fields)

    def _invalidation_gap(
        self, suggestion: MemoryUpdateSuggestion, target: dict | None, packet: Any
    ) -> str:
        """失效建议缺哪条证据。返回空串表示证据成立、可以真的归档。

        长期原则不能因为「模型觉得旧了」就作废：要么用户自己说这件事不作数了，
        要么有可信工具结果。只有模型给出的理由时，内容状态不动，
        只记下缺哪条证据、下次从哪里取证。
        """
        if self._has_tool_evidence(suggestion, packet):
            return ""
        item_text = str(suggestion.content or "").strip() or str(
            (target or {}).get("content") or ""
        )
        for text in self._cited_texts(suggestion, packet):
            if expresses_invalidation(text, item_text=item_text):
                return ""
        return (
            "缺少来源依据：引用里没有用户说这件事不作数的原话，也没有可信工具结果；"
            "模型自己给的理由不算证据"
        )

    def _record_evidence_gap(
        self, record_id: str, *, suggestion: MemoryUpdateSuggestion, missing: str
    ) -> None:
        """把「缺哪条证据、下次从哪里取证」写进记录。

        不写这一笔，这次拒绝在系统里就只剩一行日志：下次复核时没人知道
        这条记录为什么还挂着，也没人知道该去找什么。写失败不影响本次判定。
        """
        try:
            with self.repositories.connect() as db:
                row = db.execute(
                    "SELECT metadata FROM memory_records WHERE id=?", (record_id,)
                ).fetchone()
                if row is None:
                    return
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["review_gap"] = {
                    "missing": missing,
                    "action": suggestion.action,
                    "how_to_verify": (
                        "在下一次带原出处的处理任务里核对用户是否说过该事项不再适用，"
                        "或等到可信工具结果；取证前不改内容状态"
                    ),
                    "at": _now_text(),
                }
                db.execute(
                    "UPDATE memory_records SET metadata=?,updated_at=? WHERE id=?",
                    (
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        _now_text(),
                        record_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响本次拒绝判定
            LOGGER.warning("记录复核缺口失败：错误类型=%s", type(exc).__name__)

    # ------------------------------------------------------------------
    # 变更通知：实际发出去没有
    # ------------------------------------------------------------------
    @contextmanager
    def _probe_change_emission(
        self, scope: Scope, *, target: dict | None, turn_id: str
    ):
        """在这次写入期间盯着 writer 的通知口，如实记下「发出去了没有」。

        写入端自己不抛通知异常（记录已经落库，通知失败不该让写入跟着失败），
        所以只看「配没配回调」是不够的：回调抛异常时回执上写着「已发出同步」，
        而云端根本没收到这一笔（B3-R6）。

        这里的做法是在写入**期间**给通知口加一层探针，把失败按回调身份记下来。
        探针只在这一次 ``apply_semantic_update`` 调用存在，且按回调实例记账：
        并发场景下别处捕获的失败不会算到这次头上。
        """
        report = _NotificationReport()
        writer = self.writer
        original = getattr(writer, "on_change", None)
        if original is None:
            yield report
            return
        probe = _ProbeCallback(original, report)
        try:
            writer.on_change = probe
        except Exception:  # noqa: BLE001 - 写入端锁住这个属性时退回「配置即乐观」
            yield report
            return
        try:
            yield report
        finally:
            # 只在还是我们那层探针时摘掉：别的线程中途换了回调，不能替他改回去。
            try:
                if getattr(writer, "on_change", None) is probe:
                    writer.on_change = original
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("恢复变更通知口失败：错误类型=%s", type(exc).__name__)

    def _record_notification_failure(
        self,
        scope: Scope,
        failure: str,
        *,
        target: dict | None,
        turn_id: str,
    ) -> None:
        """通知失败落一行审计：这次写入的同步缺口必须留痕，不能只写日志。"""
        scope_id = (
            f"{scope.owner_id}|{scope.channel}|{scope.conversation_id}|{scope.module_id}"
        )
        LOGGER.warning(
            "变更通知失败，同步缺口已记账：范围=%s 目标=%s 回合=%s 错误=%s",
            scope_id,
            str((target or {}).get("id") or "")[:8],
            turn_id or "-",
            failure,
        )

    def _mark_pending_sync(self, record: dict | None, failure: str) -> None:
        """在记录元数据里留下「这笔变更还没同步」的标记，等下一轮对账补发。

        只写日志是不够的：进程重启后没人知道这一笔漏了。标记写在库里，
        下一轮全量对账能按它找出漏发的变更。
        """
        record_id = str((record or {}).get("id") or "")
        if not record_id:
            return
        try:
            with self.repositories.connect() as db:
                row = db.execute(
                    "SELECT metadata FROM memory_records WHERE id=?", (record_id,)
                ).fetchone()
                if row is None:
                    return
                try:
                    metadata = json.loads((row["metadata"] if not isinstance(row, dict) else row.get("metadata")) or "{}")
                except (TypeError, ValueError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                metadata["pending_sync"] = {
                    "reason": failure[:200],
                    "at": _now_text(),
                }
                db.execute(
                    "UPDATE memory_records SET metadata=?,updated_at=? WHERE id=?",
                    (
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                        _now_text(),
                        record_id,
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响本次回执
            LOGGER.warning("写入待同步标记失败：错误类型=%s", type(exc).__name__)

    def _pending_sync_flag(self, metadata: dict) -> bool:
        """记录里有没有还没补发的同步缺口。"""
        placeholder = metadata.get("pending_sync")
        return bool(placeholder)

    # ------------------------------------------------------------------
    # 应用 + 写后复核
    # ------------------------------------------------------------------
    def apply(
        self,
        suggestion: MemoryUpdateSuggestion,
        *,
        scope: Scope,
        packet: Any = None,
        turn_id: str = "",
        occurred_at: str = "",
        project_id: str = "",
        prior_receipt: Any = None,
    ) -> dict:
        """校验 → 原子应用 → 写后复核；返回一条完整回执。

        任何一步没过就 ``applied=False`` 并把原因写回来，不静默跳过。
        """
        outcome: dict[str, Any] = {
            "action": suggestion.action,
            "applied": False,
            "reason": "",
            "record": None,
            "archived": [],
            "verify": {},
        }
        target: dict | None = None
        # 可信项目绑定优先用调用方给的那个；没给就从**处理包**里取。
        # 从包的环境变量重新读是错的：两端环境不同时同一个包会读成不同范围。
        bound = project_id or str(getattr(packet, "project_binding", "") or "")
        if suggestion.action in ACTIONS_REQUIRING_TARGET:
            allowed, _ = self.read_targets(
                scope, [suggestion.target_id, *suggestion.conflict_ids], project_id=bound
            )
            target = next(
                (row for row in allowed if str(row["id"]) == suggestion.target_id), None
            )
            if target is None:
                outcome["reason"] = "目标记录不可见或不存在"
                return outcome
            known_versions = {str(row["id"]): int(row.get("record_version") or 0) for row in allowed}
            try:
                validate_suggestion(
                    suggestion,
                    known_ids=[str(row["id"]) for row in allowed],
                    known_versions=known_versions,
                )
            except MemoryCandidateError as exc:
                outcome["reason"] = str(exc)
                return outcome
        refusal = self._refuse_reason(suggestion, target=target, packet=packet)
        if refusal:
            outcome["reason"] = refusal
            return outcome

        if suggestion.action == "invalidate":
            gap = self._invalidation_gap(suggestion, target, packet)
            if gap:
                return self._apply_review_only(
                    suggestion,
                    outcome=outcome,
                    target=target,
                    scope=scope,
                    packet=packet,
                    turn_id=turn_id,
                    occurred_at=occurred_at,
                    missing=gap,
                )

        with self._probe_change_emission(scope, target=target, turn_id=turn_id) as emission:
            lifetime = self._lifetime_for(suggestion, packet)
            evidence_receipt = self._evidence_receipt(
                suggestion, target=target, packet=packet, lifetime=lifetime,
                prior_receipt=prior_receipt,
            )
            outcome['evidence_receipt'] = evidence_receipt
            if suggestion.action in ('correct', 'replace', 'merge') and suggestion.content and not evidence_receipt['passed']:
                outcome.update(reason='evidence_not_supported', refused=evidence_receipt['verdict'] == 'unsupported',
                               pending_review=evidence_receipt['verdict'] == 'uncertain')
                return outcome
            try:
                result = self.writer.apply_semantic_update(
                    scope,
                    suggestion,
                    target=target,
                    turn_id=turn_id,
                    occurred_at=occurred_at or str(getattr(packet, "occurred_at", "") or ""),
                    review_interval=self.review_interval,
                    lifetime=lifetime,
                    receipt=evidence_receipt,
                    packet=packet,
                )
            except (MemoryCandidateError, ValueError) as exc:
                # 取证不足、权限不够、版本前提不成立：这是**拒绝**，重试也不会变。
                outcome["reason"] = f"{type(exc).__name__}: {exc}"
                outcome["refused"] = True
                return outcome
            except Exception as exc:  # noqa: BLE001 - 存储或执行失败是可恢复的
                # 数据库写不进去、进程中途出错：**不能当业务拒绝**。建议和错误都留着，
                # 由队列按次数重试；回执里写清楚这是执行失败而不是「不建议这么做」。
                outcome["reason"] = f"{type(exc).__name__}: {exc}"
                outcome["failure"] = True
                outcome["retryable"] = True
                LOGGER.warning(
                    "内容维护执行失败（可恢复）：action=%s 错误类型=%s",
                    suggestion.action,
                    type(exc).__name__,
                )
                return outcome

        record = result.get("record") or result.get("bumped")
        outcome["applied"] = True
        outcome["replayed"] = bool(result.get("replayed"))
        outcome["record"] = record
        outcome["evidence_receipt"] = evidence_receipt
        outcome["archived"] = [str(row.get("id")) for row in result.get("archived", ())]
        if emission.failures:
            # 记录已经写进库，但变更通知没发出去。同步层收不到这一笔，
            # 账就要记在这条回执上：留待同步的标记（pending_sync）不能丢。
            outcome["sync_pending"] = True
            outcome["pending_sync"] = 1
            outcome["sync_error"] = emission.failures[0]
            self._record_notification_failure(
                scope,
                emission.failures[0],
                target=target,
                turn_id=turn_id,
            )
            self._mark_pending_sync(record, emission.failures[0])
        outcome["verify"] = self.verify(
            record,
            suggestion=suggestion,
            archived=result.get("archived", ()),
            notifications=emission,
        )
        return outcome

    def _apply_review_only(
        self,
        suggestion: MemoryUpdateSuggestion,
        *,
        outcome: dict,
        target: dict | None,
        scope: Scope,
        packet: Any,
        turn_id: str,
        occurred_at: str,
        missing: str,
    ) -> dict:
        """证据不足的失效建议：内容状态不动，只排一次复核并记下缺哪条证据。

        「模型觉得这条太旧了」不是失效依据。这里既不归档，也不当成业务拒绝丢掉 ——
        记下缺什么、下次去哪取证，等真凭据到了再改状态。
        """
        review_only = replace(suggestion, reason="", content="")
        try:
            result = self.writer.apply_semantic_update(
                scope,
                review_only,
                target=target,
                turn_id=turn_id,
                occurred_at=occurred_at or str(getattr(packet, "occurred_at", "") or ""),
                review_interval=self.review_interval,
            )
        except (MemoryCandidateError, ValueError) as exc:
            outcome["reason"] = f"{type(exc).__name__}: {exc}"
            outcome["refused"] = True
            return outcome
        except Exception as exc:  # noqa: BLE001 - 执行失败同样要能重试
            outcome["reason"] = f"{type(exc).__name__}: {exc}"
            outcome["failure"] = True
            outcome["retryable"] = True
            return outcome
        record = result.get("record") or result.get("bumped")
        if target is not None:
            self._record_evidence_gap(str(target["id"]), suggestion=suggestion, missing=missing)
        outcome["applied"] = True
        outcome["review_only"] = True
        outcome["reason"] = missing
        outcome["record"] = record
        outcome["verify"] = self.verify(record, suggestion=suggestion)
        return outcome

    def verify(
        self,
        record: dict | None,
        *,
        suggestion: MemoryUpdateSuggestion | None = None,
        archived: Any = (),
        notifications: Any = None,
    ) -> dict:
        """写后复核：从库里核对新版本、来源、状态，并分开记「保存/检索/同步」。

        数据库写成功、向量或同步没跟上时**分别**记账，不能因为「记录写进去了」
        就回执整件事成功。写入失败更不许回执成已入库 —— 这一条不问模型怎么说。

        ``notifications`` 是写这次变更时通知口的真实结果（没给就按「本地不发通知」
        算）。**配了回调不等于发出去了**：回调抛异常时同步是 ``failed``，
        而且这件事算未完成（``partial``）—— 漏掉的变更要靠 ``pending_sync``
        这个留待同步的标记在下一轮补发，绝不能在回执上写成「已同步」。
        """
        if record is None:
            return {"saved": False, "retrieval": "not_applicable", "sync": "not_applicable"}
        fresh = self.repositories.records_by_ids([str(record["id"])]).get(str(record["id"]))
        if fresh is None:
            return {
                "saved": False,
                "retrieval": "missing",
                "sync": "missing",
                "reason": "写入之后从库里读不到这条记录",
            }
        try:
            metadata = json.loads(fresh.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        retrieval = str(fresh.get("embedding_status") or "pending")
        report = notifications
        failures = list(getattr(report, "failures", ()) or ())
        configured = bool(getattr(report, "configured", False)) or (
            getattr(self.writer, "on_change", None) is not None
        )
        pending_sync = self._pending_sync_flag(metadata)
        if failures or pending_sync:
            sync = "failed"
        elif configured:
            sync = "emitted"
        else:
            sync = "local_only"
        notification = {
            "state": sync,
            "configured": configured,
            "calls": int(getattr(report, "calls", 0) or 0),
            "failures": failures,
            "pending": bool(pending_sync),
        }
        return {
            "saved": True,
            "record_id": str(fresh["id"]),
            "record_version": int(fresh.get("record_version") or 0),
            "content_status": fresh.get("content_status"),
            "effective_record_id": fresh.get("effective_record_id"),
            "sources": list(metadata.get("source_refs") or []),
            "retrieval": retrieval,
            "retrieval_ok": retrieval == "ready",
            "archived": [str(row.get("id")) for row in archived],
            "sync": sync,
            "notification": notification,
            "pending_sync": bool(pending_sync),
            "partial": retrieval != "ready" or sync == "failed",
        }


class MemoryMaintenanceScheduler:
    """定期内容复核：按间隔挑一批到点的记录。

    **没有新证据时不调模型。** 到点只表示「该看一眼」，真正要看的是「有没有新东西
    能解释它」—— 而新证据只会随新回合进来。所以这一趟做的事就是「把还没等到证据
    的记录排到下一次复核」，模型调用数为 0。

    反过来做（到点就调模型问「这条还对不对」）会自我循环：模型对着同一条记录
    反复想，然后把它的重复意见当成新证据，越维护越乱。
    """

    def __init__(
        self,
        service: MemoryMaintenanceService,
        *,
        interval_seconds: int = DEFAULT_REVIEW_INTERVAL_SECONDS,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.service = service
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size

    def run_once(self, *, now: datetime | None = None) -> dict:
        due = self.service.pick_due(now=now, limit=self.batch_size)
        if not due:
            return {"due": 0, "deferred": 0, "model_calls": 0}
        for row in due:
            self.service.mark_reviewed(str(row["id"]), settled=False)
        LOGGER.info(
            "定期内容复核：到点 %d 条，全部排入下一次复核（没有新证据，未调模型）", len(due)
        )
        return {"due": len(due), "deferred": len(due), "model_calls": 0}

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            self.run_once()
            if stop is None:
                await asyncio.sleep(self.interval_seconds)
            else:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_seconds)
                except asyncio.TimeoutError:
                    continue
