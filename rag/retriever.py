from rag.embedder import get_or_create_collection

def _parse_event_ids(value):
    ids = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids

def retrieve_evidence(query, incident_id, top_k=5):
    collection = get_or_create_collection()
    results = collection.query(
        query_texts=[query],
        n_results=top_k,
        where={"incident_id": incident_id}
    )
    evidence = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        evidence.append({
            "text": doc,
            "incident_id": meta.get("incident_id"),
            "chunk_index": meta.get("chunk_index"),
            "event_ids": _parse_event_ids(meta.get("event_ids")),
        })
    return evidence
