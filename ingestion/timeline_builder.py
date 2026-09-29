"""
InfraLens Timeline Builder — ingestion/timeline_builder.py
Merges events from multiple sources, deduplicates, sorts, and enriches with gap detection.
"""

from datetime import datetime, timedelta
from typing import List


def build_timeline(sources: List[list]) -> list:
    """
    Merge, deduplicate, and sort events from multiple parsed sources.

    Edge cases:
    - Empty sources list
    - Sources with zero events
    - Duplicate events (same timestamp + content from different sources)
    - Events missing required fields
    - Very large timelines (>10k events — sampled)
    """
    all_events = []

    for source_events in sources:
        if not source_events or not isinstance(source_events, list):
            continue
        for ev in source_events:
            if not isinstance(ev, dict):
                continue
            # Require at minimum a timestamp
            if "timestamp" not in ev or ev["timestamp"] is None:
                continue
            all_events.append(ev)

    if not all_events:
        return []

    # Sort by timestamp, then source priority (log > ticket > slack for same ts)
    SOURCE_PRIORITY = {"log": 0, "ticket": 1, "slack": 2}
    all_events.sort(key=lambda e: (
        e["timestamp"],
        SOURCE_PRIORITY.get(e.get("source", "log"), 9),
    ))

    # Deduplicate: same content + same timestamp (within 1 second) = skip
    seen = set()
    deduped = []
    for ev in all_events:
        # Round to second for dedup key
        ts_key = ev["timestamp"].replace(microsecond=0).isoformat()
        content_key = (ev.get("content") or ev.get("raw") or "")[:120]
        key = (ts_key, content_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)

    # Sample if too large (keep all CRITICAL/ERROR, sample INFO)
    MAX_EVENTS = 5000
    if len(deduped) > MAX_EVENTS:
        high_priority = [e for e in deduped if e.get("level") in ("CRITICAL", "ERROR")]
        low_priority  = [e for e in deduped if e.get("level") not in ("CRITICAL", "ERROR")]
        # Keep all high priority, sample low priority
        sample_count = MAX_EVENTS - len(high_priority)
        if sample_count > 0:
            step = max(1, len(low_priority) // sample_count)
            low_priority = low_priority[::step][:sample_count]
        deduped = sorted(high_priority + low_priority, key=lambda e: e["timestamp"])

    # Assign event IDs and formatted timestamps
    for i, event in enumerate(deduped):
        event["event_id"] = i
        event["timestamp_str"] = event["timestamp"].strftime("%Y-%m-%d %H:%M:%S")

    # Detect silence gaps (>5 min with no events) and inject gap markers
    # (useful for the agent to understand quiet periods)
    enriched = []
    for i, ev in enumerate(deduped):
        if i > 0:
            gap = (ev["timestamp"] - deduped[i-1]["timestamp"]).total_seconds()
            if gap > 300:  # 5+ minute gap
                gap_min = int(gap / 60)
                enriched.append({
                    "event_id": f"gap_{i}",
                    "timestamp": deduped[i-1]["timestamp"] + timedelta(seconds=1),
                    "timestamp_str": (deduped[i-1]["timestamp"] + timedelta(seconds=1)).strftime("%Y-%m-%d %H:%M:%S"),
                    "source": "system",
                    "level": "INFO",
                    "content": f"[SILENCE GAP] No events for {gap_min} minutes",
                    "component": None,
                    "patterns": [],
                    "raw": f"gap:{gap_min}m",
                })
        enriched.append(ev)

    # Re-assign event IDs after gap injection
    for i, ev in enumerate(enriched):
        ev["event_id"] = i

    return enriched