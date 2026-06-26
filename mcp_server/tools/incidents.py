"""
Incident-family tools. Read-only.

- seeql_list_incidents     — gap-clustered anomaly windows
- seeql_get_incident       — full detail: window + constituent anomaly_events
"""

import json
import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_list_incidents",
        description=(
            "List recent incident windows — gap-clustered groups of anomaly "
            "events detected by SeeQL's anomaly layer. Each row has id, "
            "start/end times, severity, event_count, and the list of metrics "
            "that fired. Filter by status (detected|analyzed|resolved) or "
            "server."
        ),
    )
    def list_incidents_tool(
        status: str | None = None,
        server: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        def _impl(status=None, server=None, limit=20):
            return _list_incidents_impl(status, server, limit)
        return wrap_tool(safety, "seeql_list_incidents", _impl)(
            status=status, server=server, limit=limit,
        )

    @mcp.tool(
        name="seeql_get_incident",
        description=(
            "Full detail for an incident window: the window itself, the "
            "individual anomaly_events that belong to it (metric_name, "
            "z_score, current vs. baseline, direction), and a linked "
            "analysis_id if any investigation has processed it."
        ),
    )
    def get_incident_tool(id: int) -> dict:
        def _impl(id: int):
            return _get_incident_impl(id)
        return wrap_tool(safety, "seeql_get_incident", _impl)(id=id)


def _list_incidents_impl(
    status: str | None, server: str | None, limit: int,
) -> list[dict]:
    from storage.connection import get_mon_reader
    where: list[str] = []
    params: list = []
    if server:
        where.append("server_id = ?")
        params.append(server)
    if status:
        where.append("status = ?")
        params.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(max(1, min(limit, 100)))
    sql = f"""
        SELECT id, server_id, start_time, end_time, severity,
               involved_metrics, event_count, status, analysis_id,
               CAST(ROUND((julianday(end_time) - julianday(start_time)) * 1440.0)
                    AS INTEGER) AS duration_minutes
        FROM incident_windows
        {where_sql}
        ORDER BY start_time DESC
        LIMIT ?
    """
    with get_mon_reader() as conn:
        rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    for r in rows:
        try:
            r["involved_metrics"] = json.loads(r["involved_metrics"])
        except (TypeError, ValueError):
            r["involved_metrics"] = []
    return rows


def _get_incident_impl(incident_id: int) -> dict:
    from storage.connection import get_mon_reader
    with get_mon_reader() as conn:
        row = conn.execute(
            "SELECT * FROM incident_windows WHERE id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return {"error": f"incident {incident_id} not found"}
        window = dict(row)
        try:
            window["involved_metrics"] = json.loads(window["involved_metrics"] or "[]")
        except (TypeError, ValueError):
            window["involved_metrics"] = []

        events = [
            dict(r)
            for r in conn.execute(
                "SELECT id, detected_at, metric_name, current_value, "
                "baseline_mean, baseline_stddev, z_score, pct_change, "
                "direction, severity "
                "FROM anomaly_events "
                "WHERE incident_id = ? ORDER BY detected_at",
                (incident_id,),
            ).fetchall()
        ]

    return {"window": window, "anomaly_events": events}
