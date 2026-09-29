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
    client = get_chroma_client()
    ef = embedding_functions.OpenAIEmbeddingFunction(
        api_key=os.getenv("OPENAI_API_KEY"),
        model_name="text-embedding-3-small"
    )
    return client.get_or_create_collection(name=name, embedding_function=ef)

def embed_timeline(timeline, incident_id):
    collection = get_or_create_collection()
    if not timeline:
        return
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
    for i, chunk in enumerate(chunks):
        text = "\n".join(
            f"[{e['timestamp_str']}] [{e['source'].upper()}] {e['content']}"
            for e in chunk
        )
        collection.add(
            documents=[text],
            ids=[f"{incident_id}_chunk_{i}"],
            metadatas=[{"incident_id": incident_id, "chunk_index": i}]
        )
    print(f"Embedded {len(chunks)} chunks for incident '{incident_id}'")
