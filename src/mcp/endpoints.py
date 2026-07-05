# -*- coding: utf-8 -*-
"""
HTTP Endpoints implementation
"""

import sys
import os
from typing import Any, Dict, Optional
from fastapi import Query

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from .core import app
from .common_logic import _store_content, _execute_query
from src.extraction.classifier import QueryClassifier
from src.planner.executor import PlanExecutor
from src.maintenance.conservative_maintainer import ConservativeMaintainer
from src.router.policy import RoutingPolicy


@app.get("/classify")
async def classify_endpoint(
    query: str = Query(..., description="The query to classify"),
):
    """Classify a query according to type and confidence"""
    classifier = QueryClassifier()
    query_type, confidence = classifier.classify(query)
    return {"query": query, "type": query_type, "confidence": confidence}


@app.get("/route")
async def route_endpoint(query: str = Query(..., description="The query to route")):
    """Route a query according to the routing policy"""
    classifier = QueryClassifier()
    policy = RoutingPolicy()

    query_type_str, confidence = classifier.classify(query)
    # Convert string to QueryType enum
    from src.router.policy import QueryType
    try:
        q_type_enum = QueryType(query_type_str)
    except ValueError:
        q_type_enum = QueryType.FACTUAL
    strategy_name, budget_level, policy_applied = policy.get_strategy(q_type_enum, confidence)

    return {
        "query": query,
        "classification": {"type": query_type_str, "confidence": confidence},
        "routing_strategy": {
            "strategy": strategy_name,
            "budget": budget_level.value if hasattr(budget_level, "value") else str(budget_level),
            "policy_applied": policy_applied,
        },
    }


@app.post("/plan_and_execute")
async def plan_and_execute_endpoint(request_data: dict):
    """Create and execute a plan for the given query"""
    query = request_data.get("query", "")
    classifier = QueryClassifier()
    policy = RoutingPolicy()
    executor = PlanExecutor()

    # Classify and route the query
    query_type_str, confidence = classifier.classify(query)
    # Convert string to QueryType enum
    from src.router.policy import QueryType
    try:
        q_type_enum = QueryType(query_type_str)
    except ValueError:
        q_type_enum = QueryType.FACTUAL
    strategy_name, budget_level, policy_applied = policy.get_strategy(q_type_enum, confidence)

    budget_str = budget_level.value if hasattr(budget_level, "value") else str(budget_level)

    result = await executor.execute_plan(
        strategy=strategy_name,
        query=query,
        budget_level=budget_str,
    )

    return result


@app.post("/maintain")
async def maintain_endpoint():
    """Perform maintenance operations"""
    maintainer = ConservativeMaintainer()
    result = await maintainer.perform_maintenance()

    return result


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "components": {
            "classifier": "ready",
            "router": "ready",
            "planner": "ready",
            "executor": "ready",
            "maintainer": "ready",
        },
    }


# HTTP Endpoints for MCP tools
@app.post("/memory/store")
async def memory_store_endpoint(request_data: dict):
    """Stores a new event in the raw event log. Runs through entropy gate. NOTE: Only English content should be stored — German or other languages produce noisy entity extraction."""
    return await _store_content(
        request_data.get("content", ""),
        request_data.get("source", "user_input"),
        metadata=request_data.get("metadata"),
    )


@app.post("/memory/store/batch")
async def memory_store_batch_endpoint(request_data: dict):
    """Stores multiple events in batch."""
    from .tools import memory_store_batch
    return await memory_store_batch(
        request_data.get("items", []),
        source=request_data.get("source", "user_input"),
    )


@app.post("/memory/query")
async def memory_query_endpoint(request_data: dict):
    """Routes a natural language query through the full pipeline: classify → plan → retrieve."""
    return await _execute_query(
        request_data.get("query", ""),
        request_data.get("cost_budget", "auto"),
        request_data.get("limit", 10),
    )


@app.post("/memory/update")
async def memory_update_endpoint(request_data: dict):
    """Updates a fact in the KG via logical invalidation. Old fact gets valid_until, new fact created.
    If the target entity does not exist yet, it will be created automatically."""
    # Import the tool function directly to reuse the logic
    from .tools import memory_update
    return await memory_update(
        subject=request_data.get("subject", ""),
        predicate=request_data.get("predicate", ""), 
        new_value=request_data.get("new_value", "")
    )


@app.get("/memory/event_log_search")
async def event_log_search_endpoint(
    query: str = Query(..., description="Search query"),
    limit: int = Query(10, description="Result limit"),
    offset: int = Query(0, description="Pagination offset"),
    since: Optional[str] = Query(None, description="Start date"),
    until: Optional[str] = Query(None, description="End date"),
    include_forgotten: bool = Query(False, description="Include forgotten events"),
):
    """Direct timeline query without router: hybrid search (BM25 + vector + RRF)."""
    # Import the tool function directly to reuse the logic
    from .tools import event_log_search
    return await event_log_search(query, limit=limit, offset=offset, since=since, until=until, include_forgotten=include_forgotten)


@app.get("/kg/query")
async def kg_query_endpoint(
    subject: Optional[str] = Query(None, description="Subject to query (in.name)"),
    object: Optional[str] = Query(None, description="Object to query (out.name)"),
    predicate: Optional[str] = Query(None, description="Predicate to query"),
    at_time: Optional[str] = Query(None, description="Time to query at"),
    limit: int = Query(100, description="Result limit (max facts to return)"),
    offset: int = Query(0, description="Pagination offset"),
):
    """Direct graph traversal: query facts by subject/object/predicate/time."""
    from .tools import kg_query
    return await kg_query(subject, object, predicate, at_time, limit=limit, offset=offset)


@app.get("/semantic/search")
async def semantic_search_endpoint(
    query: str = Query(..., description="Search query"),
    top_k: int = Query(10, description="Top-k results")
):
    """Pure vector search without KG. Filters out highly repetitive/noise content."""
    # Import the tool function directly to reuse the logic
    from .tools import semantic_search
    return await semantic_search(query, top_k)


@app.get("/memory/stats")
async def memory_stats_endpoint(random_string: str = ""):
    """Returns statistics about the memory system."""
    # Import the tool function directly to reuse the logic
    from .tools import memory_stats
    return await memory_stats(random_string)


@app.get("/explain/routing")
async def explain_routing_endpoint(query: str = Query(..., description="Query to explain routing for")):
    """Explains why the router chose a specific strategy for a query."""
    # Import the tool function directly to reuse the logic
    from .tools import memory_explain_routing
    return await memory_explain_routing(query)


@app.post("/memory/forget")
async def memory_forget_endpoint(request_data: dict):
    """Forgets a memory by event_id or entity."""
    # Import the tool function directly to reuse the logic
    from .tools import memory_forget
    return await memory_forget(
        entity=request_data.get("entity"),
        event_id=request_data.get("event_id"),
        reason=request_data.get("reason", "")
    )


@app.post("/memory/consolidate")
async def memory_consolidate_endpoint(request_data: dict):
    """Consolidates memory entries."""
    # Import the tool function directly to reuse the logic
    from .tools import memory_consolidate
    return await memory_consolidate(
        entity=request_data.get("entity"),
        scope=request_data.get("scope", "local"),
        delete_stale=request_data.get("delete_stale", False)
    )


@app.post("/memory/unforget")
async def memory_unforget_endpoint(request_data: dict):
    """Restores a previously forgotten event or entity. Resets forgotten=false.
    When restoring a forgotten entity, also restores all facts that were
    invalidated alongside it (clears valid_until and invalidated_reason)."""
    from .tools import memory_unforget
    return await memory_unforget(
        event_id=request_data.get("event_id", "")
    )


@app.get("/memory/entities")
async def list_entities_endpoint(
    limit: int = Query(20, description="Max results"),
    offset: int = Query(0, description="Pagination offset"),
    type: Optional[str] = Query(None, description="Filter by entity type"),
    name_contains: Optional[str] = Query(None, description="Case-insensitive substring filter on name"),
    sort_by: str = Query("name", description="Sort field: name, created_at, updated_at"),
    sort_order: str = Query("asc", description="Sort order: asc or desc"),
):
    """Lists entities in the knowledge graph with filtering and pagination."""
    from .tools import list_entities
    return await list_entities(limit, offset, type, name_contains, sort_by, sort_order)


@app.get("/memory/events")
async def list_events_endpoint(
    limit: int = Query(20, description="Max results"),
    offset: int = Query(0, description="Pagination offset"),
    since: Optional[str] = Query(None, description="Start date"),
    until: Optional[str] = Query(None, description="End date"),
    source: Optional[str] = Query(None, description="Filter by event source"),
    include_forgotten: bool = Query(False, description="Include forgotten events"),
):
    """Lists events from the raw event log with filtering and pagination."""
    from .tools import list_events
    return await list_events(limit, offset, since, until, source, include_forgotten)


@app.get("/graph/traverse")
async def graph_traverse_endpoint(
    start_entity: str = Query(..., description="Entity name to start from"),
    max_depth: int = Query(2, description="Max traversal depth (1-5)"),
    direction: str = Query("both", description="outbound, inbound, or both"),
    predicate: Optional[str] = Query(None, description="Filter by predicate type"),
    min_confidence: float = Query(0.0, description="Minimum confidence threshold"),
):
    """Multi-hop graph traversal: find paths by walking the knowledge graph."""
    from .tools import graph_traverse
    return await graph_traverse(start_entity, max_depth, direction, predicate, min_confidence)


@app.post("/memory/merge_entities")
async def memory_merge_entities_endpoint(request_data: dict):
    """Merges all facts from source_entity into target_entity, then forgets the source."""
    from .tools import memory_merge_entities
    return await memory_merge_entities(
        source_entity=request_data.get("source_entity", ""),
        target_entity=request_data.get("target_entity", ""),
        dry_run=request_data.get("dry_run", False),
    )