"""
evaluation/naive_baseline.py

A simple keyword-matching baseline for InfraLens.
Used as a lower-bound comparison against the LLM agent:
if the agent doesn't beat this, something is wrong.

Usage:
    from evaluation.naive_baseline import naive_analyze
    result = naive_analyze(timeline)
"""

from collections import Counter
from typing import List

# Keyword → likely root-cause label
_CAUSE_KEYWORDS = {
    "pool exhausted":       "DB connection pool exhausted",
    "max_connections":      "DB connection pool exhausted",
    "idle_in_transaction":  "Connection leak (idle-in-transaction)",
    "replication lag":      "DB replication lag",
    "oom":                  "Out-of-memory kill",
    "out of memory":        "Out-of-memory kill",
    "circuit breaker":      "Downstream circuit breaker opened",
    "timeout":              "Request timeout / latency spike",
    "deploy":               "Recent deployment",
    "rollback":             "Rollback performed",
    "health check":         "Health-check failures",
    "503":                  "HTTP 503 errors",
    "disk":                 "Disk pressure",
    "cpu":                  "CPU saturation",
}

_SEVERITY_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARN": 2, "INFO": 3, "DEBUG": 4}


def naive_analyze(timeline: List[dict]) -> dict:
    """
    Scan the timeline for known bad-pattern keywords and return a
    best-guess root cause + a list of key events.

    Parameters
    ----------
    timeline : list of enriched events from build_timeline()

    Returns
    -------
    dict with keys: root_cause, contributing_factors, key_events, confidence
    """
    if not timeline:
        return {
            "root_cause": "No events to analyse.",
            "contributing_factors": [],
            "key_events": [],
            "confidence": "none",
        }

    cause_hits: Counter = Counter()
    key_events = []

    for ev in timeline:
        text = (ev.get("content") or "").lower()
        level = ev.get("level", "INFO")

        # Collect high-severity events as key evidence
        if _SEVERITY_ORDER.get(level, 9) <= _SEVERITY_ORDER["ERROR"]:
            key_events.append({
                "timestamp": ev.get("timestamp_str", ""),
                "source":    ev.get("source", ""),
                "level":     level,
                "content":   ev.get("content", "")[:200],
            })

        for kw, label in _CAUSE_KEYWORDS.items():
            if kw in text:
                cause_hits[label] += 1

    # Most common cause wins
    if cause_hits:
        root_cause, _ = cause_hits.most_common(1)[0]
        contributing = [c for c, _ in cause_hits.most_common() if c != root_cause]
        confidence = "medium" if cause_hits.most_common(1)[0][1] >= 3 else "low"
    else:
        root_cause = "Unknown — no matching patterns found."
        contributing = []
        confidence = "none"

    return {
        "root_cause":           root_cause,
        "contributing_factors": contributing[:5],
        "key_events":           key_events[:20],
        "confidence":           confidence,
    }