"""
agent/graph.py
LangGraph agent that produces structured post-mortem JSON.
Edge cases: missing API key, empty timeline, tool failures, JSON parse errors.

There is no module-level graph or LLM cache. Callers that want to reuse a
graph (the API) build one with build_graph() and pass it to run_agent();
tests inject their own graph or LLM the same way.
"""

import os
import json
import operator
import re
import traceback
from typing import TypedDict, Annotated

from dotenv import load_dotenv, find_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from agent.tools import search_timeline, search_past_incidents
from agent.prompts import SYSTEM_PROMPT, ROOT_CAUSE_PROMPT

load_dotenv(find_dotenv())


class AgentState(TypedDict):
    messages: Annotated[list, operator.add]
    incident_id: str
    event_count: int
    timeline_text: str
    postmortem: dict


tools = [search_timeline, search_past_incidents]


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


def build_graph(llm=None):
    """
    Compile a new agent graph.

    llm: a tool-bound chat model (anything with .invoke(messages)). Defaults to
    get_llm(), which raises MissingAPIKeyError when OPENAI_API_KEY is missing.
    """
    if llm is None:
        llm = get_llm()

    def analyst_node(state: AgentState):
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
        response = llm.invoke(messages)
        return {"messages": [response]}

    graph = StateGraph(AgentState)
    graph.add_node("analyst", analyst_node)
    graph.add_node("tools", ToolNode(tools))
    graph.set_entry_point("analyst")
    graph.add_conditional_edges("analyst", should_continue, {
        "tools": "tools",
        "end": END,
    })
    graph.add_edge("tools", "analyst")
    return graph.compile()


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


def run_agent(incident_id: str, timeline: list, graph=None) -> dict:
    """
    Run the LangGraph agent and return a structured post-mortem dict.

    graph: a compiled graph from build_graph(). When None, a new graph is
    built for this call (raises MissingAPIKeyError if OPENAI_API_KEY is missing).
    Any other failure, including OSErrors from tools or Chroma, is returned as
    an "agent_error" result.

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

    try:
        if graph is None:
            graph = build_graph()
        result = graph.invoke({
            "messages": [HumanMessage(content=prompt)],
            "incident_id": incident_id,
            "event_count": len(timeline),
            "timeline_text": timeline_text,
            "postmortem": {},
        })

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
            "_error_traceback": tb,
        }
