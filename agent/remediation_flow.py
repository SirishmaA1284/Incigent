"""
agent/remediation_flow.py
Graph steps after an approval is validated:

    execute_remediation -> verify_remediation -> record_outcome

execute_remediation calls RemediationExecutor.execute(), which does
validate -> consume -> apply in one call, so "the approval was consumed" never
has to be carried through checkpointed state before acting. The approval ID,
incident and payload all come from graph state (the exact action whose hash was
approved), never from the resume value.

If this node re-runs after a crash, consume() fails ("consumed") and the
simulator refuses a second application for the same approval: at most once.
But execution and checkpoint persistence are not atomic together, so after a
crash between apply and checkpoint the persisted result can under-report what
happened (see remediation/executor.py). Not exactly-once.
"""

from langchain_core.runnables import RunnableConfig

from remediation.outcome import record_verified_outcome


def _configurable(config):
    return (config or {}).get("configurable") or {}


def execute_remediation_node(state, config: RunnableConfig):
    executor = _configurable(config).get("remediation_executor")
    req = state["approval_request"]
    result = executor.execute(req["approval_id"], incident_id=state["incident_id"],
                              payload=state["proposed_action"])
    return {"remediation": result.to_dict()}


def route_after_execution(state):
    """Only an executed action is verified; every path ends in record_outcome,
    which retains nothing unless verification passed."""
    return "verify_remediation" if (state.get("remediation") or {}).get("status") == "executed" else "record_outcome"


def verify_remediation_node(state, config: RunnableConfig):
    verifier = _configurable(config).get("remediation_verifier")
    execution = state["remediation"]
    if verifier is None:
        return {"verification": {"verified": False, "failure_reason": "no verifier configured",
                                 "incident_id": state["incident_id"], "action": execution["action"],
                                 "checks": [], "observed_state": None}}
    result = verifier.verify(incident_id=state["incident_id"], action=execution["action"],
                             before=execution["before"])
    return {"verification": result.to_dict()}


def record_outcome_node(state, config: RunnableConfig):
    """Retain validated outcomes only. Memory failures never affect the result."""
    cfg = _configurable(config)
    approvals = cfg.get("approval_service")
    approval = approvals.get(state["approval_request"]["approval_id"]) if approvals else None
    outcome = record_verified_outcome(cfg.get("memory_recorder"), approval=approval,
                                      execution=state.get("remediation") or {},
                                      verification=state.get("verification") or {},
                                      profile=state.get("incident_profile") or {})
    return {"outcome_memory": outcome}
