"""Tests for alerting/budget.py."""

from datetime import datetime, timezone, timedelta
import json

import pytest

import config as config_module
from storage.connection import reset_connections
from alerting.budget import (
    Budget,
    LIVE_TOOLS,
    EXPENSIVE_TOOL,
    queries_used_in_last_minute,
    threads_running_from_snapshot,
    threads_running_ok,
)


@pytest.fixture
def mon_db_ctx(mon_db):
    """Point global config at the mon_db DB so budget reads go there."""
    _, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
    }
    reset_connections()
    yield mon_db
    config_module._config = prev
    reset_connections()


class TestBudgetCore:
    def test_snapshot_tools_always_allowed(self):
        b = Budget(investigation_id=1, live_tool_cap=0, explain_cap=0)
        # Snapshot tools not in LIVE_TOOLS and not in EXPENSIVE_TOOLS
        assert b.can_call("get_query_history") is True
        assert b.can_call("get_table_schema") is True
        assert b.can_call("search_slow_log") is True

    def test_run_explain_budgeted_as_expensive(self):
        # run_explain falls through to a live EXPLAIN when uncached, so it is
        # capped by the explain budget rather than treated as a free snapshot.
        b = Budget(investigation_id=1, live_tool_cap=10, explain_cap=1)
        assert b.can_call("run_explain") is True
        b.record("run_explain")
        assert b.can_call("run_explain") is False
        # shares the explain counter with explain_query
        assert b.can_call(EXPENSIVE_TOOL) is False
        assert b.snapshot()["explain_used"] == 1

    def test_live_tool_cap_enforced(self):
        b = Budget(investigation_id=1, live_tool_cap=2)
        assert b.can_call("get_live_processlist") is True
        b.record("get_live_processlist")
        assert b.can_call("get_live_processlist") is True
        b.record("get_live_locks")
        assert b.can_call("get_live_transactions") is False

    def test_explain_cap_separate_from_live(self):
        b = Budget(investigation_id=1, live_tool_cap=10, explain_cap=1)
        assert b.can_call(EXPENSIVE_TOOL) is True
        b.record(EXPENSIVE_TOOL)
        assert b.can_call(EXPENSIVE_TOOL) is False
        # But live tools still work
        assert b.can_call("get_live_processlist") is True

    def test_all_known_live_tools_classified(self):
        for t in LIVE_TOOLS:
            b = Budget(investigation_id=1, live_tool_cap=0)
            assert b.can_call(t) is False, f"{t} should be live-budgeted"

    def test_rejection_message_mentions_counts(self):
        b = Budget(investigation_id=1, live_tool_cap=2, explain_cap=1)
        b.record("get_live_processlist")
        msg = b.rejection_message("get_live_processlist")
        assert "1/2" in msg
        msg = b.rejection_message(EXPENSIVE_TOOL)
        assert "0/1" in msg

    def test_snapshot_reflects_usage(self):
        b = Budget(investigation_id=42, live_tool_cap=10, explain_cap=2)
        b.record("get_live_processlist")
        b.record(EXPENSIVE_TOOL)
        s = b.snapshot()
        assert s["investigation_id"] == 42
        assert s["live_tool_used"] == 1
        assert s["explain_used"] == 1


class TestQueriesUsed:
    def test_empty_returns_zero(self, mon_db_ctx):
        assert queries_used_in_last_minute(999) == 0

    def test_sums_query_count_last_minute(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        now = datetime.now(timezone.utc)
        recent = now.isoformat()
        old = (now - timedelta(seconds=120)).isoformat()
        for sampled_at, qc in [(recent, 3), (recent, 5), (old, 99)]:
            conn.execute(
                "INSERT INTO investigation_samples (investigation_id, sampled_at, sample_type, query_count, data) "
                "VALUES (?,?,?,?,?)",
                (1, sampled_at, "processlist", qc, "{}"),
            )
        conn.commit()
        # 3 + 5 = 8 in the last minute; 99 is outside the window.
        assert queries_used_in_last_minute(1) == 8

    def test_isolated_per_investigation(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO investigation_samples (investigation_id, sampled_at, sample_type, query_count, data) "
            "VALUES (?,?,?,?,?)",
            (1, now, "locks", 10, "{}"),
        )
        conn.execute(
            "INSERT INTO investigation_samples (investigation_id, sampled_at, sample_type, query_count, data) "
            "VALUES (?,?,?,?,?)",
            (2, now, "locks", 99, "{}"),
        )
        conn.commit()
        assert queries_used_in_last_minute(1) == 10
        assert queries_used_in_last_minute(2) == 99


class TestLoadGuard:
    def test_snapshot_missing_returns_none(self, mon_db_ctx):
        assert threads_running_from_snapshot("srv-empty") is None

    def test_snapshot_within_freshness(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO global_status_snapshots (server_id, snapshot_time, variable_name, raw_value) "
            "VALUES (?,?,?,?)",
            ("srv1", ts, "Threads_running", 42),
        )
        conn.commit()
        assert threads_running_from_snapshot("srv1") == 42

    def test_snapshot_too_old_returns_none(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        conn.execute(
            "INSERT INTO global_status_snapshots (server_id, snapshot_time, variable_name, raw_value) "
            "VALUES (?,?,?,?)",
            ("srv1", old, "Threads_running", 42),
        )
        conn.commit()
        assert threads_running_from_snapshot("srv1", max_age_seconds=60) is None

    def test_threads_running_ok_true_when_under_threshold(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO global_status_snapshots (server_id, snapshot_time, variable_name, raw_value) "
            "VALUES (?,?,?,?)",
            ("srv1", ts, "Threads_running", 10),
        )
        conn.commit()
        assert threads_running_ok("srv1", threshold=40) is True

    def test_threads_running_ok_false_when_over_threshold(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        ts = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO global_status_snapshots (server_id, snapshot_time, variable_name, raw_value) "
            "VALUES (?,?,?,?)",
            ("srv1", ts, "Threads_running", 80),
        )
        conn.commit()
        assert threads_running_ok("srv1", threshold=40) is False

    def test_threads_running_ok_true_when_snapshot_stale(self, mon_db_ctx):
        # Conservative default: don't block sampling when we can't see the server.
        assert threads_running_ok("missing-server", threshold=40) is True
