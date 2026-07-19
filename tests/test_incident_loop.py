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
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent.llm_agent as la
import config as config_module
from alerting import incidents as inc
from storage import writer
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


def _insert_incident(
    db_path: Path, status: str = "detected", end_minutes_ago: int = 0,
    server_id: str = "default",
) -> int:
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO incident_windows (server_id, start_time, end_time, severity, "
        "involved_metrics, status) VALUES (?, ?, ?, 'critical', '[\"x\"]', ?)",
        (server_id, _iso(end_minutes_ago + 5), _iso(end_minutes_ago), status),
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


def _analysis_dict(**overrides) -> dict:
    """Minimal valid row for storage.writer.write_agent_analysis_and_link()."""
    base = {
        "analyzed_at": _iso(),
        "server_id": "default",
        "analysis_type": "incident",
        "severity": "warning",
        "input_summary": "s",
        "findings": "[]",
        "recommendations": "[]",
        "applied": 0,
        "outcome_notes": None,
    }
    base.update(overrides)
    return base


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
# alerting/incidents.py: set_incident_analysis guards (P1-4)
#
# The old implementation was a blind `UPDATE ... WHERE id = ?` — no status
# guard, no server scope. A hallucinated self-report could flip an arbitrary
# incident to "analyzed", and re-linking a resolved incident (e.g. a replay
# post-mortem) would resurrect it. Both are closed by scoping the UPDATE to
# `status IN ('detected', 'analyzed')` (+ server_id when given) and
# reporting back whether a row actually changed.
# ---------------------------------------------------------------------------
class TestSetIncidentAnalysisGuarded:
    def test_does_not_link_across_servers(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected", server_id="b")
        linked = inc.set_incident_analysis(iid, 999, server_id="a")
        assert linked is False
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"
        assert row["analysis_id"] is None

    def test_does_not_resurrect_resolved_incident(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="resolved", server_id="a")
        linked = inc.set_incident_analysis(iid, 999, server_id="a")
        assert linked is False
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "resolved"
        assert row["analysis_id"] is None

    def test_links_when_server_matches_and_incident_open(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected", server_id="a")
        linked = inc.set_incident_analysis(iid, 999, server_id="a")
        assert linked is True
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == 999

    def test_no_server_filter_when_server_id_omitted(self, incident_loop_db):
        """Backward-compatible: server_id=None keeps the old any-server
        behavior for callers that don't (yet) track server scoping."""
        iid = _insert_incident(incident_loop_db, status="detected", server_id="b")
        linked = inc.set_incident_analysis(iid, 999)
        assert linked is True
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"


# ---------------------------------------------------------------------------
# storage/writer.py: write_agent_analysis_and_link (P1-21)
#
# Store-then-link used to be two separate get_mon_connection() transactions.
# A concurrent resolve_returned_to_baseline() sweep could run between them and
# resolve the incident; the old blind UPDATE in the (now-separate) link step
# would then resurrect it. Doing insert + guarded UPDATE inside ONE
# transaction closes that window.
# ---------------------------------------------------------------------------
class TestWriteAgentAnalysisAndLink:
    def test_inserts_and_links_in_one_call(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected", server_id="default")

        analysis_id, linked = writer.write_agent_analysis_and_link(
            _analysis_dict(), iid, "default"
        )

        assert linked is True
        assert _analysis_row(incident_loop_db, analysis_id) is not None
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == analysis_id

    def test_no_incident_id_only_inserts(self, incident_loop_db):
        analysis_id, linked = writer.write_agent_analysis_and_link(
            _analysis_dict(analysis_type="routine"), None, "default"
        )
        assert linked is False
        assert _analysis_row(incident_loop_db, analysis_id) is not None

    def test_resolved_incident_stores_analysis_but_does_not_link(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="resolved", server_id="default")

        analysis_id, linked = writer.write_agent_analysis_and_link(
            _analysis_dict(analysis_type="replay"), iid, "default"
        )

        assert linked is False
        assert _analysis_row(incident_loop_db, analysis_id) is not None  # still persisted
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "resolved"
        assert row["analysis_id"] is None

    def test_does_not_link_across_servers(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected", server_id="b")

        analysis_id, linked = writer.write_agent_analysis_and_link(
            _analysis_dict(server_id="a"), iid, "a"
        )

        assert linked is False
        assert _analysis_row(incident_loop_db, analysis_id) is not None
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"
        assert row["analysis_id"] is None


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
# agent/llm_agent.py: self-report gating (P1-4)
#
# A model's self-reported `### Addresses incident #N` is only trustworthy
# when the analysis isn't a blind "routine" run, or when the id genuinely
# appears in the text the model was shown. A routine analysis has no
# incident in its prompt at all, so an unprompted "Addresses incident #N" is
# far more likely to be a hallucination than a real reference.
# ---------------------------------------------------------------------------
class TestSelfReportGating:
    def test_routine_self_report_absent_from_state_report_is_ignored(
        self, incident_loop_db, caplog
    ):
        iid = _insert_incident(incident_loop_db, status="detected")
        text = (
            "### Severity: info\n### Findings\nx\n### Recommendations\ny\n"
            f"### Addresses incident #{iid}\n"
        )

        with caplog.at_level(logging.WARNING):
            analysis = la._parse_and_store(
                text, "routine", "state report with no incident reference", "default"
            )

        assert analysis["id"] is not None  # analysis is still stored
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"
        assert row["analysis_id"] is None
        assert any("Ignoring self-reported" in r.message for r in caplog.records)

    def test_routine_self_report_present_in_state_report_links(self, incident_loop_db):
        iid = _insert_incident(incident_loop_db, status="detected")
        text = (
            "### Severity: info\n### Findings\nx\n### Recommendations\ny\n"
            f"### Addresses incident #{iid}\n"
        )

        analysis = la._parse_and_store(
            text, "routine", f"...incident #{iid} is still ongoing...", "default"
        )

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == analysis["id"]

    def test_routine_via_run_llm_analysis_self_report_ignored_when_absent(
        self, incident_loop_db, monkeypatch
    ):
        iid = _insert_incident(incident_loop_db, status="detected")
        monkeypatch.setattr(
            la, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(
            la, "_run_anthropic_loop",
            lambda backend, max_tokens, max_rounds, prompt: (
                "### Severity: info\n### Findings\nx\n### Recommendations\ny\n"
                f"### Addresses incident #{iid}\n"
            ),
        )

        result = la.run_llm_analysis(
            "routine prompt with no incident reference",
            analysis_type="routine", server_id="default",
        )

        assert result["analysis_id"] is not None
        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "detected"
        assert row["analysis_id"] is None

    def test_routine_via_run_llm_analysis_self_report_honored_when_present(
        self, incident_loop_db, monkeypatch
    ):
        iid = _insert_incident(incident_loop_db, status="detected")
        monkeypatch.setattr(
            la, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(
            la, "_run_anthropic_loop",
            lambda backend, max_tokens, max_rounds, prompt: (
                "### Severity: info\n### Findings\nx\n### Recommendations\ny\n"
                f"### Addresses incident #{iid}\n"
            ),
        )

        result = la.run_llm_analysis(
            f"routine prompt mentioning incident #{iid} explicitly",
            analysis_type="routine", server_id="default",
        )

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == result["analysis_id"]


# ---------------------------------------------------------------------------
# agent/llm_agent.py: run_llm_analysis atomicity + resolved-incident guard
# (P1-4 / P1-21) — this is the path agent.replay.run_replay uses.
# ---------------------------------------------------------------------------
class TestRunLlmAnalysisLinking:
    def _stub_backend(self, monkeypatch, response_text: str):
        monkeypatch.setattr(
            la, "_detect_backend",
            lambda config: {"type": "anthropic", "model": "claude-x", "api_key": "sk-x"},
        )
        monkeypatch.setattr(
            la, "_run_anthropic_loop",
            lambda backend, max_tokens, max_rounds, prompt: response_text,
        )

    def test_replay_on_resolved_incident_stores_but_does_not_relink(
        self, incident_loop_db, monkeypatch
    ):
        """P1-4/P1-21: a post-mortem replay on an already-resolved incident
        must still persist the analysis but must not resurrect the incident.
        The explicit incident_id here is authoritative (not a self-report),
        so it isn't gated by _resolve_addressed_incident — it's the guarded
        UPDATE inside write_agent_analysis_and_link that blocks the link."""
        iid = _insert_incident(incident_loop_db, status="resolved")
        self._stub_backend(
            monkeypatch,
            "### Severity: info\n### Findings\nx\n### Recommendations\ny\n",
        )

        result = la.run_llm_analysis(
            "replay prompt", analysis_type="replay", server_id="default",
            incident_id=iid,
        )

        assert result["analysis_id"] is not None
        assert _analysis_row(incident_loop_db, result["analysis_id"]) is not None

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "resolved"       # not resurrected
        assert row["analysis_id"] is None         # not linked

    def test_replay_on_open_incident_links_normally(self, incident_loop_db, monkeypatch):
        iid = _insert_incident(incident_loop_db, status="detected")
        self._stub_backend(
            monkeypatch,
            "### Severity: info\n### Findings\nx\n### Recommendations\ny\n",
        )

        result = la.run_llm_analysis(
            "replay prompt", analysis_type="replay", server_id="default",
            incident_id=iid,
        )

        row = _incident_row(incident_loop_db, iid)
        assert row["status"] == "analyzed"
        assert row["analysis_id"] == result["analysis_id"]


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
