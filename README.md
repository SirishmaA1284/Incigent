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
