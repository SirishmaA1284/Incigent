"""
tests/test_rag.py — Current-incident evidence index: event IDs flow from the
timeline into Chroma chunks and back out of retrieval. No OpenAI calls.
"""

from datetime import datetime, timedelta

import rag.embedder as embedder
import rag.retriever as retriever
from rag.embedder import chunk_timeline


def _events(minutes):
    start = datetime(2024, 3, 15, 2, 0, 0)
    return [
        {"event_id": i, "timestamp": start + timedelta(minutes=m),
         "timestamp_str": (start + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"),
         "source": "log", "content": f"event {i}"}
        for i, m in enumerate(minutes)
    ]


class FakeCollection:
    def __init__(self, query_result=None):
        self.calls = []
        self.query_result = query_result

    def delete(self, **kwargs):
        self.calls.append(("delete", kwargs))

    def add(self, **kwargs):
        self.calls.append(("add", kwargs))

    def query(self, **kwargs):
        self.calls.append(("query", kwargs))
        return self.query_result


class TestChunkTimeline:
    def test_chunks_carry_event_ids_in_text_and_metadata(self):
        chunks = chunk_timeline(_events([0, 1, 2, 12, 13]), "INC-1")
        assert len(chunks) == 2
        text0, meta0 = chunks[0]
        assert text0.splitlines()[0] == "[E0] [2024-03-15 02:00:00] [LOG] event 0"
        assert meta0 == {"incident_id": "INC-1", "chunk_index": 0, "event_ids": "0,1,2"}
        assert chunks[1][1]["event_ids"] == "3,4"

    def test_empty_timeline(self):
        assert chunk_timeline([], "INC-1") == []


class TestEmbedTimeline:
    def test_replaces_previous_chunks_for_the_same_incident(self, monkeypatch):
        fake = FakeCollection()
        monkeypatch.setattr(embedder, "get_or_create_collection", lambda: fake)
        embedder.embed_timeline(_events([0, 12]), "INC-1")
        assert fake.calls[0] == ("delete", {"where": {"incident_id": "INC-1"}})
        adds = [kw for op, kw in fake.calls if op == "add"]
        assert [a["ids"] for a in adds] == [["INC-1_chunk_0"], ["INC-1_chunk_1"]]
        assert adds[0]["metadatas"][0]["event_ids"] == "0"


class TestRetrieveEvidence:
    def test_returns_event_ids_and_filters_by_incident(self, monkeypatch):
        fake = FakeCollection({
            "documents": [["[E3] a\n[E4] b", "[E9] c"]],
            "metadatas": [[{"incident_id": "INC-1", "chunk_index": 1, "event_ids": "3,4"},
                           {"incident_id": "INC-1", "chunk_index": 2}]],
        })
        monkeypatch.setattr(retriever, "get_or_create_collection", lambda: fake)
        evidence = retriever.retrieve_evidence("pool", "INC-1")
        assert fake.calls[0][1]["where"] == {"incident_id": "INC-1"}
        assert evidence[0]["event_ids"] == [3, 4]
        assert evidence[0]["text"] == "[E3] a\n[E4] b"
        # Chunks indexed before event IDs were stored still work.
        assert evidence[1]["event_ids"] == []
