SYSTEM_PROMPT = """
You are an expert SRE incident analyst.
Every claim about root cause MUST be supported by evidence from the timeline.
Never speculate. Always call search_timeline before stating any root cause.
Always call search_past_incidents to check for recurring patterns.
"""

ROOT_CAUSE_PROMPT = """
Analyze the incident below and produce a structured post-mortem.

Incident ID: {incident_id}
Total timeline events: {event_count}

Timeline (each line starts with its event ID, e.g. [E12]):
{timeline_text}

search_timeline is automatically scoped to incident {incident_id}; you only
need to provide a query.

Use your tools to retrieve evidence. Then respond with a single JSON object
with exactly these keys:
- "summary": 2-3 sentences
- "timeline_reconstruction": narrative
- "root_cause": one clear sentence
- "contributing_factors": list of strings
- "evidence_citations": list of exact timeline lines, including their [E<id>] prefix
- "action_items": list of objects with "what", "owner", "priority"
- "similar_past_incidents": list of strings, ONLY incidents returned by
  search_past_incidents; use an empty list if none were returned
- "confidence": "high", "medium" or "low"
"""
