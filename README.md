---
title: InfraLens
emoji: 🔬
colorFrom: blue
colorTo: indigo
sdk: streamlit
sdk_version: 1.33.0
app_file: ui/app.py
pinned: false
---
# InfraLens — AI-Powered Incident Post-Mortem Generator

InfraLens ingests raw incident data (server logs, Slack threads, Jira tickets) and produces a structured post-mortem in seconds — root cause, evidence citations, action items, and a confidence score — powered by GPT-4o and a LangGraph RAG pipeline.

---

## Quick start — demo mode (no API key needed)

```bash
pip install -r requirements.txt
streamlit run ui/app.py
```

Click **Run Live Demo** — a pre-loaded database outage scenario runs instantly.  
No uploads, no API key, no configuration.

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

---

## Architecture

```
Logs + Slack + Tickets
        │
   Ingestion layer         Pattern recognition: DB pool, OOM, circuit breakers,
   (log / slack / ticket   replication lag, deployments, recovery events (18 patterns)
    parsers)
        │
   Timeline builder        Unified chronological event stream, deduped and sorted
        │
   RAG layer               ChromaDB vector store · text-embedding-3-small
   (embedder / retriever)  5-minute window chunking · incident memory store
        │
   LangGraph agent         GPT-4o · tool-calling loop
   tools:                  search_timeline()  ·  search_past_incidents()
        │
   FastAPI + Streamlit     REST API · dark-mode UI · Markdown + PDF export
```

---

## Sample output — DB outage scenario (INC-DEMO-4821)

**Input:** 48 events across logs, Slack, and tickets spanning 20 minutes  
**Processing time:** ~6 seconds

| Field | Value |
|---|---|
| Root cause | DB connection pool exhausted — `idle_in_transaction` leak in order-service v2.4.1 |
| Duration | 20 minutes (02:01 → 02:21 UTC) |
| Confidence | HIGH |
| Evidence citations | 7 log lines pinpointed |
| Patterns detected | 16 signal types (UPSTREAM_TIMEOUT ×3, DB_IDLE_LEAK ×3, CIRCUIT_BREAKER_OPEN, ...) |
| Action items | 5 tasks with owners (Backend Team, SRE, QA) and P1/P2/P3 priorities |
| Past incidents matched | 2 (INC-3901: batch job pool leak; INC-4105: Redis circuit breaker) |

Root cause identified:
> DB connection pool exhausted due to connection leak (`idle_in_transaction`) caused by improper transaction handling in application code.

---

## Evaluation

RAGAS-style evaluation against the demo scenario (`evaluation/ragas_eval.py`):

```
python -m evaluation.ragas_eval
```

| Metric | Score | What it measures |
|---|---|---|
| Faithfulness | 1.000 | All evidence citations trace back to actual log lines |
| Context recall | 0.800 | Ground-truth root-cause keywords found in output |
| Answer relevance | 1.000 | Action items cover required owners and have P1 items |
| Citation coverage | 0.750 | Critical events (pool exhausted, max_conn, health fail, circuit breaker) cited |
| **Aggregate** | **0.887 (PASS)** | |

Run against your own postmortem:
```bash
python -m evaluation.ragas_eval \
  --postmortem path/to/postmortem.json \
  --timeline   path/to/logs.txt
```

---

## Project structure

```
infralens/
├── agent/           LangGraph agent, prompts, tool definitions
├── api/             FastAPI REST endpoint
├── data/raw/        Sample incident data (DB outage scenario)
├── evaluation/      RAGAS eval — faithfulness, recall, relevance, citation coverage
├── ingestion/       Log parser (18-pattern library), Slack parser, ticket parser
├── rag/             Embedder, ChromaDB store, retriever
├── tests/           pytest suite — agent, parsers, JSON extraction
└── ui/              Streamlit interface — demo mode, filters, PDF export
```

---

## Tech stack

| Layer | Technology |
|---|---|
| Agent orchestration | LangGraph (conditional tool-calling loop) |
| LLM | GPT-4o (temperature 0, structured JSON output) |
| Vector store | ChromaDB + text-embedding-3-small |
| API | FastAPI + uvicorn |
| UI | Streamlit (dark mode, event filter, PDF export) |
| PDF generation | ReportLab |
| Evaluation | RAGAS-style custom metrics |

---

## Running tests

```bash
pytest tests/ -v
# Live API tests (requires OPENAI_API_KEY):
pytest tests/ -v -m live
```

---

## Deploy to Streamlit Cloud (free, 5 minutes)

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io) → New app → select your fork
3. Set main file path: `infralens/ui/app.py`
4. Add secret: `OPENAI_API_KEY = "sk-..."` (optional — demo mode works without it)
5. Deploy

Demo mode works with zero secrets — visitors can run the DB outage scenario immediately.