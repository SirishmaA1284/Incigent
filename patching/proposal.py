"""
patching/proposal.py
Validation of the analyst's patch INTENT (Stage 6).

The Stage 5 patch proposal is an untrusted model output. Before anything may be
written into an isolated worktree, the proposal must pass the Stage 5 display
validation AND carry a strictly validated patch intent: exactly one "modify"
operation on one existing text file, with an exact expected_old_content that
must occur exactly once in the file at the pinned base commit. Fail closed on
anything unknown, ambiguous or out of scope.
"""

import re
from dataclasses import dataclass

from repo_inspection.proposal import InvalidProposal, validate_patch_proposal

from patching.policy import PatchLimits

_MAX_LOCATION = 300
_MAX_TEXT = 2000
_MAX_LINE_LENGTH = 400


@dataclass(frozen=True)
class PatchIntent:
    file: str                       # validated repository-relative path (POSIX)
    location: str
    problem: str
    proposed_change: str
    rationale: str
    expected_effect: str
    verification_plan: list
    evidence_ids: list
    expected_old_content: str       # must match the base file content EXACTLY, once
    replacement_content: str        # full replacement text (text only)

    def to_dict(self) -> dict:
        return {
            "file": self.file, "location": self.location, "problem": self.problem,
            "proposed_change": self.proposed_change, "rationale": self.rationale,
            "expected_effect": self.expected_effect,
            "verification_plan": list(self.verification_plan),
            "evidence_ids": list(self.evidence_ids),
            "expected_old_content": self.expected_old_content,
            "replacement_content": self.replacement_content,
        }


def _bounded_text(value, name, limit):
    if not isinstance(value, str):
        raise InvalidProposal(f"{name} must be a string")
    if not value.strip():
        raise InvalidProposal(f"{name} must be non-empty")
    if len(value) > limit:
        raise InvalidProposal(f"{name} exceeds {limit} characters")
    return value


def _text(value, name, limit):
    if not isinstance(value, str) or not value.strip():
        raise InvalidProposal(f"{name} must be a non-empty string")
    if len(value) > limit:
        raise InvalidProposal(f"{name} exceeds {limit} characters")
    return value.strip()


def _require_utf8(value, name="patch_proposal"):
    """Every model-supplied string (keys included) must be encodable as UTF-8.
    JSON allows lone surrogates such as "\\ud800"; they would otherwise only
    fail later, inside approval hashing or the patch write."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidProposal(f"{name} contains text that is not valid UTF-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            _require_utf8(key, name)
            _require_utf8(item, f"{name}.{key}" if isinstance(key, str) else name)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_utf8(item, name)


def validate_patch_intent(proposal, *, inspector, known_repository_ids, limits: PatchLimits) -> PatchIntent:
    """Full Stage 6 validation of a raw proposal dict. Raises InvalidProposal on
    anything invalid; returns an immutable PatchIntent that the application may
    apply inside an isolated worktree. The original repository is never written."""
    if not isinstance(proposal, dict):
        raise InvalidProposal("patch_proposal must be an object")
    _require_utf8(proposal)
    # Stage 5 display validation: fields, path policy, evidence IDs. The
    # proposal must still reference valid [R###] repository evidence.
    validated = validate_patch_proposal(proposal, known_repository_ids=known_repository_ids,
                                        inspector=inspector)

    # Stage 6: the exact patch intent, ONE file, ONE modify operation.
    patch = proposal.get("patch")
    if isinstance(patch, list):
        raise InvalidProposal("only single-file patches are supported")
    if not isinstance(patch, dict):
        raise InvalidProposal("patch_proposal.patch must be an object with the exact change")
    if set(patch) != {"operation", "expected_old_content", "replacement_content"}:
        raise InvalidProposal("patch must contain exactly operation, expected_old_content, replacement_content")
    if patch["operation"] != "modify":
        raise InvalidProposal("only the 'modify' operation is supported")

    old = _bounded_text(patch["expected_old_content"], "expected_old_content", limits.max_old_content_chars)
    new = _bounded_text(patch["replacement_content"], "replacement_content", limits.max_replacement_chars)
    if old == new:
        raise InvalidProposal("expected_old_content and replacement_content must differ")
    if any(len(line) > _MAX_LINE_LENGTH for line in new.splitlines()):
        raise InvalidProposal("replacement_content contains an overlong line")

    plan = validated.get("verification_plan") or []
    if not isinstance(plan, list):
        raise InvalidProposal("verification_plan must be a list")

    return PatchIntent(
        file=validated["file"],
        location=_text(validated["location"], "location", _MAX_LOCATION),
        problem=_text(validated["problem"], "problem", _MAX_TEXT),
        proposed_change=_text(validated["proposed_change"], "proposed_change", _MAX_TEXT),
        rationale=_text(validated["rationale"], "rationale", _MAX_TEXT),
        expected_effect=_text(validated["expected_effect"], "expected_effect", _MAX_TEXT),
        verification_plan=[str(p)[:300] for p in plan][:10],
        evidence_ids=list(validated["evidence_ids"]),
        expected_old_content=old,
        replacement_content=new,
    )