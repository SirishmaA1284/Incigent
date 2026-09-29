"""
tests/test_api.py — API wiring: /analyze reaches the real LangGraph agent
(driven by a scripted fake LLM), and a missing API key fails loudly with 503.
No OpenAI or Chroma calls are made.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

import api.main as api_main
from agent.graph import MissingAPIKeyError, build_graph as real_build_graph
from memory import MemoryRecorder
from memory.fake_memory import FakeMemory
from tests.test_agent import FINAL_JSON, ScriptedLLM

SAMPLE_LOG = (Path(__file__).parent.parent / "data" / "raw" / "sample_logs.txt").read_bytes()


@pytest.fixture(autouse=True)
def fake_memory(monkeypatch):
    """The API builds its memory recorder from .env at startup; these tests use
    an in-process FakeMemory instead so they never reach a real Hindsight bank."""
    memory = FakeMemory()
    monkeypatch.setattr(api_main, "build_memory_recorder", lambda: MemoryRecorder(memory))
    return memory


@pytest.fixture
def embedded(monkeypatch):
    calls = []
    monkeypatch.setattr(api_main, "embed_timeline", lambda tl, iid: calls.append((len(tl), iid)))
    return calls


def test_root():
    with TestClient(api_main.app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert r.json() == {"status": "running"}


def test_analyze_reaches_the_langgraph_agent(monkeypatch, embedded):
    searched = []
    monkeypatch.setattr(
        "agent.tools.retrieve_evidence",
        lambda q, iid, top_k=5: searched.append(iid) or [{"text": "[E6] pool exhausted", "event_ids": [6]}],
    )
    llm = ScriptedLLM([
        AIMessage(content="", tool_calls=[{"name": "search_timeline", "args": {"query": "pool"}, "id": "c1"}]),
        AIMessage(content=FINAL_JSON),
    ])
    monkeypatch.setattr(api_main, "build_graph", lambda **kw: real_build_graph(llm=llm, **kw))

    with TestClient(api_main.app) as client:
        r = client.post("/analyze", data={"incident_id": "INC-API-1"},
                        files={"log_file": ("logs.txt", SAMPLE_LOG, "text/plain")})

    assert r.status_code == 200, r.text
    pm = r.json()["postmortem"]
    assert pm["analysis_mode"] == "llm_agent"
    assert pm["incident_id"] == "INC-API-1"
    assert pm["root_cause"] == "Connection leak exhausted the DB pool."
    assert embedded == [(41, "INC-API-1")]
    assert searched == ["INC-API-1"]
    assert len(llm.calls) == 2  # analyst -> tools -> analyst


def test_analyze_without_api_key_returns_503_and_skips_work(monkeypatch, embedded):
    def no_key(**kw):
        raise MissingAPIKeyError("OPENAI_API_KEY not set.")

    monkeypatch.setattr(api_main, "build_graph", no_key)
    with TestClient(api_main.app) as client:
        r = client.post("/analyze", data={"incident_id": "INC-API-2"},
                        files={"log_file": ("logs.txt", SAMPLE_LOG, "text/plain")})

    assert r.status_code == 503
    assert "OPENAI_API_KEY" in r.json()["detail"]
    assert embedded == []


def test_analyze_with_real_missing_key_returns_503(monkeypatch, embedded):
    # No stub: the real get_llm() must raise MissingAPIKeyError.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(api_main.app) as client:
        r = client.post("/analyze", data={"incident_id": "INC-API-3"},
                        files={"log_file": ("logs.txt", SAMPLE_LOG, "text/plain")})

    assert r.status_code == 503
    assert "OPENAI_API_KEY" in r.json()["detail"]
    assert embedded == []


def test_unrelated_oserror_during_graph_build_is_not_a_503(monkeypatch, embedded):
    calls = []

    def build(**kw):
        calls.append(1)
        if len(calls) == 1:  # startup: no key yet
            raise MissingAPIKeyError("OPENAI_API_KEY not set.")
        raise PermissionError("disk is read-only")  # request time: unrelated OSError

    monkeypatch.setattr(api_main, "build_graph", build)
    with TestClient(api_main.app, raise_server_exceptions=False) as client:
        r = client.post("/analyze", data={"incident_id": "INC-API-4"},
                        files={"log_file": ("logs.txt", SAMPLE_LOG, "text/plain")})

    assert r.status_code == 500
    assert embedded == []


def test_chroma_oserror_during_analysis_is_an_agent_error_not_missing_key(monkeypatch, embedded):
    def broken_retrieve(query, incident_id, top_k=5):
        raise PermissionError("chroma_db is not writable")

    monkeypatch.setattr("agent.tools.retrieve_evidence", broken_retrieve)
    llm = ScriptedLLM([
        AIMessage(content="", tool_calls=[{"name": "search_timeline", "args": {"query": "pool"}, "id": "c1"}]),
    ])
    monkeypatch.setattr(api_main, "build_graph", lambda **kw: real_build_graph(llm=llm, **kw))

    with TestClient(api_main.app) as client:
        r = client.post("/analyze", data={"incident_id": "INC-API-5"},
                        files={"log_file": ("logs.txt", SAMPLE_LOG, "text/plain")})

    assert r.status_code == 200
    pm = r.json()["postmortem"]
    assert pm["analysis_mode"] == "agent_error"
    assert "PermissionError" in pm["contributing_factors"][0]
    assert "OPENAI_API_KEY" not in pm["summary"]
