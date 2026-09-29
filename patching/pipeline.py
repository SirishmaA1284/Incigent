"""
patching/pipeline.py
Stage 6 orchestrator, in two phases separated by a HUMAN APPROVAL:

  1. prepare_patch_request()  — READ-ONLY. Validates the untrusted proposal,
     the workspace placement, the pinned base and the clean working tree, and
     builds the canonical `repository_apply` approval payload (exact patch
     intent + pinned base SHA + target + incident). Nothing is written.
  2. PatchExecutor.execute()  — runs only after a human approved that exact
     payload: re-checks everything, then ApprovalService.validate() ->
     consume() (the single-use authorization boundary) -> and only then
     materializes the pinned candidate, applies, diff-checks and verifies.

The original target repository is strictly read-only in both phases. Every
Git read goes through the Stage 5 read-only interface; every write goes into an
application-created isolated worktree under the configured workspace, which
must not overlap the target or the application repository. Commit, push, merge
and PR are NOT part of Stage 6.
"""

import os
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

from approvals import ApprovalDenied
from repo_inspection import InspectionLimits
from repo_inspection.git_read import NotAGitRepository, UnsafeGitDirectory, _open, head_info, status
from repo_inspection.policy import InspectionError
from repo_inspection.proposal import InvalidProposal

from patching.apply import apply_patch, patch_fingerprint, validate_applied_diff
from patching.policy import (
    PatchError, PatchLimits, application_root, paths_overlap, validate_patch_path, validate_workspace,
)
from patching.proposal import PatchIntent, validate_patch_intent
from patching.verify import build_verifier
from patching.worktree import WorktreeManager

APPROVAL_KIND = "repository_apply"
PAYLOAD_KEYS = frozenset({"kind", "incident_id", "target_repository", "base_sha", "patch_id",
                          "patch", "proposed_by"})
INTENT_KEYS = frozenset(f.name for f in fields(PatchIntent))

RESULT_STATUSES = (
    "skipped", "proposal_invalid", "self_target_denied", "workspace_unsafe", "repository_dirty",
    "base_changed", "base_unavailable", "target_changed", "approval_required",
    "approval_unavailable", "approval_denied", "not_executable", "not_authorized",
    "worktree_creation_failed", "unsafe_tree_entry", "patch_rejected", "patch_application_failed",
    "verification_passed", "verification_failed", "verification_not_run",
)


@dataclass(frozen=True)
class PatchConfig:
    workspace: Path
    limits: PatchLimits = PatchLimits()
    path_limits: InspectionLimits = InspectionLimits()   # Stage 5 path policy limits
    inspection_limits: InspectionLimits = InspectionLimits()   # git status limits
    verify: str = "structural"          # "structural" | "none"
    allow_self_target: bool = False     # explicit opt-in to target the app repo

    def __post_init__(self):
        if self.verify not in ("structural", "none"):
            raise ValueError("verify must be 'structural' or 'none'")


def build_patch_config():
    """(PatchConfig | None). Patching is DISABLED unless PATCH_WORKSPACE is set:
    the workspace is explicit application configuration, never inferred."""
    workspace = os.getenv("PATCH_WORKSPACE", "").strip()
    if not workspace:
        return None
    verify = os.getenv("PATCH_VERIFY", "structural").strip().lower() or "structural"
    allow = os.getenv("ALLOW_SELF_TARGET_REPO", "").strip().lower() in ("1", "true", "yes")
    return PatchConfig(workspace=Path(workspace), limits=PatchLimits.from_env(),
                       verify=verify, allow_self_target=allow)


@dataclass
class PatchResult:
    status: str = "skipped"
    incident_id: str = ""
    patch_id: str = ""
    base_sha: str = ""
    target_repository: str = ""
    approval_id: str = ""
    worktree: Optional[dict] = None
    changed_files: list = field(default_factory=list)
    diff_summary: dict = field(default_factory=dict)
    patch_fingerprint: str = ""
    verification: dict = field(default_factory=dict)
    # candidate_modified: the ISOLATED CANDIDATE WORKTREE received the patch.
    # original_repo_unchanged: the ORIGINAL target repository was re-checked
    # (HEAD + clean status) afterwards. The Stage 5 patch_proposal's
    # "applied": false is about the original repository and stays false.
    candidate_modified: bool = False
    original_repo_unchanged: Optional[bool] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "incident_id": self.incident_id,
                "patch_id": self.patch_id, "base_sha": self.base_sha,
                "target_repository": self.target_repository, "approval_id": self.approval_id,
                "worktree": self.worktree, "changed_files": list(self.changed_files),
                "diff_summary": dict(self.diff_summary),
                "patch_fingerprint": self.patch_fingerprint,
                "verification": dict(self.verification),
                "candidate_modified": self.candidate_modified,
                "original_repo_unchanged": self.original_repo_unchanged, "reason": self.reason}


def _norm(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _self_target_allowed(result: PatchResult, *, inspector, config: PatchConfig) -> bool:
    """The application repository (or anything overlapping it) must never
    become the patch target without the explicit override."""
    own = application_root()
    if own is not None and paths_overlap(Path(inspector.root).resolve(), own) and not config.allow_self_target:
        result.status = "self_target_denied"
        result.reason = ("refusing to patch the application's own repository "
                         "(set ALLOW_SELF_TARGET_REPO=1 to override)")
        return False
    return True


def _preflight(result: PatchResult, *, inspector, config: PatchConfig, base: str) -> bool:
    """Read-only checks shared by both phases. On failure sets result.status /
    reason and returns False. Nothing is created or written."""
    if not _self_target_allowed(result, inspector=inspector, config=config):
        return False
    # The workspace must not be, contain or be inside the target / app repository.
    # (Checked even with the self-target override: the workspace may never be
    # inside the application repository.)
    try:
        validate_workspace(config.workspace, target_root=inspector.root, app_root=application_root())
    except PatchError as e:
        result.status, result.reason = e.code, str(e)
        return False
    # Pinned base: fail closed if missing or if the target moved on.
    try:
        current_head = head_info(inspector.root)
    except (NotAGitRepository, UnsafeGitDirectory) as e:
        result.status, result.reason = "base_unavailable", str(e)
        return False
    if not base:
        result.status, result.reason = "base_unavailable", "no pinned base commit from repository inspection"
        return False
    result.base_sha = base
    if (current_head.get("head_sha") or "") != base:
        result.status, result.reason = "base_changed", (
            f"target moved: inspection pinned {base[:12]}, HEAD is now "
            f"{(current_head.get('head_sha') or '')[:12] or 'missing'}")
        return False
    # Dirty working tree: refuse. No stash, no reset, no clean.
    try:
        st = status(inspector.root, config.inspection_limits)
    except (NotAGitRepository, UnsafeGitDirectory) as e:
        result.status, result.reason = "base_unavailable", str(e)
        return False
    if st.get("limited"):
        result.status, result.reason = "repository_dirty", (
            "working-tree status unavailable (tracked-file limit); refusing to patch an unknown state")
        return False
    if not st["clean"]:
        result.status = "repository_dirty"
        result.reason = "the target repository has uncommitted changes; refusing to patch"
        result.changed_files = sorted({*st["unstaged"],
                                       *[p for v in st["staged"].values() for p in v],
                                       *st["untracked"]})[:50]
        return False
    return True


def build_patch_payload(intent: PatchIntent, *, incident_id: str, target_root, base_sha: str,
                        patch_id: str) -> dict:
    """The canonical repository_apply approval payload. Its action hash binds
    the approval to the exact patch intent, pinned base, target and incident."""
    return {
        "kind": APPROVAL_KIND,
        "incident_id": incident_id,
        "target_repository": str(Path(target_root).resolve()),
        "base_sha": base_sha,
        "patch_id": patch_id,
        "patch": intent.to_dict(),
        "proposed_by": "llm_agent",
    }


def prepare_patch_request(*, proposal, inspector, incident_id: str, known_repository_ids,
                          config: PatchConfig, pinned_base_sha: Optional[str] = None):
    """Phase 1 (READ-ONLY): validate an untrusted proposal and build the
    repository_apply approval payload. Returns (PatchResult, payload | None).
    Never creates a worktree, never writes, never raises for model input."""
    result = PatchResult(incident_id=incident_id, patch_id=uuid.uuid4().hex,
                         target_repository=str(inspector.root))
    if proposal is None:
        result.status, result.reason = "skipped", "no patch proposal"
        return result, None
    if not _self_target_allowed(result, inspector=inspector, config=config):
        return result, None
    try:
        intent: PatchIntent = validate_patch_intent(proposal, inspector=inspector,
                                                    known_repository_ids=known_repository_ids,
                                                    limits=config.limits)
    except InvalidProposal as e:
        result.status, result.reason = "proposal_invalid", str(e)
        return result, None
    if not _preflight(result, inspector=inspector, config=config, base=pinned_base_sha or ""):
        return result, None
    payload = build_patch_payload(intent, incident_id=incident_id, target_root=inspector.root,
                                  base_sha=result.base_sha, patch_id=result.patch_id)
    result.changed_files = [intent.file]
    result.patch_fingerprint = patch_fingerprint(intent, patch_id=result.patch_id, base_sha=result.base_sha)
    result.status = "approval_required"
    result.reason = ("a human must approve this repository_apply request before any "
                     "candidate worktree is created")
    return result, payload


class PatchExecutor:
    """Phase 2: applies an APPROVED patch into a new isolated candidate.

    Order inside execute(), for every call:
      1. payload shape, kind, incident and target (no DB, no effects)
      2. read-only preflight: self-target, workspace placement, base SHA still
         HEAD, clean working tree (a failure is audited on the approval)
      3. ApprovalService.validate()
      4. ApprovalService.consume() — THE AUTHORIZATION BOUNDARY (single use)
      5. only if consume() returned: materialize the pinned candidate,
         validate + apply, diff-check, re-check the original, verify.
    Nothing before step 5 writes anything. A failure after consume() leaves
    the approval consumed (fail closed): it is never reused."""

    actor = "patch-executor"

    def __init__(self, approvals, inspector, config: PatchConfig):
        self.approvals = approvals
        self.inspector = inspector
        self.config = config

    def execute(self, approval_id: str, *, incident_id: str, payload: dict) -> PatchResult:
        result = PatchResult(status="not_executable", incident_id=incident_id,
                             target_repository=str(getattr(self.inspector, "root", "")),
                             approval_id=str(approval_id))

        # 1. shape / kind / incident / target
        if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
            result.reason = "malformed_payload"
            return result
        if payload["kind"] != APPROVAL_KIND:
            result.reason = "wrong_approval_kind"
            return result
        if payload["incident_id"] != incident_id:
            result.reason = "wrong_incident"
            return result
        patch = payload["patch"]
        if not isinstance(patch, dict) or set(patch) != INTENT_KEYS:
            result.reason = "malformed_patch_intent"
            return result
        result.patch_id, result.base_sha = str(payload["patch_id"]), str(payload["base_sha"])
        if self.inspector is None or _norm(self.inspector.root) != os.path.normcase(str(payload["target_repository"])):
            result.status, result.reason = "target_changed", "the configured target repository differs from the approved one"
            self._audit(approval_id, "target_changed")
            return result
        intent = PatchIntent(**patch)
        result.changed_files = [intent.file]
        try:   # the original-repository path policy, re-applied (read-only)
            validate_patch_path(intent.file, Path(self.inspector.root), self.config.path_limits)
        except InspectionError as e:
            result.status, result.reason = "patch_rejected", f"{intent.file}: {e.code}"
            self._audit(approval_id, "patch_rejected")
            return result

        # 2. read-only preflight
        if not _preflight(result, inspector=self.inspector, config=self.config, base=result.base_sha):
            self._audit(approval_id, result.status)
            return result

        # 3-4. validate, then consume: consume() returning is the only authorization
        try:
            self.approvals.validate(approval_id, incident_id=incident_id, kind=APPROVAL_KIND,
                                    payload=payload, actor=self.actor)
            self.approvals.consume(approval_id, incident_id=incident_id, kind=APPROVAL_KIND,
                                   payload=payload, actor=self.actor)
        except ApprovalDenied as denied:
            result.status, result.reason = "not_authorized", denied.code
            return result

        # 5. authorized: build the candidate from the PINNED tree, then apply
        return self._build_candidate(result, intent)

    def _audit(self, approval_id, code):
        try:
            self.approvals.record_denial(approval_id, code=code, actor=self.actor)
        except Exception:
            pass

    def _build_candidate(self, result: PatchResult, intent: PatchIntent) -> PatchResult:
        config, root, base = self.config, self.inspector.root, result.base_sha
        manager = WorktreeManager(config.workspace)
        try:
            worktree = manager.create(root, base, result.incident_id, config.limits,
                                      patch_id=result.patch_id)
        except PatchError as e:
            result.status, result.reason = e.code, str(e)
            return result
        result.worktree = worktree.to_dict()

        try:
            repo = _open(root)
        except (NotAGitRepository, UnsafeGitDirectory) as e:
            result.status, result.reason = "base_unavailable", str(e)
            self._discard(manager, worktree, result)
            return result
        try:
            apply_patch(worktree, intent, config.limits, path_limits=config.path_limits,
                        repo=repo, base_sha=base)
        except PatchError as e:
            # Rejected before writing (limits, ambiguity, encoding, ...) or the
            # write itself failed: the candidate is never marked applied.
            result.status, result.reason = e.code, str(e)
            self._discard(manager, worktree, result)
            return result
        result.candidate_modified = True
        manager.mark_applied(worktree.worktree_id)

        # The applied change must match the validated scope exactly.
        try:
            result.diff_summary = validate_applied_diff(worktree, intent, config.limits,
                                                        repo=repo, base_sha=base)
        except PatchError as e:
            result.status, result.reason = e.code, str(e)
            return result
        result.patch_fingerprint = patch_fingerprint(intent, patch_id=result.patch_id, base_sha=base)

        # The original repository must be untouched (HEAD + status re-check).
        try:
            after_head = head_info(root)
            after_status = status(root, config.inspection_limits)
            unchanged = ((after_head.get("head_sha") or "") == base
                         and after_status.get("clean") is True and not after_status.get("limited"))
        except (NotAGitRepository, UnsafeGitDirectory):
            unchanged = False
        result.original_repo_unchanged = unchanged
        if not unchanged:
            result.status = "patch_rejected"
            result.reason = "original repository changed during patch preparation"
            return result

        # Verification: structural (safe, in-process) or not run.
        verifier = build_verifier(config)
        if verifier is None:
            result.verification = {"status": "not_run", "mode": "none", "checks": [],
                                   "tests": {"status": "not_run", "reason": "verification disabled in configuration"}}
            result.status = "verification_not_run"
            return result
        result.verification = verifier.verify(worktree, intent, limits=config.limits,
                                              diff_summary=result.diff_summary).to_dict()
        result.status = "verification_passed" if result.verification["status"] == "passed" else "verification_failed"
        return result

    @staticmethod
    def _discard(manager, worktree, result):
        """Remove a candidate that never received a valid patch."""
        try:
            manager.cleanup(worktree.worktree_id)
            result.worktree = {**(result.worktree or {}), "cleaned_up": True}
        except PatchError as e:
            result.worktree = {**(result.worktree or {}), "cleaned_up": False, "cleanup_error": str(e)}
