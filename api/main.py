from fastapi import FastAPI, UploadFile, File, Form, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import json
import os
import tempfile
from dotenv import load_dotenv

load_dotenv()

from ingestion.log_parser import parse_log_file
from ingestion.slack_parser import parse_slack_export
from ingestion.ticket_parser import parse_tickets
from ingestion.timeline_builder import build_timeline
from rag.embedder import embed_timeline
from agent.graph import build_graph, get_llm

app = FastAPI(title="InfraLens API")

# ── Singletons (Fix #4) ───────────────────────────────────────────────────────
# Build once at startup rather than on every request.
_graph = None
_llm = None

@app.on_event("startup")
def _init_singletons():
    global _graph, _llm
    try:
        _llm = get_llm()
        _graph = build_graph()
    except EnvironmentError as e:
        # Missing API key — log clearly but don't crash the server.
        print(f"[WARN] Could not initialise LLM at startup: {e}")

# ── Auth (Fix #3) ─────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)
_API_TOKEN = os.getenv("INFRALENS_API_TOKEN", "")

def _require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """
    Simple static bearer-token check.
    Set INFRALENS_API_TOKEN in .env.  If the env var is empty (dev mode) auth
    is skipped so local runs still work without any configuration.
    """
    if not _API_TOKEN:
        # No token configured — open access (dev/local only).
        return
    if credentials is None or credentials.credentials != _API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

# ── Upload validation helpers (Fix #8) ───────────────────────────────────────
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB per file
_ALLOWED_LOG_MIME = {"text/plain", "application/octet-stream"}
_ALLOWED_JSON_MIME = {"application/json", "text/plain", "application/octet-stream"}

async def _read_validated(upload: UploadFile, allowed_mime: set, label: str) -> bytes:
    """Read an uploaded file, enforcing size cap and MIME type."""
    content_type = (upload.content_type or "").split(";")[0].strip()
    if content_type and content_type not in allowed_mime:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"{label}: unsupported content type '{content_type}'.",
        )
    data = await upload.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"{label} exceeds the 10 MB upload limit.",
        )
    return data

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "InfraLens is running"}


@app.post("/analyze")
async def analyze_incident(
    incident_id: str = Form(...),
    log_file: UploadFile = File(None),
    slack_file: UploadFile = File(None),
    ticket_file: UploadFile = File(None),
    _auth=Depends(_require_auth),
):
    sources = []

    if log_file:
        raw = await _read_validated(log_file, _ALLOWED_LOG_MIME, "log_file")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        sources.append(parse_log_file(tmp_path))
        os.unlink(tmp_path)

    if slack_file:
        raw = await _read_validated(slack_file, _ALLOWED_JSON_MIME, "slack_file")
        data = json.loads(raw.decode())
        sources.append(parse_slack_export(data))

    if ticket_file:
        raw = await _read_validated(ticket_file, _ALLOWED_JSON_MIME, "ticket_file")
        data = json.loads(raw.decode())
        sources.append(parse_tickets(data))

    if not sources:
        return JSONResponse({"error": "No files uploaded"}, status_code=400)

    timeline = build_timeline(sources)
    embed_timeline(timeline, incident_id)

    # Re-use the module-level singleton graph; fall back to building one if
    # startup failed (e.g. API key added after server start).
    from agent.graph import run_agent
    result = run_agent(incident_id, timeline, graph=_graph)

    return JSONResponse({"incident_id": incident_id, "postmortem": result})