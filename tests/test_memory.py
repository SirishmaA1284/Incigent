"""
tests/test_memory.py — Long-term operational memory: redaction, FakeMemory,
MemoryRecorder and the Hindsight adapter (with a stand-in client; no network).
"""

import asyncio
import inspect
import logging
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from memory import APP_TAG, MemoryEvent, MemoryRecorder, MemoryUnavailableError
from memory.fake_memory import FakeMemory
from memory.hindsight_memory import HindsightMemory, normalize_result, recall_kwargs, retain_kwargs
from memory.recorder import build_record
from memory.redact import REDACTED, redact, redact_mapping


# ── Redaction ────────────────────────────────────────────────────────────────

class TestRedaction:
    @pytest.mark.parametrize("text, secret", [
        ("using key sk-proj-abcdefghijklmnop1234", "sk-proj-abcdefghijklmnop1234"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIz", "eyJhbGciOiJIUzI1NiJ9"),
        ("curl -H 'Authorization: Bearer abc123def456ghi789'", "abc123def456ghi789"),
        ("aws AKIAABCDEFGHIJKLMNOP leaked", "AKIAABCDEFGHIJKLMNOP"),
        ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghijklmnopqrstuvwxyz0123"),
        ("slack xoxb-1234567890-abcdefghij", "xoxb-1234567890-abcdefghij"),
        ("DB_PASSWORD=hunter2 service=orders", "hunter2"),
        ('{"api_key": "abc-123-secret"}', "abc-123-secret"),
        ("client_secret: s3cr3tvalue", "s3cr3tvalue"),
        ("postgres://orders:p4ssw0rd@db-primary:5432/orders", "p4ssw0rd"),
        ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ])
    def test_obvious_secrets_are_removed(self, text, secret):
        out = redact(text)
        assert secret not in out
        assert REDACTED in out

    def test_ordinary_incident_text_is_unchanged(self):
        text = "Connection pool exhausted: 200/200 connections active on db-primary (token bucket full)"
        assert redact(text) == text

    def test_redact_mapping_hides_values_of_sensitive_keys(self):
        out = redact_mapping({"password": "x", "reason": "peak traffic", "note": "api_key=abc12345"})
        assert out == {"password": REDACTED, "reason": "peak traffic", "note": f"api_key={REDACTED}"}


# ── FakeMemory ───────────────────────────────────────────────────────────────

def _record(incident_id, event_type, text, key="0", tags=None):
    return build_record(MemoryEvent(event_type, incident_id, text, key=key,
                                    pattern_tags=tuple(tags or ())))


class TestFakeMemory:
    def test_retain_recall_inspect_clear(self):
        mem = FakeMemory()
        mem.retain_event(_record("INC-1", "incident", "connection pool exhausted", tags=["DB_POOL_EXHAUSTED"]))
        mem.retain_event(_record("INC-2", "incident", "out of memory kill", tags=["OOM_KILL"]))
        hits = mem.recall("pool exhausted DB_POOL_EXHAUSTED", tags=[APP_TAG], limit=5)
        assert [h.incident_id for h in hits] == ["INC-1"]
        assert hits[0].source == "fake"
        assert len(mem.records()) == 2
        mem.clear()
        assert mem.records() == []

    def test_same_document_id_replaces(self):
        mem = FakeMemory()
        mem.retain_event(_record("INC-1", "diagnosis", "first guess"))
        mem.retain_event(_record("INC-1", "diagnosis", "revised diagnosis"))
        assert len(mem.records()) == 1
        assert "revised diagnosis" in mem.records()[0].content

    def test_tag_filter_excludes_other_apps(self):
        mem = FakeMemory()
        rec = _record("INC-1", "incident", "connection pool exhausted")
        mem.retain_event(rec.__class__(**{**rec.__dict__, "tags": ("app:other",)}))
        assert mem.recall("connection pool exhausted", tags=[APP_TAG], limit=5) == []

    def test_unavailable_raises(self):
        mem = FakeMemory()
        mem.available = False
        with pytest.raises(MemoryUnavailableError):
            mem.recall("anything", tags=[APP_TAG], limit=5)

    def test_reflect_is_not_faked(self):
        with pytest.raises(NotImplementedError):
            FakeMemory().reflect("q", tags=[])


# ── MemoryRecorder ───────────────────────────────────────────────────────────

class TestMemoryRecorder:
    def test_event_type_is_validated(self):
        with pytest.raises(ValueError):
            MemoryEvent("approval_granted", "INC-1", "x")
        with pytest.raises(ValueError):
            MemoryEvent("incident", " ", "x")

    def test_record_is_redacted_before_reaching_the_backend(self):
        mem = FakeMemory()
        rec = MemoryRecorder(mem)
        rec.record_incident("INC-1", "leaked DB_PASSWORD=hunter2 in logs",
                            details={"api_key": "sk-abcdefghijklmnopqrstu", "note": "Bearer abcdefghijklmnop"})
        stored = mem.records()[0]
        blob = stored.content + " ".join(stored.metadata.values())
        for secret in ("hunter2", "sk-abcdefghijklmnopqrstu", "abcdefghijklmnop"):
            assert secret not in blob
        assert REDACTED in stored.content

    def test_metadata_tags_and_stable_document_id(self):
        mem = FakeMemory()
        rec = MemoryRecorder(mem)
        out = rec.record_diagnosis("INC-7", "pool exhausted", confidence="high", source="llm_agent",
                                   occurred_at=datetime(2024, 3, 15, 2, 8, 33),
                                   services=["order-service"], pattern_tags=["DB_POOL_EXHAUSTED"])
        r = mem.records()[0]
        assert out.status == "retained"
        assert r.document_id == "incident-ops:INC-7:diagnosis:0" == out.document_id
        assert r.metadata["incident_id"] == "INC-7"
        assert r.metadata["event_type"] == "diagnosis"
        assert r.metadata["services"] == "order-service"
        assert r.metadata["pattern_tags"] == "DB_POOL_EXHAUSTED"
        assert r.metadata["occurred_at"] == "2024-03-15T02:08:33+00:00"
        assert all(isinstance(v, str) for v in r.metadata.values())
        assert set(r.tags) >= {APP_TAG, "event:diagnosis", "incident:INC-7",
                               "service:order-service", "pattern:DB_POOL_EXHAUSTED"}
        assert r.occurred_at.tzinfo is timezone.utc

    def test_decisions_on_different_recommendations_are_separate_documents(self):
        mem = FakeMemory()
        rec = MemoryRecorder(mem)
        for rid in ("rec-1", "rec-2"):
            rec.record_human_decision("INC-1", recommendation_id=rid, recommendation="x", decision="rejected",
                                      decided_by="sre", reason="unsafe")
        assert len(mem.records()) == 2

    def test_human_decision_requires_decider_and_reason(self):
        rec = MemoryRecorder(FakeMemory())
        with pytest.raises(ValueError):
            rec.record_human_decision("INC-1", recommendation_id="r", recommendation="x",
                                      decision="rejected", decided_by="", reason="unsafe")
        with pytest.raises(ValueError):
            rec.record_human_decision("INC-1", recommendation_id="r", recommendation="x",
                                      decision="maybe", decided_by="sre", reason="r")

    def test_retain_failure_is_logged_kept_for_retry_and_not_raised(self, caplog):
        mem = FakeMemory()
        mem.available = False
        rec = MemoryRecorder(mem)
        with caplog.at_level(logging.WARNING):
            out = rec.record_incident("INC-1", "pool exhausted")
        assert out.status == "failed" and "MemoryUnavailableError" in out.error
        assert "memory retain failed" in caplog.text
        assert len(rec.pending) == 1
        mem.available = True
        assert rec.retry_pending() == 1
        assert rec.pending == [] and len(mem.records()) == 1

    def test_disabled_recorder_sends_nothing(self):
        rec = MemoryRecorder(None, disabled_reason="HINDSIGHT_API_URL is not set")
        assert rec.record_incident("INC-1", "x").status == "disabled"
        out = rec.recall_for_incident("INC-1", "query")
        assert out.status == "disabled" and out.backend == "none" and out.memories == ()
        assert out.error == "HINDSIGHT_API_URL is not set"

    def test_recall_excludes_the_current_incident(self):
        mem = FakeMemory()
        rec = MemoryRecorder(mem)
        rec.record_incident("INC-1", "connection pool exhausted", pattern_tags=["DB_POOL_EXHAUSTED"])
        rec.record_incident("INC-2", "connection pool exhausted", pattern_tags=["DB_POOL_EXHAUSTED"])
        out = rec.recall_for_incident("INC-2", "connection pool exhausted")
        assert out.status == "available"
        assert [m.incident_id for m in out.memories] == ["INC-1"]

    def test_recall_failure_reports_unavailable(self):
        mem = FakeMemory()
        mem.available = False
        out = MemoryRecorder(mem).recall_for_incident("INC-1", "q")
        assert out.status == "unavailable" and out.memories == ()
        assert "MemoryUnavailableError" in out.error

    def test_recall_query_is_redacted(self):
        seen = []

        class Spy(FakeMemory):
            def recall(self, query, *, tags, limit):
                seen.append(query)
                return []

        MemoryRecorder(Spy()).recall_for_incident("INC-1", "error with password=hunter2")
        assert "hunter2" not in seen[0]

    def test_recorder_exposes_no_authorization_api(self):
        names = {n for n in dir(MemoryRecorder) if not n.startswith("_")}
        assert not any(w in n for n in names for w in ("approve", "authorize", "execute", "permit"))


# ── Hindsight adapter ────────────────────────────────────────────────────────

class StubHindsightClient:
    """Stands in for hindsight_client.Hindsight: async methods, records calls."""

    def __init__(self, *, recall_results=None, retain_success=True, error=None):
        self.calls = []
        self.closed = False
        self._recall_results = recall_results or []
        self._retain_success = retain_success
        self._error = error

    async def aretain(self, **kwargs):
        self.calls.append(("aretain", kwargs))
        if self._error:
            raise self._error
        return SimpleNamespace(success=self._retain_success, bank_id=kwargs["bank_id"], items_count=1)

    async def arecall(self, **kwargs):
        self.calls.append(("arecall", kwargs))
        if self._error:
            raise self._error
        return SimpleNamespace(results=self._recall_results)

    async def aclose(self):
        self.closed = True


def _adapter(client):
    return HindsightMemory("http://hindsight.test", "ops-bank", api_key="k",
                           client_factory=lambda url, key, timeout: client)


def _hs_result(**overrides):
    base = dict(id="f1", text="Restart of order-service was rejected during peak traffic",
                type="experience", context="incident response: human decision",
                metadata={"incident_id": "INC-4821", "event_type": "human_decision",
                          "services": "order-service", "occurred_at": "2024-03-15T02:12:00+00:00"},
                tags=[APP_TAG, "event:human_decision"], occurred_start=None,
                scores=SimpleNamespace(final=0.83))
    base.update(overrides)
    return SimpleNamespace(**base)


class TestHindsightAdapter:
    def test_retain_sends_verified_arguments(self):
        client = StubHindsightClient()
        record = build_record(MemoryEvent("incident", "INC-1", "pool exhausted",
                                          occurred_at=datetime(2024, 3, 15, 2, 8), services=("order-service",)))
        _adapter(client).retain_event(record)
        name, kwargs = client.calls[0]
        assert name == "aretain"
        assert kwargs["bank_id"] == "ops-bank"
        assert kwargs["document_id"] == "incident-ops:INC-1:incident:0"
        assert kwargs["context"] == "incident response: incident"
        assert kwargs["timestamp"] == datetime(2024, 3, 15, 2, 8, tzinfo=timezone.utc)
        assert all(isinstance(v, str) for v in kwargs["metadata"].values())
        assert APP_TAG in kwargs["tags"]
        assert client.closed

    def test_retain_without_success_is_a_failure(self):
        with pytest.raises(MemoryUnavailableError):
            _adapter(StubHindsightClient(retain_success=False)).retain_event(
                build_record(MemoryEvent("incident", "INC-1", "x")))

    def test_recall_arguments_and_normalization(self):
        client = StubHindsightClient(recall_results=[_hs_result()])
        hits = _adapter(client).recall("pool exhausted", tags=[APP_TAG], limit=5)
        _, kwargs = client.calls[0]
        assert kwargs["tags"] == [APP_TAG] and kwargs["tags_match"] == "any_strict"
        assert kwargs["bank_id"] == "ops-bank" and kwargs["query"] == "pool exhausted"
        m = hits[0]
        assert (m.source, m.memory_type, m.incident_id, m.event_type, m.score) == \
               ("hindsight", "experience", "INC-4821", "human_decision", 0.83)
        assert m.text.startswith("Restart of order-service was rejected")

    def test_normalize_tolerates_missing_optional_fields(self):
        m = normalize_result(SimpleNamespace(text="t", metadata=None, scores=None, tags=None,
                                             type=None, occurred_start="2024-01-01T00:00:00Z"))
        assert (m.incident_id, m.score, m.tags, m.occurred_at) == (None, None, (), "2024-01-01T00:00:00Z")

    def test_normalize_falls_back_to_tags_when_metadata_is_empty(self):
        # Hindsight observation-type hits carry no metadata, only the stored
        # tags; incident_id/event_type/services must still be recovered so
        # recall scoping (exclude the current incident) and the UI keep working.
        m = normalize_result(SimpleNamespace(
            text="t", metadata={}, scores=None, type="observation", occurred_start=None,
            tags=[APP_TAG, "incident:INC-4821", "event:lesson", "service:order-service",
                  "service:db-primary"]))
        assert (m.incident_id, m.event_type, m.services) == ("INC-4821", "lesson", "order-service,db-primary")

    def test_normalize_prefers_real_metadata_over_tags(self):
        m = normalize_result(SimpleNamespace(
            text="t", metadata={"incident_id": "INC-1", "event_type": "incident", "services": "auth-service"},
            scores=None, type="world", occurred_start=None,
            tags=[APP_TAG, "incident:INC-OTHER", "event:lesson"]))
        assert (m.incident_id, m.event_type, m.services) == ("INC-1", "incident", "auth-service")

    def test_recall_excludes_current_incident_observations_with_empty_metadata(self):
        # A current-incident observation (no metadata, only tags) must not leak
        # into its own recall: tag fallback restores the incident_id, so the
        # recorder's exclusion filter sees it.
        client = StubHindsightClient(recall_results=[
            _hs_result(type="observation", metadata={},
                       tags=[APP_TAG, "incident:INC-2", "event:evidence_digest"]),
            _hs_result(type="observation", metadata={},
                       tags=[APP_TAG, "incident:INC-1", "event:lesson"]),
        ])
        rec = MemoryRecorder(_adapter(client))
        out = rec.recall_for_incident("INC-2", "connection pool exhausted")
        assert out.status == "available"
        assert [m.incident_id for m in out.memories] == ["INC-1"]

    def test_bank_not_found_means_no_memories_yet(self):
        err = Exception("Not Found")
        err.status = 404
        assert _adapter(StubHindsightClient(error=err)).recall("q", tags=[APP_TAG], limit=5) == []

    def test_connection_failure_is_unavailable(self):
        with pytest.raises(MemoryUnavailableError, match="connection refused"):
            _adapter(StubHindsightClient(error=OSError("connection refused"))).recall("q", tags=[], limit=5)

    def test_missing_client_package_is_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "hindsight_client", None)
        mem = HindsightMemory("http://hindsight.test", "ops-bank")
        with pytest.raises(MemoryUnavailableError, match="not installed"):
            mem.recall("q", tags=[], limit=5)

    def test_works_when_called_inside_a_running_event_loop(self):
        client = StubHindsightClient(recall_results=[_hs_result()])

        async def inside_loop():
            return _adapter(client).recall("q", tags=[APP_TAG], limit=5)

        assert asyncio.run(inside_loop())[0].incident_id == "INC-4821"

    def test_from_env(self, monkeypatch):
        monkeypatch.delenv("HINDSIGHT_API_URL", raising=False)
        monkeypatch.delenv("HINDSIGHT_BANK_ID", raising=False)
        assert HindsightMemory.from_env() == (None, "HINDSIGHT_API_URL is not set")
        monkeypatch.setenv("HINDSIGHT_API_URL", "http://localhost:8888")
        assert HindsightMemory.from_env() == (None, "HINDSIGHT_BANK_ID is not set")
        monkeypatch.setenv("HINDSIGHT_BANK_ID", "ops")
        monkeypatch.setenv("HINDSIGHT_API_KEY", "secret")
        mem, reason = HindsightMemory.from_env()
        assert reason is None and (mem.base_url, mem.bank_id, mem._api_key) == ("http://localhost:8888", "ops", "secret")

    def test_kwargs_match_the_installed_client_signature(self):
        hindsight_client = pytest.importorskip("hindsight_client")
        record = build_record(MemoryEvent("incident", "INC-1", "x"))
        inspect.signature(hindsight_client.Hindsight.aretain).bind(None, **retain_kwargs("b", record))
        inspect.signature(hindsight_client.Hindsight.arecall).bind(
            None, **recall_kwargs("b", "q", [APP_TAG], None, "mid", 4096))
