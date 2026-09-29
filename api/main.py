from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Body, FastAPI, Request, UploadFile, File, Form, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import json
import os
import sqlite3
import tempfile
from dotenv import load_dotenv

load_dotenv()

from langgraph.checkpoint.sqlite import SqliteSaver

from ingestion.log_parser import parse_log_file
from ingestion.slack_parser import parse_slack_export
from ingestion.ticket_parser import parse_tickets
from ingestion.timeline_builder import build_timeline
from rag.embedder import embed_timeline
from agent.graph import (MissingAPIKeyError, WorkflowNotAwaitingApproval, build_graph,
                         resume_agent, run_agent)
from approvals import ApprovalDenied, build_approval_service
from memory import build_memory_recorder

DEFAULT_CHECKPOINT_DB = os.path.join("var", "checkpoints.sqlite3")


def build_checkpointer():
    """LangGraph checkpoints (paused workflows) in SQLite at CHECKPOINT_DB_PATH."""
    path = os.getenv("CHECKPOINT_DB_PATH") or DEFAULT_CHECKPOINT_DB
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Approvals and paused workflows live in application-owned SQLite files.
    app.state.approvals = build_approval_service()
    app.state.checkpointer = build_checkpointer()
    # Build the agent graph once at startup and share it across requests.
    try:
        app.state.graph = build_graph(checkpointer=app.state.checkpointer)
    except MissingAPIKeyError as e:
        # Missing API key — log clearly but don't crash the server.
        print(f"[WARN] Could not initialise the agent graph at startup: {e}")
        app.state.graph = None
    # Long-term operational memory (Hindsight) is optional; without it the
    # analysis runs and reports that historical memory was not consulted.
    app.state.memory_recorder = build_memory_recorder()
    if app.state.memory_recorder.memory is None:
        print(f"[INFO] Long-term memory disabled: {app.state.memory_recorder.disabled_reason}")
    yield
    app.state.checkpointer.conn.close()


app = FastAPI(title="Incident Analysis API", lifespan=lifespan)

# ── Auth ──────────────────────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)
_API_TOKEN = os.getenv("INCIDENT_API_TOKEN", "")

def _require_auth(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    """
    Simple static bearer-token check.
    Set INCIDENT_API_TOKEN in .env.  If the env var is empty (dev mode) auth
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

# ── Upload validation helpers ─────────────────────────────────────────────────
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
    return {"status": "running"}


@app.post("/analyze")
async def analyze_incident(
    request: Request,
    incident_id: str = Form(...),
    log_file: UploadFile = File(None),
    slack_file: UploadFile = File(None),
    ticket_file: UploadFile = File(None),
    _auth=Depends(_require_auth),
):
    # Resolve the agent graph first so a missing API key fails fast with a
    # clear 503 instead of partway through ingestion. If startup could not
    # build it (e.g. the key was added later), try once more now.
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        try:
            graph = build_graph(checkpointer=request.app.state.checkpointer)
        except MissingAPIKeyError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"AI agent unavailable: {e}",
            )
        request.app.state.graph = graph

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

    result = run_agent(incident_id, timeline, graph=graph,
                       recorder=getattr(request.app.state, "memory_recorder", None),
                       approvals=request.app.state.approvals)

    return JSONResponse({"incident_id": incident_id, "postmortem": result})


# ── Human approvals ───────────────────────────────────────────────────────────
# Decisions carry an `actor` string. With INCIDENT_API_TOKEN, every caller shares
# one token, so `actor` is a self-declared name for the audit log — it is NOT
# authenticated identity. Real per-user authentication is future work.

def _approval_or_404(request: Request, approval_id: str):
    approval = request.app.state.approvals.get(approval_id)
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    return approval


def _denied(e: ApprovalDenied):
    code = status.HTTP_404_NOT_FOUND if e.code == "not_found" else status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail={"code": e.code, "message": str(e)})


DEV_APPROVAL_ENDPOINT_ENV = "ENABLE_DEV_APPROVAL_ENDPOINT"
DEV_REQUESTER = "dev-endpoint"


def _dev_endpoint_enabled() -> bool:
    return os.getenv(DEV_APPROVAL_ENDPOINT_ENV, "").strip().lower() in ("1", "true", "yes")


@app.post("/approvals")
def request_approval(
    request: Request,
    incident_id: str = Body(...),
    kind: str = Body(...),
    payload: dict = Body(...),
    ttl_seconds: Optional[int] = Body(None),
    _auth=Depends(_require_auth),
):
    """
    DEVELOPMENT/DEMO ONLY — disabled (404) unless ENABLE_DEV_APPROVAL_ENDPOINT=1.

    Creates a *pending* approval for a caller-supplied payload, recorded with
    requested_by="dev-endpoint". It authorizes nothing by itself: the approval
    still needs a human decision, and any future executor must still validate
    and atomically consume it against the exact incident, kind and payload hash.
    It also cannot resume an analysis workflow, which accepts only the approval
    that workflow created. The normal path is: analysis proposes an action ->
    the application creates the approval -> human decides -> executor consumes.
    """
    if not _dev_endpoint_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    try:
        approval = request.app.state.approvals.request_approval(
            incident_id, kind, payload, requested_by=DEV_REQUESTER, ttl_seconds=ttl_seconds)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=str(e))
    return approval.to_dict()


@app.get("/approvals/{approval_id}")
def get_approval(request: Request, approval_id: str, _auth=Depends(_require_auth)):
    approval = _approval_or_404(request, approval_id)
    return {"approval": approval.to_dict(),
            "audit": request.app.state.approvals.audit_events(approval_id)}


@app.post("/approvals/{approval_id}/approve")
def approve_approval(request: Request, approval_id: str, actor: str = Form(...),
                     reason: Optional[str] = Form(None), _auth=Depends(_require_auth)):
    try:
        return request.app.state.approvals.approve(approval_id, actor=actor, reason=reason).to_dict()
    except ApprovalDenied as e:
        raise _denied(e)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/approvals/{approval_id}/reject")
def reject_approval(request: Request, approval_id: str, actor: str = Form(...),
                    reason: str = Form(...), _auth=Depends(_require_auth)):
    try:
        return request.app.state.approvals.reject(approval_id, actor=actor, reason=reason).to_dict()
    except ApprovalDenied as e:
        raise _denied(e)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/workflows/{thread_id}/resume")
def resume_workflow(request: Request, thread_id: str, approval_id: str = Form(...),
                    _auth=Depends(_require_auth)):
    """Resume a paused analysis. Only the approval ID is forwarded; the graph
    re-validates the approval against the approval database. Any other fields
    (e.g. approved=true) are ignored."""
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="AI agent unavailable")
    try:
        return resume_agent(thread_id, approval_id, graph=graph, approvals=request.app.state.approvals)
    except WorkflowNotAwaitingApproval as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
