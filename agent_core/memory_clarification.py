"""当前会话的最小澄清闭环（实施工作 B）。

背景问题：后台代理遇到"用户这句话我判断不准"时，会把疑点留在待确认回执里，
主聊天却完全看不到 —— 用户含糊改口之后，系统既不会问一句，也不会把回答接回原事项。

这个模块只做四件事，每一件都复用现有队列数据库，不新增库、不新增向量索引：

1. **读疑点**：从当前账号/渠道/模块/会话范围内、由现有代理产生且**有真实原话**
   的未解决判断里取候选，每轮最多给主模型三项（展示限额，不是语义判断）。
2. **登记提问草稿**：主模型通过内部工具登记"这一轮想问的一句话"，最多一条。
   草稿不改记忆、不表示已发布。
3. **记录发布**：只有最终确实发布、且回合成功完成时，才把该疑点记为 ``asked``。
4. **解决关联**：下一轮带上"可能相关"的关联交给现有后台代理，由**模型**判断
   是否真的回答了问题；模型确认并写入成功后，才记为 ``resolved``。

明确不做：跨会话发现全部疑点、长期不确定记忆正式入库、云端澄清同步、
自动周期提醒、替模型编造语义结论。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .memory_logging import configure_memory_logging

LOGGER = logging.getLogger("agent_core.memory.clarification")

#: 状态机最小值：等待回答 / 已经问过 / 已解决。
STATE_WAITING = "waiting"
STATE_ASKED = "asked"
STATE_RESOLVED = "resolved"
CLARIFICATION_STATES = (STATE_WAITING, STATE_ASKED, STATE_RESOLVED)

#: 每轮最多给主模型看的候选数（展示限额）。
MAX_CANDIDATES_PER_TURN = 3

#: 每次读取时扫描的最近待确认作业条数。
SCAN_LIMIT = 20

#: 这些错误是**程序或配置故障**，不是"用户需要澄清"的语义疑点。
#: 把它们变成用户疑点会让用户去回答一个系统问题 —— 明确排除。
NON_SEMANTIC_ERRORS = frozenset({
    "agent_model_timeout",
    "agent_budget_exhausted",
    "agent_model_budget_exhausted",
    "agent_token_budget_exhausted",
    "agent_tool_budget_exhausted",
    "agent_task_deadline",
    "agent_attempts_exhausted",
    "agent_model_failed",
    "repeated_tool_without_new_information",
    "invalid_protocol",
    "protocol_error",
    "empty_response",
    "no_configuration",
    "missing_configuration",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(_text(item) for item in value).strip()
    if isinstance(value, Mapping):
        return _text(value.get("content") or value.get("text") or value.get("quote"))
    return str(value).strip()


@dataclass(frozen=True)
class ClarificationItem:
    """一条疑点的稳定身份 + 状态。

    稳定编号是 **「作业号＋原方案的事项路径」**：多事项或 ``parts`` 不能只用外层
    index，否则同一轮里的不同疑点会混成一个。
    """

    candidate_id: str
    job_id: str
    item_path: str
    state: str = STATE_WAITING
    quote: str = ""
    reason: str = ""
    suggested_question: str = ""
    asked_question: str = ""
    asked_turn: str = ""
    answered_turn: str = ""
    resolved_record_id: str = ""
    target_id: str = ""
    target_version: int = 0
    deadline: str = ""
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "job_id": self.job_id,
            "item_path": self.item_path,
            "state": self.state,
            "quote": self.quote,
            "reason": self.reason,
            "suggested_question": self.suggested_question,
            "asked_question": self.asked_question,
            "asked_turn": self.asked_turn,
            "answered_turn": self.answered_turn,
            "resolved_record_id": self.resolved_record_id,
            "target_id": self.target_id,
            "target_version": self.target_version,
            "deadline": self.deadline,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "ClarificationItem":
        return cls(
            candidate_id=str(row.get("candidate_id") or ""),
            job_id=str(row.get("job_id") or ""),
            item_path=str(row.get("item_path") or ""),
            state=str(row.get("state") or STATE_WAITING),
            quote=_text(row.get("quote")),
            reason=str(row.get("reason") or ""),
            suggested_question=str(row.get("suggested_question") or ""),
            asked_question=str(row.get("asked_question") or ""),
            asked_turn=str(row.get("asked_turn") or ""),
            answered_turn=str(row.get("answered_turn") or ""),
            resolved_record_id=str(row.get("resolved_record_id") or ""),
            target_id=str(row.get("target_id") or ""),
            target_version=int(row.get("target_version") or 0),
            deadline=str(row.get("deadline") or ""),
            created_at=str(row.get("created_at") or ""),
        )

    def to_background(self) -> dict[str, Any]:
        """给主聊天看的展示形态（对应 chat_background.PendingClarification）。"""
        return {
            "candidate_id": self.candidate_id,
            "job_id": self.job_id,
            "item_path": self.item_path,
            "quote": self.quote,
            "reason": self.reason,
            "suggested_question": self.suggested_question,
            "target_id": self.target_id,
            "target_version": self.target_version,
            # 期限用已有判断；缺失就标未知 —— 不默认一天、不另建清理任务。
            "deadline": self.deadline or "未知",
        }


def item_path_of(item: Mapping[str, Any], *, outer_index: int, part_index: int | None = None) -> str:
    """算出稳定的事项路径。

    优先用方案自带的路径字段；没有就按"第几项（第几部分）"拼一个**可复现**的路径，
    绝不使用会随顺序变化的裸下标。
    """
    for key in ("item_path", "path", "target_path", "subject"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    parts = [f"item[{outer_index}]"]
    if part_index is not None:
        parts.append(f"part[{part_index}]")
    return ".".join(parts)


def candidate_id_of(job_id: str, item_path: str) -> str:
    """稳定编号：作业号 + 事项路径。"""
    return f"{job_id}::{item_path}"


def is_pending_semantic_job(job: Mapping[str, Any]) -> bool:
    """这个作业是不是"有真实原话的待确认语义判断"。

    排除程序/配置故障：格式错误、超时、空响应、无配置、程序核对故障都不算用户疑点。
    """
    if not job:
        return False
    error = str(job.get("last_error") or "").strip()
    if error in NON_SEMANTIC_ERRORS or error.startswith("agent_model_failed:"):
        return False
    return True


class ClarificationStore:
    """疑点状态的持久层。复用现有作业表的 ``clarification_state`` 文本列。"""

    table = "memory_candidate_jobs"
    key = "job_id"

    def __init__(self, db_path: str | Path) -> None:
        configure_memory_logging()
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # 连接与初始化
    # ------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def initialize(self) -> None:
        """补一个可选的 ``clarification_state`` 文本列；旧行缺省为空。

        只在隔离库验证；生产初始化与迁移不在本次授权范围。
        """
        with self.connect() as db:
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({self.table})")}
            if not columns:
                raise sqlite3.OperationalError(f"表 {self.table} 不存在，不能初始化澄清列")
            if "clarification_state" not in columns:
                db.execute(f"ALTER TABLE {self.table} ADD COLUMN clarification_state TEXT")

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _read_state(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        raw = job.get("clarification_state")
        if not raw:
            return []
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            LOGGER.warning("clarification_state 解析失败，按空处理")
            return []
        items = data.get("items") if isinstance(data, Mapping) else None
        return [dict(item) for item in (items or []) if isinstance(item, Mapping)]

    def _write_state(self, job_id: str, items: Iterable[Mapping[str, Any]]) -> None:
        payload = json.dumps({"items": [dict(item) for item in items]}, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                f"UPDATE {self.table} SET clarification_state=?,updated_at=? WHERE {self.key}=?",
                (payload, _now(), job_id),
            )

    def job(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(f"SELECT * FROM {self.table} WHERE {self.key}=?", (job_id,)).fetchone()
        return dict(row) if row else None

    def candidates(
        self,
        *,
        scope_key: str,
        limit: int = MAX_CANDIDATES_PER_TURN,
        scan_limit: int = SCAN_LIMIT,
        seed: bool = True,
    ) -> list[ClarificationItem]:
        """当前范围内的待确认疑点候选。

        只看**同范围**（账号/渠道/模块/会话）最近若干条作业，按时间倒序读，
        再取前 ``limit`` 项。这是展示限额，不是语义判断 —— 选不选由主模型决定。

        ``seed=True``（默认）时，会先把这些作业里**由现有代理产生、且带真实原话**
        的未解决判断登记成疑点（只登记一次，已登记的不覆盖）。程序故障类
        （格式错误、超时、空响应、无配置、核对故障）不会被登记成用户疑点。

        读取是只读语义的：它不会因为打开一次提问就把状态改成 ``asked``。
        """
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM {self.table} WHERE scope_key=? ORDER BY created_at DESC LIMIT ?",
                (scope_key, int(scan_limit)),
            ).fetchall()
        jobs = [dict(row) for row in rows if is_pending_semantic_job(dict(row))]
        if seed:
            for job in jobs:
                try:
                    self.seed_from_job(job)
                except Exception:  # noqa: BLE001 - 单个作业解析失败不影响其余
                    LOGGER.exception("登记疑点失败：%s", job.get("job_id"))
            # 重新读一遍，带上刚登记的条目。
            with self.connect() as db:
                rows = db.execute(
                    f"SELECT * FROM {self.table} WHERE scope_key=? ORDER BY created_at DESC LIMIT ?",
                    (scope_key, int(scan_limit)),
                ).fetchall()
            jobs = [dict(row) for row in rows if is_pending_semantic_job(dict(row))]
        found: list[ClarificationItem] = []
        for job in jobs:
            for item in self._read_state(job):
                if str(item.get("state") or STATE_WAITING) == STATE_RESOLVED:
                    continue
                found.append(ClarificationItem.from_mapping(item))
            if len(found) >= limit:
                break
        return found[:limit]

    def resolve_item(self, job_id: str, candidate_id: str) -> ClarificationItem | None:
        """按稳定编号取一条疑点（服务端校验范围时用）。"""
        job = self.job(job_id)
        if not job:
            return None
        for item in self._read_state(job):
            if candidate_id_of(job_id, str(item.get("item_path") or "")) == candidate_id:
                return ClarificationItem.from_mapping(item)
        return None

    # ------------------------------------------------------------------
    # 疑点来源：由现有代理的未解决判断派生
    # ------------------------------------------------------------------
    def seed_from_job(self, job: Mapping[str, Any], *, packet: Mapping[str, Any] | None = None) -> list[ClarificationItem]:
        """从作业的 ``processing_result`` / ``agent_state`` 里提取疑点。

        只提取**有真实原话**的未解决判断。原方案、原响应和原回执不覆盖 ——
        这里只是读出候选，写回只发生在登记提问/解决关联时。
        """
        job_id = str(job.get("job_id") or "")
        if not job_id:
            return []
        existing = {
            candidate_id_of(job_id, str(item.get("item_path") or "")): item
            for item in self._read_state(job)
        }
        rows: list[dict[str, Any]] = []
        for source_key in ("processing_result", "agent_state"):
            raw = job.get(source_key)
            if not raw:
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            for item in _iter_unresolved_items(data):
                path = item_path_of(item, outer_index=item.pop("__index__", 0),
                                    part_index=item.pop("__part_index__", None))
                candidate_id = candidate_id_of(job_id, path)
                if candidate_id in existing:
                    continue
                rows.append({
                    "candidate_id": candidate_id,
                    "job_id": job_id,
                    "item_path": path,
                    "state": STATE_WAITING,
                    "quote": _text(item.get("quote")),
                    "reason": str(item.get("reason") or item.get("doubt") or ""),
                    "suggested_question": str(item.get("clarification_question") or ""),
                    "target_id": str(item.get("target_id") or ""),
                    "target_version": int(item.get("target_version") or 0),
                    "deadline": str(item.get("lifetime_deadline") or ""),
                    "created_at": _now(),
                })
        if rows:
            self._write_state(job_id, list(existing.values()) + rows)
        return [ClarificationItem.from_mapping(row) for row in rows]

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------
    def mark_asked(self, job_id: str, candidate_id: str, *, question: str, turn_id: str) -> ClarificationItem | None:
        """记为 ``asked``。只应在"最终确实发布了这条问题且回合成功完成"之后调用。"""
        return self._transition(
            job_id, candidate_id,
            updates={"state": STATE_ASKED, "asked_question": question, "asked_turn": turn_id},
            allowed_from=(STATE_WAITING, STATE_ASKED),
        )

    def mark_resolved(
        self,
        job_id: str,
        candidate_id: str,
        *,
        answer_turn: str,
        record_id: str = "",
    ) -> ClarificationItem | None:
        """记为 ``resolved``。只在**新判断和提交都成功**之后调用。"""
        return self._transition(
            job_id, candidate_id,
            updates={"state": STATE_RESOLVED, "answered_turn": answer_turn,
                     "resolved_record_id": record_id},
            allowed_from=(STATE_WAITING, STATE_ASKED),
        )

    def _transition(
        self,
        job_id: str,
        candidate_id: str,
        *,
        updates: Mapping[str, Any],
        allowed_from: tuple[str, ...],
    ) -> ClarificationItem | None:
        job = self.job(job_id)
        if not job:
            return None
        items = self._read_state(job)
        changed: dict[str, Any] | None = None
        for item in items:
            if candidate_id_of(job_id, str(item.get("item_path") or "")) != candidate_id:
                continue
            if str(item.get("state") or STATE_WAITING) not in allowed_from:
                return ClarificationItem.from_mapping(item)
            item.update({key: value for key, value in updates.items() if not isinstance(value, dict)})
            changed = item
            break
        if changed is None:
            return None
        self._write_state(job_id, items)
        return ClarificationItem.from_mapping(changed)


def _iter_unresolved_items(node: Any, *, _depth: int = 0) -> Iterable[dict[str, Any]]:
    """在作业结果里找出"未解决且有原话"的事项。

    遍历嵌套结构与 ``parts``；每项带上它的**路径位置**，供稳定编号使用。
    只认有 ``quote`` 的项 —— 没有原话的判断不构成用户疑点。
    """
    if _depth > 6:
        return
    if isinstance(node, Mapping):
        for key in ("items", "parts", "records", "results", "plan"):
            child = node.get(key)
            if isinstance(child, (list, tuple)):
                for outer_index, entry in enumerate(child):
                    if not isinstance(entry, Mapping):
                        continue
                    if _looks_unresolved(entry):
                        row = dict(entry)
                        row["__index__"] = outer_index
                        yield row
                    for part_index, part in enumerate(
                        [p for p in (entry.get("parts") or []) if isinstance(p, Mapping)]
                    ):
                        if _looks_unresolved(part):
                            row = dict(part)
                            row["__index__"] = outer_index
                            row["__part_index__"] = part_index
                            yield row
        if _looks_unresolved(node) and not any(
            isinstance(node.get(k), (list, tuple)) for k in ("items", "parts", "records", "results")
        ):
            row = dict(node)
            row["__index__"] = 0
            yield row


def _looks_unresolved(entry: Mapping[str, Any]) -> bool:
    """未解决 = 判决/状态表示不确定，且带着真实原话。"""
    verdict = str(entry.get("verdict") or entry.get("status") or entry.get("decision") or "").lower()
    unresolved = verdict in {"unresolved", "uncertain", "ambiguous", "unclear", "pending", "doubt", "conflict"}
    if not unresolved:
        doubtful = bool(entry.get("doubt") or entry.get("needs_clarification") or entry.get("ambiguous"))
        unresolved = doubtful
    if not unresolved:
        return False
    # 必须有真实原话，否则不构成"用户需要澄清"的问题。
    return bool(_text(entry.get("quote") or entry.get("source_quote") or entry.get("user_text")))


@dataclass
class ClarificationDraft:
    """本轮登记的提问草稿。只存活一轮，不代表已发布、不改记忆。"""

    candidate_id: str = ""
    question: str = ""
    job_id: str = ""
    reason_absent: str = ""
    recorded: bool = False

    @property
    def active(self) -> bool:
        return bool(self.candidate_id and self.question)


class ClarificationTurn:
    """一个回合内的澄清登记与发布记账。

    把它做成**回合级对象**，是因为"草稿 → 实际发布 → 记 asked"必须绑定同一个回合：
    取消、超时、核查删掉的问题都不能记已问。
    """

    def __init__(self, store: ClarificationStore | None, *, scope_key: str, turn_id: str,
                 candidates: Iterable[ClarificationItem] = ()) -> None:
        self.store = store
        self.scope_key = scope_key
        self.turn_id = turn_id
        self.candidates = list(candidates)
        self.draft = ClarificationDraft()
        #: 已经在本轮记过 asked 的编号，防止重复记账。
        self.recorded: set[str] = set()
        #: 之前已经问过、还没回答的编号：本版不重复追问。
        self.already_asked = {
            item.candidate_id for item in self.candidates if item.state == STATE_ASKED
        }

    def propose(self, candidate_id: str, question: str) -> dict[str, Any]:
        """内部工具 ``memory__propose_clarification`` 的服务端实现。

        只登记草稿：不改记忆、不表示已发布、同轮最多一条。
        候选编号由**服务端**校验范围与有效性，不信模型自报。
        """
        wanted = str(candidate_id or "").strip()
        body = str(question or "").strip()
        if not wanted:
            return {"status": "error", "error": "缺少候选编号"}
        if not body:
            return {"status": "error", "error": "缺少问题正文"}
        # 先校验编号，再看"本轮是否已经登记"：否则一个伪造编号会被回成 ok（reason=本轮
        # 已登记一个问题），让调用方以为编号是有效的 —— 服务端不该借调用顺序改变结论。
        match = next((item for item in self.candidates if item.candidate_id == wanted), None)
        if match is None:
            return {"status": "error", "error": "候选编号不在本会话可用范围内"}
        if match.candidate_id in self.already_asked:
            # 已问未答不重复问。
            return {"status": "ok", "recorded": False, "reason": "该事项已经问过，尚未回答",
                    "candidate_id": match.candidate_id}
        if self.draft.active:
            # 同轮最多接受一个候选：不静默覆盖，明确告诉模型只登记了第一条。
            return {"status": "ok", "recorded": False,
                    "reason": "本轮已登记一个问题", "candidate_id": self.draft.candidate_id}
        self.draft = ClarificationDraft(
            candidate_id=match.candidate_id, question=body,
            job_id=match.job_id, recorded=True,
        )
        return {"status": "ok", "recorded": True, "candidate_id": match.candidate_id}

    def publish_tail(self) -> str:
        """要作为独立尾段附到答案后的问题（没有草稿就是空串）。"""
        return self.draft.question if self.draft.active else ""

    def record_published(self, *, published_text: str) -> list[ClarificationItem]:
        """回合**成功完成且实际发布**之后记账。

        只有"发布正文里确实包含这条问题"才记 ``asked``。取消、超时、草稿、
        核查删掉的问题都进不来 —— 这个判断只看发布结果，不看模型说了什么。
        """
        if not self.draft.active or self.store is None:
            return []
        question = self.draft.question
        if question not in (published_text or ""):
            LOGGER.info("澄清问题未出现在发布正文中，不记已问：%s", self.draft.candidate_id)
            return []
        if self.draft.candidate_id in self.recorded:
            return []
        item = self.store.mark_asked(
            self.draft.job_id, self.draft.candidate_id,
            question=question, turn_id=self.turn_id,
        )
        if item is not None:
            self.recorded.add(self.draft.candidate_id)
            return [item]
        return []


def build_clarification_context(
    store: ClarificationStore,
    *,
    scope_key: str,
    user_text: str,
    turn_id: str,
    candidates: Iterable[ClarificationItem] | None = None,
) -> dict[str, Any]:
    """构建下一轮后台处理包要用的 ``clarification_context``。

    服务端负责填写**候选编号、原作业和事项路径、实际问题及提问回合、原始来源、
    目标记录和版本**；不接受客户端自报的关联及原文作为可信依据。

    这里只给出"可能相关"，是否真的回答了问题由后台模型判断。
    """
    rows = list(candidates if candidates is not None else store.candidates(scope_key=scope_key))
    pending = [item for item in rows if item.state in (STATE_WAITING, STATE_ASKED)]
    if not pending:
        return {}
    return {
        "version": 1,
        "turn_id": turn_id,
        "user_text": user_text,
        "possible_links": [
            {
                "candidate_id": item.candidate_id,
                "job_id": item.job_id,
                "item_path": item.item_path,
                "state": item.state,
                # 助手问题本身不能证明用户采纳，所以角色分开标注。
                "asked_question": item.asked_question,
                "asked_turn": item.asked_turn,
                "source_quote": item.quote,
                "source_role": "user",
                "target_id": item.target_id,
                "target_version": item.target_version,
            }
            for item in pending
        ],
        "note": "以上只是可能关联，是否回答了该问题由判断模型决定，程序不自动确认。",
    }


#: 内部工具名。走现有工具注册与网关，不另建一条调用通道。
PROPOSE_TOOL_NAME = "memory__propose_clarification"


def propose_tool_name() -> str:
    return PROPOSE_TOOL_NAME


def propose_tool_schema() -> dict[str, Any]:
    """参数只有候选编号和问题正文 —— 语义结论由模型给，程序不编造。"""
    return {
        "type": "function",
        "function": {
            "name": PROPOSE_TOOL_NAME,
            "description": (
                "当用户的说法与该候选事项可能相关、但还不足以确认时，登记一个问题，"
                "由系统作为独立尾段附在本轮回答后。只登记草稿，不改记忆。"
                "每轮最多登记一个候选。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "candidate_id": {
                        "type": "string",
                        "description": "关键背景里给出的待澄清候选编号（原样复制）。",
                    },
                    "question": {
                        "type": "string",
                        "description": "要问用户的一句话；先答能确定的部分，再问这一句。",
                    },
                },
                "required": ["candidate_id", "question"],
                "additionalProperties": False,
            },
        },
    }


def build_propose_tool_spec(turn: "ClarificationTurn") -> Any:
    """按现有 ToolSpec 契约构造登记工具。

    它是**低风险、临时效果**的：只登记本轮提问草稿，不改记忆、不表示已发布。
    注册与调用仍走现有工具注册表和网关（本函数不绕过它们）。
    """
    from .contracts import EFFECT_TRANSIENT, ToolSpec

    def handler(arguments: Mapping[str, Any], _scope: Any) -> dict[str, Any]:
        result = turn.propose(
            str(arguments.get("candidate_id") or ""),
            str(arguments.get("question") or ""),
        )
        if result.get("status") == "error":
            return {"status": "failed", "error": result.get("error") or "登记失败", "output": dict(result)}
        return {"status": "ok", "output": dict(result)}

    return ToolSpec(
        name=PROPOSE_TOOL_NAME,
        schema=propose_tool_schema(),
        handler=handler,
        risk="low",
        effect=EFFECT_TRANSIENT,
    )
