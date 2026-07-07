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
from src.extraction.entropy_gate import character_diversity, escape_surrealql

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
async def memory_store_batch(
    items: List[Dict[str, Any]],
    source: str = "user_input",
) -> dict:
    """Stores multiple events in batch. Each item must have 'content'. Optional: 'source', 'metadata'."""
    results = []
    errors = []
    gate_counts = {"extract": 0, "ignore": 0, "skip": 0}
    for i, item in enumerate(items):
        try:
            content = item.get("content", "")
            if not content:
                errors.append({"index": i, "error": "missing content"})
                continue
            item_source = item.get("source", source)
            item_metadata = item.get("metadata")
            result = await _store_content(content, item_source, debug=False, metadata=item_metadata)
            gate_decision = result.get("gate", {}).get("decision", "unknown")
            gate_counts[gate_decision] = gate_counts.get(gate_decision, 0) + 1
            flat_result = {
                "index": i,
                "event_id": result.get("event_id"),
                "status": result.get("status", "unknown"),
                "source": result.get("source", item_source),
                "gate_decision": gate_decision,
            }
            gate_info = result.get("gate", {})
            if gate_decision == "extract":
                flat_result["entities_created"] = gate_info.get("kg", {}).get("entities_created", 0)
                flat_result["facts_created"] = gate_info.get("kg", {}).get("facts_created", 0)
            else:
                flat_result["gate_reason"] = gate_info.get("reason")
                flat_result["composite_score"] = gate_info.get("composite_score")
                flat_result["threshold"] = gate_info.get("threshold")
            results.append(flat_result)
        except Exception as e:
            errors.append({"index": i, "error": str(e)})
    return {
        "results": results,
        "errors": errors,
        "stored": len(results),
        "failed": len(errors),
        "gate_summary": gate_counts,
    }


def _read_file(file_path: str) -> str:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise FileNotFoundError(f"File not found: {file_path}")
    except UnicodeDecodeError:
        with open(file_path, "r", encoding="latin-1") as f:
            return f.read()


@mcp.tool()
async def memory_store_markdown(
    content: Optional[str] = None,
    file_path: Optional[str] = None,
    source: str = "markdown_import",
    chunk_size: int = 1500,
    overlap: int = 300,
    include_heading_context: bool = True,
    chunking_method: str = "char",
    encoding_name: str = "cl100k_base",
    strip_images: bool = True,
    parse_front_matter: bool = True,
    max_concurrent: int = 3,
    metadata: Optional[Dict[str, Any]] = None,
) -> dict:
    """Import markdown content or file with overlapping chunking. Each chunk is stored
    individually through the entropy gate and knowledge graph extraction pipeline.

    Provide either 'content' (inline markdown string) or 'file_path' (path on disk).
    Chunks are overlapped to preserve context across chunk boundaries.
    Heading hierarchy is prepended to each chunk for better retrieval context.

    Args:
        content: Inline markdown content (mutually exclusive with file_path)
        file_path: Path to a .md file on disk (mutually exclusive with content)
        source: Source label for all chunks
        chunk_size: Target size per chunk in characters (default 1500)
        overlap: Overlap in characters between consecutive chunks (default 300)
        include_heading_context: Prepend heading tree to each chunk (default True)
        chunking_method: 'char' (default) or 'token' (uses tiktoken)
        encoding_name: tiktoken encoding name (default cl100k_base)
        strip_images: Replace image references with alt text (default True)
        parse_front_matter: Extract YAML front matter into metadata (default True)
        max_concurrent: Max concurrent store operations (default 3)
        metadata: Optional metadata attached to every chunk
    """
    if not content and not file_path:
        return {"status": "error", "message": "Provide either 'content' or 'file_path'"}
    if content and file_path:
        return {"status": "error", "message": "Provide either 'content' or 'file_path', not both"}

    if file_path:
        try:
            content = _read_file(file_path)
        except FileNotFoundError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": f"Failed to read file: {e}"}
        if source == "markdown_import":
            source = f"markdown:{os.path.basename(file_path)}"

    if not content or not content.strip():
        return {"status": "error", "message": "Content is empty"}

    from .chunking import chunk_markdown

    chunker_result = chunk_markdown(
        content,
        chunk_size=chunk_size,
        overlap=overlap,
        include_heading_context=include_heading_context,
        chunking_method=chunking_method,
        encoding_name=encoding_name,
        strip_images=strip_images,
        parse_front_matter=parse_front_matter,
    )

    chunks = chunker_result["chunks"]
    front_matter = chunker_result["front_matter"]
    images_extracted = chunker_result["images"]

    if not chunks:
        return {"status": "error", "message": "No chunks generated from content"}

    if front_matter:
        meta = dict(metadata or {})
        for k, v in front_matter.items():
            if k not in meta:
                meta[k] = v
        metadata = meta

    sem = asyncio.Semaphore(max_concurrent)
    results = []
    errors = []
    gate_counts: Dict[str, int] = {"extract": 0, "ignore": 0, "skip": 0}

    async def store_one(chunk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        async with sem:
            try:
                chunk_meta = dict(metadata or {})
                chunk_meta["chunk_index"] = chunk["index"]
                chunk_meta["chunk_total"] = chunk["total_chunks"]
                chunk_meta["chunk_char_start"] = chunk["char_start"]
                chunk_meta["chunk_char_end"] = chunk["char_end"]
                if chunk["heading_context"]:
                    chunk_meta["heading_context"] = chunk["heading_context"]

                chunk_text = chunk["text"]
                if chunk["heading_context"] and include_heading_context:
                    chunk_text = f"[{chunk['heading_context']}]\n{chunk['text']}"

                chunk_source = f"{source}#chunk{chunk['index']}"

                store_result = await _store_content(chunk_text, source=chunk_source, metadata=chunk_meta)
                store_status = store_result.get("status", "unknown")
                gate_decision = store_result.get("gate", {}).get("decision", "unknown")
                if gate_decision in gate_counts:
                    gate_counts[gate_decision] += 1

                if store_status == "error":
                    err_msg = store_result.get("message", "unknown error")
                    errors.append({"chunk_index": chunk["index"], "error": err_msg, "from": "store"})
                    return None

                return {
                    "chunk_index": chunk["index"],
                    "event_id": store_result.get("event_id"),
                    "status": store_status,
                    "gate_decision": gate_decision,
                    "char_start": chunk["char_start"],
                    "char_end": chunk["char_end"],
                }
            except Exception as e:
                errors.append({"chunk_index": chunk["index"], "error": str(e), "from": "exception"})
                return None

    tasks = [store_one(c) for c in chunks]
    for coro in asyncio.as_completed(tasks):
        r = await coro
        if r is not None:
            results.append(r)

    results.sort(key=lambda x: x["chunk_index"])

    resp: Dict[str, Any] = {
        "status": "completed" if not errors else "partial",
        "source": source,
        "total_chunks": len(chunks),
        "stored": len(results),
        "failed": len(errors),
        "chunk_size": chunk_size,
        "overlap": overlap,
        "chunking_method": chunking_method,
        "max_concurrent": max_concurrent,
        "results": results,
        "errors": errors,
        "gate_summary": gate_counts,
    }

    if front_matter:
        resp["front_matter"] = front_matter
    if images_extracted:
        resp["images_extracted"] = len(images_extracted)

    return resp


@mcp.tool()
async def memory_query(query: str, cost_budget: str = "auto", limit: int = 10) -> dict:
    """Routes a natural language query through the full pipeline: classify → plan → retrieve."""
    return await _execute_query(query, cost_budget, limit)


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
    f = "forgotten = false"

    async def _q(sql):
        return _extract_result(await _query_surreal(sql), 1) or []

    event_task = asyncio.create_task(_q(f"SELECT count() FROM event WHERE {f} GROUP ALL;"))
    entity_task = asyncio.create_task(_q(f"SELECT count() FROM entity WHERE {f} GROUP ALL;"))
    fact_task = asyncio.create_task(_q("SELECT count() FROM fact WHERE (valid_until IS NONE OR valid_until = NONE) GROUP ALL;"))
    oldest_task = asyncio.create_task(_q(f"SELECT timestamp FROM event WHERE {f} ORDER BY timestamp ASC LIMIT 1;"))
    newest_task = asyncio.create_task(_q(f"SELECT timestamp FROM event WHERE {f} ORDER BY timestamp DESC LIMIT 1;"))
    total_gate_task = asyncio.create_task(_q("SELECT count() FROM gate_log GROUP ALL;"))
    extract_gate_task = asyncio.create_task(_q("SELECT count() FROM gate_log WHERE decision = 'extract' GROUP ALL;"))
    recent_gate_task = asyncio.create_task(_q("SELECT content_hash, decision, reason, gate_score, threshold, compression_ratio, ts FROM gate_log ORDER BY ts DESC LIMIT 10;"))

    results = await asyncio.gather(
        event_task, entity_task, fact_task, oldest_task, newest_task,
        total_gate_task, extract_gate_task, recent_gate_task,
    )

    event_count = results[0][0].get("count", 0) if results[0] else 0
    entity_count = results[1][0].get("count", 0) if results[1] else 0
    fact_count = results[2][0].get("count", 0) if results[2] else 0
    oldest_event = results[3][0].get("timestamp") if results[3] else None
    newest_event = results[4][0].get("timestamp") if results[4] else None
    total_decisions = results[5][0].get("count", 0) if results[5] else 0
    extract_count = results[6][0].get("count", 0) if results[6] else 0
    gate_pass_rate = extract_count / total_decisions if total_decisions > 0 else 0.0

    recent_gate_logs = results[7]
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
        forgotten_filter = "forgotten = false"

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
    object: Optional[str] = None,
    predicate: Optional[str] = None,
    at_time: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """Direct graph traversal: query facts by subject/object/predicate/time.
    Returns associated entities with their inferred types (e.g., 'organization', 'concept').
    - 'subject' matches entity name in subject position (in.name).
    - 'object' matches entity name in object position (out.name).
    - If both are provided, finds facts connecting them.
    - If neither is provided, returns all active facts (use with predicate to narrow)."""
    extra_clauses = ""

    if subject and object:
        subj_escaped = escape_surrealql(subject)
        obj_escaped = escape_surrealql(object)
        extra_clauses = f"WHERE in.name = '{subj_escaped}' AND out.name = '{obj_escaped}'"
    elif subject:
        subj_escaped = escape_surrealql(subject)
        extra_clauses = f"WHERE in.name = '{subj_escaped}'"
    elif object:
        obj_escaped = escape_surrealql(object)
        extra_clauses = f"WHERE out.name = '{obj_escaped}'"
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
    LIMIT {limit} START {offset};
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
            "object": object,
            "predicate": predicate,
            "at_time": at_time,
            "limit": limit,
            "offset": offset,
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
    forgotten_filter = "forgotten = false"

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

    # 5) Fetch KG facts for the top candidate events + query entities
    # Extract entity names from event contents AND the query itself for KG matching
    kg_facts_map = {}
    if event_ids_for_kg:
        import re
        entity_names = set()
        # Extract entities from event contents
        for _, _, ev in scored[:top_k]:
            content = ev.get("content", "")
            for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', content):
                name = match.group(1).strip()
                if len(name) >= 2:
                    entity_names.add(name)
        # Also extract capitalized entities from the query itself
        for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', query):
            name = match.group(1).strip()
            if len(name) >= 2:
                entity_names.add(name)
        # Include significant lowercase query terms (3+ chars) as potential KG entity names
        for word in query.split():
            word = word.strip(".,!?;:'\"()[]")
            if len(word) >= 3 and not word[0].isupper():
                entity_names.add(word)
        
        if entity_names:
            # Build OR conditions for entity name matching (use up to 20 entities)
            name_conditions = " OR ".join(
                f"in.name = '{escape_surrealql(name)}' OR out.name = '{escape_surrealql(name)}'"
                for name in list(entity_names)[:20]
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


def _is_highly_repetitive(text: str) -> bool:
    """Erkennt repetitive/noise content wie 'test test test test test' or 'ab ab ab ab ab'.
    Prüft character_diversity (< 0.20) und zusätzlich die Wort-Wiederholungsrate."""
    if not text or len(text) < 5:
        return False
    div = character_diversity(text)
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
async def memory_explain_routing(query: str) -> dict:
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
async def memory_get(
    id: str,
    include_facts: bool = True,
) -> dict:
    """Get a single memory item by its ID (event, entity, or fact).

    Provides direct access to a specific event, entity, or knowledge graph fact.
    - For events: returns content, timestamp, source, metadata, and related entities
    - For entities: returns name, type, and optionally active KG facts
    - For facts: returns subject, predicate, object, confidence, and validity
    - For plain names: looks up as an entity name

    Args:
        id: The record ID (event:xxx, entity:xxx, fact:xxx) or entity name
        include_facts: For entities only — include active KG facts (default True)
    """
    id_stripped = id.strip()

    is_record_id = ":" in id_stripped and not id_stripped.startswith("⟨")

    if is_record_id:
        prefix = id_stripped.split(":")[0]

        if prefix == "event":
            sql = f"SELECT id, content, timestamp, source, metadata, forgotten, forgotten_reason, content_hash FROM {id_stripped};"
            result = await _query_surreal(sql)
            data = _extract_result(result, 1)
            if not data:
                return {"status": "error", "message": f"Event '{id_stripped}' not found"}
            event = _clean_output(data[0])
            return {"status": "ok", "type": "event", "data": event}

        elif prefix == "entity":
            sql = f"SELECT id, name, type, created_at, updated_at, forgotten, forget_reason FROM {id_stripped};"
            result = await _query_surreal(sql)
            data = _extract_result(result, 1)
            if not data:
                return {"status": "error", "message": f"Entity '{id_stripped}' not found"}
            entity = _clean_output(data[0])

            if include_facts:
                facts_sql = f"""
                SELECT id, predicate, in.name AS subject, in.id AS subject_id,
                       out.name AS object, out.id AS object_id,
                       confidence, valid_from, valid_until
                FROM fact
                WHERE (in = {id_stripped} OR out = {id_stripped})
                  AND (valid_until IS NONE OR valid_until > time::now())
                ORDER BY confidence DESC
                LIMIT 100;
                """
                facts_result = await _query_surreal(facts_sql)
                facts = _clean_output(_extract_result(facts_result, 1) or [])
                entity["facts"] = facts

            return {"status": "ok", "type": "entity", "data": entity}

        elif prefix == "fact":
            sql = f"""
            SELECT id, predicate, in.name AS subject, in.id AS subject_id,
                   in.type AS subject_type,
                   out.name AS object, out.id AS object_id,
                   out.type AS object_type,
                   confidence, valid_from, valid_until, invalidated_reason
            FROM {id_stripped};
            """
            result = await _query_surreal(sql)
            data = _extract_result(result, 1)
            if not data:
                return {"status": "error", "message": f"Fact '{id_stripped}' not found"}
            return {"status": "ok", "type": "fact", "data": _clean_output(data[0])}

        else:
            return {
                "status": "error",
                "message": f"Unknown ID prefix '{prefix}'. Expected 'event:', 'entity:', or 'fact:'.",
            }

    else:
        name_escaped = escape_surrealql(id_stripped)
        sql = f"""
        SELECT id, name, type, created_at, updated_at, forgotten, forget_reason
        FROM entity
        WHERE string::lowercase(name) = string::lowercase('{name_escaped}')
          AND forgotten = false
        LIMIT 1;
        """
        result = await _query_surreal(sql)
        data = _extract_result(result, 1)
        if not data:
            return {
                "status": "error",
                "message": f"No entity found with name or ID '{id_stripped}'",
            }
        entity = _clean_output(data[0])

        if include_facts:
            eid = entity["id"]
            facts_sql = f"""
            SELECT id, predicate, in.name AS subject, in.id AS subject_id,
                   out.name AS object, out.id AS object_id,
                   confidence, valid_from, valid_until
            FROM fact
            WHERE (in = {eid} OR out = {eid})
              AND (valid_until IS NONE OR valid_until > time::now())
            ORDER BY confidence DESC
            LIMIT 100;
            """
            facts_result = await _query_surreal(facts_sql)
            facts = _clean_output(_extract_result(facts_result, 1) or [])
            entity["facts"] = facts

        return {"status": "ok", "type": "entity", "data": entity}


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

        # Find entity first (read-only, no side effects)
        try:
            entity_find_sql = (
                f"SELECT id FROM entity WHERE name = '{entity_escaped}' LIMIT 1;"
            )
            entity_result = await _query_surreal(entity_find_sql)
            entities = _extract_result(entity_result, 1)
            entity_id = entities[0].get("id") if entities else None
        except Exception as e:
            return {
                "status": "error",
                "message": f"Failed to find entity {entity}: {str(e)}",
            }

        if not entity_id:
            return {
                "status": "error",
                "message": f"Entity '{entity}' not found – nothing to forget",
            }

        # 1. First invalidate all facts related to this entity
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

        # 2. Then mark the entity itself as forgotten
        try:
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

    return {
        "forgotten_items": forgotten_items,
        "count": len(forgotten_items),
        "reason": reason,
    }


@mcp.tool()
async def memory_unforget(
    event_id: str,
) -> dict:
    """Restores a previously forgotten event or entity. Resets forgotten=false.
    When restoring a forgotten entity, also restores all facts that were
    invalidated alongside it (clears valid_until and invalidated_reason)."""
    is_entity = str(event_id).startswith("entity:")

    try:
        check_sql = f"SELECT id FROM {event_id};"
        check_result = await _query_surreal(check_sql)
        check_items = _extract_result(check_result, 1)
        if not check_items:
            return {
                "status": "error",
                "message": f"Event/entity {event_id} not found – nothing to restore",
            }

        # 1. Restore the main record (entity or event)
        update_sql = f"UPDATE {event_id} SET forgotten = false, forgotten_reason = NONE;"
        await _query_surreal(update_sql)

        result = {"status": "restored", "event_id": event_id}

        # 2. If it's an entity, also restore all facts invalidated alongside it
        if is_entity:
            name_sql = f"SELECT name FROM {event_id};"
            name_result = await _query_surreal(name_sql)
            name_items = _extract_result(name_result, 1)
            if name_items and name_items[0].get("name"):
                entity_name = name_items[0]["name"]
                en = escape_surrealql(entity_name)

                facts_sql = f"""
                SELECT id FROM fact
                WHERE (in.name = '{en}' OR out.name = '{en}')
                  AND valid_until IS NOT NONE;
                """
                facts_result = await _query_surreal(facts_sql)
                facts = _extract_result(facts_result, 1) or []

                restored_facts = 0
                for fact in facts:
                    fid = fact.get("id")
                    try:
                        await _query_surreal(
                            f"UPDATE {fid} SET valid_until = NONE, invalidated_reason = NONE;"
                        )
                        restored_facts += 1
                    except Exception as e:
                        print(f"[WARN] Failed to restore fact {fid}: {e}")

                if restored_facts:
                    result["facts_restored"] = restored_facts

        return result
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to restore {event_id}: {str(e)}",
        }


@mcp.tool()
async def memory_consolidate(
    entity: Optional[str] = None, scope: str = "local", delete_stale: bool = False
) -> dict:
    """Consolidates memory entries. When delete_stale=True, physically removes stale facts from the database."""

    # Step 1: Find stale facts (valid_until in der Vergangenheit)
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

    # Step 2: Find duplicate active facts — gleiches (subject, predicate, object) mehrfach aktiv
    if entity:
        entity_escaped = escape_surrealql(entity)
        find_dup_sql = f"""
        SELECT id, predicate, in.id AS in_id, in.name AS subject, out.id AS out_id, out.name AS object, valid_from, confidence
        FROM fact
        WHERE (in.name = '{entity_escaped}' OR out.name = '{entity_escaped}')
          AND (valid_until IS NONE OR valid_until > time::now())
          AND predicate != 'mentions'
        ORDER BY valid_from ASC;
        """
    else:
        find_dup_sql = f"""
        SELECT id, predicate, in.id AS in_id, in.name AS subject, out.id AS out_id, out.name AS object, valid_from, confidence
        FROM fact
        WHERE (valid_until IS NONE OR valid_until > time::now())
          AND predicate != 'mentions'
        ORDER BY valid_from ASC;
        """

    dup_result = await _query_surreal(find_dup_sql)
    all_active = _extract_result(dup_result, 1)

    merged_count = 0
    seen = {}
    for fact in all_active:
        key = (fact.get("in_id"), fact.get("predicate"), fact.get("out_id"))
        fid = fact.get("id")
        if key in seen:
            # Duplicate found — invalidate the newer one
            older_id = seen[key]
            if fid:
                try:
                    inv_sql = f"UPDATE {fid} SET valid_until = time::now(), invalidated_reason = 'consolidate_duplicate';"
                    await _query_surreal(inv_sql)
                    merged_count += 1
                except Exception as e:
                    print(f"[WARN] Failed to invalidate duplicate fact {fid}: {e}")
        else:
            seen[key] = fid

    # Operation ins Event-Log schreiben
    entity_info = f" for entity '{entity}'" if entity else ""
    log_content = f"Consolidation run{entity_info}: {len(stale_facts)} stale facts found, {deleted_count} deleted, {merged_count} duplicates merged."
    try:
        log_escaped = escape_surrealql(log_content)
        log_sql = f"""
        CREATE event SET
            content = '{log_escaped}',
            content_hash = '{escape_surrealql(hashlib.md5(log_content.encode()).hexdigest())}',
            source = 'system_maintenance',
            metadata = {{"action": "consolidate", "scope": "{escape_surrealql(scope)}", "stale_facts": {len(stale_facts)}, "deleted": {deleted_count}, "duplicates_merged": {merged_count}}};
        """
        await _query_surreal(log_sql)
    except Exception as e:
        # Log-Fehler sollen den Hauptvorgang nicht blockieren
        print(f"[WARN] Failed to write consolidation event log: {e}")

    return {
        "scope": scope,
        "stale_facts_found": len(stale_facts),
        "deleted_count": deleted_count,
        "duplicates_merged": merged_count,
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

    filters = ["forgotten = false"]
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
async def memory_merge_entities(
    source_entity: str,
    target_entity: str,
    dry_run: bool = False,
) -> dict:
    """Merges all facts from source_entity into target_entity, then forgets the source.
    Re-links all facts (both in and out positions) to point to target_entity.
    Use dry_run=True to preview without making changes."""
    if source_entity == target_entity:
        return {"status": "error", "message": "source and target must be different"}

    se = escape_surrealql(source_entity)
    te = escape_surrealql(target_entity)

    # Find both entity IDs
    src_sql = f"SELECT id, type FROM entity WHERE name = '{se}' LIMIT 1;"
    tgt_sql = f"SELECT id, type FROM entity WHERE name = '{te}' LIMIT 1;"
    src_result = await _query_surreal(src_sql)
    tgt_result = await _query_surreal(tgt_sql)
    src_entities = _extract_result(src_result, 1)
    tgt_entities = _extract_result(tgt_result, 1)

    if not src_entities:
        return {"status": "error", "message": f"Source entity '{source_entity}' not found"}
    if not tgt_entities:
        return {"status": "error", "message": f"Target entity '{target_entity}' not found"}

    src_id = src_entities[0]["id"]
    tgt_id = tgt_entities[0]["id"]

    # Find all active facts where source is involved (match by name, consistent with codebase patterns)
    facts_sql = f"""
    SELECT id, predicate, confidence,
           in.id AS in_id, in.name AS in_name,
           out.id AS out_id, out.name AS out_name
    FROM fact
    WHERE (in.name = '{se}' OR out.name = '{se}')
      AND (valid_until IS NONE OR valid_until > time::now());
    """
    facts_result = await _query_surreal(facts_sql)
    facts = _extract_result(facts_result, 1)

    if not facts:
        return {
            "status": "ok",
            "source_entity": source_entity,
            "target_entity": target_entity,
            "merged_count": 0,
            "message": "No active facts found for source entity",
        }

    if dry_run:
        preview = []
        for f in facts:
            role = "in" if f.get("in_id") == src_id else "out"
            other_side = f.get("out_name") if role == "in" else f.get("in_name")
            preview.append({
                "fact_id": f["id"],
                "predicate": f["predicate"],
                "role": role,
                "other_entity": other_side,
            })
        return {
            "status": "dry_run",
            "source_entity": source_entity,
            "target_entity": target_entity,
            "total_facts": len(facts),
            "preview": preview,
        }

    merged = 0
    errors = []

    for fact in facts:
        try:
            fact_id = fact["id"]
            predicate_escaped = escape_surrealql(fact.get("predicate", ""))
            confidence = fact.get("confidence", 1.0)

            # Determine which side to replace
            fact_in_id = fact.get("in_id")
            fact_out_id = fact.get("out_id")

            # Build RELATE with source replaced by target
            if fact_in_id == src_id:
                relate_sql = f"RELATE {tgt_id}->fact->{fact_out_id} SET predicate = '{predicate_escaped}', confidence = {confidence};"
            else:
                relate_sql = f"RELATE {fact_in_id}->fact->{tgt_id} SET predicate = '{predicate_escaped}', confidence = {confidence};"

            await _query_surreal(relate_sql)

            # Invalidate original fact
            invalidate_sql = f"UPDATE {fact_id} SET valid_until = time::now(), invalidated_reason = 'merged_into_{te}';"
            await _query_surreal(invalidate_sql)

            merged += 1
        except Exception as e:
            errors.append({"fact_id": fact.get("id"), "error": str(e)})

    # Mark source entity as forgotten
    if merged > 0:
        try:
            forget_sql = f"UPDATE {src_id} SET forgotten = true, forget_reason = 'merged_into_{te}', updated_at = time::now();"
            await _query_surreal(forget_sql)
        except Exception as e:
            errors.append({"type": "forget_source", "error": str(e)})

    # Operation ins Event-Log schreiben
    try:
        log_content = f"Merged entity '{source_entity}' into '{target_entity}': {merged} facts re-linked, source forgotten."
        log_escaped = escape_surrealql(log_content)
        log_sql = f"""
        CREATE event SET
            content = '{log_escaped}',
            content_hash = '{escape_surrealql(hashlib.md5(log_content.encode()).hexdigest())}',
            source = 'system_maintenance',
            metadata = {{"action": "merge_entities", "source": "{se}", "target": "{te}", "source_id": "{src_id}", "target_id": "{tgt_id}", "merged": {merged}, "errors": {len(errors)}}};
        """
        await _query_surreal(log_sql)
    except Exception as e:
        print(f"[WARN] Failed to write merge event log: {e}")

    return {
        "status": "ok",
        "source_entity": source_entity,
        "target_entity": target_entity,
        "source_id": src_id,
        "target_id": tgt_id,
        "merged_count": merged,
        "error_count": len(errors),
        "errors": errors if errors else None,
    }


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
        filters.append("forgotten = false")
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


@mcp.tool()
async def graph_traverse(
    start_entity: str,
    max_depth: int = 2,
    direction: str = "both",
    predicate: Optional[str] = None,
    min_confidence: float = 0.0,
) -> dict:
    """Multi-hop graph traversal: find paths by walking the knowledge graph.
    Starts from 'start_entity' and follows relationships up to 'max_depth' hops.
    Uses BFS with cycle detection. Returns all unique paths discovered.

    - 'direction': 'outbound' (entity → related), 'inbound' (entity ← related), or 'both'
    - 'predicate': optional filter to follow only specific relationship types
    - 'min_confidence': minimum confidence threshold (0.0 to 1.0)"""
    from collections import deque

    max_depth = max(1, min(max_depth, 5))

    is_record_id = start_entity.startswith("entity:") or start_entity.startswith("⟨entity:") or ":" in start_entity.split("entity:", 1)[-1][:1]

    if is_record_id:
        clean_id = start_entity.strip("⟨⟩")
        entity_sql = f"""
        SELECT id, name, type FROM entity
        WHERE forgotten = false
        AND (id = {clean_id} OR name = '{escape_surrealql(start_entity)}')
        LIMIT 1;
        """
    else:
        entity_sql = f"""
        SELECT id, name, type FROM entity
        WHERE forgotten = false
        AND name = '{escape_surrealql(start_entity)}'
        LIMIT 1;
        """
    entity_result = await _query_surreal(entity_sql)
    entities = _extract_result(entity_result, 1)

    if not entities:
        return {
            "status": "error",
            "error": f"Entity '{start_entity}' not found in knowledge graph",
            "paths": [],
            "path_count": 0,
        }

    start = entities[0]
    visited: set = {start["id"]}
    all_nodes: Dict[str, dict] = {}
    all_edges: List[dict] = []
    all_paths: List[list] = []
    seen_edge_keys: set = set()

    queue: deque = deque()
    queue.append((start["id"], start["name"], 0, []))

    while queue:
        eid, ename, depth, path = queue.popleft()

        if depth >= max_depth:
            continue

        clauses = []
        if direction in ("outbound", "both"):
            clauses.append(f"in = {eid}")
        if direction in ("inbound", "both"):
            clauses.append(f"out = {eid}")
        if not clauses:
            break

        pred_clause = ""
        if predicate:
            pred_clause = f"AND predicate = '{escape_surrealql(predicate)}'"

        hop_sql = f"""
        SELECT in.id AS in_id, in.name AS in_name, in.type AS in_type,
               out.id AS out_id, out.name AS out_name, out.type AS out_type,
               predicate, confidence
        FROM fact
        WHERE (valid_until IS NONE OR valid_until > time::now())
        AND ({' OR '.join(clauses)})
        {pred_clause}
        AND (confidence IS NONE OR confidence >= {min_confidence})
        ORDER BY confidence DESC;
        """

        result = await _query_surreal(hop_sql)
        facts = _extract_result(result, 1)

        for f in facts:
            pred = f.get("predicate", "")
            if pred in ("weakly_related", "mentions"):
                continue

            is_outbound = (f.get("in_id") == eid)
            neighbor_id = f.get("out_id") if is_outbound else f.get("in_id")
            neighbor_name = f.get("out_name") if is_outbound else f.get("in_name")
            neighbor_type = f.get("out_type") if is_outbound else f.get("in_type")

            if not neighbor_id or not neighbor_name:
                continue

            if neighbor_id not in all_nodes:
                all_nodes[neighbor_id] = {
                    "id": neighbor_id, "name": neighbor_name, "type": neighbor_type or "",
                }

            edge_key = (eid, pred, neighbor_id)
            if edge_key not in seen_edge_keys:
                seen_edge_keys.add(edge_key)
                all_edges.append({
                    "from_id": eid, "from_name": ename,
                    "to_id": neighbor_id, "to_name": neighbor_name,
                    "predicate": pred, "confidence": f.get("confidence", 1.0),
                    "depth": depth + 1,
                })

            segment = {
                "from": ename, "predicate": pred,
                "to": neighbor_name, "confidence": f.get("confidence", 1.0),
            }
            new_path = path + [segment]
            all_paths.append(new_path)

            if neighbor_id not in visited and (depth + 1) < max_depth:
                visited.add(neighbor_id)
                queue.append((neighbor_id, neighbor_name, depth + 1, new_path))

    seen = set()
    unique_paths = []
    for p in all_paths:
        key = " -> ".join(f"{s['from']}|{s['predicate']}|{s['to']}" for s in p)
        if key not in seen:
            seen.add(key)
            unique_paths.append(p)

    return {
        "status": "ok",
        "start_entity": {"id": start["id"], "name": start["name"], "type": start.get("type", "")},
        "max_depth": max_depth,
        "direction": direction,
        "predicate_filter": predicate,
        "nodes": list(all_nodes.values()),
        "node_count": len(all_nodes),
        "edges": all_edges,
        "edge_count": len(all_edges),
        "path_count": len(unique_paths),
        "paths": unique_paths,
    }
