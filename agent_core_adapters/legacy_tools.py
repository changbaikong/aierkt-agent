"""Translate legacy ToolDefinition objects into core ToolSpecs."""

from __future__ import annotations

import inspect
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Iterable

from agent_core.contracts import AgentOutcome as CoreOutcome
from agent_core.contracts import AgentRequest, Scope, ToolSpec
from agent_core.loop import AgentLoop as CoreAgentLoop
from agent_core.persistence import SQLiteConfirmationStore, SQLiteRunStore
from agent_core.runner import AgentRunner
from agent_core.tools import ToolRegistry


@dataclass(frozen=True)
class CompatOutcome:
    status: str
    run_id: str
    reply: str = ""
    action_id: str = ""
    confirmation_code: str = ""
    confirmation: dict[str, Any] | None = None


def registry_from_definitions(definitions: Iterable[Any]) -> ToolRegistry:
    """Adapt existing definitions without importing business modules in core."""
    registry = ToolRegistry()
    for definition in definitions:
        async def handler(arguments: dict[str, Any], _scope: Scope, legacy=definition.handler):
            result = legacy(arguments)
            if inspect.isawaitable(result):
                result = await result
            return dict(result or {})

        registry.register(
            ToolSpec(
                name=str(definition.name),
                schema=dict(definition.schema),
                handler=handler,
                risk=str(getattr(definition, "risk", "low")),
                timeout_seconds=float(getattr(definition, "timeout_seconds", 15.0)),
                confirmation_title=str(getattr(definition, "confirmation_title", "Confirmation required")),
            )
        )
    return registry


def build_registry(llm: Any, scope: Scope) -> ToolRegistry:
    """Build the production registry lazily so importing the core stays cheap."""
    from backend.channel_actions import build_agent_tool_definitions
    from backend.memory import MemoryScope

    legacy_scope = MemoryScope(scope.owner_id, scope.channel, scope.conversation_id)
    return registry_from_definitions(build_agent_tool_definitions(llm, legacy_scope))


class CompatAgentLoop:
    """Preserve the old backend AgentLoop surface while using agent_core."""

    def __init__(self, llm: Any, definitions: Iterable[Any], scope: Any, *, db_path: Path | None = None, run_id: str | None = None) -> None:
        self.llm = llm
        self.scope = Scope(scope.owner_id, scope.channel, scope.conversation_id)
        self.db_path = Path(db_path) if db_path is not None else self._default_db_path()
        self._core = CoreAgentLoop(
            _LegacyProvider(llm),
            registry_from_definitions(definitions),
            self.scope,
            run_store=SQLiteRunStore(self.db_path),
            confirmation_store=SQLiteConfirmationStore(self.db_path),
            run_id=run_id,
        )
        self._runner = AgentRunner(None, None)

    @staticmethod
    def _default_db_path() -> Path:
        from backend.memory import DB_PATH

        return Path(DB_PATH)

    @staticmethod
    def _to_legacy_outcome(outcome: CoreOutcome):
        confirmation = outcome.confirmation.as_dict() if outcome.confirmation is not None else None
        return CompatOutcome(
            outcome.status,
            outcome.run_id,
            reply=outcome.reply,
            action_id=outcome.action_id,
            confirmation_code=outcome.confirmation_code,
            confirmation=confirmation,
        )

    async def run(self, user_text: str, *, ephemeral_suffix: str = ""):
        outcome = await self._runner.run_internal(
            self._core,
            AgentRequest(
                user_text,
                scope=self.scope,
                history=list(getattr(self.llm, "messages", [])),
                ephemeral_suffix=ephemeral_suffix,
            ),
        )
        self.llm.messages = list(self._core.public_messages)
        return self._to_legacy_outcome(outcome)

    async def resume(self, action_id: str, decision: str):
        outcome = await self._runner.resume_internal(self._core, action_id, decision)
        self.llm.messages = list(self._core.public_messages)
        return self._to_legacy_outcome(outcome)

    @classmethod
    def pending_by_code(cls, code: str, scope: Any, *, db_path: Path | None = None) -> dict[str, Any] | None:
        core_scope = Scope(scope.owner_id, scope.channel, scope.conversation_id)
        path = Path(db_path) if db_path is not None else cls._default_db_path()
        pending = SQLiteConfirmationStore(path).pending_by_code(code, core_scope)
        return pending.as_dict() if pending is not None else None

    @classmethod
    def pending_for_scope(cls, scope: Any, *, db_path: Path | None = None) -> list[dict[str, Any]]:
        core_scope = Scope(scope.owner_id, scope.channel, scope.conversation_id)
        path = Path(db_path) if db_path is not None else cls._default_db_path()
        store = SQLiteConfirmationStore(path)
        return [item.as_dict() for item in store.pending_for_scope(core_scope)]

    @classmethod
    async def resume_persisted(cls, action_id: str, decision: str, scope: Any, *, db_path: Path | None = None):
        from backend.channel_actions import build_agent_tool_definitions
        from backend.llm.client import LLMClient
        from backend.memory import MemoryScope

        core_scope = Scope(scope.owner_id, scope.channel, scope.conversation_id)
        path = Path(db_path) if db_path is not None else cls._default_db_path()
        pending = SQLiteConfirmationStore(path).get(action_id, core_scope)
        if pending is None:
            return CompatOutcome("failed", "", reply="Confirmation request not found.")
        llm = LLMClient()
        definitions = build_agent_tool_definitions(llm, MemoryScope(scope.owner_id, scope.channel, scope.conversation_id))
        return await cls(llm, definitions, scope, db_path=path, run_id=pending.run_id).resume(action_id, decision)


class _LegacyProvider:
    def __init__(self, llm: Any) -> None:
        self.llm = llm

    @property
    def max_tokens(self) -> int:
        try:
            return int(self.llm.max_tokens)
        except (AttributeError, TypeError, ValueError):
            return 2048

    async def complete(self, messages, *, tools=None, max_tokens=None, temperature=None):
        response = await self.llm.complete(messages, tools=tools, max_tokens=max_tokens, temperature=temperature)
        from agent_core.contracts import ProviderResponse

        return ProviderResponse(dict(response.get("message") or {}), dict(response.get("usage") or {}))
