"""
InfraLens Agent — agent/graph.py
LangGraph agent that produces structured post-mortem JSON.
Edge cases: missing API key, empty timeline, tool failures, JSON parse errors.
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
    timeline_text: str
    postmortem: dict


tools = [search_timeline, search_past_incidents]
tool_node = ToolNode(tools)


def get_llm():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(
            "OPENAI_API_KEY not set. Add it to your .env file or set it as an environment variable."
        )
    return ChatOpenAI(model="gpt-4o", temperature=0, api_key=api_key).bind_tools(tools)


# Fix #4: build LLM and graph once at module load, not on every request.
# Falls back to None if API key is missing at import time (e.g. during tests);
# run_agent will raise a clear EnvironmentError in that case.
try:
    _llm = get_llm()
    _graph = None  # built lazily on first run_agent call so imports don't fail
except EnvironmentError:
    _llm = None
    _graph = None


def analyst_node(state: AgentState):
    # Use the module-level singleton; avoids re-instantiating the LLM client
    # and re-binding tools on every node invocation.
    llm = _llm if _llm is not None else get_llm()
    messages = [SystemMessage(content=SYSTEM_PROMPT)] + state["messages"]
    response = llm.invoke(messages)
    return {"messages": [response]}


def should_continue(state: AgentState):
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        return "tools"
    return "end"


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("analyst", analyst_node)
    graph.add_node("tools", tool_node)
    graph.set_entry_point("analyst")
    graph.add_conditional_edges("analyst", should_continue, {
        "tools": "tools",
        "end": END,
    })
    graph.add_edge("tools", "analyst")
    return graph.compile()


def _get_graph():
    """Return the module-level compiled graph, building it once if needed."""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


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


def run_agent(incident_id: str, timeline: list) -> dict:
    """
    Run the LangGraph agent and return a structured post-mortem dict.

    Edge cases handled:
    - Empty timeline
    - API key missing
    - Agent produces non-JSON output
    - Agent throws an exception
    """
    if not timeline:
        return {
            "incident_id": incident_id,
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
    timeline_text = "\n".join(
        f"[{e['timestamp_str']}] [{e['source'].upper()}] [{e.get('level','INFO')}] {e['content']}"
        for e in timeline
    )
    MAX_CHARS = 6000  # ~1500 tokens — safe for gpt-4o with tools
    if len(timeline_text) <= MAX_CHARS:
        truncated = timeline_text
    else:
        # Fix #5: count how many complete lines fit, then report the remainder.
        # The old code counted newlines in the sliced string — that gives the
        # number of *included* lines, not the remaining ones that were dropped.
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
        graph = _get_graph()  # Fix #4: reuse singleton
        result = graph.invoke({
            "messages": [HumanMessage(content=prompt)],
            "incident_id": incident_id,
            "timeline_text": timeline_text,
            "postmortem": {},
        })

        raw_content = result["messages"][-1].content

        # Parse the structured JSON from the agent's response
        postmortem = _extract_json_from_text(raw_content)

        # Ensure required keys exist
        postmortem.setdefault("incident_id", incident_id)
        postmortem.setdefault("summary", "")
        postmortem.setdefault("root_cause", "")
        postmortem.setdefault("contributing_factors", [])
        postmortem.setdefault("evidence_citations", [])
        postmortem.setdefault("action_items", [])
        postmortem.setdefault("similar_past_incidents", [])
        postmortem.setdefault("confidence", "medium")
        postmortem.setdefault("timeline_reconstruction", "")

        return postmortem

    except EnvironmentError as e:
        # Missing API key — surface clearly
        raise
    except Exception as e:
        tb = traceback.format_exc()
        return {
            "incident_id": incident_id,
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