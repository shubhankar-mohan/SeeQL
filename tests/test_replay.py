"""Tests for agent/replay.py (Phase 1.6)."""

import contextlib
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


class TestReplayHardening:
    """Task 4.5: P1-14 lock-wait downsampling, P1-17 NULL-safe formatting,
    P3-11 window padding."""

    def test_lock_wait_downsampling_caps_emitted_lines(self, replay_db):
        """P1-14: replay used to emit ONE line per lock-wait row -- during a
        real lock cascade (the exact incident type this tool exists for)
        that's thousands of lines. Must downsample like the
        threads_running series already does."""
        server_id = "default"
        base = datetime.now(timezone.utc) - timedelta(minutes=15)
        conn = sqlite3.connect(str(replay_db))
        rows = [
            (
                (base + timedelta(seconds=i)).isoformat(), server_id, 800 + i, 900,
                5, "SELECT 1", "UPDATE t SET x=1",
            )
            for i in range(500)
        ]
        conn.executemany(
            """INSERT INTO lock_wait_snapshots
               (snapshot_time, server_id, waiting_pid, blocking_pid, wait_seconds,
                waiting_query, blocking_query)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()
        conn.close()

        from_ts = (base - timedelta(minutes=1)).isoformat()
        to_ts = (base + timedelta(minutes=20)).isoformat()
        result = run_replay(from_ts=from_ts, to_ts=to_ts, server_id=server_id)

        lock_lines = result.timeline_md.count("**LOCK**")
        assert 0 < lock_lines <= 60, (
            f"expected downsampled lock lines (<=60), got {lock_lines}"
        )

    def test_null_anomaly_value_does_not_kill_timeline(self, monkeypatch):
        """P1-17: a NULL current_value/baseline_mean/z_score on an anomaly
        row used to raise TypeError from `:.2f`/`:.1f` formatting, and
        run_replay's broad except turned that into "Timeline unavailable"
        for the WHOLE window -- not just the one bad row."""
        import agent.replay as replay_mod

        anomaly_row = {
            "ts": "2026-01-01T00:00:00+00:00",
            "metric_name": "threads_running",
            "current_value": None,
            "baseline_mean": None,
            "z_score": None,
            "severity": "critical",
            "direction": "high",
        }

        class _FakeConn:
            def execute(self, sql, params):
                if "anomaly_events" in sql:
                    return [anomaly_row]
                return []

        @contextlib.contextmanager
        def _fake_reader():
            yield _FakeConn()

        monkeypatch.setattr(replay_mod, "get_mon_reader", _fake_reader)

        timeline_md, counts = replay_mod._build_timeline(
            "default", "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00"
        )

        assert "unavailable" not in timeline_md.lower()
        assert "ANOMALY" in timeline_md
        assert counts["anomalies"] == 1

    def test_window_padded_captures_boundary_event(self, replay_db):
        """P3-11: an event landing just BEFORE from_ts must still show up --
        pad the internal query window by +/-5 min so an exact BETWEEN
        doesn't silently drop the anomaly that triggered the window."""
        server_id = "default"
        from_ts = _iso(20)
        to_ts = _iso(10)
        boundary_event_ts = _iso(23)  # 3 min before from_ts -- within a 5-min pad

        persist([
            AnomalyResult(
                metric="threads_running", current=50.0, baseline_mean=10.0,
                baseline_stddev=2.0, z_score=20.0, pct_change=400.0,
                direction="high", severity="critical", server_id=server_id,
                detected_at=boundary_event_ts,
            ),
        ])

        result = run_replay(from_ts=from_ts, to_ts=to_ts, server_id=server_id)

        assert "ANOMALY" in result.timeline_md
        assert result.events_by_category.get("anomalies", 0) >= 1
