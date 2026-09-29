import os
from dotenv import load_dotenv
from rag.embedder import get_chroma_client
from chromadb.utils import embedding_functions

load_dotenv()

def get_memory_collection():
    client = get_chroma_client()
    ef = embedding_functions.OpenAIEmbeddingFunction(
        api_key=os.getenv("OPENAI_API_KEY"),
        model_name="text-embedding-3-small"
    )
    return client.get_or_create_collection("past_postmortems", embedding_function=ef)

def save_postmortem_to_memory(incident_id, postmortem_text):
    col = get_memory_collection()
    col.add(
        documents=[postmortem_text],
        ids=[incident_id],
        metadatas=[{"incident_id": incident_id}]
    )

def retrieve_similar_incidents(query, top_k=3):
    col = get_memory_collection()
    try:
        results = col.query(query_texts=[query], n_results=top_k)
        return results["documents"][0]
    except Exception:
        return []
