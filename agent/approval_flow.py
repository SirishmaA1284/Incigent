"""
agent/approval_flow.py
Human-approval checkpoint for the agent graph:

    analyst -> propose_action -> request_approval -> await_approval (interrupt) -> validate_approval

- propose_action: turns the analysis into a canonical action payload (no side effects).
- request_approval: creates the pending approval record (the only side effect; runs once).
- await_approval: pauses the graph with interrupt(); it has no side effects because
  LangGraph re-runs this node from the start on resume.
- validate_approval: looks the approval up in the application database and checks
  incident, kind, action hash, status and expiry. It never trusts the resume value
  beyond "which approval do you mean", and it executes nothing.

Nothing here consumes an approval or performs an action. A "validated" result
means only that the approval was valid when checked. The rule for any future
executor is: NO CONSEQUENTIAL ACTION MAY EXECUTE UNLESS
ApprovalService.consume() HAS JUST SUCCEEDED for that exact action —
validate -> atomically consume -> execute. Never execute after validation alone.

Current limitation (deliberate for this stage): ONE ANALYSIS -> ONE PROPOSED
ACTION -> ONE APPROVAL. Only the top recommendation (first P1, else first) is
proposed; other action items get no approval and cannot be executed. A future
version must decide whether several actions need separate approvals or whether
an explicitly defined action bundle may share one approval (the hash would then
cover the whole bundle).
"""

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from approvals import ApprovalDenied

OPERATIONAL = "operational"


def approval_service(config: RunnableConfig):
    configurable = (config or {}).get("configurable") or {}
    # Approvals need both the service and a checkpointed thread to pause/resume.
    if configurable.get("thread_id") is None:
        return None
    return configurable.get("approval_service")


def select_proposed_action(incident_id: str, analysis: dict):
    """Canonical payload for the analysis' top recommendation (first P1, else first),
    or None when there is nothing actionable. Built by the application, not the LLM."""
    if analysis.get("_parse_failed"):
        return None
    items = [i for i in (analysis.get("action_items") or []) if isinstance(i, dict) and str(i.get("what", "")).strip()]
    if not items:
        return None
    chosen_index = next((n for n, i in enumerate(items) if str(i.get("priority", "")).upper() == "P1"), 0)
    item = items[chosen_index]
    return {
        "kind": OPERATIONAL,
        "incident_id": incident_id,
        "action": {
            "description": str(item.get("what", "")).strip(),
            "owner": str(item.get("owner", "")).strip(),
            "priority": str(item.get("priority", "")).strip(),
        },
        "proposed_by": "llm_agent",
        "source_recommendation": f"rec-{chosen_index + 1}",
    }


def propose_action_node(state, config: RunnableConfig):
    if approval_service(config) is None:
        return {"proposed_action": None}
    from agent.graph import _extract_json_from_text  # local import: graph imports this module
    analysis = _extract_json_from_text(state["messages"][-1].content)
    return {"proposed_action": select_proposed_action(state["incident_id"], analysis)}


def route_after_proposal(state):
    return "request_approval" if state.get("proposed_action") else "end"


def request_approval_node(state, config: RunnableConfig):
    service = approval_service(config)
    action = state["proposed_action"]
    approval = service.request_approval(state["incident_id"], action["kind"], action)
    return {"approval_request": {
        "approval_id": approval.approval_id, "incident_id": approval.incident_id,
        "kind": approval.kind, "action_hash": approval.action_hash,
        "status": approval.status, "expires_at": approval.expires_at,
    }}


def await_approval_node(state):
    """Pause for a human decision. Pure: safe to re-run on resume."""
    req = state["approval_request"]
    resume_value = interrupt({
        "type": "approval_required",
        "approval_id": req["approval_id"],
        "incident_id": req["incident_id"],
        "kind": req["kind"],
        "action_hash": req["action_hash"],
        "expires_at": req["expires_at"],
        "resume_with": {"approval_id": req["approval_id"]},
    })
    return {"approval_resume": resume_value}


def _valid_reference(resume_value, approval_id) -> bool:
    """The resume value may only name the approval; it carries no decision."""
    return (isinstance(resume_value, dict) and set(resume_value) == {"approval_id"}
            and resume_value["approval_id"] == approval_id)


def validate_approval_node(state, config: RunnableConfig):
    service = approval_service(config)
    req, action = state["approval_request"], state["proposed_action"]
    approval_id = req["approval_id"]
    if service is None:
        return {"approval_result": {"status": "awaiting", "error": "approval_service_unavailable"}}
    if not _valid_reference(state.get("approval_resume"), approval_id):
        service.record_denial(approval_id, code="invalid_resume_reference", actor="resume-client")
        return {"approval_result": {"status": "awaiting", "error": "invalid_resume_reference"}}
    try:
        # The approval ID, incident, kind and payload all come from graph state,
        # never from the resume value.
        approval = service.validate(approval_id, incident_id=state["incident_id"],
                                    kind=action["kind"], payload=action)
    except ApprovalDenied as denied:
        if denied.code == "pending":
            return {"approval_result": {"status": "awaiting", "error": "approval_still_pending"}}
        return {"approval_result": {"status": "denied", "code": denied.code, "executed": False}}
    # Deliberately NOT consumed here: consumption is the executor's one-time
    # handoff right before it acts, and no executor exists yet.
    return {"approval_result": {
        "status": "validated", "approval_id": approval.approval_id, "decided_by": approval.decided_by,
        "action_hash": approval.action_hash, "executed": False,
        "note": "approved and validated; no executor exists yet, nothing was run",
    }}


def route_after_validation(state):
    return "await_approval" if (state.get("approval_result") or {}).get("status") == "awaiting" else "end"
