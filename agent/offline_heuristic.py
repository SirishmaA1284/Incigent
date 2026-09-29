"""
agent/offline_heuristic.py
Rule-based post-mortem used when the AI agent cannot run (no API, no key).

Results are always labelled "Offline heuristic mode". This module never
recalls historical incidents: similar_past_incidents is always empty.
"""

OFFLINE_MODE = "offline_heuristic"
OFFLINE_LABEL = "Offline heuristic mode"

ANALYSIS_MODE_LABELS = {
    "llm_agent": "AI agent analysis",
    "agent_error": "AI agent failed",
    "no_evidence": "No evidence provided",
    OFFLINE_MODE: OFFLINE_LABEL,
}


def offline_postmortem(iid, tl, reason=None):
    ap = []
    for ev in tl: ap.extend(ev.get("patterns", []))
    pc = {}
    for p in ap: pc[p] = pc.get(p, 0) + 1
    critical = [e for e in tl if e["level"] in ("CRITICAL", "ERROR")]
    recovery = [e for e in tl if "RECOVERY" in e.get("patterns", []) or "CIRCUIT_BREAKER_CLOSE" in e.get("patterns", [])]
    # Only a deployment that happened before the first failure can explain it;
    # anything later (e.g. a remediation restart) is not a candidate cause.
    first_failure = critical[0]["timestamp"] if critical else None
    deploys = [e for e in tl if "DEPLOY" in e.get("patterns", [])
               and (first_failure is None or e["timestamp"] <= first_failure)]
    dur = int((tl[-1]["timestamp"] - tl[0]["timestamp"]).total_seconds() / 60) if tl else 0
    hp = "DB_POOL_EXHAUSTED" in pc; ho = "OOM_KILL" in pc
    hcb = "CIRCUIT_BREAKER_OPEN" in pc; hl = "DB_IDLE_LEAK" in pc; hhf = "HEALTH_CHECK_FAIL" in pc
    ev6 = [f"[E{e.get('event_id', '?')}] [{e['timestamp_str']}] [{e['source'].upper()}] {e['content']}" for e in critical[:6]]
    if recovery: ev6.append(f"[E{recovery[0].get('event_id', '?')}] [{recovery[0]['timestamp_str']}] [{recovery[0]['source'].upper()}] {recovery[0]['content']}")
    if hp and hl:
        rc = "DB connection pool exhausted due to connection leak (idle_in_transaction) caused by improper transaction handling in application code."
        sm = (f"A production outage lasting ~{dur} minutes was triggered by database connection pool exhaustion. "
              f"Connections were held in idle_in_transaction state, "
              f"causing cascading errors across downstream services.")
        fa = ["Application code failed to close DB transactions on exception paths",
              "No early-warning alert at 70–80% connection pool capacity"]
        if "DB_REPLICATION_LAG" in pc or "DB_REPLICATION_BROKEN" in pc:
            fa.append("Replication lag cascaded as primary became overwhelmed")
        if hcb:
            fa.append("Circuit breaker opened, cutting off all DB-dependent services")
        if deploys: fa.insert(0, f"Deployment at {deploys[0]['timestamp_str']} preceded the failure and may have introduced the connection leak")
        ac = [{"what": "Fix transaction context manager in exception handlers", "owner": "Backend Team", "priority": "P1"},
              {"what": "Add connection pool alert at 70% and 90% thresholds", "owner": "SRE", "priority": "P1"},
              {"what": "Add DB pool checks to deployment PR template", "owner": "Engineering Lead", "priority": "P2"},
              {"what": "Implement connection pool monitoring dashboard", "owner": "SRE", "priority": "P2"},
              {"what": "Run regression suite for DB connection handling before merge", "owner": "QA", "priority": "P3"}]
        cf = "high"
    elif ho:
        rc = "Out-of-memory event caused service termination and cascading failures."
        sm = f"Service outage of ~{dur} minutes caused by an OOM kill terminating critical processes."
        fa = ["Insufficient memory limits configured", "Memory leak in application", "No memory pressure alerting"]
        ac = [{"what": "Tune memory limits in deployment manifests", "owner": "SRE", "priority": "P1"},
              {"what": "Profile application for memory leaks", "owner": "Backend Team", "priority": "P1"},
              {"what": "Add memory pressure alerts at 80% and 90%", "owner": "SRE", "priority": "P2"}]
        cf = "high"
    elif hp:
        rc = "Database connection pool exhausted, refusing new connections and causing service-wide outage."
        sm = f"Production incident spanning {dur} minutes. DB connection pool reached 100% capacity."
        fa = ["Connection pool capacity too low", "No pool capacity alerting", "Missing idle connection timeout"]
        ac = [{"what": "Increase connection pool size and add idle timeouts", "owner": "SRE", "priority": "P1"},
              {"what": "Add pool utilisation alerts", "owner": "SRE", "priority": "P1"}]
        cf = "medium"
    elif hcb and hhf:
        rc = "Upstream service health checks failed repeatedly, tripping the circuit breaker and halting traffic."
        sm = f"Circuit breaker opened after consecutive health check failures over {dur} minutes."
        fa = ["Health check endpoint not resilient to transient errors", "Circuit breaker threshold too low", "No fallback/degraded mode"]
        ac = [{"what": "Review health check endpoint for false negatives", "owner": "Backend Team", "priority": "P1"},
              {"what": "Implement graceful degradation responses", "owner": "Backend Team", "priority": "P2"},
              {"what": "Tune circuit breaker thresholds", "owner": "SRE", "priority": "P2"}]
        cf = "medium"
    else:
        rc = "Root cause requires further investigation — patterns detected but causality unclear."
        sm = f"An incident spanning {dur} minutes was detected. Automated analysis found anomalies but a definitive root cause could not be determined."
        fa = ["Insufficient log coverage", "Multiple overlapping failure signals"]
        ac = [{"what": "Gather additional metrics for the incident window", "owner": "SRE", "priority": "P1"},
              {"what": "Correlate with APM traces", "owner": "Backend Team", "priority": "P1"}]
        cf = "low"
    nar = ""
    if tl:
        nar = (f"Incident began at {tl[0]['timestamp_str']} with early warning signals. "
               f"Critical failures peaked between {critical[0]['timestamp_str'] if critical else '?'} and {critical[-1]['timestamp_str'] if critical else '?'}. ")
        if recovery: nar += f"Recovery confirmed at {recovery[0]['timestamp_str']}. "
        nar += f"Timeline spans {len(tl)} events across {dur} minutes."
    return {"incident_id": iid, "analysis_mode": OFFLINE_MODE, "analysis_mode_label": OFFLINE_LABEL,
            "fallback_reason": reason,
            "summary": sm, "timeline_reconstruction": nar, "root_cause": rc,
            "contributing_factors": fa, "evidence_citations": ev6, "action_items": ac,
            "similar_past_incidents": [], "confidence": cf, "patterns_detected": pc,
            "event_count": len(tl), "duration_minutes": dur,
            "historical_memory": {"status": "not_consulted", "backend": "none", "query": "",
                                  "memories": [], "error": "offline heuristic mode does not use memory"}}


def analyze_with_fallback(call_api, iid, tl):
    """
    Run call_api(); if it raises, return an offline heuristic post-mortem that
    records why the AI agent did not run. Never returns an offline result
    without the offline label.
    """
    try:
        pm = call_api()
    except Exception as e:
        return offline_postmortem(iid, tl, reason=f"{type(e).__name__}: {e}")
    # An unlabelled result is not assumed to be AI output.
    pm.setdefault("analysis_mode", "unknown")
    pm["analysis_mode_label"] = ANALYSIS_MODE_LABELS.get(pm["analysis_mode"], "Unknown analysis mode")
    return pm
