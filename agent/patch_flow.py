"""
agent/patch_flow.py
Stage 6: approval-gated candidate patch preparation inside the agent graph.

    analyst -> prepare_patch -> request_patch_approval -> await_patch_approval (interrupt)
            -> validate_patch_approval -> apply_patch -> propose_action -> ...

- prepare_patch: READ-ONLY. Parses the analyst's raw proposal (untrusted),
  validates it strictly (intent, workspace placement, pinned base, clean tree)
  and builds the canonical repository_apply payload. Nothing is written.
- request_patch_approval: creates the pending repository_apply approval (the
  existing Stage 3 ApprovalService; its action hash binds the exact patch
  intent, pinned base SHA, target and incident).
- await_patch_approval: pauses with interrupt(). Pure: no writes, no candidate.
- validate_patch_approval: the resume value may only NAME this workflow's own
  repository_apply approval; the decision is read from the approval database.
- apply_patch: PatchExecutor re-checks everything, validates and CONSUMES the
  approval (single use), and only then materializes the pinned candidate,
  applies, diff-checks and verifies.

Without an approval service + checkpointer, no approval can be requested and
nothing is ever applied ("approval_unavailable"). Approval and decision
functions are never exposed to the model as tools.
"""

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from agent.approval_flow import _valid_reference, approval_service
from approvals import ApprovalDenied
from patching import APPROVAL_KIND, PatchExecutor, PatchResult, prepare_patch_request


def _configurable(config):
    return (config or {}).get("configurable") or {}


def prepare_patch_node(state, config: RunnableConfig):
    cfg = _configurable(config)
    inspector, patch_config = cfg.get("repository_inspector"), cfg.get("patch_config")
    incident_id = state.get("incident_id", "")
    if inspector is None or patch_config is None:
        return {"proposed_patch": None,
                "patch_candidate": {"status": "skipped", "reason": "repository patching is not configured"}}
    from agent.graph import _extract_json_from_text   # local import avoids a cycle

    inspection = state.get("repository_inspection") or {}
    known = {f["evidence_id"] for f in inspection.get("findings", [])}
    pinned = inspection.get("head_sha") or ""

    raw = None
    last = state["messages"][-1]
    if hasattr(last, "content"):
        try:
            raw = _extract_json_from_text(last.content).get("patch_proposal")
        except Exception:
            raw = None

    try:
        result, payload = prepare_patch_request(
            proposal=raw, inspector=inspector, incident_id=incident_id,
            known_repository_ids=known, config=patch_config, pinned_base_sha=pinned)
    except Exception as e:   # pipeline errors never take the workflow down
        result, payload = PatchResult(status="patch_rejected", incident_id=incident_id,
                                      target_repository=str(getattr(inspector, "root", "")),
                                      reason=f"patch pipeline error: {type(e).__name__}: {e}"), None
    if payload is not None and approval_service(config) is None:
        result.status = "approval_unavailable"
        result.reason = ("a repository_apply approval is required, but no approval service "
                         "and checkpointer are configured; nothing was applied")
        payload = None
    return {"proposed_patch": payload, "patch_candidate": result.to_dict()}


def route_after_patch_preparation(state):
    return "request_patch_approval" if state.get("proposed_patch") else "propose_action"


def request_patch_approval_node(state, config: RunnableConfig):
    service = approval_service(config)
    payload = state["proposed_patch"]
    approval = service.request_approval(state["incident_id"], APPROVAL_KIND, payload)
    return {"patch_approval_request": {
        "approval_id": approval.approval_id, "incident_id": approval.incident_id,
        "kind": approval.kind, "action_hash": approval.action_hash,
        "status": approval.status, "expires_at": approval.expires_at,
        "base_sha": payload["base_sha"], "file": payload["patch"]["file"],
    }, "patch_candidate": {**(state.get("patch_candidate") or {}),
                           "approval_id": approval.approval_id}}


def await_patch_approval_node(state):
    """Pause for a human decision. Pure: safe to re-run on resume; no candidate
    is created and nothing is written here."""
    req = state["patch_approval_request"]
    resume_value = interrupt({
        "type": "approval_required",
        "approval_id": req["approval_id"],
        "incident_id": req["incident_id"],
        "kind": req["kind"],
        "action_hash": req["action_hash"],
        "expires_at": req["expires_at"],
        "resume_with": {"approval_id": req["approval_id"]},
    })
    return {"patch_approval_resume": resume_value}


def validate_patch_approval_node(state, config: RunnableConfig):
    service = approval_service(config)
    req, payload = state["patch_approval_request"], state["proposed_patch"]
    approval_id = req["approval_id"]
    if service is None:
        return {"patch_approval_result": {"status": "awaiting", "error": "approval_service_unavailable"}}
    # Only THIS workflow's own repository_apply approval may be named.
    if not _valid_reference(state.get("patch_approval_resume"), approval_id):
        service.record_denial(approval_id, code="invalid_resume_reference", actor="resume-client")
        return {"patch_approval_result": {"status": "awaiting", "error": "invalid_resume_reference"}}
    try:
        approval = service.validate(approval_id, incident_id=state["incident_id"],
                                    kind=APPROVAL_KIND, payload=payload)
    except ApprovalDenied as denied:
        if denied.code == "pending":
            return {"patch_approval_result": {"status": "awaiting", "error": "approval_still_pending"}}
        return {"patch_approval_result": {"status": "denied", "code": denied.code, "applied": False},
                "patch_candidate": {**(state.get("patch_candidate") or {}), "status": "approval_denied",
                                    "reason": f"repository_apply approval {denied.code}"}}
    # Validation is not permission to act: consumption happens inside the
    # executor, immediately before the first write.
    return {"patch_approval_result": {"status": "validated", "approval_id": approval.approval_id,
                                      "decided_by": approval.decided_by,
                                      "action_hash": approval.action_hash, "applied": False}}


def route_after_patch_validation(state):
    status = (state.get("patch_approval_result") or {}).get("status")
    if status == "awaiting":
        return "await_patch_approval"
    if status == "validated":
        return "apply_patch"
    return "propose_action"


def apply_patch_node(state, config: RunnableConfig):
    cfg = _configurable(config)
    req = state["patch_approval_request"]
    inspector, patch_config, service = cfg.get("repository_inspector"), cfg.get("patch_config"), approval_service(config)
    if inspector is None or patch_config is None or service is None:
        result = PatchResult(status="not_executable", incident_id=state["incident_id"],
                             approval_id=req["approval_id"],
                             reason="repository patching is not configured on resume; nothing was applied")
        return {"patch_candidate": result.to_dict()}
    try:
        result = PatchExecutor(service, inspector, patch_config).execute(
            req["approval_id"], incident_id=state["incident_id"], payload=state["proposed_patch"])
    except Exception as e:   # executor errors never take the workflow down
        result = PatchResult(status="patch_application_failed", incident_id=state["incident_id"],
                             approval_id=req["approval_id"],
                             reason=f"patch executor error: {type(e).__name__}: {e}")
    return {"patch_candidate": result.to_dict()}
