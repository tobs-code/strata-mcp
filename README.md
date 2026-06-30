# Strata

> A workload-adaptive agent memory system combining event logs, knowledge graphs, and vector embeddings. Evidence-based architecture derived from [Zhou et al. arXiv:2606.24775](https://arxiv.org/abs/2606.24775).

---

## Overview

Strata is a sophisticated agent memory system that intelligently classifies, routes, plans, and executes queries across multiple storage and retrieval strategies. It consists of **Python** (MCP control plane) and **Rust** (core services) components working together.

### Architecture

```
                  ┌──────────────────┐
                  │  MCP Server      │  (Python, stdio/HTTP)
                  │  (Control Plane) │
                  └──────┬───────────┘
                         │
         ┌───────────────┼───────────────┐
         ▼               ▼               ▼
   ┌──────────┐   ┌──────────┐   ┌──────────────┐
   │  Router  │   │ Planner  │   │ Maintenance  │  (Rust)
   │  (:8080) │   │ (:8081)  │   │              │
   └────┬─────┘   └────┬─────┘   └──────────────┘
        │              │
        └──────────────┘
               │
               ▼
        ┌──────────────┐
        │  SurrealDB   │
        │  (NS:strata  │
        │   DB:strata) │
        └──────────────┘
```

### Rust Components

| Component | Port | Description |
|-----------|------|-------------|
| **Router** | 8080 | Pattern-based query classification & policy-driven routing |
| **Planner** | 8081 | Plan building & execution with multiple retrieval strategies |
| **Maintenance** | — | Conservative maintenance (lazy flushing, logical invalidation) |
| **Common** | — | Shared data structures & utilities |

### Python Components

| Component | Path | Description |
|-----------|------|-------------|
| **MCP Server** | `src/mcp/server.py` | Control plane (Anthropic MCP protocol) — stdio mode |
| **Extraction** | `src/extraction/` | Entropy-gated entity extraction |
| **Router** | `src/router/` | Policy engine & cost tracking |
| **Planner** | `src/planner/` | Execution engine |
| **Maintenance** | `src/maintenance/` | Conservative maintainer |

---

## Key Features

- **Query Classification** — 5 types: Temporal, Factual, Multi-Hop, Conversational, Update
- **Adaptive Retrieval** — Strategy selection per query type (event log, KG, hybrid BM25+vector+temporal)
- **Entropy Gating** — LightMem-style composite score: Shannon character entropy + embedding novelty. Raw Event Log is always append-only; the gate decides only whether to extract into the Knowledge Graph.
- **Logical Invalidation** — `valid_until` timestamps instead of hard deletes
- **Cross-Language Consistency** — Identical classification & routing logic in Rust and Python
- **Cost Awareness** — Tracks & budgets resource consumption per strategy

---

## Quick Start

### Prerequisites

- Rust 1.75+
- Python 3.8+
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
# Terminal 1 — Router (Rust)
cd router && cargo run

# Terminal 2 — Planner (Rust)
cd planner && cargo run

# Terminal 3 — Maintenance (Rust)
cd maintenance && cargo run

# Terminal 4 — MCP Server (Python)
cd src/mcp && python server.py
```

---

## Performance & Benchmarks

Strata verfügt über ein integriertes Benchmark-System zur Messung der Tool-Latenz. Ergebnisse werden automatisch in `benchmarks/benchmark_results.md` protokolliert.

### Tool Performance (Stand: Juni 2026)

| Tool | Durchschnitt (ms) | P95 (ms) | Optimierung |
|------|-------------------|----------|-------------|
| `memory_stats` | ~775 ms | ~820 ms | Multi-Statement Batching |
| `memory_query` | ~350 ms | ~400 ms | Hybrid Retrieval |
| `memory_store` | ~800 ms | ~850 ms | CUDA-beschleunigt |
| `explain_routing` | < 1 ms | < 1 ms | Pure Logic |

Das `memory_stats` Tool wurde durch **SQL-Batching** um ca. **85% optimiert** (von ursprünglich >5s).

### Benchmark ausführen

```bash
python benchmarks/mcp_performance.py
```

---

## Testing

```bash
# Alle Tests (inkl. Router & Benchmark)
python tests/run_all_tests.py
python scripts/test_router_comprehensive.py
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
- **Threshold** (default): `0.55`.

**Decision:** `extract` if `composite >= threshold`, otherwise `ignore`.

**Length guardrails:** texts shorter than `min_length = 10` or longer than `max_length = 1000` characters are always skipped.

**Storage contract:** Every input is still written to the immutable Raw Event Log. The gate only controls whether the content is additionally extracted into the temporal Knowledge Graph.

**Logging:** Each decision is recorded in the `gate_log` table for later calibration/evaluation.

**Calibration note:** `alpha`, `beta`, and `threshold` are currently **initial defaults**. Use the logged `gate_log` entries to tune them against real traffic and find the sweet spot for your workload.

**MCP path status:** The current MCP memory tools (`memory_store`, query endpoints) write to the raw event log but do **not** invoke the entropy gate. To populate `gate_log`, call `EntropyGate.ingest()` from your ingestion pipeline or add a wrapper around the memory write path.

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