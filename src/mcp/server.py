# -*- coding: utf-8 -*-
"""
Control Plane Server implementing the Model Context Protocol (MCP)
Coordinates all components of the agent memory system
"""

import asyncio
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import httpx

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is not installed, skip loading .env file
    pass

# Standard imports
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# Our components
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import (
    FastMCP,  # This is the Model Context Protocol implementation
)

from src.extraction.classifier import QueryClassifier
from src.extraction.embedding_service import get_embedding_service
from src.extraction.entropy_gate import escape_surrealql
from src.extraction.entity_utils import infer_entity_type
from src.maintenance.conservative_maintainer import ConservativeMaintainer
from src.planner.executor import PlanExecutor, RetrievalExecutor
from src.router.policy import RoutingPolicy
from src.router.cost_awareness import CostTracker

# Shared CostTracker – wird von RoutingPolicy automatisch gefüttert
cost_tracker = CostTracker()

# Initialize FastMCP (Model Context Protocol) and FastAPI apps
mcp = FastMCP("strata")  # Model Context Protocol implementation

# FastAPI app
app = FastAPI(title="Strata Control Plane Server (MCP Implementation)", version="0.1.0")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SURREAL_URL = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
SURREAL_AUTH = (
    os.getenv("SURREALDB_USER", "root"),
    os.getenv("SURREALDB_PASS", "root"),
)
SURREAL_NS = os.getenv("SURREALDB_NS", "strata")  # Updated from agent_memory to strata
SURREAL_DB = os.getenv("SURREALDB_DB", "strata")  # Updated from agent_memory to strata


async def check_schema_exists() -> bool:
    """Check if the STRATA schema is already loaded in SurrealDB."""
    try:
        # Check if key tables exist - INFO FOR DB is more standard in SurrealDB 2.x+
        result = await _query_surreal("INFO FOR DB;")
        # Extract the result from the second item (index 1) since index 0 is the USE statement
        db_info = _extract_result(result, 1)

        if not db_info or not isinstance(db_info, list) or len(db_info) == 0:
            return False

        # INFO FOR DB returns a dictionary where keys are things like 'tables', 'functions', etc.
        # But _extract_result might have already wrapped it in a list.
        info_dict = db_info[0] if isinstance(db_info, list) else db_info

        if not isinstance(info_dict, dict) or "tables" not in info_dict:
            return False

        table_names = list(info_dict["tables"].keys())

        required_tables = ["event", "entity", "fact"]
        exists = all(table in table_names for table in required_tables)
        if exists:
            print(f"[DEBUG] Tables found: {table_names}")
        return exists
    except Exception as e:
        print(f"[WARN] Schema check failed: {e}")
        return False


def load_schema_file(file_path: str) -> List[str]:
    """Load and parse a .surql file, removing comments and splitting statements."""
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    clean_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--") or stripped.startswith("//"):
            continue
        if "--" in stripped:
            stripped = stripped.split("--")[0].strip()
        if stripped:
            clean_lines.append(stripped)
    content_no_comments = " ".join(clean_lines)

    statements = []
    current = ""
    depth = 0
    for part in content_no_comments.split(";"):
        part = part.strip()
        if not part:
            continue
        current += part + ";"
        depth += part.count("{") - part.count("}")
        if depth <= 0:
            statements.append(current.strip())
            current = ""
    if current.strip():
        statements.append(current.strip())

    # Add IF NOT EXISTS to table/index definitions to handle existing schema
    safe_statements = []
    for stmt in statements:
        if "DEFINE TABLE" in stmt and "IF NOT EXISTS" not in stmt:
            stmt = stmt.replace("DEFINE TABLE", "DEFINE TABLE IF NOT EXISTS")
        elif "DEFINE INDEX" in stmt and "IF NOT EXISTS" not in stmt:
            stmt = stmt.replace("DEFINE INDEX", "DEFINE INDEX IF NOT EXISTS")
        elif "DEFINE FUNCTION" in stmt and "IF NOT EXISTS" not in stmt:
            stmt = stmt.replace("DEFINE FUNCTION", "DEFINE FUNCTION IF NOT EXISTS")
        elif "DEFINE FIELD" in stmt and "IF NOT EXISTS" not in stmt:
            stmt = stmt.replace("DEFINE FIELD", "DEFINE FIELD IF NOT EXISTS")
        safe_statements.append(stmt)

    return safe_statements


async def ensure_schema_loaded():
    """Ensure the STRATA schema is loaded. If not, load it automatically."""
    if await check_schema_exists():
        print("[OK] STRATA schema already loaded")
        return

    print("[INFO] STRATA schema not found. Loading automatically...")

    # Use the existing load_schema_optimized.py script
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    load_script = os.path.join(project_root, "scripts", "load_schema_optimized.py")

    if os.path.exists(load_script):
        print(f"   Using existing load script: {load_script}")
        try:
            import subprocess

            # Fix: Added encoding and errors to avoid UnicodeDecodeError on Windows
            result = subprocess.run(
                ["python", load_script],
                cwd=project_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(f"   [WARN] Warnings: {result.stderr}")
            print("[OK] Schema loading complete!")
        except Exception as e:
            print(f"   [ERROR] Failed to run load script: {e}")
    else:
        print(f"   [WARN] Load script not found: {load_script}")
        print("   Skipping automatic schema load")


# Resilience state for SurrealDB connectivity
_surreal_failure_count = 0
_surreal_circuit_open = False
_surreal_last_failure = 0.0
_surreal_backoff_level = 0
_surreal_lock = asyncio.Lock()
_reconnect_task_started = False

# Shared HTTP client (reused across requests to avoid connection overhead)
_shared_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()

# Query embedding cache (LRU)
_embedding_cache: Dict[str, List[float]] = {}
_EMBEDDING_CACHE_MAX = 128

# Circuit-breaker thresholds (budget-aware)
_CIRCUIT_OPEN_THRESHOLD = 5  # failures before opening
_CIRCUIT_RESET_AFTER = 10.0  # seconds before half-open retry
_MAX_BACKOFF = 8.0  # cap jittered backoff
_RECONNECT_INTERVAL = 30.0  # background check every 30s


async def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        async with _client_lock:
            if _shared_client is None:
                _shared_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return _shared_client


def _get_cached_embedding(query: str) -> Optional[List[float]]:
    return _embedding_cache.get(query)


def _store_embedding_cache(query: str, vector: List[float]):
    if len(_embedding_cache) >= _EMBEDDING_CACHE_MAX:
        _embedding_cache.pop(next(iter(_embedding_cache)))
    _embedding_cache[query] = vector


async def _embed_query(query: str) -> List[float]:
    cached = _get_cached_embedding(query)
    if cached is not None:
        return cached
    service = get_embedding_service()
    vector = await asyncio.to_thread(service.embed_for_query, query)
    _store_embedding_cache(query, vector)
    return vector


async def _background_reconnect_task():
    """Background task to actively check connection and reset circuit breaker."""
    global _surreal_failure_count, _surreal_circuit_open, _surreal_backoff_level

    print("[INFO] Starting background SurrealDB reconnect task")
    while True:
        try:
            # Only probe if we've had failures or circuit is open
            should_probe = False
            async with _surreal_lock:
                if _surreal_circuit_open or _surreal_failure_count > 0:
                    should_probe = True

            if should_probe:
                # Lightweight probe: INFO FOR DB
                headers = {"Accept": "application/json", "Content-Type": "text/plain"}
                full_sql = f"USE NS {SURREAL_NS} DB {SURREAL_DB};\nINFO FOR DB;"

                client = await _get_client()
                response = await client.post(
                    SURREAL_URL,
                    content=full_sql,
                    headers=headers,
                    auth=SURREAL_AUTH,
                    timeout=httpx.Timeout(5.0),
                )

                if response.status_code < 400:
                    # Success! Reset everything
                    async with _surreal_lock:
                        if _surreal_circuit_open:
                            print(
                                "[INFO] SurrealDB connection restored. Closing circuit."
                            )
                        _surreal_failure_count = 0
                        _surreal_circuit_open = False
                        _surreal_backoff_level = 0
                        # Reset health factor to healthy
                        from src.router.policy import BudgetTracker

                        BudgetTracker.update_system_health(1.0)
                else:
                    # Still failing, update health factor based on failure count
                    from src.router.policy import BudgetTracker

                    async with _surreal_lock:
                        health = 1.0 - (min(_surreal_failure_count, 10) / 12.0)
                        BudgetTracker.update_system_health(health)

        except Exception:
            # Silent fail in background
            pass

        await asyncio.sleep(_RECONNECT_INTERVAL)


def _jittered_backoff(level: int) -> float:
    # Full jitter: uniform random in [0, min(cap, base * 2^level)]
    base = 0.5
    ceiling = min(_MAX_BACKOFF, base * (2**level))
    return random.uniform(0.0, ceiling)


def _budget_aware_should_retry(sql: str) -> bool:
    """Adaptive retry logic based on query complexity and system health."""
    from src.router.policy import BudgetTracker

    health = BudgetTracker._health_factor

    # Heavy queries get fewer retries
    heavy = sql.strip().upper().startswith(("RELATE", "DEFINE", "CREATE"))

    if health < 0.5:
        # System is struggling, be very conservative
        return 1 if heavy else 2

    return 2 if heavy else 3


async def _query_surreal(sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
    """Execute SurrealQL — with optional parameters (like prepared statements).
    
    When params is provided, uses JSON body with SurrealDB's parameterized query API:
        sql = "CREATE entity SET name = $name"
        params = {"name": "Tobias"}
    
    This avoids string injection and escaping issues.
    """
    global \
        _surreal_failure_count, \
        _surreal_circuit_open, \
        _surreal_last_failure, \
        _surreal_backoff_level, \
        _reconnect_task_started

    # Start background reconnect task if not already started
    if not _reconnect_task_started:
        async with _surreal_lock:
            if not _reconnect_task_started:
                asyncio.create_task(_background_reconnect_task())
                _reconnect_task_started = True

    if params:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body_dict: Dict[str, Any] = {"sql": sql}
        # Inject namespace + db via params (SurrealDB 2.x supports $ns, $db)
        body_dict["params"] = dict(params)
        body = json.dumps(body_dict)
        full_sql = sql  # USE NS/DB can be omitted when using params with ns/db
    else:
        headers = {
            "Accept": "application/json",
            "Content-Type": "text/plain",
        }
        body = f"USE NS {SURREAL_NS} DB {SURREAL_DB};\n{sql}"
        full_sql = body

    # Read circuit state without holding lock while query runs
    async with _surreal_lock:
        circuit_open = _surreal_circuit_open
        failure_count = _surreal_failure_count
        last_failure = _surreal_last_failure

    if circuit_open:
        # Half-open probe after quiet period
        if (time.time() - last_failure) >= _CIRCUIT_RESET_AFTER:
            async with _surreal_lock:
                _surreal_circuit_open = False
                _surreal_backoff_level = 0
        else:
            raise RuntimeError(
                f"SurrealDB circuit open (failures={failure_count}); next probe in {_CIRCUIT_RESET_AFTER:.1f}s"
            )

    max_retries = _budget_aware_should_retry(sql)
    last_exception = None

    client = await _get_client()
    for attempt in range(max_retries):
            try:
                if isinstance(body, str):
                    response = await client.post(
                        SURREAL_URL,
                        content=body,
                        headers=headers,
                        auth=SURREAL_AUTH,
                        timeout=30.0,
                    )
                else:
                    response = await client.post(
                        SURREAL_URL,
                        json=json.loads(body) if isinstance(body, str) else None,
                        headers=headers,
                        auth=SURREAL_AUTH,
                        timeout=30.0,
                    )
                if response.status_code >= 400:
                    error_msg = response.text
                    print(
                        f"[ERROR] SurrealDB Error ({response.status_code}): {error_msg}"
                    )
                    raise httpx.HTTPStatusError(
                        f"SurrealDB error {response.status_code}: {error_msg}",
                        request=response.request,
                        response=response,
                    )
                data = response.json()
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("status") == "ERR":
                            raise RuntimeError(
                                f"SurrealDB Error: {item.get('information') or item.get('result')} | SQL: {sql[:120]}"
                            )
                # success -> reset circuit state (only lock for this update)
                async with _surreal_lock:
                    _surreal_failure_count = 0
                    _surreal_circuit_open = False
                    _surreal_backoff_level = 0
                    # Success: reset health factor
                    from src.router.policy import BudgetTracker

                    BudgetTracker.update_system_health(1.0)
                return data
            except Exception as exc:
                last_exception = exc
                async with _surreal_lock:
                    _surreal_failure_count += 1
                    _surreal_last_failure = time.time()
                    level = _surreal_backoff_level
                    _surreal_backoff_level = min(level + 1, 10)

                    # Update health factor based on failure count
                    from src.router.policy import BudgetTracker

                    health = 1.0 - (min(_surreal_failure_count, 10) / 12.0)
                    BudgetTracker.update_system_health(health)

                if attempt < max_retries - 1:
                    delay = _jittered_backoff(level)
                    await asyncio.sleep(delay)

    # All retries failed -> possibly open circuit
    async with _surreal_lock:
        _surreal_circuit_open = _surreal_failure_count >= _CIRCUIT_OPEN_THRESHOLD
        opened = _surreal_circuit_open
        current_failures = _surreal_failure_count

    raise RuntimeError(
        f"SurrealDB unreachable after {max_retries} attempts (failures={current_failures}, circuit={'open' if opened else 'closed'}): {last_exception}"
    )


def _extract_result(data: List[Dict], index: int = 1) -> List[Dict]:
    """Extract results from SurrealDB response."""
    if not isinstance(data, list):
        return []

    # Filter out connection info messages
    candidates = [
        item
        for item in data
        if isinstance(item, dict)
        and item.get("status") == "OK"
        and "result" in item
        and not (
            isinstance(item["result"], dict)
            and "database" in item["result"]
            and "namespace" in item["result"]
        )
    ]

    if not candidates:
        return []

    # If index is 1, we usually want the FIRST meaningful result
    # (since index 0 was likely the USE NS/DB statement which we filtered out)
    if index == 1 and len(candidates) >= 1:
        target = candidates[0]
    elif len(candidates) <= index:
        target = candidates[-1]
    else:
        target = candidates[index]

    result = target.get("result", [])
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return []


def _extract_result_batch(data: List[Dict]) -> List[Any]:
    """Extract results from a multi-statement SurrealDB response.
    Skips the USE NS response (index 0), returns results for each subsequent statement."""
    if not isinstance(data, list) or len(data) < 2:
        return []
    return [
        item.get("result", []) if isinstance(item, dict) else [] for item in data[1:]
    ]


def _validate_limit(value: int, name: str = "limit", max_val: int = 1_000_000) -> int:
    """Validate that a limit/value is non-negative and within bounds. Raises ValueError if not."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be non-negative (got {value})")
    if value > max_val:
        raise ValueError(f"{name} exceeds maximum of {max_val} (got {value})")
    return value


def _validate_event_id(event_id: str) -> str:
    """Validate that an event_id has the correct format (event:xxx or entity:xxx)."""
    import re
    if not re.match(r'^(event|entity):[a-z0-9]+$', event_id):
        raise ValueError(f"Invalid ID format: '{event_id}'. Expected format: 'event:<id>' or 'entity:<id>'")
    return event_id


def _clean_output(obj: Any) -> Any:
    """Recursively removes large fields like 'embedding' from output objects."""
    if isinstance(obj, list):
        return [_clean_output(i) for i in obj]
    if isinstance(obj, dict):
        # Create a copy to avoid modifying the original if it's cached or reused
        new_dict = {k: _clean_output(v) for k, v in obj.items() if k != "embedding"}
        return new_dict
    return obj


@app.get("/classify")
async def classify_endpoint(
    query: str = Query(..., description="The query to classify"),
):
    """Classify a query according to type and confidence"""
    classifier = QueryClassifier()
    query_type, confidence = classifier.classify(query)
    return {"query": query, "type": query_type, "confidence": confidence}


@app.get("/route")
async def route_endpoint(query: str = Query(..., description="The query to route")):
    """Route a query according to the routing policy"""
    classifier = QueryClassifier()
    policy = RoutingPolicy()

    query_type, confidence = classifier.classify(query)
    strategy = policy.get_strategy(query_type, confidence)

    return {
        "query": query,
        "classification": {"type": query_type, "confidence": confidence},
        "routing_strategy": strategy,
    }


@app.post("/plan_and_execute")
async def plan_and_execute_endpoint(request_data: dict):
    """Create and execute a plan for the given query"""
    query = request_data.get("query", "")
    classifier = QueryClassifier()
    policy = RoutingPolicy()
    executor = PlanExecutor()

    # Classify and route the query
    query_type, confidence = classifier.classify(query)
    strategy_info = policy.get_strategy(query_type, confidence)

    # Create a plan
    plan = {
        "query": query,
        "strategy": strategy_info["strategy"],
        "classification": {"type": query_type, "confidence": confidence},
    }

    # Execute the plan (using run_in_executor to avoid blocking)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, executor.execute_plan, plan)

    return result


@app.post("/maintain")
async def maintain_endpoint():
    """Perform maintenance operations"""
    maintainer = ConservativeMaintainer()
    result = await maintainer.perform_maintenance()

    return result


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "components": {
            "classifier": "ready",
            "router": "ready",
            "planner": "ready",
            "executor": "ready",
            "maintainer": "ready",
        },
    }


MAX_CONTENT_LENGTH = 100_000


# =============================================================================
# Gemeinsame Logik (einmal definiert, von HTTP + MCP genutzt)
# =============================================================================


async def _store_content(content: str, source: str = "user_input", debug: bool = False) -> dict:
    """Einzige Store-Implementierung: validiert → EntropyGate → Event + ggf. KG."""
    if not content or not content.strip():
        return {"event_id": None, "status": "error", "source": source,
                "message": "content must not be empty or whitespace-only"}
    if '\x00' in content:
        return {"event_id": None, "status": "error", "source": source,
                "message": "content contains null bytes, rejecting"}
    if len(content) > MAX_CONTENT_LENGTH:
        return {"event_id": None, "status": "error", "source": source,
                "message": f"content exceeds maximum length of {MAX_CONTENT_LENGTH} (got {len(content)})"}

    from src.extraction.entropy_gate import EntropyGate
    gate = EntropyGate()
    # Quick gate check for the response (ingest() calls should_extract internally too)
    gate_result = gate.should_extract(content)
    event_id = await asyncio.to_thread(gate.ingest, content, source, debug)

    if event_id is None:
        return {"event_id": None, "status": "error", "source": source,
                "message": "storage failed – event could not be persisted"}
    return {"event_id": event_id, "status": "stored", "source": source,
            "gate": gate_result.get("decision", "active")}


async def _execute_query(query: str, cost_budget: str = "auto") -> dict:
    """Einzige Query-Implementierung: classify → route → execute → parse → track."""
    classifier = QueryClassifier()
    q_type, confidence = classifier.classify(query)

    policy = RoutingPolicy(cost_tracker=cost_tracker)
    strategy = policy.get_strategy(q_type, confidence)

    executor = RetrievalExecutor()
    results_raw = await executor.execute_strategy(strategy, query)
    results = _flatten_query_results(results_raw)

    btk = strategy.get("budget_tracker_key")
    if isinstance(btk, str):
        policy.finish_execution(btk)

    entities, facts, events = _categorize_results(results)

    return {
        "query": query,
        "classified_as": q_type,
        "confidence": confidence,
        "strategy": strategy["strategy"],
        "cost_budget": strategy["cost_budget"],
        "results": {"entities": entities, "facts": facts, "events": events},
        "total": len(entities) + len(facts) + len(events),
        "budget_tracker_key": btk,
    }


def _flatten_query_results(results_raw: Any) -> List[Any]:
    """Flatten the various result shapes from different strategies into one list."""
    if isinstance(results_raw, dict) and "combined_results" in results_raw:
        combined = results_raw["combined_results"]
        return combined if isinstance(combined, list) else [combined]

    if isinstance(results_raw, dict):
        flat = []
        for key in ("events", "entities", "facts", "keyword_results", "vector_results", "temporal_results", "result"):
            val = results_raw.get(key)
            if isinstance(val, list):
                flat.extend(val)
        if not flat and "result" in results_raw:
            val = results_raw["result"]
            flat = val if isinstance(val, list) else [val]
        return flat

    if isinstance(results_raw, list):
        return results_raw
    return []


def _categorize_results(results: List[Any]) -> tuple:
    """Sort results into entities, facts, events buckets."""
    entities, facts, events = [], [], []
    for r in results:
        if isinstance(r, dict):
            clean = _clean_output(r)
            rid = clean.get("id", "")
            if isinstance(rid, str):
                if rid.startswith("entity:"):
                    entities.append(clean)
                elif rid.startswith("fact:"):
                    facts.append(clean)
                elif rid.startswith("event:"):
                    events.append(clean)
                else:
                    events.append(clean)
            else:
                events.append(clean)
        elif isinstance(r, list):
            for item in r:
                if isinstance(item, dict):
                    clean = _clean_output(item)
                    rid = clean.get("id", "")
                    if isinstance(rid, str) and rid.startswith("entity:"):
                        entities.append(clean)
                    elif isinstance(rid, str) and rid.startswith("fact:"):
                        facts.append(clean)
                    else:
                        events.append(clean)
    return entities, facts, events


# =============================================================================
# Schicht 1 – HTTP-Endpoints (thin wrapper um gemeinsame Logik)
# =============================================================================


@app.post("/memory/store")
async def memory_store_endpoint(request_data: dict):
    """Stores a new event in the raw event log. Runs through entropy gate. NOTE: Only English content should be stored — German or other languages produce noisy entity extraction."""
    return await _store_content(
        request_data.get("content", ""),
        request_data.get("source", "user_input"),
    )


@app.post("/memory/query")
async def memory_query_endpoint(request_data: dict):
    """Routes a natural language query through the full pipeline: classify → plan → retrieve."""
    return await _execute_query(
        request_data.get("query", ""),
        request_data.get("cost_budget", "auto"),
    )


# =============================================================================
# Schicht 1 – MCP-Tools (thin wrapper um gemeinsame Logik)
# =============================================================================


@mcp.tool()
async def memory_store(content: str, source: str = "user_input",
                       metadata: Optional[Dict[str, Any]] = None) -> dict:
    """Stores a new event in the raw event log. Runs through entropy gate. NOTE: Only English content should be stored — German or other languages produce noisy entity extraction."""
    return await _store_content(content, source, debug=True)


@mcp.tool()
async def memory_query(query: str, cost_budget: str = "auto") -> dict:
    """Routes a natural language query through the full pipeline: classify → plan → retrieve."""
    return await _execute_query(query, cost_budget)


async def _get_or_create_entity(name: str) -> Optional[str]:
    """Find an entity by name or create it with inferred type. Returns entity ID or None."""
    name_escaped = escape_surrealql(name)
    sql = f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
    result = await _query_surreal(sql)
    entities = _extract_result(result, 1)
    if entities:
        return entities[0]["id"]

    entity_type = infer_entity_type(name)
    create_sql = f"CREATE entity SET name = '{name_escaped}', type = '{entity_type}';"
    create_result = await _query_surreal(create_sql)
    created = _extract_result(create_result, 1)
    if created:
        return created[0]["id"]
    return None


@mcp.tool()
async def memory_update(subject: str, predicate: str, new_value: str) -> dict:
    """Updates a fact in the KG via logical invalidation. Old fact gets valid_until, new fact created.
    If the target entity does not exist yet, it will be created automatically."""
    subject_id = await _get_or_create_entity(subject)
    if not subject_id:
        return {"status": "error", "message": f"Subject entity '{subject}' does not exist and could not be created"}

    object_id = await _get_or_create_entity(new_value)
    if not object_id:
        return {"status": "error", "message": f"Object entity '{new_value}' does not exist and could not be created"}

    subject_escaped = escape_surrealql(subject)
    predicate_escaped = escape_surrealql(predicate)

    find_sql = f"""
    SELECT * FROM fact
    WHERE in.name = '{subject_escaped}'
      AND predicate = '{predicate_escaped}'
      AND valid_until = NONE
    LIMIT 1;
    """
    find_result = await _query_surreal(find_sql)
    facts = _extract_result(find_result, 1)

    invalidated = None
    if facts:
        old_fact_id = facts[0]["id"]
        invalidate_sql = f"UPDATE {old_fact_id} SET valid_until = time::now();"
        await _query_surreal(invalidate_sql)
        invalidated = old_fact_id
    else:
        related_sql = f"""
        SELECT predicate, out.name AS value, out.type AS value_type FROM fact
        WHERE in.name = '{subject_escaped}'
          AND valid_until = NONE;
        """
        related_result = await _query_surreal(related_sql)
        related_facts = _extract_result(related_result, 1)
        return {
            "status": "error",
            "message": f"No active fact found for subject='{subject}', predicate='{predicate}'",
            "existing_facts_for_subject": [
                {"predicate": f["predicate"], "value": f.get("value")}
                for f in related_facts
            ] if related_facts else [],
        }

    relate_sql = f"RELATE {subject_id}->fact->{object_id} SET predicate = '{predicate_escaped}', confidence = 1.0;"
    relate_result = await _query_surreal(relate_sql)
    new_fact = _extract_result(relate_result, 1)
    new_fact_id = new_fact[0]["id"] if new_fact else None

    return {
        "status": "ok",
        "invalidated_fact": invalidated,
        "new_fact": new_fact_id,
        "subject": subject,
        "predicate": predicate,
        "new_value": new_value,
    }


# =============================================================================
# Schicht 2 - Retrieval Primitives
# =============================================================================


@app.get("/memory/event_log_search")
async def event_log_search_endpoint(
    query: str = Query(..., description="Search query"),
    since: Optional[str] = Query(None, description="Start date"),
    until: Optional[str] = Query(None, description="End date"),
    limit: int = Query(10, description="Result limit"),
):
    """Direct timeline query without router: hybrid search (BM25 + vector + RRF)."""
    if not query.strip():
        sql = f"SELECT * FROM event WHERE (forgotten IS NONE OR forgotten = false)"
        if since:
            sql += f" AND timestamp >= '{escape_surrealql(since)}'"
        if until:
            sql += f" AND timestamp <= '{escape_surrealql(until)}'"
        sql += f" ORDER BY timestamp DESC LIMIT {limit};"
        result = await _query_surreal(sql)
        events = _extract_result(result, 1)
        return {"events": _clean_output(events), "count": len(events)}

    query_escaped = escape_surrealql(query)
    time_filter = ""
    if since:
        time_filter += (
            f" AND timestamp >= '{escape_surrealql(since)}'"
        )
    if until:
        time_filter += (
            f" AND timestamp <= '{escape_surrealql(until)}'"
        )

    forgotten_filter = "(forgotten IS NONE OR forgotten = false)"

    # 1) Lexical search via FTX index
    ftx_sql = f"""
    SELECT id, content, timestamp, source, metadata, 'lexical' AS search_type
    FROM event
    WHERE content @@ '{query_escaped}'
      AND {forgotten_filter}
      {time_filter}
    LIMIT {limit * 4};
    """

    # Start FTX query immediately (overlap with embedding computation)
    ftx_task = asyncio.create_task(_query_surreal(ftx_sql))

    # 2) Vector search — compute embedding while FTX runs
    try:
        query_vector = await _embed_query(query)
        query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

        vec_sql = f"""
        SELECT id, content, timestamp, source, metadata,
               vector::similarity::cosine(embedding, {query_vector_str}) AS vec_score,
               'vector' AS search_type
        FROM event
        WHERE embedding IS NOT NONE
          AND {forgotten_filter}
          AND array::len(embedding) = {len(query_vector)}
          {time_filter}
        ORDER BY vec_score DESC
        LIMIT {limit * 4};
        """
        vec_result = await _query_surreal(vec_sql)
        ftx_result = await ftx_task
    except Exception:
        ftx_result = await ftx_task
        vec_result = None

    ftx_events = _extract_result(ftx_result, 1) or []

    # RRF fusion
    k = 60
    fused = {}
    for rank, ev in enumerate(ftx_events):
        eid = ev.get("id")
        if eid:
            fused[eid] = {"rrf": 1.0 / (k + rank), "event": ev}

    if vec_result is not None:
        vec_events = _extract_result(vec_result, 1) or []
        for rank, ev in enumerate(vec_events):
            eid = ev.get("id")
            if eid in fused:
                fused[eid]["rrf"] += 1.0 / (k + rank)
            else:
                fused[eid] = {"rrf": 1.0 / (k + rank), "event": ev}

    sorted_events = [
        item["event"]
        for item in sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)[:limit]
    ]
    events = _clean_output(sorted_events)

    return {"events": events, "count": len(events)}


@app.get("/memory/kg_query")
async def kg_query_endpoint(
    subject: Optional[str] = Query(None, description="Subject to search for"),
    predicate: Optional[str] = Query(None, description="Predicate to search for"),
    at_time: Optional[str] = Query(None, description="Time to query at"),
    limit: int = Query(20, description="Limit the number of results"),
):
    """Direct graph traversal: query facts by subject/predicate/time."""
    return await _execute_kg_query(subject, predicate, at_time, limit)


@mcp.tool()
async def event_log_search(
    query: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Direct timeline query without router: hybrid search (BM25 + vector + RRF fusion)."""
    query_escaped = escape_surrealql(query)
    time_filter = ""
    if since:
        since_escaped = escape_surrealql(since)
        time_filter += f" AND timestamp >= '{since_escaped}'"
    if until:
        until_escaped = escape_surrealql(until)
        time_filter += f" AND timestamp <= '{until_escaped}'"

    forgotten_filter = "(forgotten IS NONE OR forgotten = false)"

    # 1) Lexical search via FTX index
    ftx_sql = f"""
    SELECT id, content, timestamp, source, metadata, 'lexical' AS search_type
    FROM event
    WHERE content @@ '{query_escaped}'
      AND {forgotten_filter}
      {time_filter}
    LIMIT {limit * 4};
    """

    # Start FTX query immediately (overlap with embedding computation)
    ftx_task = asyncio.create_task(_query_surreal(ftx_sql))

    # 2) Vector search — compute embedding while FTX runs
    try:
        query_vector = await _embed_query(query)
        query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

        vec_sql = f"""
        SELECT id, content, timestamp, source, metadata,
               vector::similarity::cosine(embedding, {query_vector_str}) AS vec_score,
               'vector' AS search_type
        FROM event
        WHERE embedding IS NOT NONE
          AND {forgotten_filter}
          AND array::len(embedding) = {len(query_vector)}
          {time_filter}
        ORDER BY vec_score DESC
        LIMIT {limit * 4};
        """
        vec_result = await _query_surreal(vec_sql)
        ftx_result = await ftx_task
    except Exception:
        ftx_result = await ftx_task
        vec_result = None

    ftx_events = _extract_result(ftx_result, 1) or []

    # RRF fusion
    k = 60
    fused = {}
    for rank, ev in enumerate(ftx_events):
        eid = ev.get("id")
        if eid:
            fused[eid] = {"rrf": 1.0 / (k + rank), "event": ev}

    if vec_result is not None:
        vec_events = _extract_result(vec_result, 1) or []
        for rank, ev in enumerate(vec_events):
            eid = ev.get("id")
            if eid in fused:
                fused[eid]["rrf"] += 1.0 / (k + rank)
            else:
                fused[eid] = {"rrf": 1.0 / (k + rank), "event": ev}

    sorted_events = [
        item["event"]
        for item in sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)[:limit]
    ]
    events = _clean_output(sorted_events)

    return {"events": events, "count": len(events)}


async def _execute_kg_query(
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    at_time: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Execute KG query with two-step resolution: entity name → ID → facts.
    Resolves subject names via BM25 + CONTAINS, then queries facts with IN [...]."""
    try:
        limit = _validate_limit(limit, "limit")
    except ValueError as e:
        return {"error": str(e), "facts": [], "entities": [], "count": 0}

    resolved_entity_ids = set()

    # Step 1: Resolve subject name to entity IDs via entity table
    if subject:
        subject_escaped = escape_surrealql(subject)
        entity_sql = (
            f"SELECT id FROM entity "
            f"WHERE (name @@ '{subject_escaped}' "
            f"  OR name CONTAINS '{subject_escaped}' "
            f"  OR '{subject_escaped}' CONTAINS name) "
            f"AND (forgotten IS NONE OR forgotten = false) "
            f"LIMIT 20;"
        )
        entity_result = await _query_surreal(entity_sql)
        entity_rows = _extract_result(entity_result, 1)
        for row in entity_rows:
            if isinstance(row, dict) and row.get("id"):
                resolved_entity_ids.add(row["id"])

        if not resolved_entity_ids:
            return {"facts": [], "entities": [], "count": 0, "note": "no matching entities found"}

    # Step 2: Query facts using resolved entity IDs
    sql = "SELECT * FROM fact WHERE (valid_until IS NONE OR valid_until > time::now())"

    if resolved_entity_ids:
        ids_str = ", ".join(resolved_entity_ids)
        sql += f" AND (in IN [{ids_str}] OR out IN [{ids_str}])"

    if predicate:
        predicate_escaped = escape_surrealql(predicate)
        sql += f" AND predicate = '{predicate_escaped}'"

    if at_time:
        at_time_escaped = escape_surrealql(at_time)
        sql += f" AND valid_from <= '{at_time_escaped}'"

    sql += f" LIMIT {limit} FETCH in, out;"
    result = await _query_surreal(sql)
    facts = _extract_result(result, 1)

    entities = []
    seen_ids = set()
    for fact in facts:
        for key in ("in", "out"):
            val = fact.get(key)
            if isinstance(val, dict) and val.get("id") and val.get("id") not in seen_ids:
                entities.append(_clean_output(val))
                seen_ids.add(val.get("id"))
            elif isinstance(val, str) and val not in seen_ids:
                entities.append({"id": val})
                seen_ids.add(val)

    clean_facts = [_clean_output(fact) for fact in facts]
    return {"facts": clean_facts, "entities": entities, "count": len(clean_facts)}


@mcp.tool()
async def kg_query(
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    at_time: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """Direct graph traversal: query facts by subject/predicate/time.
    Returns associated entities with their inferred types (e.g., 'organization', 'concept')."""
    return await _execute_kg_query(subject, predicate, at_time, limit)


def _character_diversity(text: str) -> float:
    """Anteil unique chars: len(set(text)) / len(text). < 0.15 = repetitive noise."""
    if not text:
        return 0.0
    unique = len(set(text.lower()))
    return unique / len(text)


def _is_highly_repetitive(text: str) -> bool:
    """Erkennt repetitive/noise content wie 'test test test test test' or 'ab ab ab ab ab'.
    Prüft character_diversity (< 0.20) und zusätzlich die Wort-Wiederholungsrate."""
    if not text or len(text) < 5:
        return False
    div = _character_diversity(text)
    # Character-Diversity: "test test test test test" -> 4/24 = 0.167 < 0.20 ✓
    if div < 0.20:
        return True
    # Word-Repetition: zählt unique words / total words
    words = text.lower().split()
    if len(words) >= 3:
        unique_words = len(set(words))
        word_ratio = unique_words / len(words)
        if word_ratio < 0.3:
            return True
    return False


@mcp.tool()
async def semantic_search(query: str, top_k: int = 5) -> dict:
    """Pure vector search without KG. Filters out highly repetitive/noise content."""
    embedding_service = get_embedding_service()
    query_vector = await _embed_query(query)

    query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

    # Fetch extra candidates to allow post-filtering
    fetch_k = min(top_k * 4, 100)
    sql = f"""
    SELECT id, content, vector::similarity::cosine(embedding, {query_vector_str}) AS score
    FROM event
    WHERE embedding IS NOT NONE
      AND (forgotten IS NONE OR forgotten = false)
      AND array::len(embedding) = {len(query_vector)}
    ORDER BY score DESC
    LIMIT {fetch_k};
    """
    result = await _query_surreal(sql)
    raw_events = _extract_result(result, 1)

    # Post-filter: penalize highly repetitive content by re-weighting its score
    scored = []
    for ev in raw_events:
        content = ev.get("content", "")
        score = ev.get("score", 0)
        if not isinstance(score, (int, float)):
            score = 0.0
        if _is_highly_repetitive(content):
            # Heavily penalise but don't remove entirely — allows real matches to dominate
            score = score * 0.02
        scored.append((score, ev))

    # Re-sort and take top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    events = [_clean_output(ev) for _, ev in scored[:top_k]]

    return {"events": events, "count": len(events)}


# =============================================================================
# Schicht 3 - Introspection Tools
# =============================================================================


@app.post("/memory/semantic_search")
async def semantic_search_endpoint(request_data: dict):
    """Pure vector search without KG. Filters out highly repetitive/noise content."""
    query = request_data.get("query", "")
    top_k = request_data.get("top_k", 5)

    embedding_service = get_embedding_service()
    query_vector = await _embed_query(query)

    query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

    # Fetch extra candidates to allow post-filtering
    fetch_k = min(top_k * 4, 100)
    sql = f"""
    SELECT id, content, vector::similarity::cosine(embedding, {query_vector_str}) AS score
    FROM event
    WHERE embedding IS NOT NONE
      AND (forgotten IS NONE OR forgotten = false)
      AND array::len(embedding) = {len(query_vector)}
    ORDER BY score DESC
    LIMIT {fetch_k};
    """
    result = await _query_surreal(sql)
    raw_events = _extract_result(result, 1)

    # Post-filter: penalize highly repetitive content by re-weighting its score
    scored = []
    for ev in raw_events:
        content = ev.get("content", "")
        score = ev.get("score", 0)
        if not isinstance(score, (int, float)):
            score = 0.0
        if _is_highly_repetitive(content):
            score = score * 0.02
        scored.append((score, ev))

    # Re-sort and take top_k
    scored.sort(key=lambda x: x[0], reverse=True)
    events = [_clean_output(ev) for _, ev in scored[:top_k]]

    return {"events": events, "count": len(events)}


@mcp.tool()
async def memory_stats() -> dict:
    """Returns statistics about the memory system."""
    sql = """
SELECT count() AS count FROM event WHERE (forgotten = false OR forgotten IS NONE) GROUP ALL;
SELECT count() AS count FROM entity WHERE (forgotten = false OR forgotten IS NONE) GROUP ALL;
SELECT count() AS count FROM fact WHERE (valid_until IS NONE OR valid_until = NONE) AND (forgotten = false OR forgotten IS NONE) GROUP ALL;
SELECT timestamp FROM event WHERE (forgotten = false OR forgotten IS NONE) ORDER BY timestamp ASC LIMIT 1;
SELECT timestamp FROM event WHERE (forgotten = false OR forgotten IS NONE) ORDER BY timestamp DESC LIMIT 1;
SELECT count() AS count FROM gate_log GROUP ALL;
SELECT count() AS count FROM gate_log WHERE decision = 'extract' GROUP ALL;
"""
    try:
        data = await _query_surreal(sql.strip())
    except Exception as e:
        print(f"[WARN] memory_stats batch query failed: {e}")
        return {
            "event_count": 0,
            "entity_count": 0,
            "fact_count": 0,
            "oldest_event": None,
            "newest_event": None,
            "gate_pass_rate": 0.0,
        }

    results = _extract_result_batch(data)

    stats = {
        "event_count": 0,
        "entity_count": 0,
        "fact_count": 0,
        "oldest_event": None,
        "newest_event": None,
        "gate_pass_rate": 0.0,
    }

    if len(results) >= 1 and isinstance(results[0], list) and len(results[0]) > 0:
        stats["event_count"] = results[0][0].get("count", 0)
    if len(results) >= 2 and isinstance(results[1], list) and len(results[1]) > 0:
        stats["entity_count"] = results[1][0].get("count", 0)
    if len(results) >= 3 and isinstance(results[2], list) and len(results[2]) > 0:
        stats["fact_count"] = results[2][0].get("count", 0)
    if len(results) >= 4 and isinstance(results[3], list) and len(results[3]) > 0:
        stats["oldest_event"] = results[3][0].get("timestamp")
    if len(results) >= 5 and isinstance(results[4], list) and len(results[4]) > 0:
        stats["newest_event"] = results[4][0].get("timestamp")

    gate_total = (
        results[5][0].get("count", 0)
        if len(results) >= 6 and isinstance(results[5], list) and len(results[5]) > 0
        else 0
    )
    gate_extracted = (
        results[6][0].get("count", 0)
        if len(results) >= 7 and isinstance(results[6], list) and len(results[6]) > 0
        else 0
    )
    stats["gate_pass_rate"] = (
        round(gate_extracted / gate_total, 3) if gate_total > 0 else 0.0
    )

    return stats


@mcp.tool()
async def explain_routing(query: str) -> dict:
    """Explains why the router chose a specific strategy for a query."""
    classifier = QueryClassifier()
    q_type, confidence = classifier.classify(query)

    policy = RoutingPolicy()
    strategy = policy.get_strategy(q_type, confidence)

    reasons = {
        "temporal": "Query contains temporal indicators (when, date, time, since...). Event log prioritized.",
        "factual": "Query asks for specific facts (who, what, where). Knowledge graph prioritized.",
        "multi-hop": "Query implies multi-hop reasoning (why, relationship, and where...). Graph expansion enabled.",
        "conversational": "Query is conversational (remember, talked about). Composite KG+vector strategy used.",
        "update": "Query is an update instruction. Invalidation strategy selected.",
    }

    return {
        "query": query,
        "classified_as": q_type,
        "confidence": confidence,
        "strategy_selected": strategy["strategy"],
        "cost_budget": strategy["cost_budget"],
        "policy_applied": strategy["policy_applied"],
        "reason": reasons.get(q_type, "Default routing based on query type."),
    }


# =============================================================================
# Schicht 4 - Maintenance Tools
# =============================================================================


@app.post("/memory/stats")
async def memory_stats_endpoint():
    """Returns statistics about the memory system. Uses same logic as MCP memory_stats tool."""
    sql = """
SELECT count() AS count FROM event WHERE (forgotten = false OR forgotten IS NONE) GROUP ALL;
SELECT count() AS count FROM entity WHERE (forgotten = false OR forgotten IS NONE) GROUP ALL;
SELECT count() AS count FROM fact WHERE (valid_until IS NONE OR valid_until = NONE) AND (forgotten = false OR forgotten IS NONE) GROUP ALL;
SELECT timestamp FROM event WHERE (forgotten = false OR forgotten IS NONE) ORDER BY timestamp ASC LIMIT 1;
SELECT timestamp FROM event WHERE (forgotten = false OR forgotten IS NONE) ORDER BY timestamp DESC LIMIT 1;
SELECT count() AS count FROM gate_log GROUP ALL;
SELECT count() AS count FROM gate_log WHERE decision = 'extract' GROUP ALL;
"""
    try:
        data = await _query_surreal(sql.strip())
    except Exception as e:
        print(f"[WARN] memory_stats batch query failed: {e}")
        return {
            "event_count": 0,
            "entity_count": 0,
            "fact_count": 0,
            "oldest_event": None,
            "newest_event": None,
            "gate_pass_rate": 0.0,
        }

    results = _extract_result_batch(data)

    stats = {
        "event_count": 0,
        "entity_count": 0,
        "fact_count": 0,
        "oldest_event": None,
        "newest_event": None,
        "gate_pass_rate": 0.0,
    }

    if len(results) >= 1 and isinstance(results[0], list) and len(results[0]) > 0:
        stats["event_count"] = results[0][0].get("count", 0)
    if len(results) >= 2 and isinstance(results[1], list) and len(results[1]) > 0:
        stats["entity_count"] = results[1][0].get("count", 0)
    if len(results) >= 3 and isinstance(results[2], list) and len(results[2]) > 0:
        stats["fact_count"] = results[2][0].get("count", 0)
    if len(results) >= 4 and isinstance(results[3], list) and len(results[3]) > 0:
        stats["oldest_event"] = results[3][0].get("timestamp")
    if len(results) >= 5 and isinstance(results[4], list) and len(results[4]) > 0:
        stats["newest_event"] = results[4][0].get("timestamp")

    gate_total = (
        results[5][0].get("count", 0)
        if len(results) >= 6 and isinstance(results[5], list) and len(results[5]) > 0
        else 0
    )
    gate_extracted = (
        results[6][0].get("count", 0)
        if len(results) >= 7 and isinstance(results[6], list) and len(results[6]) > 0
        else 0
    )
    stats["gate_pass_rate"] = (
        round(gate_extracted / gate_total, 3) if gate_total > 0 else 0.0
    )

    return stats


@app.post("/memory/explain_routing")
async def explain_routing_endpoint(request_data: dict):
    """Explains why the router chose a specific strategy for a query."""
    query = request_data.get("query", "")
    classifier = QueryClassifier()
    q_type, confidence = classifier.classify(query)

    policy = RoutingPolicy()
    strategy = policy.get_strategy(q_type, confidence)

    reasons = {
        "temporal": "Query contains temporal indicators (when, date, time, since...). Event log prioritized.",
        "factual": "Query asks for specific facts (who, what, where). Knowledge graph prioritized.",
        "multi-hop": "Query implies multi-hop reasoning (why, relationship, and where...). Graph expansion enabled.",
        "conversational": "Query is conversational (remember, talked about). Composite KG+vector strategy used.",
        "update": "Query is an update instruction. Invalidation strategy selected.",
    }

    return {
        "query": query,
        "classified_as": q_type,
        "confidence": confidence,
        "strategy_selected": strategy["strategy"],
        "cost_budget": strategy["cost_budget"],
        "policy_applied": strategy["policy_applied"],
        "reason": reasons.get(q_type, "Default routing based on query type."),
    }


@mcp.tool()
async def memory_forget(
    event_id: Optional[str] = None, entity: Optional[str] = None, reason: str = ""
) -> dict:
    """Forgets a memory by event_id or entity."""
    if event_id:
        event_escaped = escape_surrealql(event_id)
        check_sql = f"SELECT id FROM {event_escaped} LIMIT 1;"
        check_result = await _query_surreal(check_sql)
        events = _extract_result(check_result, 1)
        if not events:
            return {"status": "error", "message": f"Event '{event_id}' not found"}
        sql = f"UPDATE {event_escaped} SET forgotten = true;"
        if reason:
            reason_escaped = escape_surrealql(reason)
            sql = f"UPDATE {event_escaped} SET forgotten = true, forget_reason = '{reason_escaped}';"
        await _query_surreal(sql)
        return {"forgotten_id": event_id, "type": "event", "reason": reason}

    if entity:
        entity_escaped = escape_surrealql(entity)
        if entity_escaped.startswith("entity:"):
            check_sql = f"SELECT id FROM {entity_escaped} LIMIT 1;"
            check_result = await _query_surreal(check_sql)
            found = _extract_result(check_result, 1)
            if not found:
                return {"status": "error", "message": f"Entity '{entity}' not found"}
            entity_id = entity_escaped
        else:
            find_sql = (
                f"SELECT id FROM entity WHERE name CONTAINS '{entity_escaped}' LIMIT 1;"
            )
            find_result = await _query_surreal(find_sql)
            entities = _extract_result(find_result, 1)
            if not entities:
                return {"status": "error", "message": f"Entity '{entity}' not found"}
            entity_id = entities[0]["id"]
        sql = f"UPDATE {entity_id} SET forgotten = true;"
        if reason:
            reason_escaped = escape_surrealql(reason)
            sql = f"UPDATE {entity_id} SET forgotten = true, forget_reason = '{reason_escaped}';"
        await _query_surreal(sql)
        return {"forgotten_id": entity_id, "type": "entity", "reason": reason}

    return {"error": "No valid event_id or entity provided", "reason": reason}


@mcp.tool()
async def memory_consolidate(
    scope: str = "local", entity: Optional[str] = None, delete_stale: bool = False
) -> dict:
    """Consolidates memory entries. When delete_stale=True, physically removes stale facts from the database."""
    maintainer = ConservativeMaintainer(debounce_seconds=0)

    if scope == "entity" and entity:
        entity_escaped = escape_surrealql(entity)
        find_sql = (
            f"SELECT id FROM entity WHERE name CONTAINS '{entity_escaped}' LIMIT 1;"
        )
        find_result_data = await _query_surreal(find_sql)
        find_result = _extract_result(find_result_data, 1)
        if find_result:
            entity_id = find_result[0]["id"]
            maintainer.queue_patch_update(
                entity_id, {"last_consolidated": datetime.now(timezone.utc).isoformat()}
            )
            await maintainer.flush_pending()
            return {
                "scope": scope,
                "entity": entity,
                "entity_id": entity_id,
                "status": "consolidated",
            }

    if scope == "local":
        stale = await maintainer.get_stale_facts(max_age_seconds=86400)
        deleted_count = 0
        if delete_stale and stale:
            for fact in stale:
                fact_id = fact.get("id")
                if fact_id:
                    try:
                        await _query_surreal(f"DELETE {fact_id};")
                        deleted_count += 1
                    except Exception as e:
                        print(f"[WARN] Could not delete stale fact {fact_id}: {e}")
        return {
            "scope": scope,
            "stale_facts_found": len(stale),
            "deleted_count": deleted_count if delete_stale else None,
            "status": "cleaned" if (delete_stale and deleted_count > 0) else "reviewed",
        }

    return {
        "error": "Invalid scope. Use 'local' or 'entity' with entity name.",
        "scope": scope,
    }


# =============================================================================
# Schicht 5 - MCP Resources (statische + dynamische)
# =============================================================================


@mcp.resource("strata://stats", description="Memory system statistics")
async def strata_stats_resource() -> str:
    """Returns memory statistics as a resource."""
    return json.dumps(await memory_stats(), indent=2)


@mcp.resource("strata://events/recent", description="Most recent events in the event log")
async def strata_recent_events_resource() -> str:
    """Returns the 10 most recent non-forgotten events."""
    sql = "SELECT id, content, timestamp, source FROM event WHERE (forgotten IS NONE OR forgotten = false) ORDER BY timestamp DESC LIMIT 10;"
    result = await _query_surreal(sql)
    events = _extract_result(result, 1)
    return json.dumps({"events": _clean_output(events), "count": len(events)}, indent=2)


@mcp.resource("strata://schema", description="Current database schema overview")
async def strata_schema_resource() -> str:
    """Returns the database schema: tables, fields, indexes."""
    sql = "INFO FOR DB;"
    result = await _query_surreal(sql)
    info = _extract_result(result, 1)
    return json.dumps({"schema": _clean_output(info[0]) if info else {}}, indent=2)


@mcp.resource("strata://tables", description="List all tables with record counts")
async def strata_tables_resource() -> str:
    """Returns all tables and their record counts."""
    tables_sql = "INFO FOR DB;"
    result = await _query_surreal(tables_sql)
    info = _extract_result(result, 1)
    tables = list(info[0].get("tables", {}).keys()) if info else []
    table_info = []
    for table in tables:
        try:
            count_result = await _query_surreal(f"SELECT count() AS count FROM {table} GROUP ALL;")
            count_data = _extract_result(count_result, 1)
            count_val = count_data[0].get("count", 0) if count_data else 0
            table_info.append({"name": table, "count": count_val})
        except Exception:
            table_info.append({"name": table, "count": -1})
    return json.dumps({"tables": table_info}, indent=2)


# =============================================================================
# Schicht 6 - MCP Prompts
# =============================================================================


@mcp.prompt(
    "memory-workflow",
    description="Guide: How to use STRATA memory tools — store, query, forget, consolidate"
)
async def memory_workflow_prompt() -> list:
    return [
        {
            "role": "user",
            "prompt": (
                "## STRATA Memory Workflow\n\n"
                "You have access to a STRATA memory server with the following tools:\n\n"
                "### Storage\n"
                "- **memory_store(content, source, metadata)** — Stores a new event. "
                "Runs through EntropyGate which decides if KG extraction is needed. "
                "Use this for any factual information you want to persist.\n\n"
                "### Retrieval\n"
                "- **memory_query(query, cost_budget)** — Full pipeline: classify → route → retrieve. "
                "Returns entities, facts, and events. Use for natural language questions.\n"
                "- **event_log_search(query, since, until, limit)** — Direct hybrid search (BM25 + vector + RRF). "
                "Best for timeline queries.\n"
                "- **semantic_search(query, top_k)** — Pure vector search. Best for similarity matching.\n"
                "- **kg_query(subject, predicate, at_time, limit)** — Knowledge graph traversal. "
                "Best for structured fact lookups.\n\n"
                "### Maintenance\n"
                "- **memory_forget(event_id, entity, reason)** — Soft-deletes a memory.\n"
                "- **memory_consolidate(scope, entity, delete_stale)** — Cleans up stale facts.\n\n"
                "### Insights\n"
                "- **memory_stats()** — System statistics.\n"
                "- **explain_routing(query)** — Explains routing decisions.\n\n"
                "### Best Practices\n"
                "1. Always use **English content** for storage (German causes noisy entity extraction)\n"
                "2. Use **memory_query** for complex questions (uses hybrid strategy)\n"
                "3. Use **kg_query** for structured relationship lookups\n"
                "4. Use **semantic_search** for finding similar content\n"
                "5. Use **event_log_search** for time-based queries\n"
                "6. Clean up test data with **memory_forget** + reason\n\n"
                "### Example: Storing and retrieving user info\n"
                "1. `memory_store(content=\"The user is named Alice.\", source=\"conversation\")`\n"
                "2. `memory_query(query=\"What is the user's name?\")`\n"
                "3. `kg_query(subject=\"Alice\")` → see structured facts\n"
            )
        }
    ]


@mcp.prompt(
    "kg-exploration",
    description="Guide: How to explore the Knowledge Graph — entities, facts, relations"
)
async def kg_exploration_prompt() -> list:
    return [
        {
            "role": "user",
            "prompt": (
                "## Knowledge Graph Exploration\n\n"
                "STRATA maintains a knowledge graph with:\n"
                "- **Entities** (people, organizations, concepts, technologies)\n"
                "- **Facts** (relationships between entities with predicate + confidence)\n"
                "- **Events** (source records with timestamps)\n\n"
                "### Query Tools\n"
                "- **kg_query(subject)** — Find all facts connected to a subject\n"
                "- **kg_query(subject, predicate)** — Filter by relationship type\n"
                "- **kg_query(subject, predicate, limit=N)** — Control result count\n\n"
                "### Predicate Types\n"
                "| Predicate | Confidence | Description |\n"
                "|-----------|-----------|-------------|\n"
                "| `mentions` | 0.80 | Explicit mention in source text |\n"
                "| `works_on` | 0.99 | SVO-extracted: subject works on object |\n"
                "| `co_occurs_with` | 0.50-0.85 | Same sentence proximity |\n"
                "| `weakly_related` | 0.30-0.50 | Low embedding similarity |\n"
                "| `created` | >0.85 | SVO: subject created object |\n"
                "| `located_in` | — | SVO: subject located in object |\n\n"
                "### Example Workflow\n"
                "1. `kg_query(subject=\"Tobias\")` → finds person entity\n"
                "2. `kg_query(subject=\"Tobias\", predicate=\"works_on\")` → specific relation\n"
                "3. `event_log_search(query=\"Tobias\")` → find source events\n"
                "4. `semantic_search(query=\"Tobias\")` → find similar content\n"
            )
        }
    ]


@mcp.prompt(
    "dba-tools",
    description="Guide: Direct database access — query, select, create, update, delete records"
)
async def dba_tools_prompt() -> list:
    return [
        {
            "role": "user",
            "prompt": (
                "## Direct Database Access\n\n"
                "STRATA also provides raw SurrealDB access via the REST API "
                "(not MCP tools — use HTTP endpoints):\n\n"
                "### Available Endpoints\n"
                "- `POST /memory/store` — Store an event\n"
                "- `POST /memory/query` — Natural language query\n"
                "- `POST /memory/semantic_search` — Pure vector search\n"
                "- `POST /memory/stats` — System statistics\n"
                "- `POST /memory/explain_routing` — Routing explanation\n"
                "- `POST /maintain` — Run maintenance\n"
                "- `GET /health` — Health check\n\n"
                "### Resources (MCP)\n"
                "- `strata://stats` — Memory statistics\n"
                "- `strata://events/recent` — Recent events\n"
                "- `strata://schema` — Database schema\n"
                "- `strata://tables` — All tables with counts\n"
            )
        }
    ]


def _start_http_server():
    """Start the FastAPI HTTP server on the configured port."""
    import uvicorn

    control_plane_port = int(os.getenv("CONTROL_PLANE_PORT", "8082"))
    print(f"[INFO] Starting HTTP server on 0.0.0.0:{control_plane_port}")
    uvicorn.run(app, host="0.0.0.0", port=control_plane_port, log_level="info")


if __name__ == "__main__":
    # Ensure schema is loaded before starting servers
    asyncio.run(ensure_schema_loaded())

    # Pre-load embedding service to avoid hang on first tool call
    get_embedding_service()

    # Start HTTP server in a background thread
    import threading

    http_thread = threading.Thread(target=_start_http_server, daemon=True)
    http_thread.start()

    # Run FastMCP stdio server (for MCP clients) in main thread
    mcp.run()
