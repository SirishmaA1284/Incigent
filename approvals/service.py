"""
approvals/service.py
Application-level human approval service.

Invariant: no consequential action may run unless consume() succeeds, and
consume() succeeds only if the approval
  - exists,
  - belongs to the given incident,
  - is of the given kind,
  - has the same action hash as the payload about to run,
  - is approved,
  - has not expired,
  - has not already been consumed.
These checks are enforced here and in the database, never by the LLM and never
by long-term memory (Hindsight is not consulted).

State machine:
  pending  -> approved | rejected | expired
  approved -> consumed | expired
  rejected, expired, consumed are terminal.

CONSUME-BEFORE-ACT (the rule every executor must follow):
  NO CONSEQUENTIAL ACTION MAY EXECUTE UNLESS consume() HAS JUST SUCCEEDED
  FOR THAT EXACT ACTION.
  - validate() only establishes that an approval is valid *right now*. It
    changes nothing and is not permission to act: the approval could be
    consumed, expire or be reused elsewhere a moment later.
  - consume() is the one-time authorization handoff. It re-checks everything
    and marks the approval consumed in the same transaction, so it succeeds at
    most once. An executor calls it immediately before acting and acts only if
    it returns; an executor must never act after validate() alone.
  No executor exists yet, so nothing in the application calls consume().

EXPIRATION IS LAZY: there is no background worker or scheduler. An approval
past its expires_at is marked "expired" (and audited) the next time anything
touches it — approve, reject, validate, consume or expire(). Until then the
stored status may still read "pending"/"approved", but every path that could
act on it checks expiry first, so an expired approval can never be approved,
validated or consumed, and it is never revived.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from approvals.hashing import action_hash
from approvals.store import ApprovalStore

KINDS = ("operational", "repository_apply", "repository_commit")
STATUSES = ("pending", "approved", "rejected", "expired", "consumed")
DEFAULT_TTL_SECONDS = 3600


class ApprovalDenied(Exception):
    """An approval check failed. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Approval:
    approval_id: str
    incident_id: str
    kind: str
    action_hash: str
    payload: dict
    status: str
    requested_at: str
    requested_by: str
    decided_by: Optional[str]
    decided_at: Optional[str]
    reason: Optional[str]
    expires_at: str
    consumed_at: Optional[str]
    consumed_by: Optional[str]

    @classmethod
    def from_row(cls, row: dict) -> "Approval":
        row = dict(row)
        payload = json.loads(row.pop("payload_json"))
        return cls(payload=payload, **row)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _require(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


class ApprovalService:
    def __init__(self, store: ApprovalStore, *, now: Callable[[], datetime] = None,
                 default_ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.store = store
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.default_ttl_seconds = default_ttl_seconds

    # -- request / read -------------------------------------------------------
    def request_approval(self, incident_id: str, kind: str, payload: dict, *,
                         requested_by: str = "incident-agent", ttl_seconds: Optional[int] = None) -> Approval:
        incident_id = _require(incident_id, "incident_id")
        if kind not in KINDS:
            raise ValueError(f"unknown approval kind {kind!r}; expected one of {KINDS}")
        digest = action_hash(payload)  # validates the payload is canonical-JSON safe
        now = self._now()
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        row = {
            "approval_id": f"apr_{uuid.uuid4().hex}",
            "incident_id": incident_id,
            "kind": kind,
            "action_hash": digest,
            "payload_json": json.dumps(payload, sort_keys=True),
            "status": "pending",
            "requested_at": _iso(now),
            "requested_by": _require(requested_by, "requested_by"),
            "expires_at": _iso(now + timedelta(seconds=ttl)),
        }
        with self.store.transaction() as conn:
            self.store.insert(conn, row)
            self.store.audit(conn, approval_id=row["approval_id"], incident_id=incident_id,
                             event_type="approval_requested", actor=row["requested_by"],
                             occurred_at=row["requested_at"],
                             details={"kind": kind, "action_hash": digest, "expires_at": row["expires_at"]})
        return self.get(row["approval_id"])

    def get(self, approval_id: str) -> Optional[Approval]:
        row = self.store.get(approval_id) if isinstance(approval_id, str) else None
        return Approval.from_row(row) if row else None

    def audit_events(self, approval_id: str) -> list:
        return self.store.audit_events(approval_id)

    # -- decisions ------------------------------------------------------------
    def approve(self, approval_id: str, *, actor: str, reason: Optional[str] = None) -> Approval:
        return self._decide(approval_id, "approved", actor=_require(actor, "actor"), reason=reason)

    def reject(self, approval_id: str, *, actor: str, reason: str) -> Approval:
        return self._decide(approval_id, "rejected", actor=_require(actor, "actor"),
                            reason=_require(reason, "reason"))

    def expire(self, approval_id: str, *, actor: str = "system") -> Approval:
        """Expire a pending or approved approval whose expiry time has passed."""
        denied = None
        with self.store.transaction() as conn:
            row = self._fetch_or_deny(conn, approval_id)
            if not self._expire_if_due(conn, row, actor):
                denied = ApprovalDenied("not_due", "approval has not reached its expiry time")
        if denied:
            raise denied
        return self.get(approval_id)

    def _decide(self, approval_id, new_status, *, actor, reason):
        denied = None
        with self.store.transaction() as conn:
            row = self._fetch_or_deny(conn, approval_id)
            now = _iso(self._now())
            if self._expire_if_due(conn, row, "system"):
                denied = ApprovalDenied("expired", "approval expired before a decision was made")
            elif row["status"] != "pending":
                denied = ApprovalDenied(row["status"], f"approval is already {row['status']}")
            elif not self.store.transition(conn, row["approval_id"], "pending", status=new_status,
                                           decided_by=actor, decided_at=now, reason=reason):
                denied = ApprovalDenied("conflict", "approval changed concurrently")
            else:
                self.store.audit(conn, approval_id=row["approval_id"], incident_id=row["incident_id"],
                                 event_type=f"approval_{new_status}", actor=actor, reason=reason,
                                 occurred_at=now)
        if denied:
            raise denied
        return self.get(approval_id)

    # -- enforcement ----------------------------------------------------------
    def validate(self, approval_id: str, *, incident_id: str, kind: str, payload: dict,
                 actor: str = "incident-agent") -> Approval:
        """Check every invariant without consuming. Raises ApprovalDenied.
        A pass means "valid right now" — it is NOT permission to execute."""
        return self._enforce(approval_id, incident_id, kind, payload, actor, consume=False)

    def consume(self, approval_id: str, *, incident_id: str, kind: str, payload: dict, actor: str) -> Approval:
        """The one-time authorization handoff: atomically check every invariant
        and mark the approval consumed. A future executor must call this
        immediately before acting, act only if it returns, and never act on the
        strength of validate() alone. Succeeds at most once per approval."""
        return self._enforce(approval_id, incident_id, kind, payload, _require(actor, "actor"), consume=True)

    def _enforce(self, approval_id, incident_id, kind, payload, actor, *, consume):
        denied, result_id = None, approval_id
        try:
            presented_hash = action_hash(payload)
        except TypeError:
            presented_hash = None
        with self.store.transaction() as conn:
            row = self._fetch_or_deny(conn, approval_id)
            now = _iso(self._now())
            code = None
            if self._expire_if_due(conn, row, "system"):
                code = "expired"
            elif row["incident_id"] != incident_id:
                code = "wrong_incident"
            elif row["kind"] != kind:
                code = "wrong_kind"
            elif presented_hash is None or presented_hash != row["action_hash"]:
                code = "hash_mismatch"
            elif row["status"] != "approved":
                code = row["status"]  # pending | rejected | expired | consumed
            elif consume and not self.store.transition(conn, row["approval_id"], "approved",
                                                       status="consumed", consumed_at=now, consumed_by=actor):
                code = "consumed"
            if code:
                self.store.audit(conn, approval_id=row["approval_id"], incident_id=row["incident_id"],
                                 event_type="approval_denied", actor=actor, reason=code, occurred_at=now,
                                 details={"attempt": "consume" if consume else "validate"})
                denied = ApprovalDenied(code)
            elif consume:
                self.store.audit(conn, approval_id=row["approval_id"], incident_id=row["incident_id"],
                                 event_type="approval_consumed", actor=actor, occurred_at=now)
        if denied:
            raise denied
        return self.get(result_id)

    def record_denial(self, approval_id: str, *, code: str, actor: str) -> None:
        """Audit a rejected attempt that never reached validation (e.g. a bad resume reference)."""
        with self.store.transaction() as conn:
            row = self.store.fetch(conn, approval_id)
            if row:
                self.store.audit(conn, approval_id=approval_id, incident_id=row["incident_id"],
                                 event_type="approval_denied", actor=actor, reason=code,
                                 occurred_at=_iso(self._now()), details={"attempt": "resume"})

    # -- helpers --------------------------------------------------------------
    def _fetch_or_deny(self, conn, approval_id):
        row = self.store.fetch(conn, approval_id) if isinstance(approval_id, str) else None
        if row is None:
            raise ApprovalDenied("not_found", "no such approval")
        return row

    def _expire_if_due(self, conn, row, actor) -> bool:
        """Mark a live approval expired once its expiry has passed. Never revives."""
        if row["status"] == "expired":
            return True
        if row["status"] not in ("pending", "approved"):
            return False
        now = self._now()
        if now < datetime.fromisoformat(row["expires_at"]):
            return False
        if self.store.transition(conn, row["approval_id"], row["status"], status="expired"):
            self.store.audit(conn, approval_id=row["approval_id"], incident_id=row["incident_id"],
                             event_type="approval_expired", actor=actor, occurred_at=_iso(now),
                             details={"previous_status": row["status"]})
            row["status"] = "expired"
        return True
