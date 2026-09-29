"""
tests/test_approvals.py — Approval store, hashing and enforcement invariants.
All checks are application/database logic; no LLM and no long-term memory involved.
"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from approvals import (
    ApprovalDenied, ApprovalService, ApprovalStore, action_hash, canonical_json,
)

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
ACTION = {"kind": "operational", "incident_id": "INC-1",
          "action": {"description": "Roll back order-service to v2.4.0", "owner": "SRE", "priority": "P1"}}


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(tmp_path, clock):
    return ApprovalService(ApprovalStore(str(tmp_path / "approvals.sqlite3")), now=clock, default_ttl_seconds=600)


def approved(svc, payload=ACTION, incident="INC-1", kind="operational"):
    a = svc.request_approval(incident, kind, payload)
    return svc.approve(a.approval_id, actor="alice")


def events(svc, approval_id):
    return [(e["event_type"], e["actor"], e["reason"]) for e in svc.audit_events(approval_id)]


# ── Hashing ──────────────────────────────────────────────────────────────────

class TestActionHash:
    def test_same_canonical_action_same_hash(self):
        reordered = {"action": {"priority": "P1", "owner": "SRE", "description": "Roll back order-service to v2.4.0"},
                     "incident_id": "INC-1", "kind": "operational"}
        assert action_hash(ACTION) == action_hash(reordered) == action_hash(dict(ACTION))
        assert action_hash(ACTION).startswith("sha256:") and len(action_hash(ACTION)) == 7 + 64

    @pytest.mark.parametrize("change", [
        lambda p: p["action"].update(description="Restart order-service"),
        lambda p: p.update(incident_id="INC-2"),
        lambda p: p.update(kind="repository_apply"),
        lambda p: p["action"].update(priority="P2"),
        lambda p: p.update(extra="x"),
    ])
    def test_different_action_different_hash(self, change):
        import copy
        other = copy.deepcopy(ACTION)
        change(other)
        assert action_hash(other) != action_hash(ACTION)

    def test_canonical_json_is_stable_text(self):
        assert canonical_json({"b": 1, "a": ["x", {"d": None, "c": True}]}) == '{"a":["x",{"c":true,"d":null}],"b":1}'
        assert canonical_json({"é": "ü"}) == '{"é":"ü"}'

    @pytest.mark.parametrize("bad", [
        {"n": 1.5}, {"n": float("nan")}, {1: "int key"}, {"s": {1, 2}}, {"o": object()}, {"t": (1, 2)},
    ])
    def test_non_canonical_values_are_refused(self, bad):
        with pytest.raises(TypeError):
            action_hash(bad)

    def test_payload_must_be_an_object(self):
        with pytest.raises(TypeError):
            action_hash(["not", "an", "object"])


# ── Lifecycle ────────────────────────────────────────────────────────────────

class TestLifecycle:
    def test_request_creates_pending(self, svc):
        a = svc.request_approval("INC-1", "operational", ACTION)
        assert a.status == "pending" and a.approval_id.startswith("apr_")
        assert a.action_hash == action_hash(ACTION) and a.payload == ACTION
        assert a.requested_at == T0.isoformat()
        assert a.expires_at == (T0 + timedelta(seconds=600)).isoformat()
        assert (a.decided_by, a.decided_at, a.consumed_at) == (None, None, None)

    @pytest.mark.parametrize("kind", ["operational", "repository_apply", "repository_commit"])
    def test_supported_kinds(self, svc, kind):
        assert svc.request_approval("INC-1", kind, ACTION).kind == kind

    def test_unknown_kind_and_missing_incident_refused(self, svc):
        with pytest.raises(ValueError):
            svc.request_approval("INC-1", "shell", ACTION)
        with pytest.raises(ValueError):
            svc.request_approval(" ", "operational", ACTION)

    def test_approve(self, svc, clock):
        a = svc.request_approval("INC-1", "operational", ACTION)
        clock.advance(minutes=1)
        a = svc.approve(a.approval_id, actor="alice", reason="safe off-peak")
        assert (a.status, a.decided_by, a.reason) == ("approved", "alice", "safe off-peak")
        assert a.decided_at == (T0 + timedelta(minutes=1)).isoformat()

    def test_reject_requires_reason(self, svc):
        a = svc.request_approval("INC-1", "operational", ACTION)
        with pytest.raises(ValueError):
            svc.reject(a.approval_id, actor="bob", reason=" ")
        a = svc.reject(a.approval_id, actor="bob", reason="unsafe during peak traffic")
        assert (a.status, a.decided_by, a.reason) == ("rejected", "bob", "unsafe during peak traffic")

    def test_decision_requires_actor(self, svc):
        a = svc.request_approval("INC-1", "operational", ACTION)
        with pytest.raises(ValueError):
            svc.approve(a.approval_id, actor="")

    def test_decisions_are_final(self, svc):
        a = svc.request_approval("INC-1", "operational", ACTION)
        svc.reject(a.approval_id, actor="bob", reason="no")
        with pytest.raises(ApprovalDenied) as e:
            svc.approve(a.approval_id, actor="alice")
        assert e.value.code == "rejected"
        assert svc.get(a.approval_id).status == "rejected"

    def test_successful_consume(self, svc, clock):
        a = approved(svc)
        clock.advance(minutes=2)
        c = svc.consume(a.approval_id, incident_id="INC-1", kind="operational", payload=ACTION, actor="executor")
        assert c.status == "consumed"
        assert (c.consumed_by, c.consumed_at) == ("executor", (T0 + timedelta(minutes=2)).isoformat())

    def test_validate_does_not_consume(self, svc):
        a = approved(svc)
        svc.validate(a.approval_id, incident_id="INC-1", kind="operational", payload=ACTION)
        assert svc.get(a.approval_id).status == "approved"


# ── Enforcement ──────────────────────────────────────────────────────────────

def consume(svc, approval_id, **overrides):
    args = {"incident_id": "INC-1", "kind": "operational", "payload": ACTION, "actor": "executor", **overrides}
    with pytest.raises(ApprovalDenied) as e:
        svc.consume(approval_id, **args)
    return e.value.code


class TestEnforcement:
    def test_rejected_cannot_be_consumed(self, svc):
        a = svc.request_approval("INC-1", "operational", ACTION)
        svc.reject(a.approval_id, actor="bob", reason="no")
        assert consume(svc, a.approval_id) == "rejected"

    def test_pending_cannot_be_consumed(self, svc):
        a = svc.request_approval("INC-1", "operational", ACTION)
        assert consume(svc, a.approval_id) == "pending"
        assert svc.get(a.approval_id).status == "pending"

    def test_expired_approved_cannot_be_consumed(self, svc, clock):
        a = approved(svc)
        clock.advance(seconds=601)
        assert consume(svc, a.approval_id) == "expired"
        assert svc.get(a.approval_id).status == "expired"

    def test_pending_past_expiry_is_marked_expired_and_never_revived(self, svc, clock):
        a = svc.request_approval("INC-1", "operational", ACTION)
        clock.advance(seconds=600)  # expiry is inclusive
        with pytest.raises(ApprovalDenied) as e:
            svc.approve(a.approval_id, actor="alice")
        assert e.value.code == "expired"
        assert svc.get(a.approval_id).status == "expired"
        clock.now = T0  # even if the clock went backwards, expired stays expired
        with pytest.raises(ApprovalDenied):
            svc.approve(a.approval_id, actor="alice")
        assert consume(svc, a.approval_id) == "expired"

    def test_explicit_expire(self, svc, clock):
        a = svc.request_approval("INC-1", "operational", ACTION)
        with pytest.raises(ApprovalDenied) as e:
            svc.expire(a.approval_id)
        assert e.value.code == "not_due"
        clock.advance(hours=1)
        assert svc.expire(a.approval_id).status == "expired"

    def test_cannot_consume_twice(self, svc):
        a = approved(svc)
        svc.consume(a.approval_id, incident_id="INC-1", kind="operational", payload=ACTION, actor="executor")
        assert consume(svc, a.approval_id) == "consumed"

    def test_concurrent_consumers_only_one_wins(self, svc):
        a = approved(svc)
        barrier, results = threading.Barrier(8), []

        def worker():
            barrier.wait()
            try:
                svc.consume(a.approval_id, incident_id="INC-1", kind="operational", payload=ACTION, actor="executor")
                results.append("ok")
            except ApprovalDenied as e:
                results.append(e.code)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count("ok") == 1 and results.count("consumed") == 7

    def test_wrong_incident_rejected(self, svc):
        a = approved(svc)
        assert consume(svc, a.approval_id, incident_id="INC-2") == "wrong_incident"
        assert svc.get(a.approval_id).status == "approved"

    def test_wrong_kind_rejected(self, svc):
        a = approved(svc)
        assert consume(svc, a.approval_id, kind="repository_commit") == "wrong_kind"

    def test_swapped_action_rejected(self, svc):
        """Approve action A, then try to run action B with A's approval."""
        a = approved(svc)
        action_b = {**ACTION, "action": {**ACTION["action"], "description": "Restart order-service"}}
        assert consume(svc, a.approval_id, payload=action_b) == "hash_mismatch"
        assert consume(svc, a.approval_id, payload={"n": 1.5}) == "hash_mismatch"
        assert svc.get(a.approval_id).status == "approved"

    def test_unknown_approval_id(self, svc):
        assert consume(svc, "apr_does_not_exist") == "not_found"
        assert consume(svc, None) == "not_found"
        assert svc.get("apr_does_not_exist") is None


# ── Audit log ────────────────────────────────────────────────────────────────

class TestAudit:
    def test_full_lifecycle_is_audited(self, svc, clock):
        a = svc.request_approval("INC-1", "operational", ACTION, requested_by="incident-agent")
        consume(svc, a.approval_id)                                   # denied: pending
        svc.approve(a.approval_id, actor="alice", reason="ok")
        consume(svc, a.approval_id, payload={"other": "action"})      # denied: hash mismatch
        svc.consume(a.approval_id, incident_id="INC-1", kind="operational", payload=ACTION, actor="executor")
        assert events(svc, a.approval_id) == [
            ("approval_requested", "incident-agent", None),
            ("approval_denied", "executor", "pending"),
            ("approval_approved", "alice", "ok"),
            ("approval_denied", "executor", "hash_mismatch"),
            ("approval_consumed", "executor", None),
        ]
        rows = svc.audit_events(a.approval_id)
        assert all(r["incident_id"] == "INC-1" and r["occurred_at"] for r in rows)

    def test_rejection_and_expiry_are_audited(self, svc, clock):
        r = svc.request_approval("INC-1", "operational", ACTION)
        svc.reject(r.approval_id, actor="bob", reason="unsafe during peak traffic")
        assert events(svc, r.approval_id)[-1] == ("approval_rejected", "bob", "unsafe during peak traffic")

        x = svc.request_approval("INC-1", "operational", ACTION)
        clock.advance(hours=2)
        consume(svc, x.approval_id)
        assert [e[0] for e in events(svc, x.approval_id)] == ["approval_requested", "approval_expired", "approval_denied"]

    def test_audit_never_contains_the_action_payload(self, svc):
        secretish = {**ACTION, "action": {**ACTION["action"], "description": "rotate key sk-abcdefghijklmnopqrstu"}}
        a = svc.request_approval("INC-1", "operational", secretish)
        svc.approve(a.approval_id, actor="alice")
        blob = str(svc.audit_events(a.approval_id))
        assert "sk-abcdefghijklmnopqrstu" not in blob and "rotate key" not in blob

    def test_approvals_package_never_imports_long_term_memory(self):
        """Authorization must not depend on Hindsight or the memory package."""
        import ast
        import glob
        import os
        import approvals
        for path in glob.glob(os.path.join(os.path.dirname(approvals.__file__), "*.py")):
            tree = ast.parse(open(path, encoding="utf-8").read())
            imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            imported |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            assert not any(m.split(".")[0] in ("memory", "hindsight_client") for m in imported), (path, imported)
