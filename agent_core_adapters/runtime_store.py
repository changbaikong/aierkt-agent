"""Adapter for the existing durable AgentRuntime event reducer."""

from __future__ import annotations

from typing import Any

from agent_core.contracts import Scope
from agent_core.runtime import AgentEvent, AgentState


class LegacyRuntimeStore:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @staticmethod
    def _scope(scope: Scope):
        from backend.memory import MemoryScope

        return MemoryScope(scope.owner_id, scope.channel, scope.conversation_id)

    def state(self, scope: Scope) -> AgentState:
        state = self.runtime.state(self._scope(scope))
        return AgentState(owner_id=state.owner_id, phase=state.phase, active_turn_id=state.active_turn_id, last_event_at=state.last_event_at)

    async def publish(self, event: AgentEvent) -> AgentState:
        from backend.agent_runtime import AgentEvent as LegacyEvent

        state = await self.runtime.publish(
            LegacyEvent(
                event.type,
                self._scope(event.scope),
                event.payload,
                "agent_core",
                event.turn_id,
                event.event_id,
                event.created_at,
            )
        )
        return AgentState(owner_id=state.owner_id, phase=state.phase, active_turn_id=state.active_turn_id, last_event_at=state.last_event_at)
