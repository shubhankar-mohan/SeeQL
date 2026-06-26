"""
Live-MySQL read tools. Every call goes through MCPSafety.check_budget
(classified in LIVE_TOOLS) — the per-session cap prevents a runaway
client from piling on load, and the rate limiter bounds sustained
throughput per tool.

- seeql_get_live_processlist    — current threads (non-Sleep, top 20)
- seeql_get_live_locks           — perf_schema.data_lock_waits snapshot
- seeql_get_live_transactions    — information_schema.innodb_trx
- seeql_get_live_innodb_status   — parsed SHOW ENGINE INNODB STATUS
- seeql_get_index_stats          — perf_schema + information_schema index usage
- seeql_get_table_status         — size + fragmentation

All handlers delegate to the existing agent.tools._tool_get_live_* functions
(which already implement timeouts, retries, and the same SET SESSION
MAX_EXECUTION_TIME guard the webhook investigator uses).
"""

import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_get_live_processlist",
        description=(
            "Current MySQL processlist (non-Sleep threads, top 20 by time). "
            "ONE live-MySQL query. Use when you need to see what's running "
            "RIGHT NOW — e.g., during an active incident."
        ),
    )
    def live_processlist_tool(server: str | None = None) -> dict:
        def _impl(server=server):
            return _with_server("get_live_processlist", server)
        return wrap_tool(safety, "seeql_get_live_processlist", _impl)(server=server)

    @mcp.tool(
        name="seeql_get_live_locks",
        description=(
            "Current InnoDB lock waits from performance_schema.data_lock_waits. "
            "ONE live-MySQL query. Use to diagnose active lock contention."
        ),
    )
    def live_locks_tool(server: str | None = None) -> dict:
        def _impl(server=server):
            return _with_server("get_live_locks", server)
        return wrap_tool(safety, "seeql_get_live_locks", _impl)(server=server)

    @mcp.tool(
        name="seeql_get_live_transactions",
        description=(
            "Active transactions from information_schema.innodb_trx. "
            "ONE live-MySQL query. Use to find long-running transactions "
            "that may be holding locks."
        ),
    )
    def live_transactions_tool(server: str | None = None) -> dict:
        def _impl(server=server):
            return _with_server("get_live_transactions", server)
        return wrap_tool(safety, "seeql_get_live_transactions", _impl)(server=server)

    @mcp.tool(
        name="seeql_get_live_innodb_status",
        description=(
            "SHOW ENGINE INNODB STATUS, parsed into sections: LATEST "
            "DETECTED DEADLOCK, TRANSACTIONS, BUFFER POOL AND MEMORY, etc. "
            "ONE live-MySQL query. Use for deadlock forensics."
        ),
    )
    def live_innodb_status_tool(server: str | None = None) -> dict:
        def _impl(server=server):
            return _with_server("get_live_innodb_status", server)
        return wrap_tool(safety, "seeql_get_live_innodb_status", _impl)(server=server)

    @mcp.tool(
        name="seeql_get_index_stats",
        description=(
            "Per-index usage statistics (perf_schema) + definitions "
            "(information_schema). TWO live-MySQL queries. Use when you "
            "need to answer 'which indexes are actually being used?' or "
            "before proposing CREATE/DROP INDEX."
        ),
    )
    def index_stats_tool(
        schema: str, table: str, server: str | None = None,
    ) -> dict:
        def _impl(schema=schema, table=table, server=server):
            return _with_server_and_args(
                "get_index_stats", server, {"schema": schema, "table": table},
            )
        return wrap_tool(safety, "seeql_get_index_stats", _impl)(
            schema=schema, table=table, server=server,
        )

    @mcp.tool(
        name="seeql_get_table_status",
        description=(
            "Table size, row count estimate, data/index length, "
            "fragmentation ratio. ONE live-MySQL query (information_schema.tables)."
        ),
    )
    def table_status_tool(
        schema: str, table: str, server: str | None = None,
    ) -> dict:
        def _impl(schema=schema, table=table, server=server):
            return _with_server_and_args(
                "get_table_status", server, {"schema": schema, "table": table},
            )
        return wrap_tool(safety, "seeql_get_table_status", _impl)(
            schema=schema, table=table, server=server,
        )


# ---------------------------------------------------------------------------
# Dispatch helpers
# ---------------------------------------------------------------------------

_TOOL_MAP = {
    "get_live_processlist": "_tool_get_live_processlist",
    "get_live_locks": "_tool_get_live_locks",
    "get_live_transactions": "_tool_get_live_transactions",
    "get_live_innodb_status": "_tool_get_live_innodb_status",
    "get_index_stats": "_tool_get_index_stats",
    "get_table_status": "_tool_get_table_status",
}


def _with_server(tool_key: str, server: str | None) -> dict:
    return _with_server_and_args(tool_key, server, {})


def _with_server_and_args(
    tool_key: str, server: str | None, input_data: dict,
) -> dict:
    from agent import tools as agent_tools
    fn_name = _TOOL_MAP[tool_key]
    fn = getattr(agent_tools, fn_name)

    sid = server or _default_server()
    agent_tools.set_current_server(sid)
    return fn(input_data)


def _default_server() -> str:
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
