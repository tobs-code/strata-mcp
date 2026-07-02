-- ========================================
-- SurrealDB 3 Schema: Agent-Native Memory System
-- Basierend auf Zhou et al. 2026 (arXiv:2606.24775)
-- ========================================

-- Namespace & Database definieren
USE NS strata DB strata;

-- ========================================
-- ① Immutable Raw Event Log
-- ========================================
-- Single Source of Truth für "wann passierte was"
-- Append-Only: Niemals löschen, niemals überschreiben!
DEFINE TABLE event SCHEMALESS;
DEFINE FIELD timestamp ON event TYPE datetime DEFAULT time::now();
DEFINE FIELD source ON event TYPE string;  -- z.B. "user_input", "system_event", "api_response"
DEFINE FIELD content ON event TYPE string; -- Der rohe Inhalt
DEFINE FIELD embedding ON event TYPE option<vector(f32, 384)>;
DEFINE FIELD metadata ON event TYPE option<object>;
DEFINE FIELD forgotten ON event TYPE bool DEFAULT false;
DEFINE FIELD forget_reason ON event TYPE option<string>;

-- Indizes für den Event Log
DEFINE INDEX event_timestamp ON event COLUMNS timestamp;
DEFINE INDEX event_source ON event COLUMNS source;
DEFINE ANALYZER event_analyzer TOKENIZERS class FILTERS lowercase, ascii, snowball(english);
DEFINE INDEX event_content_ft ON event FIELDS content FULLTEXT ANALYZER event_analyzer BM25 HIGHLIGHTS;
-- Vector Index für SurrealDB 3 (HNSW statt MTREE, da MTREE in v3 entfernt)
DEFINE INDEX event_embedding_vec ON event FIELDS embedding HNSW DIMENSION 384 DIST COSINE;

-- ========================================
-- ② Knowledge Graph mit Temporal Validity
-- ========================================
-- Fakten als timestamped Triples mit logischer Invalidierung

-- Entitäten (Knoten)
DEFINE TABLE entity SCHEMAFULL;
DEFINE FIELD name ON entity TYPE string;
DEFINE FIELD type ON entity TYPE string; -- z.B. "person", "organization", "concept"
DEFINE FIELD embedding ON entity TYPE option<array>; -- For vector similarity search (array of f32)
DEFINE FIELD metadata ON entity TYPE option<object>;
DEFINE FIELD forgotten ON entity TYPE bool DEFAULT false;
DEFINE FIELD forget_reason ON entity TYPE option<string>;
DEFINE FIELD created_at ON entity TYPE datetime DEFAULT time::now();
DEFINE FIELD updated_at ON entity TYPE datetime DEFAULT time::now();

-- Fakten als Kanten mit Gültigkeitszeitraum
-- Pattern: (subject)-[edge:predicate {valid_from, valid_until}]->(object)
DEFINE TABLE fact SCHEMALESS;
DEFINE FIELD in ON fact TYPE option<record(entity)>;
DEFINE FIELD out ON fact TYPE option<record(entity)>;
DEFINE FIELD predicate ON fact TYPE string; -- z.B. "works_at", "located_in", "friend_of"
DEFINE FIELD valid_from ON fact TYPE datetime DEFAULT time::now();
DEFINE FIELD valid_until ON fact TYPE option<datetime> DEFAULT NONE;
DEFINE FIELD source_event ON fact TYPE option<record(event)>; -- Verweis auf Ursprung-Event
DEFINE FIELD confidence ON fact TYPE option<float> DEFAULT 1.0;
DEFINE FIELD metadata ON fact TYPE option<object>;

-- Indizes für den KG
DEFINE INDEX entity_name ON entity COLUMNS name;
DEFINE INDEX entity_type ON entity COLUMNS type;
-- Vector Index für Entity-Deduplizierung (Phase 3)
DEFINE INDEX entity_embedding_vec ON entity FIELDS embedding HNSW DIMENSION 384 DIST COSINE;
DEFINE INDEX fact_valid ON fact COLUMNS valid_from, valid_until;
DEFINE INDEX fact_subject ON fact COLUMNS in;
DEFINE INDEX fact_object ON fact COLUMNS out;

-- ========================================
-- ③ Retrieval Cache (für Hybrid Index)
-- ========================================
-- Optional, aber nützlich für schnelle Wiederholabfragen
DEFINE TABLE retrieval_cache SCHEMALESS;
DEFINE FIELD query_hash ON retrieval_cache TYPE string;
DEFINE FIELD result ON retrieval_cache TYPE array;
DEFINE FIELD timestamp ON retrieval_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD ttl ON retrieval_cache TYPE duration DEFAULT 1h;

-- Index für den Cache
DEFINE INDEX cache_query ON retrieval_cache COLUMNS query_hash;

-- ========================================
-- ④ Gate Log für Entropy-Gating-Decisions
-- ========================================
-- Loggt alle Gate-Entscheidungen für spätere Kalibrierung
DEFINE TABLE gate_log SCHEMAFULL;
DEFINE FIELD content_hash ON gate_log TYPE string;
DEFINE FIELD text_score ON gate_log TYPE float; -- Normalisierte Text-Entropy
DEFINE FIELD novelty ON gate_log TYPE float; -- Embedding-Novelty
DEFINE FIELD gate_score ON gate_log TYPE float; -- Composite Score
DEFINE FIELD decision ON gate_log TYPE string; -- "extract" or "skip"
DEFINE FIELD ts ON gate_log TYPE datetime DEFAULT time::now();

-- Index für Gate Log
DEFINE INDEX gate_log_ts ON gate_log COLUMNS ts;
DEFINE INDEX gate_log_decision ON gate_log COLUMNS decision;