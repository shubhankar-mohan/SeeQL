"""
Schema / index / DDL tools. Read-only (SQLite + cached snapshots).

- seeql_get_table_schema       — cached DDL (via agent.tools, which falls back to live only on miss)
- seeql_list_unused_indexes    — rows from unused_index_snapshots
- seeql_list_redundant_indexes — rows from redundant_index_snapshots
- seeql_get_recent_ddl_changes — ddl_changes for a server in the last N hours
- seeql_get_lock_graph         — recent lock + txn snapshot
"""

import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_get_table_schema",
        description=(
            "Return CREATE TABLE DDL for a given schema.table. Uses "
            "cached schema_snapshots when available; falls back to live "
            "SHOW CREATE TABLE if not. Useful for index audits and query "
            "planning questions."
        ),
    )
    def get_table_schema_tool(
        schema: str, table: str, server: str | None = None,
    ) -> dict:
        def _impl(schema=schema, table=table, server=server):
            return _get_table_schema_impl(schema, table, server)
        return wrap_tool(safety, "seeql_get_table_schema", _impl)(
            schema=schema, table=table, server=server,
        )

    @mcp.tool(
        name="seeql_list_unused_indexes",
        description=(
            "List indexes detected as unused via sys.schema_unused_indexes "
            "(populated every 30 min by the slow loop). Cross-check this "
            "before recommending DROP INDEX — these are candidates to drop."
        ),
    )
    def list_unused_indexes_tool(
        server: str | None = None, limit: int = 50,
    ) -> list[dict]:
        def _impl(server=server, limit=limit):
            return _list_unused_indexes_impl(server, limit)
        return wrap_tool(safety, "seeql_list_unused_indexes", _impl)(
            server=server, limit=limit,
        )

    @mcp.tool(
        name="seeql_list_redundant_indexes",
        description=(
            "List redundant indexes (sys.schema_redundant_indexes). Each "
            "row includes the suggested DROP INDEX statement. Cross-check "
            "before any index recommendation to avoid duplicating a "
            "dominant index."
        ),
    )
    def list_redundant_indexes_tool(
        server: str | None = None, limit: int = 50,
    ) -> list[dict]:
        def _impl(server=server, limit=limit):
            return _list_redundant_indexes_impl(server, limit)
        return wrap_tool(safety, "seeql_list_redundant_indexes", _impl)(
            server=server, limit=limit,
        )

    @mcp.tool(
        name="seeql_get_recent_ddl_changes",
        description=(
            "DDL changes detected by SeeQL's schema-hash diff in the last "
            "N hours (default 24). Returns table, change_type "
            "(schema|index|both), old/new DDL, and detected_at. Essential "
            "for correlating 'query got slow' with 'someone changed "
            "schema'."
        ),
    )
    def recent_ddl_tool(
        hours: int = 24, server: str | None = None, limit: int = 50,
    ) -> list[dict]:
        def _impl(hours=hours, server=server, limit=limit):
            return _recent_ddl_impl(hours, server, limit)
        return wrap_tool(safety, "seeql_get_recent_ddl_changes", _impl)(
            hours=hours, server=server, limit=limit,
        )

    @mcp.tool(
        name="seeql_get_lock_graph",
        description=(
            "Return a recent snapshot of lock waits and active transactions "
            "(last few minutes, from the fast loop). Use when a user asks "
            "'is the server locked up right now?' and you want to answer "
            "without a live MySQL call."
        ),
    )
    def lock_graph_tool() -> dict:
        return wrap_tool(safety, "seeql_get_lock_graph", _lock_graph_impl)()


# ---------------------------------------------------------------------------
# Impls
# ---------------------------------------------------------------------------

def _get_table_schema_impl(schema: str, table: str, server: str | None) -> dict:
    # agent.tools._tool_get_table_schema uses the current-server ContextVar for
    # its live fallback, and reads schema_name / table_name from the payload.
    from agent.tools import _tool_get_table_schema, set_current_server
    sid = server or _default_server()
    set_current_server(sid)
    return _tool_get_table_schema({"schema_name": schema, "table_name": table})


def _list_unused_indexes_impl(server: str | None, limit: int) -> list[dict]:
    from storage.connection import get_mon_reader
    sid = server or _default_server()
    sql = """
        SELECT object_schema, table_name, index_name, MAX(snapshot_time) AS last_seen
        FROM unused_index_snapshots
        WHERE server_id = ?
        GROUP BY object_schema, table_name, index_name
        ORDER BY last_seen DESC
        LIMIT ?
    """
    with get_mon_reader() as conn:
        rows = conn.execute(sql, (sid, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]


def _list_redundant_indexes_impl(server: str | None, limit: int) -> list[dict]:
    from storage.connection import get_mon_reader
    sid = server or _default_server()
    sql = """
        SELECT table_schema, table_name, redundant_index_name,
               redundant_index_columns, dominant_index_name,
               dominant_index_columns, sql_drop_index,
               MAX(snapshot_time) AS last_seen
        FROM redundant_index_snapshots
        WHERE server_id = ?
        GROUP BY table_schema, table_name, redundant_index_name
        ORDER BY last_seen DESC
        LIMIT ?
    """
    with get_mon_reader() as conn:
        rows = conn.execute(sql, (sid, max(1, min(limit, 500)))).fetchall()
        return [dict(r) for r in rows]


def _recent_ddl_impl(
    hours: int, server: str | None, limit: int,
) -> list[dict]:
    from storage.connection import get_mon_reader
    sid = server or _default_server()
    sql = """
        SELECT id, detected_at, table_schema, table_name, change_type,
               old_schema_hash, new_schema_hash, old_ddl, new_ddl
        FROM ddl_changes
        WHERE server_id = ?
          AND detected_at >= datetime('now', ?)
        ORDER BY detected_at DESC
        LIMIT ?
    """
    lookback = f"-{max(1, min(hours, 24 * 30))} hours"
    with get_mon_reader() as conn:
        rows = conn.execute(
            sql, (sid, lookback, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(r) for r in rows]


def _lock_graph_impl() -> dict:
    from agent.tools import _tool_get_lock_graph
    return _tool_get_lock_graph({})


def _default_server() -> str:
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
