"""
Human approval layer: application-owned approval records, audit log and
enforcement. Long-term memory (Hindsight) is never used for authorization.
"""

import os

from approvals.hashing import action_hash, canonical_json
from approvals.service import (
    KINDS, STATUSES, Approval, ApprovalDenied, ApprovalService,
)
from approvals.store import ApprovalStore

__all__ = ["action_hash", "canonical_json", "KINDS", "STATUSES", "Approval", "ApprovalDenied",
           "ApprovalService", "ApprovalStore", "build_approval_service"]

DEFAULT_DB_PATH = os.path.join("var", "approvals.sqlite3")


def build_approval_service() -> ApprovalService:
    """Service backed by the SQLite file at APPROVAL_DB_PATH (default var/approvals.sqlite3)."""
    return ApprovalService(ApprovalStore(os.getenv("APPROVAL_DB_PATH") or DEFAULT_DB_PATH))
