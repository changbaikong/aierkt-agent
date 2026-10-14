"""Legacy loop budget plus shared Host outcome/checkpoint value objects."""

from dataclasses import dataclass

from ..code_task_contracts import CodeAgentCheckpoint, CodeAgentOutcome


@dataclass(frozen=True)
class CodeAgentBudget:
    max_steps: int = 40
    max_tokens: int = 120_000
    timeout_seconds: int = 30 * 60

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_tokens", "timeout_seconds"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
