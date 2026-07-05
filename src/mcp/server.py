# -*- coding: utf-8 -*-
"""
Control Plane Server implementing the Model Context Protocol (MCP)
Coordinates all components of the agent memory system

This is the main entry point that ties together all modular components:
- Core infrastructure (connection management, resilience)
- Common logic functions
- MCP tools
- HTTP endpoints
"""

import asyncio
import os
import threading
from typing import NoReturn

# Import all components to register them
from .core import (
    app,  # FastAPI app
    mcp,  # MCP instance
    ensure_schema_loaded,
    cost_tracker,
    _query_surreal,
    _extract_result,
    _clean_output
)

from .common_logic import (
    _store_content,
    _execute_query,
    _get_or_create_entity
)

from .tools import (
    memory_store,
    memory_query,
    memory_update,
    memory_stats,
    event_log_search,
    kg_query,
    graph_traverse,
    semantic_search,
    memory_explain_routing,
    memory_forget,
    memory_unforget,
    memory_consolidate,
    list_entities,
    list_events,
)

# Import endpoints module to register HTTP endpoints (without importing specific functions)
from .endpoints import *  # This ensures all endpoints are registered

# Import embedding service to initialize it
from src.extraction.embedding_service import get_embedding_service


def _start_http_server():
    """Start the FastAPI HTTP server on the configured port."""
    import uvicorn

    control_plane_port = int(os.getenv("CONTROL_PLANE_PORT", "8082"))
    print(f"[INFO] Starting HTTP server on 0.0.0.0:{control_plane_port}")
    uvicorn.run(app, host="0.0.0.0", port=control_plane_port, log_level="info")


if __name__ == "__main__":
    # Ensure schema is loaded before starting servers
    asyncio.run(ensure_schema_loaded())

    # Pre-load embedding service to avoid hang on first tool call
    get_embedding_service()

    # Start HTTP server in a background thread
    http_thread = threading.Thread(target=_start_http_server, daemon=True)
    http_thread.start()

    # Run FastMCP stdio server (for MCP clients) in main thread
    try:
        mcp.run()
    except KeyboardInterrupt:
        print("[INFO] Shutting down server...")
        # Add any cleanup code here if needed

# Export the main components for easy access
__all__ = [
    'app',
    'mcp',
    'ensure_schema_loaded',
    'cost_tracker',
    '_query_surreal',
    '_extract_result',
    '_clean_output',
    '_store_content',
    '_execute_query',
    'memory_store',
    'memory_store_batch',
    'memory_query',
    'memory_update',
    'memory_stats',
    'event_log_search',
    'kg_query',
    'graph_traverse',
    'semantic_search',
    'memory_explain_routing',
    'memory_forget',
    'memory_consolidate'
]