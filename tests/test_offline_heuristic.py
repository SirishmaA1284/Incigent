"""
tests/test_offline_heuristic.py — The offline fallback must always be labelled,
never claim historical recall, and never blame a post-failure deployment.
"""

import json
from datetime import datetime
from pathlib import Path

from agent.offline_heuristic import (
    OFFLINE_LABEL, OFFLINE_MODE, analyze_with_fallback, offline_postmortem,
)
from ingestion.log_parser import parse_log_file
from ingestion.timeline_builder import build_timeline

ROOT = Path(__file__).parent.parent
FABRICATED_IDS = ("INC-3901", "INC-4105", "INC-4200")


def _sample_timeline():
    return build_timeline([parse_log_file(str(ROOT / "data" / "raw" / "sample_logs.txt"))])


def _ev(event_id, hhmmss, level, content, patterns):
    ts = datetime.strptime(f"2024-03-15 {hhmmss}", "%Y-%m-%d %H:%M:%S")
    return {"event_id": event_id, "timestamp": ts, "timestamp_str": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "log", "level": level, "content": content, "patterns": patterns}


class TestOfflinePostmortem:
    def test_is_labelled_offline_heuristic(self):
        pm = offline_postmortem("INC-1", _sample_timeline(), reason="API down")
        assert pm["analysis_mode"] == OFFLINE_MODE
        assert pm["analysis_mode_label"] == "Offline heuristic mode" == OFFLINE_LABEL
        assert pm["fallback_reason"] == "API down"

    def test_never_claims_historical_incidents(self):
        pm = offline_postmortem("INC-1", _sample_timeline())
        assert pm["similar_past_incidents"] == []
        blob = json.dumps(pm, default=str)
        assert not any(fake in blob for fake in FABRICATED_IDS)

    def test_restart_during_remediation_is_not_blamed(self):
        # The sample's only restart is the 02:17 rolling restart, after the outage.
        pm = offline_postmortem("INC-1", _sample_timeline())
        assert not any("02:17" in f for f in pm["contributing_factors"])
        assert not any(f.startswith("Deployment at") for f in pm["contributing_factors"])

    def test_deployment_before_failure_is_attributed(self):
        tl = [
            _ev(0, "01:45:00", "INFO", "Deployed order-service v2.4.1", ["DEPLOY"]),
            _ev(1, "02:08:33", "ERROR", "Connection pool exhausted", ["DB_POOL_EXHAUSTED"]),
            _ev(2, "02:13:03", "INFO", "197 idle_in_transaction", ["DB_IDLE_LEAK"]),
            _ev(3, "02:30:00", "INFO", "Deployed hotfix v2.4.2", ["DEPLOY"]),
        ]
        factors = offline_postmortem("INC-1", tl)["contributing_factors"]
        assert factors[0].startswith("Deployment at 2024-03-15 01:45:00")
        assert not any("02:30:00" in f for f in factors)

    def test_evidence_citations_carry_event_ids(self):
        pm = offline_postmortem("INC-1", _sample_timeline())
        assert pm["evidence_citations"]
        assert all(c.startswith("[E") for c in pm["evidence_citations"])


class TestAnalyzeWithFallback:
    def test_api_failure_yields_labelled_offline_result_with_reason(self):
        def failing_api():
            raise ConnectionError("connection refused")

        pm = analyze_with_fallback(failing_api, "INC-1", _sample_timeline())
        assert pm["analysis_mode"] == OFFLINE_MODE
        assert pm["analysis_mode_label"] == OFFLINE_LABEL
        assert "connection refused" in pm["fallback_reason"]

    def test_api_success_is_passed_through_as_ai_analysis(self):
        api_result = {"incident_id": "INC-1", "analysis_mode": "llm_agent", "root_cause": "x"}
        pm = analyze_with_fallback(lambda: api_result, "INC-1", [])
        assert pm["analysis_mode"] == "llm_agent"
        assert pm["analysis_mode_label"] == "AI agent analysis"
        assert pm["root_cause"] == "x"

    def test_unlabelled_api_result_is_not_presented_as_ai_analysis(self):
        pm = analyze_with_fallback(lambda: {"incident_id": "INC-1"}, "INC-1", [])
        assert pm["analysis_mode"] == "unknown"
        assert pm["analysis_mode_label"] == "Unknown analysis mode"

    def test_api_agent_error_is_not_presented_as_success(self):
        api_result = {"incident_id": "INC-1", "analysis_mode": "agent_error"}
        pm = analyze_with_fallback(lambda: api_result, "INC-1", [])
        assert pm["analysis_mode_label"] == "AI agent failed"


def test_ui_contains_no_fabricated_incidents_or_silent_fallback():
    # The Streamlit UI can't be imported without streamlit; check its source.
    src = (ROOT / "ui" / "app.py").read_text(encoding="utf-8")
    assert not any(fake in src for fake in FABRICATED_IDS)
    assert "_local_postmortem" not in src
    assert "analyze_with_fallback" in src
