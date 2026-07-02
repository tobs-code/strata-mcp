# -*- coding: utf-8 -*-
"""
MCP Tools implementation
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from mcp.server.fastmcp import FastMCP
from src.extraction.entropy_gate import escape_surrealql

from .common_logic import _execute_query, _get_or_create_entity, _store_content
from .core import _clean_output, _embed_query, _extract_result, _query_surreal, mcp


@mcp.tool()
async def memory_store(
    content: str, source: str = "user_input", metadata: Optional[Dict[str, Any]] = None
) -> dict:
    """Stores a new event in the raw event log. Runs through entropy gate. NOTE: Only English content should be stored — German or other languages produce noisy entity extraction."""
    return await _store_content(content, source, debug=True)


@mcp.tool()
async def memory_query(query: str, cost_budget: str = "auto") -> dict:
    """Routes a natural language query through the full pipeline: classify → plan → retrieve."""
    return await _execute_query(query, cost_budget)


@mcp.tool()
async def memory_update(subject: str, predicate: str, new_value: str) -> dict:
    """Updates a fact in the KG via logical invalidation. Old fact gets valid_until, new fact created.
    If the target entity does not exist yet, it will be created automatically."""
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
            ]
            if related_facts
            else [],
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
    }


@mcp.tool()
async def event_log_search(
    query: str,
    limit: int = 10,
    since: Optional[str] = None,
    until: Optional[str] = None,
) -> dict:
    """Direct timeline query without router: hybrid search (BM25 + vector + RRF fusion)."""
    query_escaped = escape_surrealql(query)
    time_filter = ""
    if since:
        time_filter += f" AND timestamp >= '{escape_surrealql(since)}'"
    if until:
        time_filter += f" AND timestamp <= '{escape_surrealql(until)}'"

    forgotten_filter = "(forgotten IS NONE OR forgotten = false)"

    if not query.strip():
        # If no query, just return recent events
        sql = f"""
        SELECT id, content, timestamp, source, metadata
        FROM event
        WHERE {forgotten_filter}
        {time_filter}
        ORDER BY timestamp DESC
        LIMIT {limit};
        """
        result = await _query_surreal(sql)
        events = _extract_result(result, 1)
        for event in events:
            event["search_type"] = "recent"
        return {"events": _clean_output(events), "count": len(events)}

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


@mcp.tool()
async def kg_query(
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    at_time: Optional[str] = None,
) -> dict:
    """Direct graph traversal: query facts by subject/predicate/time.
    Returns associated entities with their inferred types (e.g., 'organization', 'concept')."""

    subject_clause = ""
    predicate_clause = ""
    time_clause = ""

    if subject:
        subject_escaped = escape_surrealql(subject)
        subject_clause = f"WHERE in.name = '{subject_escaped}'"
    if predicate:
        predicate_escaped = escape_surrealql(predicate)
        if subject_clause:
            subject_clause += f" AND predicate = '{predicate_escaped}'"
        else:
            subject_clause = f"WHERE predicate = '{predicate_escaped}'"
    if at_time:
        time_escaped = escape_surrealql(at_time)
        if subject_clause:
            subject_clause += (
                f" AND (valid_from <= '{time_escaped}' OR valid_from = NONE)"
            )
            subject_clause += (
                f" AND (valid_until >= '{time_escaped}' OR valid_until = NONE)"
            )
        else:
            subject_clause = (
                f"WHERE (valid_from <= '{time_escaped}' OR valid_from = NONE)"
            )
            subject_clause += (
                f" AND (valid_until >= '{time_escaped}' OR valid_until = NONE)"
            )

    # Only show active facts (not invalidated)
    valid_filter = "WHERE (valid_until IS NONE OR valid_until > time::now())"
    if subject_clause:
        # Replace initial WHERE with AND since we already have valid_filter
        subject_clause = subject_clause.replace("WHERE", "AND", 1) if "WHERE" in subject_clause else subject_clause

    sql = f"""
    SELECT id, in, out, predicate, confidence, valid_from, valid_until
    FROM fact
    {valid_filter} {subject_clause}
    ORDER BY confidence DESC
    LIMIT 100;
    """

    result = await _query_surreal(sql)
    facts = _extract_result(result, 1)

    # Enhance facts with entity information
    enhanced_facts = []
    for fact in facts:
        fact_id = fact.get("id")

        # Safely extract in_id and out_id - handle both dict and string formats
        in_data = fact.get("in")
        if isinstance(in_data, dict):
            in_id = in_data.get("id")
        else:
            in_id = None

        out_data = fact.get("out")
        if isinstance(out_data, dict):
            out_id = out_data.get("id")
        else:
            out_id = None

        # Get detailed entity info for in/out
        if in_id:
            in_entity_sql = f"SELECT name, type FROM entity WHERE id = '{in_id}';"
            in_result = await _query_surreal(in_entity_sql)
            in_entities = _extract_result(in_result, 1)
            if in_entities:
                in_entity = in_entities[0]
                fact["in"] = {
                    "id": in_id,
                    "name": in_entity.get("name"),
                    "type": in_entity.get("type"),
                }

        if out_id:
            out_entity_sql = f"SELECT name, type FROM entity WHERE id = '{out_id}';"
            out_result = await _query_surreal(out_entity_sql)
            out_entities = _extract_result(out_result, 1)
            if out_entities:
                out_entity = out_entities[0]
                fact["out"] = {
                    "id": out_id,
                    "name": out_entity.get("name"),
                    "type": out_entity.get("type"),
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
    """Pure vector search without KG. Filters out highly repetitive/noise content."""
    if not query.strip():
        return {"events": [], "count": 0, "message": "Query cannot be empty"}

    from src.extraction.embedding_service import get_embedding_service

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
        # Mark event as forgotten
        try:
            update_sql = f"UPDATE {event_id} SET forgotten = true, forgotten_reason = '{escape_surrealql(reason)}';"
            result = await _query_surreal(update_sql)
            forgotten_items.append(
                {"id": event_id, "type": "event", "status": "forgotten"}
            )
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to forget event {event_id}: {str(e)}",
            }

    if entity:
        # Mark all facts related to this entity as forgotten/invalidated
        entity_escaped = escape_surrealql(entity)

        # Find and invalidate all facts related to this entity
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

        # Mark the entity itself as forgotten
        try:
            entity_find_sql = (
                f"SELECT id FROM entity WHERE name = '{entity_escaped}' LIMIT 1;"
            )
            entity_result = await _query_surreal(entity_find_sql)
            entities = _extract_result(entity_result, 1)

            if entities:
                entity_id = entities[0].get("id")
                entity_update_sql = f"UPDATE {entity_id} SET forgotten = true, forgotten_reason = '{escape_surrealql(reason)}';"
                await _query_surreal(entity_update_sql)
                forgotten_items.append(
                    {"id": entity_id, "type": "entity", "status": "forgotten"}
                )
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to forget entity {entity}: {str(e)}",
            }

    return {
        "forgotten_items": forgotten_items,
        "count": len(forgotten_items),
        "reason": reason,
    }


@mcp.tool()
async def memory_consolidate(
    entity: Optional[str] = None, scope: str = "local", delete_stale: bool = False
) -> dict:
    """Consolidates memory entries. When delete_stale=True, physically removes stale facts from the database."""

    # Find stale facts (facts with valid_until set in the past)
    time_filter = "valid_until < time::now()" if delete_stale else "valid_until != NONE"

    if entity:
        entity_escaped = escape_surrealql(entity)
        find_stale_sql = f"""
        SELECT id, predicate, in.name AS subject, out.name AS object, valid_from, valid_until
        FROM fact
        WHERE (in.name = '{entity_escaped}' OR out.name = '{entity_escaped}')
          AND {time_filter};
        """
    else:
        find_stale_sql = f"""
        SELECT id, predicate, in.name AS subject, out.name AS object, valid_from, valid_until
        FROM fact
        WHERE {time_filter};
        """

    result = await _query_surreal(find_stale_sql)
    stale_facts = _extract_result(result, 1)

    deleted_count = 0
    if delete_stale and stale_facts:
        # Delete the stale facts
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

    return {
        "scope": scope,
        "stale_facts_found": len(stale_facts),
        "deleted_count": deleted_count,
        "stale_facts_sample": stale_facts[:10],  # Return first 10 as sample
        "status": "success",
    }
