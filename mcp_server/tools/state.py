"""
State + metadata tools. Pure SQLite reads (zero MySQL cost).

- seeql_list_servers          — list all monitored servers
- seeql_get_state_report      — the current structured state report
"""

import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    """Attach state-family tools to the FastMCP instance."""

    @mcp.tool(
        name="seeql_list_servers",
        description=(
            "List all MySQL servers SeeQL is monitoring. Returns server_id, "
            "display_name, environment, role, and whether the server is "
            "currently active. Use this to discover which server to target "
            "with other tools."
        ),
    )
    def list_servers_tool() -> list[dict]:
        return wrap_tool(safety, "seeql_list_servers", _list_servers_impl)()

    @mcp.tool(
        name="seeql_get_state_report",
        description=(
            "Produce the current Structured State Report for a server — "
            "top queries by total time, missing-index candidates, lock "
            "waits, buffer pool hit ratio, threads_running, QPS, long "
            "transactions, recent DDL changes, and anomalies. This is the "
            "primary entry point for 'what's happening right now?' RCA. "
            "Zero MySQL cost (reads pre-collected SQLite signals)."
        ),
    )
    def get_state_report_tool(server: str | None = None) -> dict:
        def _impl(server=None):
            return _state_report_impl(server)
        return wrap_tool(safety, "seeql_get_state_report", _impl)(server=server)


# ---------------------------------------------------------------------------
# Impls
# ---------------------------------------------------------------------------

def _list_servers_impl() -> list[dict]:
    from storage.connection import get_mon_reader
    with get_mon_reader() as conn:
        rows = conn.execute(
            """
            SELECT server_id, display_name, environment, role, cluster_id,
                   host, port, is_active
            FROM servers
            ORDER BY server_id
            """
        ).fetchall()
        return [dict(r) for r in rows]


def _state_report_impl(server: str | None) -> dict:
    server_id = _resolve_server(server)
    from agent.state_builder import build_state_report
    report = build_state_report(server_id=server_id)
    return {
        "server_id": server_id,
        "markdown": report.to_markdown(),
        "data": report.to_dict(),
    }


def _resolve_server(server: str | None) -> str:
    if server:
        return server
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
