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
from src.planner.executor import PlanExecutor, RetrievalExecutor, BudgetTracker
from src.router.policy import RoutingPolicy, QueryType, BudgetLevel
from .core import (
    _query_surreal,
    _extract_result,
    _clean_output,
    cost_tracker,
    MAX_CONTENT_LENGTH
)

MAX_CONTENT_LENGTH = 100_000


async def _store_content(content: str, source: str = "user_input", debug: bool = False) -> dict:
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

    from src.extraction.entropy_gate import EntropyGate
    gate = EntropyGate()
    # Quick gate check for the response (ingest() calls should_extract internally too)
    gate_result = gate.should_extract(content)
    event_id = await asyncio.to_thread(gate.ingest, content, source, debug)

    if event_id is None:
        return {"event_id": None, "status": "error", "source": source,
                "message": "storage failed – event could not be persisted"}
    return {"event_id": event_id, "status": "stored", "source": source,
            "gate": gate_result.get("decision", "active")}


async def _execute_query(query: str, cost_budget: str = "auto") -> dict:
    """Einzige Query-Implementierung: classify → route → execute → parse → track."""
    classifier = QueryClassifier()
    q_type_str, confidence = classifier.classify(query)
    
    # Convert string query type to enum
    try:
        if isinstance(q_type_str, str):
            q_type = QueryType(q_type_str)
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

    return {
        "query": query,
        "classified_as": q_type.value if hasattr(q_type, 'value') else str(q_type),
        "confidence": confidence,
        "strategy": strategy_dict["strategy"],
        "cost_budget": strategy_dict["cost_budget"],
        "results": {"entities": entities, "facts": facts, "events": events},
        "total": len(entities) + len(facts) + len(events),
        "budget_tracker_key": None,  # Simplified for now
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
    """Sort results into entities, facts, events buckets."""
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
    return entities, facts, events


async def _get_or_create_entity(name: str) -> Optional[str]:
    """Find an entity by name or create it with inferred type. Returns entity ID or None."""
    name_escaped = escape_surrealql(name)
    sql = f"SELECT id FROM entity WHERE name = '{name_escaped}' LIMIT 1;"
    result = await _query_surreal(sql)
    entities = _extract_result(result, 1)
    if entities:
        return entities[0]["id"]

    entity_type = infer_entity_type(name)
    create_sql = f"CREATE entity SET name = '{name_escaped}', type = '{entity_type}';"
    create_result = await _query_surreal(create_sql)
    created = _extract_result(create_result, 1)
    if created:
        return created[0]["id"]
    return None