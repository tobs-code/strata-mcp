# -*- coding: utf-8 -*-
"""
MCP Tools implementation
"""

import asyncio
import hashlib
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP
from src.extraction.entropy_gate import escape_surrealql

from .common_logic import _execute_query, _get_or_create_entity, _store_content
from .core import _clean_output, _embed_query, _extract_result, _query_surreal, mcp
from src.extraction.entity_utils import infer_entity_type, validate_predicate


@mcp.tool()
async def memory_store(
    content: str, source: str = "user_input", metadata: Optional[Dict[str, Any]] = None
) -> dict:
    """Stores a new event in the raw event log. Runs through entropy gate.

    ⚠️ LANGUAGE: The embedding model only supports English. Non-English content (e.g. German) produces noisy/broken entity extraction and poor search results.
    → ALWAYS translate non-English content to English BEFORE storing.
    """
    return await _store_content(content, source, debug=True, metadata=metadata)


@mcp.tool()
async def memory_query(query: str, cost_budget: str = "auto") -> dict:
    """Routes a natural language query through the full pipeline: classify → plan → retrieve."""
    return await _execute_query(query, cost_budget)


@mcp.tool()
async def memory_update(subject: str, predicate: str, new_value: str) -> dict:
    """Updates a fact in the KG via logical invalidation (valid_until). If no active fact exists,
    creates a new fact (upsert). Entities are created automatically if they don't exist."""
    subject_id = await _get_or_create_entity(subject)
    if not subject_id:
        return {
            "status": "error",
            "message": f"Subject entity '{subject}' does not exist and could not be created",
        }

    object_id = await _get_or_create_entity(new_value)
    if not object_id:
        return {
            "status": "error",
            "message": f"Object entity '{new_value}' does not exist and could not be created",
        }

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


@mcp.tool()
async def memory_stats(random_string: str = "") -> dict:
    """Returns statistics about the memory system."""
    forgotten_filter = "(forgotten = false OR forgotten IS NONE)"

    # Count events
    event_count_result = await _query_surreal(f"SELECT count() FROM event WHERE {forgotten_filter} GROUP ALL;")
    event_counts = _extract_result(event_count_result, 1)
    event_count = event_counts[0].get("count", 0) if event_counts else 0

    # Count entities
    entity_count_result = await _query_surreal(f"SELECT count() FROM entity WHERE {forgotten_filter} GROUP ALL;")
    entity_counts = _extract_result(entity_count_result, 1)
    entity_count = entity_counts[0].get("count", 0) if entity_counts else 0

    # Count facts
    fact_count_result = await _query_surreal("SELECT count() FROM fact WHERE (valid_until IS NONE OR valid_until = NONE) AND (forgotten = false OR forgotten IS NONE) GROUP ALL;")
    fact_counts = _extract_result(fact_count_result, 1)
    fact_count = fact_counts[0].get("count", 0) if fact_counts else 0

    # Get oldest and newest event timestamps
    oldest_result = await _query_surreal(f"SELECT timestamp FROM event WHERE {forgotten_filter} ORDER BY timestamp ASC LIMIT 1;")
    oldest_events = _extract_result(oldest_result, 1)
    oldest_event = oldest_events[0].get("timestamp") if oldest_events else None

    newest_result = await _query_surreal(f"SELECT timestamp FROM event WHERE {forgotten_filter} ORDER BY timestamp DESC LIMIT 1;")
    newest_events = _extract_result(newest_result, 1)
    newest_event = newest_events[0].get("timestamp") if newest_events else None

    # Calculate gate pass rate
    total_gate_logs_result = await _query_surreal(
        "SELECT count() FROM gate_log GROUP ALL;"
    )
    total_gate_logs = _extract_result(total_gate_logs_result, 1)
    total_decisions = total_gate_logs[0].get("count", 0) if total_gate_logs else 0

    extract_decisions_result = await _query_surreal(
        "SELECT count() FROM gate_log WHERE decision = 'extract' GROUP ALL;"
    )
    extract_decisions = _extract_result(extract_decisions_result, 1)
    extract_count = extract_decisions[0].get("count", 0) if extract_decisions else 0

    gate_pass_rate = extract_count / total_decisions if total_decisions > 0 else 0.0

    # Get recent gate decisions with reasons
    recent_gate_sql = """
    SELECT content_hash, decision, reason, gate_score, threshold, ts
    FROM gate_log
    ORDER BY ts DESC
    LIMIT 10;
    """
    recent_gate_result = await _query_surreal(recent_gate_sql)
    recent_gate_logs = _extract_result(recent_gate_result, 1)
    for g in recent_gate_logs:
        g.pop("content_hash", None)

    return {
        "event_count": event_count,
        "entity_count": entity_count,
        "fact_count": fact_count,
        "oldest_event": oldest_event,
        "newest_event": newest_event,
        "gate_pass_rate": gate_pass_rate,
        "total_gate_decisions": total_decisions,
        "extract_decisions": extract_count,
        "ignore_decisions": total_decisions - extract_count,
        "recent_gate_decisions": recent_gate_logs,
    }


@mcp.tool()
async def event_log_search(
    query: str,
    limit: int = 10,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
    include_forgotten: bool = False,
) -> dict:
    """Direct timeline query without router: hybrid search (BM25 + vector + RRF fusion).
    When include_forgotten=True, forgotten events are included and marked as such.
    """
    query_escaped = escape_surrealql(query)
    time_filter = ""
    if since:
        time_filter += f" AND timestamp >= '{escape_surrealql(since)}'"
    if until:
        time_filter += f" AND timestamp <= '{escape_surrealql(until)}'"

    if include_forgotten:
        forgotten_filter = "1=1"
    else:
        forgotten_filter = "(forgotten IS NONE OR forgotten = false)"

    if not query.strip():
        # If no query, just return recent events with offset
        sql = f"""
        SELECT id, content, timestamp, source, metadata, forgotten, forgotten_reason
        FROM event
        WHERE {forgotten_filter}
        {time_filter}
        ORDER BY timestamp DESC
        LIMIT {limit}
        START {offset};
        """
        result = await _query_surreal(sql)
        events = _extract_result(result, 1)
        for event in events:
            event["search_type"] = "recent"
        return {"events": _clean_output(events), "count": len(events)}

    fetch_limit = (offset + limit) * 4

    # 1) Lexical search via FTX index
    ftx_sql = f"""
    SELECT id, content, timestamp, source, metadata, forgotten, forgotten_reason, 'lexical' AS search_type
    FROM event
    WHERE content @@ '{query_escaped}'
      AND {forgotten_filter}
      {time_filter}
    LIMIT {fetch_limit};
    """

    # Start FTX query immediately (overlap with embedding computation)
    ftx_task = asyncio.create_task(_query_surreal(ftx_sql))

    # 2) Vector search — compute embedding while FTX runs
    try:
        query_vector = await _embed_query(query)
        query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

        vec_sql = f"""
        SELECT id, content, timestamp, source, metadata, forgotten, forgotten_reason,
               vector::similarity::cosine(embedding, {query_vector_str}) AS vec_score,
               'vector' AS search_type
        FROM event
        WHERE embedding IS NOT NONE
          AND {forgotten_filter}
          AND array::len(embedding) = {len(query_vector)}
          {time_filter}
        ORDER BY vec_score DESC
        LIMIT {fetch_limit};
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
        for item in sorted(fused.values(), key=lambda x: x["rrf"], reverse=True)[offset:offset + limit]
    ]
    events = _clean_output(sorted_events)

    return {"events": events, "count": len(events)}


@mcp.tool()
async def kg_query(
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    at_time: Optional[str] = None,
) -> dict:
    """Direct graph traversal: query facts by subject/predicate/time.
    Returns associated entities with their inferred types (e.g., 'organization', 'concept').
    Searches both directions — 'subject' matches entity name in subject or object position."""
    extra_clauses = ""

    if subject:
        subject_escaped = escape_surrealql(subject)
        extra_clauses = f"WHERE (in.name = '{subject_escaped}' OR out.name = '{subject_escaped}')"
    if predicate:
        predicate_escaped = escape_surrealql(predicate)
        if extra_clauses:
            extra_clauses += f" AND predicate = '{predicate_escaped}'"
        else:
            extra_clauses = f"WHERE predicate = '{predicate_escaped}'"
    if at_time:
        time_escaped = escape_surrealql(at_time)
        time_condition = (
            f"(valid_from <= '{time_escaped}' OR valid_from = NONE)"
            f" AND (valid_until >= '{time_escaped}' OR valid_until = NONE)"
        )
        if extra_clauses:
            extra_clauses += f" AND {time_condition}"
        else:
            extra_clauses = f"WHERE {time_condition}"

    # Only show active facts (not invalidated)
    valid_filter = "WHERE (valid_until IS NONE OR valid_until > time::now())"

    extra_clauses_stripped = ""
    if extra_clauses:
        extra_clauses_stripped = extra_clauses.lstrip()
        if extra_clauses_stripped.upper().startswith("WHERE"):
            extra_clauses_stripped = extra_clauses_stripped[5:].lstrip()
        if extra_clauses_stripped:
            extra_clauses_stripped = f"AND {extra_clauses_stripped}"

    sql = f"""
    SELECT
        id,
        in.name AS in_name,
        in.type AS in_type,
        in.id AS in_id,
        out.name AS out_name,
        out.type AS out_type,
        out.id AS out_id,
        predicate, confidence, valid_from, valid_until
    FROM fact
    {valid_filter} {extra_clauses_stripped}
    ORDER BY confidence DESC
    LIMIT 100;
    """

    result = await _query_surreal(sql)
    facts = _extract_result(result, 1)

    NOISY_PREDICATES = {"weakly_related", "mentions"}
    MIN_CONFIDENCE = 0.5

    enhanced_facts = []
    in_name_map = {}
    for fact in facts:
        pred = fact.get("predicate", "")
        conf = fact.get("confidence", 0) or 0
        if pred in NOISY_PREDICATES:
            continue
        if pred in ("co_occurs_with", "strongly_related", "related_to") and conf < MIN_CONFIDENCE:
            continue

        in_id = fact.pop("in_id", None)
        in_name = fact.pop("in_name", None)
        in_type = fact.pop("in_type", None)
        out_id = fact.pop("out_id", None)
        out_name = fact.pop("out_name", None)
        out_type = fact.pop("out_type", None)

        # Build name lookup for entity ID resolution
        for eid, ename, etype in [(in_id, in_name, in_type), (out_id, out_name, out_type)]:
            if eid and ename and eid not in in_name_map:
                in_name_map[eid] = (ename, etype or "")

        fact["in"] = {
            "id": in_id,
            "name": in_name if in_name else in_id,
            "type": in_type if in_type else "",
        }
        fact["out"] = {
            "id": out_id,
            "name": out_name if out_name else out_id,
            "type": out_type if out_type else "",
        }
        enhanced_facts.append(fact)

    return {
        "facts": _clean_output(enhanced_facts),
        "count": len(enhanced_facts),
        "query_params": {
            "subject": subject,
            "predicate": predicate,
            "at_time": at_time,
        },
    }


@mcp.tool()
async def semantic_search(query: str, top_k: int = 5) -> dict:
    """Hybrid search: Vector (semantic) + FTX (lexical) with RRF fusion.
    Deduplicates by content_hash, enriches with KG facts, filters repetitive noise.
    Returns normalized scores (0-1)."""
    if not query.strip():
        return {"events": [], "count": 0, "message": "Query cannot be empty"}

    query_escaped = escape_surrealql(query)
    query_vector = await _embed_query(query)
    query_vector_str = "[" + ", ".join(map(str, query_vector)) + "]"

    fetch_k = min(top_k * 6, 150)
    forgotten_filter = "(forgotten IS NONE OR forgotten = false)"

    # 1) Vector search (semantic)
    vec_sql = f"""
    SELECT id, content, timestamp, source, metadata, content_hash,
           vector::similarity::cosine(embedding, {query_vector_str}) AS vec_score
    FROM event
    WHERE embedding IS NOT NONE
      AND {forgotten_filter}
      AND array::len(embedding) = {len(query_vector)}
    ORDER BY vec_score DESC
    LIMIT {fetch_k};
    """
    vec_task = asyncio.create_task(_query_surreal(vec_sql))

    # 2) FTX search (lexical) — only if query has meaningful content
    ftx_task = None
    if query.strip():
        ftx_sql = f"""
        SELECT id, content, timestamp, source, metadata, content_hash
        FROM event
        WHERE content @@ '{query_escaped}'
          AND {forgotten_filter}
        LIMIT {fetch_k};
        """
        ftx_task = asyncio.create_task(_query_surreal(ftx_sql))

    vec_result = await vec_task
    vec_events = _extract_result(vec_result, 1) or []

    ftx_events = []
    if ftx_task:
        try:
            ftx_result = await ftx_task
            ftx_events = _extract_result(ftx_result, 1) or []
        except Exception:
            pass

    # 3) RRF fusion: combine vector + ftx results
    k = 60
    fused = {}  # content_hash -> {event, rrf_score, vec_score}
    seen_ids = set()

    for rank, ev in enumerate(vec_events):
        eid = ev.get("id")
        ch = ev.get("content_hash") or eid
        if ch in fused or eid in seen_ids:
            continue
        seen_ids.add(eid)
        vec_score = ev.get("vec_score")
        if not isinstance(vec_score, (int, float)):
            vec_score = 0.0
        fused[ch] = {
            "event": ev,
            "rrf": 1.0 / (k + rank),
            "vec_score": vec_score,
        }

    for rank, ev in enumerate(ftx_events):
        eid = ev.get("id")
        ch = ev.get("content_hash") or eid
        if eid in seen_ids:
            # Already in fused — boost its RRF score
            if ch in fused:
                fused[ch]["rrf"] += 1.0 / (k + rank)
            continue
        seen_ids.add(eid)
        if ch in fused:
            fused[ch]["rrf"] += 1.0 / (k + rank)
        else:
            fused[ch] = {
                "event": ev,
                "rrf": 1.0 / (k + rank),
                "vec_score": 0.0,
            }

    # 4) Post-filter: penalise repetitive content, collect event IDs for KG lookup
    event_ids_for_kg = []
    scored = []
    for ch, entry in fused.items():
        ev = entry["event"]
        content = ev.get("content", "")
        rrf = entry["rrf"]
        vec_score = entry["vec_score"]

        if _is_highly_repetitive(content):
            rrf = rrf * 0.02

        scored.append((rrf, vec_score, ev))
        eid = ev.get("id")
        if eid and eid.startswith("event:"):
            event_ids_for_kg.append(eid)

    # 5) Fetch KG facts for the top candidate events
    # Extract entity names from event contents for KG matching
    kg_facts_map = {}
    if event_ids_for_kg:
        # Collect unique entity names mentioned in the top events
        entity_names = set()
        for _, _, ev in scored[:top_k]:
            content = ev.get("content", "")
            # Simple heuristic: extract capitalized words as potential entities
            import re
            for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', content):
                name = match.group(1).strip()
                if len(name) >= 2:
                    entity_names.add(name)
        
        if entity_names:
            # Build OR conditions for entity name matching
            name_conditions = " OR ".join(
                f"in.name = '{escape_surrealql(name)}' OR out.name = '{escape_surrealql(name)}'"
                for name in list(entity_names)[:10]
            )
            kg_sql = f"""
            SELECT id, predicate, in.name AS subject, out.name AS object, confidence
            FROM fact
            WHERE ({name_conditions})
              AND (valid_until IS NONE OR valid_until > time::now())
            ORDER BY confidence DESC
            LIMIT 30;
            """
            try:
                kg_result = await _query_surreal(kg_sql)
                kg_facts_raw = _extract_result(kg_result, 1) or []
                if kg_facts_raw:
                    NOISY_PREDICATES = {"weakly_related", "mentions"}
                    MIN_CONFIDENCE = 0.5
                    filtered = []
                    for f in kg_facts_raw:
                        pred = f.get("predicate", "")
                        conf = f.get("confidence", 0) or 0
                        if pred in NOISY_PREDICATES:
                            continue
                        if pred in ("co_occurs_with", "strongly_related", "related_to") and conf < MIN_CONFIDENCE:
                            continue
                        # Ontologie-Validierung auf subject/object types
                        subj_name = f.get("subject", "") or ""
                        obj_name = f.get("object", "") or ""
                        if subj_name and obj_name and pred not in ("related_to", "co_occurs_with", "strongly_related"):
                            try:
                                subj_type = infer_entity_type(subj_name)
                                obj_type = infer_entity_type(obj_name)
                                if not validate_predicate(subj_type, pred, obj_type):
                                    continue
                            except Exception:
                                continue
                        filtered.append(f)
                    if filtered:
                        kg_facts_map["_all"] = [_clean_output(f) for f in filtered]
            except Exception as e:
                print(f"[semantic_search] KG fact filtering error: {e}")

    # 6) Sort by RRF, normalize scores to 0-1, build final output
    scored.sort(key=lambda x: x[0], reverse=True)
    top_results = scored[:top_k]

    # Find min/max RRF for normalization
    if top_results:
        min_rrf = min(rrf for rrf, _, _ in top_results)
        max_rrf = max(rrf for rrf, _, _ in top_results)
        range_rrf = max_rrf - min_rrf if max_rrf > min_rrf else 1.0
    else:
        min_rrf = max_rrf = range_rrf = 1.0

    events = []
    for rrf, vec_score, ev in top_results:
        normalized_score = (rrf - min_rrf) / range_rrf if range_rrf > 0 else 0.0
        event_out = {
            "id": ev.get("id"),
            "content": ev.get("content"),
            "timestamp": ev.get("timestamp"),
            "source": ev.get("source"),
            "metadata": ev.get("metadata"),
            "score": round(normalized_score, 4),
            "vec_score": round(vec_score, 4) if vec_score > 0 else None,
        }
        events.append(event_out)

    result = {
        "events": events,
        "count": len(events),
        "query": query,
    }

    # Attach KG facts if found
    all_facts = kg_facts_map.get("_all")
    if all_facts:
        result["kg_facts"] = all_facts
        result["kg_fact_count"] = len(all_facts)

    return result


def _is_fact_plausible(predicate: str, in_type: str, out_type: str) -> bool:
    """Check if a fact's predicate is plausible given its entity types.
    Filters out facts where the predicate doesn't match the ontology
    (e.g., 'works_at' between two technologies)."""
    if not in_type or not out_type or predicate in ("related_to", "co_occurs_with", "strongly_related", "weakly_related", "mentions"):
        return True
    return validate_predicate(in_type, predicate, out_type)


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
async def explain_routing(query: str) -> dict:
    """Explains why the router chose a specific strategy for a query."""
    from src.extraction.classifier import QueryClassifier
    from src.router.policy import RoutingPolicy

    classifier = QueryClassifier()
    q_type, confidence = classifier.classify(query)

    # Convert string query type to QueryType enum
    from src.router.policy import QueryType

    try:
        q_type_enum = QueryType(q_type)
    except ValueError:
        # Fallback to FACTUAL if unknown type
        q_type_enum = QueryType.FACTUAL

    policy = RoutingPolicy()
    strategy_name, budget_level, policy_applied = policy.get_strategy(
        q_type_enum, confidence
    )

    # Handle both Enum and string cases for budget_level
    if hasattr(budget_level, "value"):
        budget_str = budget_level.value
    else:
        budget_str = str(budget_level)

    return {
        "query": query,
        "classified_as": q_type,
        "confidence": confidence,
        "strategy_selected": strategy_name,
        "reason": f"The query was classified as '{q_type}' with confidence {confidence}. "
        f"Based on this classification and available budget, the system selected "
        f"the '{strategy_name}' strategy which is optimal for this type of query.",
        "cost_budget_used": budget_str,
        "policy_applied": policy_applied,
    }


@mcp.tool()
async def memory_forget(
    entity: Optional[str] = None, event_id: Optional[str] = None, reason: str = ""
) -> dict:
    """Forgets a memory by event_id or entity."""
    if not entity and not event_id:
        return {
            "status": "error",
            "message": "Either entity or event_id must be provided",
        }

    forgotten_items = []

    if event_id:
        # Prüfen ob das Event existiert
        try:
            check_sql = f"SELECT id FROM {event_id};"
            check_result = await _query_surreal(check_sql)
            check_items = _extract_result(check_result, 1)
            if not check_items:
                return {
                    "status": "error",
                    "message": f"Event {event_id} not found – nothing to forget",
                }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to verify event {event_id}: {str(e)}",
            }

        try:
            update_sql = f"UPDATE {event_id} SET forgotten = true, forgotten_reason = '{escape_surrealql(reason)}';"
            await _query_surreal(update_sql)
            forgotten_items.append(
                {"id": event_id, "type": "event", "status": "forgotten"}
            )
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to forget event {event_id}: {str(e)}",
            }

    if entity:
        entity_escaped = escape_surrealql(entity)

        # 1. Mark the entity itself as forgotten FIRST – if this fails, no facts are touched
        try:
            entity_find_sql = (
                f"SELECT id FROM entity WHERE name = '{entity_escaped}' LIMIT 1;"
            )
            entity_result = await _query_surreal(entity_find_sql)
            entities = _extract_result(entity_result, 1)

            if entities:
                entity_id = entities[0].get("id")
                entity_update_sql = f"UPDATE {entity_id} SET forgotten = true, forget_reason = '{escape_surrealql(reason)}';"
                await _query_surreal(entity_update_sql)
                forgotten_items.append(
                    {"id": entity_id, "type": "entity", "status": "forgotten"}
                )
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to forget entity {entity}: {str(e)}",
            }

        # 2. Now invalidate all facts related to this entity
        find_facts_sql = f"""
        SELECT id FROM fact
        WHERE in.name = '{entity_escaped}' OR out.name = '{entity_escaped}';
        """
        facts_result = await _query_surreal(find_facts_sql)
        facts = _extract_result(facts_result, 1)

        for fact in facts:
            fact_id = fact.get("id")
            try:
                invalidate_sql = f"UPDATE {fact_id} SET valid_until = time::now(), invalidated_reason = '{escape_surrealql(reason)}';"
                await _query_surreal(invalidate_sql)
                forgotten_items.append(
                    {"id": fact_id, "type": "fact", "status": "invalidated"}
                )
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Failed to invalidate fact {fact_id}: {str(e)}",
                }

    return {
        "forgotten_items": forgotten_items,
        "count": len(forgotten_items),
        "reason": reason,
    }


@mcp.tool()
async def memory_unforget(
    event_id: str,
) -> dict:
    """Restores a previously forgotten event. Resets forgotten=false."""
    try:
        check_sql = f"SELECT id FROM {event_id};"
        check_result = await _query_surreal(check_sql)
        check_items = _extract_result(check_result, 1)
        if not check_items:
            return {
                "status": "error",
                "message": f"Event {event_id} not found – nothing to restore",
            }

        update_sql = f"UPDATE {event_id} SET forgotten = false, forgotten_reason = NONE;"
        await _query_surreal(update_sql)
        return {"status": "restored", "event_id": event_id}
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to restore event {event_id}: {str(e)}",
        }


@mcp.tool()
async def memory_consolidate(
    entity: Optional[str] = None, scope: str = "local", delete_stale: bool = False
) -> dict:
    """Consolidates memory entries. When delete_stale=True, physically removes stale facts from the database."""

    # Find stale facts: nur facts mit valid_until in der Vergangenheit
    # Gleiche Clause für Dry-Run und Delete – Dry-Run zeigt exakt was gelöscht würde
    time_clause = "valid_until != NONE AND valid_until < time::now()"

    if entity:
        entity_escaped = escape_surrealql(entity)
        find_stale_sql = f"""
        SELECT id, predicate, in.name AS subject, out.name AS object, valid_from, valid_until
        FROM fact
        WHERE (in.name = '{entity_escaped}' OR out.name = '{entity_escaped}')
          AND {time_clause};
        """
    else:
        find_stale_sql = f"""
        SELECT id, predicate, in.name AS subject, out.name AS object, valid_from, valid_until
        FROM fact
        WHERE {time_clause};
        """

    result = await _query_surreal(find_stale_sql)
    stale_facts = _extract_result(result, 1)

    deleted_count = 0
    if delete_stale and stale_facts:
        for fact in stale_facts:
            fact_id = fact.get("id")
            try:
                delete_sql = f"DELETE {fact_id};"
                await _query_surreal(delete_sql)
                deleted_count += 1
            except Exception as e:
                return {
                    "status": "error",
                    "message": f"Failed to delete fact {fact_id}: {str(e)}",
                }

    # Operation ins Event-Log schreiben
    entity_info = f" for entity '{entity}'" if entity else ""
    log_content = f"Consolidation run{entity_info}: {len(stale_facts)} stale facts found, {deleted_count} deleted."
    try:
        log_escaped = escape_surrealql(log_content)
        log_sql = f"""
        CREATE event SET
            content = '{log_escaped}',
            content_hash = '{escape_surrealql(hashlib.md5(log_content.encode()).hexdigest())}',
            source = 'system_maintenance',
            metadata = {{"action": "consolidate", "scope": "{escape_surrealql(scope)}", "stale_facts": {len(stale_facts)}, "deleted": {deleted_count}}};
        """
        await _query_surreal(log_sql)
    except Exception as e:
        # Log-Fehler sollen den Hauptvorgang nicht blockieren
        print(f"[WARN] Failed to write consolidation event log: {e}")

    return {
        "scope": scope,
        "stale_facts_found": len(stale_facts),
        "deleted_count": deleted_count,
        "stale_facts_sample": stale_facts[:10],
        "status": "success",
    }


@mcp.tool()
async def list_entities(
    limit: int = 20,
    offset: int = 0,
    type: Optional[str] = None,
    name_contains: Optional[str] = None,
    sort_by: str = "name",
    sort_order: str = "asc",
) -> dict:
    """List entities in the knowledge graph with filtering and pagination."""
    if sort_by not in ("name", "created_at", "updated_at"):
        sort_by = "name"
    if sort_order not in ("asc", "desc"):
        sort_order = "asc"
    order = f"{sort_by} {sort_order}"

    filters = ["(forgotten IS NONE OR forgotten = false)"]
    if type:
        type_escaped = escape_surrealql(type)
        filters.append(f"type = '{type_escaped}'")
    if name_contains:
        nc_escaped = escape_surrealql(name_contains.lower())
        filters.append(f"string::lowercase(name) CONTAINS '{nc_escaped}'")
    where = " AND ".join(filters)

    sql = f"""
    SELECT id, name, type, created_at, updated_at
    FROM entity
    WHERE {where}
    ORDER BY {order}
    LIMIT {limit}
    START {offset};
    """
    count_sql = f"SELECT count() FROM entity WHERE {where} GROUP ALL;"

    result = await _query_surreal(sql)
    count_result = await _query_surreal(count_sql)

    entities = _clean_output(_extract_result(result, 1))
    counts = _extract_result(count_result, 1)
    total = counts[0].get("count", 0) if counts else 0

    return {"entities": entities, "count": len(entities), "total": total}


@mcp.tool()
async def list_events(
    limit: int = 20,
    offset: int = 0,
    since: Optional[str] = None,
    until: Optional[str] = None,
    source: Optional[str] = None,
    include_forgotten: bool = False,
) -> dict:
    """List events from the raw event log with filtering and pagination."""
    filters = []
    if not include_forgotten:
        filters.append("(forgotten IS NONE OR forgotten = false)")
    if since:
        filters.append(f"timestamp >= '{escape_surrealql(since)}'")
    if until:
        filters.append(f"timestamp <= '{escape_surrealql(until)}'")
    if source:
        source_escaped = escape_surrealql(source)
        filters.append(f"source = '{source_escaped}'")
    where = " AND ".join(filters) if filters else "1=1"

    sql = f"""
    SELECT id, content, timestamp, source, metadata, forgotten, forgotten_reason
    FROM event
    WHERE {where}
    ORDER BY timestamp DESC
    LIMIT {limit}
    START {offset};
    """
    count_sql = f"SELECT count() FROM event WHERE {where} GROUP ALL;"

    result = await _query_surreal(sql)
    count_result = await _query_surreal(count_sql)

    events = _clean_output(_extract_result(result, 1))
    counts = _extract_result(count_result, 1)
    total = counts[0].get("count", 0) if counts else 0

    return {"events": events, "count": len(events), "total": total}
