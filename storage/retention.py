"""
Data retention cleanup for the monitoring SQLite database.

Runs on schedule (daily) to delete old data and keep the database
from growing unbounded. Default retention: 90 days.

Each table has its own timestamp column name, so we handle them
individually.
"""

import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import get_config
from storage.connection import get_mon_connection

logger = logging.getLogger(__name__)

# table_name -> timestamp_column
RETENTION_TABLES = {
    "query_digest_snapshots": "snapshot_time",
    "processlist_snapshots": "snapshot_time",
    "lock_wait_snapshots": "snapshot_time",
    "transaction_snapshots": "snapshot_time",
    "metadata_lock_snapshots": "snapshot_time",
    "global_status_snapshots": "snapshot_time",
    "innodb_metric_snapshots": "snapshot_time",
    "wait_event_snapshots": "snapshot_time",
    "table_io_snapshots": "snapshot_time",
    "schema_snapshots": "snapshot_time",
    "ddl_changes": "detected_at",
    "buffer_pool_snapshots": "snapshot_time",
    "agent_analyses": "analyzed_at",
    "gcp_metric_snapshots": "snapshot_time",
    "slow_query_log": "snapshot_time",
    "unused_index_snapshots": "snapshot_time",
    "redundant_index_snapshots": "snapshot_time",
    "global_variable_snapshots": "snapshot_time",
    "innodb_status_snapshots": "snapshot_time",
    "execution_stage_snapshots": "snapshot_time",
    "explain_captures": "captured_at",
    "alert_history": "fired_at",
    "replication_lag_snapshots": "snapshot_time",
    "anomaly_events": "detected_at",
    "incident_windows": "start_time",
}

# Per-table retention overrides. If a table is listed here, it uses its own
# retention period (in days) regardless of the global default. Incident
# records and DDL history are postmortem evidence — worth keeping longer than
# raw metrics.
PER_TABLE_RETENTION_DAYS = {
    "incident_windows": 365,    # 1 year — incidents are high-value history
    "anomaly_events": 90,       # 3 months — raw anomaly events
    "ddl_changes": 365,         # 1 year — schema history
    "agent_analyses": 180,      # 6 months — LLM reasoning log
    "alert_history": 180,       # 6 months
}


def _retention_for(table: str, default_days: int) -> int:
    """Resolve retention for a table, applying per-table overrides first.

    Config can also override via `retention.overrides` in settings.yaml:

        retention:
          days: 90
          overrides:
            incident_windows: 180
    """
    cfg_overrides = get_config().get("retention", {}).get("overrides", {}) or {}
    if table in cfg_overrides:
        return int(cfg_overrides[table])
    if table in PER_TABLE_RETENTION_DAYS:
        return PER_TABLE_RETENTION_DAYS[table]
    return default_days


def _get_db_path() -> Path:
    """Resolve the monitoring DB file path."""
    config = get_config()
    return Path(config.get("monitoring_db", {}).get("path", "data/mysql_monitor.db"))


def _delete_older_than(conn, retention_days: int) -> int:
    """Delete rows older than retention_days from all tables, respecting
    per-table overrides. Returns total deleted."""
    total_deleted = 0
    now = datetime.utcnow()

    for table, ts_col in RETENTION_TABLES.items():
        table_days = _retention_for(table, retention_days)
        cutoff = (now - timedelta(days=table_days)).isoformat()
        try:
            cursor = conn.execute(
                f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)
            )
            deleted = cursor.rowcount
            total_deleted += deleted
            if deleted > 0:
                logger.info(f"  {table}: deleted {deleted} rows (retention: {table_days}d)")
        except Exception as e:
            logger.warning(f"  {table}: cleanup failed: {e}")

    return total_deleted


def check_db_size() -> bool:
    """
    Check if the SQLite DB file exceeds the configured max size.

    If it does, aggressively reduce retention (halving days each round)
    until the DB is under the limit or retention hits 1 day.

    Returns True if the DB is within limits, False if it could not be
    brought under the limit.
    """
    config = get_config()

    # Env var wins over config
    max_size_mb = int(os.environ.get(
        "SEEQL_DB_MAX_SIZE_MB",
        config.get("monitoring_db", {}).get("max_size_mb", 5000),
    ))
    max_size_bytes = max_size_mb * 1024 * 1024

    db_path = _get_db_path()
    if not db_path.exists():
        return True

    current_size = os.path.getsize(db_path)
    if current_size <= max_size_bytes:
        return True

    logger.warning(
        f"DB size {current_size / (1024*1024):.1f} MB exceeds limit of {max_size_mb} MB. "
        f"Running aggressive retention cleanup."
    )

    # Start with configured retention, halve each round
    retention_days = config.get("retention", {}).get("days", 90)

    while current_size > max_size_bytes and retention_days > 1:
        retention_days = max(1, retention_days // 2)
        logger.warning(f"Aggressive cleanup: trimming to {retention_days} day(s) retention")

        with get_mon_connection() as conn:
            deleted = _delete_older_than(conn, retention_days)
            if deleted > 0:
                conn.execute("VACUUM")
                logger.info(f"VACUUM complete after aggressive cleanup ({deleted} rows deleted)")

        current_size = os.path.getsize(db_path)
        logger.info(f"DB size after cleanup: {current_size / (1024*1024):.1f} MB")

    if current_size > max_size_bytes:
        logger.error(
            f"DB size {current_size / (1024*1024):.1f} MB still exceeds limit of {max_size_mb} MB "
            f"after aggressive cleanup with 1-day retention."
        )
        return False

    return True


def run_retention_cleanup() -> dict[str, int]:
    """
    Delete rows older than the configured retention period, respecting
    per-table overrides.

    Returns:
        Dict of table_name -> rows_deleted.
    """
    config = get_config()
    retention_days = config.get("retention", {}).get("days", 90)
    now = datetime.utcnow()

    logger.info(
        f"Running retention cleanup: default {retention_days}d "
        f"(per-table overrides: {PER_TABLE_RETENTION_DAYS})"
    )

    results = {}
    total_deleted = 0

    with get_mon_connection() as conn:
        for table, ts_col in RETENTION_TABLES.items():
            table_days = _retention_for(table, retention_days)
            cutoff = (now - timedelta(days=table_days)).isoformat()
            try:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff,)
                )
                deleted = cursor.rowcount
                results[table] = deleted
                total_deleted += deleted
                if deleted > 0:
                    logger.info(f"  {table}: deleted {deleted} rows (kept {table_days}d)")
            except Exception as e:
                logger.warning(f"  {table}: cleanup failed: {e}")
                results[table] = -1

        # VACUUM if significant rows were deleted to reclaim disk space
        if total_deleted > 1000:
            logger.info(f"Running VACUUM after deleting {total_deleted} rows...")
            conn.execute("VACUUM")
            logger.info("VACUUM complete.")

    logger.info(f"Retention cleanup complete: {total_deleted} total rows deleted")

    # Check DB size limits after normal cleanup
    check_db_size()

    return results
