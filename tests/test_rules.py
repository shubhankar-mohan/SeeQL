"""Tests for alerting/rules.py.

Covers NULL-resilience (`evaluate_query_regression` sliced `digest_text[:60]`
without a guard; a top regression with a NULL digest_text raised TypeError,
which the engine swallowed and silently dropped a real regression alert),
plus three rule-edge fixes (Task 3.3):

- P1c-5: `evaluate_high_cpu`/`evaluate_high_memory` must ignore stale
  `gcp_metric_snapshots` rows instead of re-alerting on a reading that's
  days old.
- P1c-7: `evaluate_threads_running_spike`'s 24h baseline must exclude the
  last hour, or a sustained incident raises its own baseline until the
  alert self-silences.
- P1c-8: `evaluate_query_regression` must apply an absolute floor on
  `recent_avg`, or a ratio-only gate reports "5x slower" for negligible
  absolute latencies (0.1ms -> 0.5ms).
"""

import contextlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config as config_module

SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


def _iso(dt):
    return dt.replace(tzinfo=None).isoformat()


def _fake_reader(conn):
    """Wrap an already-open sqlite3.Connection as a get_mon_reader() replacement."""
    conn.row_factory = sqlite3.Row

    @contextlib.contextmanager
    def reader():
        yield conn
    return reader


def _seed_regression(db_path, digest_text):
    conn = sqlite3.connect(str(db_path))
    # Recent (within 1h): high avg latency.
    conn.execute(
        """INSERT INTO query_digest_snapshots
           (snapshot_time, server_id, digest, digest_text, exec_count, avg_time_sec)
           VALUES (datetime('now','-5 minutes'), 'default', '0xREG', ?, 100, 0.30)""",
        (digest_text,),
    )
    # Baseline (3 days ago): low avg latency.
    conn.execute(
        """INSERT INTO query_digest_snapshots
           (snapshot_time, server_id, digest, digest_text, exec_count, avg_time_sec)
           VALUES (datetime('now','-3 days'), 'default', '0xREG', ?, 50, 0.02)""",
        (digest_text,),
    )
    conn.commit()
    conn.close()


class TestQueryRegressionNullResilience:
    def _setup(self, tmp_path, test_config, digest_text):
        db_path = tmp_path / "rules_test.db"
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        conn.commit()
        conn.close()
        _seed_regression(db_path, digest_text)
        from storage.connection import reset_connections
        reset_connections()

    def test_regression_with_null_digest_text_still_alerts(self, tmp_path, test_config):
        self._setup(tmp_path, test_config, None)
        from alerting.rules import evaluate_query_regression
        from storage.connection import reset_connections
        alert = evaluate_query_regression({"threshold": 3.0})
        assert alert is not None          # real regression must not be dropped
        assert "regression" in alert.message.lower()
        reset_connections()

    def test_regression_with_text_includes_it(self, tmp_path, test_config):
        self._setup(tmp_path, test_config, "SELECT * FROM orders WHERE id = ?")
        from alerting.rules import evaluate_query_regression
        from storage.connection import reset_connections
        alert = evaluate_query_regression({"threshold": 3.0})
        assert alert is not None
        assert "orders" in alert.message
        reset_connections()


class TestGcpMetricFreshness:
    """P1c-5: a stale GCP metrics row must not (re-)trigger high_cpu/high_memory
    and must not be surfaced as "current" by the state builder's query."""

    def _seed(self, conn, age_days, value, metric_name="cpu_utilization"):
        ts = _iso(datetime.now(timezone.utc) - timedelta(days=age_days))
        conn.execute(
            "INSERT INTO gcp_metric_snapshots (snapshot_time, server_id, metric_name, metric_type, value) "
            "VALUES (?, 'default', ?, 'gauge', ?)",
            (ts, metric_name, value),
        )
        conn.commit()

    def _conn(self, tmp_path, name):
        conn = sqlite3.connect(str(tmp_path / name))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        return conn

    def test_high_cpu_ignores_three_day_old_reading(self, tmp_path, monkeypatch):
        import alerting.rules as rules
        conn = self._conn(tmp_path, "cpu_stale.db")
        self._seed(conn, age_days=3, value=0.90)
        monkeypatch.setattr(rules, "get_mon_reader", _fake_reader(conn))

        assert rules.evaluate_high_cpu({"threshold": 0.85}, "default") is None

    def test_high_cpu_still_fires_on_fresh_reading(self, tmp_path, monkeypatch):
        import alerting.rules as rules
        conn = self._conn(tmp_path, "cpu_fresh.db")
        self._seed(conn, age_days=0, value=0.90)
        monkeypatch.setattr(rules, "get_mon_reader", _fake_reader(conn))

        alert = rules.evaluate_high_cpu({"threshold": 0.85}, "default")
        assert alert is not None

    def test_high_memory_ignores_three_day_old_reading(self, tmp_path, monkeypatch):
        import alerting.rules as rules
        conn = self._conn(tmp_path, "mem_stale.db")
        self._seed(conn, age_days=3, value=0.90, metric_name="memory_utilization")
        monkeypatch.setattr(rules, "get_mon_reader", _fake_reader(conn))

        assert rules.evaluate_high_memory({"threshold": 0.85}, "default") is None

    def test_high_memory_still_fires_on_fresh_reading(self, tmp_path, monkeypatch):
        import alerting.rules as rules
        conn = self._conn(tmp_path, "mem_fresh.db")
        self._seed(conn, age_days=0, value=0.90, metric_name="memory_utilization")
        monkeypatch.setattr(rules, "get_mon_reader", _fake_reader(conn))

        alert = rules.evaluate_high_memory({"threshold": 0.85}, "default")
        assert alert is not None

    def test_current_gcp_metrics_query_excludes_stale_row(self, tmp_path):
        """Same freshness gate applied directly to agent.queries.CURRENT_GCP_METRICS,
        which feeds the state builder's Infrastructure line."""
        from agent import queries as Q
        conn = self._conn(tmp_path, "gcpq.db")
        conn.row_factory = sqlite3.Row
        self._seed(conn, age_days=3, value=0.90)

        rows = conn.execute(Q.CURRENT_GCP_METRICS, ("default", "default")).fetchall()
        assert rows == []

    def test_current_gcp_metrics_query_includes_fresh_row(self, tmp_path):
        from agent import queries as Q
        conn = self._conn(tmp_path, "gcpq_fresh.db")
        conn.row_factory = sqlite3.Row
        self._seed(conn, age_days=0, value=0.90)

        rows = conn.execute(Q.CURRENT_GCP_METRICS, ("default", "default")).fetchall()
        assert len(rows) == 1
        assert rows[0]["value"] == 0.90


class TestThreadsRunningSpikeBaselineExclusion:
    """P1c-7: the baseline must exclude the last hour, or a sustained incident
    contaminates its own baseline (raising the average) until the alert
    self-silences."""

    def _seed(self, conn):
        # 20 hours of clean pre-incident baseline (value=10), from 22h ago
        # to 3h ago -- well within both the old and new baseline windows.
        for h in range(22, 2, -1):
            conn.execute(
                "INSERT INTO global_status_snapshots (snapshot_time, server_id, variable_name, raw_value) "
                "VALUES (?, 'default', 'Threads_running', 10)",
                (_iso(datetime.now(timezone.utc) - timedelta(hours=h)),),
            )
        # One spike sample older than 1h (80 min ago) -- leaks into the FIXED
        # baseline (-25h..-1h) since it's outside the excluded last hour.
        conn.execute(
            "INSERT INTO global_status_snapshots (snapshot_time, server_id, variable_name, raw_value) "
            "VALUES (?, 'default', 'Threads_running', 50)",
            (_iso(datetime.now(timezone.utc) - timedelta(minutes=80)),),
        )
        # Spike samples within the last hour: must be EXCLUDED from the fixed
        # baseline. The old buggy query (no upper bound) included these,
        # which is exactly the self-silencing bug.
        for m in (40, 20, 0):
            conn.execute(
                "INSERT INTO global_status_snapshots (snapshot_time, server_id, variable_name, raw_value) "
                "VALUES (?, 'default', 'Threads_running', 50)",
                (_iso(datetime.now(timezone.utc) - timedelta(minutes=m)),),
            )
        conn.commit()

    def test_still_fires_after_two_hours_of_sustained_spike(self, tmp_path, monkeypatch):
        import alerting.rules as rules
        conn = sqlite3.connect(str(tmp_path / "spike.db"))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        self._seed(conn)
        monkeypatch.setattr(rules, "get_mon_reader", _fake_reader(conn))

        alert = rules.evaluate_threads_running_spike({"multiplier": 4}, "default")
        # OLD buggy baseline (last 24h, includes all 4 spike rows + 20 clean):
        #   avg = (20*10 + 4*50)/24 = 16.67 -> current(50)/16.67 = 3.0x -> would NOT fire.
        # FIXED baseline (-25h..-1h, includes only the 80-min-old spike row):
        #   avg = (20*10 + 1*50)/21 = 11.90 -> current(50)/11.90 = 4.2x -> fires.
        assert alert is not None
        assert alert.context["current"] == 50
        assert abs(alert.context["baseline"] - 11.905) < 0.1


class TestQueryRegressionAbsoluteFloor:
    """P1c-8: an absolute floor on recent_avg must suppress ratio-only
    "regressions" that are still negligibly fast in absolute terms
    (e.g. 0.1ms -> 0.5ms is technically 5x but not worth an alert)."""

    def _setup(self, tmp_path, test_config, recent_avg, baseline_avg, digest="0xFLOOR"):
        db_path = tmp_path / "floor.db"
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        conn.execute(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, exec_count, avg_time_sec)
               VALUES (datetime('now','-5 minutes'), 'default', ?, 'SELECT 1', 100, ?)""",
            (digest, recent_avg),
        )
        conn.execute(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, exec_count, avg_time_sec)
               VALUES (datetime('now','-3 days'), 'default', ?, 'SELECT 1', 100, ?)""",
            (digest, baseline_avg),
        )
        conn.commit()
        conn.close()
        from storage.connection import reset_connections
        reset_connections()

    def test_below_floor_is_suppressed(self, tmp_path, test_config):
        # 0.0001s -> 0.0005s: a 5x ratio, but both values are negligible.
        self._setup(tmp_path, test_config, recent_avg=0.0005, baseline_avg=0.0001)
        from alerting.rules import evaluate_query_regression
        from storage.connection import reset_connections
        alert = evaluate_query_regression({"threshold": 3.0})
        assert alert is None
        reset_connections()

    def test_above_floor_still_alerts(self, tmp_path, test_config):
        # A real regression, well above the 0.01s floor.
        self._setup(tmp_path, test_config, recent_avg=0.30, baseline_avg=0.02)
        from alerting.rules import evaluate_query_regression
        from storage.connection import reset_connections
        alert = evaluate_query_regression({"threshold": 3.0})
        assert alert is not None
        reset_connections()

    def test_custom_min_recent_avg_sec_is_honored(self, tmp_path, test_config):
        # recent_avg=0.02 clears the default 0.01 floor but not a custom 0.05.
        self._setup(tmp_path, test_config, recent_avg=0.02, baseline_avg=0.002)
        from alerting.rules import evaluate_query_regression
        from storage.connection import reset_connections
        alert = evaluate_query_regression({"threshold": 3.0, "min_recent_avg_sec": 0.05})
        assert alert is None
        reset_connections()
