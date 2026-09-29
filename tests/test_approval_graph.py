"""
tests/test_approval_graph.py — The LangGraph approval checkpoint and the approval API.
The graph pauses with interrupt(); resuming only names an approval, and the
application re-validates it against the approval database. Nothing is executed.
"""

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import api.main as api_main
from agent.approval_flow import select_proposed_action
from agent.graph import WorkflowNotAwaitingApproval, build_graph, resume_agent, run_agent
from approvals import ApprovalService, ApprovalStore, action_hash
from memory import MemoryRecorder
from memory.fake_memory import FakeMemory
from tests.test_agent import ScriptedLLM
from tests.test_memory_graph import INCIDENT_2_LOGS, final_json, incident_2_timeline

INC = "INC-5002"
ACTIONS = [{"what": "Page the DBA", "owner": "SRE", "priority": "P2"},
           {"what": "Roll back order-service to v2.4.0", "owner": "SRE", "priority": "P1"}]


@pytest.fixture
def approvals(tmp_path):
    return ApprovalService(ApprovalStore(str(tmp_path / "approvals.sqlite3")))


@pytest.fixture
def cp_path(tmp_path):
    return str(tmp_path / "checkpoints.sqlite3")


def graph_with(cp_path, replies):
    """A fresh graph instance on the given checkpoint file (simulates a process restart)."""
    return build_graph(llm=ScriptedLLM(replies), checkpointer=SqliteSaver(sqlite3.connect(cp_path, check_same_thread=False)))


def start(approvals, cp_path, actions=ACTIONS):
    result = run_agent(INC, incident_2_timeline(), graph=graph_with(cp_path, [final_json(action_items=actions)]),
                       approvals=approvals)
    return result, result["approval"]["approval_id"], result["workflow"]["thread_id"]


def resume(approvals, cp_path, thread_id, approval_id):
    return resume_agent(thread_id, approval_id, graph=graph_with(cp_path, []), approvals=approvals)


# ── Proposal ─────────────────────────────────────────────────────────────────

def test_proposed_action_is_built_by_the_application():
    payload = select_proposed_action(INC, {"action_items": ACTIONS})
    assert payload == {"kind": "operational", "incident_id": INC,
                       "action": {"description": "Roll back order-service to v2.4.0", "owner": "SRE", "priority": "P1"},
                       "proposed_by": "llm_agent", "source_recommendation": "rec-2"}
    assert select_proposed_action(INC, {"action_items": []}) is None
    assert select_proposed_action(INC, {"_parse_failed": True, "action_items": ACTIONS}) is None


# ── Interrupt / resume ───────────────────────────────────────────────────────

class TestInterruptResume:
    def test_analysis_pauses_with_a_pending_approval(self, approvals, cp_path):
        result, approval_id, thread_id = start(approvals, cp_path)
        assert result["analysis_mode"] == "llm_agent"
        assert result["workflow"] == {"status": "awaiting_approval", "thread_id": thread_id}
        record = approvals.get(approval_id)
        assert record.status == "pending" and record.incident_id == INC and record.kind == "operational"
        assert record.payload["action"]["description"] == "Roll back order-service to v2.4.0"
        assert result["approval"]["action_hash"] == record.action_hash == action_hash(record.payload)

    def test_approved_then_resumed_from_persisted_state_is_validated_not_executed(self, approvals, cp_path):
        _, approval_id, thread_id = start(approvals, cp_path)
        approvals.approve(approval_id, actor="alice")
        out = resume(approvals, cp_path, thread_id, approval_id)   # brand-new graph instance
        assert out["workflow"]["status"] == "approval_validated"
        assert out["approval_result"]["status"] == "validated" and out["approval_result"]["executed"] is False
        assert out["approval"]["status"] == "approved"             # validated, NOT consumed
        assert out["incident_id"] == INC

    def test_resume_while_pending_keeps_waiting(self, approvals, cp_path):
        _, approval_id, thread_id = start(approvals, cp_path)
        out = resume(approvals, cp_path, thread_id, approval_id)
        assert out["workflow"]["status"] == "awaiting_approval"
        assert out["approval_result"]["error"] == "approval_still_pending"
        approvals.approve(approval_id, actor="alice")
        assert resume(approvals, cp_path, thread_id, approval_id)["workflow"]["status"] == "approval_validated"

    def test_rejected_approval_ends_denied(self, approvals, cp_path):
        _, approval_id, thread_id = start(approvals, cp_path)
        approvals.reject(approval_id, actor="bob", reason="unsafe during peak traffic")
        out = resume(approvals, cp_path, thread_id, approval_id)
        assert out["workflow"]["status"] == "approval_denied"
        assert out["approval_result"] == {"status": "denied", "code": "rejected", "executed": False}

    def test_expired_approval_ends_denied(self, tmp_path, cp_path):
        from datetime import datetime, timedelta, timezone
        now = [datetime(2026, 9, 29, tzinfo=timezone.utc)]
        svc = ApprovalService(ApprovalStore(str(tmp_path / "a.sqlite3")), now=lambda: now[0], default_ttl_seconds=60)
        _, approval_id, thread_id = start(svc, cp_path)
        svc.approve(approval_id, actor="alice")
        now[0] += timedelta(minutes=5)
        out = resume(svc, cp_path, thread_id, approval_id)
        assert out["approval_result"]["code"] == "expired" and svc.get(approval_id).status == "expired"

    @pytest.mark.parametrize("resume_value", [
        {"approved": True},
        {"approval_id": None, "approved": True},
        True,
        "approved",
    ])
    def test_client_cannot_bypass_with_approved_true(self, approvals, cp_path, resume_value):
        _, approval_id, thread_id = start(approvals, cp_path)
        graph = graph_with(cp_path, [])
        config = {"configurable": {"thread_id": thread_id, "approval_service": approvals}}
        state = graph.invoke(Command(resume=resume_value), config=config)
        assert state["approval_result"]["status"] == "awaiting"      # still paused
        assert state.get("__interrupt__")
        assert approvals.get(approval_id).status == "pending"         # nothing was decided

    def test_extra_fields_next_to_a_valid_reference_are_refused(self, approvals, cp_path):
        _, approval_id, thread_id = start(approvals, cp_path)
        approvals.approve(approval_id, actor="alice")
        graph = graph_with(cp_path, [])
        config = {"configurable": {"thread_id": thread_id, "approval_service": approvals}}
        state = graph.invoke(Command(resume={"approval_id": approval_id, "approved": True}), config=config)
        assert state["approval_result"]["error"] == "invalid_resume_reference"

    def test_invalid_or_foreign_approval_id_fails_safely(self, approvals, cp_path):
        _, approval_a, thread_a = start(approvals, cp_path)
        _, approval_b, _ = start(approvals, cp_path)
        approvals.approve(approval_b, actor="alice")          # approved, but for a different thread
        for bogus in ("apr_does_not_exist", approval_b):
            out = resume(approvals, cp_path, thread_a, bogus)
            assert out["workflow"]["status"] == "awaiting_approval"
            assert out["approval_result"]["error"] == "invalid_resume_reference"
        denials = [e for e in approvals.audit_events(approval_a) if e["event_type"] == "approval_denied"]
        assert [d["reason"] for d in denials] == ["invalid_resume_reference"] * 2

    def test_unknown_or_finished_thread_is_not_resumable(self, approvals, cp_path):
        with pytest.raises(WorkflowNotAwaitingApproval):
            resume(approvals, cp_path, "no-such-thread", "apr_x")
        _, approval_id, thread_id = start(approvals, cp_path)
        approvals.reject(approval_id, actor="bob", reason="no")
        resume(approvals, cp_path, thread_id, approval_id)
        with pytest.raises(WorkflowNotAwaitingApproval):
            resume(approvals, cp_path, thread_id, approval_id)

    def test_tampered_payload_in_database_is_not_validated(self, approvals, cp_path):
        """If the stored approval's hash no longer matches the graph's proposed action, validation fails."""
        _, approval_id, thread_id = start(approvals, cp_path)
        approvals.approve(approval_id, actor="alice")
        conn = sqlite3.connect(approvals.store.path)
        conn.execute("UPDATE approvals SET action_hash = ? WHERE approval_id = ?",
                     (action_hash({"action": "something else"}), approval_id))
        conn.commit(); conn.close()
        out = resume(approvals, cp_path, thread_id, approval_id)
        assert out["approval_result"]["code"] == "hash_mismatch"

    def test_no_approval_without_an_actionable_recommendation(self, approvals, cp_path):
        result = run_agent(INC, incident_2_timeline(),
                           graph=graph_with(cp_path, [final_json(action_items=[])]), approvals=approvals)
        assert result["workflow"]["status"] == "completed" and result["approval"] is None

    def test_without_approval_service_graph_behaves_as_before(self, cp_path):
        result = run_agent(INC, incident_2_timeline(), graph=graph_with(cp_path, [final_json()]))
        assert result["workflow"]["status"] == "completed" and result["approval"] is None

    def test_approval_does_not_retain_anything_to_memory(self, approvals, cp_path):
        memory = FakeMemory()
        result = run_agent(INC, incident_2_timeline(), graph=graph_with(cp_path, [final_json(action_items=ACTIONS)]),
                           recorder=MemoryRecorder(memory), approvals=approvals)
        approvals.approve(result["approval"]["approval_id"], actor="alice")
        resume(approvals, cp_path, result["workflow"]["thread_id"], result["approval"]["approval_id"])
        assert sorted(r.metadata["event_type"] for r in memory.records()) == ["evidence_digest", "incident"]


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    llm = ScriptedLLM([final_json(action_items=ACTIONS)])
    monkeypatch.setattr(api_main, "build_graph", lambda **kw: build_graph(llm=llm, **kw))
    monkeypatch.setattr(api_main, "build_memory_recorder", lambda: MemoryRecorder(FakeMemory()))
    monkeypatch.setattr(api_main, "embed_timeline", lambda tl, iid: None)
    with TestClient(api_main.app) as c:
        yield c


def _analyze(client):
    return client.post("/analyze", data={"incident_id": INC},
                       files={"log_file": ("logs.txt", INCIDENT_2_LOGS.encode(), "text/plain")}).json()["postmortem"]


class TestApprovalAPI:
    def test_full_lifecycle_through_the_api(self, client):
        pm = _analyze(client)
        approval_id, thread_id = pm["approval"]["approval_id"], pm["workflow"]["thread_id"]
        assert pm["workflow"]["status"] == "awaiting_approval"

        got = client.get(f"/approvals/{approval_id}").json()
        assert got["approval"]["status"] == "pending" and got["audit"][0]["event_type"] == "approval_requested"

        # A client posting approved=true cannot skip the decision.
        r = client.post(f"/workflows/{thread_id}/resume", data={"approval_id": approval_id, "approved": "true"})
        assert r.json()["workflow"]["status"] == "awaiting_approval"

        r = client.post(f"/approvals/{approval_id}/approve", data={"actor": "dev-tester", "reason": "off-peak"})
        assert r.status_code == 200 and r.json()["status"] == "approved" and r.json()["decided_by"] == "dev-tester"

        r = client.post(f"/workflows/{thread_id}/resume", data={"approval_id": approval_id})
        body = r.json()
        assert body["workflow"]["status"] == "approval_validated"
        assert body["approval_result"]["executed"] is False and body["approval"]["status"] == "approved"

        events = [e["event_type"] for e in client.get(f"/approvals/{approval_id}").json()["audit"]]
        assert events == ["approval_requested", "approval_denied", "approval_approved"]

    def test_reject_requires_actor_and_reason_and_is_final(self, client):
        approval_id = _analyze(client)["approval"]["approval_id"]
        assert client.post(f"/approvals/{approval_id}/reject", data={"actor": "bob"}).status_code == 422
        r = client.post(f"/approvals/{approval_id}/reject", data={"actor": "bob", "reason": "peak traffic"})
        assert r.json()["status"] == "rejected"
        r = client.post(f"/approvals/{approval_id}/approve", data={"actor": "alice"})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "rejected"

    def test_dev_approval_endpoint_is_disabled_by_default(self, client, monkeypatch):
        monkeypatch.delenv("ENABLE_DEV_APPROVAL_ENDPOINT", raising=False)
        payload = {"kind": "operational", "incident_id": "INC-9", "action": {"description": "x"}}
        r = client.post("/approvals", json={"incident_id": "INC-9", "kind": "operational", "payload": payload})
        assert r.status_code == 404

    def test_request_approval_endpoint_when_enabled(self, client, monkeypatch):
        monkeypatch.setenv("ENABLE_DEV_APPROVAL_ENDPOINT", "1")
        payload = {"kind": "operational", "incident_id": "INC-9", "action": {"description": "x"}}
        r = client.post("/approvals", json={"incident_id": "INC-9", "kind": "operational", "payload": payload})
        assert r.status_code == 200 and r.json()["status"] == "pending"
        assert r.json()["action_hash"] == action_hash(payload)
        assert r.json()["requested_by"] == "dev-endpoint"
        r = client.post("/approvals", json={"incident_id": "INC-9", "kind": "shell", "payload": payload})
        assert r.status_code == 422

    def test_dev_created_approval_is_not_an_authorization_bypass(self, client, monkeypatch):
        """A directly created approval authorizes nothing: it is pending, needs a human
        decision, must match exactly on consume, and cannot drive an analysis workflow."""
        monkeypatch.setenv("ENABLE_DEV_APPROVAL_ENDPOINT", "1")
        approvals = api_main.app.state.approvals
        pm = _analyze(client)
        own_id, thread_id = pm["approval"]["approval_id"], pm["workflow"]["thread_id"]
        own_payload = approvals.get(own_id).payload

        # Forge an approval for the very same incident and identical payload.
        forged = client.post("/approvals", json={"incident_id": INC, "kind": "operational",
                                                 "payload": own_payload}).json()
        assert forged["status"] == "pending" and forged["action_hash"] == pm["approval"]["action_hash"]

        # Not consumable while pending.
        from approvals import ApprovalDenied
        with pytest.raises(ApprovalDenied) as e:
            approvals.consume(forged["approval_id"], incident_id=INC, kind="operational",
                              payload=own_payload, actor="executor")
        assert e.value.code == "pending"

        # Even once approved, it cannot resume the workflow: only the workflow's own approval can.
        client.post(f"/approvals/{forged['approval_id']}/approve", data={"actor": "dev-tester"})
        r = client.post(f"/workflows/{thread_id}/resume", data={"approval_id": forged["approval_id"]}).json()
        assert r["workflow"]["status"] == "awaiting_approval"
        assert r["approval_result"]["error"] == "invalid_resume_reference"
        assert approvals.get(own_id).status == "pending"

        # And consuming it still requires the exact incident, kind and payload.
        for bad in ({"incident_id": "INC-OTHER"}, {"kind": "repository_commit"},
                    {"payload": {**own_payload, "action": {"description": "rm -rf /"}}}):
            args = {"incident_id": INC, "kind": "operational", "payload": own_payload, **bad}
            with pytest.raises(ApprovalDenied):
                approvals.consume(forged["approval_id"], actor="executor", **args)

    def test_unknown_approval_and_thread(self, client):
        assert client.get("/approvals/apr_nope").status_code == 404
        assert client.post("/approvals/apr_nope/approve", data={"actor": "a"}).status_code == 404
        assert client.post("/workflows/no-thread/resume", data={"approval_id": "apr_nope"}).status_code == 404
