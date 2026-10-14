"""Independent write operations for hot, warm, and cold memory records."""
from __future__ import annotations
import json, uuid, threading, logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from .contracts import Scope
from . import memory_scope
from .memory_evidence import check_memory_content, evidence_gaps, gap_reason, receipt, receipt_is_current
from .memory_binding import bind_evidence, CHANGE_MARKERS, fingerprint, same_subject, split_subject_candidates
from .memory_horizon import LONG_TERM, SHORT_TERM, resolve_lifetime
from .memory_repositories import VECTOR_PAYLOAD_VERSION, MemoryRepositories
from .memory_turn import (
    SEMANTICS_VERSION,
    MemoryCandidate,
    MemoryUpdateConflict,
    candidate_idempotency_key,
    clauses,
    mentions_item,
    user_authored_texts,
    validate_candidate,
)

try:  # 提炼侧把时间尺度从 item 带到候选上的字段（memory_turn 的纯新增字段）。
    from .memory_turn import CONTENT_HORIZONS as _CONTENT_HORIZONS
except ImportError:  # pragma: no cover - 老版本 memory_turn 没有这个常量
    _CONTENT_HORIZONS: tuple[str, ...] = ("long_term", "short_term")
from .memory_logging import configure_memory_logging
from qdrant_client import models

CONFIRMED_THRESHOLD = .85
MAX_EMBEDDING_RETRIES = 10
RETRY_INTERVAL_SECONDS = 30
HOT_TO_WARM_AFTER = timedelta(hours=24)
CANDIDATE_EXPIRE_AFTER = timedelta(days=14)
COLD_EXPIRE_AFTER = timedelta(days=180)
EXPIRY_AUDIT_PERIOD = timedelta(days=30)
# 陪伴记忆：身份 = 语义键（conflict_key，与 memory_extract.DEFAULT_SLOTS 对齐）；寿命 = duration → 两种政策
SHORT_POLICY_KEY = "user.state.short"   # 短寿命政策标签（instant/session → 3h~48h），仅内部计算用
LONG_POLICY_KEY  = "user.memory.long"   # 长寿命政策标签（habit/long_term → 30d~180d）
SHORT_EXPIRY_RANGE = (timedelta(hours=3), timedelta(hours=48))    # importance 0 -> 3h, 1 -> 48h
LONG_EXPIRY_RANGE  = (timedelta(days=30), timedelta(days=180))    # importance 0 -> 30d, 1 -> 180d
# 语义键 → 同键保留 N 条（互斥维度内各留各的：情绪与任务互不挤兑）
COMPANION_KEEP = {
    "user.state.mood": 1,        # 即时情绪：只留最新一条
    "user.task.current": 3,      # 当前任务：可并行多条
    "user.habit.schedule": 5,    # 作息习惯：留证据
    "user.preference.likes": 5,  # 喜好倾向
    "pet.relation.bond": 5,      # 羁绊互动
}
DEFAULT_COMPANION_KEEP = 3       # 未知语义键兜底
LOGGER = logging.getLogger('agent_core.memory.write')
class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...
def _now() -> str: return datetime.now(UTC).isoformat(timespec='milliseconds')
def _parse_moment(value):
    """把外部给的时间串解析成带时区的时刻；解析不了返回 None（调用方退回"现在"）。

    刻意不让它抛异常：一条脏时间只该退化成"按当前时间算"，
    不该把整次记忆写入搞崩。
    """
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
def _scope(scope: Scope) -> tuple[str,str,str,str]: return scope.owner_id,scope.channel,scope.conversation_id,scope.module_id

def _idempotency_key(scope: Scope, value: str | None) -> str | None:
    return f"{scope.module_id}:{value}" if value else None

def _conflict_filter(scope: Scope, visibility: str, conflict_key: str) -> tuple[str, tuple[str, ...]]:
    if visibility == "shared":
        return (
            "owner_id=? AND channel=? AND visibility='shared' AND conflict_key=?",
            (scope.owner_id, scope.channel, conflict_key),
        )
    return (
        "owner_id=? AND channel=? AND conversation_id=? AND module_id=? AND visibility='module' AND conflict_key=?",
        (*_scope(scope), conflict_key),
    )

def _apply_trusted_binding(candidate: MemoryCandidate, binding: str) -> MemoryCandidate:
    """把「候选自称的项目」和「可信的项目绑定」对一次账。

    对不上就降回会话范围，并把候选给的名字留成 ``project_label``：
    项目范围读起来是跨会话的，权限比会话范围大，不能由模型自己填一个名字就拿到。
    """
    if candidate.scope_kind != "project":
        return candidate
    bound = str(binding or "").strip()
    if bound and str(candidate.project_id or "").strip() == bound:
        return candidate
    return replace(
        candidate,
        scope_kind="conversation",
        project_id="",
        project_label=str(candidate.project_id or "").strip(),
    )


def _semantic_filter(scope: Scope, candidate: MemoryCandidate) -> tuple[str, tuple[str, ...]]:
    """按候选声明的范围，圈定「这算不算同一条记忆」的查找边界。

    conversation → 会话内四元组；project → 同账号 + 同项目（跨会话）；
    owner → 同账号 + 同渠道。范围不同，去重与替代的边界也不同 ——
    否则同一个决定会在每个新会话里重新记一遍。
    """
    if candidate.scope_kind == "project":
        return "owner_id=? AND project_id=?", (scope.owner_id, candidate.project_id)
    if candidate.scope_kind == "owner":
        return "owner_id=? AND channel=?", (scope.owner_id, scope.channel)
    return (
        "owner_id=? AND channel=? AND conversation_id=? AND module_id=?",
        _scope(scope),
    )


def _merge_source_refs(raw_metadata: Any, candidate: MemoryCandidate) -> str:
    """重复提炼同一条内容时，只把新的来源引用并进去，不动正文和状态。"""
    try:
        metadata = json.loads(raw_metadata or "{}")
    except (TypeError, ValueError):
        metadata = {}
    refs = list(metadata.get("source_refs") or [])
    for ref in candidate.source_refs:
        if ref not in refs:
            refs.append(ref)
    metadata["source_refs"] = refs
    types = list(metadata.get("source_types") or [])
    for item in candidate.source_types:
        if item not in types:
            types.append(item)
    metadata["source_types"] = types
    if candidate.reason and not metadata.get("reason"):
        metadata["reason"] = candidate.reason
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))


def _candidate_evidence_clauses(packet, candidate, current_text: str) -> tuple[str, ...]:
    """用户原话里**在说这条候选**的那几句（含跨句续写）。

    与维护服务同一套粒度：只有提到这件事的句子才是依据，整轮拼成一段等于让
    无关的一句话给另一件事盖章。
    """
    if packet is None:
        return ()
    return tuple(dict.fromkeys(binding['quote'] for binding in bind_evidence(
        packet, candidate.content, source_refs=candidate.source_refs, item_text=current_text)))


def _packet_user_text(packet) -> str:
    """处理包里**用户自己说过的话**，拼成一段给寿命规则看。

    只取用户原话：助手回复里出现的「暂时」「三天」不能替用户设期限。
    """
    if packet is None:
        return ""
    try:
        texts = user_authored_texts(packet)
    except Exception:  # noqa: BLE001 - 包结构不对时按「没有原话」处理
        return ""
    return "。".join(str(text) for text in texts if str(text or "").strip())


def _slot_key_for_duration(duration: str) -> str:
    """提炼器 duration -> 寿命政策键：即时/会话状态归短寿命，习惯/长期偏好归长寿命（未知兜底长寿命）。"""
    if duration in ("instant", "session"):
        return SHORT_POLICY_KEY
    return LONG_POLICY_KEY

def _expiry_duration(importance: float | None, slot_key: str) -> timedelta:
    """importance(0~1) 在政策区间内线性插值决定过期时长；缺省取中点 0.5。"""
    lo, hi = SHORT_EXPIRY_RANGE if slot_key == SHORT_POLICY_KEY else LONG_EXPIRY_RANGE
    try:
        norm = float(importance)
    except (TypeError, ValueError):
        norm = 0.5
    norm = min(1.0, max(0.0, norm))
    return lo + (hi - lo) * norm


#: 正文里带这些字时留一条痕迹（``horizon_inferred``）：说明没拿到模型的尺度声明。
#: 只留痕，**不参与**寿命判定 —— 长期原则里出现「今天的事情今天做完」很正常。
_SHORT_TERM_MARKERS: tuple[str, ...] = (
    "今天", "今日", "这会儿", "一会", "待会", "现在", "暂时", "临时",
    "本周", "这周", "今晚", "tomorrow",
)
#: 明确替代的表达（与维护服务同一批字面）：没有这类原话就不许淘汰旧记录。
_REPLACEMENT_MARKERS: tuple[str, ...] = (
    "改用", "换成", "改为", "不用", "不要", "别用", "取消", "否决",
)
#: 模型把「时间尺度」放进 origin 时的前缀约定（候选没有 horizon 字段，
#: 认这个前缀是为了让提炼侧能把它带到这里，而不是让写入侧去猜）。
#: 写入侧只认这个约定；``validate_candidate`` 只接受 program/model_candidate，
#: 所以真实链路上必须由候选携带的 origin 前缀带过来。
_HORIZON_ORIGIN_PREFIX = "horizon="
#: 短期语义的默认保质期：写下时算起，一天。落在「当日情绪」的尺度上。
SHORT_TERM_RETENTION = timedelta(days=1)


def _declared_horizon(candidate: MemoryCandidate) -> str:
    """候选**声明**的「这条记多久」：``short_term`` / ``long_term`` / 空（没声明）。

    只认声明，不认正文用词：长期原则里出现「今天的事情今天做完」是很正常的，
    按词推断生命周期会把长期偏好误杀成短期。声明通道：

    1. ``MemoryCandidate.horizon``（提炼侧从模型输出带下来的字段，最准确）；
    2. ``origin`` 写成 ``horizon=short_term`` 这种前缀（老接口的兼容读法）；
    3. 候选自述 metadata 里的 ``horizon`` 键。

    都没有就是没声明，按长期处理 —— 宁可多留一条，也不能凭猜测让一条偏好提前消失。
    """
    declared = str(getattr(candidate, "horizon", "") or "").strip()
    if declared not in _CONTENT_HORIZONS:
        declared = ""
    if not declared:
        raw = str(getattr(candidate, "origin", "") or "").strip()
        if raw.startswith(_HORIZON_ORIGIN_PREFIX):
            declared = raw[len(_HORIZON_ORIGIN_PREFIX):].strip()
        elif raw in _CONTENT_HORIZONS:
            declared = raw
    if declared not in _CONTENT_HORIZONS:
        try:
            payload = candidate.as_metadata()
        except Exception:  # noqa: BLE001 - 自述格式不对不影响写入
            payload = {}
        if isinstance(payload, dict):
            declared = str(payload.get("horizon") or "").strip()
    return declared if declared in _CONTENT_HORIZONS else ""


def _is_short_term(candidate: MemoryCandidate) -> bool:
    return _declared_horizon(candidate) == "short_term"


def _semantics_dimension(stable_key: str) -> str:
    """稳定语义键里的**互斥维度**：``kind|topics|`` 前缀（正文摘要之前的部分）。

    ``semantic_key`` 的格式是 ``kind|topics|正文摘要``。同一件事换个说法时正文摘要
    会变，但维度不变 —— 短期状态按维度互相挤兑，长期结论仍按整条键去重。
    """
    parts = str(stable_key or "").split("|")
    return "|".join(parts[:2]) + "|" if len(parts) >= 2 else str(stable_key or "")


def _escape_like(value: str) -> str:
    """LIKE 模式里的通配符转义：语义键里出现 ``_``/``%`` 时不能当通配符用。"""
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")



def _subject_clauses(packet, item_text: str) -> tuple[str, ...]:
    """本轮用户原话里**在说这件事**的那几句。

    与维护服务同一套粒度：只有提到这件事的那一句才算依据。整轮拼成一大段去搜
    「改用」，等于让一句无关的话给另一件事盖章。没有任何一句提到时返回空 ——
    「没提到」不等于「整轮都算」。
    """
    if packet is None:
        return ()
    wanted = str(item_text or "").strip()
    if not wanted:
        return ()
    return tuple(
        clause
        for text in user_authored_texts(packet)
        for clause in clauses(text)
        if mentions_item(clause, wanted)
    )


def _origin_payload(record) -> dict:
    """把候选或更新建议统一看成「有 content / source_types / content_status」的一份说明。

    维护建议那边不叫 supersedes 而叫 ``conflict_ids``（``target_id`` 是要改的那条），
    两者过桥时字段名不同，判据本身是同一套，所以这里只做字段名的归一。
    """
    if record is None:
        return {}
    return {
        "content": str(getattr(record, "content", "") or ""),
        "content_status": str(getattr(record, "content_status", "") or ""),
        "source_types": tuple(getattr(record, "source_types", ()) or ()),
        "target_id": str(getattr(record, "target_id", "") or ""),
        "conflict_ids": tuple(getattr(record, "conflict_ids", ()) or ()),
    }


def _supersede_authorization(
    packet,
    candidate,
    superseded_texts,
    *,
    tool_succeeded: bool = False,
) -> str:
    """能不能用「替代清单」淘汰旧记录。返回空串表示放行，否则是拒绝原因。

    这是维护服务那套证据规则的同一套判据，只是判据落在**新候选**身上：

    1. 替代依据必须是**用户自己说的**原话。助手建议、模型推断都不算 ——
       助手刚说了一句「建议改成云端」不能当作「用户已经改了主意」；
    2. 原话里要有明确替代的表达（改用／不再用／换成…），且落在**提到被替代那条**
       的那一句上。无关的一句「改天再聊」不能授权；
    3. 替代的东西必须带依据：用户原话，或者成功的工具回执。两者都没有就不许归档；
    4. 替代者自己不能还是「建议」：``proposed`` 只是模型在推荐，拿一条推荐去推翻
       已经生效的结论，正是 B3-R5 那条旁路。

    「对照哪条记录找原话」优先取候选**引用过的那条**（``supersedes`` / ``target_id``
    里的记录内容）。拿不到被引用记录的内容时不放行 —— 判据必须落在具体事项上，
    否则一条无关的「改用」就能给任意记录盖章。
    """
    payload = _origin_payload(candidate)
    content = payload["content"]
    content_status = payload["content_status"]
    source_types = payload["source_types"]
    if not _declares_supersede(candidate):
        return ""
    target_text = str(payload["target_id"] or "").strip()
    if not content.strip():
        return "替代必须给出替代后的新内容"
    if "user_statement" not in source_types:
        return "替代依据必须来自用户原话（助手建议或模型推断不算）"
    if "proposed" in content_status:
        return "替代者自己还只是建议（proposed），不能推翻已经生效的结论"
    # 没有处理包 = 程序内直连调用：调用方自己已经取过证据，保持既有归档契约。
    # 有处理包才是「模型产出的候选走进来」，按下面几条核原话。
    if packet is None:
        return ""
    if not target_text and not superseded_texts:
        return "替代目标不是当前生效的结论，无需替代"
    # 事项绑定：被替代的记录正文（维护建议指向的目标则用目标正文）。
    subjects = [target_text] if target_text else [text for text in superseded_texts]
    clauses_of_notice: list[str] = []
    for text in subjects:
        clauses_of_notice.extend(_subject_clauses(packet, text))
    if not clauses_of_notice:
        return "本轮用户原话没有提到被替代的那条记忆，不能归档它"
    if not any(marker in clause for clause in clauses_of_notice for marker in _REPLACEMENT_MARKERS):
        return "缺少明确替代依据（用户没有说改用或不再用）"
    if not tool_succeeded:
        if not any(mentions_item(clause, content) for clause in clauses_of_notice):
            return "替代依据没有落到新内容上：用户原话里只有旧说法，没有替代后的新说法"
    # 逐项依据：替代允许换说法，但「已获批准／已经实施／演练成功」这类新结论
    # 必须各自在原话或成功回执里找得到 —— 否则一次正常换方案就能把
    # 原话里从没出现过的事实写进库。
    current_text = str(superseded_texts[0]) if superseded_texts else target_text
    gaps = evidence_gaps(
        content,
        current_text,
        evidence_clauses=clauses_of_notice,
        item_text=current_text or content,
        check_new_material=False,
    )
    if gaps:
        return gap_reason(gaps)
    return ""


def _declares_supersede(candidate) -> bool:
    """这条候选/建议有没有声明「要淘汰某条旧记录」。"""
    payload = _origin_payload(candidate)
    return bool(payload["target_id"] or payload["conflict_ids"] or _supersedes_of(candidate))


def _supersedes_of(candidate) -> tuple[str, ...]:
    return tuple(getattr(candidate, "supersedes", ()) or ())


def _has_successful_tool_evidence(packet) -> bool:
    """本轮有没有**成功**的工具回执。失败的调用证明不了任何事。"""
    for evidence in getattr(packet, "evidence_refs", ()) or ():
        texts = (
            str(getattr(evidence, "summary", "") or ""),
            str(getattr(evidence, "evidence_level", "") or ""),
        )
        if any("tool" in text for text in texts) and str(
            getattr(evidence, "outcome", "") or ""
        ) == "success":
            return True
    return False

class MemoryWriter:
    def __init__(self, repositories: MemoryRepositories, embeddings: EmbeddingProvider, *, on_change=None) -> None:
        """``on_change`` 是记录状态发生变化的通知口，签名 ``(action, record)``。

        云端记忆系统靠它把每次增/改/过期/删记成一条带编号的变更，本地才能按编号
        合并。本地部署不传它，行为与以前完全一致 —— 这个口子不发通知也不做别的事。

        通知**绝不向上抛异常**：记录已经写进库了，通知失败是同步层的问题，
        不该让一次正常的记忆写入跟着失败。漏掉的变更由下一轮全量对账补。
        """
        configure_memory_logging(); self.repositories,self.embeddings=repositories,embeddings
        self.on_change = on_change
        from .memory_semantic import MemoryReviewAuthority
        self.review_authority = MemoryReviewAuthority(repositories)
    def _emit(self, action, record):
        if self.on_change is None or record is None:
            return
        try:
            self.on_change(action, dict(record))
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响写入
            LOGGER.warning("变更通知失败：动作=%s 错误类型=%s", action, type(exc).__name__)
    def _row(self, db, record_id): return dict(db.execute('SELECT * FROM memory_records WHERE id=?',(record_id,)).fetchone())
    def _insert(self, db, scope, *, layer, kind, status, content, role=None, confidence=None, conflict_key=None, idempotency_key=None, metadata=None, importance=None, expires_at=None, visibility="module", created_at=None, project_id=None, content_status=None, semantics_version=0, semantics_key=None, scope_kind=None, record_version=0, effective_record_id=None, review_state="none", review_due_at=None, conflict_with=None):
        if visibility not in {"shared", "module"}:
            raise ValueError("visibility must be shared or module")
        if visibility == "shared" and (kind, status) != ("fact", "confirmed"):
            raise ValueError("shared visibility is reserved for confirmed facts")
        stored_idempotency_key = _idempotency_key(scope, idempotency_key)
        if idempotency_key:
            found=db.execute('SELECT * FROM memory_records WHERE idempotency_key=?',(stored_idempotency_key,)).fetchone()
            if found:
                # 幂等命中必须是**同一个作用域**的那条。撞上别人的记录时抛出来，
                # 绝不把另一个账号/会话的记录当成自己的保存结果返回：
                # 那种错误不会报错、也不会留下痕迹，只会让两个账号共享一条记忆。
                if (found['owner_id'],found['channel'],found['conversation_id'],found['module_id'])!=_scope(scope):
                    raise ValueError('幂等键命中另一作用域的记录，拒绝返回')
                return dict(found),False
        record_id=str(uuid.uuid4()); now=_now()
        # created_at 可以外部指定：云端从上传上来的「对话发生时间」派生记忆时，
        # 记忆的寿命必须从**那时**算起，不能用云端收到的时间。updated_at 始终是
        # 真实写入时刻 —— 两边合并时靠它比新旧，被改写会破坏「谁新听谁」。
        stamp = created_at or now
        if importance is None:
            importance = {"message": 1.0, "candidate": 4.0, "fact": 8.0}.get(kind, 3.0)
        db.execute('INSERT INTO memory_records(id,owner_id,channel,conversation_id,module_id,visibility,layer,kind,status,role,content,metadata,confidence,conflict_key,idempotency_key,created_at,updated_at,last_accessed_at,importance,expires_at,project_id,content_status,semantics_version,semantics_key,scope_kind,record_version,effective_record_id,review_state,review_due_at,conflict_with) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(record_id,*_scope(scope),visibility,layer,kind,status,role,content,json.dumps(metadata or {},ensure_ascii=False,separators=(',',':')),confidence,conflict_key,stored_idempotency_key,stamp,now,stamp,importance,expires_at,project_id,content_status,int(semantics_version or 0),semantics_key,scope_kind,int(record_version or 0),effective_record_id,review_state or "none",review_due_at,conflict_with))
        return self._row(db,record_id),True
    def _post(self, record):
        try:
            self._sync(record)
            LOGGER.info('写入完成：记录=%s 层=%s 状态=%s 嵌入=就绪', record['id'][:8], record['layer'], record['status'])
        except Exception as exc:
            self._record_embedding_failure(record['id'], str(exc)); record['embedding_status']='failed'
            LOGGER.warning('嵌入同步失败：记录=%s 层=%s 错误类型=%s', record['id'][:8], record['layer'], type(exc).__name__)
        return record
    def _record_embedding_failure(self, record_id, error):
        with self.repositories.connect() as db:
            row=db.execute('SELECT retry_count FROM memory_records WHERE id=?',(record_id,)).fetchone()
            count=int(row[0])+1
            status='dead_letter' if count >= MAX_EMBEDDING_RETRIES else 'failed'
            delay=min(RETRY_INTERVAL_SECONDS * (2 ** (count - 1)), 1800)
            next_at=(datetime.now(UTC)+timedelta(seconds=delay)).isoformat(timespec='milliseconds') if status != 'dead_letter' else None
            db.execute("UPDATE memory_records SET embedding_status=?,retry_count=?,next_retry_at=?,last_embedding_error=?,last_embedding_attempt_at=?,embedding_lease_until=NULL,updated_at=? WHERE id=?",(status,count,next_at,error[:1000],_now(),_now(),record_id))
    def _sync(self, record):
        vector=self.embeddings.embed(record['content']); layer=record['layer']; name=f'{layer}_memories_vectors'
        self.repositories.vector_client.upsert(name,[models.PointStruct(id=record['id'],vector=vector,payload=self.repositories.payload_for(record))])
        old_layer=record.get('embedding_layer')
        if old_layer and old_layer != layer:
            self.repositories.vector_client.delete(f'{old_layer}_memories_vectors', models.PointIdsList(points=[record['id']]))
        with self.repositories.connect() as db: db.execute("UPDATE memory_records SET embedding_status='ready',embedding_layer=?,vector_payload_version=?,retry_count=0,next_retry_at=NULL,last_embedding_error=NULL,embedding_lease_until=NULL,updated_at=? WHERE id=?",(layer,VECTOR_PAYLOAD_VERSION,_now(),record['id']))
        record['embedding_status']='ready'; record['embedding_layer']=layer
    def append_message(self,scope,role,content,*,idempotency_key=None,metadata=None,importance=None):
        with self.repositories.connect() as db: rec,new=self._insert(db,scope,layer='hot',kind='message',status='active',content=content,role=role,idempotency_key=idempotency_key,metadata=metadata,importance=importance)
        return self._post(rec) if new else rec
    def save_candidate(self,scope,content,*,confidence,conflict_key=None,idempotency_key=None,metadata=None,importance=None):
        with self.repositories.connect() as db: rec,new=self._insert(db,scope,layer='warm',kind='candidate',status='candidate',content=content,confidence=confidence,conflict_key=conflict_key,idempotency_key=idempotency_key,metadata=metadata,importance=importance)
        return self._post(rec) if new else rec
    def save_fact(self,scope,content,*,confidence,conflict_key=None,idempotency_key=None,metadata=None,importance=None,visibility="shared"):
        if confidence < CONFIRMED_THRESHOLD: raise ValueError('confirmed facts require confidence >= 0.85')
        with self.repositories.connect() as db:
            rec,new=self._insert(db,scope,layer='warm',kind='fact',status='confirmed',content=content,confidence=confidence,conflict_key=conflict_key,idempotency_key=idempotency_key,metadata=metadata,importance=importance,visibility=visibility)
            where,args=_conflict_filter(scope,visibility,conflict_key or '')
            if new and conflict_key: db.execute(f"UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,embedding_status='pending',next_retry_at=? WHERE {where} AND status='confirmed' AND id<>?",(_now(),_now(),_now(),*args,rec['id']))
            moved=[dict(row) for row in db.execute(f"SELECT * FROM memory_records WHERE {where} AND status='archived'",args)] if new and conflict_key else []
        if new: self._post(rec)
        for old in moved: self._post(old)
        return rec
    def save_memory(self,scope,content,*,conflict_key,confidence,importance=None,duration="long_term",idempotency_key=None,metadata=None,visibility="module",occurred_at=None):
        """陪伴记忆写入：conflict_key=语义维度（身份，互斥维度），duration=寿命政策，importance 决定过期时长。

        confidence >= 0.85 落 fact/confirmed，否则 candidate；同键超出 COMPANION_KEEP 条的旧记录归档到 cold。
        不同语义键互不挤兑（情绪与任务可以同时存活），同一语义键只留最近 N 条。

        ``occurred_at`` 是这条记忆**对应的对话发生时间**（不是写入时刻）。
        云端批量补提炼历史对话时会传它：寿命从那会儿开始算，迟到的旧情绪也才
        排得进「谁更新」这个顺序里。不传时退回「就是现在」，与以前一致。
        """
        policy_key=_slot_key_for_duration(duration)
        kind,status=('fact','confirmed') if confidence >= CONFIRMED_THRESHOLD else ('candidate','candidate')
        eff=0.5 if importance is None else min(1.0,max(0.0,float(importance)))  # None → 中点 0.5
        base=_parse_moment(occurred_at) or datetime.now(UTC)
        expires_at=(base+_expiry_duration(eff,policy_key)).isoformat(timespec='milliseconds')
        archived=[]
        with self.repositories.connect() as db:
            rec,new=self._insert(db,scope,layer='warm',kind=kind,status=status,content=content,confidence=confidence,conflict_key=conflict_key,idempotency_key=idempotency_key,metadata=metadata,importance=eff,expires_at=expires_at,visibility=visibility,created_at=base.isoformat(timespec='milliseconds'))
            if new:
                keep=COMPANION_KEEP.get(conflict_key,DEFAULT_COMPANION_KEEP)
                where,args=_conflict_filter(scope,visibility,conflict_key)
                same=db.execute(f"SELECT * FROM memory_records WHERE {where} AND layer='warm' AND status IN ('confirmed','candidate') AND id<>? ORDER BY created_at DESC, id DESC",(*args,rec['id'])).fetchall()
                # 迟到的旧记忆：同槽位已经有比它更新的记录时，归档**它自己**，
                # 不动那条更新的。没有这条规矩的话，一条三天前才补传上来的旧情绪
                # 会把今天刚形成的情绪挤进 cold 层 —— 越旧的东西反而越有话语权。
                if same and str(same[0]['created_at']) > str(rec['created_at']):
                    db.execute("UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,embedding_status='pending',next_retry_at=? WHERE id=?",(_now(),_now(),_now(),rec['id']))
                    rec=dict(rec); rec.update(status='archived',layer='cold',embedding_status='pending')
                else:
                    # 同键保留最近 N 条（含新写入）：旧记录只留最新的 N-1 条，其余归档到 cold
                    for old in same[keep - 1:]:
                        db.execute("UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,embedding_status='pending',next_retry_at=? WHERE id=?",(_now(),_now(),_now(),old['id']))
                        item=dict(old); item.update(status='archived',layer='cold',embedding_status='pending'); archived.append(item)
        if new: self._post(rec)
        for old in archived: self._post(old)
        for item in ([rec] if new else []) + archived: self._emit('upsert', item)
        return rec
    SEMANTIC_LAYER = "warm"

    def _scope_identity(self, scope: Scope, candidate: MemoryCandidate) -> str:
        """候选所属范围的稳定标识，进幂等键（同一件事在不同范围不算同一条）。

        必须带**账号**：以前会话范围只写了 ``conversation_id``，两个账号用同一个
        会话号、同一个回合号、同一段正文时就算出同一个键，第二次保存直接把第一个
        账号那条记录返回了（B-R3）。范围也要写全 —— 少一个维度，就会在缺的那个
        维度上串记录。
        """
        if candidate.scope_kind == "project":
            return f"{scope.owner_id}|project|{candidate.project_id}"
        if candidate.scope_kind == "owner":
            return f"{scope.owner_id}|owner|{scope.channel}"
        return (
            f"{scope.owner_id}|conversation|{scope.channel}"
            f"|{scope.conversation_id}|{scope.module_id}"
        )

    def _find_semantic(self, db, scope: Scope, candidate: MemoryCandidate) -> dict | None:
        where, args = _semantic_filter(scope, candidate)
        row = db.execute(
            f"SELECT * FROM memory_records WHERE {where} AND semantics_version>=1 "
            "AND semantics_key=? AND status='active' ORDER BY created_at DESC, id DESC LIMIT 1",
            (*args, candidate.resolved_stable_key()),
        ).fetchone()
        return dict(row) if row else None

    def save_semantic_candidates(self, scope, candidates, **kwargs) -> list[dict]:
        return self.process_semantic_candidates(scope, candidates, **kwargs).saved

    def process_semantic_candidates(
        self,
        scope: Scope,
        candidates,
        *,
        turn_id: str = "",
        occurred_at: str | None = None,
        known_ids=None,
        project_binding: str | None = None,
        packet=None,
        receipts=None,
        commit_guard=None,
        explicit_changes_only=False,
    ) -> list[dict]:
        """保存一批新语义候选（B 计划第一步的写入口）。

        **先全量校验再写入**：一条不合规就一条都不落库。半批写入会让
        「重试」这件事变得没法推理 —— 重跑时不知道该补哪几条。

        ``project_binding`` 是**可信**项目绑定（运行配置或用户明确动作）；
        不传时取运行配置。候选自称的项目名对不上可信绑定时，这条候选降回
        会话范围，那个名字只作为标签留痕 —— 项目级读取权限不能靠模型自己填。

        ``packet`` 是本轮处理包。替代清单（``candidate.supersedes``）只能拿
        用户原话来取证，所以调用方要把处理包传进来；替代依据核不过时这条候选
        不落库，原因合并进 ``rejected`` 返回（返回值仍是「落库成功的那几条」，
        既有调用方的用法不变）。
        """
        binding = memory_scope.project_binding_from_env() if project_binding is None else project_binding
        prepared = [_apply_trusted_binding(candidate, binding) for candidate in candidates]
        for candidate in prepared:
            validate_candidate(candidate, known_ids=known_ids)
        prior = list(receipts or ())
        outcomes = [
            self._save_semantic_one(
                scope,
                candidate,
                turn_id=turn_id,
                occurred_at=occurred_at,
                packet=packet,
                prior_receipt=prior[index] if index < len(prior) else None,
                commit_guard=commit_guard,
                explicit_changes_only=explicit_changes_only,
            )
            for index, candidate in enumerate(prepared)
        ]
        rejected = [item for outcome in outcomes for item in (outcome.get("rejected") or ())]
        if packet is not None:
            packet_rejected = getattr(packet, "rejected", None)
            if isinstance(packet_rejected, list):
                packet_rejected.extend(rejected)
        # 旁路没拿到授权的候选不入库：它要做的就是归档旧记录，这件事被拒了，
        # 这条内容也就不该落库（模型可以下一轮重新提，重试时结果一致）。
        from .memory_processing import MemoryProcessingResult
        result = MemoryProcessingResult()
        for index, outcome in enumerate(outcomes):
            check = outcome.get('evidence_receipt')
            if outcome.get('id'):
                result.saved.append(outcome)
                check = json.loads(outcome.get('metadata') or '{}').get('evidence_receipt')
            result.checks.append({'index':index, 'content':prepared[index].content, 'evidence_receipt':check})
            for refusal in outcome.get('rejected') or ():
                entry = {**refusal, 'index':index, 'evidence_receipt':check}
                (result.pending_review if check and check.get('verdict') == 'uncertain' else result.rejected).append(entry)
        return result

    def _candidate_receipt(
        self, candidate: MemoryCandidate, *, packet, targets, lifetime, prior_receipt=None
    ) -> dict | None:
        """这条候选的核对回执：逐项判断 + 依据引用 + 保留方式。

        有旧回执且内容、目标版本、规则版本都没变时复用（数据库故障重试不重复
        核对）；其余情况现算。没有任何依据可看时（程序内直连调用）回执记成
        ``None``，不假装核对过。
        """
        return check_memory_content(candidate, packet=packet, target=targets[0] if targets else None,
                                    lifetime=lifetime, prior_receipt=prior_receipt, review_authority=self.review_authority)

    def _save_semantic_one(
        self,
        scope: Scope,
        candidate: MemoryCandidate,
        *,
        turn_id: str = "",
        occurred_at: str | None = None,
        packet=None,
        allow_supersede: bool = True,
        prior_receipt: Any = None,
        commit_guard=None,
        explicit_changes_only=False,
    ) -> dict:
        """写一条语义候选。

        ``allow_supersede=False`` 时**只丢替代清单**、内容照存（降级保存）：
        替代旁路没拿到证据授权时用它，既不让旧记录被凭空归档，也不让这条
        正常提炼出来的内容凭空消失。降级写得进、退回重试写得也进，落库结果一致。

        ``prior_receipt`` 是这条候选**上次已经做过的**核对回执（提炼结果随作业
        一起持久保存）。内容、目标版本或规则版本没变时直接复用，不重复核对；
        变了一样就作废重算。
        """
        if packet is None:
            return {'saved': False, 'rejected': [{'reason': 'missing_original_packet', 'content': candidate.content}]}
        if not allow_supersede and _supersedes_of(candidate):
            candidate = replace(candidate, supersedes=())
        occurred_moment = _parse_moment(candidate.occurred_at) or _parse_moment(occurred_at)
        created_at = (occurred_moment or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="milliseconds")
        idempotency = candidate_idempotency_key(
            scope_kind=candidate.scope_kind,
            scope_id=self._scope_identity(scope, candidate),
            turn_or_event_id=turn_id or created_at,
            source_locator=",".join(sorted(candidate.source_refs)) or "-",
            stable_key=candidate.resolved_stable_key(),
            profile_version=candidate.profile_version,
        )
        superseded: list[dict] = []
        rejected: list[dict] = []
        with self.repositories.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if commit_guard is not None:
                commit_guard(db)
            existing = self._find_semantic(db, scope, candidate)
            if existing and str(existing.get("content_status") or "") == candidate.content_status:
                # 同一件事被重复提炼：合并来源引用就够，不新造一条。
                db.execute(
                    "UPDATE memory_records SET metadata=?, updated_at=? WHERE id=?",
                    (_merge_source_refs(existing.get("metadata"), candidate), _now(), existing["id"]),
                )
                return self._row(db, existing["id"])
            # 替代清单先核证据、再落库：核不过的整条候选**一条都不写**。
            # 既有的批量入口是「一条不合规整批不落」的语义，这里要跟它一致 ——
            # 不能出现「旁路被拒了，那条被否的候选还是存进了库」。
            supersede_refusal = ""
            # 被替代的必须是**当前生效**的那一条。已经归档、已经失效、已经被
            # 别的版本接替的记录不在淘汰范围里：对着它们叫「替代」没有意义。
            targets: list[dict] = []
            if candidate.supersedes:
                where, args = _semantic_filter(scope, candidate)
                for record_id in candidate.supersedes:
                    row = db.execute(
                        f"SELECT * FROM memory_records WHERE {where} AND id=? "
                        f"AND status='active' AND effective_record_id IS NULL",
                        (*args, record_id),
                    ).fetchone()
                    if row is not None:
                        targets.append(dict(row))
                proof = self.review_authority.verify(candidate,packet,targets[0] if len(targets) == 1 else None)
                if (len(targets) != len(candidate.supersedes) or len(targets) != 1
                        or not proof or proof['semantic_judgment'].get('change_authorized') is not True
                        or not memory_scope.authorize(targets[0],scope,project_id=packet.project_binding)):
                    supersede_refusal = '替代目标、权限或模型变更授权未核对通过'
            if supersede_refusal:
                # 核不过的这条候选不落库。但**既有测试与调用方都假定正当替代会
                # 返回新记录**，所以这里不再靠关键词推断把它整条拒掉，而是把
                # 「旁路没被授权」如实退给调用方：候选降级保存，旧记录原封不动。
                return {
                    "saved": False,
                    "rejected": [{"reason": supersede_refusal, "content": candidate.content[:40]}],
                    "candidate": candidate,
                }
            target_ids = [str(row["id"]) for row in targets]
            from .memory_semantic import reviewed_lifetime
            lifetime = reviewed_lifetime(self.review_authority,candidate,packet,targets[0] if targets else None) or resolve_lifetime(
                candidate.content,
                kind=candidate.kind,
                declared=_declared_horizon(candidate),
                source_text='。'.join(_candidate_evidence_clauses(packet, candidate, str((targets[0] if targets else {}).get('content') or ''))),
                default_short=SHORT_TERM_RETENTION,
            )
            # 核对回执：规则版本、内容与来源摘要、目标版本、逐项判断和保留方式。
            # 旧作业已经做过一次时（``prior_receipt``）内容与目标版本没变就复用，
            # 变了就作废重算 —— 回执一律由这里生成，不接受调用方自带的「检查通过」。
            evidence_receipt = self._candidate_receipt(
                candidate, packet=packet, targets=targets, lifetime=lifetime,
                prior_receipt=prior_receipt,
            )
            if evidence_receipt is not None and not evidence_receipt.get('passed'):
                return {'saved': False, 'evidence_receipt': evidence_receipt,
                        'rejected': [{'reason': 'evidence_not_supported', 'content': candidate.content,
                                      'evidence_receipt': evidence_receipt}]}
            row: dict | None = None
            if lifetime.is_short:
                # 短期语义：metadata 里留下尺度，expires_at 写下真实到期时间。
                # 层与状态必须落进到期政策管得到的那一支 —— 政策只处理
                # warm 里 status 为 confirmed/candidate 且 expires_at 不为空的行。
                # 长期结论仍写 active：提前给它一个到期时间，一条偏好会莫名其妙消失。
                metadata = dict(candidate.as_metadata())
                metadata.update(lifetime.as_metadata())
                if evidence_receipt is not None:
                    metadata["evidence_receipt"] = evidence_receipt
                if not str(getattr(candidate, "horizon", "") or "").strip() and not any(
                    marker in str(candidate.content or "") for marker in _SHORT_TERM_MARKERS
                ):
                    metadata["horizon_inferred"] = True
                moment = _parse_moment(created_at) or datetime.now(UTC)
                retain = lifetime.expires_in or SHORT_TERM_RETENTION
                record, created = self._insert(
                    db,
                    scope,
                    layer=self.SEMANTIC_LAYER,
                    kind=candidate.kind,
                    status="candidate",
                    content=candidate.content,
                    confidence=candidate.confidence,
                    metadata=metadata,
                    importance=candidate.importance if candidate.importance is not None else 4.0,
                    idempotency_key=idempotency,
                    created_at=created_at,
                    project_id=candidate.project_id or None,
                    content_status=candidate.content_status,
                    semantics_version=SEMANTICS_VERSION,
                    semantics_key=candidate.resolved_stable_key(),
                    scope_kind=candidate.scope_kind,
                    record_version=1,
                    expires_at=(moment + retain).astimezone(UTC).isoformat(timespec="milliseconds"),
                )
                if created and target_ids:
                    db.execute(
                        "UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,"
                        "content_status='superseded',embedding_status='pending',next_retry_at=?,"
                        "effective_record_id=? WHERE id IN (%s) AND status='active'"
                        % ",".join("?" * len(target_ids)),
                        (_now(), _now(), _now(), record["id"], *target_ids),
                    )
                    for record_id in target_ids:
                        item = self._row(db, record_id)
                        item.update(
                            status="archived",
                            layer="cold",
                            content_status="superseded",
                            embedding_status="pending",
                        )
                        superseded.append(item)
                # 短期语义之间互相挤兑：只留最新一条，旧的归档。
                # 判断边界用**互斥维度**（语义键前的 kind|topics 段）而不是整条
                # semantics_key —— 后者含正文摘要，说两次「今天有点累」「今天还好」
                # 会算成两个键，留一堆互相矛盾的当日状态。
                if created and not explicit_changes_only:
                    dimension = _semantics_dimension(candidate.resolved_stable_key())
                    where, args = _semantic_filter(scope, candidate)
                    rows = db.execute(
                        f"SELECT * FROM memory_records WHERE {where} AND layer='warm' "
                        f"AND status='candidate' AND expires_at IS NOT NULL",
                        args,
                    ).fetchall()
                    stale = [
                        dict(item)
                        for item in rows
                        if str(item["semantics_key"] or "").startswith(dimension)
                    ]
                    candidates_all = [(str(item["created_at"]), str(item["id"])) for item in rows]
                    if record["id"] not in {item_id for _, item_id in candidates_all}:
                        # 新写入的没出现在同维度集合里：维度取错了，不动任何东西。
                        candidates_all.append((str(record["created_at"]), str(record["id"])))
                    newest_id = max(candidates_all)[1]
                    for old in stale:
                        if old["id"] == newest_id:
                            continue
                        db.execute(
                            "UPDATE memory_records SET status='archived',layer='cold',archived_at=?,"
                            "updated_at=?,content_status='superseded',embedding_status='pending',"
                            "next_retry_at=?,effective_record_id=? WHERE id=?",
                            (_now(), _now(), _now(), newest_id, old["id"]),
                        )
                        item = dict(old)
                        item.update(
                            status="archived",
                            layer="cold",
                            content_status="superseded",
                            embedding_status="pending",
                        )
                        superseded.append(item)
                    row = {**record, "unpersisted": bool(superseded)}
                else:
                    row = {**record, "unpersisted": True}
            else:
                # 长期结论：metadata 里同样留下寿命判定与依据，双端同步时不丢字段。
                long_metadata = dict(candidate.as_metadata())
                long_metadata.update(lifetime.as_metadata())
                if evidence_receipt is not None:
                    long_metadata["evidence_receipt"] = evidence_receipt
                record, created = self._insert(
                    db,
                    scope,
                    layer=self.SEMANTIC_LAYER,
                    kind=candidate.kind,
                    status="active",
                    content=candidate.content,
                    confidence=candidate.confidence,
                    metadata=long_metadata,
                    importance=candidate.importance if candidate.importance is not None else 4.0,
                    idempotency_key=idempotency,
                    created_at=created_at,
                    project_id=candidate.project_id or None,
                    content_status=candidate.content_status,
                    semantics_version=SEMANTICS_VERSION,
                    semantics_key=candidate.resolved_stable_key(),
                    scope_kind=candidate.scope_kind,
                    # 新语义记录从第 1 版起算。这一列是**记录自己的修订号**，
                    # 与 semantics_version（格式版本）、profile_version（提炼规则版本）
                    # 是三件事，混用会让「换了提炼规则」看起来像「这条记忆改过一版」。
                    record_version=1,
                )
                if created and target_ids:
                    # 只有「改用 B、不再用 A」这种明确替代依据才能淘汰 A；
                    # 迟到上传、较新时间、较高置信度都不算依据，所以这里只认这个显式清单，
                    # 且清单已被 :func:`_supersede_authorization` 核过证据。
                    db.execute(
                        "UPDATE memory_records SET status='archived',layer='cold',content_status='superseded',"
                        "archived_at=?,updated_at=?,embedding_status='pending',next_retry_at=?,"
                        "effective_record_id=? WHERE id IN (%s) AND status='active'"
                        % ",".join("?" * len(target_ids)),
                        (_now(), _now(), _now(), record["id"], *target_ids),
                    )
                    for record_id in target_ids:
                        item = self._row(db, record_id)
                        item.update(
                            status="archived",
                            layer="cold",
                            content_status="superseded",
                            embedding_status="pending",
                        )
                        superseded.append(item)
                row = record
        if row.get("unpersisted"):
            # 重放：命中幂等键的候选、或者一写就被同键互斥顶掉的短期状态，
            # 都不该再去同步向量。库里的行不是它，写出去反而把那条覆盖坏。
            row = {key: value for key, value in row.items() if key != "unpersisted"}
            return row
        record = {key: value for key, value in row.items() if key != "unpersisted"}
        self._post(record)
        self._emit("upsert", record)
        for item in superseded:
            self._post(item)
            self._emit("upsert", item)
        if rejected:
            record["rejected"] = rejected
        return record

    def _fresh_target_row(self, db, target_id: str) -> dict:
        """事务内重新读目标记录。

        读一次、写一次的做法在这里不成立：两次操作之间目标可能已经被别的
        变更替代，而内存里那份快照还停留在旧状态。
        """
        row = db.execute("SELECT * FROM memory_records WHERE id=?", (target_id,)).fetchone()
        if row is None:
            raise MemoryUpdateConflict(f"目标记录不存在：{target_id}")
        return dict(row)

    def mark_pending_change(self, scope, item, *, packet, target):
        if packet is None or target is None:
            return False
        proof = self.review_authority.verify(item,packet,target)
        judgment = (proof or {}).get('semantic_judgment') or {}
        if judgment.get('change_authorized') is not True:
            return False
        bindings = [b for b in judgment.get('bindings',[]) if b['source_type'] == 'user_statement']
        if not bindings or str(packet.occurred_at) < str(target['created_at']):
            return False
        expected = getattr(item, 'expected_version', None)
        if expected is None:
            expected = next((ref.version for ref in packet.recalled_memory_refs if ref.memory_id == target['id']), None)
        if expected != target['record_version']:
            return False
        with self.repositories.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = self._fresh_target_row(db, target['id'])
            if not self._is_current(fresh) or fresh['record_version'] != expected or not memory_scope.authorize(fresh, scope, project_id=packet.project_binding):
                return False
            metadata = json.loads(fresh['metadata'] or '{}')
            metadata['pending_change'] = dict(target_version=expected, bindings=bindings,
                fingerprint=fingerprint([fresh['id'], expected, bindings]),
                rule_version=proof['rule_version'],source_verified=True,semantic_judgment=judgment,
                review_key=proof['review_key'])
            db.execute("UPDATE memory_records SET review_state='pending',metadata=?,updated_at=? WHERE id=?",
                       (json.dumps(metadata, ensure_ascii=False), _now(), fresh['id']))
            changed = self._row(db, fresh['id'])
        self._emit('upsert', changed)
        return True

    @staticmethod
    def _is_current(row: dict) -> bool:
        """现行版本 = 还在生效、且没有被别的版本接替。"""
        return str(row.get("status") or "") == "active" and not row.get("effective_record_id")

    def _replay_of_same_change(self, db, scope: Scope, key: str) -> dict | None:
        """同一条变更的幂等重放：同一回合、同一目标、同一新版本号。"""
        stored = _idempotency_key(scope, key)
        if not stored:
            return None
        found = db.execute(
            "SELECT * FROM memory_records WHERE idempotency_key=?", (stored,)
        ).fetchone()
        if not found:
            return None
        if (found["owner_id"], found["channel"], found["conversation_id"], found["module_id"]) != _scope(scope):
            return None
        return dict(found)

    def _guard_current_target(
        self, db, scope: Scope, target_id: str, stored_version: int, *, idempotency_key: str
    ) -> tuple[dict, dict | None]:
        """写入事务里的版本保护。返回 ``(目标行, 幂等重放命中)``。

        - 版本对不上：说明取好输入之后这条记录已经被改过，重新取证，不覆盖；
        - 目标已经不是现行版本：只允许**同一条变更**的幂等重放（重传、重启恢复），
          别的建议一律拒绝 —— 否则两次基于同一旧版本的纠正会各起一行，
          同时存在两个「现行版本」，谁也说不清哪个算数。
        """
        fresh = self._fresh_target_row(db, target_id)
        fresh_version = int(fresh.get("record_version") or 0)
        if fresh_version != int(stored_version):
            raise MemoryUpdateConflict(
                f"写入前目标记录已是第 {fresh_version} 版（建议基于第 {stored_version} 版），需重新取证"
            )
        replay = self._replay_of_same_change(db, scope, idempotency_key)
        if replay is not None:
            return fresh, replay
        if not self._is_current(fresh):
            raise MemoryUpdateConflict(
                "目标记录已被其他变更替代，本次更新不能分叉出第二条现行版本，需重新取证"
            )
        return fresh, None

    def apply_semantic_update(
        self,
        scope: Scope,
        suggestion,
        *,
        target: dict | None,
        turn_id: str = "",
        occurred_at: str | None = None,
        review_interval: timedelta = timedelta(hours=24),
        lifetime=None,
        receipt: dict | None = None,
        packet=None,
        commit_guard=None,
    ) -> dict:
        """按一条更新建议原子地改库（自动记忆维护闭环的落库口）。

        一个事务里做完「写新版本 / 改旧记录 / 归档旧版本」：分几次提交时，
        中途失败会留下「旧记录已经退出当前有效集合、新版本却没写上」的空洞 ——
        那条记忆就此消失，而且没人看得出来。

        ``target`` 是程序**从库里读出来**的目标记录，不是模型说哪条就哪条。
        ``record_version`` 是这条记录自己的修订号：内容一改就 +1；产生新版本的
        动作（correct / replace / merge 带正文）另起一行、版本号接在旧的后面，
        旧行归档保留。**当前有效版本** = 状态 active 且 ``effective_record_id``
        为空的那一行。

        返回结果字典，调用方据此做写后复核。
        """
        action = str(suggestion.action)
        result: dict = {"action": action, "record": None, "archived": [], "changed": [], "bumped": None}
        if action == "ignore":
            return result
        if action in ('correct', 'replace', 'merge') and str(suggestion.content or '').strip():
            checked = check_memory_content(suggestion, packet=packet, target=target,
                                           lifetime=lifetime, prior_receipt=receipt, review_authority=self.review_authority)
            if not checked['passed']:
                raise ValueError('evidence_not_supported: ' + str(checked))
            receipt = checked
        if target is None:
            raise ValueError(f"{action} 需要目标记录")
        stored_version = int(target.get("record_version") or 0)
        if suggestion.expected_version is not None and int(suggestion.expected_version) != stored_version:
            raise ValueError(
                f"目标记录版本已变（库里第 {stored_version} 版，建议基于第 {suggestion.expected_version} 版）"
            )
        target_id = str(target["id"])
        now = _now()
        due_at = (datetime.now(UTC) + review_interval).isoformat(timespec="milliseconds")

        if action == "conflict":
            sides = [target_id, *(str(item) for item in suggestion.conflict_ids)]
            sides = list(dict.fromkeys(item for item in sides if item))
            if len(sides) < 2:
                raise ValueError("标记冲突至少要给出两条互相矛盾的记录")
            changed = []
            with self.repositories.connect() as db:
                fresh = self._fresh_target_row(db, target_id)
                if int(fresh.get("record_version") or 0) != stored_version:
                    raise MemoryUpdateConflict("标记冲突前目标版本已变，需重新取证")
                for record_id in sides:
                    db.execute(
                        "UPDATE memory_records SET review_state='pending',review_due_at=?,"
                        "conflict_with=?,updated_at=? WHERE id=?",
                        (due_at, json.dumps(sides, ensure_ascii=False), now, record_id),
                    )
                    if db.execute("SELECT changes() AS n").fetchone()["n"]:
                        changed.append(self._row(db, record_id))
            result["changed"] = changed
            for item in changed:
                self._emit("upsert", item)
            return result

        if action == "invalidate":
            with self.repositories.connect() as db:
                fresh = self._fresh_target_row(db, target_id)
                if int(fresh.get("record_version") or 0) != stored_version:
                    raise MemoryUpdateConflict("失效前目标版本已变，需重新取证")
                if suggestion.reason:
                    # 有具体理由才真的失效；没有理由只标「待复核」，内容先留着。
                    # 理由**能不能算依据**由维护服务核对来源，这里只负责落库。
                    db.execute(
                        "UPDATE memory_records SET status='archived',layer='cold',archived_at=?,"
                        "review_state='settled',review_due_at=NULL,updated_at=?,embedding_status='pending',"
                        "next_retry_at=? WHERE id=? AND status!='archived'",
                        (now, now, now, target_id),
                    )
                else:
                    db.execute(
                        "UPDATE memory_records SET review_state='pending',review_due_at=?,updated_at=? WHERE id=?",
                        (due_at, now, target_id),
                    )
                row = self._row(db, target_id)
            result["record"] = row
            self._post(row)
            self._emit("upsert", row)
            return result

        metadata = _merge_source_refs(target.get("metadata"), suggestion)
        try:
            merged = json.loads(metadata)
        except (TypeError, ValueError):
            merged = {}
        root_id = str(merged.get("root_id") or target_id)
        merged["root_id"] = root_id

        if action == "merge" and not str(suggestion.content or "").strip():
            # 只追加来源与原因：正文不动，就地记一次修订。
            # 同一个回合的同一份补充只记一次：工人中途重启后重放时，
            # 不该因为「又跑了一遍」就把版本号推到第 3、第 4 版。
            applied_turns = [str(item) for item in merged.get("merge_turns") or ()]
            if turn_id and turn_id in applied_turns:
                with self.repositories.connect() as db:
                    result["bumped"] = self._fresh_target_row(db, target_id)
                return result
            merged["record_version"] = stored_version + 1
            if turn_id:
                merged["merge_turns"] = [*applied_turns, turn_id]
            with self.repositories.connect() as db:
                fresh, replay = self._guard_current_target(
                    db, scope, target_id, stored_version, idempotency_key=""
                )
                db.execute(
                    "UPDATE memory_records SET metadata=?,record_version=?,updated_at=? WHERE id=?",
                    (json.dumps(merged, ensure_ascii=False, separators=(",", ":")), stored_version + 1, now, target_id),
                )
                row = self._row(db, target_id)
            result["bumped"] = row
            self._emit("upsert", row)
            return result

        # correct / replace / 带正文的 merge：另起一行作为新版本，旧行归档保留。
        relation_key = {"correct": "corrects", "replace": "replaces", "merge": "merge_of"}[action]
        merged[relation_key] = target_id
        merged.pop('pending_change', None)
        new_version = stored_version + 1
        merged["record_version"] = new_version
        # 寿命与保存侧同一条规则：短期的新版本写到期时间，长期的不写。
        # 更新建议不带处理包时（程序内直连调用）沿用目标记录自己的尺度。
        from .memory_semantic import reviewed_lifetime
        new_lifetime = reviewed_lifetime(self.review_authority,suggestion,packet,target) or lifetime
        if new_lifetime is None:
            declared = str(getattr(suggestion, "horizon", "") or "") or str(
                target.get("horizon") or ""
            )
            new_lifetime = resolve_lifetime(
                str(suggestion.content or target.get("content") or ""),
                kind=str(suggestion.kind or target.get("kind") or ""),
                declared=declared,
                default_short=SHORT_TERM_RETENTION,
            )
        merged.update(new_lifetime.as_metadata())
        if receipt is not None:
            # 核对回执随新版本一起持久保存：数据库故障重试时复用同一份判断。
            merged["evidence_receipt"] = dict(receipt)
        new_created_at = (
            (_parse_moment(occurred_at) or datetime.now(UTC))
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
        )
        new_expires_at = None
        if new_lifetime.is_short:
            moment = _parse_moment(new_created_at) or datetime.now(UTC)
            new_expires_at = (
                moment + (new_lifetime.expires_in or SHORT_TERM_RETENTION)
            ).astimezone(UTC).isoformat(timespec="milliseconds")
        idempotency = f"maint:{action}:{target_id}:v{new_version}:{turn_id or '-'}"
        archived: list[dict] = []
        with self.repositories.connect() as db:
            # 事务内重核目标：版本、现行状态，以及「这是不是同一条变更的重放」。
            db.execute('BEGIN IMMEDIATE')
            if commit_guard is not None:
                commit_guard(db)
            fresh, replay = self._guard_current_target(
                db, scope, target_id, stored_version, idempotency_key=idempotency
            )
            if replay is not None:
                # 同一变更的重传或重启恢复：返回已经写好的那条，不再造第二个现行版本。
                result["record"] = replay
                result["replayed"] = True
                return result
            if not memory_scope.authorize(fresh, scope, project_id=str(getattr(packet, 'project_binding', '') or '')):
                raise ValueError('target_permission_changed')
            evidence_at = _parse_moment(getattr(packet, 'occurred_at', ''))
            target_at = _parse_moment(fresh.get('created_at'))
            if evidence_at and target_at and evidence_at < target_at:
                raise ValueError('evidence_older_than_current_version')
            checked = check_memory_content(suggestion, packet=packet, target=fresh, lifetime=new_lifetime, prior_receipt=receipt,review_authority=self.review_authority)
            if not checked['passed']:
                raise ValueError('evidence_not_supported_at_commit')
            merged['evidence_receipt'] = checked
            record, created = self._insert(
                db,
                scope,
                layer=self.SEMANTIC_LAYER,
                kind=suggestion.kind or target.get("kind"),
                status="active",
                content=str(suggestion.content or target.get("content") or ""),
                confidence=suggestion.confidence,
                metadata=merged,
                importance=(
                    suggestion.importance if suggestion.importance is not None else target.get("importance")
                ),
                idempotency_key=idempotency,
                created_at=new_created_at,
                project_id=target.get("project_id"),
                content_status=suggestion.content_status or target.get("content_status"),
                semantics_version=SEMANTICS_VERSION,
                semantics_key=target.get("semantics_key"),
                scope_kind=target.get("scope_kind"),
                expires_at=new_expires_at,
                record_version=new_version,
                review_state="none",
            )
            if created:
                # 旧行的元数据从**事务里刚读到的那份**改，不用事务外的快照：
                # 期间已经有别的合并改过它时，拿旧快照回写会把那次合并抹掉。
                old_meta = json.loads(fresh.get("metadata") or "{}") if fresh.get("metadata") else {}
                if not isinstance(old_meta, dict):
                    old_meta = {}
                old_meta["effective_record_id"] = record["id"]
                old_meta[f"superseded_by_{action}"] = record["id"]
                cursor = db.execute(
                    "UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,"
                    "embedding_status='pending',next_retry_at=?,effective_record_id=?,content_status=?,metadata=? "
                    "WHERE id=? AND status!='archived'",
                    (
                        now,
                        now,
                        now,
                        record["id"],
                        "superseded" if action in ("replace", "correct") else target.get("content_status"),
                        json.dumps(old_meta, ensure_ascii=False, separators=(",", ":")),
                        target_id,
                    ),
                )
                if cursor.rowcount:
                    item = self._row(db, target_id)
                    item.update(status="archived", layer="cold", embedding_status="pending")
                    archived.append(item)
                else:
                    # 理论上到不了这里（事务里刚核过现行状态）。真到了说明判断和写入
                    # 之间有别的变更插进来 —— 此时**整体回滚**，宁可这次不生效，
                    # 也不能留下两条同时现行的版本。
                    raise MemoryUpdateConflict("旧目标已不是现行版本，本次更新整体回滚")
        result["record"] = record
        result["archived"] = archived
        if created:
            self._post(record)
            self._emit("upsert", record)
        for item in archived:
            self._post(item)
            self._emit("upsert", item)
        return result

    def save_extracted_memories(self, scope, memories, *, user_text="", assistant_reply="", turn_id="", run_id="", occurred_at=None):
        """Persist extractor output through the normal memory path.

        The extractor can only suggest memories; this method makes them real SQLite/Qdrant
        records by delegating to ``save_memory`` with provenance stored in metadata.

        ``occurred_at`` 透传给 ``save_memory``，让记忆寿命从对话发生时间算起 ——
        云端补提炼历史对话时靠它，否则一百条三天前的对话会在同一秒里
        变成"刚记住的事"，寿命全从今天算。
        """
        persisted = []
        for index, memory in enumerate(memories):
            metadata = {
                "source": "web.async_memory_extraction",
                "source_type": getattr(memory, "source_type", "llm_extracted"),
                "turn_id": turn_id,
                "run_id": run_id,
                "user_text": user_text,
                "assistant_reply": assistant_reply,
                "memory_index": index,
            }
            record = self.save_memory(
                scope,
                memory.content,
                conflict_key=memory.conflict_key,
                confidence=memory.confidence,
                importance=memory.importance,
                duration=memory.duration,
                metadata=metadata,
                occurred_at=occurred_at,
                # 幂等键钉在「哪一轮的第几条」上：提炼结果已经写进库、但还没
                # 来得及标记任务完成时进程被杀，重启后同一轮会被重新提炼一遍。
                # 没有这个键就会凭空多出一条一模一样的记忆，而且没人看得出来。
                idempotency_key=f"turn:{turn_id}:{index}" if turn_id else None,
            )
            record["extraction_source_type"] = metadata["source_type"]
            persisted.append(record)
        return persisted
    def confirm_candidate(self,scope,candidate_id,*,conflict_key=None):
        with self.repositories.connect() as db:
            rec=self._row(db,candidate_id)
            if not rec or _scope(scope)!=(rec['owner_id'],rec['channel'],rec['conversation_id'],rec['module_id']) or rec['status']!='candidate': raise ValueError('candidate not found')
            key=conflict_key if conflict_key is not None else rec['conflict_key']; now=_now(); visibility=rec['visibility']
            db.execute("UPDATE memory_records SET status='confirmed',kind='fact',conflict_key=?,updated_at=? WHERE id=?",(key,now,candidate_id))
            where,args=_conflict_filter(scope,visibility,key or '')
            if key: db.execute(f"UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,embedding_status='pending',next_retry_at=? WHERE {where} AND status='confirmed' AND id<>?",(now,now,now,*args,candidate_id))
            rec = self._row(db,candidate_id)
            moved=[dict(row) for row in db.execute(f"SELECT * FROM memory_records WHERE {where} AND status='archived'",args)] if key else []
        self._post(rec)
        for old in moved:self._post(old)
        for item in [rec, *moved]: self._emit('upsert', item)
        return rec
    def archive_records(self,scope,record_ids):
        rows=[]
        with self.repositories.connect() as db:
            for record_id in record_ids:
                row=db.execute("SELECT * FROM memory_records WHERE id=? AND owner_id=? AND channel=? AND conversation_id=? AND module_id=?",(record_id,*_scope(scope))).fetchone()
                if row:
                    db.execute("UPDATE memory_records SET status='archived',layer='cold',archived_at=?,updated_at=?,embedding_status='pending' WHERE id=?",(_now(),_now(),record_id))
                    rows.append(dict(row))
        for row in rows:
            row["status"]="archived"; row["layer"]="cold"; row["embedding_status"]="pending"
            self._post(row)
            self._emit('upsert', row)
        return rows

    def touch_records(self, record_ids):
        return self.repositories.touch_records(record_ids, accessed_at=_now())

    def _delete_embedding(self, record):
        old_layer = record.get("embedding_layer")
        if old_layer:
            self.repositories.vector_client.delete(f"{old_layer}_memories_vectors", models.PointIdsList(points=[record["id"]]))
        with self.repositories.connect() as db:
            db.execute("UPDATE memory_records SET embedding_status='deleted',embedding_layer=NULL,retry_count=0,next_retry_at=NULL,last_embedding_error=NULL,embedding_lease_until=NULL,updated_at=? WHERE id=?", (_now(), record["id"]))

    def run_maintenance(self, now=None, *, limit=100):
        current = now if isinstance(now, datetime) else datetime.now(UTC)
        now_text = current.astimezone(UTC).isoformat(timespec="milliseconds")
        migrated=[]; expiring=[]; deferred=0
        with self.repositories.connect() as db:
            hot_cutoff=(current-HOT_TO_WARM_AFTER).astimezone(UTC).isoformat(timespec="milliseconds")
            for row in db.execute("SELECT * FROM memory_records WHERE layer='hot' AND status='active' AND created_at<=? LIMIT ?",(hot_cutoff,limit)).fetchall():
                db.execute("UPDATE memory_records SET layer='warm',embedding_status='pending',updated_at=? WHERE id=?",(now_text,row["id"]))
                item=dict(row); item.update(layer="warm",embedding_status="pending"); migrated.append(item)
            # 双槽记忆到期即失效：warm 里带 expires_at 且已到期的行标记为 expired
            # （保留真实 expires_at）。带上 active：显式短期的新版本也是 active，
            # 只有**自己写了到期时间**的行会命中，长期结论的 expires_at 为空，不受影响。
            for row in db.execute("SELECT * FROM memory_records WHERE layer='warm' AND status IN ('confirmed','candidate','active') AND expires_at IS NOT NULL AND expires_at<=? AND retention_locked=0 LIMIT ?",(now_text,limit)).fetchall():
                purge=(current+EXPIRY_AUDIT_PERIOD).astimezone(UTC).isoformat(timespec="milliseconds")
                db.execute("UPDATE memory_records SET status='expired',purge_after=?,deletion_reason='expired',updated_at=? WHERE id=?",(purge,now_text,row["id"]))
                item=dict(row); item.update(status="expired",purge_after=purge); expiring.append(item)
            # 冷层尊重自身 expires_at：被替换/归档到 cold 的双槽记忆按自己的到期时间失效（保留真实 expires_at，不按 180 天冷保留）
            for row in db.execute("SELECT * FROM memory_records WHERE layer='cold' AND status='archived' AND expires_at IS NOT NULL AND expires_at<=? AND retention_locked=0 LIMIT ?",(now_text,limit)).fetchall():
                purge=(current+EXPIRY_AUDIT_PERIOD).astimezone(UTC).isoformat(timespec="milliseconds")
                db.execute("UPDATE memory_records SET status='expired',purge_after=?,deletion_reason='expired',updated_at=? WHERE id=?",(purge,now_text,row["id"]))
                item=dict(row); item.update(status="expired",purge_after=purge); expiring.append(item)
            candidate_cutoff=(current-CANDIDATE_EXPIRE_AFTER).astimezone(UTC).isoformat(timespec="milliseconds")
            for row in db.execute("SELECT * FROM memory_records WHERE layer='warm' AND status='candidate' AND retention_locked=0 AND COALESCE(last_accessed_at,created_at)<=? LIMIT ?",(candidate_cutoff,limit)).fetchall():
                purge=(current+EXPIRY_AUDIT_PERIOD).astimezone(UTC).isoformat(timespec="milliseconds")
                db.execute("UPDATE memory_records SET status='expired',expires_at=?,purge_after=?,deletion_reason='decay',updated_at=? WHERE id=?",(now_text,purge,now_text,row["id"]))
                item=dict(row); item.update(status="expired",expires_at=now_text,purge_after=purge); expiring.append(item)
            cold_cutoff=(current-COLD_EXPIRE_AFTER).astimezone(UTC).isoformat(timespec="milliseconds")
            for row in db.execute("SELECT * FROM memory_records WHERE layer='cold' AND status='archived' AND retention_locked=0 AND COALESCE(last_accessed_at,created_at)<=? LIMIT ?",(cold_cutoff,limit)).fetchall():
                purge=(current+EXPIRY_AUDIT_PERIOD).astimezone(UTC).isoformat(timespec="milliseconds")
                db.execute("UPDATE memory_records SET status='expired',expires_at=?,purge_after=?,deletion_reason='cold_retention',updated_at=? WHERE id=?",(now_text,purge,now_text,row["id"]))
                item=dict(row); item.update(status="expired",expires_at=now_text,purge_after=purge); expiring.append(item)
            expired=list(db.execute("SELECT * FROM memory_records WHERE status='expired' AND retention_locked=0 AND purge_after IS NOT NULL AND purge_after<=? LIMIT ?",(now_text,limit)).fetchall())
            cleanup_pending=[dict(row) for row in db.execute("SELECT * FROM memory_records WHERE status='expired' AND retention_locked=0 AND embedding_layer IS NOT NULL AND embedding_status != 'deleted' AND (next_retry_at IS NULL OR next_retry_at<=?) LIMIT ?",(now_text,limit)).fetchall()]
        for item in migrated:
            self._post(item)
        for item in [*migrated, *expiring]:
            self._emit('upsert', item)
        cleanup_items={item["id"]: item for item in [*expiring, *cleanup_pending]}
        for item in cleanup_items.values():
            try:self._delete_embedding(item)
            except Exception as exc:
                deferred += 1
                retry_at=(datetime.now(UTC)+timedelta(seconds=RETRY_INTERVAL_SECONDS)).isoformat(timespec="milliseconds")
                with self.repositories.connect() as db: db.execute("UPDATE memory_records SET embedding_status='delete_failed',retry_count=retry_count+1,next_retry_at=?,last_embedding_error=?,updated_at=? WHERE id=?",(retry_at,str(exc)[:1000],_now(),item["id"]))
        purged=0
        for row in expired:
            item=dict(row)
            try:
                self._delete_embedding(item)
                with self.repositories.connect() as db: db.execute("DELETE FROM memory_records WHERE id=?",(item["id"],))
                purged += 1
                self._emit('delete', item)
            except Exception:
                deferred += 1
        result={"migrated":len(migrated),"expired":len(expiring),"purged":purged,"deferred":deferred}
        LOGGER.info("维护完成：迁移=%d 到期=%d 清除=%d 延后=%d", result["migrated"], result["expired"], result["purged"], result["deferred"])
        return result
    def batch_save(self, records):
        result=[]; moved=[]
        with self.repositories.connect() as db:
            for item in records:
                data={k:v for k,v in item.items() if k!='scope'}; scope=item['scope']
                if data.get('kind') == 'fact' and data.get('status') == 'confirmed':
                    if data.get('confidence',0) < CONFIRMED_THRESHOLD: raise ValueError('confirmed facts require confidence >= 0.85')
                    data.setdefault('visibility', 'shared')
                    rec,new=self._insert(db,scope,**data)
                    key=data.get('conflict_key')
                    if new and key:
                        where,args=_conflict_filter(scope,data.get('visibility','shared'),key)
                        db.execute(f"UPDATE memory_records SET status='archived',layer='cold',embedding_status='pending',next_retry_at=? WHERE {where} AND status='confirmed' AND id<>?",(_now(),*args,rec['id']))
                        moved.extend(dict(x) for x in db.execute(f"SELECT * FROM memory_records WHERE {where} AND status='archived'",args))
                else: rec,new=self._insert(db,scope,**data)
                result.append((rec,new))
        for rec,new in result:
            if new:self._post(rec)
        for rec in moved:self._post(rec)
        for rec,new in result:
            if new: self._emit('upsert', rec)
        for rec in moved: self._emit('upsert', rec)
        return [rec for rec,_ in result]
    def sync_pending_embeddings(self,limit=100):
        now=_now(); lease=(datetime.now(UTC)+timedelta(minutes=5)).isoformat(timespec='milliseconds')
        with self.repositories.connect() as db:
            ids=[row[0] for row in db.execute("SELECT id FROM memory_records WHERE (embedding_status='pending' OR (embedding_status='failed' AND (next_retry_at IS NULL OR next_retry_at<=?)) OR (embedding_status='processing' AND embedding_lease_until<?)) AND retry_count<? LIMIT ?",(now,now,MAX_EMBEDDING_RETRIES,limit))]
            db.executemany("UPDATE memory_records SET embedding_status='processing',embedding_lease_until=?,last_embedding_attempt_at=? WHERE id=?",[(lease,now,record_id) for record_id in ids])
            records=[self._row(db,record_id) for record_id in ids]
        result={'ready':0,'failed':0,'dead_letter':0}
        for record in records:
            try:self._sync(record);result['ready']+=1
            except Exception as exc:
                self._record_embedding_failure(record['id'],str(exc))
                with self.repositories.connect() as db: status=db.execute('SELECT embedding_status FROM memory_records WHERE id=?',(record['id'],)).fetchone()[0]
                result[status]+=1
        LOGGER.info('嵌入同步：成功=%d 失败=%d 死信=%d', result['ready'], result['failed'], result['dead_letter'])
        return result
    def list_records(self,scope):
        with self.repositories.connect() as db:return [dict(x) for x in db.execute('SELECT * FROM memory_records WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=? ORDER BY created_at',_scope(scope))]
    def get_memory_state_overview(self,scope=None):
        query='SELECT layer,status,count(*) n FROM memory_records'; args=()
        if scope: query+=' WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?';args=_scope(scope)
        query+=' GROUP BY layer,status'
        with self.repositories.connect() as db:
            counts={(x['layer'],x['status']):x['n'] for x in db.execute(query,args)}
            where=" WHERE owner_id=? AND channel=? AND conversation_id=? AND module_id=?" if scope else ""
            embedding={x['embedding_status']:x['n'] for x in db.execute("SELECT embedding_status,count(*) n FROM memory_records"+where+" GROUP BY embedding_status",args)}
        return {'counts':counts,'embedding':embedding,'transitions':['active_hot->archived_cold','candidate_warm->confirmed_warm','confirmed_warm->archived_cold']}

class EmbeddingRetryWorker:
    def __init__(self, writer, *, interval_seconds=30, batch_size=100):
        self.writer,self.interval_seconds,self.batch_size=writer,interval_seconds,batch_size
        self._stop=threading.Event(); self._thread=None
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._run,daemon=True,name='embedding-retry-worker'); self._thread.start()
    def stop(self, timeout=None):
        self._stop.set()
        if self._thread:self._thread.join(timeout)
    def _run(self):
        while not self._stop.is_set():
            self.writer.sync_pending_embeddings(self.batch_size)
            self._stop.wait(self.interval_seconds)


class MemoryMaintenanceWorker:
    def __init__(self, writer, *, interval_seconds=60):
        self.writer,self.interval_seconds=writer,interval_seconds
        self._stop=threading.Event(); self._thread=None
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._run,daemon=True,name='memory-maintenance-worker'); self._thread.start()
    def stop(self, timeout=None):
        self._stop.set()
        if self._thread:self._thread.join(timeout)
    def _run(self):
        while not self._stop.is_set():
            try:self.writer.run_maintenance()
            except Exception as exc: LOGGER.warning("维护失败：错误类型=%s",type(exc).__name__)
            self._stop.wait(self.interval_seconds)
