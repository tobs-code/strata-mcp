"""
Conservative Maintainer for sieveon
Implements conservative maintenance operations with lazy flushing and debounce
"""

import asyncio
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from src.extraction.entropy_gate import escape_surrealql

# Try to load environment variables from .env file
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # python-dotenv is not installed, skip loading .env file
    pass

# Standard imports
import json

SURREAL_URL = os.getenv("SURREALDB_URL", "http://127.0.0.1:8000/sql")
SURREAL_AUTH = (
    os.getenv("SURREALDB_USER", "root"),
    os.getenv("SURREALDB_PASS", "root"),
)
SURREAL_NS = os.getenv("SURREALDB_NS", "sieveon")
SURREAL_DB = os.getenv("SURREALDB_DB", "sieveon")

_shared_async_client = None
_async_client_lock = asyncio.Lock()


async def _get_async_client() -> httpx.AsyncClient:
    global _shared_async_client
    if _shared_async_client is None:
        async with _async_client_lock:
            if _shared_async_client is None:
                _shared_async_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
    return _shared_async_client


async def _query_surreal(sql: str) -> Any:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    full_sql = f"USE NS {SURREAL_NS} DB {SURREAL_DB};\n{sql}"
    client = await _get_async_client()
    try:
        response = await client.post(
            SURREAL_URL,
            content=full_sql,
            headers=headers,
            auth=SURREAL_AUTH,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("status") == "ERR":
                    raise RuntimeError(
                        f"SurrealDB Error: {item.get('information') or item.get('result')} | SQL: {sql[:120]}"
                    )
        return data
    except httpx.TimeoutException:
        raise RuntimeError(f"SurrealDB timeout: {sql[:120]}")
    except httpx.HTTPStatusError as e:
        raise RuntimeError(f"SurrealDB HTTP {e.response.status_code}: {sql[:120]}")


def _extract_result(data: List[Dict], index: int = 1) -> List[Dict]:
    """Extract results from SurrealDB response."""
    if not isinstance(data, list):
        return []
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
    if len(candidates) <= index:
        target = candidates[-1]
    else:
        target = candidates[index]
    result = target.get("result", [])
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return [result]
    return []


class ConservativeMaintainer:
    def __init__(self, debounce_seconds: int = 300):  # 5 minutes default
        self.debounce_seconds = debounce_seconds
        self.pending_updates = {}
        self.last_flush_time = time.time()

    async def perform_maintenance(self) -> Dict[str, Any]:
        """Perform conservative maintenance operations"""
        # First, flush any pending updates
        await self.flush_pending()

        # Then perform cleanup operations
        result = {
            "timestamp": datetime.now().isoformat(),
            "operations_performed": [],
            "stats": {},
        }

        # Clean up stale facts (those marked with valid_until)
        stale_facts_cleaned = await self._clean_stale_facts()
        result["operations_performed"].append(
            {"type": "stale_fact_cleanup", "count": stale_facts_cleaned}
        )

        # Consolidate similar events
        consolidated_events = await self._consolidate_events()
        result["operations_performed"].append(
            {"type": "event_consolidation", "count": consolidated_events}
        )

        # Update statistics
        result["stats"] = await self._get_memory_stats()

        return result

    async def queue_patch_update(self, entity_id: str, updates: Dict[str, Any]):
        """Queue a patch update to be applied later"""
        if entity_id not in self.pending_updates:
            self.pending_updates[entity_id] = {}
        self.pending_updates[entity_id].update(updates)

        # Schedule flush if debounce period has passed
        if time.time() - self.last_flush_time > self.debounce_seconds:
            await self.flush_pending()

    async def flush_pending(self):
        """Apply all pending updates"""
        if not self.pending_updates:
            return

        for entity_id, updates in self.pending_updates.items():
            try:
                # Build update query
                set_clauses = []
                for key, value in updates.items():
                    if isinstance(value, str):
                        escaped_value = escape_surrealql(value)
                        set_clauses.append(f"{key} = '{escaped_value}'")
                    else:
                        set_clauses.append(f"{key} = {json.dumps(value)}")

                update_sql = f"UPDATE {entity_id} SET {', '.join(set_clauses)};"
                await _query_surreal(update_sql)
            except Exception as e:
                print(f"Error applying pending update to {entity_id}: {e}")

        # Clear pending updates
        self.pending_updates.clear()
        self.last_flush_time = time.time()

    async def _clean_stale_facts(self) -> int:
        """Clean up facts that have been marked as stale"""
        try:
            # Find facts that are marked as invalid/stale
            sql = """
            SELECT * FROM fact
            WHERE valid_until != NONE
              AND valid_until < time::now()
            LIMIT 50;
            """
            result = await _query_surreal(sql)
            stale_facts = _extract_result(result)

            # Actually remove the stale facts (physical deletion)
            removed_count = 0
            for fact in stale_facts:
                fact_id = fact.get("id")
                if fact_id:
                    try:
                        delete_sql = f"DELETE {fact_id};"
                        await _query_surreal(delete_sql)
                        removed_count += 1
                    except Exception as e:
                        print(f"Could not delete stale fact {fact_id}: {e}")

            return removed_count
        except Exception as e:
            print(f"Error cleaning stale facts: {e}")
            return 0

    async def _consolidate_events(self) -> int:
        """Deduplicate events with identical content_hash within 1-hour windows."""
        try:
            sql = """
            SELECT id, content_hash, timestamp, source
            FROM event
            WHERE (forgotten IS NONE OR forgotten = false)
            ORDER BY timestamp DESC
            LIMIT 500;
            """
            result = await _query_surreal(sql)
            events = _extract_result(result)
            if not events:
                return 0

            from collections import defaultdict
            buckets = defaultdict(list)
            for ev in events:
                key = ev.get("content_hash") or ev.get("content", "")[:50]
                buckets[key].append(ev)

            consolidated_count = 0
            for key, group in buckets.items():
                if len(group) < 2:
                    continue
                group.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
                keep = group[0]
                for dup in group[1:]:
                    dup_id = dup.get("id")
                    if not dup_id:
                        continue
                    try:
                        forget_sql = f"UPDATE {dup_id} SET forgotten = true, forgotten_reason = 'consolidated_duplicate';"
                        await _query_surreal(forget_sql)
                        consolidated_count += 1
                    except Exception as e:
                        print(f"Could not consolidate event {dup_id}: {e}")

            return consolidated_count

        except Exception as e:
            print(f"Error consolidating events: {e}")
            return 0

    async def _get_memory_stats(self) -> Dict[str, Any]:
        """Get statistics about the memory system"""
        try:
            # Get event count
            result = await _query_surreal(
                "SELECT count() AS count FROM event GROUP ALL;"
            )
            extracted = _extract_result(result)
            event_count = extracted[0].get("count", 0) if extracted else 0

            # Get entity count
            result = await _query_surreal(
                "SELECT count() AS count FROM entity GROUP ALL;"
            )
            extracted = _extract_result(result)
            entity_count = extracted[0].get("count", 0) if extracted else 0

            # Get fact count
            result = await _query_surreal(
                "SELECT count() AS count FROM fact WHERE valid_until = NONE GROUP ALL;"
            )
            extracted = _extract_result(result)
            fact_count = extracted[0].get("count", 0) if extracted else 0

            return {
                "event_count": event_count,
                "entity_count": entity_count,
                "fact_count": fact_count,
            }
        except Exception as e:
            print(f"Error getting memory stats: {e}")
            return {}

    async def get_stale_facts(
        self, max_age_seconds: int = 86400
    ) -> List[Dict[str, Any]]:
        """Get facts that haven't been accessed in a while"""
        try:
            cutoff_time = datetime.now() - timedelta(seconds=max_age_seconds)
            sql = f"""
            SELECT * FROM fact
            WHERE last_accessed < '{cutoff_time.isoformat()}'
               OR last_accessed = NONE
            LIMIT 20;
            """
            result = await _query_surreal(sql)
            return _extract_result(result)
        except Exception as e:
            print(f"Error getting stale facts: {e}")
            return []


# Example usage
if __name__ == "__main__":

    async def test_maintainer():
        maintainer = ConservativeMaintainer()
        result = await maintainer.perform_maintenance()
        print("Maintenance completed:", result)

    asyncio.run(test_maintainer())
