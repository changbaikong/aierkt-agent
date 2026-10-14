"""Provider activity: the durable "what is the provider doing right now" projection.

Why this module exists
----------------------
Before the ACP bridge, a Work attempt only had a status (``running`` /
``completed`` / ...). That is enough for a provider the Host fully controls
(``LocalWorkerProvider``), because the Host can describe the attempt from its own
state. It is **not** enough for a provider that runs an external agent process:
the interesting facts (session id, which tool is running, why a permission is
pending, whether the process died, whether the result can be trusted) live on the
other side of the bridge and must survive a Host restart.

So the provider publishes an :class:`ProviderActivity` snapshot as an ordinary
Work event (``provider_activity``). ``WorkHost`` persists the projection in the
ledger and the Web page re-reads it — the same "persist first, broadcast a hint
second" rule the rest of the Work surface already follows.

Two rules are enforced *here*, because both are easy to get wrong per provider:

1. ``status`` and ``confidence`` come from closed vocabularies. A typo would
   otherwise show up only as a blank label in the UI.
2. ``elapsed_ms`` is computed from ``started_at``/``finished_at``, never
   accumulated by the caller. A caller-accumulated counter drifts the moment an
   event is dropped, and after a restart there is no counter at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

#: Event type a provider uses to publish an activity snapshot. ``WorkHost``
#: special-cases exactly this string when persisting events.
PROVIDER_ACTIVITY_EVENT_TYPE = "provider_activity"

#: Task states a provider can be in, as shown on the Web work page.
#:
#: ``cancelled`` is one step past ``cancelling``: the cancel was *confirmed* by
#: the agent. Collapsing the two would leave the page showing 「取消中」 forever
#: for a task that is demonstrably finished.
PROVIDER_ACTIVITY_STATUSES = frozenset(
    {
        "connecting",
        "thinking",
        "tool_running",
        "waiting_permission",
        "cancelling",
        "cancelled",
        "completed",
        "failed",
        "disconnected",
        "unknown_result",
    }
)

#: Statuses after which no further event is expected for the attempt.
TERMINAL_PROVIDER_ACTIVITY_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "disconnected", "unknown_result"}
)

#: How much the Host trusts the reported result.
PROVIDER_ACTIVITY_CONFIDENCE = frozenset({"confirmed", "assumed", "unknown"})

#: Display names. The UI falls back to the raw provider id for unknown providers.
PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "deepseek_harness": "DeepSeek Harness",
    "code-agent": "现有代码代理",
    "local-worker": "本机任务执行器",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def elapsed_ms(
    started_at: Any,
    finished_at: Any = None,
    *,
    now: datetime | None = None,
) -> int:
    """Milliseconds between two ISO timestamps; 0 when either end is unknown.

    ``finished_at`` empty means "still running": the elapsed value then grows on
    every read, which is what the page wants. Negative values are clamped to 0
    instead of raising, because a clock adjustment is not a task failure.
    """
    start = _parse(started_at)
    if start is None:
        return 0
    end = _parse(finished_at) or now or _now()
    delta = (end - start).total_seconds() * 1000.0
    return max(0, int(delta))


@dataclass(frozen=True)
class ProviderActivity:
    """One attempt's provider-side state, as persisted and shown to the user."""

    attempt_id: str
    provider_id: str
    status: str
    provider_name: str = ""
    session_id: str | None = None
    step: str | None = None
    tool: str | None = None
    waiting_reason: str | None = None
    error: str | None = None
    disconnect_reason: str | None = None
    confidence: str = "unknown"
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    elapsed_ms: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in PROVIDER_ACTIVITY_STATUSES:
            raise ValueError(f"invalid provider activity status: {self.status}")
        if self.confidence not in PROVIDER_ACTIVITY_CONFIDENCE:
            raise ValueError(f"invalid provider activity confidence: {self.confidence}")
        if not str(self.provider_id).strip():
            raise ValueError("provider activity requires a provider id")

    @property
    def provider_label(self) -> str:
        return self.provider_name or PROVIDER_DISPLAY_NAMES.get(
            self.provider_id, self.provider_id
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_PROVIDER_ACTIVITY_STATUSES

    @property
    def unknown_result(self) -> bool:
        return self.status == "unknown_result"

    def with_changes(self, **changes: Any) -> "ProviderActivity":
        """Return a copy with ``updated_at`` refreshed unless the caller set it."""
        if "updated_at" not in changes:
            changes["updated_at"] = _iso(_now())
        return replace(self, **changes)

    def as_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Serializable form: this is exactly what the API and UI consume."""
        return {
            "attempt_id": self.attempt_id,
            "provider_id": self.provider_id,
            "provider_name": self.provider_label,
            "session_id": self.session_id,
            "status": self.status,
            "step": self.step,
            "tool": self.tool,
            "waiting_reason": self.waiting_reason,
            "error": self.error,
            "disconnect_reason": self.disconnect_reason,
            "confidence": self.confidence,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_event_at": self.updated_at,
            "finished_at": self.finished_at,
            "elapsed_ms": elapsed_ms(self.started_at, self.finished_at, now=now),
            "details": dict(self.details or {}),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ProviderActivity | None":
        """Rebuild a snapshot from persisted JSON; malformed rows return None.

        Being strict here would mean a single bad row can take down a task
        detail page. The projection is observational, so "unreadable" is
        reported as "no projection".
        """
        if not isinstance(payload, Mapping):
            return None
        try:
            return cls(
                attempt_id=str(payload.get("attempt_id") or ""),
                provider_id=str(payload.get("provider_id") or ""),
                status=str(payload.get("status") or ""),
                provider_name=str(payload.get("provider_name") or ""),
                session_id=_optional_text(payload.get("session_id")),
                step=_optional_text(payload.get("step")),
                tool=_optional_text(payload.get("tool")),
                waiting_reason=_optional_text(payload.get("waiting_reason")),
                error=_optional_text(payload.get("error")),
                disconnect_reason=_optional_text(payload.get("disconnect_reason")),
                confidence=str(payload.get("confidence") or "unknown"),
                started_at=_optional_text(payload.get("started_at")),
                updated_at=_optional_text(payload.get("updated_at")),
                finished_at=_optional_text(payload.get("finished_at")),
                elapsed_ms=int(payload.get("elapsed_ms") or 0),
                details=dict(payload.get("details") or {}),
            )
        except (TypeError, ValueError):
            return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
