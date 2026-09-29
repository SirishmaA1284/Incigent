"""
tests/test_parsers.py — Unit tests for the ingestion layer.

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

    def test_leading_component_bracket_is_preserved(self):
        # Regression: content used to become "db-primary] Connection pool ..."
        line = "2024-03-15 02:08:33 ERROR [db-primary] Connection pool exhausted: 200/200 connections active"
        ev = parse_log_line(line)
        assert ev["content"] == "[db-primary] Connection pool exhausted: 200/200 connections active"

    def test_bracketed_timestamp_and_level_leave_no_empty_brackets(self):
        ev = parse_log_line("[2024-03-15 02:08:33] [ERROR] [db] boom")
        assert ev["content"] == "[db] boom"
        assert ev["level"] == "ERROR"

    def test_leading_separators_are_stripped(self):
        ev = parse_log_line("2024-03-15 02:08:33 | ERROR | api: something failed")
        assert ev["content"] == "api: something failed"

    def test_sample_log_has_no_dangling_component_brackets(self):
        from pathlib import Path
        from ingestion.log_parser import parse_log_file
        path = Path(__file__).parent.parent / "data" / "raw" / "sample_logs.txt"
        for ev in parse_log_file(str(path)):
            if ev["component"]:
                assert ev["content"].startswith(f"[{ev['component']}]"), ev["content"]

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


class TestDeployRollbackClassification:
    @staticmethod
    def tags(text):
        return {t for t, _ in detect_patterns(text)}

    def test_transaction_rollback_is_not_a_deployment_rollback(self):
        tags = self.tags("Transaction rollback: DB unavailable — order ORD-928372 NOT charged")
        assert "ROLLBACK" not in tags
        assert "TXN_ROLLBACK" in tags

    @pytest.mark.parametrize("text", [
        "DB transaction was rolled back",
        "rollback of txn 12 due to deadlock",
    ])
    def test_other_transaction_rollback_phrasings(self, text):
        tags = self.tags(text)
        assert "ROLLBACK" not in tags
        assert "TXN_ROLLBACK" in tags

    @pytest.mark.parametrize("text", [
        "Rolled back order-service to v2.4.0",
        "Rollback initiated for release 42",
        "Rolling back deployment of api-gateway",
    ])
    def test_deployment_rollback_still_detected(self, text):
        assert "ROLLBACK" in self.tags(text)

    def test_rolling_restart_is_not_a_deployment(self):
        tags = self.tags("Rolling restart initiated for order-service to apply connection pool fix")
        assert "DEPLOY" not in tags
        assert "ROLLING_RESTART" in tags

    def test_transaction_rollback_does_not_raise_level(self):
        ev = parse_log_line("2024-03-15 02:08:37 INFO [payment-service] Transaction rollback: DB unavailable")
        assert ev["level"] == "INFO"


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

    def test_content_format_is_deterministic(self):
        events = parse_tickets(self.SAMPLE)
        assert events[0]["content"] == (
            "[TICKET INC-4821] P1: Production DB down — DB connection pool exhausted. (status: resolved)"
        )
        assert events[0]["ticket_id"] == "INC-4821"

    def test_ticket_without_id_keeps_plain_label(self):
        ticket = dict(self.SAMPLE[0]); del ticket["id"]
        events = parse_tickets([ticket])
        assert events[0]["content"].startswith("[TICKET] ")
        assert events[0]["ticket_id"] is None

    def test_skips_ticket_with_null_created_at(self):
        ticket = dict(self.SAMPLE[0], created_at=None)
        assert parse_tickets([ticket]) == []

    def test_ticket_id_survives_into_timeline(self):
        timeline = build_timeline([parse_tickets(self.SAMPLE)])
        assert "INC-4821" in timeline[0]["content"]

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