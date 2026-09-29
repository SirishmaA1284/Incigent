import chromadb
from chromadb.utils import embedding_functions
import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

CHROMA_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_db")

def get_chroma_client():
    return chromadb.PersistentClient(path=CHROMA_DIR)

def get_or_create_collection(name="incidents"):
    """EMBEDDINGS_PROVIDER=local uses Chroma's built-in local embedding model
    (no API key; downloaded once), for chat gateways that offer no embeddings
    API. Local vectors have a different dimension, so they live in their own
    collection. Default: OpenAI text-embedding-3-small."""
    client = get_chroma_client()
    if os.getenv("EMBEDDINGS_PROVIDER", "openai").strip().lower() == "local":
        ef = embedding_functions.DefaultEmbeddingFunction()
        return client.get_or_create_collection(name=f"{name}_local", embedding_function=ef)
    ef = embedding_functions.OpenAIEmbeddingFunction(
        api_key=os.getenv("OPENAI_API_KEY"),
        model_name="text-embedding-3-small"
    )
    return client.get_or_create_collection(name=name, embedding_function=ef)

def chunk_timeline(timeline, incident_id):
    """
    Group events into 5-minute windows. Returns (document, metadata) pairs.
    Each line carries its [E<id>] prefix and the metadata lists the chunk's
    event IDs, so retrieved evidence can be traced back to timeline events.
    """
    if not timeline:
        return []
    chunks = []
    current_chunk = []
    chunk_start = timeline[0]["timestamp"]
    for event in timeline:
        if event["timestamp"] - chunk_start <= timedelta(minutes=5):
            current_chunk.append(event)
        else:
            chunks.append(current_chunk)
            current_chunk = [event]
            chunk_start = event["timestamp"]
    if current_chunk:
        chunks.append(current_chunk)

    out = []
    for i, chunk in enumerate(chunks):
        text = "\n".join(
            f"[E{e.get('event_id', '?')}] [{e['timestamp_str']}] [{e['source'].upper()}] {e['content']}"
            for e in chunk
        )
        # Chroma metadata values must be scalars, so event IDs are stored as a
        # comma-separated string.
        event_ids = ",".join(str(e.get("event_id")) for e in chunk if e.get("event_id") is not None)
        out.append((text, {"incident_id": incident_id, "chunk_index": i, "event_ids": event_ids}))
    return out

def embed_timeline(timeline, incident_id):
    collection = get_or_create_collection()
    if not timeline:
        return
    chunks = chunk_timeline(timeline, incident_id)
    # Drop chunks from a previous analysis of the same incident so re-running
    # it never mixes stale evidence with new evidence.
    collection.delete(where={"incident_id": incident_id})
    for i, (text, metadata) in enumerate(chunks):
        collection.add(
            documents=[text],
            ids=[f"{incident_id}_chunk_{i}"],
            metadatas=[metadata]
        )
    print(f"Embedded {len(chunks)} chunks for incident '{incident_id}'")
