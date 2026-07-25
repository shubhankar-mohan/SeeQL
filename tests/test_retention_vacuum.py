"""Tests for storage/retention.py — VACUUM must not roll back the deletes.

SQLite refuses to run VACUUM inside a transaction (`OperationalError: cannot
VACUUM from within a transaction`). `get_mon_connection()` opens an implicit
transaction on the first DELETE and only commits when the `with` block exits
cleanly. Because the old code called `conn.execute("VACUUM")` *inside* that
same `with` block, a busy server (total_deleted > 1000) would hit the
VACUUM error, which propagated out of the context manager and rolled back
every DELETE that had just run — so retention silently deleted nothing and
the monitoring DB grew unbounded. This guards that the deletes survive.
"""

import config as config_module
from storage.connection import get_mon_connection, reset_connections
from storage.retention import run_retention_cleanup


class TestRetentionSurvivesVacuum:
    def test_stale_rows_stay_deleted_after_vacuum(self, mon_db):
        """Seed >1000 stale rows so the `total_deleted > 1000` VACUUM branch
        triggers, then assert run_retention_cleanup() actually deletes them
        instead of the VACUUM failure rolling back the whole transaction."""
        conn, db_path = mon_db
        config_module._config = {
            "monitoring_db": {
                "path": str(db_path),
                "wal_mode": False,
                "busy_timeout_ms": 5000,
            },
            "retention": {"days": 90},
        }
        reset_connections()

        with get_mon_connection() as mon_conn:
            mon_conn.executemany(
                "INSERT INTO global_status_snapshots "
                "(snapshot_time, server_id, variable_name, raw_value) VALUES (?,?,?,?)",
                [("2020-01-01T00:00:00", "default", "Threads_running", "1")] * 1500,
            )

        run_retention_cleanup()

        with get_mon_connection() as mon_conn:
            remaining = mon_conn.execute(
                "SELECT COUNT(*) FROM global_status_snapshots"
            ).fetchone()[0]
        assert remaining == 0, "stale rows must stay deleted after VACUUM"

        reset_connections()
