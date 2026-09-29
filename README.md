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
   LangGraph agent         GPT-4o · tool-calling loop
   tools:                  search_timeline()  (incident ID injected from graph state)
                           search_past_incidents()
        │
   FastAPI + Streamlit     REST API · UI with event filters · JSON + Markdown export
```

Past-incident memory (`rag/memory_store.py`) is not populated yet, so `search_past_incidents` currently returns no matches.

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
├── rag/             Embedder, ChromaDB store, retriever
├── tests/           pytest suite
└── ui/              Streamlit interface — demo mode, filters, JSON/Markdown export
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph (conditional tool-calling loop) |
| LLM | GPT-4o (temperature 0, JSON output requested in the prompt) |
| Vector store | ChromaDB + text-embedding-3-small |
| API | FastAPI + uvicorn |
| UI | Streamlit (event filters, JSON + Markdown export) |
| Evaluation | RAGAS-style custom metrics |

---

## Running tests

```bash
python -m pytest -q
# Live tests call the real OpenAI API and are deselected by default:
python -m pytest -m live -v
```

---

## Deploy to Streamlit Cloud

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select your fork
3. Set main file path: `ui/app.py`
4. Deploy

The Streamlit app calls the API at `INCIDENT_API_URL`. Without a reachable API, reports run in the labelled Offline heuristic mode.
