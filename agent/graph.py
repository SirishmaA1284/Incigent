"""
agent/graph.py
LangGraph agent that produces structured post-mortem JSON.
Edge cases: missing API key, empty timeline, tool failures, JSON parse errors.

There is no module-level graph or LLM cache. Callers that want to reuse a
graph (the API) build one with build_graph() and pass it to run_agent();
tests inject their own graph or LLM the same way.

Flow: recall_memory -> record_intake -> inspect_repository (read-only) -> analyst <-> tools
      -> prepare_patch (read-only) [-> request_patch_approval -> await_patch_approval (interrupt)
         -> validate_patch_approval -> apply_patch (consume -> candidate worktree)]
      -> propose_action -> request_approval -> await_approval (interrupt) -> validate_approval
      -> execute_remediation (validate -> consume -> apply, simulator only)
      -> verify_remediation -> record_outcome.
Execution happens only when a RemediationExecutor is passed on resume, and only
after ApprovalService.consume() succeeds for the exact approved payload.
Current-incident evidence comes from the timeline and Chroma (search_timeline).
Historical operational memory comes from the MemoryRecorder, passed per run in
config["configurable"]["memory_recorder"]; it is advisory context only.
Human approval runs only when an ApprovalService is passed and the graph has a
checkpointer; approvals are enforced by the application database, not memory.
"""

import os
import json
import operator
import re
import traceback
import uuid
from datetime import datetime
from typing import Any, Optional, TypedDict, Annotated

from dotenv import load_dotenv, find_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from agent.tools import search_timeline
from agent.prompts import SYSTEM_PROMPT, ROOT_CAUSE_PROMPT
from agent.memory_context import build_incident_profile, build_recall_query, format_memory_context
from agent.approval_flow import (
    await_approval_node, propose_action_node, request_approval_node, route_after_proposal,
    route_after_validation, validate_approval_node,
)
from agent.remediation_flow import (
    execute_remediation_node, record_outcome_node, route_after_execution, verify_remediation_node,
)
from agent.repository_flow import apply_repository_analysis, inspect_repository_node
from agent.patch_flow import (
    apply_patch_node, await_patch_approval_node, prepare_patch_node, request_patch_approval_node,
    route_after_patch_preparation, route_after_patch_validation, validate_patch_approval_node,
)

load_dotenv(find_dotenv())


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    incident_id: str
    event_count: int
    incident_profile: dict          # services / patterns / signatures from the timeline
    current_evidence: str           # the current incident's timeline text
    recalled_memories: list         # normalized historical memories (dicts), never raw client objects
    memory_status: dict             # recall outcome: status, backend, query, error
    memory_retention: Annotated[list, operator.add]
    proposed_action: Optional[dict] # canonical action payload awaiting human approval
    approval_request: dict          # approval_id, kind, action_hash, expires_at (from the approval DB)
    approval_resume: Any            # raw resume value; only ever used as a reference, never trusted
    approval_result: dict           # outcome of application-side validation
    remediation: dict               # ExecutionResult: consume-gated simulator execution
    verification: dict              # VerificationResult: independent check of the postcondition
    outcome_memory: dict            # what (if anything) was retained as validated memory
    proposal_rejection: dict        # set when the model's remediation was invalid (no approval created)
    repository_inspection: dict     # read-only evidence from the configured target repository ([R###])
    patch_candidate: dict           # Stage 6: patch request / approved candidate in an isolated worktree
    proposed_patch: Optional[dict]  # Stage 6: canonical repository_apply payload awaiting approval
    patch_approval_request: dict    # the workflow's own repository_apply approval (from the approval DB)
    patch_approval_resume: Any      # raw resume value; only ever used as a reference, never trusted
    patch_approval_result: dict     # outcome of application-side validation of that approval


tools = [search_timeline]

NOT_CONSULTED = {"status": "not_consulted", "backend": "none", "query": "", "memories": [], "error": None}


class MissingAPIKeyError(EnvironmentError):
    """OPENAI_API_KEY is not configured. Other OSErrors are not this."""


def get_llm():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise MissingAPIKeyError(
            "OPENAI_API_KEY not set. Add it to your .env file or set it as an environment variable."
        )
    return ChatOpenAI(model="gpt-4o", temperature=0, api_key=api_key).bind_tools(tools)


def should_continue(state: AgentState):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return "end"


def _recorder(config: RunnableConfig):
    return ((config or {}).get("configurable") or {}).get("memory_recorder")


def recall_memory_node(state: AgentState, config: RunnableConfig):
    """Recall prior operational experience before any root-cause reasoning.
    The query and incident scope come from application state, not the model."""
    recorder = _recorder(config)
    query = build_recall_query(state["incident_profile"])
    if recorder is None:
        outcome = {**NOT_CONSULTED, "status": "disabled", "query": query,
                   "error": "long-term memory is not configured"}
    else:
        outcome = recorder.recall_for_incident(state["incident_id"], query).to_dict()
    return {
        "recalled_memories": outcome["memories"],
        "memory_status": {k: v for k, v in outcome.items() if k != "memories"},
        "messages": [HumanMessage(content=format_memory_context(outcome))],
    }


def record_intake_node(state: AgentState, config: RunnableConfig):
    """Retain the 'incident received' and 'evidence collected' milestones."""
    recorder = _recorder(config)
    if recorder is None:
        return {"memory_retention": []}
    p = state["incident_profile"]
    common = {"services": p["services"], "pattern_tags": p["pattern_tags"]}
    first_seen = datetime.fromisoformat(p["first_seen"]) if p.get("first_seen") else None
    last_seen = datetime.fromisoformat(p["last_seen"]) if p.get("last_seen") else None
    outcomes = [
        recorder.record_incident(
            state["incident_id"],
            f"Incident received with {state['event_count']} timeline events"
            + (f"; failure patterns {', '.join(p['pattern_tags'])}" if p["pattern_tags"] else "") + ".",
            occurred_at=first_seen, details={"sources": p.get("sources", "")}, **common),
        recorder.record_evidence_digest(
            state["incident_id"],
            "Key evidence: " + (" | ".join(p.get("key_evidence") or []) or "no ERROR/CRITICAL events"),
            occurred_at=last_seen,
            details={"error_signatures": "; ".join(p["error_signatures"])}, **common),
    ]
    return {"memory_retention": [o.to_dict() for o in outcomes]}


def build_graph(llm=None, checkpointer=None):
    """
    Compile a new agent graph.

    llm: a tool-bound chat model (anything with .invoke(messages)). Defaults to
    get_llm(), which raises MissingAPIKeyError when OPENAI_API_KEY is missing.
    checkpointer: a LangGraph checkpointer (e.g. SqliteSaver). Required for the
    approval interrupt to pause and later resume.
    """
    if llm is None:
        llm = get_llm()

    def analyst_node(state: AgentState):
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm.invoke(messages)
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("recall_memory", recall_memory_node)
    graph.add_node("record_intake", record_intake_node)
    graph.add_node("inspect_repository", inspect_repository_node)
    graph.add_node("prepare_patch", prepare_patch_node)
    graph.add_node("analyst", analyst_node)
    graph.add_node("tools", ToolNode(tools))
    graph.set_entry_point("recall_memory")
    graph.add_edge("recall_memory", "record_intake")
    graph.add_edge("record_intake", "inspect_repository")
    graph.add_edge("inspect_repository", "analyst")
    graph.add_node("propose_action", propose_action_node)
    graph.add_node("request_approval", request_approval_node)
    graph.add_node("await_approval", await_approval_node)
    graph.add_node("validate_approval", validate_approval_node)
    graph.add_conditional_edges("analyst", should_continue, {
        "tools": "tools",
        "end": "prepare_patch",
    })
    graph.add_edge("tools", "analyst")
    # Stage 6: a patch is applied only after its own repository_apply approval.
    graph.add_node("request_patch_approval", request_patch_approval_node)
    graph.add_node("await_patch_approval", await_patch_approval_node)
    graph.add_node("validate_patch_approval", validate_patch_approval_node)
    graph.add_node("apply_patch", apply_patch_node)
    graph.add_conditional_edges("prepare_patch", route_after_patch_preparation,
                                {"request_patch_approval": "request_patch_approval",
                                 "propose_action": "propose_action"})
    graph.add_edge("request_patch_approval", "await_patch_approval")
    graph.add_edge("await_patch_approval", "validate_patch_approval")
    graph.add_conditional_edges("validate_patch_approval", route_after_patch_validation,
                                {"await_patch_approval": "await_patch_approval",
                                 "apply_patch": "apply_patch", "propose_action": "propose_action"})
    graph.add_edge("apply_patch", "propose_action")
    graph.add_conditional_edges("propose_action", route_after_proposal,
                                {"request_approval": "request_approval", "end": END})
    graph.add_edge("request_approval", "await_approval")
    graph.add_edge("await_approval", "validate_approval")
    graph.add_node("execute_remediation", execute_remediation_node)
    graph.add_node("verify_remediation", verify_remediation_node)
    graph.add_node("record_outcome", record_outcome_node)
    graph.add_conditional_edges("validate_approval", route_after_validation,
                                {"await_approval": "await_approval",
                                 "execute_remediation": "execute_remediation", "end": END})
    graph.add_conditional_edges("execute_remediation", route_after_execution,
                                {"verify_remediation": "verify_remediation", "record_outcome": "record_outcome"})
    graph.add_edge("verify_remediation", "record_outcome")
    graph.add_edge("record_outcome", END)
    return graph.compile(checkpointer=checkpointer)


def _extract_json_from_text(text: str) -> dict:
    """
    Try to extract a JSON object from the LLM's response.
    Handles: bare JSON, ```json fenced, partial JSON with trailing text.
    """
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting from ```json ... ``` fence
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if fence:
        try:
            return json.loads(fence.group(1))
        except json.JSONDecodeError:
            pass

    # Try finding first { ... } block
    brace = re.search(r'\{.*\}', text, re.DOTALL)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass

    # Give up — return the raw text as summary so the UI still gets something useful
    return {
        "_parse_failed": True,
        "summary": text[:500] if len(text) > 500 else text,
        "root_cause": "Could not parse structured output from agent. See summary for raw response.",
        "contributing_factors": [],
        "evidence_citations": [],
        "action_items": [],
        "similar_past_incidents": [],
        "confidence": "low",
        "timeline_reconstruction": "",
    }


def format_timeline(timeline: list) -> str:
    """One line per event, prefixed with its event ID so evidence can be cited."""
    return "\n".join(
        f"[E{e.get('event_id', '?')}] [{e['timestamp_str']}] [{e['source'].upper()}] "
        f"[{e.get('level', 'INFO')}] {e['content']}"
        for e in timeline
    )


def _only_recalled_incidents(similar: list, memories: list) -> list:
    """Keep only 'similar incident' entries that name an incident actually recalled."""
    recalled_ids = {m["incident_id"] for m in memories if m.get("incident_id")}
    return [s for s in similar if isinstance(s, str) and any(i in s for i in recalled_ids)]


def _workflow(result: dict, thread_id) -> dict:
    """Where the workflow stands after an invoke. Approved != executed != verified."""
    remediation = result.get("remediation") or {}
    if result.get("__interrupt__"):
        status = "awaiting_approval"
    elif result.get("proposal_rejection"):
        status = "proposal_rejected"                               # invalid action: no approval created
    elif remediation:
        if remediation.get("status") != "executed":
            status = f"remediation_{remediation.get('status')}"   # not_executable / not_authorized / execution_failed
        elif (result.get("verification") or {}).get("verified"):
            status = "remediation_verified"
        else:
            status = "remediation_unverified"                      # executed, but recovery NOT verified
    else:
        status = {"validated": "approval_validated", "denied": "approval_denied"}.get(
            (result.get("approval_result") or {}).get("status"), "completed")
    return {"status": status, "thread_id": thread_id}


def run_agent(incident_id: str, timeline: list, graph=None, recorder=None,
              approvals=None, thread_id: Optional[str] = None, inspector=None,
              patch_config=None) -> dict:
    """
    Run the LangGraph agent and return a structured post-mortem dict.

    patch_config: a patching.PatchConfig (explicit PATCH_WORKSPACE etc.) or
    None. When given (and an inspector is given), a valid patch proposal is
    validated READ-ONLY and becomes a repository_apply approval request; the
    graph pauses (patch_candidate "approval_required"). Nothing is written
    until a human approves and resume_agent() is called with the same
    inspector and patch_config; the approval is then consumed and the patch is
    applied in an isolated application-created worktree at the pinned base
    commit, diff-checked and structurally verified. Without approvals (or a
    checkpointer) the result is "approval_unavailable" and nothing is applied.
    The original repository is NEVER modified; nothing is committed, pushed,
    merged or submitted as a PR. When None, patch_candidate reports "skipped".

    inspector: a repo_inspection.RepositoryInspector for ONE explicitly configured
    target repository, or None. Inspection is read-only; its evidence ([R###]) is
    shown to the model separately from the timeline and historical memory, and a
    resulting patch proposal is a validated description only — never applied.

    approvals: an approvals.ApprovalService. When given and the graph has a
    checkpointer, the top recommendation becomes a pending approval request and
    the graph pauses (result["workflow"]["status"] == "awaiting_approval").
    Resume with resume_agent(). Nothing is ever executed by this stage.

    graph: a compiled graph from build_graph(). When None, a new graph is
    built for this call (raises MissingAPIKeyError if OPENAI_API_KEY is missing).
    Any other failure, including OSErrors from tools or Chroma, is returned as
    an "agent_error" result.

    recorder: a memory.MemoryRecorder for long-term operational memory, or None
    (memory disabled). Memory failures never fail the analysis; the result's
    "historical_memory" reports whether memory was actually consulted.

    The result's "analysis_mode" is set by this function, never by the LLM:
    "llm_agent", "agent_error" or "no_evidence".

    Edge cases handled:
    - Empty timeline
    - API key missing
    - Agent produces non-JSON output
    - Agent throws an exception
    """
    if not timeline:
        return {
            "incident_id": incident_id,
            "analysis_mode": "no_evidence",
            "summary": "No timeline events provided.",
            "root_cause": "Cannot determine — empty timeline.",
            "contributing_factors": [],
            "evidence_citations": [],
            "action_items": [
                {"what": "Upload server logs, Slack export, or tickets", "owner": "SRE", "priority": "P1"}
            ],
            "similar_past_incidents": [],
            "confidence": "low",
            "timeline_reconstruction": "",
            "historical_memory": dict(NOT_CONSULTED),
        }

    # Build the timeline text (truncated to avoid context window overflow)
    timeline_text = format_timeline(timeline)
    MAX_CHARS = 6000  # ~1500 tokens — safe for gpt-4o with tools
    if len(timeline_text) <= MAX_CHARS:
        truncated = timeline_text
    else:
        # Count how many complete lines fit, then report the remainder.
        lines = timeline_text.splitlines()
        chars, included = 0, 0
        for line in lines:
            chars += len(line) + 1
            if chars > MAX_CHARS:
                break
            included += 1
        truncated_count = len(timeline) - included
        truncated = "\n".join(lines[:included])
        truncated += f"\n... [{truncated_count} more events truncated]"

    prompt = ROOT_CAUSE_PROMPT.format(
        timeline_text=truncated,
        incident_id=incident_id,
        event_count=len(timeline),
    )

    profile = build_incident_profile(timeline)

    try:
        if graph is None:
            graph = build_graph()
        configurable = {"memory_recorder": recorder, "approval_service": approvals,
                        "repository_inspector": inspector, "patch_config": patch_config}
        if getattr(graph, "checkpointer", None) is not None:
            thread_id = thread_id or f"{incident_id}:{uuid.uuid4().hex[:12]}"
            configurable["thread_id"] = thread_id
        else:
            thread_id = None
        result = graph.invoke(
            {
                "messages": [HumanMessage(content=prompt)],
                "incident_id": incident_id,
                "event_count": len(timeline),
                "incident_profile": profile,
                "current_evidence": timeline_text,
                "recalled_memories": [],
                "memory_status": {},
                "memory_retention": [],
                "proposed_action": None,
                "approval_request": {},
                "approval_result": {},
                "proposed_patch": None,
                "patch_approval_request": {},
                "patch_approval_result": {},
            },
            config={"configurable": configurable},
        )

        raw_content = result["messages"][-1].content

        # Parse the structured JSON from the agent's response
        postmortem = _extract_json_from_text(raw_content)

        # The incident ID and analysis mode come from the caller, not the LLM.
        postmortem["incident_id"] = incident_id
        postmortem["analysis_mode"] = "llm_agent"

        # Ensure required keys exist
        postmortem.setdefault("summary", "")
        postmortem.setdefault("root_cause", "")
        postmortem.setdefault("contributing_factors", [])
        postmortem.setdefault("evidence_citations", [])
        postmortem.setdefault("action_items", [])
        postmortem.setdefault("similar_past_incidents", [])
        postmortem.setdefault("confidence", "medium")
        postmortem.setdefault("timeline_reconstruction", "")
        postmortem.setdefault("memory_influence", "")

        # Historical memory status comes from the recall step, never from the LLM.
        status = result.get("memory_status") or {}
        memories = result.get("recalled_memories") or []
        postmortem["historical_memory"] = ({**status, "memories": memories} if status
                                           else dict(NOT_CONSULTED))
        if postmortem["historical_memory"]["status"] != "available":
            postmortem["similar_past_incidents"] = []
            postmortem["memory_influence"] = ""
        else:
            postmortem["similar_past_incidents"] = _only_recalled_incidents(
                postmortem["similar_past_incidents"], memories)

        # Only the intake milestones (incident, evidence digest) are retained.
        # The LLM's diagnosis and recommendations are NOT sent to long-term
        # memory: they are unverified until a human decision validates them,
        # and the system must not learn from its own guesses.
        postmortem["memory_retention"] = list(result.get("memory_retention") or [])

        # Approval state comes from the application approval store via the graph.
        postmortem["workflow"] = _workflow(result, thread_id)
        postmortem["approval"] = dict(result.get("approval_request") or {}) or None
        postmortem["proposal_rejection"] = result.get("proposal_rejection")

        # Repository evidence (read-only) and a display-only patch proposal.
        inspection = result.get("repository_inspection") or {"status": "not_configured", "findings": []}
        postmortem["repository_inspection"] = inspection
        apply_repository_analysis(postmortem, inspection, inspector)
        # Stage 6: the validated candidate patch (isolated worktree) or why not.
        postmortem["patch_candidate"] = result.get("patch_candidate") or {
            "status": "skipped", "reason": "repository patching is not configured"}
        postmortem["patch_approval"] = dict(result.get("patch_approval_request") or {}) or None
        return postmortem

    except MissingAPIKeyError:
        # Missing API key — surface clearly
        raise
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "incident_id": incident_id,
            "analysis_mode": "agent_error",
            "summary": f"Agent error: {str(e)}",
            "root_cause": "Agent failed — see summary for error details.",
            "contributing_factors": [f"Exception: {type(e).__name__}"],
            "evidence_citations": [],
            "action_items": [
                {"what": "Check OPENAI_API_KEY and ChromaDB setup", "owner": "SRE", "priority": "P1"}
            ],
            "similar_past_incidents": [],
            "confidence": "low",
            "timeline_reconstruction": "",
            # The run failed, so whether memory was consulted is not known.
            "historical_memory": {**NOT_CONSULTED, "status": "unknown",
                                  "error": "analysis failed before completing"},
            "_error_traceback": tb,
        }


class WorkflowNotAwaitingApproval(Exception):
    """The thread does not exist or is not paused at an approval checkpoint."""


AWAIT_NODES = ("await_approval", "await_patch_approval")


def _awaiting(snapshot) -> bool:
    return bool(snapshot) and any(n in (snapshot.next or ()) for n in AWAIT_NODES)


def resume_agent(thread_id: str, approval_id: str, *, graph, approvals,
                 executor=None, verifier=None, recorder=None,
                 inspector=None, patch_config=None) -> dict:
    """
    Resume a paused workflow after a human decision. The resume value is only a
    reference ({"approval_id": ...}); the graph re-checks the approval in the
    application database. This function never accepts or forwards a decision.

    With an executor, a validated approval proceeds to execute_remediation, where
    the action runs only after consume() succeeds; the verifier then checks the
    outcome independently. recorder receives validated outcomes only.

    For a workflow paused at its repository_apply approval, pass the same
    inspector and patch_config: the patch is applied into a new candidate
    worktree only after that approval is consumed.
    """
    config = {"configurable": {"thread_id": thread_id, "approval_service": approvals,
                               "memory_recorder": recorder, "remediation_executor": executor,
                               "remediation_verifier": verifier,
                               "repository_inspector": inspector, "patch_config": patch_config}}
    snapshot = graph.get_state(config)
    if not _awaiting(snapshot):
        raise WorkflowNotAwaitingApproval(f"thread {thread_id!r} is not awaiting approval")
    result = graph.invoke(Command(resume={"approval_id": str(approval_id)}), config=config)
    request = result.get("approval_request") or {}
    record = approvals.get(request.get("approval_id", "")) if request else None
    patch_request = result.get("patch_approval_request") or {}
    patch_record = approvals.get(patch_request.get("approval_id", "")) if patch_request else None
    return {
        "thread_id": thread_id,
        "incident_id": result.get("incident_id"),
        "workflow": _workflow(result, thread_id),
        "approval_result": result.get("approval_result") or {},
        "approval": record.to_dict() if record else None,
        "remediation": result.get("remediation"),
        "verification": result.get("verification"),
        "outcome_memory": result.get("outcome_memory"),
        "patch_approval_result": result.get("patch_approval_result") or {},
        "patch_approval": patch_record.to_dict() if patch_record else None,
        "patch_candidate": result.get("patch_candidate"),
    }


def workflow_state(thread_id: str, *, graph) -> Optional[dict]:
    """Read-only view of a workflow's persisted state (approval, execution, verification)."""
    snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
    values = snapshot.values if snapshot else None
    if not values:
        return None
    result = dict(values)
    if _awaiting(snapshot):
        result["__interrupt__"] = True
    return {
        "thread_id": thread_id,
        "incident_id": values.get("incident_id"),
        "workflow": _workflow(result, thread_id),
        "proposed_action": values.get("proposed_action"),
        "approval_request": values.get("approval_request"),
        "approval_result": values.get("approval_result"),
        "remediation": values.get("remediation"),
        "verification": values.get("verification"),
        "outcome_memory": values.get("outcome_memory"),
        "proposal_rejection": values.get("proposal_rejection"),
        "repository_inspection": values.get("repository_inspection"),
        "patch_candidate": values.get("patch_candidate"),
        "patch_approval_request": values.get("patch_approval_request"),
        "patch_approval_result": values.get("patch_approval_result"),
    }
