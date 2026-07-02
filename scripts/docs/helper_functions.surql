-- ========================================
-- SurrealDB Helper Functions
-- ========================================

USE NS strata DB strata;

-- 1. fn::active_fact($subject, $predicate)
-- Holt alle aktiven Facts für ein Subjekt und Prädikat
DEFINE FUNCTION fn::active_fact($subject: record, $predicate: string) {
    RETURN SELECT *
    FROM fact
    WHERE in = $subject
      AND predicate = $predicate
      AND valid_until = NONE
    ORDER BY valid_from DESC;
};

-- 2. fn::facts_at($subject, $predicate, $time)
-- Holt Facts, die zu einem bestimmten Zeitpunkt gültig waren
DEFINE FUNCTION fn::facts_at($subject: record, $predicate: string, $time: datetime) {
    RETURN SELECT *
    FROM fact
    WHERE in = $subject
      AND predicate = $predicate
      AND valid_from <= $time
      AND (valid_until = NONE OR valid_until > $time)
    ORDER BY valid_from DESC;
};

-- 3. fn::temporal_context($entity, $window)
-- Holt temporalen Kontext für eine Entität in einem Zeitfenster
DEFINE FUNCTION fn::temporal_context($entity: record, $window: duration) {
    RETURN SELECT *
    FROM event
    WHERE (content CONTAINS $entity.name OR metadata.entity_id = $entity.id)
      AND timestamp >= time::now() - $window
    ORDER BY timestamp DESC;
};

-- 4. fn::invalidate_fact($fact_id)
-- Logische Invalidierung eines Facts (statt Löschen)
DEFINE FUNCTION fn::invalidate_fact($fact_id: record(fact)) {
    UPDATE $fact_id SET valid_until = time::now();
    RETURN "Fact invalidated successfully";
};

-- 5. fn::append_event($source, $content, $embedding, $metadata)
-- Hilfsfunktion zum Append eines neuen Events (garantiert immutable)
DEFINE FUNCTION fn::append_event($source: string, $content: string, $embedding: option<vector>, $metadata: option<object>) {
    RETURN CREATE event SET
        source = $source,
        content = $content,
        embedding = $embedding,
        metadata = $metadata;
};
