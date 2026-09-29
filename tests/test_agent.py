"""
tests/test_agent.py — Unit tests for the LangGraph agent and post-mortem logic.

Run:  pytest tests/test_agent.py -v
Note: Tests that call the live OpenAI API are marked with @pytest.mark.live
      and deselected by default (see pytest.ini). Run with -m live to include them.
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

    def test_missing_api_key_raises_the_dedicated_error(self):
        from agent.graph import MissingAPIKeyError, get_llm
        timeline = [self._make_event("2024-03-15T02:08:00")]
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(MissingAPIKeyError, match="OPENAI_API_KEY"):
                get_llm()
            with pytest.raises(MissingAPIKeyError, match="OPENAI_API_KEY"):
                run_agent("INC-NOKEY", timeline)
        assert issubclass(MissingAPIKeyError, OSError)

    @pytest.mark.parametrize("exc", [
        OSError("disk full"),
        PermissionError("chroma_db is not writable"),
        FileNotFoundError("chroma_db/chroma.sqlite3"),
    ])
    def test_unrelated_oserror_is_an_agent_error_not_missing_key(self, exc):
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = exc
        timeline = [self._make_event("2024-03-15T02:08:00", level="ERROR")]
        result = run_agent("INC-OSERR", timeline, graph=mock_graph)  # must not raise
        assert result["analysis_mode"] == "agent_error"
        assert type(exc).__name__ in result["contributing_factors"][0]

    def test_error_result_is_labelled_agent_error(self):
        mock_graph = MagicMock()
        mock_graph.invoke.side_effect = RuntimeError("boom")
        timeline = [self._make_event("2024-03-15T02:08:00", level="ERROR")]
        result = run_agent("INC-ERR", timeline, graph=mock_graph)
        assert result["analysis_mode"] == "agent_error"

    def test_empty_timeline_is_labelled_no_evidence(self):
        assert run_agent("INC-EMPTY", [])["analysis_mode"] == "no_evidence"


# ── Graph injection and isolation ─────────────────────────────────────────────

import agent.graph as graph_module
from agent.graph import build_graph
from agent.tools import search_timeline
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class ScriptedLLM:
    """Stands in for a tool-bound chat model: returns canned replies in order
    and records the messages it was called with."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def invoke(self, messages):
        self.calls.append(list(messages))
        return self.replies.pop(0)


def _timeline():
    return [
        {"event_id": 0, "timestamp": datetime(2024, 3, 15, 2, 8, 33),
         "timestamp_str": "2024-03-15 02:08:33", "source": "log", "level": "CRITICAL",
         "content": "[db-primary] Connection pool exhausted: 200/200 connections active",
         "patterns": ["DB_POOL_EXHAUSTED"], "raw": ""},
        {"event_id": 1, "timestamp": datetime(2024, 3, 15, 2, 13, 3),
         "timestamp_str": "2024-03-15 02:13:03", "source": "log", "level": "INFO",
         "content": "[db-primary] 197 connections in state=idle_in_transaction",
         "patterns": ["DB_IDLE_LEAK"], "raw": ""},
    ]


FINAL_JSON = json.dumps({
    "incident_id": "INC-FROM-MODEL",
    "summary": "Pool exhausted.",
    "root_cause": "Connection leak exhausted the DB pool.",
    "contributing_factors": [],
    "evidence_citations": ["[E0] [2024-03-15 02:08:33] [LOG] [CRITICAL] [db-primary] Connection pool exhausted: 200/200 connections active"],
    "action_items": [],
    "similar_past_incidents": [],
    "confidence": "high",
    "timeline_reconstruction": "",
})


class TestGraphInjection:
    def test_injected_graph_is_used_and_build_graph_is_not_called(self):
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"messages": [AIMessage(content=FINAL_JSON)]}
        with patch("agent.graph.build_graph") as mock_build:
            result = run_agent("INC-1", _timeline(), graph=mock_graph)
        mock_build.assert_not_called()
        mock_graph.invoke.assert_called_once()
        assert result["root_cause"] == "Connection leak exhausted the DB pool."
        assert result["analysis_mode"] == "llm_agent"

    def test_graph_state_carries_incident_id_and_event_count(self):
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"messages": [AIMessage(content=FINAL_JSON)]}
        run_agent("INC-STATE", _timeline(), graph=mock_graph)
        state = mock_graph.invoke.call_args.args[0]
        assert state["incident_id"] == "INC-STATE"
        assert state["event_count"] == 2

    def test_result_incident_id_comes_from_caller_not_model(self):
        mock_graph = MagicMock()
        mock_graph.invoke.return_value = {"messages": [AIMessage(content=FINAL_JSON)]}
        result = run_agent("INC-REAL", _timeline(), graph=mock_graph)
        assert result["incident_id"] == "INC-REAL"

    def test_no_module_level_graph_or_llm_cache(self):
        assert not hasattr(graph_module, "_graph")
        assert not hasattr(graph_module, "_llm")

    def test_each_uninjected_run_builds_a_fresh_graph(self):
        with patch("agent.graph.build_graph") as mock_build:
            mock_build.return_value.invoke.return_value = {"messages": [AIMessage(content=FINAL_JSON)]}
            run_agent("INC-A", _timeline())
            run_agent("INC-B", _timeline())
        assert mock_build.call_count == 2

    def test_mocked_graph_does_not_leak_into_later_runs(self):
        failing = MagicMock()
        failing.invoke.side_effect = RuntimeError("from an earlier test")
        with patch("agent.graph.build_graph", return_value=failing):
            assert run_agent("INC-X", _timeline())["analysis_mode"] == "agent_error"
        # A later run with a different graph must not see the failing one.
        ok = MagicMock()
        ok.invoke.return_value = {"messages": [AIMessage(content=FINAL_JSON)]}
        assert run_agent("INC-Y", _timeline(), graph=ok)["analysis_mode"] == "llm_agent"


class TestIncidentIdPropagation:
    def test_prompt_contains_incident_id_event_count_and_event_ids(self):
        llm = ScriptedLLM([AIMessage(content=FINAL_JSON)])
        run_agent("INC-42", _timeline(), graph=build_graph(llm=llm))
        human = next(m for m in llm.calls[0] if isinstance(m, HumanMessage))
        assert "Incident ID: INC-42" in human.content
        assert "Total timeline events: 2" in human.content
        assert "[E0] [2024-03-15 02:08:33] [LOG] [CRITICAL] [db-primary] Connection pool exhausted" in human.content

    def test_model_cannot_see_incident_id_in_search_timeline_schema(self):
        props = search_timeline.tool_call_schema.model_json_schema()["properties"]
        assert list(props) == ["query"]

    def test_search_timeline_receives_incident_id_from_graph_state(self, monkeypatch):
        seen = []

        def fake_retrieve(query, incident_id, top_k=5):
            seen.append((query, incident_id))
            return [{"text": "[E0] pool exhausted", "event_ids": [0]}]

        monkeypatch.setattr("agent.tools.retrieve_evidence", fake_retrieve)
        llm = ScriptedLLM([
            # The model even tries to pass a wrong incident ID; state must win.
            AIMessage(content="", tool_calls=[{
                "name": "search_timeline",
                "args": {"query": "connection pool", "incident_id": "INC-WRONG"},
                "id": "call_1",
            }]),
            AIMessage(content=FINAL_JSON),
        ])
        result = run_agent("INC-42", _timeline(), graph=build_graph(llm=llm))

        assert seen == [("connection pool", "INC-42")]
        tool_msgs = [m for m in llm.calls[1] if isinstance(m, ToolMessage)]
        assert tool_msgs and "[E0] pool exhausted" in tool_msgs[0].content
        assert result["analysis_mode"] == "llm_agent"


# ── Live integration test (deselected unless -m live) ─────────────────────────

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