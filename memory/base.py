"""
memory/base.py
Application-level types for long-term operational memory.

Long-term memory is advisory context only: it never authorizes actions and is
not an approval or audit store. Current-incident evidence lives in Chroma
(rag/), not here.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol

EVENT_TYPES = (
    "incident",
    "evidence_digest",
    "diagnosis",
    "recommendation",
    "human_decision",
    "action",
    "verification",
    "lesson",
)

# Every record this application retains carries this tag, so recall can be
# restricted to our operational memories even in a shared bank.
APP_TAG = "app:incident-ops"


class MemoryUnavailableError(Exception):
    """The memory backend could not be reached or rejected the request."""


@dataclass(frozen=True)
class MemoryEvent:
    """One workflow milestone for an incident, before redaction."""
    event_type: str
    incident_id: str
    summary: str
    occurred_at: Optional[datetime] = None
    services: tuple = ()
    pattern_tags: tuple = ()
    details: dict = field(default_factory=dict)
    # Distinguishes several events of the same type for one incident
    # (e.g. decisions on two different recommendations).
    key: str = "0"

    def __post_init__(self):
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown memory event type: {self.event_type!r}")
        if not str(self.incident_id).strip():
            raise ValueError("incident_id is required")


@dataclass(frozen=True)
class MemoryRecord:
    """What a backend stores: already rendered and redacted."""
    document_id: str
    content: str
    context: str
    occurred_at: Optional[datetime]
    metadata: dict
    tags: tuple


@dataclass(frozen=True)
class RecalledMemory:
    """A normalized recall hit. Never a raw backend object."""
    text: str
    source: str                       # "hindsight" or "fake"
    memory_type: Optional[str] = None
    incident_id: Optional[str] = None
    event_type: Optional[str] = None
    services: Optional[str] = None
    occurred_at: Optional[str] = None
    tags: tuple = ()
    score: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "text": self.text, "source": self.source, "memory_type": self.memory_type,
            "incident_id": self.incident_id, "event_type": self.event_type,
            "services": self.services, "occurred_at": self.occurred_at,
            "tags": list(self.tags), "score": self.score,
        }


class OperationalMemory(Protocol):
    """Backend interface. Only implementations know backend-specific APIs."""

    backend: str  # "hindsight" or "fake"

    def retain_event(self, record: MemoryRecord) -> None:
        """Store one record. Raises MemoryUnavailableError on failure."""

    def recall(self, query: str, *, tags: list, limit: int) -> list:
        """Return RecalledMemory items. Raises MemoryUnavailableError on failure."""

    def reflect(self, query: str, *, tags: list) -> str:
        """Synthesize an answer over memories. Not used by the workflow yet."""
