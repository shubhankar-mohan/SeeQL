"""Tests for alerting/anomaly_store.py and alerting/incidents.py (Phase 1.3 + 1.4)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import config as config_module
from alerting.anomaly import AnomalyResult
from alerting.anomaly_store import persist
from alerting.incidents import update_windows
from storage.connection import reset_connections


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def incident_db(tmp_path):
    """Fresh SQLite DB with schema loaded, wired as the monitoring DB."""
    db_path = tmp_path / "incident_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL_PATH.read_text())
    conn.commit()
    conn.close()

    config_module._config = {
        "monitoring_db": {
            "path": str(db_path),
            "wal_mode": False,
            "busy_timeout_ms": 5000,
        },
        "alerting": {
            "incident_gap_minutes": 15,
            "incident_max_duration_minutes": 120,
            "channels": {"slack": {"enabled": False}},
        },
    }
    reset_connections()
    yield db_path
    reset_connections()


def _make_result(minutes_ago: int, metric: str, severity: str = "warning",
                 server_id: str = "default") -> AnomalyResult:
    """Build a synthetic AnomalyResult detected `minutes_ago`."""
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return AnomalyResult(
        metric=metric,
        current=50.0,
        baseline_mean=10.0,
        baseline_stddev=2.0,
        z_score=20.0 if severity == "critical" else 3.5,
        pct_change=400.0,
        direction="high",
        severity=severity,
        server_id=server_id,
        detected_at=ts.isoformat(),
    )


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _incidents(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM incident_windows ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# anomaly_store.persist
# ---------------------------------------------------------------------------
class TestAnomalyStore:
    def test_persist_empty(self, incident_db):
        ids = persist([])
        assert ids == []
        assert _count(incident_db, "anomaly_events") == 0

    def test_persist_single(self, incident_db):
        r = _make_result(minutes_ago=5, metric="threads_running")
        ids = persist([r])
        assert len(ids) == 1
        assert _count(incident_db, "anomaly_events") == 1

        conn = sqlite3.connect(str(incident_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM anomaly_events WHERE id = ?", (ids[0],)
        ).fetchone()
        conn.close()
        assert row["metric_name"] == "threads_running"
        assert row["server_id"] == "default"
        assert row["incident_id"] is None  # not grouped yet

    def test_persist_multiple(self, incident_db):
        results = [
            _make_result(10, "threads_running"),
            _make_result(8, "lock_frequency"),
            _make_result(5, "cpu_utilization"),
        ]
        ids = persist(results)
        assert len(ids) == 3
        assert len(set(ids)) == 3  # distinct ids
        assert _count(incident_db, "anomaly_events") == 3


# ---------------------------------------------------------------------------
# incidents.update_windows — gap-based clustering
# ---------------------------------------------------------------------------
class TestIncidentWindowing:

    def test_empty_returns_nothing(self, incident_db):
        new_ids = update_windows("default")
        assert new_ids == []
        assert _count(incident_db, "incident_windows") == 0

    def test_single_event_creates_one_incident(self, incident_db):
        persist([_make_result(5, "threads_running", "critical")])
        new_ids = update_windows("default")
        assert len(new_ids) == 1

        incidents = _incidents(incident_db)
        assert len(incidents) == 1
        assert incidents[0]["event_count"] == 1
        assert incidents[0]["severity"] == "critical"
        assert json.loads(incidents[0]["involved_metrics"]) == ["threads_running"]

        # The event should now be tagged with the incident_id
        conn = sqlite3.connect(str(incident_db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT incident_id FROM anomaly_events").fetchone()
        conn.close()
        assert row["incident_id"] == new_ids[0]

    def test_events_within_gap_merge(self, incident_db):
        """Events within 15 minutes should cluster into one incident."""
        persist([
            _make_result(20, "threads_running"),
            _make_result(12, "lock_frequency"),  # 8 min later
            _make_result(5, "cpu_utilization"),  # 7 min later
        ])
        new_ids = update_windows("default")
        # Exactly one new incident even with three events
        assert len(new_ids) == 1

        incidents = _incidents(incident_db)
        assert len(incidents) == 1
        assert incidents[0]["event_count"] == 3
        metrics = json.loads(incidents[0]["involved_metrics"])
        assert set(metrics) == {"threads_running", "lock_frequency", "cpu_utilization"}

    def test_events_outside_gap_split(self, incident_db):
        """Events more than 15 minutes apart become separate incidents."""
        persist([
            _make_result(60, "threads_running"),
            _make_result(5, "lock_frequency"),  # 55 min later — way beyond gap
        ])
        new_ids = update_windows("default")
        assert len(new_ids) == 2
        assert _count(incident_db, "incident_windows") == 2

    def test_severity_upgrades(self, incident_db):
        """A warning followed by a critical should upgrade the incident."""
        persist([_make_result(10, "threads_running", "warning")])
        update_windows("default")

        persist([_make_result(2, "lock_frequency", "critical")])
        update_windows("default")

        incidents = _incidents(incident_db)
        assert len(incidents) == 1
        assert incidents[0]["severity"] == "critical"
        assert incidents[0]["event_count"] == 2

    def test_multi_server_isolation(self, incident_db):
        """Events on different servers get independent incidents."""
        persist([
            _make_result(5, "threads_running", server_id="A"),
            _make_result(5, "threads_running", server_id="B"),
        ])

        new_a = update_windows("A")
        new_b = update_windows("B")

        assert len(new_a) == 1
        assert len(new_b) == 1
        assert new_a[0] != new_b[0]

        incidents = _incidents(incident_db)
        assert len(incidents) == 2
        sids = {i["server_id"] for i in incidents}
        assert sids == {"A", "B"}

    def test_new_ids_only_for_new_incidents(self, incident_db):
        """Extending an existing incident should NOT report it as new."""
        persist([_make_result(20, "threads_running")])
        new_first = update_windows("default")
        assert len(new_first) == 1

        # Add another event within the gap — extends the existing incident
        persist([_make_result(10, "lock_frequency")])
        new_second = update_windows("default")
        assert new_second == []  # No new incidents created

        incidents = _incidents(incident_db)
        assert len(incidents) == 1
        assert incidents[0]["event_count"] == 2

    def test_metric_dedup(self, incident_db):
        """Duplicate metrics in involved_metrics should collapse."""
        persist([
            _make_result(10, "threads_running"),
            _make_result(5, "threads_running"),  # same metric, different event
        ])
        update_windows("default")
        incidents = _incidents(incident_db)
        assert json.loads(incidents[0]["involved_metrics"]) == ["threads_running"]
        assert incidents[0]["event_count"] == 2

    def test_idempotent(self, incident_db):
        """Running update_windows twice in a row shouldn't double-group."""
        persist([_make_result(5, "threads_running")])
        update_windows("default")
        incidents_before = _incidents(incident_db)

        new_ids = update_windows("default")  # second call, no new events
        assert new_ids == []

        incidents_after = _incidents(incident_db)
        assert len(incidents_before) == len(incidents_after)
        assert incidents_before[0]["event_count"] == incidents_after[0]["event_count"]
