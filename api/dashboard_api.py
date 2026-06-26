"""JSON API endpoints for dashboard charts and data tables."""

import logging
from fastapi import APIRouter, Query as QueryParam
from api.query_helpers import parse_time_range, query_rows, query_single, resolve_server_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["dashboard-api"])


def _sf(server: str | None) -> str:
    """Build a server_id SQL filter clause."""
    return "AND server_id = ?" if server else ""


def _sp(server: str | None, *other_params) -> tuple:
    """Build params tuple, prepending server_id if present."""
    if server:
        return (server,) + tuple(other_params)
    return tuple(other_params)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

@router.get("/queries/top",
    summary="Top-N queries by metric",
    tags=["queries"])
def queries_top(
    range: str = QueryParam(default="24h", description="Preset: 1h/6h/24h/7d/30d"),
    from_ts: str = QueryParam(default=None, alias="from", description="Custom start (ISO8601)"),
    to_ts: str = QueryParam(default=None, alias="to", description="Custom end (ISO8601)"),
    sort: str = QueryParam(default="total_time_sec"),
    limit: int = QueryParam(default=20, le=100),
    server: str = QueryParam(default=None),
    schema: str = QueryParam(default=None, description="Filter by schema_name"),
    table: str = QueryParam(default=None, description="Filter by table name (LIKE on digest_text)"),
):
    """
    Top-N queries in the time range, grouped by digest, sorted by metric.

    The `from`/`to` params override `range` for custom windows. `schema` and
    `table` filter the results; `table` uses LIKE on `digest_text` and is
    best-effort (digest texts don't carry structured table info).
    """
    server = resolve_server_id(server)
    start, end = parse_time_range(range, from_ts, to_ts)
    allowed_sorts = {
        "total_time_sec", "avg_time_sec", "exec_count",
        "rows_examined", "full_scans", "no_index_used",
    }
    sort_col = sort if sort in allowed_sorts else "total_time_sec"

    # Build optional filters
    extra_where = ""
    extra_params: list = []
    if schema:
        extra_where += " AND schema_name = ?"
        extra_params.append(schema)
    if table:
        extra_where += " AND digest_text LIKE ?"
        extra_params.append(f"%{table}%")

    sql = f"""
        SELECT
            digest,
            digest_text,
            schema_name,
            SUM(exec_count) as exec_count,
            SUM(total_time_sec) as total_time_sec,
            AVG(avg_time_sec) as avg_time_sec,
            MAX(max_time_sec) as max_time_sec,
            SUM(rows_examined) as rows_examined,
            SUM(rows_sent) as rows_sent,
            SUM(full_scans) as full_scans,
            SUM(no_index_used) as no_index_used,
            SUM(tmp_tables) as tmp_tables,
            SUM(tmp_disk_tables) as tmp_disk_tables,
            MAX(last_seen) as last_seen
        FROM query_digest_snapshots
        WHERE snapshot_time BETWEEN ? AND ?
          {_sf(server)}
          {extra_where}
        GROUP BY digest
        ORDER BY {sort_col} DESC
        LIMIT ?
    """
    params = (start, end, *_sp(server), *extra_params, limit)
    return query_rows(sql, params)


@router.get("/queries/{digest}/trend")
def query_trend(
    digest: str,
    range: str = QueryParam(default="24h"),
    server: str = QueryParam(default=None),
):
    """Latency trend for a specific query digest."""
    server = resolve_server_id(server)
    start, end = parse_time_range(range)
    sql = f"""
        SELECT snapshot_time, avg_time_sec, exec_count, total_time_sec,
               rows_examined, rows_sent
        FROM query_digest_snapshots
        WHERE digest = ? AND snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        ORDER BY snapshot_time ASC
    """
    return query_rows(sql, (digest, start, end, *_sp(server)))


@router.get("/queries/{digest}/explain")
def query_explain(digest: str, server: str = QueryParam(default=None)):
    """Latest EXPLAIN capture for a query digest."""
    server = resolve_server_id(server)
    sql = f"""
        SELECT captured_at, digest_text, schema_name, explain_json,
               total_time_sec, avg_time_sec, exec_count
        FROM explain_captures
        WHERE digest = ?
          {_sf(server)}
        ORDER BY captured_at DESC
        LIMIT 1
    """
    return query_single(sql, (digest, *_sp(server)))


@router.get("/queries/regressions")
def query_regressions(
    threshold: float = QueryParam(default=3.0),
    server: str = QueryParam(default=None),
):
    """Queries with avg_time regression vs 7-day baseline."""
    server = resolve_server_id(server)
    sf = _sf(server)
    sql = f"""
        WITH recent AS (
            SELECT digest, digest_text, AVG(avg_time_sec) as recent_avg,
                   SUM(exec_count) as recent_execs
            FROM query_digest_snapshots
            WHERE snapshot_time >= datetime('now', '-1 hour')
              {sf}
            GROUP BY digest
        ),
        baseline AS (
            SELECT digest, AVG(avg_time_sec) as baseline_avg
            FROM query_digest_snapshots
            WHERE snapshot_time BETWEEN datetime('now', '-7 days') AND datetime('now', '-1 hour')
              {sf}
            GROUP BY digest
        )
        SELECT r.digest, r.digest_text, r.recent_avg, b.baseline_avg,
               r.recent_avg / NULLIF(b.baseline_avg, 0) as regression_factor,
               r.recent_execs
        FROM recent r
        JOIN baseline b ON r.digest = b.digest
        WHERE b.baseline_avg > 0
          AND r.recent_avg / b.baseline_avg >= ?
        ORDER BY regression_factor DESC
        LIMIT 20
    """
    # CTEs each need server_id param
    params = (*_sp(server), *_sp(server), threshold)
    return query_rows(sql, params)


# ---------------------------------------------------------------------------
# Metrics (for charts)
# ---------------------------------------------------------------------------

@router.get("/metrics/qps",
    summary="Queries per second time series",
    tags=["metrics"])
def metrics_qps(
    range: str = QueryParam(default="1h"),
    from_ts: str = QueryParam(default=None, alias="from"),
    to_ts: str = QueryParam(default=None, alias="to"),
    server: str = QueryParam(default=None),
):
    """Queries per second over time from global status deltas."""
    server = resolve_server_id(server)
    start, end = parse_time_range(range, from_ts, to_ts)
    sql = f"""
        SELECT snapshot_time, per_second as value
        FROM global_status_snapshots
        WHERE variable_name = 'Queries'
          AND snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        ORDER BY snapshot_time ASC
    """
    return query_rows(sql, (start, end, *_sp(server)))


@router.get("/metrics/threads",
    summary="Threads_running and Threads_connected time series",
    tags=["metrics"])
def metrics_threads(
    range: str = QueryParam(default="1h"),
    from_ts: str = QueryParam(default=None, alias="from"),
    to_ts: str = QueryParam(default=None, alias="to"),
    server: str = QueryParam(default=None),
):
    """
    Returns `{running: [{t, v}], connected: [{t, v}]}`.

    Threads_running is the load-bearing metric during lock cascades; a spike
    in running without a matching spike in connected is a strong incident
    signal. See Phase 2.11.3 in PLAN.md.
    """
    server = resolve_server_id(server)
    start, end = parse_time_range(range, from_ts, to_ts)
    sql = f"""
        SELECT snapshot_time, variable_name, raw_value as value
        FROM global_status_snapshots
        WHERE variable_name IN ('Threads_running', 'Threads_connected')
          AND snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        ORDER BY snapshot_time ASC
    """
    rows = query_rows(sql, (start, end, *_sp(server)))
    running = [{"t": r["snapshot_time"], "v": r["value"]} for r in rows if r["variable_name"] == "Threads_running"]
    connected = [{"t": r["snapshot_time"], "v": r["value"]} for r in rows if r["variable_name"] == "Threads_connected"]
    return {"running": running, "connected": connected}


@router.get("/metrics/buffer-pool")
def metrics_buffer_pool(
    range: str = QueryParam(default="1h"),
    server: str = QueryParam(default=None),
):
    """
    Buffer pool hit ratio over time.

    Computed from SHOW GLOBAL STATUS cumulative counters
    (`Innodb_buffer_pool_reads` / `Innodb_buffer_pool_read_requests`) because
    `information_schema.INNODB_BUFFER_POOL_STATS.HIT_RATE` is an instantaneous
    sample over the last ~1-second interval and returns 0 when no page gets
    occurred in that window — making it unreliable on any real workload. See
    PLAN.md Phase 0.5 for details.
    """
    server = resolve_server_id(server)
    start, end = parse_time_range(range)
    sql = f"""
        WITH bucketed AS (
            SELECT snapshot_time,
                   MAX(CASE WHEN variable_name='Innodb_buffer_pool_reads'
                            THEN raw_value END) AS reads,
                   MAX(CASE WHEN variable_name='Innodb_buffer_pool_read_requests'
                            THEN raw_value END) AS requests
            FROM global_status_snapshots
            WHERE variable_name IN ('Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests')
              AND snapshot_time BETWEEN ? AND ?
              {_sf(server)}
            GROUP BY snapshot_time
        )
        SELECT snapshot_time,
               CASE WHEN requests > 0
                    THEN 100.0 * (1.0 - (CAST(reads AS REAL) / requests))
                    ELSE NULL END AS hit_ratio
        FROM bucketed
        WHERE reads IS NOT NULL AND requests IS NOT NULL
        ORDER BY snapshot_time ASC
    """
    return query_rows(sql, (start, end, *_sp(server)))


@router.get("/metrics/buffer-pool-pages")
def metrics_buffer_pool_pages(
    range: str = QueryParam(default="1h"),
    server: str = QueryParam(default=None),
):
    """
    Buffer pool page counts (dirty/free/database) over time.

    Split from `/metrics/buffer-pool` so the hit-ratio endpoint can read from
    global_status_snapshots while this one keeps reading the per-pool page
    counts from buffer_pool_snapshots.
    """
    server = resolve_server_id(server)
    start, end = parse_time_range(range)
    sql = f"""
        SELECT snapshot_time, dirty_pages, free_buffers, database_pages
        FROM buffer_pool_snapshots
        WHERE snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        ORDER BY snapshot_time ASC
    """
    return query_rows(sql, (start, end, *_sp(server)))


@router.get("/metrics/innodb")
def metrics_innodb(
    range: str = QueryParam(default="1h"),
    metrics: str = QueryParam(default="rows_read,rows_inserted"),
    server: str = QueryParam(default=None),
):
    """InnoDB metric counters over time."""
    server = resolve_server_id(server)
    start, end = parse_time_range(range)
    metric_list = [m.strip() for m in metrics.split(",")]
    placeholders = ",".join("?" * len(metric_list))
    sql = f"""
        SELECT snapshot_time, metric_name, count_value
        FROM innodb_metric_snapshots
        WHERE metric_name IN ({placeholders})
          AND snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        ORDER BY snapshot_time ASC
    """
    return query_rows(sql, (*metric_list, start, end, *_sp(server)))


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

@router.get("/locks/history")
def locks_history(
    range: str = QueryParam(default="24h"),
    bucket: str = QueryParam(default="5m"),
    server: str = QueryParam(default=None),
):
    """Lock wait counts bucketed over time."""
    server = resolve_server_id(server)
    start, end = parse_time_range(range)
    bucket_map = {"1m": "%Y-%m-%d %H:%M", "5m": "%Y-%m-%d %H:%M", "1h": "%Y-%m-%d %H"}
    fmt = bucket_map.get(bucket, "%Y-%m-%d %H:%M")
    sql = f"""
        SELECT strftime('{fmt}', snapshot_time) as bucket,
               COUNT(*) as lock_count,
               MAX(wait_seconds) as max_wait,
               AVG(wait_seconds) as avg_wait
        FROM lock_wait_snapshots
        WHERE snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        GROUP BY strftime('{fmt}', snapshot_time)
        ORDER BY bucket ASC
    """
    return query_rows(sql, (start, end, *_sp(server)))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@router.get("/schemas",
    summary="Distinct schemas and tables seen in the monitored DB",
    tags=["schema"])
def list_schemas(server: str = QueryParam(default=None)):
    """
    Returns `{schemas: [names], tables: [{schema, name}]}` for the
    schema/table filter dropdowns on the dashboard. Cached per-request;
    deduped across `query_digest_snapshots`, `table_io_snapshots`, and
    `schema_snapshots`.
    """
    server = resolve_server_id(server)

    schemas_sql = f"""
        SELECT name FROM (
            SELECT DISTINCT schema_name AS name
            FROM query_digest_snapshots
            WHERE schema_name IS NOT NULL
              {_sf(server)}
            UNION
            SELECT DISTINCT object_schema AS name
            FROM table_io_snapshots
            WHERE object_schema IS NOT NULL
              {_sf(server)}
            UNION
            SELECT DISTINCT table_schema AS name
            FROM schema_snapshots
            WHERE table_schema IS NOT NULL
              {_sf(server)}
        )
        ORDER BY name
    """
    schemas_params = (*_sp(server), *_sp(server), *_sp(server))
    schemas = [r["name"] for r in query_rows(schemas_sql, schemas_params)]

    tables_sql = f"""
        SELECT schema, name FROM (
            SELECT object_schema AS schema, table_name AS name
            FROM table_io_snapshots
            WHERE object_schema IS NOT NULL
              {_sf(server)}
            UNION
            SELECT table_schema AS schema, table_name AS name
            FROM schema_snapshots
            WHERE table_schema IS NOT NULL
              {_sf(server)}
        )
        GROUP BY schema, name
        ORDER BY schema, name
    """
    tables_params = (*_sp(server), *_sp(server))
    tables = query_rows(tables_sql, tables_params)

    return {"schemas": schemas, "tables": tables}


@router.get("/schema/table-sizes")
def schema_table_sizes(
    sort: str = QueryParam(default="total_mb"),
    server: str = QueryParam(default=None),
):
    """Latest table sizes from schema snapshots."""
    server = resolve_server_id(server)
    allowed = {"total_mb", "data_mb", "index_mb", "table_rows"}
    sort_col = sort if sort in allowed else "total_mb"
    order = "data_mb + index_mb" if sort_col == "total_mb" else sort_col
    sql = f"""
        SELECT table_schema, table_name, table_rows, data_mb, index_mb,
               data_mb + index_mb as total_mb
        FROM schema_snapshots
        WHERE snapshot_time = (
            SELECT MAX(snapshot_time) FROM schema_snapshots WHERE server_id = ?
        )
          AND server_id = ?
        ORDER BY {order} DESC
    """
    return query_rows(sql, (server, server))


# ---------------------------------------------------------------------------
# Server / Wait Events
# ---------------------------------------------------------------------------

@router.get("/server/wait-events/top")
def wait_events_top(
    limit: int = QueryParam(default=10),
    server: str = QueryParam(default=None),
):
    """Top wait events from latest snapshot."""
    server = resolve_server_id(server)
    sql = f"""
        SELECT event_name, count_star, total_wait_sec, avg_wait_sec
        FROM wait_event_snapshots
        WHERE snapshot_time = (
            SELECT MAX(snapshot_time) FROM wait_event_snapshots WHERE server_id = ?
        )
          AND server_id = ?
          AND total_wait_sec > 0
        ORDER BY total_wait_sec DESC
        LIMIT ?
    """
    return query_rows(sql, (server, server, limit))


@router.get("/server/gcp-metrics")
def gcp_metrics(
    range: str = QueryParam(default="1h"),
    metrics: str = QueryParam(default="cpu,memory,disk"),
    server: str = QueryParam(default=None),
):
    """GCP Cloud Monitoring metrics over time."""
    server = resolve_server_id(server)
    start, end = parse_time_range(range)
    metric_list = [m.strip() for m in metrics.split(",")]
    placeholders = ",".join("?" * len(metric_list))
    sql = f"""
        SELECT snapshot_time, metric_name, value, unit
        FROM gcp_metric_snapshots
        WHERE metric_name IN ({placeholders})
          AND snapshot_time BETWEEN ? AND ?
          {_sf(server)}
        ORDER BY snapshot_time ASC
    """
    return query_rows(sql, (*metric_list, start, end, *_sp(server)))


# ---------------------------------------------------------------------------
# Incidents (Phase 1.10)
# ---------------------------------------------------------------------------

@router.get("/incidents/recent")
def incidents_recent(
    limit: int = QueryParam(default=10, le=50),
    status: str = QueryParam(default=None),
    server: str = QueryParam(default=None),
):
    """
    Recent incident windows for the overview-page widget.

    Returns an array of dicts with `involved_metrics` decoded from JSON
    (so the frontend doesn't re-parse) and `duration_minutes` computed from
    start/end timestamps.
    """
    import json as _json
    server = resolve_server_id(server)
    where = ["server_id = ?"]
    params: list = [server]
    if status:
        where.append("status = ?")
        params.append(status)
    sql = f"""
        SELECT id, start_time, end_time, severity, involved_metrics,
               event_count, status,
               CAST(ROUND((julianday(end_time) - julianday(start_time)) * 1440.0) AS INTEGER)
                 AS duration_minutes
        FROM incident_windows
        WHERE {' AND '.join(where)}
        ORDER BY start_time DESC
        LIMIT ?
    """
    params.append(limit)
    rows = query_rows(sql, tuple(params))
    for row in rows:
        try:
            row["involved_metrics"] = _json.loads(row["involved_metrics"])
        except (TypeError, ValueError):
            row["involved_metrics"] = []
    return rows


# ---------------------------------------------------------------------------
# Investigations (webhook-triggered root-cause; CP7)
# ---------------------------------------------------------------------------

@router.get("/investigations/recent")
def investigations_recent(
    limit: int = QueryParam(default=10, le=50),
    status: str = QueryParam(default=None),
    server: str = QueryParam(default=None),
):
    """
    Recent investigations for the dashboard widget.

    Joins inbound_alerts for the alert metadata. Returns an array of
    dicts with ISO timestamps, alert type / severity, current status,
    and a duration_seconds if terminal.
    """
    server = resolve_server_id(server)
    where = ["i.server_id = ?"]
    params: list = [server]
    if status:
        where.append("i.status = ?")
        params.append(status)
    sql = f"""
        SELECT i.id, i.server_id, i.status, i.started_at, i.ended_at,
               i.confidence, i.root_cause_summary,
               a.provider, a.alert_type, a.severity, a.summary,
               CAST(ROUND((julianday(COALESCE(i.ended_at, strftime('%Y-%m-%dT%H:%M:%S','now')))
                           - julianday(i.started_at)) * 86400.0) AS INTEGER)
                 AS duration_seconds
        FROM investigations i
        JOIN inbound_alerts a ON i.inbound_alert_id = a.id
        WHERE {' AND '.join(where)}
        ORDER BY i.started_at DESC
        LIMIT ?
    """
    params.append(limit)
    return query_rows(sql, tuple(params))


@router.get("/investigations/{investigation_id}")
def investigation_detail(investigation_id: int):
    """
    Full detail: investigation row + findings + sample counts grouped by
    sample_type. Consumed by the dashboard detail view.
    """
    import json as _json
    inv_rows = query_rows(
        """
        SELECT i.*, a.provider, a.alert_type, a.severity AS alert_severity,
               a.summary, a.external_id, a.received_at
        FROM investigations i
        JOIN inbound_alerts a ON i.inbound_alert_id = a.id
        WHERE i.id = ?
        """,
        (investigation_id,),
    )
    if not inv_rows:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="investigation not found")
    inv = inv_rows[0]

    findings = query_rows(
        "SELECT id, phase, kind, severity, content, created_at "
        "FROM investigation_findings WHERE investigation_id = ? ORDER BY id",
        (investigation_id,),
    )
    for f in findings:
        try:
            f["content_parsed"] = _json.loads(f["content"] or "{}")
        except (TypeError, ValueError):
            f["content_parsed"] = None

    samples = query_rows(
        "SELECT sample_type, COUNT(*) AS n, "
        "       MIN(sampled_at) AS first_at, MAX(sampled_at) AS last_at, "
        "       SUM(query_count) AS query_count "
        "FROM investigation_samples WHERE investigation_id = ? "
        "GROUP BY sample_type ORDER BY sample_type",
        (investigation_id,),
    )
    return {"investigation": inv, "findings": findings, "samples": samples}


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------

@router.get("/servers")
def list_servers():
    """List all monitored servers with health status."""
    from config.server_registry import get_server_registry
    from storage.connection import check_prod_connection
    registry = get_server_registry()
    return [
        {
            "server_id": s.server_id,
            "display_name": s.display_name,
            "environment": s.environment,
            "role": s.role,
            "cluster_id": s.cluster_id,
            "tags": s.tags,
            "is_active": s.is_active,
            "healthy": check_prod_connection(s.server_id),
        }
        for s in registry.get_all_servers()
    ]
