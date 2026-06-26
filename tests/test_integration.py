"""Integration tests — full collection cycle with mocked MySQL."""

import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import config as config_module
from storage.connection import reset_connections
from tests.fixtures.mysql_mock_data import (
    MOCK_PROCESSLIST,
    MOCK_LOCK_WAITS,
    MOCK_TRANSACTIONS,
    MOCK_METADATA_LOCKS,
    MOCK_QUERY_DIGESTS,
    MOCK_WAIT_EVENTS,
    MOCK_TABLE_IO,
    MOCK_INNODB_METRICS,
    MOCK_BUFFER_POOL,
    MOCK_GLOBAL_STATUS,
    MOCK_SCHEMA_FINGERPRINT,
    MOCK_INDEX_FINGERPRINT,
    MOCK_TABLE_SIZES,
)


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"

ALL_TABLES = [
    "query_digest_snapshots",
    "processlist_snapshots",
    "lock_wait_snapshots",
    "transaction_snapshots",
    "metadata_lock_snapshots",
    "global_status_snapshots",
    "innodb_metric_snapshots",
    "wait_event_snapshots",
    "table_io_snapshots",
    "schema_snapshots",
    "ddl_changes",
    "buffer_pool_snapshots",
    "agent_analyses",
    "gcp_metric_snapshots",
    "slow_query_log",
    "unused_index_snapshots",
    "redundant_index_snapshots",
    "global_variable_snapshots",
    "innodb_status_snapshots",
    "execution_stage_snapshots",
    "explain_captures",
    "alert_history",
    "inbound_alerts",
    "investigations",
    "investigation_samples",
    "investigation_findings",
]


class TestSchemaValidation:
    """Verify all 22 tables are created by schema.sql."""

    def test_all_tables_created(self, mon_db):
        conn, _ = mon_db
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row["name"] for row in cursor.fetchall()}
        for table in ALL_TABLES:
            assert table in tables, f"Table {table} not found in schema"

    def test_table_count(self, mon_db):
        conn, _ = mon_db
        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM sqlite_master WHERE type='table'"
        )
        count = cursor.fetchone()["c"]
        # 24 base tables + anomaly_events + incident_windows
        # + inbound_alerts + investigations + investigation_samples + investigation_findings = 30
        assert count == 30


class TestFullCycleIntegration:
    """Run all three loops with mocked MySQL and verify SQLite has data.

    Each loop runner is passed an explicit mock ``ServerContext`` whose
    ``get_connection()`` yields one mock MySQL connection per collector (in
    collector order). The real writer runs against a temp SQLite DB.
    """

    @pytest.fixture(autouse=True)
    def setup_full_cycle(self, tmp_path, test_config):
        """Set up config pointing to a real temp SQLite DB."""
        db_path = tmp_path / "integration_test.db"
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config

        # Initialize the schema
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        conn.commit()
        conn.close()

        self.db_path = db_path
        yield
        reset_connections()

    def _count_rows(self, table):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        count = conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]
        conn.close()
        return count

    @staticmethod
    def _conn(data):
        """Mock connection whose cursor.fetchall() returns ``data`` (one call)."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [row.copy() for row in data]
        conn.cursor.return_value = cursor
        return conn

    @staticmethod
    def _multi_fetchall_conn(data_sequence):
        """Mock connection returning a different list on each fetchall() call."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[row.copy() for row in d] for d in data_sequence]
        conn.cursor.return_value = cursor
        return conn

    @staticmethod
    def _innodb_status_conn():
        """innodb_status uses cursor.fetchone() returning a 3-tuple."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = ("InnoDB", "", "")
        conn.cursor.return_value = cursor
        return conn

    @staticmethod
    def _cm(conn):
        """Wrap a mock conn in a context manager (for ctx.get_connection)."""
        cm = MagicMock()
        cm.__enter__.return_value = conn
        cm.__exit__.return_value = False
        return cm

    def _make_ctx(self, conns):
        """Build a mock ServerContext that yields each conn in order."""
        ctx = MagicMock()
        ctx.server_id = "test-server"
        ctx.get_connection.side_effect = [self._cm(c) for c in conns]
        return ctx

    def test_fast_loop(self):
        from collectors.fast_loop import run_fast_loop

        # Collector order: processlist, lock_waits, transactions, metadata_locks.
        ctx = self._make_ctx([
            self._conn(MOCK_PROCESSLIST),
            self._conn(MOCK_LOCK_WAITS),
            self._conn(MOCK_TRANSACTIONS),
            self._conn(MOCK_METADATA_LOCKS),
        ])

        results = run_fast_loop(ctx)
        assert all(results.values()), f"Some collectors failed: {results}"
        assert self._count_rows("processlist_snapshots") > 0
        assert self._count_rows("lock_wait_snapshots") > 0
        assert self._count_rows("transaction_snapshots") > 0
        assert self._count_rows("metadata_lock_snapshots") > 0

    def test_medium_loop(self):
        from collectors.medium_loop import run_medium_loop, _global_status_collector

        # Ensure first-run delta behavior for the shared global_status singleton.
        _global_status_collector._delta_calcs = {}

        # get_connection is consumed (in order) by the MySQL collectors:
        # query_digests, wait_events, table_io, innodb_metrics, buffer_pool,
        # global_status, innodb_status, execution_stages, explain_capture.
        # The GCP collectors run but DO NOT call get_connection (they will fail
        # harmlessly with no GCP creds — not asserted).
        ctx = self._make_ctx([
            self._conn(MOCK_QUERY_DIGESTS),
            self._conn(MOCK_WAIT_EVENTS),
            self._conn(MOCK_TABLE_IO),
            self._conn(MOCK_INNODB_METRICS),
            self._conn(MOCK_BUFFER_POOL),
            self._conn(MOCK_GLOBAL_STATUS),
            self._innodb_status_conn(),
            self._conn([]),   # execution_stages
            self._conn([]),   # explain_capture
        ])

        results = run_medium_loop(ctx)
        mysql_collectors = ["query_digests", "wait_events", "table_io",
                            "innodb_metrics", "buffer_pool", "global_status"]
        for name in mysql_collectors:
            assert results.get(name, False), f"{name} failed unexpectedly"
        assert self._count_rows("query_digest_snapshots") > 0
        assert self._count_rows("wait_event_snapshots") > 0
        assert self._count_rows("table_io_snapshots") > 0
        assert self._count_rows("innodb_metric_snapshots") > 0
        assert self._count_rows("buffer_pool_snapshots") > 0
        assert self._count_rows("global_status_snapshots") > 0

    @patch("collectors.slow_loop.get_mon_reader")
    def test_slow_loop(self, mock_get_mon_reader):
        from collectors.slow_loop import run_slow_loop, _schema_collector

        # Reset schema collector state for a clean first run.
        _schema_collector._previous_hashes = {}
        _schema_collector._initialized = set()

        # _load_previous_hashes reads the monitoring DB → return no rows.
        reader_conn = MagicMock()
        reader_cursor = MagicMock()
        reader_cursor.fetchall.return_value = []
        reader_conn.execute.return_value = reader_cursor
        reader_cm = MagicMock()
        reader_cm.__enter__.return_value = reader_conn
        reader_cm.__exit__.return_value = False
        mock_get_mon_reader.return_value = reader_cm

        # schema_snapshot makes 3 fetchall calls on one cursor (fingerprints,
        # indexes, table sizes). With no previous hashes, no SHOW CREATE TABLE.
        schema_conn = self._multi_fetchall_conn([
            MOCK_SCHEMA_FINGERPRINT, MOCK_INDEX_FINGERPRINT, MOCK_TABLE_SIZES,
        ])

        # Collector order: schema_snapshot, unused_indexes, redundant_indexes,
        # global_variables.
        ctx = self._make_ctx([
            schema_conn,
            self._conn([]),   # unused_indexes
            self._conn([]),   # redundant_indexes
            self._conn([{"Variable_name": "max_connections", "Value": "151"}]),
        ])

        results = run_slow_loop(ctx)
        assert results.get("schema_snapshot", False), f"Schema snapshot failed: {results}"
        assert self._count_rows("schema_snapshots") > 0


class TestSchedulerCreation:
    def test_create_scheduler_has_4_jobs(self, test_config):
        config_module._config = test_config
        from scheduler.runner import create_scheduler
        scheduler = create_scheduler()
        jobs = scheduler.get_jobs()
        assert len(jobs) == 4
        job_ids = {j.id for j in jobs}
        assert job_ids == {"fast_loop", "medium_loop", "slow_loop", "retention_loop"}
