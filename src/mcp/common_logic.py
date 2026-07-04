# -*- coding: utf-8 -*-
"""
Common logic functions shared between HTTP endpoints and MCP tools
"""

import asyncio
import sys
import os
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.extraction.classifier import QueryClassifier
from src.extraction.embedding_service import get_embedding_service
from src.extraction.entropy_gate import escape_surrealql
from src.extraction.entity_utils import infer_entity_type
from src.maintenance.conservative_maintainer import ConservativeMaintainer
from src.planner.executor import PlanExecutor, RetrievalExecutor
from src.router.budget import BudgetLevel, BudgetTracker
from src.router.policy import RoutingPolicy, QueryType
from .core import (
    _query_surreal,
    _extract_result,
    _clean_output,
    cost_tracker,
    MAX_CONTENT_LENGTH
)

MAX_CONTENT_LENGTH = 100_000

_entropy_gate_instance = None


def _get_entropy_gate():
    global _entropy_gate_instance
    if _entropy_gate_instance is None:
        from src.extraction.entropy_gate import EntropyGate
        _entropy_gate_instance = EntropyGate()
    return _entropy_gate_instance


async def _store_content(content: str, source: str = "user_input", debug: bool = False, metadata: Optional[Dict[str, Any]] = None) -> dict:
    """Einzige Store-Implementierung: validiert → EntropyGate → Event + ggf. KG."""
    if not content or not content.strip():
        return {"event_id": None, "status": "error", "source": source,
                "message": "content must not be empty or whitespace-only"}
    if '\x00' in content:
        return {"event_id": None, "status": "error", "source": source,
                "message": "content contains null bytes, rejecting"}
    if len(content) > MAX_CONTENT_LENGTH:
        return {"event_id": None, "status": "error", "source": source,
                "message": f"content exceeds maximum length of {MAX_CONTENT_LENGTH} (got {len(content)})"}

    gate = _get_entropy_gate()
    gate_result = await asyncio.to_thread(gate.should_extract, content)
    event_id, kg_result = await asyncio.to_thread(gate.ingest, content, source, debug=False, metadata=metadata)

    if event_id is None:
        return {"event_id": None, "status": "error", "source": source,
                "message": "storage failed – event could not be persisted"}

    gate_info = {
        "decision": gate_result.get("decision", "active"),
    }
    if gate_result.get("decision") != "extract":
        gate_info["reason"] = gate_result.get("reason", "unknown")
        gate_info["composite_score"] = gate_result.get("composite_score")
        gate_info["threshold"] = gate_result.get("threshold")

    # KG-Resultate aus ingest() verwenden (kein zweiter _extract_to_kg-Aufruf!)
    if kg_result:
        gate_info["kg"] = {"entities_created": kg_result.get("entities_created", 0),
                            "facts_created": kg_result.get("facts_created", 0)}

    return {"event_id": event_id, "status": "stored", "source": source,
            "gate": gate_info}


async def _execute_query(query: str, cost_budget: str = "auto", limit: int = 10) -> dict:
    """Einzige Query-Implementierung: classify → route → execute → parse → track."""
    classifier = QueryClassifier()
    q_type_str, confidence = classifier.classify(query)
    
    # Convert string query type to enum
    # Classifier returns "multi-hop" but QueryType expects "multi_hop"
    try:
        if isinstance(q_type_str, str):
            q_type_normalized = q_type_str.replace("-", "_")
            q_type = QueryType(q_type_normalized)
        else:
            q_type = q_type_str
    except ValueError:
        # If the string doesn't match a known enum value, default to factual
        q_type = QueryType.FACTUAL
        print(f"Warning: Unknown query type '{q_type_str}', defaulting to FACTUAL")

    policy = RoutingPolicy()
    strategy_info = policy.get_strategy(q_type, confidence)
    
    # Unpack the tuple returned by get_strategy
    strategy, budget_level, policy_applied = strategy_info
    
    # Create strategy dict with required fields
    strategy_dict = {
        "strategy": strategy,
        "cost_budget": cost_budget if cost_budget != "auto" else budget_level.value,
        "policy_applied": policy_applied
    }

    executor = RetrievalExecutor()
    
    # Create a BudgetTracker instance with the appropriate budget level
    # Convert string budget level to BudgetLevel enum
    try:
        budget_enum = BudgetLevel(strategy_dict["cost_budget"])
    except ValueError:
        # If the budget level is not valid, default to MEDIUM
        budget_enum = BudgetLevel.MEDIUM
    
    budget_tracker = BudgetTracker(budget_enum)
    
    try:
        # Convert strategy string to RetrievalStrategy enum
        from src.planner.executor import RetrievalStrategy
        try:
            strategy_enum = RetrievalStrategy(strategy_dict["strategy"])
        except ValueError:
            # If the strategy is not valid, default to event log first
            strategy_enum = RetrievalStrategy.EVENT_LOG_FIRST
        
        results_raw = await executor.execute_strategy(strategy_enum, query, budget_tracker)
        results = _flatten_query_results(results_raw)
    except Exception as e:
        print(f"Error executing strategy: {e}")
        # Fallback: return empty results
        results = []

    entities, facts, events = _categorize_results(results)

    summary = _synthesize_answer(query, entities, facts, events)

    # Apply limit to each result category
    entities = entities[:limit]
    facts = facts[:limit]
    events = events[:limit]

    return {
        "query": query,
        "classified_as": q_type.value if hasattr(q_type, 'value') else str(q_type),
        "confidence": confidence,
        "strategy": strategy_dict["strategy"],
        "cost_budget": strategy_dict["cost_budget"],
        "results": {"entities": entities, "facts": facts, "events": events},
        "total": len(entities) + len(facts) + len(events),
        "summary": {
            "found": summary["found"],
            "answer": summary["answer"],
            "total_facts": summary["total_facts"],
            "total_entities": summary["total_entities"],
            "total_events": summary["total_events"],
        },
        "budget_tracker_key": None,
    }


def _flatten_query_results(results_raw: Any) -> List[Any]:
    """Flatten the various result shapes from different strategies into one list."""
    if isinstance(results_raw, dict) and "combined_results" in results_raw:
        combined = results_raw["combined_results"]
        return combined if isinstance(combined, list) else [combined]

    if isinstance(results_raw, dict):
        flat = []
        for key in ("events", "entities", "facts", "keyword_results", "vector_results", "temporal_results", "result"):
            val = results_raw.get(key)
            if isinstance(val, list):
                flat.extend(val)
        if not flat and "result" in results_raw:
            val = results_raw["result"]
            flat = val if isinstance(val, list) else [val]
        return flat

    if isinstance(results_raw, list):
        return results_raw
    return []


def _categorize_results(results: List[Any]) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Sort results into entities, facts, events buckets and enrich fact names."""
    entities, facts, events = [], [], []
    for r in results:
        if isinstance(r, dict):
            clean = _clean_output(r)
            rid = clean.get("id", "")
            if isinstance(rid, str):
                if rid.startswith("entity:"):
                    entities.append(clean)
                elif rid.startswith("fact:"):
                    facts.append(clean)
                elif rid.startswith("event:"):
                    events.append(clean)
                else:
                    events.append(clean)
            else:
                events.append(clean)
        elif isinstance(r, list):
            for item in r:
                if isinstance(item, dict):
                    clean = _clean_output(item)
                    rid = clean.get("id", "")
                    if isinstance(rid, str) and rid.startswith("entity:"):
                        entities.append(clean)
                    elif isinstance(rid, str) and rid.startswith("fact:"):
                        facts.append(clean)
                    else:
                        events.append(clean)

    NOISY_PREDICATES = {"weakly_related", "mentions"}
    FACTS_MIN_CONFIDENCE = 0.5
    filtered_facts = []
    for f in facts:
        pred = f.get("predicate", "")
        conf = f.get("confidence", 0) or 0
        if pred in NOISY_PREDICATES:
            continue
        if pred in ("co_occurs_with", "strongly_related", "related_to") and conf < FACTS_MIN_CONFIDENCE:
            continue
        # Ontologie-Validierung: Prädikat muss zu Entity-Typen passen
        in_data = f.get("in", {})
        out_data = f.get("out", {})
        in_type = in_data.get("type", "") if isinstance(in_data, dict) else ""
        out_type = out_data.get("type", "") if isinstance(out_data, dict) else ""
        if pred not in ("related_to", "co_occurs_with", "strongly_related", "weakly_related", "mentions"):
            in_name = in_data.get("name", "") if isinstance(in_data, dict) else ""
            out_name = out_data.get("name", "") if isinstance(out_data, dict) else ""
            # Nur types via infer_entity_type ergänzen wenn der Name keine SurrealDB-ID ist
            if not in_type and in_name and not in_name.startswith("entity:") and not in_name.startswith("fact:"):
                in_type = infer_entity_type(in_name)
            if not out_type and out_name and not out_name.startswith("entity:") and not out_name.startswith("fact:"):
                out_type = infer_entity_type(out_name)
            # Validierung wenn beide types bekannt sind und keiner "concept" (Fallback) ist
            if in_type and out_type and in_type != "concept" and out_type != "concept":
                from src.extraction.entity_utils import validate_predicate
                if not validate_predicate(in_type, pred, out_type):
                    continue
            # Fallback: wenn Typen nicht bestimmbar (entity-ID als name), require high confidence
            elif conf < 0.8:
                continue
        filtered_facts.append(f)
    facts = filtered_facts

    name_to_entity = {}
    for e in entities:
        eid = e.get("id")
        ename = e.get("name")
        if eid and ename:
            name_to_entity[eid] = ename

    for f in facts:
        # Normalize executor format (in_name/in_id) to in/out dicts
        for key, name_key, type_key, id_key in [
            ('in', 'in_name', 'in_type', 'in_id'),
            ('out', 'out_name', 'out_type', 'out_id'),
        ]:
            val = f.get(key)
            if not isinstance(val, (dict, str)):
                name_val = f.pop(name_key, None) if name_key in f else None
                type_val = f.pop(type_key, None) if type_key in f else None
                id_val = f.pop(id_key, None) if id_key in f else None
                if id_val:
                    f[key] = {"id": id_val, "name": name_val, "type": type_val}
                    val = f[key]

            if isinstance(val, dict):
                val_id = val.get("id")
                if val_id and val_id not in name_to_entity:
                    for e in entities:
                        if e.get("id") == val_id:
                            name_to_entity[val_id] = e.get("name", val_id)
                            break

        for key in ('in', 'out'):
            val = f.get(key)
            if isinstance(val, str):
                f[key] = {"id": val, "name": name_to_entity.get(val, val), "type": ""}
            elif isinstance(val, dict):
                val_id = val.get("id", "")
                val_name = val.get("name")
                if not val_name and val_id in name_to_entity:
                    val_name = name_to_entity[val_id]
                f[key] = {
                    "id": val_id,
                    "name": val_name if val_name else val_id,
                    "type": val.get("type", ""),
                }

    return entities, facts, events


def _synthesize_answer(query: str, entities: List[Dict], facts: List[Dict], events: List[Dict]) -> Dict[str, Any]:
    """Synthesize a structured answer from entities, facts, and events."""
    answer_parts = []
    key_facts = []
    key_entities = []
    event_snippets = []

    for f in facts[:5]:
        in_data = f.get("in")
        out_data = f.get("out")
        subj = ""
        obj = ""
        if isinstance(in_data, dict):
            subj = in_data.get("name", in_data.get("id", ""))
        elif isinstance(in_data, str):
            subj = in_data
        if isinstance(out_data, dict):
            obj = out_data.get("name", out_data.get("id", ""))
        elif isinstance(out_data, str):
            obj = out_data
        pred = f.get("predicate", "")
        if subj and obj and pred and pred not in ("mentions", "weakly_related"):
            key_facts.append(f"{subj} {pred} {obj}")

    for e in entities[:5]:
        name = e.get("name", "")
        etype = e.get("type", "")
        if name:
            key_entities.append(f"{name} ({etype})" if etype else name)

    import re
    query_lower = query.lower()
    query_words = [w for w in re.findall(r'\b\w+\b', query_lower) if len(w) > 2]
    for ev in events[:5]:
        content = ev.get("content", "")
        if content:
            score = 0
            matched = set()
            for word in query_words:
                if word.lower() in content.lower():
                    score += 1
                    matched.add(word)
            if score > 0:
                event_snippets.append({
                    "content": content[:400],
                    "relevance_hits": score,
                    "matched_terms": sorted(matched),
                    "source": ev.get("source", ""),
                    "timestamp": ev.get("timestamp", ""),
                })

    event_snippets.sort(key=lambda x: x["relevance_hits"], reverse=True)

    if key_facts:
        answer_parts.append({"type": "facts_found", "detail": key_facts})
    if key_entities:
        answer_parts.append({"type": "entities_found", "detail": key_entities})
    if event_snippets:
        answer_parts.append({"type": "relevant_events", "detail": event_snippets[:3]})

    has_content = bool(key_facts or key_entities or event_snippets)

    # Build a concise natural-language summary
    text_parts = []
    if key_facts:
        text_parts.append(". ".join(key_facts[:3]))
    if key_entities:
        text_parts.append("Related entities: " + ", ".join(key_entities[:5]))
    if event_snippets:
        best = event_snippets[0]
        text_parts.append(f"Best match: \"{best['content'][:150]}...\" (source: {best['source']}, hits: {best['relevance_hits']})")

    answer_text = ". ".join(text_parts) if text_parts else ""

    return {
        "found": has_content,
        "answer": answer_text,
        "parts": answer_parts,
        "total_facts": len(facts),
        "total_entities": len(entities),
        "total_events": len(events),
    }


async def _get_or_create_entity(name: str) -> Optional[str]:
    """Find an entity by name or create it with inferred type. Returns entity ID or None."""
    name_escaped = escape_surrealql(name)
    sql = f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
    result = await _query_surreal(sql)
    entities = _extract_result(result, 1)
    if entities:
        return entities[0]["id"]

    entity_type = infer_entity_type(name)
    create_sql = f"CREATE entity SET name = '{name_escaped}', type = '{entity_type}', created_at = time::now(), updated_at = time::now();"
    create_result = await _query_surreal(create_sql)
    created = _extract_result(create_result, 1)
    if created:
        return created[0]["id"]
    return None