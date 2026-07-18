"""
Prometheus metrics endpoint.

Exposes key MySQL and SeeQL metrics in Prometheus exposition format
at /metrics for scraping by Prometheus/Grafana.
"""

import time
import logging

from prometheus_client import (
    Gauge, Counter,
    generate_latest, CONTENT_TYPE_LATEST, REGISTRY,
)
from fastapi import APIRouter, Response

from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prometheus"])

# ---------------------------------------------------------------------------
# Metric definitions
#
# Every mysql_* gauge carries a `server` label (P1c-9): update_metrics()
# loops over all configured servers and sets each gauge per server_id, so a
# multi-server install gets independent time series instead of one gauge
# flapping between servers.
# ---------------------------------------------------------------------------

# Server metrics
mysql_threads_running = Gauge("mysql_threads_running", "Current Threads_running value", ["server"])
mysql_threads_connected = Gauge("mysql_threads_connected", "Current Threads_connected value", ["server"])
mysql_qps = Gauge("mysql_queries_per_second", "Current queries per second", ["server"])
mysql_slow_queries = Gauge("mysql_slow_queries_per_second", "Slow queries per second", ["server"])

# Lock metrics
mysql_lock_waits_current = Gauge("mysql_lock_waits_current", "Current number of lock waits", ["server"])
mysql_lock_wait_max_seconds = Gauge(
    "mysql_lock_wait_max_seconds", "Longest current lock wait in seconds", ["server"]
)

# Buffer pool
mysql_buffer_pool_hit_ratio = Gauge("mysql_buffer_pool_hit_ratio", "InnoDB buffer pool hit ratio", ["server"])
mysql_buffer_pool_dirty_pages = Gauge("mysql_buffer_pool_dirty_pages", "InnoDB dirty pages count", ["server"])
mysql_buffer_pool_free = Gauge("mysql_buffer_pool_free_buffers", "InnoDB free buffer count", ["server"])

# GCP infrastructure
mysql_cpu_utilization = Gauge("mysql_cpu_utilization", "Cloud SQL CPU utilization (0-1)", ["server"])
mysql_memory_utilization = Gauge("mysql_memory_utilization", "Cloud SQL memory utilization (0-1)", ["server"])
mysql_disk_utilization = Gauge("mysql_disk_utilization", "Cloud SQL disk utilization (0-1)", ["server"])
mysql_disk_read_ops = Gauge("mysql_disk_read_ops", "Cloud SQL disk read ops", ["server"])
mysql_disk_write_ops = Gauge("mysql_disk_write_ops", "Cloud SQL disk write ops", ["server"])
mysql_network_connections = Gauge("mysql_network_connections", "Cloud SQL network connections", ["server"])

# Index health
mysql_unused_indexes = Gauge("mysql_unused_indexes_count", "Number of unused indexes detected", ["server"])
mysql_redundant_indexes = Gauge(
    "mysql_redundant_indexes_count", "Number of redundant indexes detected", ["server"]
)

# InnoDB internals
mysql_innodb_rows_read_per_sec = Gauge(
    "mysql_innodb_rows_read_per_sec", "InnoDB rows read/s from status", ["server"]
)
mysql_innodb_row_lock_waits = Gauge(
    "mysql_innodb_row_lock_waits_per_sec", "InnoDB row lock waits/s", ["server"]
)

# Collection health
seeql_collection_last_ts = Gauge("seeql_collection_last_timestamp", "Unix timestamp of last collection", ["loop"])
seeql_alerts_fired = Counter("seeql_alerts_fired_total", "Total alerts fired", ["rule"])

# Cache to avoid hammering SQLite on every /metrics scrape
_last_update = 0.0
_cache_ttl = 10  # seconds

# P1c-4: a server's row is only trusted as "live" if its most recent
# snapshot_time is within this many minutes of now. Older data means the
# collector feeding that table has stopped for that server — the gauge is
# left unset (absent from the scrape) rather than serving a frozen
# last-known value forever.
FRESHNESS_WINDOW_MINUTES = 10


def _is_fresh(conn, table: str, server_id: str) -> bool:
    """
    True if `table` has at least one row for `server_id` whose snapshot_time
    is within FRESHNESS_WINDOW_MINUTES of now.

    Equivalent to "is the latest row for this server fresh" (MAX(snapshot_time)
    is monotonically >= any single row, so if any row is in-window the latest
    one necessarily is too) — phrased as an EXISTS check so it can use the
    existing (server_id, snapshot_time) index instead of a correlated
    MAX() subquery.

    `table` is always one of a fixed set of literal names passed by call
    sites in this module — never user/request-derived — so the f-string
    interpolation below is not a SQL-injection vector.

    NOTE: snapshot_time is stored as a naive-UTC 'T'-separated string (repo
    convention), while datetime('now', ...) is space-separated. The column
    is always wrapped in REPLACE(...,'T',' ') before comparison — a raw
    compare would silently match everything, since 'T' > ' ' lexicographically
    (see tests/test_timestamp_boundaries.py).
    """
    row = conn.execute(
        f"""
        SELECT 1 FROM {table}
        WHERE server_id = ?
          AND datetime(REPLACE(snapshot_time, 'T', ' ')) >= datetime('now', ?)
        LIMIT 1
        """,
        (server_id, f"-{FRESHNESS_WINDOW_MINUTES} minutes"),
    ).fetchone()
    return row is not None


def _heartbeat_fresh(conn, server_id: str) -> bool:
    """
    True if the collector heartbeat (global_status_snapshots, written every
    medium-loop cycle regardless of whether anything interesting happened)
    has a recent row for server_id.

    Sparse/event tables — lock_wait_snapshots, unused_index_snapshots,
    redundant_index_snapshots — only gain a row when their collector
    actually finds something to report (a lock wait, an unused/redundant
    index). "No row in the freshness window" is therefore consistent with
    *both* "the collector died" *and* "the collector is healthy and
    correctly found nothing to report" — gating those gauges on their own
    table's freshness can't tell the two apart, so a perfectly healthy,
    quiet server wrongly goes absent instead of reporting a true 0 (M1).

    Gate those gauges on this heartbeat instead: heartbeat fresh + no
    sparse rows => report a true 0 (healthy, quiet). Heartbeat stale => the
    collector itself is dead => absent, same as every other gauge.
    """
    return _is_fresh(conn, "global_status_snapshots", server_id)


def _clear(*gauges, server_id: str) -> None:
    """
    Drop `server_id`'s child from each gauge so a stale reading disappears
    from the next scrape instead of continuing to report its last known
    value forever — the crux of P1c-4.

    Gauge.set() has no complementary "unset": once `.labels(server_id).set(v)`
    has been called, that time series stays in the registry (and keeps
    reporting `v`) until something explicitly calls `.remove(server_id)`, even
    if the code simply stops calling `.set()` for it. Skipping the `.set()`
    call alone is therefore NOT enough to make a gauge that was once live —
    then went stale — disappear; it only prevents a gauge that was NEVER
    fresh in this process's lifetime from ever appearing.

    `.remove()` on a label combination that was never set is a documented
    no-op in prometheus_client (verified against the installed version), so
    this is safe to call unconditionally on every stale check; the try/except
    is defense-in-depth against future client versions raising instead.
    """
    for gauge in gauges:
        try:
            gauge.remove(server_id)
        except KeyError:
            pass


def update_metrics():
    """Read latest values from SQLite and update Prometheus gauges.

    Loops over every configured server (P1c-9) so gauges are labelled and
    updated per server_id instead of one shared, unlabelled global reading.
    Each server's update is independently isolated: one server's bad data
    or a missing table doesn't stop the others from refreshing.
    """
    global _last_update

    now = time.time()
    if now - _last_update < _cache_ttl:
        return
    _last_update = now

    try:
        from config.server_registry import get_server_registry
        servers = get_server_registry().get_active_servers()
    except Exception as e:
        logger.warning(f"Prometheus: failed to load server registry: {e}")
        servers = []

    if not servers:
        class _DefaultServer:
            server_id = "default"
        servers = [_DefaultServer()]

    try:
        with get_mon_reader() as conn:
            for server in servers:
                sid = server.server_id
                try:
                    _update_server_metrics(conn, sid)
                    _update_lock_metrics(conn, sid)
                    _update_buffer_pool(conn, sid)
                    _update_gcp_metrics(conn, sid)
                    _update_index_metrics(conn, sid)
                    _update_innodb_metrics(conn, sid)
                except Exception as e:
                    logger.warning(f"Prometheus metric update failed for server {sid}: {e}")
    except Exception as e:
        logger.warning(f"Prometheus metric update failed to open reader: {e}")

    # P1c-4: always set, independent of whether any mysql_* gauge above was
    # fresh enough to update. Proves the /metrics scrape path itself is
    # alive even in a long-running process where every per-server gauge
    # might be legitimately stale/absent.
    seeql_collection_last_ts.labels(loop="metrics_cache").set(time.time())


def _update_server_metrics(conn, server_id):
    if not _is_fresh(conn, "global_status_snapshots", server_id):
        _clear(mysql_threads_running, mysql_threads_connected, mysql_qps, mysql_slow_queries,
               server_id=server_id)
        return

    rows = conn.execute("""
        SELECT variable_name, raw_value, per_second
        FROM global_status_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM global_status_snapshots WHERE server_id = ?
          )
          AND variable_name IN ('Threads_running', 'Threads_connected', 'Queries', 'Slow_queries')
    """, (server_id, server_id)).fetchall()

    for r in rows:
        name = r["variable_name"]
        if name == "Threads_running":
            mysql_threads_running.labels(server=server_id).set(r["raw_value"] or 0)
        elif name == "Threads_connected":
            mysql_threads_connected.labels(server=server_id).set(r["raw_value"] or 0)
        elif name == "Queries" and r["per_second"]:
            mysql_qps.labels(server=server_id).set(r["per_second"])
        elif name == "Slow_queries" and r["per_second"]:
            mysql_slow_queries.labels(server=server_id).set(r["per_second"])


def _update_lock_metrics(conn, server_id):
    # lock_wait_snapshots is sparse (LockWaitCollector only inserts rows
    # when contention actually exists), so gate on the collector heartbeat
    # (global_status_snapshots) instead of this table's own freshness —
    # see _heartbeat_fresh's docstring and task-2.7-report.md /
    # task-2.9-fixwave-report.md for the "quiet vs dead" ambiguity this
    # resolves (M1). Heartbeat alive + no rows in the window below just
    # means "0 lock waits right now", not "collector dead".
    if not _heartbeat_fresh(conn, server_id):
        _clear(mysql_lock_waits_current, mysql_lock_wait_max_seconds, server_id=server_id)
        return

    row = conn.execute("""
        SELECT COUNT(*) as cnt, COALESCE(MAX(wait_seconds), 0) as max_wait
        FROM lock_wait_snapshots
        WHERE server_id = ?
          AND datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now', '-2 minutes')
    """, (server_id,)).fetchone()

    if row:
        mysql_lock_waits_current.labels(server=server_id).set(row["cnt"])
        mysql_lock_wait_max_seconds.labels(server=server_id).set(row["max_wait"])


def _update_buffer_pool(conn, server_id):
    # hit_ratio: cumulative from global_status_snapshots. The column
    # buffer_pool_snapshots.hit_ratio is an unreliable instantaneous sample
    # (see api.query_helpers.latest_hit_ratio_pct docstring).
    hit_pct = None
    if _is_fresh(conn, "global_status_snapshots", server_id):
        from api.query_helpers import latest_hit_ratio_pct
        hit_pct = latest_hit_ratio_pct(server_id=server_id, conn=conn)
    if hit_pct is not None:
        # Gauge stored as a fraction in [0, 1] for Grafana-friendliness
        mysql_buffer_pool_hit_ratio.labels(server=server_id).set(hit_pct / 100.0)
    else:
        _clear(mysql_buffer_pool_hit_ratio, server_id=server_id)

    if not _is_fresh(conn, "buffer_pool_snapshots", server_id):
        _clear(mysql_buffer_pool_dirty_pages, mysql_buffer_pool_free, server_id=server_id)
        return

    row = conn.execute("""
        SELECT dirty_pages, free_buffers
        FROM buffer_pool_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM buffer_pool_snapshots WHERE server_id = ?
          )
        LIMIT 1
    """, (server_id, server_id)).fetchone()
    if row:
        mysql_buffer_pool_dirty_pages.labels(server=server_id).set(row["dirty_pages"] or 0)
        mysql_buffer_pool_free.labels(server=server_id).set(row["free_buffers"] or 0)
    else:
        _clear(mysql_buffer_pool_dirty_pages, mysql_buffer_pool_free, server_id=server_id)


def _update_gcp_metrics(conn, server_id):
    metric_map = {
        "cpu_utilization": mysql_cpu_utilization,
        "memory_utilization": mysql_memory_utilization,
        "disk_utilization": mysql_disk_utilization,
        "disk_read_ops": mysql_disk_read_ops,
        "disk_write_ops": mysql_disk_write_ops,
        "network_connections": mysql_network_connections,
    }

    if not _is_fresh(conn, "gcp_metric_snapshots", server_id):
        _clear(*metric_map.values(), server_id=server_id)
        return

    rows = conn.execute("""
        SELECT metric_name, value
        FROM gcp_metric_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM gcp_metric_snapshots WHERE server_id = ?
          )
    """, (server_id, server_id)).fetchall()

    seen = set()
    for r in rows:
        gauge = metric_map.get(r["metric_name"])
        if gauge and r["value"] is not None:
            gauge.labels(server=server_id).set(r["value"])
            seen.add(r["metric_name"])

    # Clear any GCP gauge this fresh scrape didn't report (e.g. Cloud
    # Monitoring stopped returning one particular metric type for this
    # server) so it doesn't keep serving an older reading forever.
    for name, gauge in metric_map.items():
        if name not in seen:
            _clear(gauge, server_id=server_id)


def _update_index_metrics(conn, server_id):
    # unused_index_snapshots / redundant_index_snapshots are sparse — the
    # slow-loop collectors only write a row when they actually flag one —
    # so gate both on the collector heartbeat (global_status_snapshots)
    # instead of each table's own freshness. Same "quiet vs dead"
    # ambiguity _update_lock_metrics resolves above (M1): heartbeat alive +
    # zero matching rows means "0 unused/redundant indexes right now", not
    # "collector dead".
    if not _heartbeat_fresh(conn, server_id):
        _clear(mysql_unused_indexes, mysql_redundant_indexes, server_id=server_id)
        return

    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM unused_index_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM unused_index_snapshots WHERE server_id = ?
          )
    """, (server_id, server_id)).fetchone()
    mysql_unused_indexes.labels(server=server_id).set(row["cnt"] if row else 0)

    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM redundant_index_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM redundant_index_snapshots WHERE server_id = ?
          )
    """, (server_id, server_id)).fetchone()
    mysql_redundant_indexes.labels(server=server_id).set(row["cnt"] if row else 0)


def _update_innodb_metrics(conn, server_id):
    if not _is_fresh(conn, "global_status_snapshots", server_id):
        _clear(mysql_innodb_rows_read_per_sec, mysql_innodb_row_lock_waits, server_id=server_id)
        return

    rows = conn.execute("""
        SELECT variable_name, per_second
        FROM global_status_snapshots
        WHERE server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM global_status_snapshots WHERE server_id = ?
          )
          AND variable_name IN ('Innodb_rows_read', 'Innodb_row_lock_waits')
          AND per_second IS NOT NULL
    """, (server_id, server_id)).fetchall()

    for r in rows:
        if r["variable_name"] == "Innodb_rows_read":
            mysql_innodb_rows_read_per_sec.labels(server=server_id).set(r["per_second"])
        elif r["variable_name"] == "Innodb_row_lock_waits":
            mysql_innodb_row_lock_waits.labels(server=server_id).set(r["per_second"])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get("/metrics")
def metrics_endpoint():
    """Prometheus metrics scrape endpoint."""
    update_metrics()
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
