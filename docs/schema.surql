-- ========================================
-- SurrealDB 3 Schema: Agent-Native Memory System
-- Basierend auf Zhou et al. 2026 (arXiv:2606.24775)
-- ========================================
-- ⚠️  BREAKING SCHEMA CHANGES
-- Dieses File ist die kanonische Referenz. Änderungen an bestehenden
-- Feldern/Tabellen werden über das Migration-System in src/mcp/migrations.py
-- als versionierte Migrationsschritte ausgerollt. Dieses File dient als
-- Source of Truth für Neuanlagen ("baseline") und Dokumentation.
-- ========================================

USE NS strata DB strata;

-- ========================================
-- ① Immutable Raw Event Log
-- ========================================
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

-- ========================================
-- ② Knowledge Graph mit Temporal Validity
-- ========================================
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

-- ========================================
-- ③ Retrieval Cache
-- ========================================
DEFINE TABLE IF NOT EXISTS retrieval_cache SCHEMALESS;
DEFINE FIELD IF NOT EXISTS query_hash ON retrieval_cache TYPE string;
DEFINE FIELD IF NOT EXISTS result ON retrieval_cache TYPE array;
DEFINE FIELD IF NOT EXISTS timestamp ON retrieval_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS ttl ON retrieval_cache TYPE duration DEFAULT 1h;
DEFINE INDEX IF NOT EXISTS cache_query ON retrieval_cache COLUMNS query_hash;

-- ========================================
-- ④ Gate Log
-- ========================================
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

-- ========================================
-- ⑤ Schema Migration Tracking
-- ========================================
DEFINE TABLE IF NOT EXISTS _schema_migrations SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS version ON _schema_migrations TYPE int;
DEFINE FIELD IF NOT EXISTS description ON _schema_migrations TYPE string;
DEFINE FIELD IF NOT EXISTS checksum ON _schema_migrations TYPE string;
DEFINE FIELD IF NOT EXISTS applied_at ON _schema_migrations TYPE datetime DEFAULT time::now();