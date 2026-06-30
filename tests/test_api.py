"""Tests for the API layer."""

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

import config as config_module
from api.app import create_app
from storage.connection import reset_connections


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


@pytest.fixture
def api_client(tmp_path, test_config):
    """Create a FastAPI test client with a temp SQLite DB."""
    db_path = tmp_path / "api_test.db"
    test_config["monitoring_db"]["path"] = str(db_path)
    config_module._config = test_config

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL_PATH.read_text())
    conn.commit()
    conn.close()

    app = create_app()
    client = TestClient(app)
    yield client
    reset_connections()


class TestHealthEndpoint:
    @patch("api.routes.check_prod_connection", return_value=True)
    @patch("api.routes.check_mon_connection", return_value=True)
    def test_healthy(self, mock_mon, mock_prod, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    @patch("api.routes.check_prod_connection", return_value=False)
    @patch("api.routes.check_mon_connection", return_value=True)
    def test_degraded(self, mock_mon, mock_prod, api_client):
        resp = api_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"


class TestDashboardRoutes:
    """Render dashboard HTML routes — guards the Starlette TemplateResponse
    signature (request must be the first positional arg) and the root redirect."""

    def test_overview_renders(self, api_client):
        resp = api_client.get("/dashboard")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_queries_page_renders(self, api_client):
        resp = api_client.get("/dashboard/queries")
        assert resp.status_code == 200

    def test_todo_renders_with_null_aggregates(self, api_client):
        """The todo route formats SQL aggregates (AVG/SUM/MAX) that come back
        NULL on an empty DB. Guards against `unsupported format string passed
        to NoneType` — every formatted aggregate must be None-coalesced."""
        resp = api_client.get("/dashboard/todo")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_root_redirects_to_dashboard(self, api_client):
        resp = api_client.get("/", follow_redirects=False)
        assert resp.status_code in (302, 307)
        assert resp.headers["location"] == "/dashboard"


class TestCollectEndpoints:
    @patch("collectors.fast_loop.writer")
    @patch("storage.connection.get_prod_connection")
    def test_collect_fast(self, mock_get_conn, mock_writer, api_client):
        # The endpoint calls run_fast_loop() with no ctx, so each collector
        # falls back to the default server from the registry, whose
        # ctx.get_connection() ultimately calls storage.connection.get_prod_connection.
        # Reset the registry singleton so it reloads the test config's default server.
        import config.server_registry as sr
        sr._registry = None

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor

        cm = MagicMock()
        cm.__enter__.return_value = mock_conn
        cm.__exit__.return_value = False
        mock_get_conn.return_value = cm

        resp = api_client.post("/collect/fast")
        assert resp.status_code == 200
        data = resp.json()
        assert data["loop"] == "fast"
        assert "results" in data
        # All four fast collectors should have run and succeeded.
        assert data["results"] == {
            "processlist": True,
            "lock_waits": True,
            "transactions": True,
            "metadata_locks": True,
        }


class TestIncidentsEndpoint:
    def test_empty(self, api_client):
        resp = api_client.get("/api/v1/incidents/recent")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_seeded(self, api_client, tmp_path):
        """Seed one incident row and confirm the endpoint shapes it correctly."""
        import sqlite3, json as _json
        # Find the DB path from config
        import config as config_module
        db_path = config_module._config["monitoring_db"]["path"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO incident_windows
               (server_id, start_time, end_time, severity, involved_metrics,
                event_count, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("default", "2026-04-10T03:12:00", "2026-04-10T03:47:00",
             "critical", _json.dumps(["threads_running", "lock_frequency"]),
             8, "detected"),
        )
        conn.commit()
        conn.close()

        # Reset any cached reader connection so we pick up the seeded row.
        from api.query_helpers import _reader_conn
        import api.query_helpers as qh
        if qh._reader_conn is not None:
            qh._reader_conn.close()
            qh._reader_conn = None

        resp = api_client.get("/api/v1/incidents/recent?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        row = data[0]
        assert row["severity"] == "critical"
        assert row["event_count"] == 8
        assert row["involved_metrics"] == ["threads_running", "lock_frequency"]
        assert row["duration_minutes"] == 35
        assert row["status"] == "detected"


class TestTimeRangeCustom:
    def test_custom_from_to_accepted(self, api_client):
        """Custom from/to should override the preset range."""
        resp = api_client.get(
            "/api/v1/queries/top?from=2026-04-10T03:00:00&to=2026-04-10T05:00:00"
        )
        assert resp.status_code == 200
        # Empty data is fine — we just need the query shape to parse
        assert isinstance(resp.json(), list)


class TestSchemasEndpoint:
    def test_schemas_empty(self, api_client):
        resp = api_client.get("/api/v1/schemas")
        assert resp.status_code == 200
        body = resp.json()
        assert "schemas" in body
        assert "tables" in body

    def test_schemas_with_seed(self, api_client):
        """Seed distinct schemas and tables across the contributing sources."""
        import sqlite3
        import config as config_module
        db_path = config_module._config["monitoring_db"]["path"]

        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, digest, digest_text, schema_name, exec_count)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-04-10T03:00:00", "0xA", "SELECT 1", "shop", 1),
        )
        conn.execute(
            """INSERT INTO table_io_snapshots
               (snapshot_time, object_schema, table_name)
               VALUES (?, ?, ?)""",
            ("2026-04-10T03:00:00", "shop", "loyalty_members"),
        )
        conn.execute(
            """INSERT INTO schema_snapshots
               (snapshot_time, table_schema, table_name, schema_hash, index_hash)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-04-10T03:00:00", "otherdb", "orders", "h1", "h2"),
        )
        conn.commit()
        conn.close()

        # Reset cached reader so the new rows are visible
        import api.query_helpers as qh
        if qh._reader_conn is not None:
            qh._reader_conn.close()
            qh._reader_conn = None

        resp = api_client.get("/api/v1/schemas")
        body = resp.json()
        assert "shop" in body["schemas"]
        assert "otherdb" in body["schemas"]
        table_names = {(t["schema"], t["name"]) for t in body["tables"]}
        assert ("shop", "loyalty_members") in table_names
        assert ("otherdb", "orders") in table_names


class TestDataEndpoints:
    def test_queries_empty(self, api_client):
        resp = api_client.get("/data/queries")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_locks_empty(self, api_client):
        resp = api_client.get("/data/locks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_schema_changes_empty(self, api_client):
        resp = api_client.get("/data/schema-changes")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_global_status_empty(self, api_client):
        resp = api_client.get("/data/global-status")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_queries_with_limit(self, api_client):
        resp = api_client.get("/data/queries?limit=5")
        assert resp.status_code == 200


class TestStatusEndpoint:
    def test_scheduler_not_running(self, api_client):
        resp = api_client.get("/status")
        assert resp.status_code == 200
