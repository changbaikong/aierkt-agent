"""Deterministic hybrid retrieval for hot, warm, and cold memories."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import re
from typing import Any, Protocol

from .contracts import Scope
from . import memory_scope
from .memory import MemoryContext
from .memory_repositories import MemoryRepositories, _expires_at_passed
from .memory_logging import configure_memory_logging
from .memory_filtering import FilteringRanker
from . import memory_switches
from .memory_turn import SUPERSEDED_STATUSES, estimate_units


RRF_K = 60
CONFIRMED_LIMIT = 6
WARM_OTHER_LIMIT = 4
COLD_LIMIT = 5
DEFAULT_TOKEN_BUDGET = 1200
#: 单条记忆最多占多少预算；决策的必要原因/状态可以多占，所以这里是总量控制而不是硬截断。
SINGLE_RECORD_TOKEN_CAP = 350
#: 直接相关记录条数上限，以及由它们扩展出来的关联记录上限。
DIRECT_RECORD_LIMIT = 5
EXPANSION_RECORD_LIMIT = 10
LOGGER = logging.getLogger("agent_core.memory.retrieve")
#: 指代词：出现这些词、又拿得到近期历史时，检索词要带上文再查一次。
REFERENCE_WORDS = (
    "这个", "那个", "这次", "那次", "上次", "它", "该方案", "上述", "前面", "刚才",
)
HISTORY_MARKERS = (
    "\u4ee5\u524d", "\u4e4b\u524d", "\u4e0a\u6b21", "\u8fc7\u53bb", "\u5386\u53f2",
    "\u804a\u8fc7", "\u8fd8\u8bb0\u5f97", "\u8bb0\u5f97", "\u5f53\u65f6", "\u90a3\u65f6\u5019",
    "previous", "history",
)
CITATION_MARKERS = (
    "\u6765\u6e90", "\u4f9d\u636e", "\u5386\u53f2", "source", "basis", "history",
)


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


def resolve_query(query: str, history: list[dict[str, Any]] | None) -> tuple[str, bool]:
    """代词续问时，把上文并进检索词。

    「这个怎么和搜索结合」单独去查，字面完全匹配不到任何东西；把上一条用户话
    并进去，才可能命中前面讨论过的方案。只用**已经拿到的**历史，不调模型、
    也不生成新的历史事实。
    """
    text = str(query or "")
    if not history or not any(word in text for word in REFERENCE_WORDS):
        return text, False
    recent = [
        str(item.get("content") or "").strip()
        for item in history
        if str(item.get("role") or "") == "user" and str(item.get("content") or "").strip()
    ]
    if not recent:
        return text, False
    return f"{text} {recent[-1][:120]}", True


def rank_group(row: dict[str, Any]) -> int:
    """任务价值分组：数字越小越该先给模型看。

    0 已采纳且有效的决策（当前约束）→ 1 直接相关的事实/计划 → 2 经验 →
    3 偏好 → 4 开放问题与假设。旧格式记录按老的确认状态近似落组。
    """
    if not memory_scope.is_semantic(row):
        if row.get("kind") == "fact" and row.get("status") == "confirmed":
            return 1
        return 3
    kind = str(row.get("kind") or "")
    status = str(row.get("content_status") or "")
    if kind == "decision":
        return 0 if status == "accepted" else 3
    if kind in ("fact", "plan"):
        return 1 if status in ("reported", "verified", "accepted", "in_progress", "completed") else 3
    if kind == "experience":
        return 2
    if kind == "preference":
        return 3
    return 4


def slice_content(
    content: str,
    query: str,
    *,
    cap_units: int = SINGLE_RECORD_TOKEN_CAP,
    cap_chars: int | None = None,
) -> tuple[str, bool]:
    """长记忆按当前查询选片段，不整条丢掉。

    返回 ``(片段, 是否被裁)``。找不到和当前问题相关的片段时返回空串 ——
    调用方据此**不选**这条记录。硬塞一段无关长文进去，比不选更糟：
    它会挤掉真正相关的内容，而且模型无从判断这段是不是回答所需的。

    刻意不截断中段之外的结构化内容：片段边界取句号/换行，避免把
    「失败条件」这类后半段信息切成两半。

    ``cap_chars`` 是「按字符数算的另一个上限」：下游还有个按字符长度过滤的
    环节，只按单位判会出现「单位没超、字符却超了」的记录被整条删掉的情况。
    """
    raw = str(content or "")
    overlong = estimate_units(raw) > cap_units or (
        cap_chars is not None and len(raw) > cap_chars
    )
    if not overlong:
        return raw, False
    terms = [term for term in re.findall(r"[\w\u4e00-\u9fff]+", str(query or "")) if len(term) >= 2]
    best_at = -1
    for term in terms:
        at = raw.find(term)
        if at >= 0 and (best_at < 0 or at < best_at):
            best_at = at
    if best_at < 0:
        # 超过单条上限、又找不到与当前问题相关的片段：返回空片段并标记为已裁，
        # 调用方据此**不选**这条记录。硬塞一段无关长文进去比不选更糟：
        # 它会挤掉真正相关的内容，模型也无从判断这段是不是回答所需的。
        return "", True
    # 以命中位置为中心取一段，再向两侧扩张到句子边界。
    window = cap_units if cap_chars is None else min(cap_units, cap_chars)
    half = max(1, window // 2)
    start = max(0, best_at - half)
    end = min(len(raw), best_at + half)
    left = max(raw.rfind("\n", 0, start), raw.rfind("。", 0, start), raw.rfind("；", 0, start))
    right_candidates = [at for at in (raw.find("\n", end), raw.find("。", end), raw.find("；", end)) if at >= 0]
    if left >= 0:
        start = left + 1
    if right_candidates:
        end = min(right_candidates) + 1
    fragment = raw[start:end].strip()
    return fragment, True


@dataclass
class RetrievalResult:
    hot: list[dict[str, Any]] = field(default_factory=list)
    warm: list[dict[str, Any]] = field(default_factory=list)
    cold_evidence: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def items(self) -> list[dict[str, Any]]:
        return [*self.hot, *self.warm, *self.cold_evidence]


class MemoryRetriever:
    """Read-only RAG service. SQLite decides authorization and lifecycle state."""

    def __init__(
        self,
        repositories: MemoryRepositories,
        embeddings: EmbeddingProvider,
        filtering_ranker: FilteringRanker | None = None,
    ) -> None:
        configure_memory_logging()
        self.repositories = repositories
        self.embeddings = embeddings
        self.filtering_ranker = filtering_ranker or FilteringRanker()

    @staticmethod
    def _is_history_query(query: str) -> bool:
        lowered = query.lower()
        return any(marker in lowered for marker in HISTORY_MARKERS)

    @staticmethod
    def _wants_citations(query: str) -> bool:
        lowered = query.lower()
        return any(marker in lowered for marker in CITATION_MARKERS)

    @staticmethod
    def _full_scope(row: dict[str, Any], scope: Scope) -> bool:
        return (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"]) == (
            scope.owner_id, scope.channel, scope.conversation_id, scope.module_id,
        )

    @staticmethod
    def _owner_channel(row: dict[str, Any], scope: Scope) -> bool:
        return (row["owner_id"], row["channel"]) == (scope.owner_id, scope.channel)

    def _warm_allowed(
        self,
        row: dict[str, Any],
        scope: Scope,
        *,
        vector: bool,
        project_id: str = "",
        channels: tuple[str, ...] | None = None,
    ) -> bool:
        if row["layer"] != "warm":
            return False
        if row["status"] == "expired":
            return False
        # 直接按 expires_at 判一次，不只看状态字段。
        # 云端模式下本地不再跑轮转维护，而轮转正是把过期记录翻成 'expired' 的那一步；
        # 只认状态的话，一条已经到期、但云端刚改了到期时间还没来得及同步过来的记忆，
        # 会继续被当成有效事实用 —— 而"过期就不该再用"是这条记忆自己的属性，
        # 不该依赖某台机器有没有按时跑维护。
        if _expires_at_passed(row.get("expires_at"), now=datetime.now(UTC)):
            return False
        if vector and row["embedding_status"] != "ready":
            return False
        if memory_scope.is_semantic(row):
            from .memory_binding import valid_pending_change
            if valid_pending_change(row):
                return False
            # 新语义记录：范围由记录自己声明，授权规则只有一处。
            return memory_scope.authorize(row, scope, project_id=project_id, channels=channels)
        if row["visibility"] == "shared" and row["kind"] == "fact" and row["status"] == "confirmed":
            return self._owner_channel(row, scope)
        if row["visibility"] == "module" and row["kind"] == "candidate" and row["status"] == "candidate":
            return self._full_scope(row, scope)
        return row["visibility"] == "module" and row["status"] in {"active", "confirmed"} and self._full_scope(row, scope)

    def _cold_allowed(
        self,
        row: dict[str, Any],
        scope: Scope,
        *,
        vector: bool,
        project_id: str = "",
        channels: tuple[str, ...] | None = None,
    ) -> bool:
        if row["layer"] != "cold" or row["status"] != "archived":
            return False
        if vector and row["embedding_status"] != "ready":
            return False
        # 冷层同样分新旧两种格式：被替代的新语义决策进冷层后，解释历史时仍要按
        # 它自己的范围读，不能因为"在冷层"就放宽成账号级。
        if memory_scope.is_semantic(row):
            return memory_scope.authorize(row, scope, project_id=project_id, channels=channels)
        if row["visibility"] == "shared":
            return self._owner_channel(row, scope)
        return self._full_scope(row, scope)

    def _hybrid_candidates(
        self,
        scope: Scope,
        query: str,
        *,
        layer: str,
        project_id: str = "",
        channels: tuple[str, ...] | None = None,
        include_semantic: bool = True,
    ) -> list[dict[str, Any]]:
        fts_ids = self.repositories.fts_candidates(
            scope, query, layer=layer, limit=30, project_id=project_id, channels=channels,
            include_semantic=include_semantic,
        )
        try:
            vector_ids = self.repositories.vector_candidates(
                scope, layer, self.embeddings.embed(query), limit=30,
                project_id=project_id, channels=channels, include_semantic=include_semantic,
            )
        except Exception:
            vector_ids = []

        records = self.repositories.records_by_ids([*fts_ids, *vector_ids])
        scores: dict[str, float] = {}
        sources: dict[str, set[str]] = {}
        for source, ids in (("fts", fts_ids), ("vector", vector_ids)):
            for rank, record_id in enumerate(ids, start=1):
                row = records.get(record_id)
                if row is None:
                    continue
                if not include_semantic and memory_scope.is_semantic(row):
                    continue
                allowed = (
                    self._warm_allowed(
                        row, scope, vector=source == "vector",
                        project_id=project_id, channels=channels,
                    )
                    if layer == "warm"
                    else self._cold_allowed(
                        row, scope, vector=source == "vector",
                        project_id=project_id, channels=channels,
                    )
                )
                if not allowed:
                    continue
                scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (RRF_K + rank)
                sources.setdefault(record_id, set()).add(source)

        items: list[dict[str, Any]] = []
        for record_id, score in scores.items():
            row = dict(records[record_id])
            row["hit_sources"] = tuple(source for source in ("fts", "vector") if source in sources[record_id])
            row["score"] = score
            row["evidence_only"] = layer == "cold"
            items.append(row)
        return sorted(items, key=lambda row: self._sort_key(row, scope), reverse=True)

    @staticmethod
    def _sort_key(row: dict[str, Any], scope: Scope) -> tuple[int, int, float, float, str]:
        """排序：任务价值分组 → 同会话优先 → 融合得分 → 依据可信度 → 时间。

        时间排在最后：它只用来区分适用性和相近候选，不能让「最近很热闹的闲聊」
        压过一条仍然有效的架构约束。硬约束与已采纳决策由 :func:`rank_group` 决定。
        """
        full_scope = int(
            (row["owner_id"], row["channel"], row["conversation_id"], row["module_id"])
            == (scope.owner_id, scope.channel, scope.conversation_id, scope.module_id)
        )
        return (
            -rank_group(row),
            full_scope,
            float(row["score"]),
            float(row["confidence"] or 0),
            str(row["created_at"]),
        )

    @staticmethod
    def _tokens(item: dict[str, Any]) -> int:
        """一条记忆的预算占用：正文 + 原因 + 标签与引用。

        旧的 ``字数/4`` 在中文上低估约四倍，中文按每字一个单位算。

        处于热窗口的对话消息只按正文算：它们注入时只有 ``角色: 正文``，
        没有类型、状态和原因，把标签也算进去会让短消息凭空贵一倍，
        热窗口莫名其妙地缩短。
        """
        if str(item.get("kind") or "") == "message":
            return max(1, estimate_units(item.get("content")))
        try:
            metadata = json.loads(item.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        reason = str(metadata.get("reason") or "") if isinstance(metadata, dict) else ""
        labels = f"{item.get('kind','')}{item.get('content_status') or ''}{item.get('conflict_key') or ''}"
        return max(1, estimate_units(item.get("content")) + estimate_units(reason) + estimate_units(labels))

    def _apply_token_budget(
        self,
        hot: list[dict[str, Any]],
        warm: list[dict[str, Any]],
        cold: list[dict[str, Any]],
        budget: int,
        *,
        history_intent: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], int]:
        remaining = max(0, budget)
        selected: set[str] = set()

        def add_group(group: list[dict[str, Any]], *, contiguous: bool = False) -> None:
            nonlocal remaining
            for item in group:
                cost = self._tokens(item)
                if cost > remaining:
                    if contiguous:
                        break
                    continue
                selected.add(item["id"])
                remaining -= cost

        confirmed = [item for item in warm if item["kind"] == "fact" and item["status"] == "confirmed"]
        other_warm = [item for item in warm if item not in confirmed]
        # **任务价值先于类别**：已采纳的决策、可直接用的事实与计划先占预算，
        # 再轮到近期闲聊与低价值材料。以前只在前段排序里按价值分组，预算发放
        # 仍按「确认事实 → 热对话 → 其它温区 → 冷区」的老顺序走 —— 65 单位的
        # 预算会被 60 字的无关闲聊占满，一条已采纳的架构约束反而选不进来（B-R8）。
        high_value = [item for item in other_warm if rank_group(item) <= 1]
        rest_warm = [item for item in other_warm if rank_group(item) > 1]
        # Historical questions must reserve budget for historical evidence. For normal
        # turns, confirmed facts and the latest contiguous hot window remain first.
        if history_intent:
            add_group(cold)
            add_group(confirmed)
            add_group(high_value)
            add_group(list(reversed(hot)), contiguous=True)
            add_group(rest_warm)
        else:
            add_group(high_value)
            add_group(confirmed)
            add_group(list(reversed(hot)), contiguous=True)
            add_group(rest_warm)
            add_group(cold)
        select_group = lambda group: [item for item in group if item["id"] in selected]
        return select_group(hot), select_group(warm), select_group(cold), budget - remaining

    def _expand_once(
        self,
        scope: Scope,
        seeds: list[dict[str, Any]],
        *,
        project_id: str,
        channels: tuple[str, ...],
        limit: int = EXPANSION_RECORD_LIMIT,
        include_semantic: bool = True,
    ) -> list[dict[str, Any]]:
        """一跳关联：从已选中的记录出发，按显式关系或共享主题各扩展一次。

        关联记录**重新授权**：关系指向的 ID 可能落在别的会话或别的项目里，
        不重新过一遍授权就等于把范围规则绕过去了。

        召回开关关掉时，关联这条路也要关上：直接候选守开关、一跳关联不守，
        结果就是「新版召回明明关了，新记录还是从关系旁路漏进上下文」。
        """
        seed_ids = {str(item["id"]) for item in seeds}
        related: dict[str, list[str]] = {}
        topics: dict[str, list[str]] = {}
        for item in seeds:
            try:
                metadata = json.loads(item.get("metadata") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                continue
            for key in ("related_ids", "supersedes"):
                for target in metadata.get(key) or []:
                    target_id = str(target)
                    if target_id and target_id not in seed_ids:
                        related.setdefault(target_id, []).append(str(item["id"]))
            for topic in metadata.get("topics") or []:
                topics.setdefault(str(topic), []).append(str(item["id"]))

        candidates: list[str] = list(related)
        if topics and include_semantic and len(candidates) < limit:
            # 共享主题扩展：只在同账号、同为新格式的记录里找，具体放行仍由授权函数决定。
            topic_values = list(topics)[:10]
            placeholders = ",".join("?" for _ in topic_values)
            try:
                with self.repositories.connect() as db:
                    rows = db.execute(
                        "SELECT DISTINCT memory_records.id FROM memory_records, "
                        "json_each(memory_records.metadata, '$.topics') "
                        "WHERE memory_records.owner_id=? AND memory_records.semantics_version>=1 "
                        f"AND json_each.value IN ({placeholders}) LIMIT ?",
                        (scope.owner_id, *topic_values, limit * 2),
                    ).fetchall()
            except Exception:
                rows = []
            for row in rows:
                record_id = str(row["id"])
                if record_id not in seed_ids and record_id not in candidates:
                    candidates.append(record_id)

        records = self.repositories.records_by_ids(candidates[: limit * 2])
        expanded: list[dict[str, Any]] = []
        for record_id in candidates:
            row = records.get(record_id)
            if row is None:
                continue
            if memory_scope.is_semantic(row):
                if not include_semantic:
                    # 开关关掉时新记录整层不可见 —— 关系旁路也不行。
                    continue
                allowed = memory_scope.authorize(row, scope, project_id=project_id, channels=channels)
            else:
                allowed = self._warm_allowed(row, scope, vector=False, project_id=project_id, channels=channels)
            if not allowed:
                continue
            item = dict(row)
            item.update(
                hit_sources=("relation",),
                score=0.0,
                evidence_only=row["layer"] == "cold",
                relation_from=related.get(record_id, []),
            )
            expanded.append(item)
            if len(expanded) >= limit:
                break
        return expanded

    @staticmethod
    def _hot_contents(hot: list[dict[str, Any]]) -> set[str]:
        return {re.sub(r"\s+", "", str(item.get("content") or "")).lower() for item in hot}

    def _dedupe_against_history(
        self, items: list[dict[str, Any]], hot_contents: set[str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """近期历史里已经有的同源内容不重复注入，只留引用。

        同一段内容既以对话历史出现、又以记忆出现，等于用两份预算说同一句话；
        长对话里正是这样把真正相关的温冷记忆挤出去的。
        """
        kept: list[dict[str, Any]] = []
        elided: list[dict[str, Any]] = []
        for item in items:
            key = re.sub(r"\s+", "", str(item.get("content") or "")).lower()
            if key and key in hot_contents:
                elided.append(
                    {
                        "id": item["id"],
                        "kind": item.get("kind"),
                        "content_status": item.get("content_status"),
                    }
                )
                continue
            kept.append(item)
        return kept, elided

    def _slice_long_records(
        self,
        items: list[dict[str, Any]],
        query: str,
        cap_units: int,
        *,
        cap_chars: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """长记忆按当前查询取片段；取不到相关片段就**不选**这条记录。"""
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for item in items:
            fragment, sliced = slice_content(
                item.get("content") or "", query, cap_units=cap_units, cap_chars=cap_chars
            )
            if sliced and not fragment:
                dropped.append({"id": item["id"], "reason": "no_relevant_fragment"})
                continue
            if sliced:
                item = dict(item)
                item["content"] = fragment
                item["content_sliced"] = True
            kept.append(item)
        return kept, dropped

    def retrieve(
        self,
        scope: Scope,
        query: str,
        *,
        token_budget: int | None = None,
        project_id: str | None = None,
        channels: tuple[str, ...] | None = None,
        history: list[dict[str, Any]] | None = None,
        query_rewriter: Any = None,
        record_cap_units: int = SINGLE_RECORD_TOKEN_CAP,
        include_semantic: bool | None = None,
    ) -> RetrievalResult:
        # 可信项目绑定默认取运行配置；调用方显式传时以调用方为准。
        bound_project = memory_scope.project_binding_from_env() if project_id is None else project_id
        allowed_channels = tuple(channels) if channels is not None else memory_scope.owner_channels()
        # 召回开关：默认跟随新版语义记忆总开关；关掉时新记录对检索不可见。
        semantic_on = (
            memory_switches.semantic_recall_enabled() if include_semantic is None else include_semantic
        )

        # 指代解析：代词续问先把上文并进检索词；还含糊时最多允许一次模型改写。
        retrieval_query, reference_resolved = resolve_query(query, history)
        model_rewrites = 0
        if query_rewriter is not None and not reference_resolved:
            try:
                rewritten = query_rewriter(retrieval_query)
            except Exception:
                rewritten = ""
            if rewritten:
                retrieval_query = str(rewritten)
                model_rewrites = 1

        hot = []
        for row in self.repositories.recent_hot_records(scope):
            item = dict(row)
            item.update(hit_sources=("hot",), score=0.0, evidence_only=False)
            hot.append(item)

        # 召回 warm 层候选（FTS + 向量检索 + RRF 融合）
        warm_candidates = self._hybrid_candidates(
            scope, retrieval_query, layer="warm", project_id=bound_project,
            channels=allowed_channels, include_semantic=semantic_on,
        )

        history_intent = self._is_history_query(query)

        # 冷层**不因为温区有结果就整层跳过**：温区是近期闲聊时，真正的架构约束
        # 常常只在冷区。温冷都参与直接候选，谁先占预算由历史型问法决定。
        cold_candidates = self._hybrid_candidates(
            scope, retrieval_query, layer="cold", project_id=bound_project,
            channels=allowed_channels, include_semantic=semantic_on,
        )[: COLD_LIMIT * 2]

        # **先切片，再让过滤层计量长度**。反过来是「先按字符长度整条删掉，再想
        # 切片」—— 900 多字的记忆里最后那句失败条件永远轮不到被看一眼（B-R7）。
        slice_chars = getattr(self.filtering_ranker, "max_length", None)
        long_dropped: list[dict[str, Any]] = []
        warm_candidates, dropped_w = self._slice_long_records(
            warm_candidates, retrieval_query, record_cap_units, cap_chars=slice_chars
        )
        cold_candidates, dropped_c = self._slice_long_records(
            cold_candidates, retrieval_query, record_cap_units, cap_chars=slice_chars
        )
        long_dropped.extend(dropped_w)
        long_dropped.extend(dropped_c)

        # 🔥 过滤精排层：对 warm 层候选进行过滤精排
        warm_filtered = self.filtering_ranker.filter_and_rerank(warm_candidates, layer="warm")

        # 按类型分组（保持原有逻辑）
        confirmed = [item for item in warm_filtered if item["kind"] == "fact" and item["status"] == "confirmed"][:CONFIRMED_LIMIT]
        other_warm = [item for item in warm_filtered if item not in confirmed][:WARM_OTHER_LIMIT]
        warm = [*confirmed, *other_warm]
        cold = self.filtering_ranker.filter_and_rerank(cold_candidates, layer="cold")[:COLD_LIMIT] if cold_candidates else []

        # 一跳关联：从直接相关的记录扩展一次，关联记录重新授权。
        seeds = [*warm, *cold][:DIRECT_RECORD_LIMIT]
        expanded_all = self._expand_once(
            scope, seeds, project_id=bound_project, channels=allowed_channels,
            include_semantic=semantic_on,
        )
        expanded_warm = [item for item in expanded_all if item["layer"] == "warm"]
        expanded_cold = [item for item in expanded_all if item["layer"] == "cold"]

        # 近期历史去重（长记忆在过滤之前已经切过片了）
        hot_contents = self._hot_contents(hot)
        expanded_warm, dropped_ew = self._slice_long_records(
            expanded_warm, retrieval_query, record_cap_units, cap_chars=slice_chars
        )
        expanded_cold, dropped_ec = self._slice_long_records(
            expanded_cold, retrieval_query, record_cap_units, cap_chars=slice_chars
        )
        long_dropped.extend(dropped_ew)
        long_dropped.extend(dropped_ec)
        warm, elided_warm = self._dedupe_against_history(warm, hot_contents)
        cold, elided_cold = self._dedupe_against_history(cold, hot_contents)

        budget = DEFAULT_TOKEN_BUDGET if token_budget is None else token_budget
        hot, warm, cold, estimated_tokens = self._apply_token_budget(
            hot, warm, cold, budget, history_intent=history_intent,
        )
        # 关联记录排在直接候选之后，且只能用**剩下的**预算：它们只是补充，
        # 不该挤掉直接相关的内容，也不能不记账就塞进上下文。
        direct_ids = {item["id"] for item in [*warm, *cold]}
        expanded_warm = [item for item in expanded_warm if item["id"] not in direct_ids]
        expanded_cold = [item for item in expanded_cold if item["id"] not in direct_ids]
        _, expanded_warm, expanded_cold, extra_tokens = self._apply_token_budget(
            [], expanded_warm, expanded_cold, max(0, budget - estimated_tokens),
            history_intent=history_intent,
        )
        estimated_tokens += extra_tokens
        self.repositories.touch_records(
            [item["id"] for item in [*hot, *warm, *cold, *expanded_warm, *expanded_cold]],
            accessed_at=datetime.now(UTC).isoformat(timespec="milliseconds"),
        )

        conflict_keys = {item.get("conflict_key") for item in warm if item.get("conflict_key")}
        # 冲突有两种记法：旧记录用 conflict_key 撞出来的，新记录用显式的冲突关系。
        # 只看其中一种，另一种冲突就会被当成确定事实答出去。
        has_conflict = any(item.get("conflict_key") in conflict_keys for item in cold) or any(
            str(item.get("conflict_with") or "").strip() for item in [*warm, *cold]
        )
        citations: list[dict[str, Any]] = []
        if self._wants_citations(query) or has_conflict:
            citations = [
                {"id": item["id"], "created_at": item["created_at"], "kind": item["kind"], "summary": str(item["content"])[:160]}
                for item in [*warm, *cold]
            ]
        result = RetrievalResult(
            hot=hot,
            warm=[*warm, *expanded_warm],
            cold_evidence=[*cold, *expanded_cold],
            citations=citations,
            metadata={
                "estimated_tokens": estimated_tokens,
                "cold_enabled": True,
                "rrf_k": RRF_K,
                "has_conflict": has_conflict,
                "reference_resolved": reference_resolved,
                "model_rewrites": model_rewrites,
                "expanded": len(expanded_warm) + len(expanded_cold),
                "elided_duplicates": [*elided_warm, *elided_cold],
                "dropped_unrelated_long": long_dropped,
            },
        )
        LOGGER.info(
            "检索完成：hot=%d 条 warm=%d 条 cold=%d 条 关联=%d 引用=%d 预估token=%d 指代=%s 改写=%d",
            len(result.hot), len(warm), len(cold), len(expanded_warm) + len(expanded_cold),
            len(result.citations), estimated_tokens, reference_resolved, model_rewrites,
        )
        return result

    @staticmethod
    def _format_memory_line(item: dict[str, Any]) -> str:
        """一条记忆的注入行：正文 + 类型/状态 + 原因 + 发生时间 + 记忆 ID。

        元数据不能被压平：模型得知道这是事实、计划、偏好、假设还是失败经验，
        也得知道结论为什么成立，否则很容易把计划当成已完成的事实。
        """
        try:
            metadata = json.loads(item.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        kind = str(item.get("kind") or "")
        status = str(item.get("content_status") or item.get("status") or "")
        tags = [f"{kind}/{status}" if kind else status]
        if kind == "hypothesis":
            tags.append("未验证")
        if item.get("content_sliced"):
            tags.append("片段")
        # 冲突与待复核必须**写在注入行里**：两条互相矛盾的说法长得都很像结论，
        # 不标出来，模型就会挑一条当确定事实答出去。标了它才知道要说明不确定性。
        if str(item.get("conflict_with") or "").strip():
            tags.append("与他条冲突")
        if str(item.get("review_state") or "") == "pending":
            tags.append("待核对")
        parts = [f"[{'｜'.join(t for t in tags if t)}] {item['content']}"]
        if metadata.get("reason"):
            parts.append(f"原因：{metadata['reason']}")
        if metadata.get("goal"):
            parts.append(f"目的：{metadata['goal']}")
        from .memory_semantic import judgment_of
        judgment = judgment_of(item)
        if judgment:
            parts.append('模型判断（可据新原话纠正）：' + str(judgment.get('reason') or '')[:300])
            parts.append('原文依据：' + json.dumps(judgment.get('quotes') or [],ensure_ascii=False)[:1000])
            if judgment.get('lifetime'):
                parts.append('期限依据：' + json.dumps(judgment['lifetime'],ensure_ascii=False)[:300])
        if item.get("created_at"):
            parts.append(f"发生时间：{item['created_at']}")
        parts.append(f"记忆：{str(item['id'])[:8]}")
        return " ｜ ".join(parts)

    def build_context(
        self,
        scope: Scope,
        query: str,
        *,
        history: list[dict[str, Any]] | None = None,
        project_id: str | None = None,
        include_semantic: bool | None = None,
    ) -> MemoryContext:
        result = self.retrieve(
            scope, query, history=history, project_id=project_id, include_semantic=include_semantic
        )
        confirmed = [item for item in result.warm if item["kind"] == "fact" and item["status"] == "confirmed"]
        candidates = [item for item in result.warm if item["kind"] == "candidate"]
        # 已替代/已否决的方案只在历史里解释，presented 成现行结论会误导判断。
        replaced = [
            item for item in result.warm
            if str(item.get("content_status") or "") in SUPERSEDED_STATUSES
        ]
        summaries = [
            item for item in result.warm
            if item not in confirmed and item not in candidates and item not in replaced
        ]
        sections: list[str] = []
        if any(item.get('kind') in ('decision', 'plan') for item in [*result.warm, *result.cold_evidence]):
            sections.append(
                '[MEMORY_INTERPRETATION]\n'
                '决定、选择和计划类记忆只说明用户的当前决定、选择或计划，不能据此声称已经执行或目前正在执行。'
                '没有实施依据时，实际执行状态保持未知；暂缓或取消的决定也不能证明动作已经停止。'
                '用户问目前怎么决定时，先说明现行决定；已替代记录仅作为历史背景，不能沿用为现行决定。'
            )
        if confirmed:
            sections.append("[CONFIRMED_FACT]\n" + "\n".join(f"- {self._format_memory_line(item)}" for item in confirmed))
        if candidates:
            sections.append("[UNCONFIRMED_CANDIDATE]\n" + "\n".join(f"- {self._format_memory_line(item)}" for item in candidates))
        if summaries:
            sections.append("[WARM_MEMORY]\n" + "\n".join(f"- {self._format_memory_line(item)}" for item in summaries))
        if result.hot:
            sections.append("[RECENT_DIALOGUE]\n" + "\n".join(f"- {item.get('role')}: {item['content']}" for item in result.hot))
        if result.cold_evidence or replaced:
            history_items = [*result.cold_evidence, *replaced]
            sections.append(
                "[HISTORICAL_EVIDENCE]\n"
                + "\n".join(f"- {item['created_at']}: {self._format_memory_line(item)}" for item in history_items)
            )
        if not sections:
            sections.append("[MEMORY_POLICY]\nNo verifiable memory was found; do not invent historical details.")
        return MemoryContext(
            hot=result.hot,
            warm=result.warm,
            cold_evidence=result.cold_evidence,
            injected_text="\n\n".join(sections),
            citations=result.citations,
            metadata=result.metadata,
        )
