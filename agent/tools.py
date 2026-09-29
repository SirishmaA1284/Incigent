from typing import Annotated

from langchain.tools import tool
from langgraph.prebuilt import InjectedState

from rag.retriever import retrieve_evidence


@tool
def search_timeline(
    query: str,
    incident_id: Annotated[str, InjectedState("incident_id")],
) -> str:
    """Search the current incident's timeline for events relevant to a query. Use before making any claim."""
    # incident_id is injected from graph state by ToolNode and is hidden from
    # the model's tool schema, so the model can never guess or override it.
    results = retrieve_evidence(query, incident_id)
    if not results:
        return "No relevant evidence found."
    return "\n\n---\n".join(r["text"] for r in results)


# Past incidents are no longer a model-callable tool: historical operational
# memory is recalled deterministically by the graph's recall_memory step.
