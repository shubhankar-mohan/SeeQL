"""Shared pytest fixtures for SeeQL tests."""

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config as config_module
from storage.connection import reset_connections


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


@pytest.fixture(autouse=True)
def reset_config():
    """Reset the config singleton between tests."""
    config_module._config = None
    yield
    config_module._config = None


@pytest.fixture(autouse=True)
def reset_storage():
    """Reset storage connections + anomaly cache between tests."""
    yield
    reset_connections()
    try:
        from alerting.anomaly import _clear_cache
        _clear_cache()
    except ImportError:
        pass


@pytest.fixture
def mon_db(tmp_path, monkeypatch):
    """Create a SQLite monitoring DB with schema initialized + migrations run."""
    db_path = tmp_path / "test_monitor.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    schema_sql = SCHEMA_SQL_PATH.read_text()
    conn.executescript(schema_sql)
    conn.commit()

    # Also run migrations — belt-and-suspenders for any test that upgrades from
    # an older test fixture. schema.sql is canonical, so this is usually a no-op.
    # We have to monkeypatch the mon DB path so get_mon_connection() finds our temp DB.
    import config as config_module
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
    }
    try:
        from storage.migrations import run_all_migrations
        from storage.connection import reset_connections
        reset_connections()
        run_all_migrations()
    finally:
        config_module._config = prev
        from storage.connection import reset_connections
        reset_connections()

    yield conn, db_path
    conn.close()


@pytest.fixture
def mock_prod_connection():
    """Mock MySQL production connection + cursor."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn, mock_cursor


@pytest.fixture
def test_config(tmp_path):
    """Return a minimal test config dict."""
    return {
        "production_db": {
            "host": "127.0.0.1",
            "port": 3307,
            "user": "test_user",
            "password": "test_pass",
            "database": "mydb",
            "pool_size": 1,
            "connect_timeout": 5,
        },
        "monitoring_db": {
            "path": str(tmp_path / "test_monitor.db"),
            "wal_mode": True,
            "busy_timeout_ms": 5000,
        },
        "intervals": {
            "fast_loop": 1,
            "medium_loop": 1,
            "slow_loop": 1,
        },
        "limits": {
            "top_queries": 10,
            "processlist_query_max_len": 500,
            "digest_text_max_len": 200,
            "max_batch_size": 500,
        },
        "excluded_schemas": ["mysql", "performance_schema", "sys", "information_schema"],
        "retention": {
            "metric_snapshots": 90,
            "query_digests": 90,
            "schema_snapshots": 180,
            "lock_snapshots": 30,
            "processlist_snapshots": 7,
        },
        "gcp": {
            "project_id": "test-project",
            "cloud_sql_instance_id": "test-instance",
            "region": "us-central1",
        },
        "logging": {
            "level": "DEBUG",
            "file": str(tmp_path / "test.log"),
            "max_bytes": 10485760,
            "backup_count": 1,
        },
    }
