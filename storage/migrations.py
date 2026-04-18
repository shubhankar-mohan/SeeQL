"""
Schema migrations for the monitoring SQLite database.

Handles adding new columns and tables as the schema evolves,
without requiring a full re-init.
"""

import logging
from storage.connection import get_mon_connection

logger = logging.getLogger(__name__)

# All tables that need a server_id column, with their timestamp column name
_TABLES_NEEDING_SERVER_ID = {
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
}


def _get_columns(conn, table: str) -> set[str]:
    """Get column names for a table."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row["cnt"] > 0


def migrate_add_server_id() -> int:
    """Add server_id column to all monitoring tables. Returns number of tables altered."""
    altered = 0
    with get_mon_connection() as conn:
        for table, ts_col in _TABLES_NEEDING_SERVER_ID.items():
            if not _table_exists(conn, table):
                continue
            cols = _get_columns(conn, table)
            if "server_id" in cols:
                continue

            logger.info(f"Migration: adding server_id to {table}")
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN server_id TEXT NOT NULL DEFAULT 'default'"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_sid_time ON {table}(server_id, {ts_col})"
            )
            altered += 1

    if altered:
        logger.info(f"Migration complete: added server_id to {altered} table(s)")
    return altered


def migrate_create_servers_table() -> bool:
    """Create the servers registry table if it doesn't exist."""
    with get_mon_connection() as conn:
        if _table_exists(conn, "servers"):
            return False

        logger.info("Migration: creating servers table")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS servers (
                server_id       TEXT PRIMARY KEY,
                display_name    TEXT NOT NULL,
                environment     TEXT NOT NULL DEFAULT 'production',
                role            TEXT NOT NULL DEFAULT 'primary',
                cluster_id      TEXT,
                tags            TEXT,
                host            TEXT,
                port            INTEGER DEFAULT 3306,
                is_active       INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS replication_lag_snapshots (
                id              INTEGER PRIMARY KEY,
                server_id       TEXT NOT NULL,
                snapshot_time   TEXT NOT NULL,
                lag_seconds     REAL,
                source_server_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_rls_server_time
                ON replication_lag_snapshots(server_id, snapshot_time);
        """)
        return True


def run_all_migrations():
    """Run all pending migrations."""
    logger.info("Checking for pending migrations...")
    migrate_create_servers_table()
    migrate_add_server_id()
    logger.info("All migrations complete.")
