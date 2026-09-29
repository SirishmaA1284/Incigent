"""
patching/__init__.py
Stage 6: controlled patch preparation in isolated, application-owned worktrees.

The original target repository is never modified. The candidate patch is
validated strictly and turned into a `repository_apply` approval request
(read-only); only after a human approves that exact request, and the approval
is consumed, is it applied inside an isolated worktree materialised from the
pinned base commit and verified without executing repository code. Commit,
push, merge and PR are NOT part of Stage 6 (Stage 7 owns publication and its
human-approval boundary).
"""

from pathlib import Path

from patching.apply import apply_patch, patch_fingerprint, validate_applied_diff
from patching.pipeline import (
    APPROVAL_KIND, PatchConfig, PatchExecutor, PatchResult, build_patch_config, build_patch_payload,
    prepare_patch_request,
)
from patching.policy import (
    PatchError, PatchLimits, check_tree_component, validate_patch_path,
    validate_patch_target_against_worktree, validate_workspace,
)
from patching.proposal import PatchIntent, validate_patch_intent
from patching.verify import StructuralVerifier, VerificationResult, build_verifier
from patching.worktree import Worktree, WorktreeManager

__all__ = ["apply_patch", "patch_fingerprint", "validate_applied_diff", "APPROVAL_KIND",
           "PatchConfig", "PatchExecutor", "PatchResult", "build_patch_config",
           "build_patch_payload", "prepare_patch_request", "PatchError", "PatchLimits",
           "check_tree_component", "validate_patch_path", "validate_patch_target_against_worktree",
           "validate_workspace", "PatchIntent", "validate_patch_intent", "StructuralVerifier",
           "VerificationResult", "build_verifier", "Worktree", "WorktreeManager",
           "application_repo_root"]


def application_repo_root() -> Path:
    """The application's own repository root (or None when it is not a Git
    checkout). Used ONLY to refuse patching the application's own source tree."""
    try:
        from dulwich.repo import Repo
        repo = Repo.discover(str(Path(__file__).resolve().parent.parent))
    except Exception:
        return None
    try:
        return Path(repo.path).resolve()
    finally:
        try:
            repo.close()
        except Exception:
            pass