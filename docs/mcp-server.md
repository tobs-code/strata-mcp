# MCP Server — Sieveon Memory Stack

The MCP Server exposes the entire Sieveon Memory Stack via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io) — 15 memory tools and 3 MCP resources. Any MCP-compatible client (e.g. Claude Desktop, Cursor, VS Code with MCP extension) can call them directly, without hosting the stack itself.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Client (Claude, Cursor, …)            │
│                     ↔  stdio / SSE / HTTP                    │
├─────────────────────────────────────────────────────────────┤
│                    MCP Server (FastMCP)                       │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌──────────┐│
│  │   Core      │ │ Primitives  │ │Introspection│ │  Maint.  ││
│  │  (4 Tools)  │ │  (5 Tools)  │ │  (2 Tools)  │ │(4 Tools) ││
│  │             │ │             │ │             │ │          ││
│  │  Resources  │ │  Resources  │ │  Resources  │ │          ││
│  │ (3: entity, │ │ (entity/{id}│ │  (stats)    │ │          ││
│  │  event,     │ │  event/{id} │ │             │ │          ││
│  │  stats)     │ │  stats)     │ │             │ │          ││
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └───┬────┘│
│         │               │               │            │      │
│         ▼               ▼               ▼            ▼      │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              Sieveon Memory Stack (Python)                │ │
│  │  Classifier → Router → Planner → RetrievalExecutor     │ │
│  │  EntropyGate → EmbeddingService → ConservativeMaint.   │ │
│  └──────────────────────────┬─────────────────────────────┘ │
│                             │  HTTP SQL                     │
│                             ▼                               │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              SurrealDB 3 (Docker)                       │ │
│  │  Immutable Event Log  |  Temporal KG  |  Vector Index  │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.10+ | Runtime |
| Docker + Docker Compose | latest | SurrealDB container |
| SurrealDB | latest | Storage (Event Log, KG, Vector Index) |
| CUDA-capable GPU | optional | Embedding model (`sentence-transformers/all-MiniLM-L6-v2`) |

Python packages (from `requirements.txt`):

```
requests>=2.28.0
numpy>=1.21.0
fastapi>=0.100.0
uvicorn>=0.20.0
httpx>=0.24.0
python-dotenv>=1.0.0
```

Additional dependencies (must be installed separately):

```
sentence-transformers>=2.2.0
scikit-learn>=1.3.0
mcp[fastmcp]>=1.0.0       # FastMCP SDK
```

Installation:

```bash
pip install -r requirements.txt
pip install sentence-transformers scikit-learn mcp[fastmcp]
```

## Quick Start

### 1. Start SurrealDB

```bash
docker-compose up -d
```

Verify the container is running:

```bash
docker ps --filter "name=Sieveon-surrealdb"
```

SurrealDB is then reachable at `http://127.0.0.1:8000` with credentials `root` / `root`.

### 2. Load Schema and Helper Functions

```bash
python scripts/load_schema_optimized.py
```

The script loads in order:
- `docs/schema.surql` — tables, fields, indexes, fulltext analyzer, vector index
- `docs/helper_functions.surql` — DB-side functions (`fn::active_fact`, `fn::facts_at`, …)
- `docs/test_data.surql` — sample data (Alice, Acme Corp, …)

**Important:** The loader automatically prepends `USE NS Sieveon DB Sieveon;` to each batch and strips inline comments so SurrealDB 3 executes statements correctly.

### 3. Start MCP Server

#### As stdio Server (for MCP clients)

```bash
python -m src.mcp.server
```

The server speaks MCP over stdin/stdout. Most MCP clients start the server as a subprocess and communicate via JSON-RPC.

#### Configuration in Claude Desktop (Example)

```json
{
  "mcpServers": {
    "Sieveon-memory": {
      "command": "python",
      "args": ["-m", "src.mcp.server"],
      "cwd": "C:\\workspace\\Sieveon",
      "env": {
        "PYTHONPATH": "C:\\workspace\\Sieveon"
      }
    }
  }
}
```

#### Configuration in Cursor / VS Code

In `.cursor/mcp.json` or via the VS Code MCP extension:

```json
{
  "servers": {
    "Sieveon-memory": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "src.mcp.server"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

## The 16 Tools (4 Layers) + MCP Resources

### Layer 1 — Core Memory Operations

The four core tools an Sieveon needs 90% of the time in daily operation.

#### `memory_store`

Stores an event in the immutable Raw Event Log. The Entropy Gate later decides whether the content is additionally extracted into the Knowledge Graph.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `content` | `string` | yes | Raw content |
| `source` | `string` | no | Source (default: `"user_input"`) |
| `metadata` | `object` | no | Additional metadata |

**Returns:**

The `gate` object shows the Entropy Gate decision:

- `"decision": "extract"` → extracted into KG (includes `kg: {entities_created, facts_created}`)
- `"decision": "skip"` → rejected by guardrail (`"reason"`: `"text_too_short"`, `"text_too_long"`, `"too_repetitive"`, or `"near_duplicate"`)
- `"decision": "ignore"` → passed guardrails but composite score below threshold (`"composite_score"`, `"threshold"`)

```json
{
  "event_id": "event:abc123…",
  "status": "stored",
  "source": "user_input",
  "gate": {
    "decision": "extract",
    "kg": {"entities_created": 3, "facts_created": 5}
  }
}
```

When skipped or ignored:
```json
{
  "event_id": "event:abc123…",
  "status": "stored",
  "source": "user_input",
  "gate": {
    "decision": "skip",
    "reason": "too_repetitive",
    "composite_score": null,
    "threshold": 0.3
  }
}
```

**Example:**

```json
{
  "content": "Alice works at Acme Corp in Berlin.",
  "source": "user_input"
}
```

The embedding is automatically generated via `nomic-ai/nomic-embed-text-v1.5` (768 dimensions) and stored as `vector(f32, 768)` in SurrealDB.

---

#### `memory_store_batch`

Stores multiple events in batch. Each item goes through the Entropy Gate independently.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `items` | `array` | yes | List of objects with `content`, optional `source` and `metadata` |
| `source` | `string` | no | Default source for all items (`"user_input"`) |

**Returns:**

```json
{
  "results": [
    {
      "index": 0,
      "event_id": "event:...",
      "status": "stored",
      "source": "user_input",
      "gate_decision": "extract",
      "entities_created": 3,
      "facts_created": 5
    },
    {
      "index": 1,
      "event_id": "event:...",
      "status": "stored",
      "source": "user_input",
      "gate_decision": "skip",
      "gate_reason": "text_too_short"
    }
  ],
  "errors": [],
  "stored": 2,
  "failed": 0,
  "gate_summary": {"extract": 1, "ignore": 0, "skip": 1}
}
```

---

#### `memory_query`

The high-level entry point. Classifies the query, selects a retrieval strategy, and returns structured results.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | `string` | yes | Natural language query |
| `cost_budget` | `string` | no | `"auto"` (default), `"low"`, `"medium"`, `"high"` |
| `limit` | `int` | no | Max results per category (default: 10) |

**Returns:**

```json
{
  "query": "Where does Alice work?",
  "classified_as": "factual",
  "confidence": 1.0,
  "strategy": "knowledge_graph_first",
  "cost_budget": "low",
  "results": {
    "entities": [{"id": "entity:alice", "name": "Alice", "type": "person"}],
    "facts": [{"id": "fact:xyz", "predicate": "works_at", …}],
    "events": []
  },
  "total": 2,
  "summary": {
    "found": true,
    "answer": "Alice works_at Acme Corp. Related entities: Alice (person). Best match: \"Alice works at Acme Corp…\" (source: user_input, hits: 2)",
    "total_facts": 1,
    "total_entities": 1,
    "total_events": 0
  }
}
```

**Possible classifications:**
- `temporal` — Time-related query (when, since, until)
- `factual` — Fact query (who, what, where)
- `multi-hop` — Multi-step reasoning (and, why, relationship)
- `conversational` — Conversation context (remember, talked about)
- `update` — Update instruction

---

#### `memory_update`

Updates a fact in the Knowledge Graph through logical invalidation. The old fact gets `valid_until` set, a new fact is created. The Raw Event Log remains immutable.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `subject` | `string` | yes | Subject entity |
| `predicate` | `string` | yes | Predicate (e.g. `"works_at"`) |
| `new_value` | `string` | yes | New object value |

**Returns:**

```json
{
  "invalidated_fact": "fact:old123",
  "new_fact": "fact:new456",
  "subject": "Alice",
  "predicate": "works_at",
  "new_value": "Beta Inc"
}
```

**Note:** If the target entity does not exist yet, it will be created automatically with an inferred type.

---

### Layer 2 — Retrieval Primitives

Five tools for direct access to events, entities, and facts — bypasses the router.

#### `event_log_search`

Hybrid search in the Event Log (BM25 via FTX index + Vector via HNSW + RRF fusion) without router. Runs FTX query in parallel with embedding computation; query embeddings are cached.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | `string` | yes | Search term |
| `since` | `string` | no | ISO timestamp (lower bound) |
| `until` | `string` | no | ISO timestamp (upper bound) |
| `limit` | `int` | no | Max results (default: 10) |
| `offset` | `int` | no | Pagination offset (default: 0) |
| `include_forgotten` | `bool` | no | Include forgotten events (default: false) |

**Returns:**

```json
{
  "events": [
    {
      "id": "event:abc",
      "content": "Alice works at Acme Corp…",
      "timestamp": "2026-06-29T15:00:00Z",
      "source": "user_input",
      "search_type": "lexical",
      "metadata": null
    },
    {
      "id": "event:def",
      "content": "Bob works at Beta Inc…",
      "timestamp": "2026-06-28T12:00:00Z",
      "source": "user_input",
      "search_type": "vector",
      "vec_score": 0.72,
      "metadata": null
    }
  ],
  "count": 2
}
```

---

#### `kg_query`

Direct graph traversal query on the temporal Knowledge Graph.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `subject` | `string` | no | Filter by subject name (`in.name`) |
| `object` | `string` | no | Filter by object name (`out.name`) |
| `predicate` | `string` | no | Exact predicate |
| `at_time` | `string` | no | ISO timestamp — returns only facts valid at that time |
| `limit` | `int` | no | Max facts to return (default: 100) |
| `offset` | `int` | no | Pagination offset (default: 0) |

**Returns:**

```json
{
  "facts": [
    {
      "id": "fact:xyz",
      "predicate": "works_at",
      "valid_from": "2026-06-20T00:00:00Z",
      "valid_until": null,
      "in": {"id": "entity:alice", "name": "Alice", "type": "person"},
      "out": {"id": "entity:acme", "name": "Acme Corp", "type": "organization"}
    }
  ],
  "count": 1,
  "query_params": {
    "subject": "Alice",
    "object": null,
    "predicate": "works_at",
    "at_time": null,
    "limit": 100,
    "offset": 0
  }
}
```

---

#### `graph_traverse`

Multi-hop graph traversal: walks the knowledge graph starting from an entity,
following relationships up to `max_depth` hops. Uses BFS with cycle detection.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `start_entity` | `string` | yes | — | Entity name to start traversal from |
| `max_depth` | `int` | no | `2` | Max hops (1–5, clamped automatically) |
| `direction` | `string` | no | `"both"` | `"outbound"`, `"inbound"`, or `"both"` |
| `predicate` | `string` | no | `null` | Filter to a specific relationship type |
| `min_confidence` | `float` | no | `0.0` | Minimum confidence threshold (0.0–1.0) |

**Returns:**

```json
{
  "status": "ok",
  "start_entity": {"id": "entity:sieveon", "name": "Sieveon", "type": "technology"},
  "max_depth": 2,
  "direction": "both",
  "predicate_filter": null,
  "nodes": [
    {"id": "entity:postgres", "name": "PostgreSQL", "type": "technology"}
  ],
  "node_count": 5,
  "edges": [
    {
      "from_id": "entity:sieveon", "from_name": "Sieveon",
      "to_id": "entity:postgres", "to_name": "PostgreSQL",
      "predicate": "has_database", "confidence": 1.0, "depth": 1
    }
  ],
  "edge_count": 7,
  "path_count": 7,
  "paths": [
    [
      {"from": "Sieveon", "predicate": "has_database", "to": "PostgreSQL", "confidence": 1.0}
    ],
    [
      {"from": "Sieveon", "predicate": "co_occurs_with", "to": "MCP", "confidence": 0.63},
      {"from": "MCP", "predicate": "created", "to": "Python", "confidence": 0.68}
    ]
  ]
}
```

On error (entity not found):
```json
{"status": "error", "error": "Entity 'X' not found in knowledge graph", "paths": [], "path_count": 0}
```

---

#### `list_entities`

Lists entities in the knowledge graph with filtering and pagination.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | `int` | no | Max results (default: 20) |
| `offset` | `int` | no | Pagination offset (default: 0) |
| `type` | `string` | no | Filter by entity type (e.g. `"person"`) |
| `name_contains` | `string` | no | Case-insensitive substring filter on name |
| `sort_by` | `string` | no | Sort field: `"name"`, `"created_at"`, `"updated_at"` (default: `"name"`) |
| `sort_order` | `string` | no | `"asc"` or `"desc"` (default: `"asc"`) |

**Returns:**

```json
{
  "entities": [
    {"id": "entity:alice", "name": "Alice", "type": "person", "created_at": "2026-07-03T15:12:12Z", "updated_at": "2026-07-03T15:12:13Z"}
  ],
  "count": 1,
  "total": 63
}
```

---

#### `list_events`

Lists events from the raw event log with filtering and pagination.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | `int` | no | Max results (default: 20) |
| `offset` | `int` | no | Pagination offset (default: 0) |
| `since` | `string` | no | ISO timestamp (lower bound) |
| `until` | `string` | no | ISO timestamp (upper bound) |
| `source` | `string` | no | Filter by event source |
| `include_forgotten` | `bool` | no | Include forgotten events (default: false) |

**Returns:**

```json
{
  "events": [
    {
      "id": "event:abc",
      "content": "Alice works at Acme Corp…",
      "timestamp": "2026-06-29T15:00:00Z",
      "source": "user_input",
      "metadata": null,
      "forgotten": null,
      "forgotten_reason": null
    }
  ],
  "count": 5,
  "total": 34
}
```

---

#### `semantic_search`

Pure vector search path without Knowledge Graph. Uses SurrealDB's `vector::similarity::cosine()` directly in SQL.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | `string` | yes | Search term |
| `top_k` | `int` | no | Number of top results (default: 5) |

**Returns:**

```json
{
  "events": [
    {
      "id": "event:abc",
      "content": "Alice works at Acme Corp…",
      "score": 0.87
    }
  ],
  "count": 3
}
```

**Note:** Cosine similarity is computed server-side in SurrealDB (`ORDER BY vector::similarity::cosine(embedding, $query_vec) DESC`). The embedding field is automatically removed from the response.

---

### Layer 3 — Introspection Tools

For debugging, meta-reasoning, and evaluation setup. These distinguish the stack from other systems.

#### `memory_stats`

Returns statistics about the current state of the Memory System.

**No parameters.**

**Returns:**

```json
{
  "event_count": 42,
  "entity_count": 12,
  "fact_count": 8,
  "oldest_event": "2026-06-01T10:00:00Z",
  "newest_event": "2026-06-29T17:53:51Z",
  "gate_pass_rate": 0.73
}
```

| Field | Description |
|-------|-------------|
| `event_count` | Total events in the Raw Log |
| `entity_count` | Total entities in the KG |
| `fact_count` | Number of active (non-invalidated) facts |
| `oldest_event` | Timestamp of the oldest event |
| `newest_event` | Timestamp of the newest event |
| `gate_pass_rate` | Ratio of `"extract"` decisions to all gate decisions (global) |

---

#### `explain_routing`

Explains why the router chose a specific retrieval strategy for a query. Invaluable for evaluation setup and debugging.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `query` | `string` | yes | The query to classify |

**Returns:**

```json
{
  "query": "Where does Alice work?",
  "classified_as": "factual",
  "confidence": 1.0,
  "strategy_selected": "knowledge_graph_first",
  "cost_budget": "low",
  "policy_applied": "strict",
  "reason": "Query asks for specific facts (who, what, where). Knowledge graph prioritized."
}
```

---

### Layer 4 — Maintenance Tools

Used less frequently, but essential for long-term operation.

#### `memory_forget`

Soft-delete: Marks an event or entity as forgotten without altering the Raw Log. Retrieval automatically filters `WHERE forgotten != true`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_id` | `string` | no* | SurrealDB event ID (e.g. `"event:abc123"`) |
| `entity` | `string` | no* | Entity name (substring search) |
| `reason` | `string` | no | Reason for forgetting |

\* Exactly one of `event_id` or `entity` must be provided.

**Returns:**

```json
{
  "forgotten_items": [
    {"id": "event:abc123", "type": "event", "status": "forgotten"}
  ],
  "count": 1,
  "reason": "User requested deletion"
}
```

**Internally:** `UPDATE <id> SET forgotten = true`

---

#### `memory_unforget`

Restores a previously forgotten event. Resets `forgotten = false`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `event_id` | `string` | yes | Event ID to restore (e.g. `"event:abc123"`) |

**Returns:**

```json
{
  "status": "restored",
  "event_id": "event:abc123"
}
```

---

#### `memory_consolidate`

Manual trigger for Conservative Maintenance — local patches only, no global reindexing.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `scope` | `string` | yes | `"local"` (default) or `"entity"` |
| `entity` | `string` | no | Entity name (only when `scope="entity"`) |
| `delete_stale` | `bool` | no | Physically remove stale facts (default: false) |

**Return — `scope="local"`:**

```json
{
  "scope": "local",
  "stale_facts_found": 26,
  "deleted_count": 0,
  "status": "success"
}
```

**Return — `scope="entity"`:**

```json
{
  "scope": "entity",
  "entity": "Alice",
  "entity_id": "entity:alice",
  "status": "consolidated"
}
```

---

#### `memory_merge_entities`

Merges all active facts from one entity into another, then marks the source as forgotten. Useful for deduplicating entities like `"Python 3.12"` → `"Python"`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `source_entity` | `string` | yes | Source entity name (will be forgotten) |
| `target_entity` | `string` | yes | Target entity name (receives the facts) |
| `dry_run` | `bool` | no | Preview without making changes (default: false) |

**Returns:**

```json
{
  "status": "ok",
  "source_entity": "Python 3.12",
  "target_entity": "Python",
  "source_id": "entity:abc",
  "target_id": "entity:def",
  "merged_count": 1,
  "error_count": 0,
  "errors": null
}
```

Dry-run preview:

```json
{
  "status": "dry_run",
  "source_entity": "Python 3.12",
  "target_entity": "Python",
  "total_facts": 1,
  "preview": [
    {"fact_id": "fact:xyz", "predicate": "related_to", "role": "out", "other_entity": "FastAPI"}
  ]
}
```

---

## MCP Resources

In addition to tools, the server exposes **3 MCP resources** for reading entity and event details directly via `read_mcp_resource`.

### Static Resource

| URI | Description | Return |
|-----|-------------|--------|
| `sieveon://stats` | Memory system statistics | JSON: event/entity/fact counts, timestamps |

### Resource Templates

| URI Template | Description | Return |
|--------------|-------------|--------|
| `sieveon://entity/{entity_id}` | Entity details with active KG facts | JSON: full entity record + `facts[]` |
| `sieveon://event/{event_id}` | Single event details | JSON: event record |

**Usage example (via MCP protocol):**
```
read_resource("sieveon://entity/entity:alice")
→ { "id": "entity:alice", "name": "Alice", "type": "person",
    "facts": [ { "predicate": "works_at", "object": "Acme Corp", … } ] }
```

---

## Tool Reference (compact)

| Tool | Layer | Input | Output |
|------|-------|-------|--------|
| `memory_store` | Core | `content`, `source?`, `metadata?` | `event_id`, `status`, `gate` |
| `memory_store_batch` | Core | `items`, `source?` | `results[]`, `errors[]`, `stored`, `failed` |
| `memory_query` | Core | `query`, `cost_budget?`, `limit?` | `classified_as`, `strategy`, `results` |
| `memory_update` | Core | `subject`, `predicate`, `new_value` | `invalidated_fact`, `new_fact` |
| `event_log_search` | Primitives | `query`, `since?`, `until?`, `limit?`, `offset?`, `include_forgotten?` | `events[]`, `count` |
| `kg_query` | Primitives | `subject?`, `object?`, `predicate?`, `at_time?`, `limit?`, `offset?` | `facts[]`, `count`, `query_params` |
| `graph_traverse` | Primitives | `start_entity`, `max_depth?`, `direction?`, `predicate?`, `min_confidence?` | `paths[]`, `nodes[]`, `edges[]`, `node_count`, `edge_count`, `path_count` |
| `list_entities` | Primitives | `limit?`, `offset?`, `type?`, `name_contains?`, `sort_by?`, `sort_order?` | `entities[]`, `count`, `total` |
| `list_events` | Primitives | `limit?`, `offset?`, `since?`, `until?`, `source?`, `include_forgotten?` | `events[]`, `count`, `total` |
| `semantic_search` | Primitives | `query`, `top_k?` | `events[]`, `count` |
| `memory_stats` | Introspection | — | `event_count`, `entity_count`, `fact_count`, `gate_pass_rate`, … |
| `explain_routing` | Introspection | `query` | `classified_as`, `strategy_selected`, `reason` |
| `memory_forget` | Maintenance | `event_id?` or `entity?`, `reason?` | `forgotten_items[]`, `count`, `reason` |
| `memory_unforget` | Maintenance | `event_id` | `status`, `event_id` |
| `memory_consolidate` | Maintenance | `scope`, `entity?`, `delete_stale?` | `stale_facts_found`, `deleted_count`, `status` |
| `memory_merge_entities` | Maintenance | `source_entity`, `target_entity`, `dry_run?` | `status`, `merged_count`, `error_count` |

---

## Server Implementation

File: `src/mcp/server.py`

### Technical Details

**Framework:** [FastMCP](https://github.com/jlowin/fastmcp) — official Python implementation of Anthropic's Model Context Protocol SDK.

**SurrealDB Access:** Direct via HTTP SQL endpoint (`http://127.0.0.1:8000/sql`) with Basic Auth (`root`/`root`). Each SQL batch is prefixed with `USE NS Sieveon DB Sieveon;` to prevent statements from targeting an empty namespace.

**Embedding Service:** `SentenceTransformer` with model `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions, CUDA if available). The model is loaded lazily and cached. Query embeddings are LRU-cached with a capacity of 128 entries.

**Connection Reuse:** A module-level `httpx.AsyncClient` is shared across all SurrealDB calls, eliminating TCP handshake overhead on every query. Circuit breaker and retry with jittered backoff protect against transient failures.

**Response Extraction:** `_extract_result()` parses SurrealDB's multi-row JSON responses (list of `{status, result}` objects) and returns the relevant `result` portion — robust against multi-statement batches and metadata items (`database`, `namespace`).

### Key Helper Functions

| Function | Purpose |
|----------|---------|
| `_query_surreal(sql)` | Executes SQL against SurrealDB, raises on `ERR` |
| `_extract_result(data, index)` | Extracts `result` from SurrealDB response |
| `_extract_result_batch(data)` | Extracts multiple results from a multi-statement batch |
| `_get_client()` | Returns the shared `httpx.AsyncClient` singleton |
| `_embed_query(query)` | Cached query embedding (LRU, max 128) |
| `_clean_output(obj)` | Recursively removes `embedding` fields from output |

---

## Integration with Other Components

### With the Query Classifier

```python
from src.extraction.classifier import QueryClassifier
from src.router.policy import RoutingPolicy
from src.planner.executor import RetrievalExecutor

classifier = QueryClassifier()
q_type, confidence = classifier.classify("Where does Alice work?")

policy = RoutingPolicy()
strategy = policy.get_strategy(q_type, confidence, "auto")

executor = RetrievalExecutor()
results = asyncio.run(executor.execute_strategy(strategy, "Where does Alice work?"))
```

### With the Entropy Gate

```python
from src.extraction.entropy_gate import EntropyGate

gate = EntropyGate()
decision = gate.should_extract("Alice works at Acme Corp.")
# → {"decision": "extract", "text_score": 0.4, "novelty": 0.8, "gate_score": 0.65}
```

### With the Conservative Maintainer

```python
from src.maintenance.conservative_maintainer import ConservativeMaintainer

maintainer = ConservativeMaintainer(debounce_seconds=3600)
stale_facts = maintainer.get_stale_facts(max_age_seconds=86400)
maintainer.flush_pending()
```

---

## Troubleshooting

### `RuntimeError: SurrealDB Error: Specify a namespace to use`

The schema has not been loaded or the `USE NS/DB` prefix is missing. Check:

```bash
python scripts/load_schema_optimized.py
```

### `ConnectionError` when starting the MCP Server

SurrealDB container is not running or port 8000 is blocked:

```bash
docker ps --filter "name=Sieveon-surrealdb"
# If not running:
docker-compose up -d
```

### Embedding model loads extremely slowly

On first start, `sentence-transformers/all-MiniLM-L6-v2` is downloaded from HuggingFace (~90 MB). After that it is cached under `~/.cache/huggingface/hub/`. For offline operation, download beforehand:

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
model.save("./models/all-MiniLM-L6-v2")
```

### `datetime.utcnow()` Deprecation Warning

Already fixed in the server (`datetime.now(timezone.utc)`). If other scripts still use `utcnow()`, replace with `datetime.now(timezone.utc)`.

### HNSW vector index is not being used

The HNSW index `event_embedding_vec` is defined on the `embedding` field with `DIST COSINE`. The vector search query (`ORDER BY vector::similarity::cosine(embedding, $vec) DESC`) may trigger a full scan if the query planner determines the index is not cost-effective. For production workloads with >10,000 events, ensure SurrealDB is using the HNSW index by checking `INFO FOR TABLE event;`.

---

## Known Limitations

1. **SurrealDB runs in-memory:** `docker-compose down` deletes all data. For persistence, enable the `surrealdb-data` volume in `docker-compose.yml` (already configured).
2. **Embedding computation on `memory_store`:** The model is loaded on first call (lazy). For high throughput, a persistently loaded service should be used.
3. **No Auth Layer on the MCP Server:** The MCP server itself has no authentication. Access control must be configured at the transport level (e.g. local socket) or in the client.
4. **`event_log_search` is limited to 4× `limit` candidates per sub-search (FTX and vector):** For very large event logs, this may miss relevant results. Increase via `fn:` extensions if needed.

---

## Related Files

| File | Purpose |
|------|---------|
| `src/mcp/server.py` | MCP server implementation (15 tools, 3 resources) |
| `docs/schema.surql` | SurrealDB schema (Event Log, KG, indexes) |
| `docs/helper_functions.surql` | DB-side functions |
| `docs/test_data.surql` | Sample test data |
| `docker-compose.yml` | SurrealDB container setup |
| `scripts/load_schema_optimized.py` | Schema loader with USE prefix |
| `src/extraction/classifier.py` | Query classification (5 types) |
| `src/router/policy.py` | Routing strategies |
| `src/planner/executor.py` | Retrieval execution |
| `src/maintenance/conservative_maintainer.py` | Maintenance engine |
| `src/extraction/embedding_service.py` | Embedding service (SentenceTransformers) |

---

## Next Steps

- [ ] Set up MCP server as a persistent service (systemd / Docker)
- [ ] Implement SSE transport in addition to stdio (for remote clients)
- [ ] Add auth layer for MCP server
- [ ] Batch `memory_store` for bulk ingestion
- [ ] Streaming responses for large retrieval results
- [ ] Connect eval harness to MCP tools
