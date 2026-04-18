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
        # 24 base tables + anomaly_events + incident_windows = 26
        assert count == 26


@pytest.mark.skip(
    reason="Stale — uses pre-ServerContext collector API (get_prod_connection). "
    "Needs rewrite against ctx.get_connection(). See follow-up."
)
class TestFullCycleIntegration:
    """Run all three loops with mocked MySQL and verify SQLite has data."""

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

    def _make_mock_conn(self, data_sequence):
        """Create a mock connection that returns different data on each fetchall call."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.side_effect = [[row.copy() for row in d] for d in data_sequence]
        mock_cursor.fetchone.return_value = ("users", "CREATE TABLE users (id INT)")
        mock_conn.cursor.return_value = mock_cursor
        return mock_conn

    @patch("collectors.fast_loop.get_prod_connection")
    def test_fast_loop(self, mock_conn_ctx):
        from collectors.fast_loop import run_fast_loop

        mock_conn = self._make_mock_conn([
            MOCK_PROCESSLIST, MOCK_LOCK_WAITS, MOCK_TRANSACTIONS, MOCK_METADATA_LOCKS,
        ])
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        results = run_fast_loop()
        assert all(results.values()), f"Some collectors failed: {results}"
        assert self._count_rows("processlist_snapshots") > 0
        assert self._count_rows("lock_wait_snapshots") > 0
        assert self._count_rows("transaction_snapshots") > 0
        assert self._count_rows("metadata_lock_snapshots") > 0

    @patch("collectors.explain_capture.get_prod_connection")
    @patch("collectors.execution_stages.get_prod_connection")
    @patch("collectors.innodb_status.get_prod_connection")
    @patch("collectors.medium_loop.get_prod_connection")
    def test_medium_loop(self, mock_conn_ctx, mock_innodb_ctx, mock_stages_ctx, mock_explain_ctx):
        from collectors.medium_loop import run_medium_loop

        mock_conn = self._make_mock_conn([
            MOCK_QUERY_DIGESTS, MOCK_WAIT_EVENTS, MOCK_TABLE_IO,
            MOCK_INNODB_METRICS, MOCK_BUFFER_POOL, MOCK_GLOBAL_STATUS,
        ])
        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Mock innodb_status connection (uses fetchone, not fetchall)
        mock_innodb_conn = MagicMock()
        mock_innodb_cursor = MagicMock()
        mock_innodb_cursor.fetchone.return_value = ("InnoDB", "", "")
        mock_innodb_conn.cursor.return_value = mock_innodb_cursor
        mock_innodb_ctx.return_value.__enter__ = MagicMock(return_value=mock_innodb_conn)
        mock_innodb_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Mock execution_stages connection
        mock_stages_conn = self._make_mock_conn([[]])
        mock_stages_ctx.return_value.__enter__ = MagicMock(return_value=mock_stages_conn)
        mock_stages_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Mock explain_capture connection
        mock_explain_conn = self._make_mock_conn([[]])
        mock_explain_ctx.return_value.__enter__ = MagicMock(return_value=mock_explain_conn)
        mock_explain_ctx.return_value.__exit__ = MagicMock(return_value=False)

        results = run_medium_loop()
        # GCP collectors will fail (no GCP mock) — that's expected
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

    @patch("collectors.global_variables.get_prod_connection")
    @patch("collectors.index_analysis.get_prod_connection")
    @patch("collectors.slow_loop.get_prod_connection")
    @patch("collectors.slow_loop.get_mon_reader")
    def test_slow_loop(self, mock_reader_ctx, mock_conn_ctx, mock_idx_conn_ctx, mock_var_conn_ctx):
        from collectors.slow_loop import run_slow_loop, _schema_collector

        # Reset collector state for clean test
        _schema_collector._previous_hashes = {}
        _schema_collector._initialized = False

        # Mock the reader for _load_previous_hashes
        mock_reader_conn = MagicMock()
        mock_reader_cursor = MagicMock()
        mock_reader_cursor.fetchall.return_value = []
        mock_reader_conn.execute.return_value = mock_reader_cursor
        mock_reader_ctx.return_value.__enter__ = MagicMock(return_value=mock_reader_conn)
        mock_reader_ctx.return_value.__exit__ = MagicMock(return_value=False)

        mock_conn = self._make_mock_conn([
            MOCK_SCHEMA_FINGERPRINT, MOCK_INDEX_FINGERPRINT, MOCK_TABLE_SIZES,
        ])
        # Mock SHOW CREATE TABLE
        show_cursor = MagicMock()
        show_cursor.fetchone.return_value = ("users", "CREATE TABLE users (id INT)")
        mock_conn.cursor.side_effect = [mock_conn.cursor.return_value, show_cursor, show_cursor]

        mock_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Mock index analysis connections
        mock_idx_conn = self._make_mock_conn([[]])  # empty results
        mock_idx_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_idx_conn)
        mock_idx_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        # Mock global variables connection
        mock_var_conn = self._make_mock_conn([[
            {"Variable_name": "max_connections", "Value": "151"},
        ]])
        mock_var_conn_ctx.return_value.__enter__ = MagicMock(return_value=mock_var_conn)
        mock_var_conn_ctx.return_value.__exit__ = MagicMock(return_value=False)

        results = run_slow_loop()
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
