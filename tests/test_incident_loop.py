"""Tests for the incident -> analysis loop (P1.3 / E(a)).

Covers:
  - agent/llm_agent.py: _extract_confidence / _extract_addresses_incident
    parsing the `### Confidence` and `### Addresses incident #N` contract,
    and _parse_and_store() linking an incident when incident_id is known.
  - alerting/incidents.py: set_incident_analysis() and
    resolve_returned_to_baseline().
  - scheduler/runner.py: the agent.enabled guard around triggering +
    linking incident analyses.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent.llm_agent as la
import config as config_module
from alerting import incidents as inc
from storage.connection import reset_connections


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def incident_loop_db(tmp_path):
    """Fresh SQLite DB with schema loaded, wired as the monitoring DB."""
    db_path = tmp_path / "incident_loop_test.db"
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
        "agent": {"enabled": False},
        "gcp": {},
    }
    reset_connections()
    yield db_path
    reset_connections()


def _iso(minutes_ago: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(
        tzinfo=None
    ).isoformat()


def _insert_incident(db_path: Path, status: str = "detected", end_minutes_ago: int = 0) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO incident_windows (server_id, start_time, end_time, severity, "
        "involved_metrics, status) VALUES ('default', ?, ?, 'critical', '[\"x\"]', ?)",
        (_iso(end_minutes_ago + 5), _iso(end_minutes_ago), status),
    )
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


def _incident_row(db_path: Path, incident_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM incident_windows WHERE id = ?", (incident_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _analysis_row(db_path: Path, analysis_id: int) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM agent_analyses WHERE id = ?", (analysis_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Parser contract: ### Confidence / ### Addresses incident #N
# ---------------------------------------------------------------------------
class TestParserContract:
    def test_extract_confidence_and_incident(self):
        text = (
            "### Severity: warning\n### Findings\nx\n### Recommendations\ny\n"
            "### Confidence: 0.82 — strong evidence\n### Addresses incident #7\n"
        )
        assert abs(la._extract_confidence(text) - 0.82) < 1e-6
        assert la._extract_addresses_incident(text) == 7

    def test_extract_confidence_missing(self):
        assert la._extract_confidence("### Severity: info\nno confidence here") is None

    def test_extract_addresses_incident_missing(self):
        assert la._extract_addresses_incident("### Severity: info\nnothing here") is None

    def test_extract_confidence_boundary_values(self):
        assert la._extract_confidence("### Confidence: 0") == 0.0
        assert la._extract_confidence("### Confidence: 1") == 1.0

    def test_extract_addresses_incident_without_hash(self):
        # Regex tolerates a missing "#" before the number.
        assert la._extract_addresses_incident("### Addresses incident 42") == 42


# ---------------------------------------------------------------------------
# alerting/incidents.py: set_incident_analysis
# ---------------------------------------------------------------------------
class TestSetIncidentAnalysis:
    def test_sets_status_and_analysis_id(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected")
        conn = sqlite3.connect(str(incident_loop_db))
        aid = conn.execute(
            "INSERT INTO agent_analyses (analyzed_at, server_id, analysis_type, severity) "
            "VALUES (?, 'default', 'incident', 'warning')",
            (_iso(),),
        ).lastrowid
        conn.commit()
        conn.close()

        inc.set_incident_analysis(iid, aid, status="analyzed")

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == aid

    def test_default_status_is_analyzed(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected")
        inc.set_incident_analysis(iid, 999)
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == 999


# ---------------------------------------------------------------------------
# alerting/incidents.py: resolve_returned_to_baseline
# ---------------------------------------------------------------------------
class TestResolveReturnedToBaseline:
    def test_resolves_stale_incident_with_no_recent_events(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="analyzed", end_minutes_ago=60)
        resolved = inc.resolve_returned_to_baseline("default")
        assert iid in resolved
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "resolved"

    def test_does_not_resolve_recent_incident(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected", end_minutes_ago=1)
        resolved = inc.resolve_returned_to_baseline("default")
        assert iid not in resolved
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"

    def test_does_not_resolve_when_new_events_landed_since(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected", end_minutes_ago=60)
        conn = sqlite3.connect(str(incident_loop_db))
        conn.execute(
            "INSERT INTO anomaly_events (server_id, detected_at, metric_name, "
            "current_value, baseline_mean, baseline_stddev, z_score, pct_change, "
            "direction, severity, incident_id) VALUES "
            "('default', ?, 'threads_running', 50, 10, 2, 20, 400, 'high', 'warning', NULL)",
            (_iso(1),),
        )
        conn.commit()
        conn.close()

        resolved = inc.resolve_returned_to_baseline("default")
        assert iid not in resolved
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"

    def test_resolves_superseded_incident_when_newer_event_belongs_to_another(
        self, incident_loop_db
    ):
        """P1-8: incident 1 is stale and quiet; a newer anomaly event exists
        for the same server but is already grouped into incident 2 (a
        separate, superseding incident). That must NOT block incident 1 from
        resolving. Before the fix, the newer-event guard checked ANY newer
        event for the whole server regardless of which incident it belonged
        to, so a superseded incident like #1 here would stay
        'detected'/'analyzed' forever."""
        iid1 = _insert_incident(incident_loop_db, status="detected", end_minutes_ago=60)
        iid2 = _insert_incident(incident_loop_db, status="detected", end_minutes_ago=5)

        conn = sqlite3.connect(str(incident_loop_db))
        conn.execute(
            "INSERT INTO anomaly_events (server_id, detected_at, metric_name, "
            "current_value, baseline_mean, baseline_stddev, z_score, pct_change, "
            "direction, severity, incident_id) VALUES "
            "('default', ?, 'threads_running', 50, 10, 2, 20, 400, 'high', 'warning', ?)",
            (_iso(2), iid2),
        )
        conn.commit()
        conn.close()

        resolved = inc.resolve_returned_to_baseline("default")

        assert iid1 in resolved
        row1 = _incident_row(incident_loop_db, iid1)
        assert row1["status"] == "resolved"

    def test_leaves_already_resolved_incidents_alone(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="resolved", end_minutes_ago=60)
        resolved = inc.resolve_returned_to_baseline("default")
        assert iid not in resolved


# ---------------------------------------------------------------------------
# agent/llm_agent.py: _parse_and_store links an incident when it can
# ---------------------------------------------------------------------------
class TestParseAndStoreLinksIncident:
    def test_links_via_explicit_incident_id_and_persists_confidence(self, incident_loop_db):
        text = (
            "### Severity: warning\n"
            "### Findings\n- something happened\n"
            "### Recommendations\n- do something\n"
            "### Confidence: 0.75 — decent evidence\n"
        )
        iid = _insert_incident(incident_loop_db, status="detected")

        analysis = la._parse_and_store(text, "incident", "summary", "default", incident_id=iid)

        assert analysis["id"] is not None
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == analysis["id"]

        stored = _analysis_row(incident_loop_db, analysis["id"])
        notes = json.loads(stored["outcome_notes"])
        assert abs(notes["confidence"] - 0.75) < 1e-6

    def test_links_via_addresses_incident_header(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected")
        text = (
            "### Severity: critical\n"
            "### Findings\n- x\n"
            "### Recommendations\n- y\n"
            f"### Addresses incident #{iid}\n"
        )

        analysis = la._parse_and_store(text, "incident", "summary", "default")

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == analysis["id"]

    def test_no_incident_reference_does_not_touch_incident_windows(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected")
        text = "### Severity: info\n### Findings\nnone\n### Recommendations\nnone\n"

        la._parse_and_store(text, "routine", "summary", "default")

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"
        assert row["analysis_id"] is None


# ---------------------------------------------------------------------------
# scheduler/runner.py: guard around triggering + linking incident analyses
# ---------------------------------------------------------------------------
class TestSchedulerIncidentWiring:
    def test_triggers_and_links_when_agent_enabled(self, incident_loop_db, monkeypatch):
        config_module._config["agent"] = {"enabled": True}
        iid = _insert_incident(incident_loop_db, status="detected")

        calls = []

        def fake_run_analysis(analysis_type, server_id=None, incident_id=None, **kw):
            calls.append((analysis_type, server_id, incident_id))
            conn = sqlite3.connect(str(incident_loop_db))
            aid = conn.execute(
                "INSERT INTO agent_analyses (analyzed_at, server_id, analysis_type, severity) "
                "VALUES (?, 'default', 'incident', 'warning')",
                (_iso(),),
            ).lastrowid
            conn.commit()
            conn.close()
            inc.set_incident_analysis(incident_id, aid, status="analyzed")
            return {"id": aid}

        monkeypatch.setattr("agent.llm_agent.run_analysis", fake_run_analysis)

        from scheduler import runner
        runner._trigger_incident_analyses("default", [iid])

        assert calls == [("incident", "default", iid)]
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] is not None

    def test_noop_when_agent_disabled(self, incident_loop_db, monkeypatch):
        config_module._config["agent"] = {"enabled": False}
        iid = _insert_incident(incident_loop_db, status="detected")

        called = []
        monkeypatch.setattr(
            "agent.llm_agent.run_analysis",
            lambda *a, **kw: called.append(1),
        )

        from scheduler import runner
        runner._trigger_incident_analyses("default", [iid])

        assert called == []
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"

    def test_exception_in_run_analysis_never_propagates(self, incident_loop_db, monkeypatch):
        config_module._config["agent"] = {"enabled": True}
        iid = _insert_incident(incident_loop_db, status="detected")

        def boom(*a, **kw):
            raise RuntimeError("no LLM key configured")

        monkeypatch.setattr("agent.llm_agent.run_analysis", boom)

        from scheduler import runner
        # Must not raise.
        runner._trigger_incident_analyses("default", [iid])

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"
