"""
Prometheus metrics endpoint.

Exposes key MySQL and SeeQL metrics in Prometheus exposition format
at /metrics for scraping by Prometheus/Grafana.
"""

import time
import logging

from prometheus_client import (
    Gauge, Counter, Info,
    generate_latest, CONTENT_TYPE_LATEST, REGISTRY,
    CollectorRegistry,
)
from fastapi import APIRouter, Response

from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)

router = APIRouter(tags=["prometheus"])

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

# Server metrics
mysql_threads_running = Gauge("mysql_threads_running", "Current Threads_running value")
mysql_threads_connected = Gauge("mysql_threads_connected", "Current Threads_connected value")
mysql_qps = Gauge("mysql_queries_per_second", "Current queries per second")
mysql_slow_queries = Gauge("mysql_slow_queries_per_second", "Slow queries per second")

# Lock metrics
mysql_lock_waits_current = Gauge("mysql_lock_waits_current", "Current number of lock waits")
mysql_lock_wait_max_seconds = Gauge("mysql_lock_wait_max_seconds", "Longest current lock wait in seconds")

# Buffer pool
mysql_buffer_pool_hit_ratio = Gauge("mysql_buffer_pool_hit_ratio", "InnoDB buffer pool hit ratio")
mysql_buffer_pool_dirty_pages = Gauge("mysql_buffer_pool_dirty_pages", "InnoDB dirty pages count")
mysql_buffer_pool_free = Gauge("mysql_buffer_pool_free_buffers", "InnoDB free buffer count")

# GCP infrastructure
mysql_cpu_utilization = Gauge("mysql_cpu_utilization", "Cloud SQL CPU utilization (0-1)")
mysql_memory_utilization = Gauge("mysql_memory_utilization", "Cloud SQL memory utilization (0-1)")
mysql_disk_utilization = Gauge("mysql_disk_utilization", "Cloud SQL disk utilization (0-1)")
mysql_disk_read_ops = Gauge("mysql_disk_read_ops", "Cloud SQL disk read ops")
mysql_disk_write_ops = Gauge("mysql_disk_write_ops", "Cloud SQL disk write ops")
mysql_network_connections = Gauge("mysql_network_connections", "Cloud SQL network connections")

# Index health
mysql_unused_indexes = Gauge("mysql_unused_indexes_count", "Number of unused indexes detected")
mysql_redundant_indexes = Gauge("mysql_redundant_indexes_count", "Number of redundant indexes detected")

# InnoDB internals
mysql_innodb_rows_read_per_sec = Gauge("mysql_innodb_rows_read_per_sec", "InnoDB rows read/s from status")
mysql_innodb_row_lock_waits = Gauge("mysql_innodb_row_lock_waits_per_sec", "InnoDB row lock waits/s")

# Collection health
seeql_collection_last_ts = Gauge("seeql_collection_last_timestamp", "Unix timestamp of last collection", ["loop"])
seeql_alerts_fired = Counter("seeql_alerts_fired_total", "Total alerts fired", ["rule"])

# Cache to avoid hammering SQLite on every /metrics scrape
_last_update = 0.0
_cache_ttl = 10  # seconds


def update_metrics():
    """Read latest values from SQLite and update Prometheus gauges."""
    global _last_update

    now = time.time()
    if now - _last_update < _cache_ttl:
        return
    _last_update = now

    try:
        with get_mon_reader() as conn:
            _update_server_metrics(conn)
            _update_lock_metrics(conn)
            _update_buffer_pool(conn)
            _update_gcp_metrics(conn)
            _update_index_metrics(conn)
            _update_innodb_metrics(conn)
    except Exception as e:
        logger.warning(f"Prometheus metric update failed: {e}")


def _update_server_metrics(conn):
    rows = conn.execute("""
        SELECT variable_name, raw_value, per_second
        FROM global_status_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM global_status_snapshots)
          AND variable_name IN ('Threads_running', 'Threads_connected', 'Queries', 'Slow_queries')
    """).fetchall()

    for r in rows:
        name = r["variable_name"]
        if name == "Threads_running":
            mysql_threads_running.set(r["raw_value"] or 0)
        elif name == "Threads_connected":
            mysql_threads_connected.set(r["raw_value"] or 0)
        elif name == "Queries" and r["per_second"]:
            mysql_qps.set(r["per_second"])
        elif name == "Slow_queries" and r["per_second"]:
            mysql_slow_queries.set(r["per_second"])


def _update_lock_metrics(conn):
    row = conn.execute("""
        SELECT COUNT(*) as cnt, COALESCE(MAX(wait_seconds), 0) as max_wait
        FROM lock_wait_snapshots
        WHERE snapshot_time >= datetime('now', '-2 minutes')
    """).fetchone()

    if row:
        mysql_lock_waits_current.set(row["cnt"])
        mysql_lock_wait_max_seconds.set(row["max_wait"])


def _update_buffer_pool(conn):
    # hit_ratio: cumulative from global_status_snapshots. The column
    # buffer_pool_snapshots.hit_ratio is an unreliable instantaneous sample
    # (see api.query_helpers.latest_hit_ratio_pct docstring).
    from api.query_helpers import latest_hit_ratio_pct
    hit_pct = latest_hit_ratio_pct(conn=conn)
    if hit_pct is not None:
        # Gauge stored as a fraction in [0, 1] for Grafana-friendliness
        mysql_buffer_pool_hit_ratio.set(hit_pct / 100.0)

    row = conn.execute("""
        SELECT dirty_pages, free_buffers
        FROM buffer_pool_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM buffer_pool_snapshots)
        LIMIT 1
    """).fetchone()
    if row:
        mysql_buffer_pool_dirty_pages.set(row["dirty_pages"] or 0)
        mysql_buffer_pool_free.set(row["free_buffers"] or 0)


def _update_gcp_metrics(conn):
    rows = conn.execute("""
        SELECT metric_name, value
        FROM gcp_metric_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM gcp_metric_snapshots)
    """).fetchall()

    metric_map = {
        "cpu_utilization": mysql_cpu_utilization,
        "memory_utilization": mysql_memory_utilization,
        "disk_utilization": mysql_disk_utilization,
        "disk_read_ops": mysql_disk_read_ops,
        "disk_write_ops": mysql_disk_write_ops,
        "network_connections": mysql_network_connections,
    }

    for r in rows:
        gauge = metric_map.get(r["metric_name"])
        if gauge and r["value"] is not None:
            gauge.set(r["value"])


def _update_index_metrics(conn):
    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM unused_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM unused_index_snapshots)
    """).fetchone()
    if row:
        mysql_unused_indexes.set(row["cnt"])

    row = conn.execute("""
        SELECT COUNT(*) as cnt FROM redundant_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM redundant_index_snapshots)
    """).fetchone()
    if row:
        mysql_redundant_indexes.set(row["cnt"])


def _update_innodb_metrics(conn):
    rows = conn.execute("""
        SELECT variable_name, per_second
        FROM global_status_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM global_status_snapshots)
          AND variable_name IN ('Innodb_rows_read', 'Innodb_row_lock_waits')
          AND per_second IS NOT NULL
    """).fetchall()

    for r in rows:
        if r["variable_name"] == "Innodb_rows_read":
            mysql_innodb_rows_read_per_sec.set(r["per_second"])
        elif r["variable_name"] == "Innodb_row_lock_waits":
            mysql_innodb_row_lock_waits.set(r["per_second"])


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
