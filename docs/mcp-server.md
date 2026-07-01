# MCP Server — STRATA Memory Stack

Der MCP-Server exponiert den gesamten STRATA Memory Stack als standardisierte Tool-Schnittstelle über das [Model Context Protocol (MCP)](https://modelcontextprotocol.io). Jeder MCP-kompatible Client (z.B. Claude Desktop, Cursor, VS Code mit MCP-Extension) kann die 10 Memory-Tools direkt aufrufen — ohne den Stack selbst hosten zu müssen.

## Architektur-Überblick

```
┌─────────────────────────────────────────────────────────────┐
│                    MCP Client (Claude, Cursor, …)            │
│                     ↔  stdio / SSE / HTTP                    │
├─────────────────────────────────────────────────────────────┤
│                    MCP Server (FastMCP)                       │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌────────┐│
│  │   Core      │ │ Primitives  │ │Introspection│ │Maint.  ││
│  │  (3 Tools)  │ │  (3 Tools)  │ │  (2 Tools)  │ │(2 Tools││
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └───┬────┘│
│         │               │               │            │      │
│         ▼               ▼               ▼            ▼      │
│  ┌────────────────────────────────────────────────────────┐ │
│  │              STRATA Memory Stack (Python)                │ │
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

## Voraussetzungen

| Komponente | Version | Zweck |
|------------|---------|-------|
| Python | 3.10+ | Runtime |
| Docker + Docker Compose | aktuell | SurrealDB Container |
| SurrealDB | latest | Speicher (Event Log, KG, Vektor-Index) |
| CUDA-fähige GPU | optional | Embedding-Modell (`nomic-ai/nomic-embed-text-v1.5`) |

Python-Pakete (aus `requirements.txt`):

```
requests>=2.28.0
numpy>=1.21.0
fastapi>=0.100.0
uvicorn>=0.20.0
surrealdb>=1.0.0
python-dotenv>=1.0.0
```

Zusätzlich benötigt (muss separat installiert werden):

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

## Schnellstart

### 1. SurrealDB starten

```bash
cd sdb
docker-compose up -d
```

Prüfen, dass der Container läuft:

```bash
docker ps --filter "name=strata-surrealdb"
```

SurrealDB ist dann erreichbar unter `http://127.0.0.1:8000` mit Credentials `root` / `root`.

### 2. Schema und Helper-Funktionen laden

```bash
cd ..
python scripts/load_schema_optimized.py
```

Das Skript lädt nacheinander:
- `docs/schema.surql` — Tabellen, Felder, Indizes, Fulltext-Analyzer, Vektor-Index
- `docs/helper_functions.surql` — DB-seitige Funktionen (`fn::active_fact`, `fn::facts_at`, …)
- `docs/test_data.surql` — Beispiel-Daten (Alice, Acme Corp, …)

**Wichtig:** Der Loader prependet automatisch `USE NS strata DB strata;` an jeden Batch und entfernt Inline-Kommentare, damit SurrealDB 3 die Statements korrekt ausführt.

### 3. MCP Server starten

#### Als stdio-Server (für MCP-Clients)

```bash
python -m src.mcp.server
```

Der Server spricht MCP über stdin/stdout. Die meisten MCP-Clients starten den Server als Subprozess und kommunizieren per JSON-RPC darüber.

#### Konfiguration in Claude Desktop (Beispiel)

```json
{
  "mcpServers": {
    "STRATA-memory": {
      "command": "python",
      "args": ["-m", "src.mcp.server"],
      "cwd": "C:\\Users\\tobs\\.cursor\\workspace\\STRATA",
      "env": {
        "PYTHONPATH": "C:\\Users\\tobs\\.cursor\\workspace\\STRATA"
      }
    }
  }
}
```

#### Konfiguration in Cursor / VS Code

In `.cursor/mcp.json` oder über die VS Code MCP-Extension:

```json
{
  "servers": {
    "STRATA-memory": {
      "type": "stdio",
      "command": "python",
      "args": ["-m", "src.mcp.server"],
      "cwd": "${workspaceFolder}"
    }
  }
}
```

## Die 10 Tools (4 Schichten)

### Schicht 1 — Core Memory Operations

Die drei Tools, die ein STRATA im täglichen Betrieb 90 % der Zeit braucht.

#### `memory_store`

Speichert ein Event im immutable Raw Event Log. Das Entropy-Gate entscheidet später, ob der Inhalt zusätzlich in den Knowledge Graph extrahiert wird.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `content` | `string` | ja | Roher Inhalt |
| `source` | `string` | nein | Quelle (Standard: `"user_input"`) |
| `metadata` | `object` | nein | Zusätzliche Metadaten |

**Rückgabe:**

```json
{
  "event_id": "event:abc123…",
  "status": "stored",
  "source": "user_input"
}
```

**Beispiel:**

```json
{
  "content": "Alice arbeitet bei Acme Corp in Berlin.",
  "source": "user_input"
}
```

Das Embedding wird automatisch über `nomic-ai/nomic-embed-text-v1.5` (768 Dimensionen) erzeugt und als `vector(f32, 768)` in SurrealDB gespeichert.

---

#### `memory_query`

Der High-Level-Einstieg. Klassifiziert die Query, wählt eine Retrieval-Strategie und gibt strukturierte Ergebnisse zurück.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `query` | `string` | ja | Natürlichsprachige Query |
| `cost_budget` | `string` | nein | `"auto"` (Standard), `"low"`, `"medium"`, `"high"` |

**Rückgabe:**

```json
{
  "query": "Wo arbeitet Alice?",
  "classified_as": "factual",
  "confidence": 1.0,
  "strategy": "knowledge_graph_first",
  "cost_budget": "low",
  "results": {
    "entities": [{"id": "entity:alice", "name": "Alice", "type": "person"}],
    "facts": [{"id": "fact:xyz", "predicate": "works_at", …}],
    "events": []
  },
  "total": 2
}
```

**Mögliche Klassifizierungen:**
- `temporal` — Zeitbezogene Query (wann, seit, bis)
- `factual` — Faktenabfrage (wer, was, wo)
- `multi-hop` — Mehrstufige Schlussfolgerung (und, warum, Beziehung)
- `conversational` — Konversationsbezug (erinnerst du dich, haben wir gesprochen)
- `update` — Aktualisierungsanweisung

---

#### `memory_update`

Aktualisiert einen Fact im Knowledge Graph durch logische Invalidation. Der alte Fact bekommt `valid_until` gesetzt, ein neuer Fact wird angelegt. Der Raw Event Log bleibt immutable.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `subject` | `string` | ja | Subjekt-Entität |
| `predicate` | `string` | ja | Prädikat (z.B. `"works_at"`) |
| `new_value` | `string` | ja | Neuer Objekt-Wert |

**Rückgabe:**

```json
{
  "invalidated_fact": "fact:old123",
  "new_fact": "fact:new456",
  "subject": "Alice",
  "predicate": "works_at",
  "new_value": "Beta Inc"
}
```

**Hinweis:** Subjekt und Objekt müssen bereits als Entitäten existieren. Eine automatische Entity-Erstellung findet nicht statt.

---

### Schicht 2 — Retrieval Primitives

Für STRATAs, die mehr Kontrolle über den Retrieval-Prozess brauchen und den Router umgehen wollen.

#### `event_log_search`

Hybride Suche im Event Log (BM25 über FTX-Index + Vector über HNSW + RRF-Fusion) ohne Router. Führt FTX-Query parallel zur Embedding-Berechnung aus; Query-Embeddings werden gecached.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `query` | `string` | ja | Suchbegriff |
| `since` | `string` | nein | ISO-Timestamp (untere Grenze) |
| `until` | `string` | nein | ISO-Timestamp (obere Grenze) |
| `limit` | `int` | nein | Max. Ergebnisse (Standard: 10) |

**Rückgabe:**

```json
{
  "events": [
    {
      "id": "event:abc",
      "content": "Alice arbeitet bei Acme Corp…",
      "timestamp": "2026-06-29T15:00:00Z",
      "source": "user_input",
      "search_type": "lexical",
      "metadata": null
    },
    {
      "id": "event:def",
      "content": "Bob arbeitet bei Beta Inc…",
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

Direkte Graph-Traversal-Abfrage auf dem temporalen Knowledge Graph.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `subject` | `string` | nein | Filter nach Subjekt-Name (Teilstring) |
| `predicate` | `string` | nein | Exaktes Prädikat |
| `at_time` | `string` | nein | ISO-Timestamp — gibt nur Facts zurück, die zu diesem Zeitpunkt gültig waren |

**Rückgabe:**

```json
{
  "facts": [
    {
      "id": "fact:xyz",
      "predicate": "works_at",
      "valid_from": "2026-06-20T00:00:00Z",
      "valid_until": null,
      "in": {"id": "entity:alice", "name": "Alice"},
      "out": {"id": "entity:acme", "name": "Acme Corp"}
    }
  ],
  "entities": [
    {"id": "entity:alice", "name": "Alice", "type": "person"},
    {"id": "entity:acme", "name": "Acme Corp", "type": "organization"}
  ],
  "count": 1
}
```

---

#### `semantic_search`

Reiner Vektor-Suchpfad ohne Knowledge Graph. Nutzt SurrealDBs `vector::similarity::cosine()` direkt in SQL.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `query` | `string` | ja | Suchbegriff |
| `top_k` | `int` | nein | Anzahl Top-Ergebnisse (Standard: 5) |

**Rückgabe:**

```json
{
  "events": [
    {
      "id": "event:abc",
      "content": "Alice arbeitet bei Acme Corp…",
      "score": 0.87
    }
  ],
  "count": 3
}
```

**Hinweis:** Die Cosine Similarity wird serverseitig in SurrealDB berechnet (`ORDER BY vector::similarity::cosine(embedding, $query_vec) DESC`). Das Embedding-Feld wird in der Antwort automatisch entfernt.

---

### Schicht 3 — Introspection Tools

Für Debugging, Meta-Reasoning und Eval-Setup. Machen den Stack von anderen Systemen unterscheidbar.

#### `memory_stats`

Gibt Statistiken über den aktuellen Zustand des Memory Systems zurück.

**Keine Parameter.**

**Rückgabe:**

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

| Feld | Beschreibung |
|------|-------------|
| `event_count` | Gesamtzahl Events im Raw Log |
| `entity_count` | Gesamtzahl Entitäten im KG |
| `fact_count` | Anzahl aktiver (nicht invaliderter) Facts |
| `oldest_event` | Timestamp des ältesten Events |
| `newest_event` | Timestamp des neuesten Events |
| `gate_pass_rate` | Anteil `"extract"`-Entscheidungen an allen Gate-Entscheidungen (global) |

---

#### `explain_routing`

Erklärt, warum der Router eine bestimmte Retrieval-Strategie für eine Query gewählt hat. Gold wert für Eval-Setup und Debugging.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `query` | `string` | ja | Die zu klassifizierende Query |

**Rückgabe:**

```json
{
  "query": "Wo arbeitet Alice?",
  "classified_as": "factual",
  "confidence": 1.0,
  "strategy_selected": "knowledge_graph_first",
  "cost_budget": "low",
  "policy_applied": "strict",
  "reason": "Query asks for specific facts (who, what, where). Knowledge graph prioritized."
}
```

---

### Schicht 4 — Maintenance Tools

Weniger häufig gebraucht, aber essenziell für Langzeitbetrieb.

#### `memory_forget`

Soft-Delete: Markiert ein Event oder eine Entität als vergessen, ohne den Raw Log zu verändern. Retrieval filtert automatisch `WHERE forgotten != true`.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `event_id` | `string` | nein* | SurrealDB Event-ID (z.B. `"event:abc123"`) |
| `entity` | `string` | nein* | Entitätsname (Teilstring-Suche) |
| `reason` | `string` | nein | Grund für das Vergessen |

\* Genau einer von `event_id` oder `entity` muss angegeben werden.

**Rückgabe:**

```json
{
  "forgotten_id": "entity:alice",
  "type": "entity",
  "reason": "User requested deletion"
}
```

**Intern:** `UPDATE <id> SET forgotten = true`

---

#### `memory_consolidate`

Manueller Trigger für Conservative Maintenance — nur lokale Patches, kein globales Reindexieren.

| Parameter | Typ | Pflicht | Beschreibung |
|-----------|-----|---------|-------------|
| `scope` | `string` | ja | `"local"` (Standard) oder `"entity"` |
| `entity` | `string` | nein | Entitätsname (nur bei `scope="entity"`) |

**Rückgabe — `scope="local"`:**

```json
{
  "scope": "local",
  "stale_facts_found": 26,
  "status": "reviewed"
}
```

**Rückgabe — `scope="entity"`:**

```json
{
  "scope": "entity",
  "entity": "Alice",
  "entity_id": "entity:alice",
  "status": "consolidated"
}
```

---

## Tool-Referenz (kompakt)

| Tool | Schicht | Eingabe | Ausgabe |
|------|---------|---------|---------|
| `memory_store` | Core | `content`, `source?`, `metadata?` | `event_id`, `status` |
| `memory_query` | Core | `query`, `cost_budget?` | `classified_as`, `strategy`, `results` |
| `memory_update` | Core | `subject`, `predicate`, `new_value` | `invalidated_fact`, `new_fact` |
| `event_log_search` | Primitives | `query`, `since?`, `until?`, `limit?` | `events[]`, `count` |
| `kg_query` | Primitives | `subject?`, `predicate?`, `at_time?` | `facts[]`, `entities[]`, `count` |
| `semantic_search` | Primitives | `query`, `top_k?` | `events[]`, `count` |
| `memory_stats` | Introspection | — | `event_count`, `entity_count`, `fact_count`, `gate_pass_rate`, … |
| `explain_routing` | Introspection | `query` | `classified_as`, `strategy_selected`, `reason` |
| `memory_forget` | Maintenance | `event_id?` oder `entity?`, `reason?` | `forgotten_id`, `type` |
| `memory_consolidate` | Maintenance | `scope`, `entity?` | `stale_facts_found` oder `status` |

---

## Server-Implementierung

Datei: `src/mcp/server.py`

### Technische Details

**Framework:** [FastMCP](https://github.com/jlowin/fastmcp) — offizielle Python-Implementierung des Model Context Protocol SDKs von Anthropic.

**SurrealDB-Zugriff:** Direkt über HTTP-SQL-Endpoint (`http://127.0.0.1:8000/sql`) mit Basic Auth (`root`/`root`). Jeder SQL-Batch wird mit `USE NS strata DB strata;` prefixiert, damit Statements nicht ins Leere laufen.

**Embedding-Service:** `SentenceTransformer` mit Modell `nomic-ai/nomic-embed-text-v1.5` (768 Dimensionen, CUDA wenn verfügbar). Das Modell wird lazy geladen und gecacht.

**Antwort-Extraktion:** `_extract_result()` parst SurrealDBs mehrzeilige JSON-Responses (Liste von `{status, result}`-Objekten) und gibt den relevanten `result`-Teil zurück — robust gegen Multi-Statement-Batches und Metadaten-Items (`database`, `namespace`).

### Wichtige Hilfsfunktionen

| Funktion | Zweck |
|----------|-------|
| `_query_surreal(sql)` | Führt SQL gegen SurrealDB aus, raise bei `ERR` |
| `_extract_result(data, index)` | Extrahiert `result` aus SurrealDB-Response |

---

## Integration mit anderen Komponenten

### Mit dem Query Classifier kombinieren

```python
from src.extraction.classifier import QueryClassifier
from src.router.policy import RoutingPolicy
from src.planner.executor import RetrievalExecutor

classifier = QueryClassifier()
q_type, confidence = classifier.classify("Wo arbeitet Alice?")

policy = RoutingPolicy()
strategy = policy.get_strategy(q_type, confidence, "auto")

executor = RetrievalExecutor()
results = asyncio.run(executor.execute_strategy(strategy, "Wo arbeitet Alice?"))
```

### Mit dem Entropy Gate kombinieren

```python
from src.extraction.entropy_gate import EntropyGate

gate = EntropyGate()
decision = gate.should_extract("Alice arbeitet bei Acme Corp.")
# → {"decision": "extract", "text_score": 0.4, "novelty": 0.8, "gate_score": 0.65}
```

### Mit dem Conservative Maintainer kombinieren

```python
from src.maintenance.conservative_maintainer import ConservativeMaintainer

maintainer = ConservativeMaintainer(debounce_seconds=3600)
stale_facts = maintainer.get_stale_facts(max_age_seconds=86400)
maintainer.flush_pending()
```

---

## Troubleshooting

### `RuntimeError: SurrealDB Error: Specify a namespace to use`

Das Schema wurde nicht geladen oder der `USE NS/DB`-Prefix fehlt. Prüfe:

```bash
python scripts/load_schema_optimized.py
```

### `ConnectionError` beim Start des MCP-Servers

SurrealDB-Container läuft nicht oder Port 8000 ist blockiert:

```bash
docker ps --filter "name=strata-surrealdb"
# Falls nicht running:
cd sdb && docker-compose up -d
```

### Embedding-Modell lädt extrem langsam

Beim ersten Start wird `nomic-ai/nomic-embed-text-v1.5` von HuggingFace heruntergeladen (~500 MB). Danach liegt es im Cache unter `~/.cache/huggingface/hub/`. Für Offline-Betrieb vorher herunterladen:

```python
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("nomic-ai/nomic-embed-text-v1.5")
model.save("./models/nomic-embed-text-v1.5")
```

### `datetime.utcnow()` Deprecation Warning

Bereits behoben im Server (`datetime.now(timezone.utc)`). Falls andere Skripte noch `utcnow()` verwenden, ersetzen durch `datetime.now(timezone.utc)`.

### SurrealDB 3 Vektor-Index wird nicht genutzt

SurrealDBs `MTREE`-Vektor-Index ist in dieser Umgebung konfiguriert, aber der MCP-Server berechnet Cosine Similarity in Python, weil die SQL-Syntax für Vektor-Distanzen in SurrealDB 3 noch instabil ist. Für produktive Workloads mit >10.000 Events sollte auf den SurrealDB-internen Vektor-Index migriert werden, sobald die Syntax stabil ist.

---

## Bekannte Einschränkungen

1. **SurrealDB läuft in-memory:** `docker-compose down` löscht alle Daten. Für Persistenz das Volume `surrealdb-data` in `docker-compose.yml` aktivieren (bereits konfiguriert).
2. **Embedding-Berechnung bei `memory_store`:** Das Modell wird bei jedem Store-Call geladen (lazy). Bei hohem Throughput sollte ein persistent geladener Service verwendet werden.
3. **`semantic_search` lädt alle Embeddings in Python:** Funktioniert bis ~50k Events. Für größere Datenmengen auf SurrealDB Vektor-Suche migrieren.
4. **Kein Auth-Layer im MCP-Server:** Der MCP-Server selbst hat keine Authentifizierung. Zugriffskontrolle muss auf Transport-Ebene (z.B. lokaler Socket) oder im Client konfiguriert werden.
5. **`memory_update` erstellt keine Entitäten automatisch:** Subjekt und Objekt müssen bereits als Entitäten existieren, sonst schlägt die Operation fehl.

---

## Verwandte Dateien

| Datei | Zweck |
|-------|-------|
| `src/mcp/server.py` | MCP-Server-Implementierung (10 Tools) |
| `docs/schema.surql` | SurrealDB-Schema (Event Log, KG, Indizes) |
| `docs/helper_functions.surql` | DB-seitige Funktionen |
| `docs/test_data.surql` | Beispiel-Testdaten |
| `docker-compose.yml` | SurrealDB Container-Setup |
| `scripts/load_schema_optimized.py` | Schema-Loader mit USE-Prefix |
| `src/extraction/classifier.py` | Query-Klassifizierung (5 Typen) |
| `src/router/policy.py` | Routing-Strategien |
| `src/planner/executor.py` | Retrieval-Ausführung |
| `src/maintenance/conservative_maintainer.py` | Maintenance-Engine |
| `src/extraction/embedding_service.py` | Embedding-Service (SentenceTransformers) |

---

## Nächste Schritte

- [ ] MCP-Server als persistenten Service aufsetzen (systemd / Docker)
- [ ] SSE-Transport zusätzlich zu stdio implementieren (für Remote-Clients)
- [ ] Auth-Layer für MCP-Server hinzufügen
- [ ] Batch-`memory_store` für Bulk-Ingestion
- [ ] Streaming-Responses für große Retrieval-Ergebnisse
- [ ] Eval-Harness an MCP-Tools anbinden
- [ ] Automatische Entity-Erstellung in `memory_update`