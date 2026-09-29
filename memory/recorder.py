"""
memory/recorder.py
Application-facing entry point for long-term operational memory.

MemoryRecorder renders workflow milestones into memory records, redacts them,
adds consistent metadata/tags, and calls an OperationalMemory backend. Backend
failures are logged and kept for retry; they never fail incident analysis.

Recalled memory is advisory context. Nothing here returns, stores or checks an
authorization: the recorder cannot approve or trigger any action.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from memory.base import APP_TAG, MemoryEvent, MemoryRecord, MemoryUnavailableError
from memory.redact import redact, redact_mapping

log = logging.getLogger(__name__)

DOCUMENT_PREFIX = "incident-ops"


@dataclass(frozen=True)
class RetainOutcome:
    event_type: str
    document_id: str
    status: str             # "retained" | "failed" | "disabled"
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {"event_type": self.event_type, "document_id": self.document_id,
                "status": self.status, "error": self.error}


@dataclass(frozen=True)
class RecallOutcome:
    status: str             # "available" | "unavailable" | "disabled"
    backend: str            # "hindsight" | "fake" | "none"
    query: str
    memories: tuple = ()
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {"status": self.status, "backend": self.backend, "query": self.query,
                "memories": [m.to_dict() for m in self.memories], "error": self.error}


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    # Timeline timestamps are naive; they are treated as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def render_event(event: MemoryEvent) -> str:
    """Plain-language text for the memory backend to extract facts from."""
    when = _utc(event.occurred_at)
    head = f"Incident {event.incident_id}"
    if event.services:
        head += f" (service: {', '.join(event.services)})"
    if when:
        head += f" at {when.isoformat()}"
    lines = [f"{head} — {event.event_type.replace('_', ' ').upper()}: {event.summary}"]
    for key, value in event.details.items():
        if value not in (None, ""):
            lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    if event.pattern_tags:
        lines.append(f"Failure patterns: {', '.join(event.pattern_tags)}")
    return "\n".join(lines)


def build_record(event: MemoryEvent) -> MemoryRecord:
    """Normalize + redact an event into what a backend stores."""
    when = _utc(event.occurred_at)
    metadata = {
        "app": APP_TAG.split(":", 1)[1],
        "incident_id": event.incident_id,
        "event_type": event.event_type,
        "services": ",".join(event.services),
        "pattern_tags": ",".join(event.pattern_tags),
        "occurred_at": when.isoformat() if when else "",
    }
    metadata.update(redact_mapping({f"detail_{k}": v for k, v in event.details.items() if v not in (None, "")}))
    tags = [APP_TAG, f"event:{event.event_type}", f"incident:{event.incident_id}"]
    tags += [f"service:{s}" for s in event.services]
    tags += [f"pattern:{p}" for p in event.pattern_tags]
    return MemoryRecord(
        # Stable per logical event: re-recording the same milestone replaces it.
        document_id=f"{DOCUMENT_PREFIX}:{event.incident_id}:{event.event_type}:{event.key}",
        content=redact(render_event(event)),
        context=f"incident response: {event.event_type.replace('_', ' ')}",
        occurred_at=when,
        metadata={k: redact(str(v)) for k, v in metadata.items()},
        tags=tuple(redact(t) for t in tags),
    )


class MemoryRecorder:
    def __init__(self, memory=None, disabled_reason: Optional[str] = None):
        self.memory = memory
        self.disabled_reason = disabled_reason or ("long-term memory is not configured" if memory is None else None)
        self.pending = []  # records whose retain failed, kept (process-local) for retry

    @property
    def backend(self) -> str:
        return getattr(self.memory, "backend", "none") if self.memory is not None else "none"

    # -- retain ---------------------------------------------------------------
    def record(self, event: MemoryEvent) -> RetainOutcome:
        record = build_record(event)
        if self.memory is None:
            return RetainOutcome(event.event_type, record.document_id, "disabled", self.disabled_reason)
        try:
            self.memory.retain_event(record)
        except Exception as e:  # never fail the analysis because memory failed
            log.warning("memory retain failed for %s: %s: %s", record.document_id, type(e).__name__, e)
            self.pending.append(record)
            return RetainOutcome(event.event_type, record.document_id, "failed", f"{type(e).__name__}: {e}")
        return RetainOutcome(event.event_type, record.document_id, "retained")

    def retry_pending(self) -> int:
        """Re-send failed records. Returns how many succeeded."""
        if self.memory is None:
            return 0
        still_failing, sent = [], 0
        for record in self.pending:
            try:
                self.memory.retain_event(record)
                sent += 1
            except Exception as e:
                log.warning("memory retain retry failed for %s: %s", record.document_id, e)
                still_failing.append(record)
        self.pending = still_failing
        return sent

    # Milestone helpers. Each maps one workflow step to one memory event.
    def record_incident(self, incident_id, summary, *, occurred_at=None, services=(), pattern_tags=(), details=None):
        return self.record(MemoryEvent("incident", incident_id, summary, occurred_at,
                                       tuple(services), tuple(pattern_tags), dict(details or {})))

    def record_evidence_digest(self, incident_id, summary, *, occurred_at=None, services=(), pattern_tags=(), details=None):
        return self.record(MemoryEvent("evidence_digest", incident_id, summary, occurred_at,
                                       tuple(services), tuple(pattern_tags), dict(details or {})))

    def record_diagnosis(self, incident_id, root_cause, *, confidence, source, occurred_at=None,
                         services=(), pattern_tags=(), details=None):
        """For diagnoses a human has validated. The automatic analysis path does
        not call this: unverified LLM diagnoses are not long-term memory."""
        d = {"confidence": confidence, "diagnosed_by": source, **(details or {})}
        return self.record(MemoryEvent("diagnosis", incident_id, root_cause, occurred_at,
                                       tuple(services), tuple(pattern_tags), d))

    def record_recommendation(self, incident_id, recommendation, *, recommendation_id, source,
                              occurred_at=None, services=(), pattern_tags=(), details=None):
        """For recommendations recorded alongside a human decision on them. The
        automatic analysis path does not call this."""
        d = {"recommendation_id": recommendation_id, "proposed_by": source,
             "status": "proposed, not approved", **(details or {})}
        return self.record(MemoryEvent("recommendation", incident_id, recommendation, occurred_at,
                                       tuple(services), tuple(pattern_tags), d, key=str(recommendation_id)))

    def record_human_decision(self, incident_id, *, recommendation_id, recommendation, decision,
                              decided_by, reason, alternative=None, occurred_at=None,
                              services=(), pattern_tags=()):
        """
        Record a decision a human actually made. There is no approval workflow
        yet, so nothing in the application calls this; it exists so the future
        approval step can record approvals AND rejections (with reasons).
        """
        decision = str(decision).lower()
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        if not str(decided_by).strip() or not str(reason).strip():
            raise ValueError("a human decision needs decided_by and a reason")
        summary = f"Recommendation '{recommendation}' was {decision.upper()} by {decided_by}."
        d = {"recommendation_id": recommendation_id, "decision": decision, "decided_by": decided_by,
             "reason": reason, "alternative_selected": alternative}
        return self.record(MemoryEvent("human_decision", incident_id, summary, occurred_at,
                                       tuple(services), tuple(pattern_tags), d, key=str(recommendation_id)))

    def record_action(self, incident_id, action, *, action_id, result, performed_by, occurred_at=None,
                      services=(), pattern_tags=()):
        d = {"action_id": action_id, "result": result, "performed_by": performed_by}
        return self.record(MemoryEvent("action", incident_id, action, occurred_at,
                                       tuple(services), tuple(pattern_tags), d, key=str(action_id)))

    def record_verification(self, incident_id, outcome, *, check_id, recovered, occurred_at=None,
                            services=(), pattern_tags=(), details=None):
        d = {"check_id": check_id, "recovered": "yes" if recovered else "no", **(details or {})}
        return self.record(MemoryEvent("verification", incident_id, outcome, occurred_at,
                                       tuple(services), tuple(pattern_tags), d, key=str(check_id)))

    def record_lesson(self, incident_id, lesson, *, lesson_id, source, occurred_at=None,
                      services=(), pattern_tags=()):
        d = {"lesson_id": lesson_id, "source": source}
        return self.record(MemoryEvent("lesson", incident_id, lesson, occurred_at,
                                       tuple(services), tuple(pattern_tags), d, key=str(lesson_id)))

    # -- recall ---------------------------------------------------------------
    def recall_for_incident(self, incident_id: str, query: str, *, limit: int = 8) -> RecallOutcome:
        """
        Recall prior operational experience relevant to incident_id.
        Memories recorded for incident_id itself are excluded: history means
        other incidents. The incident_id comes from application state.
        """
        query = redact(query)
        if self.memory is None:
            return RecallOutcome("disabled", "none", query, error=self.disabled_reason)
        try:
            hits = self.memory.recall(query, tags=[APP_TAG], limit=limit + 20)
        except Exception as e:
            log.warning("memory recall failed: %s: %s", type(e).__name__, e)
            return RecallOutcome("unavailable", self.backend, query, error=f"{type(e).__name__}: {e}")
        history = tuple(m for m in hits if m.incident_id != incident_id)[:limit]
        return RecallOutcome("available", self.backend, query, history)
