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

# Import all components to register them
from .core import (
    app,  # FastAPI app
    mcp,  # MCP instance
    ensure_schema_loaded,
    cost_tracker,
    _query_surreal,
    _extract_result,
    _clean_output,
    _get_or_create_entity
)

from .common_logic import (
    _store_content,
    _execute_query,
    _flatten_query_results,
    _categorize_results,
    _get_or_create_entity as common_get_or_create_entity
)

from .tools import (
    memory_store,
    memory_query,
    memory_update,
    memory_stats,
    event_log_search,
    kg_query,
    semantic_search,
    explain_routing,
    memory_forget,
    memory_consolidate
)

from .endpoints import (
    # All HTTP endpoints are registered when importing this module
)

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
    mcp.run()

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
    'memory_query',
    'memory_update',
    'memory_stats',
    'event_log_search',
    'kg_query',
    'semantic_search',
    'explain_routing',
    'memory_forget',
    'memory_consolidate'
]