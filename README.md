# Strata

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![SurrealDB](https://img.shields.io/badge/SurrealDB-3.1.5-8B5CF6)
![License](https://img.shields.io/badge/license-Apache%202.0-green)
![arXiv](https://img.shields.io/badge/arXiv-2606.24775-b31b1b)
![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen)

> A workload-adaptive agent memory system combining event logs, knowledge graphs, and vector embeddings. Evidence-based architecture inspired from [Zhou et al. arXiv:2606.24775](https://arxiv.org/abs/2606.24775).

---

## Overview

Strata is an agent memory system that intelligently classifies, routes, plans, and executes queries across multiple storage and retrieval strategies. It consists of a **Python-based Control Plane** (MCP Server) that interfaces with SurrealDB for storage and retrieval operations.

### Architecture

```
                  ┌──────────────────┐
                  │  MCP Server      │  (Python, stdio/HTTP)
                  └──────┬───────────┘
                         │
                         ▼
        ┌─────────────────────────────┐
        │     SurrealDB Storage       │
        │  (NS:strata DB:strata)      │
        └─────────────────────────────┘
```

### Python Components

| Component | Path | Description |
|-----------|------|-------------|
| **MCP Server** | `src/mcp/server.py` | Control plane (Anthropic MCP protocol) — stdio mode. 10 tools: `memory_store`, `memory_query`, `memory_update`, `memory_forget`, `memory_consolidate`, `event_log_search`, `kg_query`, `semantic_search`, `memory_stats`, `explain_routing` |
| **Extraction** | `src/extraction/` | Entropy-gated entity extraction with Groq API (llama-3.1-8b-instant) or spaCy fallback. Pipe-separated LLM prompt, type preservation |
| **Router** | `src/router/` | Policy engine & cost tracking |
| **Planner** | `src/planner/` | Execution engine |
| **Maintenance** | `src/maintenance/` | Conservative maintainer |

---

## Key Features

- **Query Classification** — 5 types: Temporal, Factual, Multi-Hop, Conversational, Update
- **Adaptive Retrieval** — Strategy selection per query type (event log, KG, hybrid BM25+vector+temporal)
- **Entropy Gating** — LightMem-style composite score: Shannon character entropy + embedding novelty. Raw Event Log is always append-only; the gate decides only whether to extract into the Knowledge Graph.
- **Entity Extraction** — Groq API (`llama-3.1-8b-instant`) with spaCy fallback. Pipe-separated LLM prompt, type preservation (LLM classification preferred over heuristic).
- **Logical Invalidation** — `valid_until` timestamps instead of hard deletes. `memory_update` auto-creates target entities if they don't exist yet.
- **Forgetting & Consolidation** — `memory_forget` soft-deletes events or entities; `memory_consolidate` triggers maintenance runs (with optional physical stale-fact removal).
- **Cross-Language Consistency** — Identical classification & routing logic in Rust and Python
- **Cost Awareness** — Tracks & budgets resource consumption per strategy

---

## Quick Start

### Prerequisites

- Rust 1.75+
- Python 3.11+
- SurrealDB running on `http://127.0.0.1:8000`

### Setup

```bash
# 1. Copy environment config
cp .env.example .env

# 2. Start SurrealDB (Docker)
docker-compose up -d

# 3. Load schema
python scripts/load_schema.py

# 4. Run all tests
python tests/run_all_tests.py
```

### Start Services

```bash
# Terminal 1 — MCP Server (Python)
cd src/mcp && python server.py
```

---

## Performance & Benchmarks

Strata has an integrated benchmark system to measure tool latency. Results are automatically logged to `benchmarks/benchmark_results.md`.

### Tool Performance (as of July 2026, warm SurrealDB, CPU-only embeddings)

| Tool | Average (ms) | P95 (ms) | Notes |
|------|--------------|----------|-------|
| `memory_stats` | ~790 | ~860 | Multi-statement batch query |
| `memory_store` | ~228 | ~254 | SentenceTransformers (CPU); first call ~800ms (cold model load) |
| `memory_query` | ~405 | ~435 | Hybrid retrieval (classify → plan → execute) |
| `semantic_search` | ~800 | ~875 | Embedding + HNSW vector search |
| `event_log_search` | ~760 | ~810 | Hybrid FTX+Vector (RRF) |
| `kg_query` | ~760 | ~800 | SurrealDB graph traversal |
| `explain_routing` | ~0.20 | ~0.23 | Pure in-process logic |

### Run Benchmarks

```bash
python benchmarks/mcp_performance.py
```

---

## Testing

```bash
# All tests 
python tests/run_all_tests.py
python benchmarks/mcp_performance.py
```

---

## Entropy Gating — How It Works

The gate prevents the Knowledge Graph from being flooded with low-value entries.

**Formula (LightMem-style):**

```
composite = alpha * normalized_text_entropy + beta * embedding_novelty
```

- **Text entropy** = Shannon entropy on character level (alphanumeric + whitespace), normalized to `[0, 1]` using a max of ~4.5 bits.
- **Embedding novelty** = `1 − avg cosine similarity` to the top-5 most similar previously stored embeddings (in-memory index for the current session).
- **Weights** (default): `alpha = 0.35`, `beta = 0.65`.
- **Threshold** (adaptive): starts at `0.30` (cold start) and ramps linearly to `0.55` after ~150 events.

**Decision:** `extract` if `composite >= threshold`, otherwise `ignore`.

**Length guardrails:** texts shorter than `min_length = 10` or longer than `max_length = 1000` characters are always skipped.

**Storage contract:** Every input is still written to the immutable Raw Event Log. The gate only controls whether the content is additionally extracted into the temporal Knowledge Graph.

**Logging:** Each decision is recorded in the `gate_log` table for later calibration/evaluation.

**Calibration note:** `alpha`, `beta`, and `threshold` are currently **initial defaults**. Use the logged `gate_log` entries to tune them against real traffic and find the sweet spot for your workload.

**MCP path status:** The current MCP memory tools (`memory_store`, query endpoints) write to the raw event log **and invoke the entropy gate**. The `memory_store` tool calls `EntropyGate.ingest()` which logs decisions to `gate_log` for calibration.

---

## Resilience & Error Handling

**Implemented in `src/mcp/server.py`:**

- **Retry:** up to **3 attempts** by default; heavy queries (`RELATE`/`DEFINE`/`CREATE`) use **2 attempts**.
- **Jittered backoff:** full jitter (`uniform(0, min(8s, 0.5 * 2^level))`) to avoid thundering herd.
- **Circuit breaker:** opens after **5 failures**; half-open probe after **10s** quiet period.
- **Background Reconnect:** A dedicated async task periodically probes SurrealDB when the circuit is open, ensuring automatic recovery.
- **Thread safety:** circuit state protected by a lock; successful calls reset failure count and backoff level.
- **Timeouts:** `timeout=30` seconds per HTTP call to SurrealDB.

---

## Cost Model & Adaptive Enforcement

Budgets are **measured, enforced, and adaptively scaled** per execution.

| Budget | Base limit | Strategy examples | Enforcement |
|--------|------------|-------------------|-------------|
| `low` | <= 10 DB calls / 1k tokens | KG-first | result truncation |
| `medium` | <= 25 DB calls / 3k tokens | Hybrid BM25+vector+temporal | result truncation |
| `high` | <= 50 DB calls / 8k tokens | Graph expansion + invalidation | best-effort truncation |

- **Adaptive Scaling:** Limits are automatically scaled down based on a **System Health Factor**. As SurrealDB failures increase, budgets are tightened to reduce load and improve stability.
- **Token counting:** uses `tiktoken` (`gpt-3.5-turbo` encoding) where available; otherwise falls back to `chars/4`.
- **BudgetTracker:** records `db_calls` and `estimated_tokens` and exposes `OverBudget` for aborts/throttling.

---

## Schema Evolution / Migration

- Schema files live in `docs/*.surql`.
- `docs/schema.surql` uses `DEFINE TABLE IF NOT EXISTS`, `DEFINE INDEX IF NOT EXISTS`, and `DEFINE FIELD IF NOT EXISTS` — so repeated loads are idempotent.
- **There is no automatic data migration** for breaking schema changes (e.g. renaming fields or changing types). In that case, export (`surreal export` or custom scripts), transform, and re-import into a new namespace/DB.
- Non-breaking additive changes: just add new fields/tables and deploy.

---

## Database Schema (SurrealDB)

| Table | Type | Purpose |
|-------|------|---------|
| `event` | NORMAL | Raw event log (content, timestamp) |
| `entity` | SCHEMAFULL | Knowledge graph entities (name) |
| `fact` | NORMAL | Relations between entities (subject, object, predicate) |
| `gate_log` | SCHEMAFULL | Entropy gate decisions |

---

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for details.
