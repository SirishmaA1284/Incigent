"""
memory/fake_memory.py
Deterministic in-process memory for tests and offline development ONLY.

It is never used by the application unless a caller passes it explicitly, and
every result it returns is labelled source="fake". It does not imitate
Hindsight's extraction or ranking: recall is plain keyword overlap.
"""

import re

from memory.base import MemoryRecord, MemoryUnavailableError, RecalledMemory

_TOKEN = re.compile(r"[a-z0-9_]{3,}")
_STOPWORDS = {
    "the", "and", "for", "with", "was", "were", "this", "that", "from", "past",
    "incident", "incidents", "service", "services", "failure", "patterns", "symptoms",
    "app", "event", "pattern", "incident-ops", "ops",
}


def _tokens(text: str) -> set:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


class FakeMemory:
    backend = "fake"

    def __init__(self):
        self._records = {}  # document_id -> MemoryRecord (retain replaces, like an upsert)
        self.available = True

    # -- OperationalMemory ----------------------------------------------------
    def retain_event(self, record: MemoryRecord) -> None:
        if not self.available:
            raise MemoryUnavailableError("fake memory set unavailable")
        self._records[record.document_id] = record

    def recall(self, query: str, *, tags: list, limit: int) -> list:
        if not self.available:
            raise MemoryUnavailableError("fake memory set unavailable")
        wanted_tags = set(tags or [])
        query_tokens = _tokens(query)
        scored = []
        for order, record in enumerate(self._records.values()):
            if wanted_tags and not wanted_tags & set(record.tags):
                continue  # like tags_match="any_strict": untagged/unmatched excluded
            doc_tokens = _tokens(record.content) | _tokens(" ".join(t.split(":", 1)[-1] for t in record.tags))
            score = len(query_tokens & doc_tokens)
            if score:
                scored.append((-score, order, record))
        scored.sort(key=lambda s: (s[0], s[1]))
        return [
            RecalledMemory(
                text=record.content, source=self.backend, memory_type="experience",
                incident_id=record.metadata.get("incident_id"),
                event_type=record.metadata.get("event_type"),
                services=record.metadata.get("services"),
                occurred_at=record.metadata.get("occurred_at"),
                tags=tuple(record.tags), score=float(-neg),
            )
            for neg, _, record in scored[:limit]
        ]

    def reflect(self, query: str, *, tags: list) -> str:
        raise NotImplementedError("FakeMemory does not synthesize answers")

    # -- test helpers ---------------------------------------------------------
    def records(self) -> list:
        return list(self._records.values())

    def clear(self) -> None:
        self._records.clear()
