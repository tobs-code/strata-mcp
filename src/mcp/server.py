# -*- coding: utf-8 -*-
"""
Control Plane Server implementing the Model Context Protocol (MCP)
Coordinates all components of the agent memory system

NOTE: This implements the Model Context Protocol (MCP) specification from Anthropic.
This server serves as the system coordinator.
"""
import sys
import os
import json
import time
import random
import httpx
import asyncio
from typing import Optional, Dict, Any, List, Union
from datetime import datetime, timezone

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is not installed, skip loading .env file
    pass

# Standard imports
import uvicorn
from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Our components
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP  # This is the Model Context Protocol implementation
from src.extraction.classifier import QueryClassifier
from src.router.policy import RoutingPolicy
from src.planner.executor import RetrievalExecutor, PlanExecutor
from src.maintenance.conservative_maintainer import ConservativeMaintainer
from src.extraction.embedding_service import get_embedding_service

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
SURREAL_AUTH = (os.getenv("SURREALDB_USER", "root"), os.getenv("SURREALDB_PASS", "root"))
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
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
                timeout=60
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

# Circuit-breaker thresholds (budget-aware)
_CIRCUIT_OPEN_THRESHOLD = 5  # failures before opening
_CIRCUIT_RESET_AFTER = 10.0  # seconds before half-open retry
_MAX_BACKOFF = 8.0  # cap jittered backoff
_RECONNECT_INTERVAL = 30.0  # background check every 30s


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
                
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        SURREAL_URL,
                        content=full_sql,
                        headers=headers,
                        auth=SURREAL_AUTH,
                        timeout=5.0
                    )
                    
                    if response.status_code < 400:
                        # Success! Reset everything
                        async with _surreal_lock:
                            if _surreal_circuit_open:
                                print("[INFO] SurrealDB connection restored. Closing circuit.")
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
    ceiling = min(_MAX_BACKOFF, base * (2 ** level))
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


async def _query_surreal(sql: str) -> Any:
    global _surreal_failure_count, _surreal_circuit_open, _surreal_last_failure, _surreal_backoff_level, _reconnect_task_started

    # Start background reconnect task if not already started
    if not _reconnect_task_started:
        async with _surreal_lock:
            if not _reconnect_task_started:
                asyncio.create_task(_background_reconnect_task())
                _reconnect_task_started = True

    headers = {
        "Accept": "application/json",
        "Content-Type": "text/plain",
    }
    full_sql = f"USE NS {SURREAL_NS} DB {SURREAL_DB};\n{sql}"

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

    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                response = await client.post(
                    SURREAL_URL,
                    content=full_sql,
                    headers=headers,
                    auth=SURREAL_AUTH,
                    timeout=30.0,
                )
                if response.status_code >= 400:
                    error_msg = response.text
                    print(f"[ERROR] SurrealDB Error ({response.status_code}): {error_msg}")
                    raise httpx.HTTPStatusError(
                        f"SurrealDB error {response.status_code}: {error_msg}",
                        request=response.request,
                        response=response
                    )
                data = response.json()
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("status") == "ERR":
                            raise RuntimeError(
                                f"SurrealDB Error: {item.get('information') or item.get('result')} | SQL: {sql[:120]}"
                            )
                # success -> reset circuit state
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

    raise RuntimeError(
        f"SurrealDB unreachable after {max_retries} attempts (failures={_surreal_failure_count}, circuit={'open' if opened else 'closed'}): {last_exception}"
    )


def _extract_result(data: List[Dict], index: int = 1) -> List[Dict]:
    """Extract results from SurrealDB response."""
    if not isinstance(data, list):
        return []
    
    # Filter out connection info messages
    candidates = [
        item for item in data
        if isinstance(item, dict)
        and item.get("status") == "OK"
        and "result" in item
        and not (isinstance(item["result"], dict) and "database" in item["result"] and "namespace" in item["result"])
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
async def classify_endpoint(query: str = Query(..., description="The query to classify")):
    """Classify a query according to type and confidence"""
    classifier = QueryClassifier()
    query_type, confidence = classifier.classify(query)
    return {
        "query": query,
        "type": query_type,
        "confidence": confidence
    }


@app.get("/route")
async def route_endpoint(query: str = Query(..., description="The query to route")):
    """Route a query according to the routing policy"""
    classifier = QueryClassifier()
    policy = RoutingPolicy()
    
    query_type, confidence = classifier.classify(query)
    strategy = policy.get_strategy(query_type, confidence)
    
    return {
        "query": query,
        "classification": {
            "type": query_type,
            "confidence": confidence
        },
        "routing_strategy": strategy
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
        "classification": {
            "type": query_type,
            "confidence": confidence
        }
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
            "maintainer": "ready"
        }
    }


# =============================================================================
# Schicht 1 - Core Memory Operations
# =============================================================================

# Expose memory operations as HTTP endpoints
@app.post("/memory/store")
async def memory_store_endpoint(request_data: dict):
    """Stores a new event in the raw event log. Always persists, entropy gate decides KG extraction."""
    content = request_data.get("content", "")
    source = request_data.get("source", "user_input")
    metadata = request_data.get("metadata", None)
    
    embedding_service = get_embedding_service()
    embedding_list = await asyncio.to_thread(embedding_service.embed_for_storage, content)
    content_escaped = content.replace("'", "\\'")
    source_escaped = source.replace("'", "\\'")
    embedding_storage = "[" + ", ".join(str(v) for v in embedding_list) + "]"

    if metadata:
        meta_json = json.dumps(metadata)
        sql = (
            f"CREATE event SET content = '{content_escaped}', "
            f"source = '{source_escaped}', embedding = {embedding_storage}, "
            f"metadata = {meta_json};"
        )
    else:
        sql = (
            f"CREATE event SET content = '{content_escaped}', "
            f"source = '{source_escaped}', embedding = {embedding_storage};"
        )

    result = await _query_surreal(sql)
    event_result = _extract_result(result)
    if event_result and isinstance(event_result, list) and len(event_result) > 0 and isinstance(event_result[0], dict):
        event_id = event_result[0]["id"]
    elif event_result and isinstance(event_result, dict):
        event_id = event_result.get("id")
    else:
        event_id = None
    return {"event_id": event_id, "status": "stored", "source": source}


@app.post("/memory/query")
async def memory_query_endpoint(request_data: dict):
    """Routes a natural language query through the full pipeline: classify -> plan -> retrieve."""
    query = request_data.get("query", "")
    cost_budget = request_data.get("cost_budget", "auto")
    
    classifier = QueryClassifier()
    q_type, confidence = classifier.classify(query)

    policy = RoutingPolicy()
    strategy = policy.get_strategy(q_type, confidence)

    executor = RetrievalExecutor()
    plan = {"strategy": strategy.get("strategy"), "query": query}
    raw_results = await executor.execute(plan)

    entities = []
    facts = []
    events = []
    results = raw_results.get("result", [])
    if isinstance(results, dict) and "combined_results" in results:
        results = results["combined_results"]
    elif isinstance(results, dict):
        # Falls es ein Dictionary mit keys wie 'events', 'entities' ist
        new_results = []
        for key in ["events", "entities", "facts", "keyword_results", "vector_results", "temporal_results"]:
            if key in results and isinstance(results[key], list):
                new_results.extend(results[key])
        if not new_results and "result" in results:
             new_results = results["result"] if isinstance(results["result"], list) else [results["result"]]
        results = new_results

    for r in results:
        if isinstance(r, dict):
            clean_r = _clean_output(r)
            rid = clean_r.get("id", "")
            if rid.startswith("entity:"):
                entities.append(clean_r)
            elif rid.startswith("fact:"):
                facts.append(clean_r)
            elif rid.startswith("event:"):
                events.append(clean_r)

    return {
        "query": query,
        "classified_as": q_type,
        "confidence": confidence,
        "strategy": strategy["strategy"],
        "cost_budget": strategy["cost_budget"],
        "results": {
            "entities": entities,
            "facts": facts,
            "events": events,
        },
        "total": len(entities) + len(facts) + len(events),
    }

@mcp.tool()
async def memory_store(content: str, source: str = "user_input", metadata: Optional[Dict[str, Any]] = None) -> dict:
    """Stores a new event in the raw event log. Runs through entropy gate which logs decisions to gate_log for later calibration."""
    from src.extraction.entropy_gate import EntropyGate
    gate = EntropyGate()
    
    # ingest() schreibt ins Event-Log, evaluiert Entropy, loggt in gate_log
    event_id = await asyncio.to_thread(gate.ingest, content, source, True)
    
    return {"event_id": event_id, "status": "stored", "source": source, "gate": "active"}

@mcp.tool()
async def memory_query(query: str, cost_budget: str = "auto") -> dict:
    """Routes a natural language query through the full pipeline: classify -> plan -> retrieve."""
    classifier = QueryClassifier()
    q_type, confidence = classifier.classify(query)

    policy = RoutingPolicy()
    strategy: Dict[str, Any] = policy.get_strategy(q_type, confidence)

    executor = RetrievalExecutor()
    results_raw: Any = await executor.execute_strategy(strategy, query)
    results: List[Any] = []
    
    if isinstance(results_raw, dict) and "combined_results" in results_raw:
        combined = results_raw["combined_results"]
        if isinstance(combined, list):
            results = combined
        else:
            results = [combined]
    elif isinstance(results_raw, dict):
        new_results: List[Any] = []
        for key in ["events", "entities", "facts", "keyword_results", "vector_results", "temporal_results", "result"]:
            if key in results_raw and isinstance(results_raw[key], list):
                new_results.extend(results_raw[key])
        if not new_results and "result" in results_raw:
            val = results_raw["result"]
            new_results = val if isinstance(val, list) else [val]
        results = new_results
    elif isinstance(results_raw, list):
        results = results_raw

    # Mark budget usage finished
    btk = strategy.get("budget_tracker_key")
    if isinstance(btk, str):
        policy.finish_execution(btk)

    entities = []
    facts = []
    events = []
    for r in results:
        if isinstance(r, dict):
            # Clean the object
            clean_r = _clean_output(r)
            rid = clean_r.get("id", "")
            if rid and isinstance(rid, str):
                if rid.startswith("entity:"):
                    entities.append(clean_r)
                elif rid.startswith("fact:"):
                    facts.append(clean_r)
                elif rid.startswith("event:"):
                    events.append(clean_r)
                else:
                    events.append(clean_r)
            else:
                events.append(clean_r)
        elif isinstance(r, list):
            # Manche Strategien geben Listen von Listen zurück
            for item in r:
                if isinstance(item, dict):
                    clean_item = _clean_output(item)
                    rid = clean_item.get("id", "")
                    if rid and isinstance(rid, str):
                        if rid.startswith("entity:"):
                            entities.append(clean_item)
                        elif rid.startswith("fact:"):
                            facts.append(clean_item)
                        elif rid.startswith("event:"):
                            events.append(clean_item)
                        else:
                            events.append(clean_item)
                    else:
                        events.append(clean_item)

    return {
        "query": query,
        "classified_as": q_type,
        "confidence": confidence,
        "strategy": strategy["strategy"],
        "cost_budget": strategy["cost_budget"],
        "results": {
            "entities": entities,
            "facts": facts,
            "events": events,
        },
        "total": len(entities) + len(facts) + len(events),
    }


@mcp.tool()
async def memory_update(subject: str, predicate: str, new_value: str) -> dict:
    """Updates a fact in the KG via logical invalidation. Old fact gets valid_until, new fact created. 
    Automatically infers entity types (e.g., 'organization' for companies/projects, otherwise 'concept') if entities are auto-created."""
    subject_escaped = subject.replace("'", "\\'")
    predicate_escaped = predicate.replace("'", "\\'")
    new_value_escaped = new_value.replace("'", "\\'")

    def _infer_entity_type(name: str) -> str:
        """Simple heuristic to infer entity type from name."""
        lower = name.lower()
        if any(suffix in lower for suffix in ['corp', 'inc', 'ltd', 'gmbh', 'company', 'org', 'projekt', 'projekt']):
            return 'organization'
        return 'concept'

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
        return {"status": "error", "message": f"No existing fact found for subject='{subject}', predicate='{predicate}'"}

    subject_sql = f"SELECT id FROM entity WHERE name = '{subject_escaped}' LIMIT 1;"
    subject_result = await _query_surreal(subject_sql)
    subject_entities = _extract_result(subject_result, 1)

    if not subject_entities:
        return {"status": "error", "message": f"Subject entity '{subject}' does not exist"}

    object_sql = f"SELECT id FROM entity WHERE name = '{new_value_escaped}' LIMIT 1;"
    object_result = await _query_surreal(object_sql)
    object_entities = _extract_result(object_result, 1)

    if not object_entities:
        return {"status": "error", "message": f"Object entity '{new_value}' does not exist"}

    new_fact_id = None
    if subject_entities and object_entities:
        subject_id = subject_entities[0]["id"]
        object_id = object_entities[0]["id"]
        relate_sql = f"RELATE {subject_id}->fact->{object_id} SET predicate = '{predicate_escaped}', confidence = 1.0;"
        relate_result = await _query_surreal(relate_sql)
        new_fact = _extract_result(relate_result, 1)
        if new_fact:
            new_fact_id = new_fact[0]["id"]

    return {
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
async def event_log_search_endpoint(query: str = Query(..., description="Search query"), 
                                   since: Optional[str] = Query(None, description="Start date"), 
                                   until: Optional[str] = Query(None, description="End date"), 
                                   limit: int = Query(10, description="Result limit")):
    """Direct timeline query without router: search raw event log."""
    query_escaped = query.replace("'", "\\'")
    sql = f"SELECT * FROM event WHERE content @@ '{query_escaped}'"

    if since:
        since_escaped = since.replace("'", "\\'")
        sql += f" AND timestamp >= '{since_escaped}'"
    if until:
        until_escaped = until.replace("'", "\\'")
        sql += f" AND timestamp <= '{until_escaped}'"

    sql += f" ORDER BY timestamp DESC LIMIT {limit};"
    result = await _query_surreal(sql)
    events = _extract_result(result)
    
    # Remove embeddings from output
    clean_events = _clean_output(events)
            
    return {"events": clean_events, "count": len(clean_events)}


@app.get("/memory/kg_query")
async def kg_query_endpoint(subject: Optional[str] = Query(None, description="Subject to search for"), 
                           predicate: Optional[str] = Query(None, description="Predicate to search for"), 
                           at_time: Optional[str] = Query(None, description="Time to query at"),
                           limit: int = Query(20, description="Limit the number of results")):
    """Direct graph traversal: query facts by subject/predicate/time."""
    sql = "SELECT * FROM fact WHERE valid_until = NONE"

    if subject:
        subject_escaped = subject.replace("'", "\\'")
        sql += f" AND in.name CONTAINS '{subject_escaped}'"
    if predicate:
        predicate_escaped = predicate.replace("'", "\\'")
        sql += f" AND predicate = '{predicate_escaped}'"
    if at_time:
        at_time_escaped = at_time.replace("'", "\\'")
        sql += f" AND valid_from <= '{at_time_escaped}'"

    sql += f" LIMIT {limit};"
    result = await _query_surreal(sql)
    facts = _extract_result(result)

    entities = []
    seen_ids = set()
    for fact in facts:
        for key in ("in", "out"):
            eid = fact.get(key)
            if isinstance(eid, dict) and eid.get("id") not in seen_ids:
                entities.append(eid)
                seen_ids.add(eid.get("id"))

    return {"facts": facts, "entities": entities, "count": len(facts)}


@mcp.tool()
async def event_log_search(query: str, since: Optional[str] = None, until: Optional[str] = None, limit: int = 10) -> dict:
    """Direct timeline query without router: search raw event log."""
    query_escaped = query.replace("'", "\\'")
    # Hybrid search: Try full-text index (@@), fallback to CONTAINS
    sql = f"SELECT * FROM event WHERE (content @@ '{query_escaped}' OR content CONTAINS '{query_escaped}') AND (forgotten IS NONE OR forgotten = false)"

    if since:
        since_escaped = since.replace("'", "\\'")
        sql += f" AND timestamp >= '{since_escaped}'"
    if until:
        until_escaped = until.replace("'", "\\'")
        sql += f" AND timestamp <= '{until_escaped}'"

    sql += f" ORDER BY timestamp DESC LIMIT {limit};"
    result = await _query_surreal(sql)
    events = _extract_result(result, 1)
    
    # Remove embeddings from output
    clean_events = _clean_output(events)
            
    return {"events": clean_events, "count": len(clean_events)}


@mcp.tool()
async def kg_query(subject: Optional[str] = None, predicate: Optional[str] = None, at_time: Optional[str] = None, limit: int = 20) -> dict:
    """Direct graph traversal: query facts by subject/predicate/time. 
    Returns associated entities with their inferred types (e.g., 'organization', 'concept')."""
    # Use a simpler query first to ensure compatibility
    sql = "SELECT * FROM fact WHERE (valid_until = NONE OR valid_until = NULL)"
    
    # Optional forgotten filter
    sql += " AND (forgotten = NONE OR forgotten = false)"

    if subject:
        subject_escaped = subject.replace("'", "\\'")
        # Robust check for subject name via the 'in' edge
        sql += f" AND (in.name CONTAINS '{subject_escaped}' OR in = '{subject_escaped}')"
    if predicate:
        predicate_escaped = predicate.replace("'", "\\'")
        sql += f" AND predicate = '{predicate_escaped}'"
    if at_time:
        at_time_escaped = at_time.replace("'", "\\'")
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

    return {"facts": facts, "entities": entities, "count": len(facts)}


@mcp.tool()
async def semantic_search(query: str, top_k: int = 5) -> dict:
    """Pure vector search without KG."""
    embedding_service = get_embedding_service()
    query_vector = await asyncio.to_thread(embedding_service.embed_for_query, query)
    
    # Format query_vector as a string for SurrealQL
    query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

    # Use SurrealDB's native vector::similarity::cosine function
    # We add a dimension check to avoid errors if some events have different embedding sizes
    sql = f"""
    SELECT id, content, vector::similarity::cosine(embedding, {query_vector_str}) AS score
    FROM event
    WHERE embedding IS NOT NONE 
      AND (forgotten IS NONE OR forgotten = false)
      AND array::len(embedding) = {len(query_vector)}
    ORDER BY score DESC
    LIMIT {top_k};
    """
    result = await _query_surreal(sql)
    events = _extract_result(result, 1)

    # Clean the output (remove embedding field if present)
    clean_events = [_clean_output(event) for event in events]

    return {"events": clean_events, "count": len(clean_events)}


# =============================================================================
# Schicht 3 - Introspection Tools
# =============================================================================

@app.post("/memory/semantic_search")
async def semantic_search_endpoint(request_data: dict):
    """Pure vector search without KG."""
    query = request_data.get("query", "")
    top_k = request_data.get("top_k", 5)
    
    embedding_service = get_embedding_service()
    query_vector = await asyncio.to_thread(embedding_service.embed_for_query, query)
    
    # Format query_vector as a string for SurrealQL
    query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

    # Use SurrealDB's native vector::similarity::cosine function
    # We add a dimension check to avoid errors if some events have different embedding sizes
    sql = f"""
    SELECT id, content, vector::similarity::cosine(embedding, {query_vector_str}) AS score
    FROM event
    WHERE embedding IS NOT NONE 
      AND (forgotten IS NONE OR forgotten = false)
      AND array::len(embedding) = {len(query_vector)}
    ORDER BY score DESC
    LIMIT {top_k};
    """
    result = await _query_surreal(sql)
    events = _extract_result(result, 1)

    # Clean the output (remove embedding field if present)
    clean_events = [_clean_output(event) for event in events]

    return {"events": clean_events, "count": len(clean_events)}


@mcp.tool()
async def memory_stats() -> dict:
    """Returns statistics about the memory system."""
    # Use separate queries instead of multi-statement to avoid indexing issues
    event_sql = "SELECT count() AS count FROM event WHERE (forgotten = false OR forgotten IS NONE) GROUP ALL;"
    entity_sql = "SELECT count() AS count FROM entity WHERE (forgotten = false OR forgotten IS NONE) GROUP ALL;"
    fact_sql = "SELECT count() AS count FROM fact WHERE (valid_until IS NONE OR valid_until = NONE) AND (forgotten = false OR forgotten IS NONE) GROUP ALL;"
    oldest_sql = "SELECT timestamp FROM event WHERE (forgotten = false OR forgotten IS NONE) ORDER BY timestamp ASC LIMIT 1;"
    newest_sql = "SELECT timestamp FROM event WHERE (forgotten = false OR forgotten IS NONE) ORDER BY timestamp DESC LIMIT 1;"
    gate_total_sql = "SELECT count() AS count FROM gate_log GROUP ALL;"
    gate_extracted_sql = "SELECT count() AS count FROM gate_log WHERE decision = 'extract' GROUP ALL;"

    stats = {
        "event_count": 0,
        "entity_count": 0,
        "fact_count": 0,
        "oldest_event": None,
        "newest_event": None,
        "gate_pass_rate": 0.0
    }

    try:
        event_result = _extract_result(await _query_surreal(event_sql), 1)
        if event_result and isinstance(event_result, list):
            stats["event_count"] = event_result[0].get("count", 0)
    except Exception as e:
        print(f"[WARN] memory_stats event count failed: {e}")

    try:
        entity_result = _extract_result(await _query_surreal(entity_sql), 1)
        if entity_result and isinstance(entity_result, list):
            stats["entity_count"] = entity_result[0].get("count", 0)
    except Exception as e:
        print(f"[WARN] memory_stats entity count failed: {e}")

    try:
        fact_result = _extract_result(await _query_surreal(fact_sql), 1)
        if fact_result and isinstance(fact_result, list):
            stats["fact_count"] = fact_result[0].get("count", 0)
    except Exception as e:
        print(f"[WARN] memory_stats fact count failed: {e}")

    try:
        oldest_result = _extract_result(await _query_surreal(oldest_sql), 1)
        if oldest_result and isinstance(oldest_result, list):
            stats["oldest_event"] = oldest_result[0].get("timestamp")
    except Exception:
        pass

    try:
        newest_result = _extract_result(await _query_surreal(newest_sql), 1)
        if newest_result and isinstance(newest_result, list):
            stats["newest_event"] = newest_result[0].get("timestamp")
    except Exception:
        pass

    try:
        gate_total_result = _extract_result(await _query_surreal(gate_total_sql), 1)
        gate_extracted_result = _extract_result(await _query_surreal(gate_extracted_sql), 1)
        gate_total = gate_total_result[0].get("count", 0) if gate_total_result and isinstance(gate_total_result, list) else 0
        gate_extracted = gate_extracted_result[0].get("count", 0) if gate_extracted_result and isinstance(gate_extracted_result, list) else 0
        stats["gate_pass_rate"] = round(gate_extracted / gate_total, 3) if gate_total > 0 else 0.0
    except Exception:
        pass

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
    """Returns statistics about the memory system."""
    event_count_sql = "SELECT count() AS event_count FROM event;"
    entity_count_sql = "SELECT count() AS entity_count FROM entity;"
    fact_count_sql = "SELECT count() AS fact_count FROM fact WHERE valid_until = NONE;"

    event_count = _extract_result(await _query_surreal(event_count_sql), 1)
    entity_count = _extract_result(await _query_surreal(entity_count_sql), 1)
    fact_count = _extract_result(await _query_surreal(fact_count_sql), 1)

    stats = {
        "event_count": event_count[0]["event_count"] if event_count else 0,
        "entity_count": entity_count[0]["entity_count"] if entity_count else 0,
        "fact_count": fact_count[0]["fact_count"] if fact_count else 0,
    }

    oldest_sql = "SELECT timestamp FROM event ORDER BY timestamp ASC LIMIT 1;"
    newest_sql = "SELECT timestamp FROM event ORDER BY timestamp DESC LIMIT 1;"
    oldest_result = _extract_result(await _query_surreal(oldest_sql), 1)
    newest_result = _extract_result(await _query_surreal(newest_sql), 1)
    if oldest_result:
        stats["oldest_event"] = oldest_result[0].get("timestamp")
    if newest_result:
        stats["newest_event"] = newest_result[0].get("timestamp")

    total_sql = "SELECT count() AS total FROM gate_log;"
    extracted_sql = "SELECT count() AS extracted FROM gate_log WHERE decision = 'extract';"
    total_result = _extract_result(await _query_surreal(total_sql), 1)
    extracted_result = _extract_result(await _query_surreal(extracted_sql), 1)
    if total_result:
        total = total_result[0].get("total", 0)
        extracted = extracted_result[0].get("extracted", 0) if extracted_result else 0
        stats["gate_pass_rate"] = round(extracted / total, 3) if total > 0 else 0.0

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
async def memory_forget(event_id: Optional[str] = None, entity: Optional[str] = None, reason: str = "") -> dict:
    """Forgets a memory by event_id or entity."""
    if event_id:
        event_escaped = event_id.replace("'", "\\'")
        check_sql = f"SELECT id FROM {event_escaped} LIMIT 1;"
        check_result = await _query_surreal(check_sql)
        events = _extract_result(check_result, 1)
        if not events:
            return {"status": "error", "message": f"Event '{event_id}' not found"}
        sql = f"UPDATE {event_escaped} SET forgotten = true;"
        if reason:
            reason_escaped = reason.replace("'", "\\'")
            sql = f"UPDATE {event_escaped} SET forgotten = true, forget_reason = '{reason_escaped}';"
        await _query_surreal(sql)
        return {"forgotten_id": event_id, "type": "event", "reason": reason}
    
    if entity:
        entity_escaped = entity.replace("'", "\\'")
        if entity_escaped.startswith("entity:"):
            check_sql = f"SELECT id FROM {entity_escaped} LIMIT 1;"
            check_result = await _query_surreal(check_sql)
            found = _extract_result(check_result, 1)
            if not found:
                return {"status": "error", "message": f"Entity '{entity}' not found"}
            entity_id = entity_escaped
        else:
            find_sql = f"SELECT id FROM entity WHERE name CONTAINS '{entity_escaped}' LIMIT 1;"
            find_result = await _query_surreal(find_sql)
            entities = _extract_result(find_result, 1)
            if not entities:
                return {"status": "error", "message": f"Entity '{entity}' not found"}
            entity_id = entities[0]["id"]
        sql = f"UPDATE {entity_id} SET forgotten = true;"
        if reason:
            reason_escaped = reason.replace("'", "\\'")
            sql = f"UPDATE {entity_id} SET forgotten = true, forget_reason = '{reason_escaped}';"
        await _query_surreal(sql)
        return {"forgotten_id": entity_id, "type": "entity", "reason": reason}

    return {"error": "No valid event_id or entity provided", "reason": reason}


@mcp.tool()
async def memory_consolidate(scope: str = "local", entity: Optional[str] = None, delete_stale: bool = False) -> dict:
    """Consolidates memory entries. When delete_stale=True, physically removes stale facts from the database."""
    maintainer = ConservativeMaintainer(debounce_seconds=0)

    if scope == "entity" and entity:
        entity_escaped = entity.replace("'", "''")
        find_sql = f"SELECT id FROM entity WHERE name CONTAINS '{entity_escaped}' LIMIT 1;"
        find_result_data = await _query_surreal(find_sql)
        find_result = _extract_result(find_result_data, 1)
        if find_result:
            entity_id = find_result[0]["id"]
            maintainer.queue_patch_update(entity_id, {"last_consolidated": datetime.now(timezone.utc).isoformat()})
            await maintainer.flush_pending()
            return {"scope": scope, "entity": entity, "entity_id": entity_id, "status": "consolidated"}

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
            "status": "cleaned" if (delete_stale and deleted_count > 0) else "reviewed"
        }

    return {"error": "Invalid scope. Use 'local' or 'entity' with entity name.", "scope": scope}


if __name__ == "__main__":
    # Ensure schema is loaded before starting servers
    asyncio.run(ensure_schema_loaded())
    
    # Pre-load embedding service to avoid hang on first tool call
    get_embedding_service()
    
    # Run FastMCP stdio server (for MCP clients)
    mcp.run()
