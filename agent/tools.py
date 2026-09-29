from langchain.tools import tool
from rag.retriever import retrieve_evidence
from rag.memory_store import retrieve_similar_incidents as _retrieve_similar

@tool
def search_timeline(query: str, incident_id: str) -> str:
    """Search the incident timeline for events relevant to a query. Use before making any claim."""
    results = retrieve_evidence(query, incident_id)
    if not results:
        return "No relevant evidence found."
    return "\n\n---\n".join(r["text"] for r in results)

@tool
def search_past_incidents(query: str) -> str:
    """Search previously resolved incidents for similar failure patterns."""
    results = _retrieve_similar(query)
    if not results:
        return "No similar past incidents found."
    return "\n\n---\n".join(results)
