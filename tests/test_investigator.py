"""
Tests for alerting/investigator.py — Phase 1 + Phase 2.

Strategy: seed an inbound_alerts + investigations row, then invoke
run_investigation(id). Monkey-patch run_llm_analysis to return canned text.
All MySQL tools are not touched because Phase 1 is SQLite-only and the LLM
path is mocked.
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import config as config_module
from storage.connection import reset_connections
from storage import writer

from alerting import investigator as INV
from alerting.inbound.models import InboundAlert
from alerting.budget import Budget, LIVE_TOOLS, EXPENSIVE_TOOL


@pytest.fixture
def mon_db_ctx(mon_db):
    _, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {
            "path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000,
        },
        "investigator": {
            "phase2_live_tool_cap": 3,
            "phase2_explain_cap": 1,
            "phase2_max_tool_rounds": 4,
            "phase3_sampling_interval_seconds": 20,
            "confidence_completion_threshold": 0.8,
        },
        "alerting": {"enabled": False},
    }
    reset_connections()
    yield mon_db
    config_module._config = prev
    reset_connections()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed_alert_and_investigation(
    server_id: str = "srv1",
    alert_type: str = "missing_index",
    severity: str = "warning",
    summary: str = "Slow query on members",
    external_id: str = "ext-abc",
) -> tuple[int, int]:
    alert_id = writer.write_inbound_alert({
        "provider": "generic",
        "received_at": _iso_now(),
        "server_id": server_id,
        "external_id": external_id,
        "alert_type": alert_type,
        "severity": severity,
        "summary": summary,
        "payload": json.dumps({}),
        "signature_verified": 1,
    })
    inv_id = writer.write_investigation({
        "inbound_alert_id": alert_id,
        "server_id": server_id,
        "started_at": _iso_now(),
        "status": "queued",
    })
    return alert_id, inv_id


def _fetch_inv(conn, inv_id: int) -> dict:
    row = conn.execute("SELECT * FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    return dict(row)


def _fetch_findings(conn, inv_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM investigation_findings WHERE investigation_id = ? ORDER BY id",
        (inv_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Root cause / confidence extraction
# ---------------------------------------------------------------------------

class TestExtraction:
    def test_extract_root_cause_basic(self):
        text = (
            "### Severity: warning\n"
            "### Findings\n"
            "- **Root cause**: Query 0xABC full-scans `members` after idx_foo was dropped.\n"
            "- **Evidence**: EXPLAIN shows type=ALL.\n"
            "### Recommendations\n"
            "- **Immediate action**: CREATE INDEX idx_foo ON members(foo).\n"
            "- **Confidence**: 0.92\n"
        )
        rc = INV._extract_root_cause(text)
        conf = INV._extract_confidence(text)
        assert rc is not None and "0xABC" in rc
        assert abs(conf - 0.92) < 1e-6

    def test_extract_confidence_percent_form(self):
        assert INV._extract_confidence("**Confidence**: 85") == 0.85

    def test_extract_confidence_missing(self):
        assert INV._extract_confidence("no confidence field") == 0.0

    def test_extract_root_cause_missing(self):
        assert INV._extract_root_cause("no root cause here") is None


# ---------------------------------------------------------------------------
# Phase 1
# ---------------------------------------------------------------------------

class TestPhase1Triage:
    def test_missing_investigation_returns_missing(self, mon_db_ctx):
        result = INV.run_investigation(999)
        assert result == {"status": "missing"}

    def test_transient_alert_short_circuits_to_completed(self, mon_db_ctx):
        # No seeded signals => no events, correlator returns no findings,
        # alert is non-critical => should not proceed.
        _, inv_id = _seed_alert_and_investigation(alert_type="default", severity="warning")
        # Patch LLM to fail if called; this path should not call it.
        with patch("agent.llm_agent.run_llm_analysis") as mock_llm:
            result = INV.run_investigation(inv_id)
            mock_llm.assert_not_called()
        conn, _ = mon_db_ctx
        inv = _fetch_inv(conn, inv_id)
        assert inv["status"] == "completed"
        assert inv["ended_at"] is not None

        # A phase-1 hypothesis finding should exist.
        findings = _fetch_findings(conn, inv_id)
        assert any(f["phase"] == 1 and f["kind"] == "hypothesis" for f in findings)

    def test_critical_alert_always_proceeds(self, mon_db_ctx):
        _, inv_id = _seed_alert_and_investigation(
            alert_type="default", severity="critical",
        )
        fake_text = (
            "### Severity: critical\n"
            "### Findings\n"
            "- **Root cause**: Lock wait cascade on `orders`.\n"
            "### Recommendations\n"
            "- **Immediate action**: KILL <pid>\n"
            "- **Confidence**: 0.9\n"
        )
        with patch(
            "agent.llm_agent.run_llm_analysis",
            return_value={"text": fake_text, "severity": "critical", "analysis_id": 42},
        ) as mock_llm:
            result = INV.run_investigation(inv_id)
            mock_llm.assert_called_once()
        conn, _ = mon_db_ctx
        inv = _fetch_inv(conn, inv_id)
        # Confidence 0.9 ≥ threshold 0.8 → completed at Phase 2
        assert inv["status"] == "completed"
        assert inv["analysis_id"] == 42


# ---------------------------------------------------------------------------
# Phase 2
# ---------------------------------------------------------------------------

class TestPhase2:
    def _seed_correlator_signal(self, conn, server_id="srv1"):
        """Seed a missing-index signal so Phase 1 proceeds to Phase 2."""
        conn.execute(
            """INSERT INTO query_digest_snapshots
               (server_id, snapshot_time, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
                rows_examined, rows_sent, rows_affected,
                full_scans, no_index_used)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                server_id, _iso_now(), "0xHIGH",
                "SELECT * FROM members WHERE foo = ?", "testdb",
                100, 5.0, 0.05, 1.0, 0.01,
                1000000, 10, 0, 1, 1,
            ),
        )
        conn.commit()

    def test_phase2_completes_on_high_confidence(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        self._seed_correlator_signal(conn)
        _, inv_id = _seed_alert_and_investigation()
        fake_text = (
            "### Severity: warning\n"
            "### Findings\n"
            "- **Root cause**: Full-scan on members due to missing idx_foo.\n"
            "### Recommendations\n"
            "- **Immediate action**: CREATE INDEX idx_foo ON members(foo)\n"
            "- **Confidence**: 0.9\n"
        )
        with patch(
            "agent.llm_agent.run_llm_analysis",
            return_value={"text": fake_text, "severity": "warning", "analysis_id": 7},
        ):
            INV.run_investigation(inv_id)

        conn2, _ = mon_db_ctx
        inv = _fetch_inv(conn2, inv_id)
        assert inv["status"] == "completed"
        assert inv["analysis_id"] == 7
        assert "missing idx_foo" in (inv["root_cause_summary"] or "")

        findings = _fetch_findings(conn2, inv_id)
        assert any(f["phase"] == 1 and f["kind"] == "correlation" for f in findings)
        assert any(f["phase"] == 2 for f in findings)

    def test_phase2_schedules_phase3_on_low_confidence(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        self._seed_correlator_signal(conn)
        _, inv_id = _seed_alert_and_investigation()
        fake_text = (
            "### Severity: warning\n"
            "### Findings\n"
            "- **Root cause**: Unclear — suspect multiple digests.\n"
            "### Recommendations\n"
            "- **Immediate action**: Watch next sampling window.\n"
            "- **Confidence**: 0.4\n"
        )
        with patch(
            "agent.llm_agent.run_llm_analysis",
            return_value={"text": fake_text, "severity": "warning", "analysis_id": 8},
        ):
            INV.run_investigation(inv_id)

        conn2, _ = mon_db_ctx
        inv = _fetch_inv(conn2, inv_id)
        assert inv["status"] == "phase3"
        assert inv["ended_at"] is None    # still running
        assert inv["phase3_next_run_at"] is not None

    def test_phase2_llm_unavailable_falls_back(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        self._seed_correlator_signal(conn)
        _, inv_id = _seed_alert_and_investigation(severity="critical")
        with patch(
            "agent.llm_agent.run_llm_analysis",
            side_effect=RuntimeError("No LLM backend configured"),
        ):
            INV.run_investigation(inv_id)
        conn2, _ = mon_db_ctx
        findings = _fetch_findings(conn2, inv_id)
        # A Phase 2 finding exists marking the LLM as unavailable.
        p2 = [f for f in findings if f["phase"] == 2]
        assert len(p2) == 1
        content = json.loads(p2[0]["content"])
        assert content.get("llm_unavailable") is True
        # Low-confidence fallback → Phase 3 scheduled
        inv = _fetch_inv(conn2, inv_id)
        assert inv["status"] == "phase3"

    def test_phase2_llm_errors_caught(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        self._seed_correlator_signal(conn)
        _, inv_id = _seed_alert_and_investigation(severity="critical")
        with patch(
            "agent.llm_agent.run_llm_analysis",
            side_effect=ValueError("LLM blew up"),
        ):
            INV.run_investigation(inv_id)  # must not raise
        inv = _fetch_inv(mon_db_ctx[0], inv_id)
        assert inv["status"] == "phase3"  # falls back the same way


# ---------------------------------------------------------------------------
# Budget integration via agent.tools.execute_tool
# ---------------------------------------------------------------------------

class TestBudgetIntegration:
    def test_execute_tool_rejects_when_budget_exhausted(self):
        # Reproduce the real-world path: set_current_budget(Budget) → execute_tool
        # should respect the cap without touching production MySQL.
        from agent.tools import set_current_budget, execute_tool
        b = Budget(investigation_id=1, live_tool_cap=0, explain_cap=0)
        set_current_budget(b)
        try:
            # Expensive tool rejected
            result = execute_tool("explain_query", {"query": "SELECT 1"})
            data = json.loads(result)
            assert data.get("budget_rejected") is True
            assert "EXPLAIN" in data.get("error", "")

            # Live tool rejected
            result = execute_tool("get_live_processlist", {})
            data = json.loads(result)
            assert data.get("budget_rejected") is True

            # Snapshot tool still works (will get past the budget check; the
            # handler may still fail for lack of real data but that's OK —
            # we only care that budget doesn't reject it).
            result = execute_tool("get_recent_analyses", {})
            data = json.loads(result)
            assert data.get("budget_rejected") is not True
        finally:
            set_current_budget(None)
