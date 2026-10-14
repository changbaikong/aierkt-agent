"""SQLite adapter over the existing scoped memory module."""

from __future__ import annotations

from typing import Any

from agent_core.contracts import Scope


class SQLiteMemoryStore:
    def __init__(self) -> None:
        from backend import memory

        self._memory = memory

    @staticmethod
    def _scope(scope: Scope):
        from backend.memory import MemoryScope

        return MemoryScope(scope.owner_id, scope.channel, scope.conversation_id)

    def append_message(self, scope: Scope, role: str, content: str, source: str = "user_chat") -> None:
        # The legacy table currently hard-codes ``user_chat`` as its source.
        # Keep the core parameter while adapting to that older signature.
        self._memory.save_raw_message(role, content, self._scope(scope), source=source)

    def recall_hot(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]:
        return list(self._memory.recall_hot(self._scope(scope), user_message))

    def recall_warm(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]:
        return list(self._memory.recall_warm(self._scope(scope), user_message))

    def recall_cold(self, scope: Scope, user_message: str = "") -> list[dict[str, Any]]:
        return list(self._memory._recall_cold_records(self._scope(scope), user_message))

    def save_fact(
        self,
        scope: Scope,
        content: str,
        *,
        category: str = "general",
        importance: int = 2,
        confidence: float = 1.0,
        source_type: str = "user_explicit",
        conflict_key: str = "",
        expires_at: str = "",
    ) -> None:
        self._memory.save_fact(
            content,
            category=category,
            importance=importance,
            scope=self._scope(scope),
            confidence=confidence,
            source_type=source_type,
            conflict_key=conflict_key,
            expires_at=expires_at,
        )

    def save_candidate(
        self,
        scope: Scope,
        content: str,
        *,
        category: str = "general",
        confidence: float = 0.0,
    ) -> int | None:
        return self._memory.save_memory_candidate(
            content,
            category=category,
            confidence=confidence,
            scope=self._scope(scope),
        )

    def save_summary(
        self,
        scope: Scope,
        content: str,
        *,
        confidence: float = 0.75,
        source_type: str = "generated_summary",
        expires_at: str = "",
    ) -> None:
        self._memory.save_summary(
            content,
            confidence=confidence,
            source_type=source_type,
            expires_at=expires_at,
            scope=self._scope(scope),
        )

    def promote_candidate(self, scope: Scope, candidate_id: int) -> bool:
        return bool(self._memory.promote_memory_candidate(candidate_id, self._scope(scope)))

    def reject_candidate(self, scope: Scope, candidate_id: int) -> bool:
        return bool(self._memory.reject_memory_candidate(candidate_id, self._scope(scope)))
