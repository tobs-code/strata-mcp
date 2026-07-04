"""
Automatic data migration for breaking schema changes.

Tracks applied migrations in a `_schema_migrations` table in SurrealDB.
On server startup, compares current version against registered migrations
and applies any pending ones in order.
"""

import asyncio
import hashlib
import inspect
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration definition
# ---------------------------------------------------------------------------

class Migration:
    def __init__(
        self,
        version: int,
        description: str,
        apply_fn: Callable[..., Coroutine[Any, Any, None]],
    ):
        if version < 1:
            raise ValueError("version must be >= 1")
        self.version = version
        self.description = description
        self.apply_fn = apply_fn

    @property
    def checksum(self) -> str:
        h = hashlib.sha256()
        h.update(str(self.version).encode())
        h.update(self.description.encode())
        try:
            source = inspect.getsource(self.apply_fn)
            h.update(source.encode())
        except (OSError, TypeError):
            h.update(str(id(self.apply_fn)).encode())
        return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Migration engine
# ---------------------------------------------------------------------------

MIGRATIONS_TABLE = "_schema_migrations"


class MigrationEngine:
    def __init__(self, query_fn: Callable[..., Coroutine[Any, Any, Any]]):
        self._query = query_fn
        self._registry: Dict[int, Migration] = {}

    def register(self, migration: Migration):
        if migration.version in self._registry:
            raise ValueError(f"Migration version {migration.version} already registered")
        self._registry[migration.version] = migration

    def registered_versions(self) -> List[int]:
        return sorted(self._registry.keys())

    async def _ensure_tracking_table(self):
        sql = f"""
        DEFINE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} SCHEMAFUL;
        DEFINE FIELD IF NOT EXISTS version ON {MIGRATIONS_TABLE} TYPE int;
        DEFINE FIELD IF NOT EXISTS description ON {MIGRATIONS_TABLE} TYPE string;
        DEFINE FIELD IF NOT EXISTS checksum ON {MIGRATIONS_TABLE} TYPE string;
        DEFINE FIELD IF NOT EXISTS applied_at ON {MIGRATIONS_TABLE} TYPE datetime DEFAULT time::now();
        """
        await self._query(sql)

    async def _applied_versions(self) -> Dict[int, Dict[str, Any]]:
        raw = await self._query(f"SELECT * FROM {MIGRATIONS_TABLE} ORDER BY version ASC;")
        if not raw:
            return {}
        rows = self._extract_result(raw)
        applied: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            if isinstance(row, dict) and "version" in row:
                applied[int(row["version"])] = row
        return applied

    @staticmethod
    def _extract_result(data: Any) -> List[Dict]:
        """Extract data rows from SurrealDB multi-statement response."""
        if not isinstance(data, list):
            return []
        # Find the last OK result entry (skip USE NS response at index 0)
        for item in reversed(data):
            if isinstance(item, dict) and item.get("status") == "OK":
                r = item.get("result")
                if isinstance(r, list):
                    return r
                if isinstance(r, dict):
                    return [r]
        return []

    async def current_db_version(self) -> int:
        applied = await self._applied_versions()
        return max(applied.keys()) if applied else 0

    async def pending(self) -> List[Migration]:
        applied = await self._applied_versions()
        pending = []
        for v in sorted(self._registry.keys()):
            if v not in applied:
                pending.append(self._registry[v])
            else:
                record = applied[v]
                expected = self._registry[v].checksum
                actual = record.get("checksum", "")
                if actual and actual != expected:
                    logger.warning(
                        "Migration v%d checksum mismatch: expected=%s actual=%s",
                        v, expected, actual,
                    )
        return pending

    async def apply_all(self, dry_run: bool = False) -> List[str]:
        await self._ensure_tracking_table()
        pending = await self.pending()
        logs: List[str] = []
        for m in pending:
            log_msg = f"[v{m.version}] {m.description}"
            if dry_run:
                logger.info("DRY-RUN: would apply %s", log_msg)
                logs.append(f"DRY-RUN: {log_msg}")
                continue
            logger.info("Applying migration %s ...", log_msg)
            try:
                await m.apply_fn(self._query)
                await self._query(
                    f"CREATE {MIGRATIONS_TABLE} SET version = {m.version}, "
                    f"description = '{m.description.replace(chr(39), chr(39)+chr(39))}', "
                    f"checksum = '{m.checksum}';"
                )
                logger.info("  OK")
                logs.append(f"OK: {log_msg}")
            except Exception as e:
                logger.error("  FAILED: %s", e)
                logs.append(f"FAIL: {log_msg} – {e}")
                raise
        return logs


# ---------------------------------------------------------------------------
# Built-in migrations
# ---------------------------------------------------------------------------

def _register_builtin(engine: MigrationEngine):

    engine.register(Migration(
        version=1,
        description="Baseline schema – event, entity, fact, retrieval_cache, gate_log tables",
        apply_fn=_m001_baseline,
    ))

    engine.register(Migration(
        version=2,
        description="Add UNIQUE index on entity.name to prevent duplicate entities",
        apply_fn=_m002_entity_name_unique,
    ))


async def _m001_baseline(query):
    sql = r"""
DEFINE TABLE IF NOT EXISTS event SCHEMALESS;
DEFINE FIELD IF NOT EXISTS timestamp ON event TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS source ON event TYPE string;
DEFINE FIELD IF NOT EXISTS content ON event TYPE string;
DEFINE FIELD IF NOT EXISTS embedding ON event TYPE none | array;
DEFINE FIELD IF NOT EXISTS metadata ON event TYPE none | object;
DEFINE FIELD IF NOT EXISTS forgotten ON event TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS forget_reason ON event TYPE none | string;
DEFINE FIELD IF NOT EXISTS content_hash ON event TYPE none | string;
DEFINE INDEX IF NOT EXISTS event_timestamp ON event COLUMNS timestamp;
DEFINE INDEX IF NOT EXISTS event_source ON event COLUMNS source;
DEFINE ANALYZER IF NOT EXISTS event_analyzer TOKENIZERS class FILTERS lowercase, ascii, snowball(english);
DEFINE INDEX IF NOT EXISTS event_content_ft ON event FIELDS content FULLTEXT ANALYZER event_analyzer BM25 HIGHLIGHTS;
DEFINE INDEX IF NOT EXISTS event_embedding_vec ON event FIELDS embedding HNSW DIMENSION 768 DIST COSINE;
DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS name ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS type ON entity TYPE string;
DEFINE FIELD IF NOT EXISTS embedding ON entity TYPE none | array;
DEFINE FIELD IF NOT EXISTS metadata ON entity TYPE none | object;
DEFINE FIELD IF NOT EXISTS forgotten ON entity TYPE bool DEFAULT false;
DEFINE FIELD IF NOT EXISTS forget_reason ON entity TYPE none | string;
DEFINE FIELD IF NOT EXISTS created_at ON entity TYPE none | datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at ON entity TYPE none | datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS entity_name ON entity COLUMNS name;
DEFINE INDEX IF NOT EXISTS entity_type ON entity COLUMNS type;
DEFINE INDEX IF NOT EXISTS entity_embedding_vec ON entity FIELDS embedding HNSW DIMENSION 768 DIST COSINE;
DEFINE TABLE IF NOT EXISTS fact SCHEMALESS;
DEFINE FIELD IF NOT EXISTS predicate ON fact TYPE string;
DEFINE FIELD IF NOT EXISTS valid_from ON fact TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS valid_until ON fact TYPE none | datetime;
DEFINE FIELD IF NOT EXISTS confidence ON fact TYPE none | float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS metadata ON fact TYPE none | object;
DEFINE INDEX IF NOT EXISTS fact_valid ON fact COLUMNS valid_from, valid_until;
DEFINE INDEX IF NOT EXISTS fact_subject ON fact COLUMNS in;
DEFINE INDEX IF NOT EXISTS fact_object ON fact COLUMNS out;
DEFINE TABLE IF NOT EXISTS retrieval_cache SCHEMALESS;
DEFINE FIELD IF NOT EXISTS query_hash ON retrieval_cache TYPE string;
DEFINE FIELD IF NOT EXISTS result ON retrieval_cache TYPE array;
DEFINE FIELD IF NOT EXISTS timestamp ON retrieval_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS ttl ON retrieval_cache TYPE duration DEFAULT 1h;
DEFINE INDEX IF NOT EXISTS cache_query ON retrieval_cache COLUMNS query_hash;
DEFINE TABLE IF NOT EXISTS gate_log SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS content_hash ON gate_log TYPE string;
DEFINE FIELD IF NOT EXISTS text_score ON gate_log TYPE float;
DEFINE FIELD IF NOT EXISTS novelty ON gate_log TYPE float;
DEFINE FIELD IF NOT EXISTS gate_score ON gate_log TYPE float;
DEFINE FIELD IF NOT EXISTS decision ON gate_log TYPE string;
DEFINE FIELD IF NOT EXISTS reason ON gate_log TYPE string;
DEFINE FIELD IF NOT EXISTS threshold ON gate_log TYPE float;
DEFINE FIELD IF NOT EXISTS ts ON gate_log TYPE datetime DEFAULT time::now();
DEFINE INDEX IF NOT EXISTS gate_log_ts ON gate_log COLUMNS ts;
DEFINE INDEX IF NOT EXISTS gate_log_decision ON gate_log COLUMNS decision;
"""
    for stmt in sql.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            await query(stmt + ";")


async def _m002_entity_name_unique(query):
    """Drop existing non-unique index and recreate as UNIQUE.
    Before applying, merges any duplicate entities by keeping the oldest and
    re-linking all facts to it, then removing the newer duplicates."""

    # 1. Find duplicate entity names (simple GROUP BY)
    dup_sql = "SELECT name, count() AS cnt FROM entity GROUP BY name;"
    raw = await query(dup_sql)
    if raw:
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            result = entry.get("result", [])
            if not isinstance(result, list):
                continue
            for row in result:
                cnt = row.get("cnt", 0)
                if not isinstance(cnt, int) or cnt < 2:
                    continue
                name = row.get("name", "")
                if not name:
                    continue
                name_escaped = name.replace("'", "''")
                list_sql = f"SELECT id, created_at FROM entity WHERE name = '{name_escaped}' ORDER BY created_at ASC;"
                raw_ids = await query(list_sql)
                ids = []
                if isinstance(raw_ids, list):
                    for e in raw_ids:
                        if isinstance(e, dict):
                            r = e.get("result", [])
                            if isinstance(r, list):
                                for item in r:
                                    if isinstance(item, dict) and "id" in item:
                                        ids.append(item["id"])
                if len(ids) < 2:
                    continue
                keep_id = ids[0]
                for remove_id in ids[1:]:
                    await query(f"UPDATE fact SET in = {keep_id} WHERE in = {remove_id};")
                    await query(f"UPDATE fact SET out = {keep_id} WHERE out = {remove_id};")
                    await query(f"DELETE {remove_id};")

    # 2. Drop old non-unique index
    await query("REMOVE INDEX entity_name ON entity;")

    # 3. Create UNIQUE index
    await query("DEFINE INDEX IF NOT EXISTS entity_name ON entity COLUMNS name UNIQUE;")
