"""
Read-only inspection of an explicitly configured target repository (Stage 5).

No writes, no commands, no Git state changes. Paths must resolve inside the
configured root; sensitive files are denied by default. Results are structured,
traceable evidence ([R###]) plus a display-only patch proposal.
"""

import os

from repo_inspection.collector import collect_repository_evidence, format_repository_context
from repo_inspection.evidence import EvidenceLog, FileEvidence, GitEvidence, RepositoryInspection
from repo_inspection.inspector import OPERATIONS, RepositoryInspector, run_operation
from repo_inspection.policy import InspectionError, InspectionLimits, is_sensitive
from repo_inspection.proposal import InvalidProposal, normalize_findings, validate_patch_proposal

__all__ = ["collect_repository_evidence", "format_repository_context", "EvidenceLog", "FileEvidence",
           "GitEvidence", "RepositoryInspection", "OPERATIONS", "RepositoryInspector", "run_operation",
           "InspectionError", "InspectionLimits", "is_sensitive", "InvalidProposal", "normalize_findings",
           "validate_patch_proposal", "build_repository_inspector"]


def build_repository_inspector():
    """(inspector | None, reason). Only an explicitly configured TARGET_REPO_PATH is
    ever inspected; there is no default and never the agent's own repository."""
    root = os.getenv("TARGET_REPO_PATH", "").strip()
    if not root:
        return None, "TARGET_REPO_PATH is not set"
    try:
        return RepositoryInspector(root, repository_id=os.getenv("TARGET_REPO_ID", "target").strip() or "target",
                                   limits=InspectionLimits.from_env()), None
    except InspectionError as e:
        return None, f"TARGET_REPO_PATH rejected: {e.code}"
