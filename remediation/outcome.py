"""
remediation/outcome.py
Decides what a finished remediation may contribute to long-term memory.

Trust rule:
  AI recommendation      != validated operational knowledge
  human approval         != successful remediation
  successful execution   != verified recovery
Only a real human decision + the action actually executed + a PASSED
verification may be retained, and only then is a lesson derived. Anything
else (not executed, execution failed, verification failed) retains nothing.

Memory is advisory: the executor never reads it, and it never authorizes.
"""


def retention_decision(*, approval, execution: dict, verification: dict):
    """Return (retain: bool, reason: str)."""
    if approval is None or approval.status != "consumed" or not approval.decided_by:
        return False, "no consumed human-approved decision"
    if execution.get("status") != "executed":
        return False, f"action not executed ({execution.get('status')})"
    if not verification or not verification.get("verified"):
        return False, "verification failed: outcome is not validated knowledge"
    return True, "human decision + executed action + passed verification"


def record_verified_outcome(recorder, *, approval, execution: dict, verification: dict, profile: dict) -> dict:
    retain, reason = retention_decision(approval=approval, execution=execution, verification=verification)
    if not retain or recorder is None:
        return {"retained": False, "reason": reason if not retain else "long-term memory not configured",
                "outcomes": []}

    incident_id = approval.incident_id
    action = execution["action"]
    what = approval.payload.get("action", {}).get("description") or action["kind"]
    common = {"services": [action["service"]], "pattern_tags": profile.get("pattern_tags", [])}
    passed = [c["name"] for c in verification["checks"] if c["passed"]]
    outcomes = [
        recorder.record_human_decision(
            incident_id, recommendation_id=approval.approval_id, recommendation=what, decision="approved",
            decided_by=approval.decided_by, reason=approval.reason or "approved (no reason given)", **common),
        recorder.record_action(
            incident_id, f"Executed {action['kind']} on {action['service']}: {action}",
            action_id=approval.approval_id, result="executed", performed_by="remediation-executor", **common),
        recorder.record_verification(
            incident_id, f"Verified recovery after {action['kind']} on {action['service']}.",
            check_id=approval.approval_id, recovered=True, details={"checks_passed": ", ".join(passed)}, **common),
        recorder.record_lesson(
            incident_id,
            f"For {', '.join(common['pattern_tags']) or 'this failure'} on {action['service']}, "
            f"{action['kind']} ({action}) was approved by a human and verified to restore service.",
            lesson_id=approval.approval_id, source="verified remediation", **common),
    ]
    return {"retained": True, "reason": reason, "outcomes": [o.to_dict() for o in outcomes]}
