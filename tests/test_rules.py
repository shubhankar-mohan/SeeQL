"""Tests for alerting/rules.py — focused on NULL-resilience.

`evaluate_query_regression` sliced `digest_text[:60]` without a guard; a top
regression with a NULL digest_text raised TypeError, which the engine swallowed
and silently dropped a real regression alert.
"""

import json
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


class TestDeadlockDedup:
    """P1b-3: InnoDB reprints the same LATEST DETECTED DEADLOCK section on
    every SHOW ENGINE INNODB STATUS call until the server restarts.
    evaluate_deadlock used to filter only on snapshot freshness (`bool(text)`
    via has_deadlock), so a single weeks-old deadlock re-fired a critical
    alert every evaluation cycle forever. It must key off the deadlock's own
    header timestamp (deadlock_at) and only fire when that is newer than the
    last deadlock_at we already alerted on (read from alert_history, so a
    process restart doesn't re-alert an old deadlock).
    """

    def _setup(self, tmp_path, test_config):
        db_path = tmp_path / "rules_test.db"
        test_config["monitoring_db"]["path"] = str(db_path)
        config_module._config = test_config
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        conn.commit()
        conn.close()
        from storage.connection import reset_connections
        reset_connections()
        return db_path

    def _seed_deadlock(self, db_path, deadlock_at, now_offset_sql, tables=None):
        """Insert a LATEST DETECTED DEADLOCK snapshot whose parsed_json carries
        `deadlock_at`. `now_offset_sql` is an SQLite datetime() modifier (e.g.
        '-3 minutes') so snapshot_time is always fresh/within the rule's
        10-minute window — mirroring InnoDB reprinting an old deadlock under a
        brand-new snapshot_time every collection cycle."""
        parsed_json = json.dumps({
            "has_deadlock": True,
            "deadlock_at": deadlock_at,
            "transaction_count": 2,
            "tables_involved": tables or ["mydb.orders"],
        })
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """INSERT INTO innodb_status_snapshots
               (snapshot_time, server_id, section_name, section_data, parsed_json)
               VALUES (datetime('now', ?), 'default', 'LATEST DETECTED DEADLOCK', ?, ?)""",
            (now_offset_sql, f"{deadlock_at} 0x1\n*** (1) TRANSACTION:", parsed_json),
        )
        conn.commit()
        conn.close()

    def test_deadlock_rule_dedups_same_deadlock(self, tmp_path, test_config):
        db_path = self._setup(tmp_path, test_config)
        from alerting.engine import _store_alert
        from alerting.rules import evaluate_deadlock
        from storage.connection import reset_connections

        # First sighting of this deadlock -> must fire.
        self._seed_deadlock(db_path, "2026-07-17 01:42:33", "-3 minutes")
        alert = evaluate_deadlock({}, "default")
        assert alert is not None
        assert alert.context["deadlock_at"] == "2026-07-17 01:42:33"

        # Persist it exactly the way alerting/engine.evaluate() does after a
        # rule fires (channels + delivered get set before _store_alert()).
        alert.channels = ["log"]
        alert.delivered = True
        _store_alert(alert)

        check = sqlite3.connect(str(db_path))
        check.row_factory = sqlite3.Row
        row = check.execute(
            "SELECT context_json FROM alert_history WHERE rule_name = ?",
            ("deadlock_detected:default",),
        ).fetchone()
        check.close()
        assert row is not None
        assert json.loads(row["context_json"])["deadlock_at"] == "2026-07-17 01:42:33"

        # InnoDB reprints the SAME deadlock under a fresh snapshot_time on the
        # next collection cycle -> must NOT re-fire.
        self._seed_deadlock(db_path, "2026-07-17 01:42:33", "-1 minutes")
        assert evaluate_deadlock({}, "default") is None

        # A genuinely NEW deadlock (later deadlock_at) must fire again.
        self._seed_deadlock(db_path, "2026-07-17 01:50:00", "+0 seconds")
        alert2 = evaluate_deadlock({}, "default")
        assert alert2 is not None
        assert alert2.context["deadlock_at"] == "2026-07-17 01:50:00"

        reset_connections()
