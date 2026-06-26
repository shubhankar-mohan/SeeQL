"""
Investigation-family tools.

Read-only:
- seeql_list_investigations   — browse recent
- seeql_get_investigation     — full detail (row + findings + samples)

Action tools (trigger / abort) live in mcp_server/tools/action.py (MCP-4).
"""

import json
import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_list_investigations",
        description=(
            "List recent webhook-triggered investigations. Each row includes "
            "provider, alert_type, severity, status (queued, phase1..phase3, "
            "completed, aborted, load_guard_paused), server_id, started/ended "
            "timestamps, confidence (if scored), and root_cause_summary. "
            "Filter by status or server. Zero MySQL cost."
        ),
    )
    def list_investigations_tool(
        status: str | None = None,
        server: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        def _impl(status=None, server=None, limit=20):
            return _list_investigations_impl(status, server, limit)
        return wrap_tool(safety, "seeql_list_investigations", _impl)(
            status=status, server=server, limit=limit,
        )

    @mcp.tool(
        name="seeql_get_investigation",
        description=(
            "Full detail for a single investigation: the investigations row, "
            "every finding (phase 1/2/3 hypotheses, correlations, evidence, "
            "root causes), and a rollup of Phase-3 samples grouped by "
            "sample_type with timing. Use this to understand what an "
            "investigation concluded and how it got there."
        ),
    )
    def get_investigation_tool(id: int) -> dict:
        def _impl(id: int):
            return _get_investigation_impl(id)
        return wrap_tool(safety, "seeql_get_investigation", _impl)(id=id)


# ---------------------------------------------------------------------------
# Impls
# ---------------------------------------------------------------------------

def _list_investigations_impl(
    status: str | None,
    server: str | None,
    limit: int,
) -> list[dict]:
    from storage.connection import get_mon_reader

    where: list[str] = []
    params: list = []
    if server:
        where.append("i.server_id = ?")
        params.append(server)
    if status:
        where.append("i.status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(limit, 100)))

    sql = f"""
        SELECT i.id, i.server_id, i.status, i.started_at, i.ended_at,
               i.confidence, i.root_cause_summary,
               i.query_count_total,
               a.provider, a.alert_type, a.severity, a.summary,
               a.external_id
        FROM investigations i
        JOIN inbound_alerts a ON i.inbound_alert_id = a.id
        {where_sql}
        ORDER BY i.started_at DESC
        LIMIT ?
    """
    with get_mon_reader() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def _get_investigation_impl(investigation_id: int) -> dict:
    from storage.connection import get_mon_reader
    with get_mon_reader() as conn:
        inv_row = conn.execute(
            """
            SELECT i.*, a.provider, a.alert_type, a.severity AS alert_severity,
                   a.summary, a.external_id, a.received_at
            FROM investigations i
            JOIN inbound_alerts a ON i.inbound_alert_id = a.id
            WHERE i.id = ?
            """,
            (investigation_id,),
        ).fetchone()
        if inv_row is None:
            return {"error": f"investigation {investigation_id} not found"}
        investigation = dict(inv_row)

        findings = [
            dict(r)
            for r in conn.execute(
                "SELECT id, phase, kind, severity, content, created_at "
                "FROM investigation_findings WHERE investigation_id = ? ORDER BY id",
                (investigation_id,),
            ).fetchall()
        ]
        for f in findings:
            try:
                f["content_parsed"] = json.loads(f["content"] or "{}")
            except (TypeError, ValueError):
                f["content_parsed"] = None

        samples = [
            dict(r)
            for r in conn.execute(
                "SELECT sample_type, COUNT(*) AS n, "
                "       MIN(sampled_at) AS first_at, MAX(sampled_at) AS last_at, "
                "       SUM(query_count) AS query_count "
                "FROM investigation_samples WHERE investigation_id = ? "
                "GROUP BY sample_type ORDER BY sample_type",
                (investigation_id,),
            ).fetchall()
        ]

    return {
        "investigation": investigation,
        "findings": findings,
        "samples": samples,
    }
