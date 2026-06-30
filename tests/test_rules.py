"""Tests for alerting/rules.py — focused on NULL-resilience.

`evaluate_query_regression` sliced `digest_text[:60]` without a guard; a top
regression with a NULL digest_text raised TypeError, which the engine swallowed
and silently dropped a real regression alert.
"""

import sqlite3
from pathlib import Path

import config as config_module

SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


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
