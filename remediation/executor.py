"""
remediation/executor.py
Executes an APPROVED remediation against the simulator, and nothing else.

Order inside execute(), for every call:
  1. check the approval payload shape and the allowlisted action (no DB, no effects)
  2. check the target exists in this incident's simulated environment
  3. ApprovalService.validate()   - fails fast with a clear reason
  4. ApprovalService.consume()    - THE AUTHORIZATION BOUNDARY: atomic, succeeds at most once
  5. only if consume() returned: apply the action to the simulator
If any step before 5 fails, nothing is applied. A failure after consume() leaves
the approval consumed (fail closed): the action is never retried with it.

Crash consistency (known, accepted limitation for this in-memory demo):
  - consume() gives AT-MOST-ONCE AUTHORIZATION: one approval can authorize one
    execution, never two.
  - Applying the action and persisting the graph checkpoint that records the
    result are NOT one atomic transaction.
  - If the process crashes after apply() but before that checkpoint is saved,
    a later resume finds the approval already consumed (so nothing runs again),
    but the persisted workflow result can under-report what actually happened
    (e.g. "not_authorized: consumed" although the action was applied).
  - This is not exactly-once distributed execution. Persistent execution
    infrastructure will need a durable execution record keyed by approval ID,
    idempotent operations and a reconciliation step.

The executor translates only the three allowlisted actions into simulator calls.
It never builds, interprets or runs commands, and it does not consult long-term
memory (Hindsight) for anything.
"""

from dataclasses import asdict, dataclass, field
from typing import Optional

from approvals import ApprovalDenied
from remediation.actions import InvalidAction, parse_action

APPROVAL_KIND = "operational"
PAYLOAD_KEYS = frozenset({"kind", "incident_id", "action", "remediation", "proposed_by", "source_recommendation"})


@dataclass
class ExecutionResult:
    status: str                    # "executed" | "not_executable" | "not_authorized" | "execution_failed"
    incident_id: str
    approval_id: str
    action: Optional[dict] = None
    reason: Optional[str] = None
    effect: Optional[dict] = None
    before: Optional[dict] = None  # service snapshot taken just before applying
    trace: list = field(default_factory=list)  # ordered steps actually performed

    def to_dict(self) -> dict:
        return asdict(self)


class RemediationExecutor:
    actor = "remediation-executor"

    def __init__(self, approvals, simulator):
        self.approvals = approvals
        self.simulator = simulator

    def execute(self, approval_id: str, *, incident_id: str, payload: dict) -> ExecutionResult:
        result = ExecutionResult("not_executable", incident_id, approval_id)

        # 1. payload shape + allowlisted action (before touching the approval)
        if not isinstance(payload, dict) or not set(payload) <= PAYLOAD_KEYS:
            result.reason = "malformed_payload"
            return result
        if payload.get("kind") != APPROVAL_KIND:
            result.reason = "wrong_approval_kind"
            return result
        if payload.get("incident_id") != incident_id:
            result.reason = "wrong_incident"
            return result
        if "remediation" not in payload:
            result.reason = "no_executable_action"
            return result
        try:
            action = parse_action(payload["remediation"])
        except InvalidAction as e:
            result.reason = f"invalid_action: {e}"
            return result
        result.action = action.to_payload()
        result.trace.append("checked_action")

        # 2. the action must target a service of THIS incident's environment
        if not self.simulator.has_service(incident_id, action.service):
            result.reason = "unknown_service_for_incident"
            return result

        # 3-4. validate, then consume: consume() returning is the only authorization
        try:
            self.approvals.validate(approval_id, incident_id=incident_id, kind=APPROVAL_KIND,
                                    payload=payload, actor=self.actor)
            result.trace.append("validated")
            self.approvals.consume(approval_id, incident_id=incident_id, kind=APPROVAL_KIND,
                                   payload=payload, actor=self.actor)
            result.trace.append("consumed")
        except ApprovalDenied as denied:
            result.status, result.reason = "not_authorized", denied.code
            return result

        # 5. authorized: apply exactly the approved action, once
        try:
            result.before = self.simulator.snapshot(incident_id, action.service)
            result.effect = self.simulator.apply(action, incident_id=incident_id, approval_id=approval_id)
            result.trace.append("executed")
            result.status = "executed"
        except Exception as e:  # consumed but not applied: fail closed, never retried
            result.status, result.reason = "execution_failed", f"{type(e).__name__}: {e}"
        return result
