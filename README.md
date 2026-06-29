# Strata

> A workload-adaptive agent memory system combining event logs, knowledge graphs, and vector embeddings. Evidence-based architecture derived from [Zhou et al. arXiv:2606.24775](https://arxiv.org/abs/2606.24775).

---

## Overview

Strata is a sophisticated agent memory system that intelligently classifies, routes, plans, and executes queries across multiple storage and retrieval strategies. It consists of **Rust** (core services) and **Python** (control plane & extraction) components working together.

### Architecture

```
                  ┌──────────────┐
                  │  MCP Server  │  (Python, :8082)
                  │  (Control    │
                  │   Plane)     │
                  └──────┬───────┘
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
| **MCP Server** | `src/mcp/server.py` | Control plane (Anthropic MCP protocol) |
| **Extraction** | `src/extraction/` | Entropy-gated entity extraction |
| **Router** | `src/router/` | Policy engine & cost tracking |
| **Planner** | `src/planner/` | Execution engine |
| **Maintenance** | `src/maintenance/` | Conservative maintainer |

---

## Key Features

- **Query Classification** — 5 types: Temporal, Factual, Multi-Hop, Conversational, Update
- **Adaptive Retrieval** — Strategy selection per query type (event log, KG, hybrid BM25+vector+temporal)
- **Entropy Gating** — Decides extraction to KG based on text entropy + embedding novelty
- **Logical Invalidation** — `valid_until` timestamps instead of hard deletes
- **Cross-Language Consistency** — Identical classification & routing in Rust and Python
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

## Testing

```bash
# All tests
python tests/run_all_tests.py

# Python only
python -m pytest tests/python_unit_tests.py tests/python_router_tests.py tests/edge_case_tests.py tests/surreal_integration_tests.py -v

# Rust only
cargo test --workspace
```

Current status: **74 tests passing** (67 Python + 7 Rust across 5 test files)

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

This project is licensed under the terms specified in the repository.