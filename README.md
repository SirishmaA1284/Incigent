---
title: Incident Analysis
emoji: 🔬
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: 1.33.0
app_file: ui/app.py
pinned: false
---
# Incident Analysis — AI-Assisted Incident Post-Mortem Generator

Ingests raw incident data (server logs, Slack threads, Jira tickets) and produces a structured post-mortem — root cause, evidence citations, action items, and a confidence level — using a GPT-4o LangGraph agent with retrieval over the incident's own evidence.

---

## Quick start — UI only

```bash
pip install -r requirements.txt
streamlit run ui/app.py
```

Choose **Demo — pre-loaded P1 outage** and click **Generate**. If the API is not running (or has no OpenAI key), the UI produces a rule-based report that is clearly labelled **Offline heuristic mode**, states why the AI agent did not run, and does not claim to recall any historical incidents.

---

## Full AI mode (GPT-4o)

```bash
cp .env.example .env
# Add your OPENAI_API_KEY to .env

# Terminal 1 — REST API
uvicorn api.main:app --reload

# Terminal 2 — UI
streamlit run ui/app.py
```

Every report shows its analysis mode: **AI agent analysis**, **AI agent failed**, or **Offline heuristic mode**. Without an OpenAI key, `POST /analyze` returns HTTP 503 rather than a substitute result.

### API authentication

Set `INCIDENT_API_TOKEN` to require `Authorization: Bearer <token>` on `POST /analyze`. If it is empty or unset, the API accepts unauthenticated requests — use that only for local development. `INCIDENT_API_TOKEN` is the only token variable read; the previous API token variable name is no longer recognised, so a deployment still using it will run without authentication.

---

## Architecture

```
Logs + Slack + Tickets
        │
   Ingestion layer         Regex signature library (ingestion/log_parser.PATTERNS):
   (log / slack / ticket   DB pool, OOM, circuit breakers, replication lag,
    parsers)               deployments, rollbacks, restarts, recovery events
        │
   Timeline builder        Unified chronological event stream, deduped, sorted,
                           numbered with event IDs, silence gaps marked
        │
   Evidence index          ChromaDB · text-embedding-3-small · 5-minute window chunks
   (embedder / retriever)  scoped per incident; chunks carry their event IDs
        │
   LangGraph agent         recall_memory → record_intake → analyst ⇄ tools
                           GPT-4o · search_timeline() (incident ID injected from graph state)
        │                         ▲ historical memory (advisory)
   FastAPI + Streamlit     REST API · UI with event filters · JSON + Markdown export

   Long-term memory        Hindsight (optional) via memory/ — past incidents, diagnoses,
                           recommendations, human decisions, actions, outcomes, lessons
```

Chroma holds only the **current incident's evidence**. Hindsight holds **long-term operational memory** across incidents. Recalled memory is advisory context: it never authorizes an action and is not an approval or audit store.

---

## Long-term memory (Hindsight, optional)

Uses the [Hindsight](https://hindsight.vectorize.io) Python client (`hindsight-client`, verified against 0.10.1) through `memory/hindsight_memory.py`; nothing else in the app talks to Hindsight directly.

```bash
pip install -r requirements.txt   # includes hindsight-client
# in .env
HINDSIGHT_API_URL=http://localhost:8888   # your Hindsight server or cloud endpoint
HINDSIGHT_API_KEY=                         # sent as a Bearer token, if your server needs one
HINDSIGHT_BANK_ID=incident-ops             # memory bank used by this app
```

Memory is enabled only when both `HINDSIGHT_API_URL` and `HINDSIGHT_BANK_ID` are set. The bank is created by Hindsight on the first retain; until then, recall finds no memories.

What happens on each analysis:

1. **Recall, before any reasoning.** A query is built from the timeline (services, failure patterns, error signatures) and sent with Hindsight's `recall`, restricted to this app's memories by tag. Memories recorded for the current incident are excluded.
2. The model receives **CURRENT INCIDENT EVIDENCE** and **HISTORICAL OPERATIONAL MEMORY** as separate inputs and is told to treat memory as unverified prior experience.
3. **Retain** only facts about the incident itself, with Hindsight's `retain`: incident received and an evidence digest. Each uses a stable `document_id`, so re-analysing an incident replaces its earlier records instead of duplicating them. Obvious secrets are redacted first.

The LLM's diagnosis and recommendations are **not** retained: they are unverified until a human decides on them, and the system must not learn from its own guesses.

Every report states the memory status: consulted, unavailable (analysis continues on current evidence only), not configured, or not consulted. If Hindsight is down, the analysis still runs. Failed retains are logged and kept in process memory for retry; they are lost if the process restarts.

The recorder can also store validated diagnoses and recommendations, human decisions (approvals and rejections with reasons), actions, verification results and lessons, but the app has no approval step yet, so it records none of these today.

Normal test runs never contact Hindsight, even with real credentials in `.env`: `tests/conftest.py` blocks the real client for every test not marked `hindsight_live`.

---

## Human approval

After analysis, the top recommendation (first P1, else first) becomes a canonical action payload and a **pending approval** in an application-owned SQLite database (`APPROVAL_DB_PATH`). The graph then pauses with LangGraph's `interrupt()`; its state is checkpointed to SQLite (`CHECKPOINT_DB_PATH`), so it can resume after a restart.

| Endpoint | Purpose |
|---|---|
| `GET /approvals/{id}` | Approval record and its audit log |
| `POST /approvals/{id}/approve` | Form: `actor`, optional `reason` |
| `POST /approvals/{id}/reject` | Form: `actor`, `reason` (required) |
| `POST /workflows/{thread_id}/resume` | Form: `approval_id` — resumes the paused analysis |
| `POST /approvals` | **Development/demo only**, disabled unless `ENABLE_DEV_APPROVAL_ENDPOINT=1`. JSON: `incident_id`, `kind`, `payload` |

Resuming passes only the approval ID. The application looks the approval up and checks that it belongs to this incident, has the expected kind, has the same SHA-256 hash of the canonical action payload, is approved, and has not expired or been used. Sending `approved=true` does nothing. Approval is never read from long-term memory, and approval events are not retained there.

**Consume before act.** No consequential action may execute unless its approval has been successfully *consumed*:

```
validate approval  →  atomically consume approval  →  execute action
```

- `validate()` establishes that an approval is valid *right now*. It changes nothing and is not permission to act.
- `consume()` is the one-time authorization handoff immediately before execution: it re-checks everything and marks the approval `consumed` in one transaction, so it succeeds at most once.
- An executor must never execute after validation alone. The only executor is the simulated remediation executor below, which follows exactly this order.

**The direct `POST /approvals` endpoint** is for development and demos only and returns 404 unless `ENABLE_DEV_APPROVAL_ENDPOINT=1`. It is not an authorization path: it creates a *pending* approval (recorded as `requested_by="dev-endpoint"`) that still needs a human decision and still has to pass the same validate/consume checks for the exact incident, kind and payload. It cannot resume an analysis workflow, which only accepts the approval that workflow created. The intended flow is: analysis proposes an action → the application creates the approval → a human decides → the executor validates and consumes → the action runs.

**One analysis, one proposed action, one approval.** Only the top recommendation is proposed; other action items get no approval and cannot run. A future version must decide whether multiple actions need separate approvals or whether an explicitly defined action bundle can share one.

**Expiration is lazy.** There is no background worker or scheduler. An approval past its expiry time is marked `expired` (and audited) the next time it is touched — approve, reject, validate, consume or expire. Until then its stored status may still read `pending` or `approved`, but every path that could act on it checks expiry first, so an expired approval can never be approved or consumed and is never revived.

**Checkpoint data is sensitive.** `CHECKPOINT_DB_PATH` (default `var/checkpoints.sqlite3`) holds full paused-workflow state: incident IDs, log and ticket excerpts, model messages and the proposed action. `APPROVAL_DB_PATH` holds approval payloads and the audit log. `var/` is gitignored — never commit these files or share them casually, and in production put them on protected storage with restricted access. They are not encrypted by this application. Anyone who can write to them can alter workflow or approval state.

`actor` is a self-declared name recorded in the audit log, not authenticated identity: the API has only the shared `INCIDENT_API_TOKEN`. Per-user authentication is future work.

---

## Simulated remediation and verification

After a human approves, the resumed workflow can carry out the approved action — **against an in-memory simulator only** (`remediation/simulator.py`). The simulator is plain Python state with fixed scenarios (`INC-SIM-001` to `INC-SIM-004`). It never uses a shell, subprocesses, the network, containers, clusters, SSH or real databases.

```
propose_action → request_approval → interrupt → human decision → validate_approval
  → execute_remediation (validate → consume → apply) → verify_remediation → record_outcome
```

- **Only three allowlisted actions:** `restart_service`, `rollback_deployment` (`target_version`: `"previous"` or a version), and `scale_service` (`replicas` 1–20). Each has an exact field set. Unknown kinds, missing or extra fields, and anything resembling a command are rejected. The model may propose one under `remediation`. The application validates it first:
  - **valid** → canonicalized into the approval payload, where the action hash covers it
  - **invalid** → the proposal is dropped and **no approval is created** (workflow status `proposal_rejected`)
  - **null or absent** → the usual non-executable approval for the top recommendation
- **`consume()` is the authorization boundary.** The executor checks the payload and the target service, calls `validate()`, then `consume()`, and touches the simulator only after `consume()` returns. If `consume()` fails, nothing runs. One approval executes at most once: a second attempt fails with `consumed`, and the simulator also refuses a second application for the same approval.
- **Verification is independent.** The verifier reads the simulator's state, never the executor's report, and checks the specific postcondition (deployed version, replicas, or restart count) plus service health.
- **Approved ≠ executed ≠ verified.** A restart on `INC-SIM-001` executes but the leaking version keeps the service unhealthy, so the workflow ends `remediation_unverified`, not recovered.
- **Only validated outcomes become memory.** A human decision, the executed action, a passed verification, and a lesson are retained only after verification passes. Unverified or failed outcomes retain nothing. The executor never reads memory, and it works with Hindsight unavailable or unconfigured.

**Crash consistency (known limitation).** Consuming the approval gives *at-most-once authorization*: one approval can never authorize two executions. But applying the action and saving the graph checkpoint that records the result are **not one atomic transaction**. If the process crashes after the action is applied but before that checkpoint is saved, a later resume finds the approval already consumed, so nothing runs again. The persisted workflow result can then under-report what actually happened, and lag behind the simulator's real state. This is **not** exactly-once distributed execution. It's acceptable for this in-memory demo; persistent execution infrastructure will need a durable execution record, idempotency keyed by approval ID, and reconciliation.

Read-only views: `GET /workflows/{thread_id}` (approval, execution and verification results) and `GET /simulator/{incident_id}` (simulated state). There is no endpoint that executes an action directly; execution only happens inside a resumed workflow after its own approval is consumed.

---

## Evaluation

`evaluation/ragas_eval.py` implements RAGAS-style string-matching metrics (faithfulness, context recall, answer relevance, citation coverage):

```bash
python -m evaluation.ragas_eval
python -m evaluation.ragas_eval --postmortem path/to/postmortem.json --timeline path/to/logs.txt
```

Without `--postmortem` it scores a built-in sample post-mortem, not live agent output. No benchmark results are published here yet.

---

## Project structure

```
.
├── agent/           LangGraph agent, prompts, tools, offline heuristic fallback
├── api/             FastAPI REST endpoint
├── data/raw/        Sample incident data (DB outage scenario)
├── evaluation/      Evaluation metrics and baselines
├── ingestion/       Log parser (signature library), Slack parser, ticket parser, timeline builder
├── memory/          Long-term operational memory: recorder, redaction, Hindsight adapter, test fake
├── rag/             Current-incident evidence: embedder, ChromaDB store, retriever
├── tests/           pytest suite
└── ui/              Streamlit interface — demo mode, filters, JSON/Markdown export
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph (conditional tool-calling loop) |
| LLM | GPT-4o (temperature 0, JSON output requested in the prompt) |
| Vector store (current incident) | ChromaDB + text-embedding-3-small |
| Long-term memory (optional) | Hindsight (`hindsight-client`) |
| API | FastAPI + uvicorn |
| UI | Streamlit (event filters, JSON + Markdown export) |
| Evaluation | RAGAS-style custom metrics |

---

## Running tests

```bash
python -m pytest -q
# Live tests call the real OpenAI API and are deselected by default:
python -m pytest -m live -v
# Live Hindsight round trip (needs HINDSIGHT_API_URL and a separate HINDSIGHT_TEST_BANK_ID):
python -m pytest -m hindsight_live -v
```

The default suite needs no OpenAI key and no Hindsight server; memory tests use an in-process fake that is never used by the application itself.

---

## Deploy to Streamlit Cloud

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select your fork
3. Set main file path: `ui/app.py`
4. Deploy

The Streamlit app calls the API at `INCIDENT_API_URL`. Without a reachable API, reports run in the labelled Offline heuristic mode.
