"""Tests for storage module (connection + writer)."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import config as config_module
from storage import writer
from storage.writer import _batch_insert, _serialize_value


class TestSerializeValue:
    def test_datetime_to_iso(self):
        dt = datetime(2025, 1, 15, 10, 30, 0)
        assert _serialize_value(dt) == "2025-01-15T10:30:00"

    def test_int_passthrough(self):
        assert _serialize_value(42) == 42

    def test_float_passthrough(self):
        assert _serialize_value(3.14) == 3.14

    def test_str_passthrough(self):
        assert _serialize_value("hello") == "hello"

    def test_none_passthrough(self):
        assert _serialize_value(None) is None


class TestBatchInsert:
    def test_insert_rows(self, mon_db, test_config):
        conn, db_path = mon_db
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [
            {
                "snapshot_time": now,
                "event_name": "wait/io/file/innodb/innodb_data_file",
                "count_star": 50000,
                "total_wait_sec": 1.234,
                "avg_wait_sec": 0.000025,
            },
        ]
        count = _batch_insert(
            "wait_event_snapshots",
            ["snapshot_time", "event_name", "count_star", "total_wait_sec", "avg_wait_sec"],
            rows,
        )
        assert count == 1

        cursor = conn.execute("SELECT * FROM wait_event_snapshots")
        db_rows = cursor.fetchall()
        assert len(db_rows) == 1
        assert db_rows[0]["event_name"] == "wait/io/file/innodb/innodb_data_file"

    def test_insert_empty_list(self, mon_db, test_config):
        conn, db_path = mon_db
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config

        count = _batch_insert("wait_event_snapshots", ["snapshot_time"], [])
        assert count == 0


class TestWriteFunctions:
    @pytest.fixture(autouse=True)
    def setup_config(self, mon_db, test_config):
        conn, db_path = mon_db
        self.conn = conn
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config

    def _count_rows(self, table):
        return self.conn.execute(f"SELECT COUNT(*) as c FROM {table}").fetchone()["c"]

    def test_write_processlist(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "thread_id": 1, "pid": 10,
            "user": "app", "db": "mydb", "command": "Query",
            "state": "executing", "time_sec": 5, "query": "SELECT 1",
        }]
        writer.write_processlist(rows)
        assert self._count_rows("processlist_snapshots") == 1

    def test_write_lock_waits(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now,
            "waiting_trx_id": "T1", "waiting_pid": 10,
            "waiting_query": "UPDATE t SET x=1", "wait_seconds": 5,
            "blocking_trx_id": "T2", "blocking_pid": 11,
            "blocking_query": "SELECT * FROM t FOR UPDATE",
            "blocking_trx_age_sec": 30,
            "blocking_rows_locked": 100, "blocking_rows_modified": 0,
        }]
        writer.write_lock_waits(rows)
        assert self._count_rows("lock_wait_snapshots") == 1

    def test_write_transactions(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "trx_id": "T1", "trx_state": "RUNNING",
            "trx_started": "2025-01-01 10:00:00", "age_sec": 30,
            "pid": 11, "trx_query": "SELECT 1", "operation_state": "sending",
            "tables_in_use": 1, "tables_locked": 1, "lock_structs": 5,
            "rows_locked": 100, "rows_modified": 0, "isolation_level": "REPEATABLE READ",
        }]
        writer.write_transactions(rows)
        assert self._count_rows("transaction_snapshots") == 1

    def test_write_metadata_locks(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "object_type": "TABLE",
            "object_schema": "mydb", "object_name": "users",
            "lock_type": "SHARED_READ", "lock_duration": "TRANSACTION",
            "lock_status": "GRANTED", "owner_thread_id": 100,
        }]
        writer.write_metadata_locks(rows)
        assert self._count_rows("metadata_lock_snapshots") == 1

    def test_write_query_digests(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "digest": "abc123", "digest_text": "SELECT ?",
            "schema_name": "mydb", "exec_count": 100,
            "total_time_sec": 1.5, "avg_time_sec": 0.015,
            "max_time_sec": 0.1, "min_time_sec": 0.001,
            "rows_examined": 1000, "rows_sent": 100, "rows_affected": 0,
            "tmp_tables": 0, "tmp_disk_tables": 0, "full_joins": 0,
            "full_scans": 0, "no_index_used": 0, "no_good_index_used": 0,
            "sort_merge_passes": 0, "sum_errors": 0, "sum_warnings": 0,
            "first_seen": "2025-01-01", "last_seen": "2025-01-02",
        }]
        writer.write_query_digests(rows)
        assert self._count_rows("query_digest_snapshots") == 1

    def test_write_global_status(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "variable_name": "Questions",
            "raw_value": 1000, "delta_value": 500, "per_second": 1.67,
        }]
        writer.write_global_status(rows)
        assert self._count_rows("global_status_snapshots") == 1

    def test_write_innodb_metrics(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "metric_name": "buffer_pool_reads",
            "subsystem": "buffer", "count_value": 12345, "metric_type": "status_counter",
        }]
        writer.write_innodb_metrics(rows)
        assert self._count_rows("innodb_metric_snapshots") == 1

    def test_write_wait_events(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "event_name": "wait/io/file",
            "count_star": 50000, "total_wait_sec": 1.234, "avg_wait_sec": 0.00002,
        }]
        writer.write_wait_events(rows)
        assert self._count_rows("wait_event_snapshots") == 1

    def test_write_table_io(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "object_schema": "mydb", "table_name": "users",
            "count_read": 1000, "count_write": 500, "count_fetch": 1000,
            "count_insert": 100, "count_update": 300, "count_delete": 100,
            "total_io_sec": 5.5, "read_io_sec": 4.0, "write_io_sec": 1.5,
        }]
        writer.write_table_io(rows)
        assert self._count_rows("table_io_snapshots") == 1

    def test_write_buffer_pool(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "pool_id": 0, "pool_size": 65536,
            "free_buffers": 1000, "database_pages": 64000, "dirty_pages": 100,
            "pending_reads": 0, "pages_read": 50000, "pages_written": 30000,
            "hit_ratio": 0.992,
        }]
        writer.write_buffer_pool(rows)
        assert self._count_rows("buffer_pool_snapshots") == 1

    def test_write_schema_snapshots(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "snapshot_time": now, "table_schema": "mydb", "table_name": "users",
            "schema_hash": "abc", "index_hash": "def", "create_stmt": "CREATE TABLE ...",
            "table_rows": 1000, "data_mb": 5.0, "index_mb": 1.0,
        }]
        writer.write_schema_snapshots(rows)
        assert self._count_rows("schema_snapshots") == 1

    def test_write_ddl_changes(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{
            "detected_at": now, "table_schema": "mydb", "table_name": "users",
            "change_type": "schema",
            "old_schema_hash": "old_abc", "new_schema_hash": "new_abc",
            "old_index_hash": "old_def", "new_index_hash": "new_def",
            "old_ddl": "CREATE TABLE old", "new_ddl": "CREATE TABLE new",
        }]
        writer.write_ddl_changes(rows)
        assert self._count_rows("ddl_changes") == 1
