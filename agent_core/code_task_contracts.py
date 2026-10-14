"""Host-owned code task outcomes and approval checkpoints, independent of executor."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


CODE_AGENT_OUTCOME_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "budget_exhausted", "waiting_approval"}
)


@dataclass(frozen=True)
class CodeAgentCheckpoint:
    snapshot_id: str
    messages: tuple[Mapping[str, Any], ...] = ()
    steps_used: int = 0
    tokens_used: int = 0
    original_action: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip():
            raise ValueError("snapshot_id is required")
        if self.steps_used < 0:
            raise ValueError("steps_used cannot be negative")
        if self.tokens_used < 0:
            raise ValueError("tokens_used cannot be negative")


@dataclass(frozen=True)
class CodeAgentOutcome:
    status: str
    summary: str = ""
    output: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Mapping[str, Any], ...] = ()
    verification_status: str = "unverified"
    checkpoint: CodeAgentCheckpoint | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.status not in CODE_AGENT_OUTCOME_STATUSES:
            raise ValueError(f"invalid code agent status: {self.status}")
        if self.verification_status not in {"verified", "unverified", "not_run"}:
            raise ValueError(
                f"invalid verification_status: {self.verification_status}"
            )
