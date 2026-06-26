"""
Tests for alerting/phase3.py — continuous sampling, load guard, budget,
clearance detection, scheduler restart sweep.

Strategy: seed investigations + inbound_alerts + signal tables in SQLite,
then call phase3_sample(id) directly. Mock `get_prod_connection` so no
real MySQL is touched. The scheduler side-effect is covered by observing
the SQLite state transitions — we don't exercise APScheduler here.
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

import config as config_module
from storage.connection import reset_connections
from storage import writer

from alerting import phase3 as P3


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def mon_db_ctx(mon_db):
    _, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "investigator": {
            "phase3_sampling_interval_seconds": 20,
            "phase3_max_duration_seconds": 480,
            "query_budget_per_minute": 20,
            "load_guard_threads_running_threshold": 40,
            "load_guard_pause_seconds": 60,
            "clearance": {
                "lock_waits": 1,
                "max_wait_seconds": 5,
                "cpu_pct": 0.75,
                "rows_examined_ratio": 100,
            },
        },
    }
    reset_connections()
    yield mon_db
    config_module._config = prev
    reset_connections()


def _seed_investigation(
    alert_type="lock_cascade",
    severity="critical",
    server_id="srv1",
    status="phase3",
    started_ago_minutes: int = 1,
) -> tuple[int, int]:
    started_at = _iso(_now() - timedelta(minutes=started_ago_minutes))
    alert_id = writer.write_inbound_alert({
        "provider": "generic",
        "received_at": started_at,
        "server_id": server_id,
        "external_id": f"ext-{alert_type}",
        "alert_type": alert_type,
        "severity": severity,
        "summary": f"test {alert_type}",
        "payload": "{}",
        "signature_verified": 1,
    })
    inv_id = writer.write_investigation({
        "inbound_alert_id": alert_id,
        "server_id": server_id,
        "started_at": started_at,
        "status": status,
    })
    return alert_id, inv_id


def _mock_prod_connection(rows_by_sql_fragment: dict[str, list[dict]]):
    """
    Build a context-manager mock for get_prod_connection that dispatches
    based on a SQL substring match.
    """
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    last_sql = {"value": ""}

    def exec_side(sql, params=None):
        last_sql["value"] = sql
        return None

    def fetch_side():
        sql = last_sql["value"]
        for fragment, rows in rows_by_sql_fragment.items():
            if fragment in sql:
                return rows
        return []

    mock_cursor.execute.side_effect = exec_side
    mock_cursor.fetchall.side_effect = fetch_side
    mock_conn.cursor.return_value = mock_cursor

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _seed_threads_running(conn, server_id, value):
    conn.execute(
        "INSERT INTO global_status_snapshots (server_id, snapshot_time, variable_name, raw_value) "
        "VALUES (?,?,?,?)",
        (server_id, _iso(_now()), "Threads_running", value),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Hard timeout
# ---------------------------------------------------------------------------

class TestHardTimeout:
    def test_over_max_duration_terminates(self, mon_db_ctx):
        _, inv_id = _seed_investigation(started_ago_minutes=120)
        with patch("alerting.phase3.get_prod_connection"):
            result = P3.phase3_sample(inv_id)
        assert result["status"] == "completed"
        assert result["reason"] == "max_duration"
        conn, _ = mon_db_ctx
        row = conn.execute(
            "SELECT status, ended_at FROM investigations WHERE id = ?", (inv_id,)
        ).fetchone()
        assert row["status"] == "completed"
        assert row["ended_at"] is not None


# ---------------------------------------------------------------------------
# Load guard
# ---------------------------------------------------------------------------

class TestLoadGuard:
    def test_high_threads_running_pauses(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _, inv_id = _seed_investigation()
        _seed_threads_running(conn, "srv1", 99)
        with patch("alerting.phase3.get_prod_connection"):
            result = P3.phase3_sample(inv_id)
        assert result["status"] == "load_guard_paused"
        assert result["threads_running"] == 99

        row = conn.execute(
            "SELECT status FROM investigations WHERE id = ?", (inv_id,)
        ).fetchone()
        assert row["status"] == "load_guard_paused"

    def test_low_threads_running_does_not_pause(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _, inv_id = _seed_investigation()
        _seed_threads_running(conn, "srv1", 5)

        with patch(
            "alerting.phase3.get_prod_connection",
            return_value=_mock_prod_connection({
                "performance_schema.threads": [],
                "data_lock_waits": [{"lock_count": 0, "max_wait_seconds": 0}],
            }),
        ):
            result = P3.phase3_sample(inv_id)
        assert result["status"] in ("sampled", "completed")


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

class TestBudget:
    def test_over_budget_skips(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _, inv_id = _seed_investigation()
        # Pre-fill rolling window past the budget.
        now_ts = _iso(_now())
        conn.execute(
            "INSERT INTO investigation_samples (investigation_id, sampled_at, sample_type, query_count, data) "
            "VALUES (?,?,?,?,?)",
            (inv_id, now_ts, "processlist", 25, "[]"),
        )
        conn.commit()
        with patch("alerting.phase3.get_prod_connection") as mock_conn:
            result = P3.phase3_sample(inv_id)
            mock_conn.assert_not_called()
        assert result["status"] == "budget_skipped"


# ---------------------------------------------------------------------------
# Sampling + clearance for lock_cascade
# ---------------------------------------------------------------------------

class TestLockCascadeClearance:
    def test_low_locks_completes(self, mon_db_ctx):
        _, inv_id = _seed_investigation(alert_type="lock_cascade")

        with patch(
            "alerting.phase3.get_prod_connection",
            return_value=_mock_prod_connection({
                "performance_schema.threads": [],
                "data_lock_waits": [{"lock_count": 0, "max_wait_seconds": 0}],
            }),
        ):
            result = P3.phase3_sample(inv_id)

        assert result["status"] == "completed"
        assert result["reason"] == "cleared"

    def test_still_locked_reschedules(self, mon_db_ctx):
        _, inv_id = _seed_investigation(alert_type="lock_cascade")
        with patch(
            "alerting.phase3.get_prod_connection",
            return_value=_mock_prod_connection({
                "performance_schema.threads": [{"pid": 1, "user": "app",
                    "db": "mydb", "command": "Query", "state": "updating",
                    "time_sec": 30, "query": "UPDATE members SET ..."}],
                "data_lock_waits": [{"lock_count": 5, "max_wait_seconds": 30}],
            }),
        ):
            result = P3.phase3_sample(inv_id)

        assert result["status"] == "sampled"

        conn, _ = mon_db_ctx
        samples = conn.execute(
            "SELECT sample_type, query_count FROM investigation_samples WHERE investigation_id = ?",
            (inv_id,),
        ).fetchall()
        types = {s["sample_type"] for s in samples}
        assert "processlist" in types
        assert "locks" in types


# ---------------------------------------------------------------------------
# Missing-index clearance
# ---------------------------------------------------------------------------

class TestMissingIndexClearance:
    def _seed_phase1_correlation(self, conn, inv_id, digests):
        content = json.dumps({
            "suspect_digests": list(digests),
            "evidence": [],
            "server_id": "srv1",
            "window_start": _iso(_now() - timedelta(minutes=5)),
            "window_end": _iso(_now()),
            "has_findings": True,
        })
        conn.execute(
            "INSERT INTO investigation_findings (investigation_id, created_at, phase, kind, severity, content) "
            "VALUES (?,?,?,?,?,?)",
            (inv_id, _iso(_now()), 1, "correlation", "warning", content),
        )
        conn.commit()

    def _seed_digest(self, conn, digest, server_id, rows_examined, rows_sent):
        conn.execute(
            """INSERT INTO query_digest_snapshots
               (server_id, snapshot_time, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
                rows_examined, rows_sent, rows_affected)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                server_id, _iso(_now()), digest,
                f"SELECT * FROM members WHERE foo = ?", "testdb",
                10, 1.0, 0.1, 0.5, 0.01,
                rows_examined, rows_sent, 0,
            ),
        )
        conn.commit()

    def test_ratio_back_to_normal_clears(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _, inv_id = _seed_investigation(alert_type="missing_index")
        self._seed_phase1_correlation(conn, inv_id, ["0xFIXED"])
        self._seed_digest(conn, "0xFIXED", "srv1", rows_examined=10, rows_sent=10)

        with patch(
            "alerting.phase3.get_prod_connection",
            return_value=_mock_prod_connection({
                "performance_schema.threads": [],
                "data_lock_waits": [{"lock_count": 0, "max_wait_seconds": 0}],
                "events_statements_summary_by_digest": [
                    {"digest": "0xFIXED", "rows_examined": 10, "rows_sent": 10,
                     "exec_count": 1, "total_time_sec": 0.1},
                ],
            }),
        ):
            result = P3.phase3_sample(inv_id)
        assert result["status"] == "completed"
        assert result["reason"] == "cleared"

    def test_ratio_still_bad_reschedules(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _, inv_id = _seed_investigation(alert_type="missing_index")
        self._seed_phase1_correlation(conn, inv_id, ["0xSTILL"])
        self._seed_digest(conn, "0xSTILL", "srv1", rows_examined=1_000_000, rows_sent=10)

        with patch(
            "alerting.phase3.get_prod_connection",
            return_value=_mock_prod_connection({
                "performance_schema.threads": [],
                "data_lock_waits": [{"lock_count": 0, "max_wait_seconds": 0}],
                "events_statements_summary_by_digest": [
                    {"digest": "0xSTILL", "rows_examined": 1_000_000, "rows_sent": 10,
                     "exec_count": 1, "total_time_sec": 1.0},
                ],
            }),
        ):
            result = P3.phase3_sample(inv_id)
        assert result["status"] == "sampled"  # not cleared

        # Sample set should include an index_delta row.
        types = {
            r["sample_type"] for r in conn.execute(
                "SELECT sample_type FROM investigation_samples WHERE investigation_id = ?",
                (inv_id,),
            ).fetchall()
        }
        assert "index_delta" in types


# ---------------------------------------------------------------------------
# Terminal short-circuit
# ---------------------------------------------------------------------------

class TestTerminalShortCircuit:
    def test_already_completed_skips(self, mon_db_ctx):
        _, inv_id = _seed_investigation(status="completed")
        with patch("alerting.phase3.get_prod_connection") as mock_conn:
            result = P3.phase3_sample(inv_id)
            mock_conn.assert_not_called()
        assert result == {"status": "completed"}


# ---------------------------------------------------------------------------
# Startup sweep
# ---------------------------------------------------------------------------

class TestSweep:
    def test_stale_non_terminal_rows_aborted(self, mon_db_ctx):
        _, fresh = _seed_investigation(started_ago_minutes=1, status="phase1")
        _, stale1 = _seed_investigation(started_ago_minutes=30, status="phase2",
                                         alert_type="missing_index")
        _, stale2 = _seed_investigation(started_ago_minutes=30, status="phase3",
                                         alert_type="high_cpu")
        _, completed = _seed_investigation(started_ago_minutes=30, status="completed",
                                            alert_type="deadlock_detected")

        aborted = P3.sweep_stale_investigations(max_age_minutes=10)
        assert aborted == 2

        conn, _ = mon_db_ctx
        rows = {
            r["id"]: r["status"]
            for r in conn.execute(
                "SELECT id, status FROM investigations"
            ).fetchall()
        }
        assert rows[fresh] == "phase1"          # too recent
        assert rows[stale1] == "aborted"
        assert rows[stale2] == "aborted"
        assert rows[completed] == "completed"   # was already terminal
