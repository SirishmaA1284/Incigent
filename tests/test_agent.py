"""
tests/test_agent.py — Unit tests for the LangGraph agent and post-mortem logic.

Run:  pytest tests/test_agent.py -v
Note: Tests that call the live OpenAI API are marked with @pytest.mark.live
      and skipped by default. Run with -m live to include them.
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


# ── _extract_json_from_text ───────────────────────────────────────────────────

from agent.graph import _extract_json_from_text


class TestExtractJsonFromText:
    def test_parses_bare_json(self):
        text = '{"root_cause": "DB pool exhausted", "confidence": "high"}'
        result = _extract_json_from_text(text)
        assert result["root_cause"] == "DB pool exhausted"
        assert result["confidence"] == "high"

    def test_parses_fenced_json(self):
        text = '```json\n{"root_cause": "OOM kill", "confidence": "high"}\n```'
        result = _extract_json_from_text(text)
        assert result["root_cause"] == "OOM kill"

    def test_parses_json_embedded_in_prose(self):
        text = 'Here is my analysis:\n{"root_cause": "Connection leak"}\nEnd of analysis.'
        result = _extract_json_from_text(text)
        assert result["root_cause"] == "Connection leak"

    def test_falls_back_gracefully_on_invalid_json(self):
        text = "This is not JSON at all."
        result = _extract_json_from_text(text)
        assert "root_cause" in result          # fallback dict always has this key
        assert result["confidence"] == "low"

    def test_required_keys_present_in_fallback(self):
        result = _extract_json_from_text("unparseable gibberish")
        required = [
            "summary", "root_cause", "contributing_factors",
            "evidence_citations", "action_items", "similar_past_incidents",
            "confidence", "timeline_reconstruction",
        ]
        for key in required:
            assert key in result, f"Missing key: {key}"

    def test_handles_empty_string(self):
        result = _extract_json_from_text("")
        assert isinstance(result, dict)


# ── run_agent (mocked) ────────────────────────────────────────────────────────

from agent.graph import run_agent


class TestRunAgent:
    def _make_event(self, ts_str, level="INFO", content="test event", patterns=None):
        return {
            "timestamp":     datetime.fromisoformat(ts_str),
            "timestamp_str": ts_str.replace("T", " "),
            "source":        "log",
            "level":         level,
            "content":       content,
            "patterns":      patterns or [],
            "raw":           content,
        }

    def test_returns_dict_with_required_keys(self):
        """run_agent with empty timeline returns a structured dict immediately."""
        result = run_agent("INC-TEST-001", [])
        required = [
            "incident_id", "summary", "root_cause",
            "contributing_factors", "evidence_citations",
            "action_items", "similar_past_incidents",
            "confidence", "timeline_reconstruction",
        ]
        for key in required:
            assert key in result, f"Missing key: {key}"

    def test_empty_timeline_returns_low_confidence(self):
        result = run_agent("INC-EMPTY", [])
        assert result["confidence"] == "low"
        assert result["incident_id"] == "INC-EMPTY"

    def test_empty_timeline_action_item_suggests_uploading_data(self):
        result = run_agent("INC-EMPTY", [])
        actions = result.get("action_items", [])
        assert len(actions) >= 1

    @patch("agent.graph.build_graph")
    def test_graceful_error_handling_on_agent_exception(self, mock_build):
        """If the LangGraph graph throws, run_agent returns a structured error dict."""
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("Simulated network failure")
        mock_build.return_value = mock_graph

        # We need an OPENAI_API_KEY env var to pass the get_llm() check
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            timeline = [self._make_event("2024-03-15T02:08:00", level="ERROR",
                                         content="DB pool exhausted")]
            result = run_agent("INC-ERR", timeline)

        assert "root_cause" in result
        assert result.get("confidence") == "low"

    @patch("agent.graph.build_graph")
    @patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"})
    def test_non_json_agent_response_is_handled(self, mock_build):
        """Agent returning plain text instead of JSON falls back gracefully."""
        mock_msg = MagicMock()
        mock_msg.content = "I believe the root cause is a connection leak."
        mock_msg.tool_calls = []

        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"messages": [mock_msg]}
        mock_build.return_value = mock_graph

        timeline = [self._make_event("2024-03-15T02:08:00", level="ERROR",
                                     content="Connection pool exhausted")]
        result = run_agent("INC-PROSE", timeline)

        # Should still return a valid dict
        assert isinstance(result, dict)
        assert "root_cause" in result

    def test_missing_api_key_raises_environment_error(self):
        timeline = [self._make_event("2024-03-15T02:08:00")]
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("OPENAI_API_KEY", None)
            with pytest.raises(EnvironmentError, match="OPENAI_API_KEY"):
                run_agent("INC-NOKEY", timeline)


# ── Live integration test (skipped unless -m live) ────────────────────────────

@pytest.mark.live
def test_live_agent_returns_structured_postmortem():
    """
    Live integration test — calls the real OpenAI API.
    Requires OPENAI_API_KEY to be set.
    Run: pytest tests/test_agent.py -m live -v
    """
    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")

    timeline = [
        {
            "timestamp":     datetime(2024, 3, 15, 2, 8, 33),
            "timestamp_str": "2024-03-15 02:08:33",
            "source": "log", "level": "CRITICAL",
            "content": "Connection pool exhausted: 200/200 connections active",
            "patterns": ["DB_POOL_EXHAUSTED"],
            "raw": "2024-03-15 02:08:33 CRITICAL Connection pool exhausted",
        },
        {
            "timestamp":     datetime(2024, 3, 15, 2, 13, 3),
            "timestamp_str": "2024-03-15 02:13:03",
            "source": "log", "level": "INFO",
            "content": "197 connections in state=idle_in_transaction",
            "patterns": ["DB_IDLE_LEAK"],
            "raw": "2024-03-15 02:13:03 INFO 197 connections in state=idle_in_transaction",
        },
    ]
    result = run_agent("INC-LIVE-TEST", timeline)
    assert isinstance(result, dict)
    assert result.get("confidence") in ("high", "medium", "low")
    assert len(result.get("root_cause", "")) > 10