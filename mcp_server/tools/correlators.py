"""
Correlator tools — thin wrappers around alerting.correlators.
"""

import logging
from datetime import datetime, timedelta, timezone

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_find_missing_index_candidates",
        description=(
            "Run the missing-index correlator over a time window. Joins "
            "rows_examined/rows_sent ratios, cached EXPLAIN plans, recent "
            "DDL (including dropped indexes), unused indexes, and "
            "redundant indexes into structured evidence. Essential tool "
            "for 'why did this query get slow?' — returns per-digest "
            "evidence with a confidence score. Zero MySQL cost."
        ),
    )
    def find_missing_index_candidates_tool(
        server: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        suspect_digests: list[str] | None = None,
        top_n: int = 5,
    ) -> dict:
        def _impl(
            server=server,
            window_start=window_start,
            window_end=window_end,
            suspect_digests=suspect_digests,
            top_n=top_n,
        ):
            return _find_missing_index_impl(
                server, window_start, window_end, suspect_digests, top_n,
            )
        return wrap_tool(safety, "seeql_find_missing_index_candidates", _impl)(
            server=server,
            window_start=window_start,
            window_end=window_end,
            suspect_digests=suspect_digests,
            top_n=top_n,
        )


def _find_missing_index_impl(
    server: str | None,
    window_start: str | None,
    window_end: str | None,
    suspect_digests: list[str] | None,
    top_n: int,
) -> dict:
    from alerting.correlators.missing_index import correlate_missing_index
    sid = server or _default_server()
    now = datetime.now(timezone.utc)
    ws = window_start or (now - timedelta(hours=1)).isoformat()
    we = window_end or now.isoformat()
    correlation = correlate_missing_index(
        server_id=sid,
        window_start=ws,
        window_end=we,
        suspect_digests=suspect_digests,
        top_n=max(1, min(top_n, 20)),
    )
    d = correlation.to_dict()
    d["markdown"] = correlation.to_markdown()
    return d


def _default_server() -> str:
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
