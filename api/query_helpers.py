"""Shared query helpers for dashboard routes and API endpoints."""

import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from config import get_mon_db_config

logger = logging.getLogger(__name__)

# Time range presets
RANGE_MAP = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Module-level shared read connection (separate from the writer)
_reader_conn: sqlite3.Connection | None = None


def _get_reader() -> sqlite3.Connection:
    """Get or create a shared read-only SQLite connection for dashboard queries."""
    global _reader_conn
    if _reader_conn is not None:
        try:
            _reader_conn.execute("SELECT 1")
            return _reader_conn
        except Exception:
            _reader_conn = None

    config = get_mon_db_config()
    db_path = Path(config["path"])

    conn = sqlite3.connect(
        str(db_path),
        check_same_thread=False,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA cache_size = -16000")  # 16MB read cache
    _reader_conn = conn
    return _reader_conn


def parse_time_range(
    range_str: str | None = None,
    from_ts: str | None = None,
    to_ts: str | None = None,
) -> tuple[str, str]:
    """Parse a time range spec into (start_iso, end_iso).

    Two modes:
    1. Preset: `range_str` in {"1h","6h","24h","7d","30d"} → last N from now.
    2. Custom: `from_ts` + `to_ts` as ISO8601 → exact window.

    If both are supplied, custom wins. If neither, defaults to 24h.
    """
    if from_ts and to_ts:
        return from_ts, to_ts
    delta = RANGE_MAP.get(range_str)
    if delta is None:
        delta = RANGE_MAP["24h"]
    now = datetime.now(timezone.utc)
    start = now - delta
    return start.isoformat(), now.isoformat()


def query_rows(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read query and return list of dicts."""
    conn = _get_reader()
    cursor = conn.execute(sql, params)
    rows = cursor.fetchall()
    return [dict(row) for row in rows]


def query_single(sql: str, params: tuple = ()) -> dict | None:
    """Execute a read query and return a single dict or None."""
    conn = _get_reader()
    cursor = conn.execute(sql, params)
    row = cursor.fetchone()
    return dict(row) if row else None


def resolve_server_id(server: str | None) -> str:
    """Resolve a server_id from the query param, defaulting to the primary server."""
    if server:
        return server
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()


def inject_server_filter(sql: str, params: tuple, server_id: str | None,
                         table_alias: str = "") -> tuple[str, tuple]:
    """Inject 'AND server_id = ?' into a SQL query if server_id is provided.

    Looks for WHERE clause and appends the filter. If no WHERE exists,
    the caller must handle it. Works for most dashboard queries.
    """
    if not server_id:
        return sql, params

    col = f"{table_alias}.server_id" if table_alias else "server_id"

    # Try to inject after WHERE
    if "WHERE" in sql.upper():
        # Insert after the first WHERE keyword
        idx = sql.upper().index("WHERE") + 5
        sql = sql[:idx] + f" {col} = ? AND" + sql[idx:]
        params = (server_id,) + params
    else:
        # No WHERE — caller needs to handle this case
        pass

    return sql, params


def latest_hit_ratio_pct(server_id: str | None = None, conn=None) -> float | None:
    """
    Compute the cumulative InnoDB buffer pool hit ratio as a percentage
    (0.0–100.0) from the latest global_status_snapshots row.

    ``buffer_pool_snapshots.hit_ratio`` (from
    ``information_schema.INNODB_BUFFER_POOL_STATS.HIT_RATE``) is an
    instantaneous sample over the last ~1 second and returns 0 when no page
    gets occur in that window — unreliable on any real workload. This helper
    uses ``SHOW GLOBAL STATUS`` counters instead, which are cumulative since
    server start:

        hit_ratio = 1 - (Innodb_buffer_pool_reads / Innodb_buffer_pool_read_requests)

    Returns a percentage in [0.0, 100.0], or ``None`` if we don't have both
    counters (first run, or counters rolled over).
    """
    sql = """
        SELECT
            MAX(CASE WHEN variable_name='Innodb_buffer_pool_reads'
                     THEN raw_value END) AS reads,
            MAX(CASE WHEN variable_name='Innodb_buffer_pool_read_requests'
                     THEN raw_value END) AS requests
        FROM global_status_snapshots
        WHERE variable_name IN ('Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests')
          AND server_id = ?
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM global_status_snapshots
              WHERE variable_name = 'Innodb_buffer_pool_read_requests'
                AND server_id = ?
          )
    """
    sid = server_id
    if sid is None:
        from config.server_registry import get_server_registry
        sid = get_server_registry().get_default_server_id()

    if conn is None:
        conn = _get_reader()
    row = conn.execute(sql, (sid, sid)).fetchone()
    if not row or row["reads"] is None or row["requests"] is None:
        return None
    if row["requests"] <= 0:
        return None
    return 100.0 * (1.0 - (float(row["reads"]) / float(row["requests"])))


def get_all_servers_for_ui() -> dict:
    """Get server list grouped by environment for the UI dropdown."""
    from config.server_registry import get_server_registry
    registry = get_server_registry()
    return {
        "servers": [
            {
                "server_id": s.server_id,
                "display_name": s.display_name,
                "environment": s.environment,
                "role": s.role,
                "cluster_id": s.cluster_id,
                "is_active": s.is_active,
            }
            for s in registry.get_all_servers()
        ],
        "grouped": {
            env: [
                {"server_id": s.server_id, "display_name": s.display_name, "role": s.role}
                for s in servers
            ]
            for env, servers in registry.get_servers_grouped_by_env().items()
        },
        "default": registry.get_default_server_id(),
    }
