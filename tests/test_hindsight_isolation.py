"""
tests/test_hindsight_isolation.py — Normal tests never reach a real Hindsight
server, even with real-looking HINDSIGHT_* settings present (as in a real .env).
"""

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from agent.graph import build_graph
from memory import build_memory_recorder
from tests.test_agent import ScriptedLLM
from tests.test_memory_graph import INCIDENT_2_LOGS, final_json

hindsight_client = pytest.importorskip("hindsight_client")

REAL_LOOKING = {
    "HINDSIGHT_API_URL": "https://hindsight.invalid",
    "HINDSIGHT_API_KEY": "not-a-real-key",
    "HINDSIGHT_BANK_ID": "incident-ops",
}


@pytest.fixture
def real_looking_env(monkeypatch):
    for k, v in REAL_LOOKING.items():
        monkeypatch.setenv(k, v)


def test_guard_blocks_real_client_and_nothing_is_retained(real_looking_env, hindsight_guard):
    recorder = build_memory_recorder()
    assert recorder.backend == "hindsight"          # configuration is read normally...

    recalled = recorder.recall_for_incident("INC-ISO-1", "pool exhausted")
    retained = recorder.record_incident("INC-ISO-1", "pool exhausted")

    # ...but no client was ever built, so nothing left the process.
    assert hindsight_guard == [REAL_LOOKING["HINDSIGHT_API_URL"]] * 2
    assert recalled.status == "unavailable" and "RealHindsightBlocked" in recalled.error
    assert retained.status == "failed" and "RealHindsightBlocked" in retained.error
    hindsight_guard.clear()  # acknowledged: this test triggers the guard on purpose


def test_api_with_real_config_and_no_stub_still_cannot_reach_hindsight(real_looking_env, hindsight_guard, monkeypatch):
    llm = ScriptedLLM([final_json()])
    monkeypatch.setattr(api_main, "build_graph", lambda: build_graph(llm=llm))
    monkeypatch.setattr(api_main, "embed_timeline", lambda tl, iid: None)
    with TestClient(api_main.app) as client:
        pm = client.post("/analyze", data={"incident_id": "INC-ISO-2"},
                         files={"log_file": ("logs.txt", INCIDENT_2_LOGS.encode(), "text/plain")}).json()["postmortem"]

    assert pm["historical_memory"]["status"] == "unavailable"
    assert all(o["status"] == "failed" for o in pm["memory_retention"])
    # 1 recall + 2 intake retains were all stopped before any client existed.
    assert len(hindsight_guard) == 3
    hindsight_guard.clear()


def test_guard_keeps_client_signatures_inspectable():
    # Signature checks against the installed client must still work under the guard.
    assert "document_id" in hindsight_client.Hindsight.aretain.__code__.co_varnames
