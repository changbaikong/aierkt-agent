"""Business-neutral memory layers and context assembly."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from .contracts import Scope


class MemoryLayer:
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass
class MemoryContext:
    hot: list[dict[str, Any]] = field(default_factory=list)
    warm: list[dict[str, Any]] = field(default_factory=list)
    cold_evidence: list[dict[str, Any]] = field(default_factory=list)
    injected_text: str = ""
    dropped_items: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class MemoryStore(Protocol):
    def append_message(self, scope: Scope, role: str, content: str, source: str = "user_chat") -> None: ...
    def recall_hot(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]: ...
    def recall_warm(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]: ...
    def recall_cold(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]: ...
    def save_fact(self, scope: Scope, content: str, **kwargs: Any) -> None: ...
    def save_summary(self, scope: Scope, content: str, **kwargs: Any) -> None: ...
    def save_candidate(self, scope: Scope, content: str, **kwargs: Any) -> int | None: ...
    def promote_candidate(self, scope: Scope, candidate_id: int) -> bool: ...
    def reject_candidate(self, scope: Scope, candidate_id: int) -> bool: ...


def _should_recall_cold(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ("以前", "过去", "上次", "之前", "历史", "聊过", "记得", "previous", "history"))


class MemoryAssembler:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    @staticmethod
    def _dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result = []
        for record in records:
            key = " ".join(str(record.get("content") or "").split()).casefold()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(record)
        return result

    def assemble(self, scope: Scope, user_message: str = "") -> MemoryContext:
        hot = self.store.recall_hot(scope, user_message)
        raw_warm = self._dedupe(self.store.recall_warm(scope, user_message))
        now = datetime.now()
        dropped = []
        warm = []
        for item in raw_warm:
            expires_at = str(item.get("expires_at") or "").strip()
            if expires_at:
                try:
                    if datetime.fromisoformat(expires_at) <= now:
                        dropped.append(item)
                        continue
                except ValueError:
                    pass
            warm.append(item)

        conflicts: list[dict[str, Any]] = []
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in warm:
            key = str(item.get("conflict_key") or "").strip()
            if key and item.get("is_fact"):
                grouped.setdefault(key, []).append(item)
        for key, records in grouped.items():
            if len(records) < 2:
                continue
            current = records[-1]
            historical = records[:-1]
            warm = [item for item in warm if item not in historical]
            conflicts.append({"key": key, "current": current, "historical": historical})
        cold = self.store.recall_cold(scope, user_message) if _should_recall_cold(user_message) else []
        injected: list[str] = []
        confirmed = [
            item for item in warm
            if item.get("is_fact")
            and float(item.get("confidence") or 0) >= 0.85
            and item.get("source_type") in {"user_explicit", "candidate_confirmed"}
        ]
        unconfirmed = [item for item in warm if item.get("is_fact") and item not in confirmed]
        summaries = [item for item in warm if not item.get("is_fact")]
        if confirmed:
            injected.append("[USER_CONFIRMED_FACT]\n" + "\n".join(f"- {item['content']}" for item in confirmed[:8]))
        if unconfirmed:
            injected.append("[UNCONFIRMED_CANDIDATE]\n" + "\n".join(f"- {item['content']}" for item in unconfirmed[:5]))
        if summaries:
            injected.append("[RECENT_SUMMARY]\n" + "\n".join(f"- {item['content']}" for item in summaries[:3]))
        if hot:
            injected.append("[RECENT_DIALOGUE]\n" + "\n".join(f"- {item.get('role')}: {item['content']}" for item in hot))
        if cold:
            injected.append(
                "[RETRIEVED_EVIDENCE]\n"
                "These records prove only that something appeared in history.\n"
                + "\n".join(f"- {item.get('created_at', '')}: {item['content']}" for item in cold[:5])
            )
        if conflicts:
            injected.append(
                "[MEMORY_CONFLICT]\n"
                "Prefer the latest explicit statement; older records are historical only.\n"
                + "\n".join(
                    f"- current: {item['current']['content']}\n- historical: "
                    + "; ".join(str(old.get("content") or "") for old in item["historical"])
                    for item in conflicts[:3]
                )
            )
        if not hot and not warm and not cold:
            injected.append("[MEMORY_POLICY]\nNo verifiable memory was found; do not invent historical details.")
        return MemoryContext(
            hot=hot,
            warm=warm,
            cold_evidence=cold,
            injected_text="\n\n".join(injected),
            dropped_items=dropped + [item for item in warm if item.get("is_fact") and float(item.get("confidence") or 0) < 0.85],
            conflicts=conflicts,
            metadata={"layer_counts": {"hot": len(hot), "warm": len(warm), "cold": len(cold)}},
        )


class InMemoryMemoryStore:
    """Small deterministic store used by the core contract tests and examples."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.facts: list[dict[str, Any]] = []
        self.summaries: list[dict[str, Any]] = []
        self.candidates: list[dict[str, Any]] = []
        self._candidate_id = 0
        self._sequence = 0

    def append_message(self, scope: Scope, role: str, content: str, source: str = "user_chat") -> None:
        self._sequence += 1
        self.messages.append({"scope": scope, "role": role, "content": content, "source": source, "created_at": str(self._sequence)})

    def save_fact(self, scope: Scope, content: str, *, confidence: float = 1.0, source_type: str = "user_explicit", conflict_key: str = "", expires_at: str = "") -> None:
        self._sequence += 1
        self.facts.append({
            "scope": scope,
            "layer": MemoryLayer.WARM,
            "source": "facts",
            "content": content,
            "confidence": confidence,
            "source_type": source_type,
            "conflict_key": conflict_key,
            "expires_at": expires_at,
            "created_at": str(self._sequence),
            "is_fact": True,
        })

    def save_candidate(self, scope: Scope, content: str, *, confidence: float = 0.0, category: str = "general") -> int:
        self._candidate_id += 1
        self.candidates.append({"id": self._candidate_id, "scope": scope, "content": content, "confidence": confidence, "category": category, "status": "pending"})
        return self._candidate_id

    def save_summary(self, scope: Scope, content: str, *, confidence: float = 0.75, source_type: str = "generated_summary", expires_at: str = "") -> None:
        self._sequence += 1
        self.summaries.append({
            "scope": scope,
            "layer": MemoryLayer.WARM,
            "source": "summaries",
            "content": content,
            "confidence": confidence,
            "source_type": source_type,
            "expires_at": expires_at,
            "created_at": str(self._sequence),
            "is_fact": False,
        })

    def promote_candidate(self, scope: Scope, candidate_id: int) -> bool:
        candidate = next((item for item in self.candidates if item["id"] == candidate_id and item["scope"] == scope and item["status"] == "pending"), None)
        if candidate is None:
            return False
        candidate["status"] = "promoted"
        self.save_fact(scope, candidate["content"], confidence=1.0, source_type="candidate_confirmed")
        return True

    def reject_candidate(self, scope: Scope, candidate_id: int) -> bool:
        candidate = next((item for item in self.candidates if item["id"] == candidate_id and item["scope"] == scope and item["status"] == "pending"), None)
        if candidate is None:
            return False
        candidate["status"] = "rejected"
        return True

    def recall_hot(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]:
        return [
            {"layer": MemoryLayer.HOT, "role": item["role"], "content": item["content"], "created_at": item["created_at"], "is_fact": False}
            for item in self.messages
            if item["scope"].owner_id == scope.owner_id
            and item["scope"].channel == scope.channel
            and item["scope"].conversation_id == scope.conversation_id
        ]

    def recall_warm(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]:
        return [item for item in self.facts if item["scope"].owner_id == scope.owner_id and item["scope"].channel == scope.channel] + [
            item for item in self.summaries if item["scope"].owner_id == scope.owner_id and item["scope"].channel == scope.channel
        ]

    def recall_cold(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]:
        return [
            {"layer": MemoryLayer.COLD, "content": item["content"], "created_at": item["created_at"], "evidence_only": True}
            for item in self.messages
            if item["scope"].owner_id == scope.owner_id and item["scope"].channel == scope.channel
        ][-20:]
