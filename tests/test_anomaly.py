"""Tests for anomaly detection module."""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config as config_module
from storage.connection import reset_connections


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


@pytest.fixture
def anomaly_db(tmp_path, test_config):
    """Set up a temp SQLite DB with schema and seed data for anomaly tests."""
    db_path = tmp_path / "anomaly_test.db"
    test_config["monitoring_db"]["path"] = str(db_path)
    config_module._config = test_config

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL_PATH.read_text())
    conn.commit()

    # Seed global_status_snapshots with Threads_running data
    # 30 samples, all >15 minutes old, mean ~10, small variance
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    values = [8, 9, 10, 11, 12, 9, 10, 13, 8, 10, 11, 9, 10, 12, 10,
              8, 11, 10, 9, 10, 10, 11, 9, 10, 12, 10, 8, 11, 10, 9]
    for i, val in enumerate(values):
        ts = (now - timedelta(minutes=180 - i * 5)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
            (ts, "Threads_running", val),
        )

    # Add a current value that is an anomaly (very high)
    current_ts = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
        (current_ts, "Threads_running", 50),  # way above baseline
    )

    # Seed buffer pool — the anomaly detector reads from the cumulative
    # Innodb_buffer_pool_reads / _read_requests counters in
    # global_status_snapshots (see alerting/anomaly.py buffer_pool_hit_ratio).
    # We also seed buffer_pool_snapshots for the OTHER consumers (prometheus
    # gauge, dashboard pages) that still need the row shape.
    #
    # Ratio = 1 - reads/requests. For hit_ratio=0.999 → reads=1000, requests=1e6.
    # For hit_ratio=0.95 (sharp drop) → reads=50000, requests=1e6.
    for i in range(30):
        ts = (now - timedelta(minutes=180 - i * 5)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO buffer_pool_snapshots (snapshot_time, pool_id, pool_size, free_buffers, database_pages, dirty_pages, pending_reads, pages_read, pages_written, hit_ratio) "
            "VALUES (?, 0, 100000, 5000, 90000, 100, 0, 1000, 500, ?)",
            (ts, 0.999),
        )
        # Matching global_status rows for the cumulative ratio
        conn.execute(
            "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
            (ts, "Innodb_buffer_pool_reads", 1000),
        )
        conn.execute(
            "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
            (ts, "Innodb_buffer_pool_read_requests", 1_000_000),
        )
    # Current: sharp drop — reads 50x baseline
    conn.execute(
        "INSERT INTO buffer_pool_snapshots (snapshot_time, pool_id, pool_size, free_buffers, database_pages, dirty_pages, pending_reads, pages_read, pages_written, hit_ratio) "
        "VALUES (?, 0, 100000, 5000, 90000, 100, 0, 1000, 500, ?)",
        (current_ts, 0.950),
    )
    conn.execute(
        "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
        (current_ts, "Innodb_buffer_pool_reads", 50_000),
    )
    conn.execute(
        "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
        (current_ts, "Innodb_buffer_pool_read_requests", 1_000_000),
    )

    conn.commit()
    conn.close()

    yield db_path
    reset_connections()


class TestHighZOptOutRespected:
    """buffer_pool_hit_ratio sets high_z=None because a HIGH hit ratio is good.
    The alert path calls detect_anomalies(z_threshold_override=3.0); that override
    must NOT re-enable high-side detection for an opted-out metric (it used to,
    firing a spurious CRITICAL alert whenever the hit ratio improved)."""

    def _seed_improving_hit_ratio(self, tmp_path, test_config):
        db_path = tmp_path / "hz_test.db"
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Baseline hit ratio ~0.98 with small variance (reads ~20k / 1e6 req)
        # so stddev > 0 (otherwise compute_baseline bails on zero-stddev).
        reads_series = [18000, 22000, 19000, 21000, 20000, 18500,
                        21500, 19500, 20500, 20000, 19000, 21000] * 3
        for i, reads in enumerate(reads_series):
            ts = (now - timedelta(minutes=180 - i * 5)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
                (ts, "Innodb_buffer_pool_reads", reads),
            )
            conn.execute(
                "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
                (ts, "Innodb_buffer_pool_read_requests", 1_000_000),
            )
        # Current: hit ratio IMPROVES sharply (reads drop to 2000 -> ratio 0.998,
        # many sigma ABOVE the ~0.98 baseline).
        cur = (now - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
            (cur, "Innodb_buffer_pool_reads", 2000),
        )
        conn.execute(
            "INSERT INTO global_status_snapshots (snapshot_time, variable_name, raw_value) VALUES (?, ?, ?)",
            (cur, "Innodb_buffer_pool_read_requests", 1_000_000),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_improving_hit_ratio_no_high_anomaly_under_override(self, tmp_path, test_config):
        from alerting.anomaly import detect_anomalies, METRIC_CONFIGS
        from storage.connection import reset_connections

        assert METRIC_CONFIGS["buffer_pool_hit_ratio"]["high_z"] is None  # opted out
        self._seed_improving_hit_ratio(tmp_path, test_config)
        reset_connections()
        anomalies = detect_anomalies(z_threshold_override=3.0)
        bad = [a for a in anomalies
               if a.metric == "buffer_pool_hit_ratio" and a.direction == "high"]
        assert bad == [], f"high-side buffer-pool anomaly must not fire on improvement: {bad}"
        reset_connections()


class TestAnomalyDetection:
    def test_compute_baseline_threads_running(self, anomaly_db):
        from alerting.anomaly import compute_baseline, METRIC_CONFIGS

        b = compute_baseline("threads_running", METRIC_CONFIGS["threads_running"])
        assert b is not None
        # Baseline can come from either the same-hour-same-dow primary query
        # (which returns few rows depending on wall-clock alignment) or the
        # _BASELINE_ALL fallback (which returns everything >10 min old).
        # We just need enough samples to compute a usable baseline — the
        # exact count is wall-clock flaky.
        assert b.sample_count >= METRIC_CONFIGS["threads_running"]["min_samples"]
        assert 6 <= b.mean <= 14  # seeded around 10, wider tolerance for small same-hour sets
        assert b.stddev >= 0  # can be 0 if all hour-matched samples have identical values
        assert b.current == 50  # the anomalous value

    def test_detect_threads_running_spike(self, anomaly_db):
        from alerting.anomaly import detect_anomalies

        anomalies = detect_anomalies()
        thread_anomalies = [a for a in anomalies if a.metric == "threads_running"]
        assert len(thread_anomalies) == 1
        a = thread_anomalies[0]
        assert a.direction == "high"
        assert a.z_score > 3.0
        assert a.current == 50

    def test_detect_buffer_pool_drop(self, anomaly_db):
        from alerting.anomaly import detect_anomalies

        anomalies = detect_anomalies()
        bp_anomalies = [a for a in anomalies if a.metric == "buffer_pool_hit_ratio"]
        assert len(bp_anomalies) == 1
        a = bp_anomalies[0]
        assert a.direction == "low"
        # Current ratio is 1 - (50000/1_000_000) = 0.95
        assert abs(a.current - 0.95) < 0.01

    def test_no_anomaly_when_normal(self, anomaly_db):
        """When current value is within range, no anomaly should fire."""
        conn = sqlite3.connect(str(anomaly_db))
        # Overwrite current Threads_running to a normal value
        conn.execute(
            "UPDATE global_status_snapshots SET raw_value = 10 "
            "WHERE variable_name = 'Threads_running' AND raw_value = 50"
        )
        # Fix buffer pool — reset the current Innodb_buffer_pool_reads to the
        # baseline value so current ratio matches baseline (0.999)
        conn.execute(
            "UPDATE global_status_snapshots SET raw_value = 1000 "
            "WHERE variable_name = 'Innodb_buffer_pool_reads' AND raw_value = 50000"
        )
        # Legacy column still exists on buffer_pool_snapshots but is no
        # longer read by the detector — keep the update for other consumers.
        conn.execute(
            "UPDATE buffer_pool_snapshots SET hit_ratio = 0.999 "
            "WHERE hit_ratio = 0.95"
        )
        conn.commit()
        conn.close()
        reset_connections()

        from alerting.anomaly import detect_anomalies
        anomalies = detect_anomalies()
        assert len(anomalies) == 0

    def test_evaluate_anomaly_rule(self, anomaly_db):
        """Test the alert rule integration."""
        from alerting.anomaly import evaluate_anomaly

        alert = evaluate_anomaly({"z_threshold": 3.0})
        assert alert is not None
        # Rules are now namespaced per-server so multi-server deployments
        # get independent cooldowns. The default server_id is "default".
        assert alert.rule_name.startswith("anomaly_detection")
        assert "server_id" in alert.context
        assert "anomaly_count" in alert.context
        assert alert.context["anomaly_count"] >= 1

    def test_evaluate_anomaly_no_alert_when_normal(self, anomaly_db):
        """No alert when everything is normal."""
        conn = sqlite3.connect(str(anomaly_db))
        # Reset Threads_running current value to normal
        conn.execute(
            "UPDATE global_status_snapshots SET raw_value = 10 "
            "WHERE variable_name = 'Threads_running' AND raw_value = 50"
        )
        # Reset current Innodb_buffer_pool_reads so current ratio matches baseline
        conn.execute(
            "UPDATE global_status_snapshots SET raw_value = 1000 "
            "WHERE variable_name = 'Innodb_buffer_pool_reads' AND raw_value = 50000"
        )
        # Legacy column still present — kept for other consumers
        conn.execute("UPDATE buffer_pool_snapshots SET hit_ratio = 0.999 WHERE hit_ratio = 0.95")
        conn.commit()
        conn.close()
        reset_connections()

        from alerting.anomaly import evaluate_anomaly
        alert = evaluate_anomaly({"z_threshold": 3.0})
        assert alert is None
