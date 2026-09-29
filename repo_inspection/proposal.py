"""
repo_inspection/proposal.py
Validation of the analyst's structured findings and PATCH PROPOSAL.

A patch proposal is a DESCRIPTION of a possible future change. It is not a
patch, not a diff, and not executable: it has a fixed set of text fields, no
command/shell/script field is accepted, and nothing in this stage reads it
except to display it. Applying changes belongs to a later stage with its own
approval boundary.
"""

import re

from repo_inspection.policy import InspectionError, is_sensitive, validate_relative_path

PROPOSAL_REQUIRED = frozenset({"file", "location", "problem", "proposed_change", "rationale", "evidence_ids"})
PROPOSAL_OPTIONAL = frozenset({"expected_effect", "verification_plan"})
FINDING_TYPES = ("observed", "inference", "hypothesis", "recommendation")
MAX_TEXT = 2000
MAX_PLAN_ITEMS, MAX_PLAN_TEXT = 10, 300
_EVIDENCE_ID = re.compile(r"^[ER]\d{1,6}$")


class InvalidProposal(ValueError):
    pass


def _text(value, name, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip():
        raise InvalidProposal(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise InvalidProposal(f"{name} exceeds {limit} characters")
    return value.strip()


def validate_patch_proposal(proposal, *, known_repository_ids, inspector):
    """Return a normalized, display-only proposal or raise InvalidProposal."""
    if not isinstance(proposal, dict):
        raise InvalidProposal("patch_proposal must be an object")
    keys = set(proposal)
    missing, extra = PROPOSAL_REQUIRED - keys, keys - PROPOSAL_REQUIRED - PROPOSAL_OPTIONAL
    if missing or extra:
        raise InvalidProposal(f"missing {sorted(missing)} / unexpected {sorted(extra)}")

    if inspector is None:
        raise InvalidProposal("no repository was inspected")
    try:
        target = validate_relative_path(proposal["file"], inspector.limits)
        inspector._checked_file(target)          # must exist inside the root and not be sensitive
    except InspectionError as e:
        raise InvalidProposal(f"file rejected: {e.code}")
    if is_sensitive(target):
        raise InvalidProposal("file rejected: sensitive_file")

    ids = proposal["evidence_ids"]
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and _EVIDENCE_ID.match(i) for i in ids):
        raise InvalidProposal("evidence_ids must be a non-empty list of IDs like 'R003'")
    unknown = [i for i in ids if i.startswith("R") and i not in known_repository_ids]
    if unknown:
        raise InvalidProposal(f"unknown repository evidence IDs {unknown}")
    if not any(i.startswith("R") for i in ids):
        raise InvalidProposal("a patch proposal must cite at least one repository evidence ID")

    plan = proposal.get("verification_plan", [])
    if not isinstance(plan, list) or len(plan) > MAX_PLAN_ITEMS:
        raise InvalidProposal(f"verification_plan must be a list of at most {MAX_PLAN_ITEMS} items")

    return {
        "file": target,
        "location": _text(proposal["location"], "location", 300),
        "problem": _text(proposal["problem"], "problem"),
        "proposed_change": _text(proposal["proposed_change"], "proposed_change"),
        "rationale": _text(proposal["rationale"], "rationale"),
        "expected_effect": _text(proposal["expected_effect"], "expected_effect") if proposal.get("expected_effect") else "",
        "verification_plan": [_text(p, "verification_plan item", MAX_PLAN_TEXT) for p in plan],
        "evidence_ids": list(dict.fromkeys(ids)),
        # Stage 5 stops here: a description only. Nothing applies it.
        "status": "proposal_only",
        "applied": False,
        "requires": "a future repository-change approval (separate from operational approvals)",
    }


def normalize_findings(findings, *, known_repository_ids):
    """Keep typed findings; an 'observed' claim without valid evidence is demoted
    to 'hypothesis' so an inference is never presented as a direct observation."""
    out, notes = [], []
    for f in findings if isinstance(findings, list) else []:
        if not isinstance(f, dict) or f.get("type") not in FINDING_TYPES or not isinstance(f.get("statement"), str):
            notes.append("dropped a malformed finding")
            continue
        ids = [i for i in (f.get("evidence_ids") or []) if isinstance(i, str) and _EVIDENCE_ID.match(i)
               and (not i.startswith("R") or i in known_repository_ids)]
        kind = f["type"]
        if kind == "observed" and not ids:
            kind = "hypothesis"
            notes.append("demoted an 'observed' finding without valid evidence to 'hypothesis'")
        out.append({"type": kind, "statement": f["statement"][:MAX_TEXT], "evidence_ids": ids})
    return out, notes
