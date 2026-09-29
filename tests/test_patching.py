"""
tests/test_patching.py — Stage 6: approval-gated patch preparation in isolated,
application-owned worktrees. The original target repository (and the
application repository) must remain byte-identical under every scenario, and
no candidate is ever created before a repository_apply approval is consumed.
"""

import ast
import glob
import inspect
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from dulwich import porcelain
from dulwich.objects import Blob, Commit, Tree
from dulwich.repo import Repo
from langgraph.checkpoint.sqlite import SqliteSaver

from agent.graph import WorkflowNotAwaitingApproval, build_graph, resume_agent, run_agent, tools
from approvals import ApprovalService, ApprovalStore
from memory import MemoryRecorder
from memory.fake_memory import FakeMemory
from patching import (
    PatchConfig, PatchError, PatchExecutor, PatchIntent, PatchLimits, WorktreeManager,
    application_repo_root, check_tree_component, prepare_patch_request, validate_workspace,
)
from patching.apply import apply_patch
from patching.policy import application_root
from patching.worktree import METADATA_NAME
from repo_inspection import InspectionLimits, RepositoryInspector, collect_repository_evidence
from tests.repo_fixture import make_custom_repo, make_outside_link, make_repo, tree_digest
from tests.test_repo_inspection import GOOD, TRACE, analyst_reply
from tests.test_agent import ScriptedLLM


FIX = {"expected_old_content": "    return user_id  # BUG: user_id is undefined -> NameError -> HTTP 500\n",
       "replacement_content": "    return session\n"}

GOOD_PATCH = {**GOOD, "patch": {"operation": "modify", **FIX}}
INC = "INC-PATCH-1"


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path)


@pytest.fixture
def config(tmp_path):
    return PatchConfig(workspace=tmp_path / "patch-workspace")


@pytest.fixture
def approvals(tmp_path):
    return ApprovalService(ApprovalStore(str(tmp_path / "patch-approvals.sqlite3")))


def inspect_and_pin(repo, timeline=None):
    insp = RepositoryInspector(str(repo))
    result = collect_repository_evidence(insp, timeline or [{"content": 'File "src/auth.py", line 5'}])
    known = {f["evidence_id"] for f in result.findings}
    return insp, known, result.head_sha


def request(repo, proposal, cfg, *, pinned=None, timeline=None, incident=INC):
    """Phase 1 only (read-only): (result, payload, inspector)."""
    insp, known, head = inspect_and_pin(repo, timeline)
    result, payload = prepare_patch_request(proposal=proposal, inspector=insp, incident_id=incident,
                                            known_repository_ids=known, config=cfg,
                                            pinned_base_sha=pinned if pinned is not None else head)
    return result, payload, insp


def approve(approvals, payload, incident=INC, kind="repository_apply"):
    a = approvals.request_approval(incident, kind, payload)
    approvals.approve(a.approval_id, actor="reviewer")
    return a.approval_id


def run(repo, proposal, cfg, *, pinned=None, timeline=None, approvals=None):
    """The full approved path: request -> human approves -> executor consumes -> candidate."""
    result, payload, insp = request(repo, proposal, cfg, pinned=pinned, timeline=timeline)
    if payload is None:
        return result, insp
    approvals = approvals or ApprovalService(
        ApprovalStore(str(Path(cfg.workspace).parent / f"apr-{uuid.uuid4().hex[:8]}.sqlite3")))
    approval_id = approve(approvals, payload)
    return PatchExecutor(approvals, insp, cfg).execute(approval_id, incident_id=INC, payload=payload), insp


def candidates(cfg):
    ws = Path(cfg.workspace)
    return list(ws.rglob("candidate-*")) if ws.exists() else []


def timeline():
    return [{"event_id": 0, "timestamp": datetime(2026, 9, 29), "timestamp_str": "x", "source": "log",
             "level": "ERROR", "content": TRACE[0]["content"]}]


def raw_commit(root: Path, entries: dict) -> str:
    """A repository whose single commit is built directly from objects, so tree
    entry names can be anything Git's object format allows (hostile names).
    entries: {name_bytes: bytes | nested dict}."""
    root.mkdir(parents=True)
    porcelain.init(str(root))
    r = Repo(str(root))

    def build(spec):
        tree = Tree()
        for name, value in spec.items():
            if isinstance(value, dict):
                tree.add(name, 0o40000, build(value))
            else:
                blob = Blob.from_string(value)
                r.object_store.add_object(blob)
                tree.add(name, 0o100644, blob.id)
        r.object_store.add_object(tree)
        return tree.id

    c = Commit()
    c.tree = build(entries)
    c.author = c.committer = b"Dev <dev@example.com>"
    c.author_time = c.commit_time = int(time.time())
    c.author_timezone = c.commit_timezone = 0
    c.message = b"crafted"
    r.object_store.add_object(c)
    r.refs[b"refs/heads/master"] = c.id
    r.close()
    return c.id.decode()


def all_files(root: Path) -> set:
    return {str(p) for p in Path(root).rglob("*") if p.is_file()} if Path(root).exists() else set()


# ── Happy path + the hard immutability invariant ─────────────────────────────

class TestHappyPath:
    def test_candidate_patch_is_prepared_and_verified(self, repo, config):
        before = tree_digest(repo)
        result, insp = run(repo, GOOD_PATCH, config)
        assert result.status == "verification_passed", result.reason
        assert result.base_sha and len(result.base_sha) == 40
        assert result.changed_files == ["src/auth.py"]
        assert result.diff_summary["file"] == "src/auth.py"
        assert result.diff_summary["added"] == 1 and result.diff_summary["removed"] == 1
        assert result.diff_summary["binary"] is False
        assert result.patch_fingerprint.startswith("sha256:")
        assert result.verification["status"] == "passed"
        assert result.verification["tests"]["status"] == "not_run"
        assert result.original_repo_unchanged is True
        assert result.candidate_modified is True
        # the worktree exists outside the target and holds the patched content
        assert result.worktree and Path(result.worktree["path"]).is_dir()
        assert not Path(result.worktree["path"]).is_relative_to(repo)
        patched = (Path(result.worktree["path"]) / "src" / "auth.py").read_text()
        assert "return session" in patched and "return user_id" not in patched
        assert tree_digest(repo) == before

    def test_original_repository_is_byte_identical_after_full_flow(self, repo, config):
        """MANDATORY invariant: inspection + proposal + approval + worktree +
        apply + verify leaves the original repository byte-identical (every
        file, .git included), with HEAD, branch and status unchanged."""
        before = tree_digest(repo)
        result, insp = run(repo, GOOD_PATCH, config)
        assert result.status == "verification_passed"
        after = tree_digest(repo)
        assert after == before
        assert set(after) == set(before)
        from repo_inspection.git_read import head_info, status
        assert head_info(repo)["head_sha"] == result.base_sha
        assert head_info(repo)["branch"] == "master"
        assert status(repo, InspectionLimits())["clean"] is True

    def test_request_phase_is_read_only(self, repo, config):
        before = tree_digest(repo)
        result, payload, _ = request(repo, GOOD_PATCH, config)
        assert result.status == "approval_required"
        assert payload["kind"] == "repository_apply" and payload["incident_id"] == INC
        assert payload["base_sha"] == result.base_sha and payload["patch"]["file"] == "src/auth.py"
        assert payload["patch"]["expected_old_content"] == FIX["expected_old_content"]
        assert not Path(config.workspace).exists()          # nothing created, not even the workspace
        assert tree_digest(repo) == before

    def test_without_patch_config_nothing_happens(self, repo):
        llm = ScriptedLLM([analyst_reply(GOOD_PATCH)])
        before = tree_digest(repo)
        result = run_agent("INC-NOPATCH", timeline(), graph=build_graph(llm=llm),
                           inspector=RepositoryInspector(str(repo)))
        assert result["patch_candidate"]["status"] == "skipped"
        assert tree_digest(repo) == before


# ── S3: human approval gates every candidate ─────────────────────────────────

class GraphWorld:
    def __init__(self, tmp_path, repo, config):
        self.approvals = ApprovalService(ApprovalStore(str(tmp_path / "graph-approvals.sqlite3")))
        self.cp = str(tmp_path / "graph-checkpoints.sqlite3")
        self.repo, self.config = repo, config
        self.inspector = RepositoryInspector(str(repo))

    def graph(self, replies=()):
        return build_graph(llm=ScriptedLLM(list(replies)),
                           checkpointer=SqliteSaver(sqlite3.connect(self.cp, check_same_thread=False)))

    def start(self, proposal=GOOD_PATCH, incident="INC-PATCH-GRAPH", recorder=None):
        return run_agent(incident, timeline(), graph=self.graph([analyst_reply(proposal)]),
                         approvals=self.approvals, inspector=self.inspector,
                         patch_config=self.config, recorder=recorder)

    def resume(self, thread_id, approval_id, **kw):
        kw.setdefault("inspector", self.inspector)
        kw.setdefault("patch_config", self.config)
        return resume_agent(thread_id, approval_id, graph=self.graph(), approvals=self.approvals, **kw)


@pytest.fixture
def world(tmp_path, repo, config):
    return GraphWorld(tmp_path, repo, config)


class TestApprovalGate:
    def test_graph_pauses_before_any_candidate_is_created(self, world):
        before = tree_digest(world.repo)
        result = world.start()
        assert result["workflow"]["status"] == "awaiting_approval"
        cand = result["patch_candidate"]
        assert cand["status"] == "approval_required" and cand["candidate_modified"] is False
        assert cand["worktree"] is None
        req = result["patch_approval"]
        assert req["kind"] == "repository_apply" and req["status"] == "pending"
        record = world.approvals.get(req["approval_id"])
        assert record.payload["base_sha"] == result["repository_inspection"]["head_sha"]
        assert record.payload["patch"]["replacement_content"] == FIX["replacement_content"]
        assert not Path(world.config.workspace).exists()     # no candidate, no workspace
        assert tree_digest(world.repo) == before

    def test_approved_patch_is_applied_after_consume(self, world):
        before = tree_digest(world.repo)
        result = world.start()
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        world.approvals.approve(approval_id, actor="alice", reason="fix looks right")
        out = world.resume(thread_id, approval_id)
        cand = out["patch_candidate"]
        assert cand["status"] == "verification_passed", cand
        assert cand["approval_id"] == approval_id and cand["candidate_modified"] is True
        assert out["patch_approval"]["status"] == "consumed"
        events = [e["event_type"] for e in world.approvals.audit_events(approval_id)]
        assert events == ["approval_requested", "approval_approved", "approval_consumed"]
        assert Path(cand["worktree"]["path"]).is_dir()
        assert tree_digest(world.repo) == before

    def test_rejected_approval_creates_nothing(self, world):
        result = world.start()
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        world.approvals.reject(approval_id, actor="alice", reason="not now")
        out = world.resume(thread_id, approval_id)
        assert out["patch_candidate"]["status"] == "approval_denied"
        assert out["patch_approval_result"]["code"] == "rejected"
        assert candidates(world.config) == []

    def test_pending_approval_keeps_waiting(self, world):
        result = world.start()
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        out = world.resume(thread_id, approval_id)           # no decision yet
        assert out["workflow"]["status"] == "awaiting_approval"
        assert out["patch_approval_result"]["error"] == "approval_still_pending"
        assert candidates(world.config) == []

    def test_resume_may_only_name_the_workflows_own_approval(self, world):
        result = world.start()
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        # a different, APPROVED repository_apply approval for the same payload
        other = approve(world.approvals, world.approvals.get(approval_id).payload,
                        incident="INC-PATCH-GRAPH")
        out = world.resume(thread_id, other)
        assert out["workflow"]["status"] == "awaiting_approval"
        assert out["patch_approval_result"]["error"] == "invalid_resume_reference"
        assert world.approvals.get(other).status == "approved"       # never consumed
        assert candidates(world.config) == []

    def test_approval_is_single_use(self, world):
        result = world.start()
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        world.approvals.approve(approval_id, actor="alice")
        world.resume(thread_id, approval_id)
        assert len(candidates(world.config)) == 1
        with pytest.raises(WorkflowNotAwaitingApproval):
            world.resume(thread_id, approval_id)
        payload = world.approvals.get(approval_id).payload
        again = PatchExecutor(world.approvals, world.inspector, world.config).execute(
            approval_id, incident_id="INC-PATCH-GRAPH", payload=payload)
        assert again.status == "not_authorized" and again.reason == "consumed"
        assert len(candidates(world.config)) == 1

    def test_without_approval_service_nothing_is_applied(self, repo, config):
        before = tree_digest(repo)
        result = run_agent("INC-NO-APPROVALS", timeline(),
                           graph=build_graph(llm=ScriptedLLM([analyst_reply(GOOD_PATCH)])),
                           inspector=RepositoryInspector(str(repo)), patch_config=config)
        assert result["patch_candidate"]["status"] == "approval_unavailable"
        assert result["patch_approval"] is None
        assert not Path(config.workspace).exists()
        assert tree_digest(repo) == before

    def test_resume_without_patch_configuration_applies_nothing(self, world):
        result = world.start()
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        world.approvals.approve(approval_id, actor="alice")
        out = world.resume(thread_id, approval_id, inspector=None, patch_config=None)
        assert out["patch_candidate"]["status"] == "not_executable"
        assert world.approvals.get(approval_id).status == "approved"   # not consumed
        assert candidates(world.config) == []

    def test_interrupt_node_performs_no_writes(self):
        from agent import patch_flow
        src = inspect.getsource(patch_flow.await_patch_approval_node)
        tree = ast.parse(src)
        calls = {getattr(n.func, "id", getattr(n.func, "attr", None))
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert calls == {"interrupt"}

    def test_approval_functions_are_not_model_tools(self):
        assert [t.name for t in tools] == ["search_timeline"]


class TestLoneSurrogate:
    """JSON can carry lone surrogates ("\\ud800"). They must be refused during
    intent validation — before approval hashing, which would otherwise raise
    UnicodeEncodeError and end the whole analysis as agent_error."""

    @pytest.mark.parametrize("mutate", [
        lambda p: {**p, "patch": {**p["patch"],
                                  "expected_old_content": p["patch"]["expected_old_content"] + "\ud800"}},
        lambda p: {**p, "patch": {**p["patch"], "replacement_content": "    return session  # \udfff\n"}},
        lambda p: {**p, "rationale": "NameError \ud83d causes HTTP 500"},
        lambda p: {**p, "verification_plan": ["run tests/test_auth.py \udc00"]},
    ], ids=["expected_old_content", "replacement_content", "rationale", "verification_plan"])
    def test_lone_surrogate_is_refused_before_any_approval(self, world, tmp_path, mutate):
        before = tree_digest(world.repo)
        result = world.start(proposal=mutate(GOOD_PATCH))
        assert result["analysis_mode"] == "llm_agent"                 # not agent_error
        cand = result["patch_candidate"]
        assert cand["status"] == "proposal_invalid" and "not valid UTF-8" in cand["reason"]
        assert result["patch_approval"] is None
        assert result["workflow"]["status"] != "awaiting_approval"
        conn = sqlite3.connect(str(tmp_path / "graph-approvals.sqlite3"))
        try:
            assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        finally:
            conn.close()
        assert not Path(world.config.workspace).exists()             # no candidate, no workspace
        assert tree_digest(world.repo) == before


class TestApprovalBinding:
    """The approval hash binds patch intent + pinned base + target + incident."""

    def setup(self, repo, config, approvals):
        result, payload, insp = request(repo, GOOD_PATCH, config)
        assert result.status == "approval_required"
        return payload, insp, approve(approvals, payload)

    def test_changed_patch_invalidates_the_approval(self, repo, config, approvals):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        tampered = {**payload, "patch": {**payload["patch"], "replacement_content": "    return 42\n"}}
        out = PatchExecutor(approvals, insp, config).execute(approval_id, incident_id=INC, payload=tampered)
        assert out.status == "not_authorized" and out.reason == "hash_mismatch"
        assert candidates(config) == [] and approvals.get(approval_id).status == "approved"

    def test_changed_base_sha_invalidates_the_approval(self, repo, config, approvals):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        parent = Repo(str(repo))[payload["base_sha"].encode()].parents[0].decode()
        tampered = {**payload, "base_sha": parent}
        out = PatchExecutor(approvals, insp, config).execute(approval_id, incident_id=INC, payload=tampered)
        assert out.status == "base_changed"
        assert candidates(config) == [] and approvals.get(approval_id).status == "approved"

    def test_target_moving_after_approval_fails_closed(self, repo, config, approvals):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        (repo / "extra.txt").write_text("x")
        porcelain.add(str(repo), paths=[str(repo / "extra.txt")])
        porcelain.commit(str(repo), message=b"move on", author=b"Dev <d@e.com>", committer=b"Dev <d@e.com>")
        out = PatchExecutor(approvals, insp, config).execute(approval_id, incident_id=INC, payload=payload)
        assert out.status == "base_changed"
        assert candidates(config) == []
        codes = [e["reason"] for e in approvals.audit_events(approval_id) if e["event_type"] == "approval_denied"]
        assert "base_changed" in codes

    def test_changed_target_invalidates_the_approval(self, repo, config, approvals, tmp_path):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        other = make_repo(tmp_path / "other")
        out = PatchExecutor(approvals, RepositoryInspector(str(other)), config).execute(
            approval_id, incident_id=INC, payload=payload)
        assert out.status == "target_changed"
        tampered = {**payload, "target_repository": str(other.resolve())}
        out = PatchExecutor(approvals, RepositoryInspector(str(other)), config).execute(
            approval_id, incident_id=INC, payload=tampered)
        # identical fixture content can even give the same commit SHA; then
        # the hash binding is what refuses it
        assert (out.status, out.reason) in {("not_authorized", "hash_mismatch")} or out.status == "base_changed"
        assert candidates(config) == [] and approvals.get(approval_id).status == "approved"

    def test_changed_incident_invalidates_the_approval(self, repo, config, approvals):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        out = PatchExecutor(approvals, insp, config).execute(
            approval_id, incident_id="INC-OTHER", payload={**payload, "incident_id": "INC-OTHER"})
        assert out.status == "not_authorized" and out.reason == "wrong_incident"
        out = PatchExecutor(approvals, insp, config).execute(approval_id, incident_id="INC-OTHER", payload=payload)
        assert out.status == "not_executable" and out.reason == "wrong_incident"
        assert candidates(config) == []

    def test_operational_approval_cannot_authorize_a_patch(self, repo, config, approvals):
        payload, insp, _ = self.setup(repo, config, approvals)
        wrong_kind = approve(approvals, payload, kind="operational")
        out = PatchExecutor(approvals, insp, config).execute(wrong_kind, incident_id=INC, payload=payload)
        assert out.status == "not_authorized" and out.reason == "wrong_kind"
        assert candidates(config) == []

    def test_unapproved_and_expired_approvals_do_not_authorize(self, repo, config, tmp_path):
        clock = {"now": datetime(2026, 9, 29, tzinfo=timezone.utc)}
        approvals = ApprovalService(ApprovalStore(str(tmp_path / "clock.sqlite3")), now=lambda: clock["now"])
        result, payload, insp = request(repo, GOOD_PATCH, config)
        pending = approvals.request_approval(INC, "repository_apply", payload).approval_id
        out = PatchExecutor(approvals, insp, config).execute(pending, incident_id=INC, payload=payload)
        assert out.status == "not_authorized" and out.reason == "pending"
        approvals.approve(pending, actor="alice")
        clock["now"] += timedelta(days=2)
        out = PatchExecutor(approvals, insp, config).execute(pending, incident_id=INC, payload=payload)
        assert out.status == "not_authorized" and out.reason == "expired"
        assert candidates(config) == []

    def test_consume_happens_before_the_first_write(self, repo, config, approvals, monkeypatch):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        order = []
        real_consume, real_create = approvals.consume, WorktreeManager.create
        monkeypatch.setattr(approvals, "consume", lambda *a, **k: (order.append("consume"), real_consume(*a, **k))[1])
        monkeypatch.setattr(WorktreeManager, "create",
                            lambda self, *a, **k: (order.append("create"), real_create(self, *a, **k))[1])
        out = PatchExecutor(approvals, insp, config).execute(approval_id, incident_id=INC, payload=payload)
        assert out.status == "verification_passed"
        assert order == ["consume", "create"]

    def test_malformed_payload_is_not_executable(self, repo, config, approvals):
        payload, insp, approval_id = self.setup(repo, config, approvals)
        for bad in ({**payload, "command": "git apply"}, {**payload, "kind": "operational"},
                    {**payload, "patch": {**payload["patch"], "path": "/etc/passwd"}}):
            out = PatchExecutor(approvals, insp, config).execute(approval_id, incident_id=INC, payload=bad)
            assert out.status == "not_executable"
        assert approvals.get(approval_id).status == "approved" and candidates(config) == []


# ── Fail-closed scenarios ────────────────────────────────────────────────────

class TestFailClosed:
    def test_dirty_working_tree_is_rejected(self, tmp_path, config):
        repo = make_repo(tmp_path, dirty=True)
        result, _ = run(repo, GOOD_PATCH, config)
        assert result.status == "repository_dirty"
        assert "uncommitted changes" in result.reason
        assert "src/auth.py" in result.changed_files
        assert candidates(config) == []

    def test_status_limited_is_treated_as_dirty(self, tmp_path):
        repo = make_repo(tmp_path)
        config = PatchConfig(workspace=tmp_path / "ws",
                             inspection_limits=InspectionLimits(max_tracked_files=1))
        result, _ = run(repo, GOOD_PATCH, config)
        assert result.status == "repository_dirty"

    def test_base_changed_after_inspection_fails_closed(self, tmp_path, config):
        repo = make_repo(tmp_path)
        insp, known, head = inspect_and_pin(repo)
        (repo / "extra.txt").write_text("x")
        porcelain.add(str(repo), paths=[str(repo / "extra.txt")])
        porcelain.commit(str(repo), message=b"move on", author=b"Dev <d@e.com>",
                         committer=b"Dev <d@e.com>")
        result, payload = prepare_patch_request(proposal=GOOD_PATCH, inspector=insp, incident_id="x",
                                                known_repository_ids=known, config=config,
                                                pinned_base_sha=head)
        assert result.status == "base_changed" and payload is None
        assert "moved" in result.reason and head[:12] in result.reason
        assert candidates(config) == []

    def test_no_pinned_base_is_unavailable(self, repo, config):
        result, _ = run(repo, GOOD_PATCH, config, pinned="")
        assert result.status == "base_unavailable"

    @pytest.mark.parametrize("bad, why", [
        ({**GOOD_PATCH, "file": "src/nope.py"}, "not_found"),
        ({**GOOD_PATCH, "file": ".env"}, "sensitive"),
        ({**GOOD_PATCH, "file": "../outside.txt"}, "traversal"),
        ({**GOOD_PATCH, "file": "src/../../outside.txt"}, "traversal"),
        ({**GOOD_PATCH, "file": ".git/../outside.py"}, "traversal"),
        ({**GOOD_PATCH, "file": "C:/Windows/win.ini"}, "absolute"),
        ({**GOOD_PATCH, "file": "\\\\server\\share\\x"}, "absolute"),
        ({**GOOD_PATCH, "file": ".git/config"}, "git_internals"),
        ({**GOOD_PATCH, "command": "git apply x.patch"}, "unexpected"),
        ({**GOOD_PATCH, "script": "fix.sh"}, "unexpected"),
        ({**GOOD_PATCH, "patch": [{"operation": "modify", **FIX}, {"operation": "modify", **FIX}]}, "object"),
        ({**GOOD_PATCH, "patch": {"operation": "delete"}}, "operation"),
        ({**GOOD_PATCH, "patch": {"operation": "modify", "expected_old_content": "x",
                                   "replacement_content": "y", "extra": 1}}, "exactly"),
    ])
    def test_malicious_or_invalid_proposals_fail_closed(self, repo, config, bad, why):
        result, _ = run(repo, bad, config)
        assert result.status == "proposal_invalid", result.reason
        assert why in result.reason
        assert candidates(config) == []

    def test_expected_content_missing_is_rejected(self, repo, config):
        bad = {**GOOD_PATCH, "patch": {"operation": "modify",
                                       "expected_old_content": "bogus content that is not there\n",
                                       "replacement_content": "x = 1\n"}}
        result, _ = run(repo, bad, config)
        assert result.status == "patch_rejected" and "not found" in result.reason
        assert result.worktree["cleaned_up"] is True and candidates(config) == []

    def test_expected_content_ambiguous_is_rejected(self, tmp_path, config):
        repo = make_custom_repo(tmp_path, {"src/app.py": b"x = 1\nx = 1\n"})
        proposal = {**GOOD_PATCH, "file": "src/app.py",
                    "patch": {"operation": "modify", "expected_old_content": "x = 1\n",
                              "replacement_content": "x = 2\n"}}
        result, _ = run(repo, proposal, config)
        assert result.status == "patch_rejected" and "ambiguous" in result.reason

    def test_diff_too_large_is_rejected(self, repo, tmp_path):
        config = PatchConfig(workspace=tmp_path / "ws", limits=PatchLimits(max_lines_added=5))
        filler = "".join(f"    # padding {i}\n" for i in range(10))
        bad = {**GOOD_PATCH, "patch": {"operation": "modify",
                                       "expected_old_content": FIX["expected_old_content"],
                                       "replacement_content": "    return session\n" + filler}}
        result, _ = run(repo, bad, config)
        assert result.status == "patch_rejected" and "lines" in result.reason

    def test_binary_target_is_rejected(self, tmp_path, config):
        repo = make_custom_repo(tmp_path, {"blob.bin": b"\x00\x01\x02binary\x00"})
        proposal = {**GOOD_PATCH, "file": "blob.bin",
                    "patch": {"operation": "modify", "expected_old_content": "\x00\x01",
                              "replacement_content": "\x02\x03"}}
        result, _ = run(repo, proposal, config)
        assert result.status == "patch_rejected" and "binary" in result.reason

    def test_symlink_escape_path_is_rejected(self, tmp_path, config):
        repo = make_repo(tmp_path)
        outside = tmp_path / "outside6"
        outside.mkdir()
        (outside / "secret.cfg").write_text("x")
        make_outside_link(repo / "link", outside)
        bad = {**GOOD_PATCH, "file": "link/secret.cfg",
               "patch": {"operation": "modify", "expected_old_content": "x", "replacement_content": "y"}}
        result, _ = run(repo, bad, config)
        assert result.status == "proposal_invalid"
        assert candidates(config) == []

    def test_patch_application_failure_is_reported(self, repo, config, monkeypatch):
        before = tree_digest(repo)
        orig = Path.write_bytes

        def boom(self, data):
            # fail only on the APPLY write (the worktree file gaining the fix)
            if b"return session" in bytes(data):
                raise OSError("disk full (simulated)")
            return orig(self, data)

        monkeypatch.setattr(Path, "write_bytes", boom)
        result, _ = run(repo, GOOD_PATCH, config)
        assert result.status == "patch_application_failed"
        assert result.candidate_modified is False
        assert tree_digest(repo) == before

    def test_verification_failure_for_broken_python(self, repo, config):
        bad = {**GOOD_PATCH, "patch": {"operation": "modify",
                                       "expected_old_content": FIX["expected_old_content"],
                                       "replacement_content": "    return =  \n"}}
        result, _ = run(repo, bad, config)
        assert result.status == "verification_failed"
        assert "does not parse" in result.verification["reason"]

    def test_verification_not_run_when_disabled(self, repo, tmp_path):
        config = PatchConfig(workspace=tmp_path / "ws", verify="none")
        result, _ = run(repo, GOOD_PATCH, config)
        assert result.status == "verification_not_run"
        assert result.verification["status"] == "not_run"


# ── S1: hostile Git tree entry names ─────────────────────────────────────────

ESCAPE_TAG = uuid.uuid4().hex[:10]


class TestHostileTreeEntries:
    @pytest.mark.parametrize("name", [
        b"..\\escaped.txt",
        b"..\\..\\..\\escaped.txt",
        b"C:\\incigent-escape-" + ESCAPE_TAG.encode() + b".txt",
        b"C:escaped.txt",
        b"\\\\server\\share\\escaped.txt",
        b"\\\\?\\C:\\escaped.txt",
        b"\\escaped.txt",
        b"foo\\bar",
        b"foo/bar",
        b"foo:bar",
        b"file.txt::$DATA",
        b"evil\x01name",
        b"new\nline",
        b"tab\tname",
        b".", b"..", b".git", b".GIT", b"GIT~1",
        b"CON", b"nul.txt", b"com1", b"LPT9.log", b"CONIN$",
        b"trailing.", b"trailing ",
        b"",
        b"bad\xff-utf8",
    ])
    def test_unsafe_component_is_refused(self, name):
        with pytest.raises(PatchError) as e:
            check_tree_component(name)
        assert e.value.code == "unsafe_tree_entry"

    @pytest.mark.parametrize("name", [b"README.md", b".gitignore", b".github", b"src", b"a b.py",
                                      b"con_fig.py", b"console.py", b"caf\xc3\xa9.txt"])
    def test_ordinary_component_is_allowed(self, name):
        assert check_tree_component(name) == name.decode("utf-8")

    @pytest.mark.parametrize("entries, escape_paths", [
        ({b"ok.txt": b"fine\n", b"..\\escaped.txt": b"PWNED\n"}, ["../escaped.txt"]),
        ({b"ok.txt": b"fine\n", b"..\\..\\..\\escaped.txt": b"PWNED\n"},
         ["../../../escaped.txt", "../../escaped.txt", "../escaped.txt"]),
        ({b"src": {b"..\\..\\escaped.txt": b"PWNED\n"}}, ["../../escaped.txt", "../escaped.txt"]),
        ({b"C:\\incigent-escape-" + ESCAPE_TAG.encode() + b".txt": b"PWNED\n"},
         ["C:/incigent-escape-" + ESCAPE_TAG + ".txt"]),
        ({b"\\\\localhost\\C$\\incigent-escape.txt": b"PWNED\n"}, []),
        ({b"foo\\bar": b"x"}, ["foo/bar", "foo"]),
        ({b"foo:bar": b"x"}, ["foo"]),
        ({b"evil\x01": b"x"}, []),
        ({b"Readme.md": b"a\n", b"README.md": b"b\n"}, []),
        ({b"Src": {b"a.py": b"a"}, b"src": {b"b.py": b"b"}}, []),
    ])
    def test_materialization_fails_closed_and_writes_nothing_outside(self, tmp_path, entries, escape_paths):
        target = tmp_path / "hostile-repo"
        sha = raw_commit(target, entries)
        workspace = tmp_path / "deep" / "er" / "workspace"
        before_outside = all_files(tmp_path)
        manager = WorktreeManager(workspace)
        with pytest.raises(PatchError) as e:
            manager.create(target, sha, "INC-HOSTILE", PatchLimits(), patch_id="0123456789abcdef")
        assert e.value.code == "unsafe_tree_entry", str(e.value)
        candidate = workspace / "INC-HOSTILE" / "candidate-0123456789ab"
        assert not candidate.exists()                      # the partial candidate was removed
        assert all_files(tmp_path) == before_outside        # no file anywhere under tmp_path
        for rel in escape_paths:
            p = Path(os.path.normpath(str(candidate / rel))) if not Path(rel).is_absolute() else Path(rel)
            assert not p.exists(), p
        assert manager.list_worktrees() == []

    def test_containment_holds_even_if_name_validation_were_bypassed(self, tmp_path, monkeypatch):
        """Defence in depth: the pre-create containment check alone stops a
        '..\\' name before any file (even an empty one) is created outside."""
        if sys.platform != "win32":
            pytest.skip("'\\' is only a path separator on Windows")
        import patching.worktree as wt_mod
        monkeypatch.setattr(wt_mod, "check_tree_component", lambda raw: raw.decode("utf-8"))
        target = tmp_path / "hostile-repo"
        sha = raw_commit(target, {b"..\\escaped.txt": b"PWNED\n"})
        workspace = tmp_path / "ws"
        with pytest.raises(PatchError) as e:
            WorktreeManager(workspace).create(target, sha, "INC-H", PatchLimits(), patch_id="ab" * 8)
        assert e.value.code == "unsafe_tree_entry"
        assert not (workspace / "INC-H" / "escaped.txt").exists()
        assert not any(p.name == "escaped.txt" for p in tmp_path.rglob("*"))

    def test_symlink_and_submodule_entries_are_not_materialized(self, tmp_path):
        target = tmp_path / "linky"
        target.mkdir()
        porcelain.init(str(target))
        r = Repo(str(target))
        tree, blob, link = Tree(), Blob.from_string(b"ok\n"), Blob.from_string(b"../../outside")
        r.object_store.add_object(blob)
        r.object_store.add_object(link)
        tree.add(b"ok.txt", 0o100644, blob.id)
        tree.add(b"link", 0o120000, link.id)
        tree.add(b"sub", 0o160000, b"1" * 40)
        r.object_store.add_object(tree)
        c = Commit()
        c.tree, c.author, c.committer = tree.id, b"D <d@e.com>", b"D <d@e.com>"
        c.author_time = c.commit_time = int(time.time())
        c.author_timezone = c.commit_timezone = 0
        c.message = b"links"
        r.object_store.add_object(c)
        r.close()
        wt = WorktreeManager(tmp_path / "ws").create(target, c.id.decode(), "INC-L", PatchLimits())
        assert sorted(p.name for p in wt.root.iterdir()) == ["ok.txt"]


# ── S2: workspace placement ──────────────────────────────────────────────────

def top_level(path: Path) -> set:
    return set(os.listdir(path))


class TestWorkspacePlacement:
    @pytest.mark.parametrize("where", ["equal", "inside", "contains"])
    def test_workspace_overlapping_target_is_refused(self, tmp_path, where):
        repo = make_repo(tmp_path / "parent")
        ws = {"equal": repo, "inside": repo / "patch-ws", "contains": tmp_path}[where]
        cfg = PatchConfig(workspace=ws)
        before = tree_digest(repo)
        before_parent = all_files(tmp_path)
        result, payload, _ = request(repo, GOOD_PATCH, cfg)
        assert result.status == "workspace_unsafe" and payload is None
        assert "target repository" in result.reason
        with pytest.raises(PatchError) as e:
            WorktreeManager(ws).create(repo, result.base_sha or "0" * 40, INC, PatchLimits())
        assert e.value.code == "workspace_unsafe"
        assert tree_digest(repo) == before                      # byte-for-byte, .git included
        assert all_files(tmp_path) == before_parent             # no new file anywhere
        assert not (repo / "patch-ws").exists() and not (Path(ws) / METADATA_NAME).exists()

    @pytest.mark.parametrize("where", ["equal", "inside", "contains"])
    def test_workspace_overlapping_application_repo_is_refused(self, repo, where):
        app = application_root()
        ws = {"equal": app, "inside": app / f"patch-ws-{uuid.uuid4().hex[:8]}", "contains": app.parent}[where]
        cfg = PatchConfig(workspace=ws)
        app_before, parent_before = top_level(app), top_level(app.parent)
        result, payload, _ = request(repo, GOOD_PATCH, cfg)
        assert result.status == "workspace_unsafe" and payload is None
        assert "application repository" in result.reason
        with pytest.raises(PatchError) as e:
            WorktreeManager(ws).create(repo, "0" * 40, INC, PatchLimits())
        assert e.value.code == "workspace_unsafe"
        assert top_level(app) == app_before and top_level(app.parent) == parent_before
        assert not Path(ws, METADATA_NAME).exists()

    def test_case_variant_workspace_is_refused(self, tmp_path):
        if sys.platform != "win32":
            pytest.skip("case-insensitive paths are a Windows property")
        repo = make_repo(tmp_path)
        ws = Path(str(repo).upper()) / "patch-ws"
        with pytest.raises(PatchError) as e:
            validate_workspace(ws, target_root=repo, app_root=application_root())
        assert e.value.code == "workspace_unsafe"
        assert not (repo / "patch-ws").exists()

    def test_workspace_reaching_the_target_through_a_link_is_refused(self, tmp_path):
        repo = make_repo(tmp_path)
        link = tmp_path / "ws-link"
        try:
            make_outside_link(link, repo)
        except OSError:
            pytest.skip("cannot create a directory link here")
        before = tree_digest(repo)
        with pytest.raises(PatchError) as e:
            validate_workspace(link / "sub", target_root=repo, app_root=application_root())
        assert e.value.code == "workspace_unsafe"
        result, payload, _ = request(repo, GOOD_PATCH, PatchConfig(workspace=link / "sub"))
        assert result.status == "workspace_unsafe"
        assert tree_digest(repo) == before

    def test_executor_rechecks_the_workspace_before_consuming(self, repo, config, approvals):
        result, payload, insp = request(repo, GOOD_PATCH, config)
        approval_id = approve(approvals, payload)
        unsafe = PatchConfig(workspace=repo / "patch-ws")
        before = tree_digest(repo)
        out = PatchExecutor(approvals, insp, unsafe).execute(approval_id, incident_id=INC, payload=payload)
        assert out.status == "workspace_unsafe"
        assert approvals.get(approval_id).status == "approved"       # never consumed
        assert tree_digest(repo) == before


# ── C1 / C2 / C3 / C4 / C6 / C7 ──────────────────────────────────────────────

def intent_for(file, old, new):
    return PatchIntent(file=file, location="x", problem="p", proposed_change="c", rationale="r",
                       expected_effect="e", verification_plan=[], evidence_ids=["R001"],
                       expected_old_content=old, replacement_content=new)


class TestContentSafety:
    def test_non_utf8_file_is_rejected_and_left_byte_identical(self, tmp_path, config):
        original = b"# caf\xe9 latin-1 comment\nx = 1\n"
        repo = make_custom_repo(tmp_path, {"src/latin.py": original})
        proposal = {**GOOD_PATCH, "file": "src/latin.py",
                    "patch": {"operation": "modify", "expected_old_content": "x = 1\n",
                              "replacement_content": "x = 2\n"}}
        before = tree_digest(repo)
        result, _ = run(repo, proposal, config)
        assert result.status == "patch_rejected" and "UTF-8" in result.reason
        assert result.candidate_modified is False and candidates(config) == []
        assert tree_digest(repo) == before
        # directly at the apply layer: the candidate file stays byte-identical
        sha = Repo(str(repo)).head().decode()
        wt = WorktreeManager(tmp_path / "ws2").create(repo, sha, "INC-C1", PatchLimits())
        with pytest.raises(PatchError, match="UTF-8"):
            apply_patch(wt, intent_for("src/latin.py", "x = 1\n", "x = 2\n"), PatchLimits(),
                        path_limits=InspectionLimits(), repo=Repo(str(repo)), base_sha=sha)
        assert (wt.root / "src" / "latin.py").read_bytes() == original

    def test_only_the_intended_bytes_change(self, tmp_path, config):
        original = "# naïve ünïcode — keep me\r\nvalue = 'old'\n".encode("utf-8")
        repo = make_custom_repo(tmp_path, {"src/u.py": original})
        sha = Repo(str(repo)).head().decode()
        wt = WorktreeManager(tmp_path / "ws").create(repo, sha, "INC-B", PatchLimits())
        blob = (wt.root / "src" / "u.py").read_bytes()
        apply_patch(wt, intent_for("src/u.py", "value = 'old'\n", "value = 'new'\n"), PatchLimits(),
                    path_limits=InspectionLimits(), repo=Repo(str(repo)), base_sha=sha)
        assert (wt.root / "src" / "u.py").read_bytes() == blob.replace(b"value = 'old'\n", b"value = 'new'\n")

    def test_large_existing_file_with_small_change_passes(self, tmp_path, config):
        big = "".join(f"line_{i} = {i}\n" for i in range(5000)) + "TARGET = 1\n"
        repo = make_custom_repo(tmp_path, {"src/big.py": big.encode()})
        proposal = {**GOOD_PATCH, "file": "src/big.py",
                    "patch": {"operation": "modify", "expected_old_content": "TARGET = 1\n",
                              "replacement_content": "TARGET = 2\n"}}
        result, _ = run(repo, proposal, config)
        assert result.status == "verification_passed", (result.reason, result.verification)
        assert result.diff_summary["added"] == 1 and result.diff_summary["removed"] == 1

    def test_oversized_patch_is_rejected_before_any_write(self, tmp_path):
        """True added/removed lines (not net) and diff bytes are enforced
        in memory: replacing 3 lines with 3 different lines adds 3."""
        original = b"a = 1\nb = 2\nc = 3\n"
        repo = make_custom_repo(tmp_path, {"src/m.py": original})
        sha = Repo(str(repo)).head().decode()
        limits = PatchLimits(max_lines_added=2, max_lines_removed=100)
        manager = WorktreeManager(tmp_path / "ws")
        wt = manager.create(repo, sha, "INC-C3", limits)
        writes = []
        real = Path.write_bytes
        try:
            Path.write_bytes = lambda self, data: (writes.append(self), real(self, data))[1]
            with pytest.raises(PatchError, match="adds 3 lines"):
                apply_patch(wt, intent_for("src/m.py", "a = 1\nb = 2\nc = 3\n", "x = 1\ny = 2\nz = 3\n"),
                            limits, path_limits=InspectionLimits(), repo=Repo(str(repo)), base_sha=sha)
            with pytest.raises(PatchError, match="diff is"):
                apply_patch(wt, intent_for("src/m.py", "a = 1\n", "a = '" + "x" * 300 + "'\n"),
                            PatchLimits(max_diff_bytes=200), path_limits=InspectionLimits(),
                            repo=Repo(str(repo)), base_sha=sha)
        finally:
            Path.write_bytes = real
        assert writes == []
        assert (wt.root / "src" / "m.py").read_bytes() == original
        assert manager.inspect(wt.worktree_id)["applied"] is False

    def test_oversized_patch_via_executor_leaves_no_applied_candidate(self, tmp_path):
        repo = make_custom_repo(tmp_path, {"src/m.py": b"a = 1\nb = 2\nc = 3\n"})
        cfg = PatchConfig(workspace=tmp_path / "ws", limits=PatchLimits(max_lines_added=2))
        proposal = {**GOOD_PATCH, "file": "src/m.py",
                    "patch": {"operation": "modify", "expected_old_content": "a = 1\nb = 2\nc = 3\n",
                              "replacement_content": "x = 1\ny = 2\nz = 3\n"}}
        result, _ = run(repo, proposal, cfg)
        assert result.status == "patch_rejected" and "adds 3 lines" in result.reason
        assert result.candidate_modified is False
        assert result.worktree["cleaned_up"] is True and candidates(cfg) == []

    def test_patch_max_files_setting_is_gone(self, monkeypatch):
        monkeypatch.setenv("PATCH_MAX_FILES", "50")
        assert not hasattr(PatchLimits.from_env(), "max_files")
        assert "max_files" not in PatchLimits().to_dict()

    def test_crlf_file_requires_exact_line_endings(self, tmp_path):
        target = tmp_path / "crlf-repo"
        original = b"x = 1\r\ny = 2\r\n"
        sha = raw_commit(target, {b"src": {b"crlf.py": original}})
        wt = WorktreeManager(tmp_path / "ws").create(target, sha, "INC-CRLF", PatchLimits())
        with pytest.raises(PatchError, match="CRLF"):
            apply_patch(wt, intent_for("src/crlf.py", "x = 1\n", "x = 2\n"), PatchLimits(),
                        path_limits=InspectionLimits(), repo=Repo(str(target)), base_sha=sha)
        assert (wt.root / "src" / "crlf.py").read_bytes() == original     # never normalized
        apply_patch(wt, intent_for("src/crlf.py", "x = 1\r\n", "x = 2\r\n"), PatchLimits(),
                    path_limits=InspectionLimits(), repo=Repo(str(target)), base_sha=sha)
        assert (wt.root / "src" / "crlf.py").read_bytes() == b"x = 2\r\ny = 2\r\n"

    def test_labels_distinguish_original_and_candidate(self, repo, config):
        result, _ = run(repo, GOOD_PATCH, config)
        d = result.to_dict()
        assert d["candidate_modified"] is True and d["original_repo_unchanged"] is True
        requested, _, _ = request(repo, GOOD_PATCH, config)
        assert requested.to_dict()["candidate_modified"] is False


# ── Worktree isolation and lifecycle ─────────────────────────────────────────

class TestWorktree:
    def test_worktree_is_isolated_and_not_a_git_repo(self, repo, config):
        result, _ = run(repo, GOOD_PATCH, config)
        wt = Path(result.worktree["path"])
        assert wt.is_dir() and not (wt / ".git").exists()
        assert not wt.is_relative_to(repo)
        assert result.worktree["base_sha"] == result.base_sha
        assert (wt / "src" / "auth.py").read_text().startswith("def validate_token(token):")

    def test_worktree_starts_from_the_pinned_sha(self, repo, config):
        result, _ = run(repo, GOOD_PATCH, config)
        head = result.base_sha
        wt = Path(result.worktree["path"])
        from repo_inspection.git_read import _open
        repo_obj = _open(repo)
        base_commit = repo_obj.object_store[head.encode("ascii")]
        tree = repo_obj.object_store[base_commit.tree]
        _, src_sha = tree[b"src"]
        src = repo_obj.object_store[src_sha]
        _, auth_sha = src[b"auth.py"]
        base_auth = bytes(repo_obj.object_store[auth_sha].data)
        wt_auth = (wt / "src" / "auth.py").read_bytes()
        assert base_auth != wt_auth
        assert wt_auth == base_auth.replace(
            b"    return user_id  # BUG: user_id is undefined -> NameError -> HTTP 500\n",
            b"    return session\n")
        _, init_sha = src[b"__init__.py"]
        assert (wt / "src" / "__init__.py").read_bytes() == bytes(repo_obj.object_store[init_sha].data)

    def test_unexpected_change_in_worktree_is_rejected(self, repo, config):
        before = tree_digest(repo)
        insp, known, head = inspect_and_pin(repo)
        from repo_inspection.git_read import _open
        from patching.apply import validate_applied_diff
        from patching.proposal import validate_patch_intent
        manager = WorktreeManager(config.workspace)
        wt = manager.create(repo, head, "x", config.limits)
        intent = validate_patch_intent(GOOD_PATCH, inspector=insp, known_repository_ids=known,
                                       limits=config.limits)
        apply_patch(wt, intent, config.limits, path_limits=config.path_limits,
                    repo=_open(repo), base_sha=head)
        (wt.root / "unexpected.txt").write_text("sneaky")     # simulated tampering
        with pytest.raises(PatchError, match="unexpected changes"):
            validate_applied_diff(wt, intent, config.limits, repo=_open(repo), base_sha=head)
        assert tree_digest(repo) == before

    def test_cleanup_only_removes_application_owned_worktrees(self, repo, config):
        manager = WorktreeManager(config.workspace)
        with pytest.raises(PatchError, match="no application-owned"):
            manager.cleanup("wt-not-mine")
        result, _ = run(repo, GOOD_PATCH, config)
        wt_id = result.worktree["worktree_id"]
        wt_path = Path(result.worktree["path"])
        assert manager.inspect(wt_id)["exists"] is True
        manager.cleanup(wt_id)
        assert not wt_path.exists()
        assert manager.inspect(wt_id) is None

    def test_patch_id_and_traceability(self, repo, config):
        a, _ = run(repo, GOOD_PATCH, config)
        b, _ = run(repo, GOOD_PATCH, config)
        assert a.patch_id != b.patch_id
        assert a.worktree["worktree_id"].endswith(a.patch_id[:12])
        assert a.base_sha == b.base_sha


class TestCleanupHardening:
    """Metadata is treated as hostile: cleanup only ever removes
    <workspace>/<incident>/candidate-<id> strictly below the workspace."""

    WT = "wt-0123456789ab"

    def tamper(self, manager, path, target_root="unused"):
        manager._register(self.WT, {"worktree_id": self.WT, "path": str(path),
                                    "target_root": str(target_root), "base_sha": "x",
                                    "incident_id": "x", "patch_id": "0123456789ab"})

    def populated(self, config):
        ws = Path(config.workspace)
        (ws / "INC" / "candidate-0123456789ab").mkdir(parents=True)
        (ws / "keep.txt").write_text("keep")
        return WorktreeManager(ws), ws

    def test_cleanup_cannot_delete_the_workspace_root(self, config):
        manager, ws = self.populated(config)
        for variant in (ws, Path(str(ws) + os.sep), ws / "INC" / ".."):
            self.tamper(manager, variant)
            with pytest.raises(PatchError) as e:
                manager.cleanup(self.WT)
            assert e.value.code == "cleanup_failed"
        if sys.platform == "win32":
            self.tamper(manager, str(ws).upper())
            with pytest.raises(PatchError):
                manager.cleanup(self.WT)
        assert (ws / "keep.txt").exists() and (ws / METADATA_NAME).exists()

    def test_cleanup_refuses_incident_dir_other_names_and_outside_paths(self, config, tmp_path):
        manager, ws = self.populated(config)
        outside = tmp_path / "victim-dir"
        outside.mkdir()
        (ws / "INC" / "candidate-ffffffffffff").mkdir()
        for bad in (ws / "INC", ws / "INC" / "candidate-ffffffffffff", outside,
                    Path("relative") / "candidate-0123456789ab"):
            self.tamper(manager, bad)
            with pytest.raises(PatchError, match="cleanup_failed|escapes|not an application|not absolute|cannot be resolved"):
                manager.cleanup(self.WT)
        assert outside.exists() and (ws / "INC").exists()
        assert (ws / "INC" / "candidate-ffffffffffff").exists()

    def test_cleanup_refuses_the_target_repository_case_insensitively(self, config):
        manager, ws = self.populated(config)
        cand = ws / "INC" / "candidate-0123456789ab"
        variant = str(cand).upper() if sys.platform == "win32" else str(cand)
        self.tamper(manager, cand, target_root=variant)
        with pytest.raises(PatchError, match="target repository"):
            manager.cleanup(self.WT)
        assert cand.exists()

    def test_cleanup_refuses_a_link_alias(self, config, tmp_path):
        manager, ws = self.populated(config)
        outside = tmp_path / "victim"
        outside.mkdir()
        (outside / "precious.txt").write_text("x")
        alias = ws / "INC2" / "candidate-0123456789ab"
        alias.parent.mkdir()
        try:
            make_outside_link(alias, outside)
        except OSError:
            pytest.skip("cannot create a directory link here")
        self.tamper(manager, alias)
        with pytest.raises(PatchError):
            manager.cleanup(self.WT)
        assert (outside / "precious.txt").exists()

    def test_malformed_worktree_id_is_refused(self, config):
        manager, ws = self.populated(config)
        manager._register("wt-evil", {"worktree_id": "wt-evil",
                                      "path": str(ws / "INC" / "candidate-0123456789ab")})
        with pytest.raises(PatchError, match="malformed"):
            manager.cleanup("wt-evil")
        assert (ws / "INC" / "candidate-0123456789ab").exists()


# ── Self-target protection ───────────────────────────────────────────────────

class TestSelfTarget:
    def test_application_repo_is_refused_as_patch_target(self, config):
        own = application_repo_root()
        if own is None:
            pytest.skip("application is not a git checkout")
        insp = RepositoryInspector(str(own))
        result, payload = prepare_patch_request(proposal=GOOD_PATCH, inspector=insp, incident_id="x",
                                                known_repository_ids=set(), config=config,
                                                pinned_base_sha="")
        assert result.status == "self_target_denied" and payload is None
        assert candidates(config) == []

    def test_self_target_override_is_explicit(self, config):
        own = application_repo_root()
        if own is None:
            pytest.skip("application is not a git checkout")
        config = PatchConfig(workspace=config.workspace, allow_self_target=True)
        insp = RepositoryInspector(str(own))
        result, payload = prepare_patch_request(proposal=GOOD_PATCH, inspector=insp, incident_id="x",
                                                known_repository_ids=set(), config=config,
                                                pinned_base_sha="")
        assert result.status != "self_target_denied"
        assert payload is None and candidates(config) == []


# ── Trust boundary + memory ──────────────────────────────────────────────────

class TestTrustAndMemory:
    def test_malicious_replacement_is_only_text(self, tmp_path, config):
        repo = make_custom_repo(tmp_path, {"src/evil.py": b"x = 1\n"})
        malicious = ("import os\nos.system('format C:')\n"
                     "import subprocess\nsubprocess.run(['rm', '-rf', '/'])\n")
        proposal = {**GOOD_PATCH, "file": "src/evil.py",
                    "patch": {"operation": "modify", "expected_old_content": "x = 1\n",
                              "replacement_content": malicious}}
        before = tree_digest(repo)
        result, _ = run(repo, proposal, config)
        assert result.status == "verification_passed", result.reason
        wt = Path(result.worktree["path"])
        assert "subprocess.run(['rm', '-rf', '/'])" in (wt / "src" / "evil.py").read_text()
        assert tree_digest(repo) == before

    def test_hindsight_outage_does_not_prevent_patch_preparation(self, world):
        memory = FakeMemory()
        memory.available = False
        result = world.start(recorder=MemoryRecorder(memory))
        assert result["historical_memory"]["status"] == "unavailable"
        assert result["patch_candidate"]["status"] == "approval_required"
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        world.approvals.approve(approval_id, actor="alice")
        out = world.resume(thread_id, approval_id, recorder=MemoryRecorder(memory))
        assert out["patch_candidate"]["status"] == "verification_passed"

    def test_patch_details_are_never_retained_in_memory(self, world):
        healthy = FakeMemory()
        result = world.start(incident="INC-PATCH-MEM2", recorder=MemoryRecorder(healthy))
        approval_id, thread_id = result["patch_approval"]["approval_id"], result["workflow"]["thread_id"]
        world.approvals.approve(approval_id, actor="alice")
        world.resume(thread_id, approval_id, recorder=MemoryRecorder(healthy))
        blob = " ".join(r.content + " ".join(r.metadata.values()) for r in healthy.records())
        for leaked in ("return session", "candidate-", "worktree", "replacement_content", "repository_apply"):
            assert leaked not in blob, leaked


# ── Static security scan for the patching package ────────────────────────────

STAGE6_SOURCES = sorted(
    glob.glob(os.path.join(os.path.dirname(__file__), "..", "patching", "*.py"))
    + [os.path.join(os.path.dirname(__file__), "..", "agent", "patch_flow.py")])


class TestStaticSecurity:
    @pytest.mark.parametrize("path", STAGE6_SOURCES)
    def test_no_shell_command_network_or_unrestricted_write_capabilities(self, path):
        tree = ast.parse(open(path, encoding="utf-8").read())
        name = os.path.basename(path)
        forbidden_mods = {"subprocess", "socket", "paramiko", "docker", "kubernetes", "pexpect",
                          "pty", "multiprocessing", "concurrent", "asyncio", "urllib", "http",
                          "requests", "httpx", "aiohttp", "ftplib", "smtplib", "telnetlib",
                          "runpy", "importlib", "ctypes", "pytest", "unittest", "nose", "tox",
                          "webbrowser"}
        os_mutators = {"system", "popen", "execv", "execve", "execl", "execvp", "spawnv", "spawnl",
                       "posix_spawn", "startfile", "fork", "kill", "remove", "unlink", "rmdir",
                       "removedirs", "rename", "renames", "replace", "chmod", "chown", "truncate",
                       "symlink", "link", "mkdir", "makedirs"}
        any_mutators = {"eval", "exec", "__import__", "run", "check_output", "check_call",
                        "call", "Popen", "getoutput", "getstatusoutput", "system"}
        builtin_exec = {"eval", "exec", "compile", "__import__", "breakpoint"}
        path_writers = {"write_text", "write_bytes", "open"}
        allowed_writers = name in ("worktree.py", "apply.py")   # the ONLY write sites
        allowed_shutil = name == "worktree.py"                  # rmtree only with ownership checks
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = {a.name.split(".")[0] for a in node.names}
                assert not mods & forbidden_mods, (path, mods)
                assert not ("shutil" in mods and not allowed_shutil), path
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").split(".")[0]
                assert mod not in forbidden_mods, (path, mod)
                assert not (mod == "shutil" and not allowed_shutil), path
            elif isinstance(node, ast.Call):
                func = node.func
                attr = getattr(func, "attr", None)
                name_ = getattr(func, "id", None)
                receiver = getattr(getattr(func, "value", None), "id", None)
                if receiver == "os":
                    assert attr not in os_mutators, (path, f"os.{attr}")
                assert attr not in any_mutators and name_ not in any_mutators, (path, attr or name_)
                assert name_ not in builtin_exec, (path, name_)
                if attr in path_writers and not allowed_writers:
                    assert False, (path, attr)
                if name_ == "open" and not allowed_writers:
                    assert False, (path, name_)

    def test_no_generic_command_api_on_worktree_manager(self):
        names = {n.lower() for n in dir(WorktreeManager)}
        for verb in ("run", "exec", "command", "shell", "git_", "checkout", "commit", "push"):
            assert not any(verb in n for n in names), verb
        assert {"create", "cleanup", "inspect", "list_worktrees"} <= names
