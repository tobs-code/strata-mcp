-- ========================================
-- Testdaten für das Agent-Native Memory System
-- ========================================

USE NS strata DB strata;

-- 1. Events hinzufügen (Append-Only)
CREATE event SET
    source = "user_input",
    content = "My name is Alice and I work at Acme Corp.",
    timestamp = time::now() - 2h;

CREATE event SET
    source = "user_input",
    content = "Today I signed a contract with a new client.",
    timestamp = time::now() - 1h;

CREATE event SET
    source = "user_input",
    content = "Tomorrow I will discuss the project with Bob.",
    timestamp = time::now() - 30m;

-- 2. Entitäten erstellen
CREATE entity:alice SET
    name = "Alice",
    type = "person";

CREATE entity:bob SET
    name = "Bob",
    type = "person";

CREATE entity:acme_corp SET
    name = "Acme Corp",
    type = "organization";

CREATE entity:project_x SET
    name = "Project X",
    type = "concept";

-- 3. Facts (Kanten) mit Temporal Validity hinzufügen
CREATE fact SET
    in = entity:alice,
    out = entity:acme_corp,
    predicate = "works_at",
    valid_from = time::now() - 5d,
    confidence = 1.0;

CREATE fact SET
    in = entity:alice,
    out = entity:bob,
    predicate = "knows",
    valid_from = time::now() - 3d,
    confidence = 0.9;

CREATE fact SET
    in = entity:alice,
    out = entity:project_x,
    predicate = "working_on",
    valid_from = time::now() - 2d,
    confidence = 0.95;
