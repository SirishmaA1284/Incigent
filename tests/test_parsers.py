"""
tests/test_parsers.py — Unit tests for InfraLens ingestion layer.

Run:  pytest tests/test_parsers.py -v
"""

import pytest
from datetime import datetime
from ingestion.log_parser import parse_log_line, detect_patterns
from ingestion.slack_parser import parse_slack_export          # Fix #1: was parse_slack_messages
from ingestion.ticket_parser import parse_tickets
from ingestion.timeline_builder import build_timeline


# ── log_parser ────────────────────────────────────────────────────────────────

class TestParseLogLine:
    def test_parses_standard_timestamp(self):
        line = "2024-03-15 02:08:33 ERROR [db-primary] Connection pool exhausted: 200/200 connections active"
        ev = parse_log_line(line)
        assert ev is not None
        assert ev["timestamp"] == datetime(2024, 3, 15, 2, 8, 33)
        assert ev["level"] == "CRITICAL"          # elevated by pattern detection
        assert ev["source"] == "log"
        assert "db-primary" in (ev.get("component") or "")

    def test_parses_nginx_timestamp(self):
        line = '10.0.0.1 - - [15/Mar/2024:02:08:33 +0000] "GET /api/health HTTP/1.1" 503 0'
        ev = parse_log_line(line)
        assert ev is not None
        assert ev["timestamp"].day == 15

    def test_skips_blank_lines(self):
        assert parse_log_line("") is None
        assert parse_log_line("   ") is None

    def test_skips_comment_lines(self):
        assert parse_log_line("# this is a comment") is None

    def test_skips_lines_without_timestamp(self):
        assert parse_log_line("no timestamp here at all") is None

    def test_level_normalisation(self):
        cases = [
            ("FATAL",   "CRITICAL"),
            ("CRITICAL","CRITICAL"),
            ("ERR",     "ERROR"),
            ("WARNING", "WARN"),
            ("NOTICE",  "INFO"),
            ("TRACE",   "DEBUG"),
        ]
        for raw_level, expected in cases:
            line = f"2024-01-01 00:00:00 {raw_level} [svc] some message"
            ev = parse_log_line(line)
            assert ev is not None, f"failed to parse line with level {raw_level}"
            assert ev["level"] == expected, f"{raw_level} → expected {expected}, got {ev['level']}"

    def test_level_elevated_by_pattern(self):
        # Line says INFO but pattern detection should raise to CRITICAL
        line = "2024-03-15 02:08:35 INFO [db-primary] max_connections limit reached"
        ev = parse_log_line(line)
        assert ev is not None
        assert ev["level"] == "CRITICAL"

    def test_db_pool_exhausted_pattern_detected(self):
        line = "2024-03-15 02:08:33 ERROR [db] Connection pool exhausted: 200/200 connections active"
        ev = parse_log_line(line)
        assert ev is not None
        assert "DB_POOL_EXHAUSTED" in ev["patterns"]

    def test_circuit_breaker_open_pattern_detected(self):
        line = "2024-03-15 02:08:38 CRITICAL [api] Circuit breaker OPEN for db-primary"
        ev = parse_log_line(line)
        assert ev is not None
        assert "CIRCUIT_BREAKER_OPEN" in ev["patterns"]

    def test_oom_kill_pattern_detected(self):
        line = "2024-03-15 02:00:00 CRITICAL [kernel] Out of memory: kill process 1234"
        ev = parse_log_line(line)
        assert ev is not None
        assert "OOM_KILL" in ev["patterns"]

    def test_recovery_pattern_detected(self):
        line = "2024-03-15 02:14:23 INFO [api-gateway] Circuit breaker CLOSED — traffic restored"
        ev = parse_log_line(line)
        assert ev is not None
        assert "RECOVERY" in ev["patterns"] or "CIRCUIT_BREAKER_CLOSE" in ev["patterns"]

    def test_replication_lag_pattern_detected(self):
        line = "2024-03-15 02:09:01 WARN [db-replica-1] Replication lag: 47 seconds behind primary"
        ev = parse_log_line(line)
        assert ev is not None
        assert "DB_REPLICATION_LAG" in ev["patterns"]


class TestDetectPatterns:
    def test_returns_empty_for_clean_line(self):
        hits = detect_patterns("Connection established successfully at startup")
        assert hits == []

    def test_detects_multiple_patterns_in_one_line(self):
        line = "Connection pool exhausted and health check failed"
        hits = detect_patterns(line)
        tags = [t for t, _ in hits]
        assert "DB_POOL_EXHAUSTED" in tags
        assert "HEALTH_CHECK_FAIL" in tags

    def test_idle_in_transaction_pattern(self):
        hits = detect_patterns("197 connections in state=idle_in_transaction")
        assert any(t == "DB_IDLE_LEAK" for t, _ in hits)

    def test_deploy_pattern(self):
        hits = detect_patterns("Deploying order-service v2.4.1 to production")
        assert any(t == "DEPLOY" for t, _ in hits)


# ── slack_parser ──────────────────────────────────────────────────────────────

class TestParseSlackExport:                                    # Fix #1: class renamed to match function
    SAMPLE = [
        {"ts": "1710468203.000001", "user": "rahul.sre",  "text": "DB is down, checking now"},
        {"ts": "1710468215.000002", "user": "priya.oncall","text": "Declaring P1"},
    ]

    def test_returns_correct_count(self):
        events = parse_slack_export(self.SAMPLE)              # Fix #1: was parse_slack_messages
        assert len(events) == 2

    def test_timestamps_parsed_correctly(self):
        events = parse_slack_export(self.SAMPLE)
        assert events[0]["timestamp"] == datetime.utcfromtimestamp(1710468203.000001)

    def test_source_is_slack(self):
        events = parse_slack_export(self.SAMPLE)
        assert all(ev["source"] == "slack" for ev in events)

    def test_content_includes_user_and_text(self):
        events = parse_slack_export(self.SAMPLE)
        assert "rahul.sre" in events[0]["content"]
        assert "DB is down" in events[0]["content"]

    def test_skips_messages_with_missing_ts(self):
        bad_msgs = [{"user": "alice", "text": "no timestamp"}]
        events = parse_slack_export(bad_msgs)
        assert len(events) == 0

    def test_handles_empty_list(self):
        assert parse_slack_export([]) == []


# ── ticket_parser ─────────────────────────────────────────────────────────────

class TestParseTickets:
    SAMPLE = [
        {
            "id": "INC-4821",
            "title": "P1: Production DB down",
            "description": "DB connection pool exhausted.",
            "status": "resolved",
            "priority": "P1",
            "created_at": "2024-03-15T02:10:05Z",
            "assignee": "rahul.sre",
        }
    ]

    def test_returns_correct_count(self):
        events = parse_tickets(self.SAMPLE)
        assert len(events) == 1

    def test_source_is_ticket(self):
        events = parse_tickets(self.SAMPLE)
        assert events[0]["source"] == "ticket"

    def test_content_includes_ticket_id(self):
        events = parse_tickets(self.SAMPLE)
        assert "INC-4821" in events[0]["content"]

    def test_timestamp_parsed_correctly(self):
        events = parse_tickets(self.SAMPLE)
        assert events[0]["timestamp"] == datetime(2024, 3, 15, 2, 10, 5)

    def test_skips_tickets_with_missing_created_at(self):
        bad = [{"id": "X", "title": "No date", "description": "", "status": "open"}]
        events = parse_tickets(bad)
        assert len(events) == 0

    def test_handles_empty_list(self):
        assert parse_tickets([]) == []


# ── timeline_builder ──────────────────────────────────────────────────────────

class TestBuildTimeline:
    def _make_event(self, ts_str, source="log", level="INFO", content="test"):
        return {
            "timestamp": datetime.fromisoformat(ts_str),
            "source": source, "level": level,
            "content": content, "patterns": [], "raw": content,
        }

    def test_events_sorted_by_timestamp(self):
        events = [
            self._make_event("2024-03-15T02:10:00", content="later"),
            self._make_event("2024-03-15T02:08:00", content="earlier"),
        ]
        timeline = build_timeline([events])
        assert timeline[0]["content"] == "earlier"
        assert timeline[1]["content"] == "later"

    def test_merges_multiple_sources(self):
        log_events    = [self._make_event("2024-03-15T02:08:00", source="log")]
        slack_events  = [self._make_event("2024-03-15T02:09:00", source="slack")]
        ticket_events = [self._make_event("2024-03-15T02:10:00", source="ticket")]
        timeline = build_timeline([log_events, slack_events, ticket_events])
        assert len(timeline) == 3
        assert [e["source"] for e in timeline] == ["log", "slack", "ticket"]

    def test_deduplicates_same_ts_and_content(self):
        ev1 = self._make_event("2024-03-15T02:08:00", content="identical message")
        ev2 = self._make_event("2024-03-15T02:08:00", content="identical message")
        timeline = build_timeline([[ev1, ev2]])
        assert len(timeline) == 1

    def test_handles_empty_sources(self):
        assert build_timeline([]) == []
        assert build_timeline([[]]) == []

    def test_events_have_timestamp_str_field(self):
        events = [self._make_event("2024-03-15T02:08:33")]
        timeline = build_timeline([events])
        assert "timestamp_str" in timeline[0]
        assert timeline[0]["timestamp_str"] == "2024-03-15 02:08:33"

    def test_events_have_sequential_event_ids(self):
        events = [
            self._make_event("2024-03-15T02:08:00"),
            self._make_event("2024-03-15T02:09:00"),
            self._make_event("2024-03-15T02:10:00"),
        ]
        timeline = build_timeline([events])
        assert [e["event_id"] for e in timeline] == [0, 1, 2]

    def test_skips_events_without_timestamp(self):
        bad_event = {"source": "log", "level": "INFO", "content": "no ts", "patterns": []}
        good_event = self._make_event("2024-03-15T02:08:00")
        timeline = build_timeline([[bad_event, good_event]])
        assert len(timeline) == 1