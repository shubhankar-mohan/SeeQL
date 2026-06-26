"""
Tool registrations. Each submodule exposes a `register(mcp, safety)`
function that decorates its tool functions with `@mcp.tool()` after
wrapping them through `safety.wrap_tool`.

The pattern: every tool function is a plain Python function; it becomes
an MCP tool when the submodule's `register` is called at server startup.
This keeps each tool module import-cheap and testable in isolation.
"""

from mcp_server.tools import (
    state,
    investigations,
    incidents,
    replay,
    query,
    schema,
    correlators,
    live,
    action,
)

__all__ = [
    "state",
    "investigations",
    "incidents",
    "replay",
    "query",
    "schema",
    "correlators",
    "live",
    "action",
]


def register_all(mcp, safety) -> None:
    """Call each submodule's `register(mcp, safety)`. Order doesn't matter."""
    state.register(mcp, safety)
    investigations.register(mcp, safety)
    incidents.register(mcp, safety)
    replay.register(mcp, safety)
    query.register(mcp, safety)
    schema.register(mcp, safety)
    correlators.register(mcp, safety)
    live.register(mcp, safety)
    action.register(mcp, safety)
