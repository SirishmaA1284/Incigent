"""
evaluation/human_baseline.py — Compare InfraLens output against a human-written
post-mortem for the same incident.

This gives a rough inter-rater agreement score: how closely does the AI output
match what an experienced SRE would write?

Metrics:
  - root_cause_overlap   : token-level Jaccard similarity between root causes
  - action_item_overlap  : how many AI action items match human ones (fuzzy)
  - factor_recall        : fraction of human contributing factors covered by AI

Usage:
    python -m evaluation.human_baseline
"""

import re
from typing import Optional


# ── Human-written baseline for INC-DEMO-4821 ──────────────────────────────────

HUMAN_BASELINE = {
    "incident_id": "INC-DEMO-4821",
    "root_cause": (
        "A connection leak in order-service v2.4.1 caused by missing transaction "
        "cleanup in exception handlers left connections idle in PostgreSQL, "
        "exhausting the connection pool and triggering a cascading outage."
    ),
    "contributing_factors": [
        "Exception paths in order-service did not close database transactions",
        "No alerting on connection pool utilisation above 70%",
        "Replication lag compounded the primary load",
        "Circuit breaker propagated the failure to all downstream services",
    ],
    "action_items": [
        "Fix context manager in order-service exception handlers",
        "Add PagerDuty alert at 70% and 90% pool usage",
        "Gate deployments on DB connection handling tests",
        "Build connection pool dashboard in Grafana",
    ],
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def tokenise(text: str) -> set:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a: str, b: str) -> float:
    ta, tb = tokenise(a), tokenise(b)
    if not ta and not tb:
        return 1.0
    return len(ta & tb) / len(ta | tb)


def best_match_score(candidate: str, pool: list[str]) -> float:
    """Return the highest Jaccard similarity between candidate and any item in pool."""
    if not pool:
        return 0.0
    return max(jaccard(candidate, item) for item in pool)


# ── Metrics ────────────────────────────────────────────────────────────────────

def score_root_cause_overlap(ai: dict, human: dict) -> dict:
    score = jaccard(ai.get("root_cause", ""), human.get("root_cause", ""))
    return {"score": round(score, 3), "threshold": 0.40,
            "pass": score >= 0.40}


def score_action_item_overlap(ai: dict, human: dict, threshold: float = 0.30) -> dict:
    ai_items   = [i.get("what", "") if isinstance(i, dict) else i for i in ai.get("action_items", [])]
    human_items = human.get("action_items", [])
    if not ai_items or not human_items:
        return {"score": 0.0, "pass": False}

    matched = sum(
        1 for item in ai_items
        if best_match_score(item, human_items) >= threshold
    )
    score = matched / max(len(human_items), 1)
    return {
        "score": round(score, 3),
        "matched": matched,
        "human_total": len(human_items),
        "pass": score >= 0.50,
    }


def score_factor_recall(ai: dict, human: dict, threshold: float = 0.25) -> dict:
    ai_factors    = ai.get("contributing_factors", [])
    human_factors = human.get("contributing_factors", [])
    if not human_factors:
        return {"score": 1.0, "pass": True}

    covered = sum(
        1 for hf in human_factors
        if best_match_score(hf, ai_factors) >= threshold
    )
    score = covered / len(human_factors)
    return {
        "score": round(score, 3),
        "covered": covered,
        "human_total": len(human_factors),
        "pass": score >= 0.60,
    }


# ── Runner ─────────────────────────────────────────────────────────────────────

def run_human_baseline(
    ai_postmortem: dict,
    human_postmortem: Optional[dict] = None,
) -> dict:
    human = human_postmortem or HUMAN_BASELINE

    rc   = score_root_cause_overlap(ai_postmortem, human)
    ai_  = score_action_item_overlap(ai_postmortem, human)
    fr   = score_factor_recall(ai_postmortem, human)

    aggregate = round((rc["score"] + ai_["score"] + fr["score"]) / 3, 3)
    return {
        "incident_id": ai_postmortem.get("incident_id", "unknown"),
        "metrics": {
            "root_cause_overlap":   rc,
            "action_item_overlap":  ai_,
            "factor_recall":        fr,
        },
        "aggregate_score": aggregate,
        "grade": "PASS" if aggregate >= 0.55 else "BORDERLINE" if aggregate >= 0.40 else "FAIL",
    }


def print_report(results: dict) -> None:
    print(f"\n{'='*58}")
    print(f"  Human Baseline Comparison — {results['incident_id']}")
    print(f"{'='*58}")
    print(f"  Aggregate score : {results['aggregate_score']:.3f}  [{results['grade']}]")
    print(f"\n  Metric breakdown:")
    for name, detail in results["metrics"].items():
        bar = "█" * int(detail["score"] * 20)
        verdict = "✓" if detail.get("pass") else "✗"
        print(f"    {verdict} {name:<26} {detail['score']:.3f}  |{bar:<20}|")
    print(f"{'='*58}\n")


if __name__ == "__main__":
    import json
    # Default: run against the demo postmortem inline
    demo_ai = {
        "incident_id": "INC-DEMO-4821",
        "root_cause": "DB connection pool exhausted due to connection leak (idle_in_transaction) caused by improper transaction handling in application code.",
        "contributing_factors": [
            "Application code failed to close DB transactions on exception paths",
            "No early-warning alert at 70–80% connection pool capacity",
            "Replication lag cascaded as primary became overwhelmed",
            "Circuit breaker opened, cutting off all DB-dependent services",
        ],
        "action_items": [
            {"what": "Fix transaction context manager in exception handlers", "owner": "Backend Team", "priority": "P1"},
            {"what": "Add connection pool alert at 70% and 90% thresholds", "owner": "SRE", "priority": "P1"},
            {"what": "Add DB pool checks to deployment PR template", "owner": "Engineering Lead", "priority": "P2"},
            {"what": "Implement connection pool monitoring dashboard", "owner": "SRE", "priority": "P2"},
        ],
    }
    results = run_human_baseline(demo_ai)
    print_report(results)
    print(json.dumps(results, indent=2))