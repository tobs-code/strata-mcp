# -*- coding: utf-8 -*-
"""
Core infrastructure for the STRATA Memory Control Plane
Handles connection management, resilience patterns, and basic utilities
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

# Standard imports
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Our components
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP
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

# Maximum content length constant
MAX_CONTENT_LENGTH = 100_000

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
    from src.extraction.embedding_service import get_embedding_service
    service = get_embedding_service()
    vector = await asyncio.to_thread(service.embed_for_query, query)
    _store_embedding_cache(query, vector)
    return vector


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

        except Exception as e:
            # Log the exception instead of silent fail
            print(f"[ERROR] Background reconnect task error: {e}")

        await asyncio.sleep(_RECONNECT_INTERVAL)


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
    if not await check_schema_exists():
        print("[INFO] STRATA schema not found. Loading automatically...")

        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        load_script = os.path.join(project_root, "scripts", "load_schema_optimized.py")

        if os.path.exists(load_script):
            print(f"   Using existing load script: {load_script}")
            try:
                import subprocess

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
    else:
        print("[OK] STRATA schema already loaded")

    # Ensure entity table has all required fields (in case schema was loaded without them)
    try:
        required_fields = [
            "DEFINE FIELD OVERWRITE name ON entity TYPE string;",
            "DEFINE FIELD OVERWRITE type ON entity TYPE string;",
            "DEFINE FIELD OVERWRITE embedding ON entity TYPE option<array>;",
            "DEFINE FIELD OVERWRITE metadata ON entity TYPE option<object>;",
            "DEFINE FIELD OVERWRITE forgotten ON entity TYPE bool DEFAULT false;",
            "DEFINE FIELD OVERWRITE forget_reason ON entity TYPE option<string>;",
            "DEFINE FIELD OVERWRITE created_at ON entity TYPE option<datetime> DEFAULT time::now();",
            "DEFINE FIELD OVERWRITE updated_at ON entity TYPE option<datetime> DEFAULT time::now();",
        ]
        for field_def in required_fields:
            await _query_surreal(field_def)
    except Exception as e:
        print(f"   [WARN] Entity field sync failed (non-fatal): {e}")

    # Backfill missing timestamps on existing entities
    try:
        await _query_surreal(
            "UPDATE entity SET created_at = time::now() WHERE created_at IS NONE;"
        )
        await _query_surreal(
            "UPDATE entity SET updated_at = time::now() WHERE updated_at IS NONE;"
        )
        backfilled = _extract_result(
            await _query_surreal(
                "SELECT count() AS c FROM (SELECT * FROM entity WHERE created_at = time::now()) GROUP ALL;"
            ), 1
        )
        count = backfilled[0].get("c", 0) if backfilled else 0
        if count > 0:
            print(f"[OK] Backfilled timestamps for {count} entities")
    except Exception as e:
        print(f"   [WARN] Timestamp backfill failed (non-fatal): {e}")

    # ── Automatic data migration ──────────────────────────────────────
    # Apply pending schema/data migrations in version order.
    try:
        from src.mcp.migrations import MigrationEngine, _register_builtin
        engine = MigrationEngine(_query_surreal)
        _register_builtin(engine)
        logs = await engine.apply_all()
        for line in logs:
            print(f"[MIGRATION] {line}")
    except Exception as e:
        print(f"   [WARN] Migration check failed (non-fatal): {e}")