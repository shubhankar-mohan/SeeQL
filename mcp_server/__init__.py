"""
SeeQL MCP server.

Exposes SeeQL's monitoring surface via Model Context Protocol so external
Claude clients (Claude Desktop, Claude Code, remote HTTP/SSE clients) can
use it as a full RCA tool: list and inspect investigations/incidents,
read SQLite-cached signals with zero MySQL load, take safe live reads
against production MySQL (budgeted), and — behind config gates —
trigger new investigations or run arbitrary EXPLAINs.

Entry point:
    from mcp_server.server import create_server, run_stdio, run_http
"""

from mcp_server.server import create_server, run_stdio, run_http

__all__ = ["create_server", "run_stdio", "run_http"]
