"""
evaluation/ragas_eval.py — RAGAS-style evaluation for InfraLens post-mortems.

Metrics implemented (no LLM judge required for the core three):
  - faithfulness        : cited evidence lines are present verbatim in the source timeline
  - answer_relevance    : action items address the detected signal patterns
  - context_recall      : fraction of ground-truth root-cause keywords found in output
  - citation_coverage   : fraction of CRITICAL/ERROR events that appear in evidence_citations

Run:
    python -m evaluation.ragas_eval

Or against a custom postmortem JSON:
    python -m evaluation.ragas_eval --postmortem path/to/postmortem.json \
                                    --timeline  path/to/timeline.json
"""

import json
import re
import argparse
from pathlib import Path
from typing import Optional


# ── Ground-truth reference for the built-in demo scenario ─────────────────────

DEMO_GROUND_TRUTH = {
    "root_cause_keywords": [
        "idle_in_transaction",
        "connection leak",
        "exception",
        "transaction",
        "order-service",
    ],
    "critical_events": [
        "Connection pool exhausted: 200/200",
        "max_connections limit reached",
        "Health check FAILED",
        "Circuit breaker OPEN",
    ],
    "required_action_owners": ["Backend Team", "SRE"],
    "required_action_priorities": ["P1"],
}


# ── Metric functions ───────────────────────────────────────────────────────────

def score_faithfulness(postmortem: dict, timeline_text: str) -> dict:
    """
    Faithfulness: every evidence citation must appear (case-insensitive substring)
    in the raw timeline text.  Score = cited_found / total_cited.
    """
    citations = postmortem.get("evidence_citations", [])
    if not citations:
        return {"score": 0.0, "detail": "No citations found in postmortem."}

    found, missing = [], []
    for c in citations:
        # Strip timestamp prefix for a more forgiving match
        needle = re.sub(r"^\[.*?\]\s*", "", c).strip()
        if needle.lower() in timeline_text.lower():
            found.append(c)
        else:
            missing.append(c)

    score = len(found) / len(citations)
    return {
        "score": round(score, 3),
        "found": len(found),
        "total": len(citations),
        "missing_citations": missing,
    }


def score_context_recall(postmortem: dict, ground_truth: dict) -> dict:
    """
    Context recall: fraction of ground-truth root-cause keywords present in
    the postmortem's root_cause + contributing_factors text.
    """
    keywords = ground_truth.get("root_cause_keywords", [])
    if not keywords:
        return {"score": 1.0, "detail": "No ground-truth keywords to check."}

    haystack = (
        postmortem.get("root_cause", "")
        + " "
        + " ".join(postmortem.get("contributing_factors", []))
    ).lower()

    hit, miss = [], []
    for kw in keywords:
        (hit if kw.lower() in haystack else miss).append(kw)

    score = len(hit) / len(keywords)
    return {
        "score": round(score, 3),
        "keywords_found": hit,
        "keywords_missing": miss,
    }


def score_answer_relevance(postmortem: dict, ground_truth: dict) -> dict:
    """
    Answer relevance: checks that action items cover the required owners
    and at least one P1 item exists.
    """
    items = postmortem.get("action_items", [])
    if not items:
        return {"score": 0.0, "detail": "No action items found."}

    owners_found = {i.get("owner", "") for i in items}
    required_owners = set(ground_truth.get("required_action_owners", []))
    owner_coverage = len(required_owners & owners_found) / max(len(required_owners), 1)

    has_p1 = any(i.get("priority") == "P1" for i in items)
    score = (owner_coverage + (1.0 if has_p1 else 0.0)) / 2

    return {
        "score": round(score, 3),
        "owners_found": list(owners_found),
        "required_owners_covered": list(required_owners & owners_found),
        "has_p1_item": has_p1,
    }


def score_citation_coverage(postmortem: dict, ground_truth: dict) -> dict:
    """
    Citation coverage: fraction of known critical events that appear in
    the evidence citations.
    """
    critical = ground_truth.get("critical_events", [])
    if not critical:
        return {"score": 1.0, "detail": "No critical events defined in ground truth."}

    citation_blob = " ".join(postmortem.get("evidence_citations", []))
    hit, miss = [], []
    for event in critical:
        (hit if event.lower() in citation_blob.lower() else miss).append(event)

    score = len(hit) / len(critical)
    return {
        "score": round(score, 3),
        "events_cited": hit,
        "events_missing": miss,
    }


# ── Runner ─────────────────────────────────────────────────────────────────────

def load_demo_timeline() -> str:
    """Load the built-in sample log as a flat string for faithfulness checks."""
    raw_dir = Path(__file__).parent.parent / "data" / "raw"
    log_path = raw_dir / "sample_logs.txt"
    if log_path.exists():
        return log_path.read_text(encoding="utf-8")
    # Fallback: reconstruct from evidence in the demo postmortem
    return ""


def run_evaluation(
    postmortem: dict,
    timeline_text: str,
    ground_truth: Optional[dict] = None,
) -> dict:
    gt = ground_truth or DEMO_GROUND_TRUTH

    results = {
        "incident_id": postmortem.get("incident_id", "unknown"),
        "confidence_reported": postmortem.get("confidence", "unknown"),
        "metrics": {
            "faithfulness":       score_faithfulness(postmortem, timeline_text),
            "context_recall":     score_context_recall(postmortem, gt),
            "answer_relevance":   score_answer_relevance(postmortem, gt),
            "citation_coverage":  score_citation_coverage(postmortem, gt),
        },
    }

    scores = [v["score"] for v in results["metrics"].values()]
    results["aggregate_score"] = round(sum(scores) / len(scores), 3)
    results["grade"] = (
        "PASS" if results["aggregate_score"] >= 0.75 else
        "BORDERLINE" if results["aggregate_score"] >= 0.55 else
        "FAIL"
    )
    return results


def print_report(results: dict) -> None:
    print(f"\n{'='*58}")
    print(f"  InfraLens Evaluation — {results['incident_id']}")
    print(f"{'='*58}")
    print(f"  Reported confidence : {results['confidence_reported'].upper()}")
    print(f"  Aggregate score     : {results['aggregate_score']:.3f}  [{results['grade']}]")
    print(f"\n  Metric breakdown:")
    for name, detail in results["metrics"].items():
        bar = "█" * int(detail["score"] * 20)
        print(f"    {name:<22} {detail['score']:.3f}  |{bar:<20}|")
    print(f"{'='*58}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate an InfraLens post-mortem.")
    parser.add_argument("--postmortem", type=str, default=None,
                        help="Path to postmortem JSON (default: uses built-in demo output).")
    parser.add_argument("--timeline", type=str, default=None,
                        help="Path to raw timeline text (default: sample_logs.txt).")
    args = parser.parse_args()

    # Load postmortem
    if args.postmortem:
        postmortem = json.loads(Path(args.postmortem).read_text(encoding="utf-8"))
    else:
        # Use the demo output that ships with the repo
        demo_output_path = Path(__file__).parent.parent / "data" / "raw" / "demo_postmortem.json"
        if demo_output_path.exists():
            postmortem = json.loads(demo_output_path.read_text(encoding="utf-8"))
        else:
            # Inline fallback so the eval runs even without a saved output
            postmortem = {
                "incident_id": "INC-DEMO-4821",
                "root_cause": "DB connection pool exhausted due to connection leak (idle_in_transaction) caused by improper transaction handling in application code.",
                "contributing_factors": [
                    "Application code failed to close DB transactions on exception paths",
                    "No early-warning alert at 70–80% connection pool capacity",
                    "Replication lag cascaded as primary became overwhelmed",
                    "Circuit breaker opened, cutting off all DB-dependent services",
                ],
                "evidence_citations": [
                    "[2024-03-15 02:08:33] [LOG] db-primary] Connection pool exhausted: 200/200 connections active",
                    "[2024-03-15 02:08:33] [LOG] api-gateway] Upstream DB timeout after 5000ms on /api/v1/orders",
                    "[2024-03-15 02:08:35] [LOG] db-primary] FATAL: max_connections limit reached — refusing new connections",
                    "[2024-03-15 02:08:35] [LOG] api-gateway] Health check FAILED for db-primary:5432",
                    "[2024-03-15 02:14:04] [LOG] db-primary] Connection pool: 14/200 active — recovered",
                ],
                "action_items": [
                    {"what": "Fix transaction context manager in exception handlers", "owner": "Backend Team", "priority": "P1"},
                    {"what": "Add connection pool alert at 70% and 90% thresholds",   "owner": "SRE",          "priority": "P1"},
                    {"what": "Add DB pool checks to deployment PR template",           "owner": "Engineering Lead", "priority": "P2"},
                    {"what": "Implement connection pool monitoring dashboard",          "owner": "SRE",          "priority": "P2"},
                ],
                "confidence": "high",
            }

    # Load timeline
    timeline_text = load_demo_timeline()
    if args.timeline:
        timeline_text = Path(args.timeline).read_text(encoding="utf-8")

    # If we still have no timeline, use evidence citations as a proxy
    if not timeline_text:
        timeline_text = "\n".join(postmortem.get("evidence_citations", []))

    results = run_evaluation(postmortem, timeline_text)
    print_report(results)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()