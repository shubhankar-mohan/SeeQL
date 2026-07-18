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


class TestNullDataResilience:
    """Regression guards for the bug class that kept shipping: `digest_text` is
    the only nullable column in query_digest_snapshots, and routes that slice or
    format it (`x[:N]`, `:.2f`) crashed with a 500 when it came back NULL.
    Each test seeds a NULL-`digest_text` row and asserts the route still renders."""

    def _seed_and_reset(self, rows):
        import sqlite3
        import config as config_module
        db_path = config_module._config["monitoring_db"]["path"]
        conn = sqlite3.connect(db_path)
        for sql, params in rows:
            conn.execute(sql, params)
        conn.commit()
        conn.close()
        # Make seeded rows + the synthesized 'default' server visible.
        import api.query_helpers as qh
        if qh._reader_conn is not None:
            qh._reader_conn.close()
            qh._reader_conn = None
        import config.server_registry as sr
        sr._registry = None

    def test_state_report_with_null_digest_text(self, api_client):
        self._seed_and_reset([(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec,
                rows_examined, rows_sent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-06-30T03:00:00", "default", "0xNULLTXT", None, "shop",
             100, 5.0, 0.05, 0.2, 5000, 5),
        )])
        resp = api_client.get("/api/v1/state-report")
        assert resp.status_code == 200

    def test_todo_regression_with_null_digest_text(self, api_client):
        self._seed_and_reset([
            ("""INSERT INTO query_digest_snapshots
                (snapshot_time, server_id, digest, digest_text, schema_name,
                 exec_count, total_time_sec, avg_time_sec, max_time_sec,
                 rows_examined, rows_sent)
                VALUES (datetime('now','-5 minutes'),?,?,?,?,?,?,?,?,?,?)""",
             ("default", "0xREGNULL", None, "shop", 100, 30.0, 0.30, 0.5, 9000, 9)),
            ("""INSERT INTO query_digest_snapshots
                (snapshot_time, server_id, digest, digest_text, schema_name,
                 exec_count, total_time_sec, avg_time_sec, max_time_sec,
                 rows_examined, rows_sent)
                VALUES (datetime('now','-3 days'),?,?,?,?,?,?,?,?,?,?)""",
             ("default", "0xREGNULL", None, "shop", 50, 1.0, 0.02, 0.05, 100, 5)),
        ])
        resp = api_client.get("/dashboard/todo")
        assert resp.status_code == 200

    def test_query_detail_with_null_sample_and_text(self, api_client):
        self._seed_and_reset([(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, query_sample_text,
                schema_name, exec_count, total_time_sec, avg_time_sec, max_time_sec,
                rows_examined, rows_sent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("2026-06-30T03:00:00", "default", "0xNOSAMPLE", None, None, "shop",
             10, 1.0, 0.1, 0.3, 5000, 5),
        )])
        resp = api_client.get("/dashboard/partials/query-detail/0xNOSAMPLE")
        assert resp.status_code == 200


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
        import sqlite3
        import json as _json
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


class TestPrometheusMetrics:
    """Guards P1c-4 (freshness) and P1c-9 (per-server labels) in api/prometheus.py.

    The `mysql_*`/`seeql_*` Gauges are module-level singletons registered
    once against prometheus_client's global REGISTRY (they persist for the
    life of the test process), so each test resets the metrics-cache TTL
    guard (`api.prometheus._last_update`) and the server-registry singleton
    before hitting `/metrics` — otherwise a prior test's cached read, or a
    stale server list from a previous test's config, would leak in.

    `config.server_registry._registry` is itself a process-wide singleton
    that `conftest.py`'s autouse fixtures do NOT reset (only `config._config`
    and storage connections are). The two-server test below points it at a
    registry with no 'default' entry — left in place, that changes
    `get_default_server_id()` for every later test in the same process. Reset
    it after each test here too, not just before.
    """

    @pytest.fixture(autouse=True)
    def _reset_server_registry(self):
        import config.server_registry as sr
        sr._registry = None
        yield
        sr._registry = None

    @staticmethod
    def _has_sample(metric_name: str, **labels) -> bool:
        """Inspect the live prometheus_client REGISTRY for an exact
        (metric, label-set) sample. More precise than substring-matching the
        exposition text: it can tell "never set" apart from "set to a value
        that just doesn't happen to appear in the text elsewhere."""
        from prometheus_client import REGISTRY
        for family in REGISTRY.collect():
            for sample in family.samples:
                if sample.name == metric_name and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return True
        return False

    @staticmethod
    def _sample_value(metric_name: str, **labels):
        """Same lookup as _has_sample, but returns the sample's numeric
        value (or None if absent) so a test can assert "present AND equal
        to 0" rather than just "present" — a gauge that's absent and a
        gauge that's present-but-wrong both need to fail this check."""
        from prometheus_client import REGISTRY
        for family in REGISTRY.collect():
            for sample in family.samples:
                if sample.name == metric_name and all(
                    sample.labels.get(k) == v for k, v in labels.items()
                ):
                    return sample.value
        return None

    def test_stale_data_dropped_and_freshness_exported(self, api_client):
        """P1c-4: a row older than the freshness window must not be served
        as a live gauge value forever, but the collection-freshness signal
        itself must always be exported so the staleness is observable."""
        import sqlite3
        import config as config_module
        import api.prometheus as prom

        db_path = config_module._config["monitoring_db"]["path"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO global_status_snapshots
               (snapshot_time, server_id, variable_name, raw_value)
               VALUES (datetime('now','-30 minutes'), 'default', 'Threads_running', 42)"""
        )
        conn.commit()
        conn.close()

        # Bypass the metrics-cache TTL so this scrape actually re-reads SQLite.
        prom._last_update = 0

        resp = api_client.get("/metrics")
        assert resp.status_code == 200

        # The collection-freshness signal is always exported...
        assert self._has_sample("seeql_collection_last_timestamp", loop="metrics_cache")
        # ...but the 30-minute-stale row must not be served as a live gauge.
        assert not self._has_sample("mysql_threads_running", server="default")

    def test_two_server_labels_both_present(self, api_client):
        """P1c-9: gauges must carry a per-server label so a two-server
        install reports independent time series instead of one gauge
        flapping between servers."""
        import sqlite3
        import config as config_module
        import api.prometheus as prom

        # The registry singleton is already None here (reset by the autouse
        # fixture above before this test started), so the next
        # get_server_registry() call will lazily rebuild it from this
        # two-server config.
        config_module._config["servers"] = {
            "server_a": {"display_name": "Server A"},
            "server_b": {"display_name": "Server B"},
        }

        db_path = config_module._config["monitoring_db"]["path"]
        conn = sqlite3.connect(db_path)
        for sid in ("server_a", "server_b"):
            conn.execute(
                """INSERT INTO global_status_snapshots
                   (snapshot_time, server_id, variable_name, raw_value)
                   VALUES (datetime('now'), ?, 'Threads_running', 7)""",
                (sid,),
            )
        conn.commit()
        conn.close()

        prom._last_update = 0

        resp = api_client.get("/metrics")
        assert resp.status_code == 200
        assert self._has_sample("mysql_threads_running", server="server_a")
        assert self._has_sample("mysql_threads_running", server="server_b")

    def test_gauge_disappears_after_going_stale_in_same_process(self, api_client):
        """P1c-4, the core "frozen forever" scenario: a gauge that WAS live
        during this process must actually disappear once its data goes
        stale, not just fail to appear the first time.

        Gauge.set() has no complementary "unset" — once a label combination
        has been set, prometheus_client keeps reporting that value on every
        future scrape until something calls .remove() on it, even if the
        code simply stops calling .set(). The other two tests in this class
        can't catch a regression here: the freshness test never sets the
        gauge in the first place, and the two-server test never lets its
        rows go stale. This test seeds a fresh row, confirms the gauge is
        live, ages the row past the freshness window, and confirms the
        gauge is gone — using a server_id not touched by any other test in
        this class, so it can't pick up a leftover value from one of them.
        """
        import sqlite3
        import config as config_module
        import api.prometheus as prom

        config_module._config["servers"] = {"server_transient": {"display_name": "Transient"}}

        db_path = config_module._config["monitoring_db"]["path"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO global_status_snapshots
               (snapshot_time, server_id, variable_name, raw_value)
               VALUES (datetime('now'), 'server_transient', 'Threads_running', 99)"""
        )
        conn.commit()
        conn.close()

        prom._last_update = 0
        resp = api_client.get("/metrics")
        assert resp.status_code == 200
        assert self._has_sample("mysql_threads_running", server="server_transient")

        # Age the same row past the freshness window — simulates the
        # collector for this server dying without any new row arriving.
        conn = sqlite3.connect(db_path)
        conn.execute(
            """UPDATE global_status_snapshots
               SET snapshot_time = datetime('now','-30 minutes')
               WHERE server_id = 'server_transient'"""
        )
        conn.commit()
        conn.close()

        prom._last_update = 0
        resp = api_client.get("/metrics")
        assert resp.status_code == 200
        assert not self._has_sample("mysql_threads_running", server="server_transient")

    def test_lock_gauge_reports_zero_when_quiet_but_heartbeat_alive(self, api_client):
        """M1: lock_wait_snapshots is a sparse/event table — LockWaitCollector
        only inserts a row when contention actually exists, so "no row in
        the last 10 minutes" is consistent with both "the collector died"
        and "the collector is healthy and correctly found nothing to
        report" (see task-2.7-report.md's sparse-table caveat). Gating on
        lock_wait_snapshots' own freshness can't tell these apart, so a
        perfectly healthy, quiet server wrongly goes ABSENT instead of
        reporting a true 0.

        Fix: gate on the collector heartbeat (global_status_snapshots,
        written every medium-loop cycle regardless of outcome) instead.
        Heartbeat fresh + zero lock rows => present, value 0. Heartbeat
        stale => the collector itself is dead => absent, same as every
        other gauge in this module.
        """
        import sqlite3
        import config as config_module
        import api.prometheus as prom

        config_module._config["servers"] = {"server_lockheartbeat": {"display_name": "LockHeartbeat"}}

        db_path = config_module._config["monitoring_db"]["path"]
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO global_status_snapshots
               (snapshot_time, server_id, variable_name, raw_value)
               VALUES (datetime('now'), 'server_lockheartbeat', 'Threads_running', 5)"""
        )
        # Deliberately no lock_wait_snapshots row: this server has been
        # quiet (healthy), not dead.
        conn.commit()
        conn.close()

        prom._last_update = 0
        resp = api_client.get("/metrics")
        assert resp.status_code == 200
        assert self._has_sample("mysql_lock_waits_current", server="server_lockheartbeat")
        assert self._sample_value("mysql_lock_waits_current", server="server_lockheartbeat") == 0

        # Now the heartbeat itself goes stale — the collector for this
        # server has actually stopped. Age global_status_snapshots, not
        # lock_wait_snapshots (which never had a row to begin with).
        conn = sqlite3.connect(db_path)
        conn.execute(
            """UPDATE global_status_snapshots
               SET snapshot_time = datetime('now','-30 minutes')
               WHERE server_id = 'server_lockheartbeat'"""
        )
        conn.commit()
        conn.close()

        prom._last_update = 0
        resp = api_client.get("/metrics")
        assert resp.status_code == 200
        assert not self._has_sample("mysql_lock_waits_current", server="server_lockheartbeat")
