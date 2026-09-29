"""
agent/repository_flow.py
Read-only repository inspection inside the agent graph.

    record_intake -> inspect_repository -> analyst

The inspector is supplied by the application (config["configurable"]
["repository_inspector"]) for one explicitly configured target repository; the
model never chooses a root or a path. Repository evidence is CURRENT evidence
and is presented separately from both the incident timeline and historical
memory. It is never retained to long-term memory, and there is no route from a
patch proposal to any file change.
"""

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from repo_inspection import (
    InvalidProposal, collect_repository_evidence, format_repository_context, normalize_findings,
    validate_patch_proposal,
)


def _inspector(config):
    return ((config or {}).get("configurable") or {}).get("repository_inspector")


def inspect_repository_node(state, config: RunnableConfig):
    inspector = _inspector(config)
    if inspector is None:
        inspection = {"status": "not_configured", "findings": [], "error": "no target repository configured"}
    else:
        timeline = [{"content": line} for line in (state.get("current_evidence") or "").splitlines()]
        try:
            inspection = collect_repository_evidence(inspector, timeline).to_dict()
        except Exception as e:  # inspection problems never stop the incident analysis
            inspection = {"status": "unavailable", "findings": [], "error": f"{type(e).__name__}: {e}"}
    max_chars = inspector.limits.max_output_chars if inspector else 60_000
    return {"repository_inspection": inspection,
            "messages": [HumanMessage(content=format_repository_context(inspection, max_chars))]}


def apply_repository_analysis(postmortem: dict, inspection: dict, inspector) -> None:
    """Post-process the model output against the collected repository evidence:
    typed findings are checked, and the patch proposal is validated as a
    display-only description (or rejected). Nothing is written anywhere."""
    known = {f["evidence_id"] for f in (inspection or {}).get("findings", [])}
    findings, notes = normalize_findings(postmortem.get("findings"), known_repository_ids=known)
    postmortem["findings"] = findings
    postmortem["analysis_notes"] = notes

    raw = postmortem.get("patch_proposal")
    postmortem["patch_proposal"], postmortem["patch_proposal_rejection"] = None, None
    if raw is None:
        return
    if (inspection or {}).get("status") != "inspected":
        postmortem["patch_proposal_rejection"] = "no repository was inspected"
        return
    try:
        postmortem["patch_proposal"] = validate_patch_proposal(raw, known_repository_ids=known, inspector=inspector)
    except InvalidProposal as e:
        postmortem["patch_proposal_rejection"] = str(e)
