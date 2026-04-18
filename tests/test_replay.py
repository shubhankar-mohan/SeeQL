"""Tests for agent/replay.py (Phase 1.6)."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import config as config_module
from alerting.anomaly_store import persist
from alerting.anomaly import AnomalyResult
from alerting.incidents import update_windows
from agent.replay import run_replay, ReplayResult
from storage.connection import reset_connections


SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


@pytest.fixture
def replay_db(tmp_path):
    """SQLite DB wired as the monitoring DB. No LLM backend."""
    db_path = tmp_path / "replay_test.db"
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
        "agent": {
            # No credentials → _detect_backend returns None → timeline-only
            "enabled": False,
        },
        "gcp": {},
    }
    reset_connections()
    yield db_path
    reset_connections()


def _iso(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _seed_lock_cascade(db_path: Path, server_id: str = "default"):
    """Seed a synthetic lock cascade scenario for replay."""
    # Persist anomaly events via the real writer
    persist([
        AnomalyResult(
            metric="threads_running",
            current=47.0,
            baseline_mean=12.0,
            baseline_stddev=2.5,
            z_score=14.0,
            pct_change=291.7,
            direction="high",
            severity="critical",
            server_id=server_id,
            detected_at=_iso(10),
        ),
        AnomalyResult(
            metric="lock_frequency",
            current=23.0,
            baseline_mean=2.0,
            baseline_stddev=1.0,
            z_score=21.0,
            pct_change=1050.0,
            direction="high",
            severity="critical",
            server_id=server_id,
            detected_at=_iso(8),
        ),
    ])
    update_windows(server_id)

    # Also seed a lock_wait_snapshot and a ddl_change so the timeline
    # exercises all query branches
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT INTO lock_wait_snapshots
           (snapshot_time, server_id, waiting_pid, blocking_pid, wait_seconds,
            waiting_query, blocking_query)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_iso(9), server_id, 812, 847, 14,
         "SELECT * FROM loyalty_members WHERE uid=?",
         "UPDATE loyalty_members SET points=points+10 WHERE batch_id=?"),
    )
    conn.execute(
        """INSERT INTO ddl_changes
           (detected_at, server_id, table_schema, table_name, change_type,
            old_ddl, new_ddl)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (_iso(15), server_id, "shop", "loyalty_members", "index",
         "-- old", "-- new"),
    )
    conn.execute(
        """INSERT INTO global_status_snapshots
           (snapshot_time, server_id, variable_name, raw_value)
           VALUES (?, ?, ?, ?)""",
        (_iso(7), server_id, "Threads_running", 47),
    )
    conn.commit()
    conn.close()


class TestReplay:
    def test_timeline_only_when_no_llm(self, replay_db):
        """With no LLM backend, replay falls back to timeline-only."""
        _seed_lock_cascade(replay_db)

        from_ts = _iso(30)
        to_ts = _iso(1)
        result = run_replay(from_ts=from_ts, to_ts=to_ts)

        assert isinstance(result, ReplayResult)
        assert result.analysis_md is None  # no LLM
        # Timeline should contain all seeded events
        assert "ANOMALY" in result.timeline_md
        assert "threads_running" in result.timeline_md
        assert "lock_frequency" in result.timeline_md
        assert "LOCK" in result.timeline_md
        assert "812" in result.timeline_md  # waiting pid
        assert "DDL" in result.timeline_md
        assert "loyalty_members" in result.timeline_md
        # Counts should reflect seeded data
        assert result.events_by_category.get("anomalies", 0) >= 2
        assert result.events_by_category.get("lock_waits", 0) >= 1
        assert result.events_by_category.get("ddl_changes", 0) >= 1

    def test_empty_window(self, replay_db):
        """Replay on a window with no data returns the empty marker."""
        from_ts = "2020-01-01T00:00:00+00:00"
        to_ts = "2020-01-01T01:00:00+00:00"
        result = run_replay(from_ts=from_ts, to_ts=to_ts)
        assert "No events recorded" in result.timeline_md
        assert result.analysis_md is None

    def test_to_markdown_includes_fallback_note_without_llm(self, replay_db):
        """The rendered markdown should explain the LLM fallback to the reader."""
        _seed_lock_cascade(replay_db)
        result = run_replay(from_ts=_iso(30), to_ts=_iso(1))
        md = result.to_markdown()
        assert "# Incident Replay" in md
        assert "## Timeline" in md
        assert "## Root Cause Analysis" in md
        assert "LLM analysis unavailable" in md
        assert "postmortem primer" in md

    def test_incident_id_in_header(self, replay_db):
        """When called with incident_id, the header shows it."""
        _seed_lock_cascade(replay_db)
        # The seeded data creates one incident
        conn = sqlite3.connect(str(replay_db))
        row = conn.execute("SELECT id, start_time, end_time FROM incident_windows LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        incident_id = row[0]

        result = run_replay(
            from_ts=row[1], to_ts=row[2], incident_id=incident_id
        )
        md = result.to_markdown()
        assert f"incident #{incident_id}" in md
