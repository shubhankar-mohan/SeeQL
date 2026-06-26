"""
Query-focused read-only tools. All SQLite-only (zero MySQL cost).

- seeql_top_queries            — top-N digests by time / exec / rows
- seeql_get_query_history      — per-digest trend + latest cached EXPLAIN
- seeql_run_explain            — cached EXPLAIN for a digest
- seeql_search_slow_log        — keyword search in slow query log
- seeql_get_recent_analyses    — agent's own prior findings

The expensive `seeql_explain_query` (live EXPLAIN against prod) lives in
mcp_server/tools/action.py (MCP-4) because it's gated behind config.
"""

import json
import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_top_queries",
        description=(
            "Top-N query digests over the most recent snapshot window, "
            "sorted by a metric. `metric` is one of: total_time_sec, "
            "avg_time_sec, exec_count, rows_examined, ratio "
            "(rows_examined/rows_sent). Use `ratio` to surface "
            "missing-index candidates. Returns digest, digest_text, "
            "schema, timing stats, and scan counters."
        ),
    )
    def top_queries_tool(
        metric: str = "total_time_sec",
        server: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        def _impl(metric=metric, server=server, limit=limit):
            return _top_queries_impl(metric, server, limit)
        return wrap_tool(safety, "seeql_top_queries", _impl)(
            metric=metric, server=server, limit=limit,
        )

    @mcp.tool(
        name="seeql_get_query_history",
        description=(
            "Per-digest performance trend — avg_time_sec, exec_count, "
            "rows_examined over the last N days (default 7). Also returns "
            "the latest cached EXPLAIN if one was captured. Use this to "
            "answer 'when did this digest start getting slow?'."
        ),
    )
    def get_query_history_tool(digest: str, days: int = 7) -> dict:
        def _impl(digest=digest, days=days):
            return _query_history_impl(digest, days)
        return wrap_tool(safety, "seeql_get_query_history", _impl)(
            digest=digest, days=days,
        )

    @mcp.tool(
        name="seeql_run_explain",
        description=(
            "Return the most recent cached EXPLAIN plan for a digest. Pulls "
            "from explain_captures (populated by the medium loop for top-N "
            "expensive queries). Zero MySQL cost. For arbitrary SELECT "
            "EXPLAINs against production, use seeql_explain_query (behind "
            "an explicit config gate)."
        ),
    )
    def run_explain_tool(digest: str) -> dict:
        def _impl(digest=digest):
            return _run_explain_impl(digest)
        return wrap_tool(safety, "seeql_run_explain", _impl)(digest=digest)

    @mcp.tool(
        name="seeql_search_slow_log",
        description=(
            "Keyword-search the slow query log (populated by GCP Cloud "
            "Logging). Returns real SQL with actual parameter values, "
            "query_time_sec, lock_time_sec, rows examined/sent. Useful "
            "when you need to see the literal query that fired, not just "
            "the digest-ified form."
        ),
    )
    def search_slow_log_tool(keyword: str, limit: int = 10) -> dict:
        def _impl(keyword=keyword, limit=limit):
            return _search_slow_log_impl(keyword, limit)
        return wrap_tool(safety, "seeql_search_slow_log", _impl)(
            keyword=keyword, limit=limit,
        )

    @mcp.tool(
        name="seeql_get_recent_analyses",
        description=(
            "Agent's own prior analyses (routine, incident, investigation, "
            "replay). Essential for context — check this FIRST so you "
            "don't re-investigate what SeeQL has already reported."
        ),
    )
    def get_recent_analyses_tool(hours: int = 24, limit: int = 10) -> list[dict]:
        def _impl(hours=hours, limit=limit):
            return _recent_analyses_impl(hours, limit)
        return wrap_tool(safety, "seeql_get_recent_analyses", _impl)(
            hours=hours, limit=limit,
        )


# ---------------------------------------------------------------------------
# Impls — reuse agent.queries + agent.tools helpers where they already exist.
# ---------------------------------------------------------------------------

_TOP_METRIC_COLUMNS = {
    "total_time_sec": "total_time_sec",
    "avg_time_sec": "avg_time_sec",
    "exec_count": "exec_count",
    "rows_examined": "rows_examined",
    # ratio is computed in SQL below
}


def _top_queries_impl(
    metric: str, server: str | None, limit: int,
) -> list[dict]:
    from storage.connection import get_mon_reader
    sid = server or _default_server()
    limit = max(1, min(limit, 100))

    if metric == "ratio":
        order_clause = (
            "CASE WHEN rows_sent > 0 "
            "THEN CAST(rows_examined AS REAL) / rows_sent "
            "ELSE rows_examined END"
        )
        extra_where = "AND rows_sent > 0"
    else:
        col = _TOP_METRIC_COLUMNS.get(metric)
        if col is None:
            return [{"error": f"unknown metric '{metric}'. Valid: "
                     "total_time_sec, avg_time_sec, exec_count, "
                     "rows_examined, ratio"}]
        order_clause = col
        extra_where = ""

    sql = f"""
        SELECT digest, digest_text, schema_name,
               exec_count, total_time_sec, avg_time_sec, max_time_sec,
               rows_examined, rows_sent,
               full_scans, no_index_used,
               CASE WHEN rows_sent > 0
                    THEN CAST(rows_examined AS REAL) / rows_sent
                    ELSE rows_examined END AS ratio,
               snapshot_time
        FROM query_digest_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM query_digest_snapshots
              WHERE server_id = ?
          )
          {extra_where}
        ORDER BY {order_clause} DESC
        LIMIT ?
    """
    with get_mon_reader() as conn:
        rows = conn.execute(sql, (sid, sid, limit)).fetchall()
        return [dict(r) for r in rows]


def _query_history_impl(digest: str, days: int) -> dict:
    from agent.tools import _tool_get_query_history
    raw = _tool_get_query_history({"digest": digest, "days": max(1, min(days, 90))})
    return raw


def _run_explain_impl(digest: str) -> dict:
    from agent.tools import _tool_run_explain
    return _tool_run_explain({"digest": digest})


def _search_slow_log_impl(keyword: str, limit: int) -> dict:
    from agent.tools import _tool_search_slow_log
    return _tool_search_slow_log({
        "keyword": keyword,
        "limit": max(1, min(limit, 50)),
    })


def _recent_analyses_impl(hours: int, limit: int) -> list[dict]:
    from agent.tools import _tool_get_recent_analyses
    # The existing handler takes {hours, limit} per its signature in agent/tools.py
    result = _tool_get_recent_analyses({
        "hours": max(1, min(hours, 168)),
        "limit": max(1, min(limit, 50)),
    })
    # Handler returns a dict; normalize to a list if it wraps one.
    if isinstance(result, dict) and "analyses" in result:
        return result["analyses"]
    return result if isinstance(result, list) else [result]


def _default_server() -> str:
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
