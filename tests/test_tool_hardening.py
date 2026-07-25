"""
Tests for Task 4.5 tool hardening.

Covers the parts of P1-14/P1-15/P1-16 that don't already have a natural
home in an existing test file:
  - context caps: get_query_history days/rows, search_slow_log limit,
    get_recent_analyses limit, execute_tool's 16KB result cap, and the new
    LIMIT 100 on get_live_locks / get_live_transactions.
  - run_explain's live-path safety: MAX_EXECUTION_TIME + the digest-text-only
    fallback guard (P1-15).
  - _run_live_query's retry classification (P1-16): only the transient MySQL
    errno set collectors use should be retried.

(P1-9 budget-charges-failures lives in test_budget.py; P2-5 identifier
guards live in test_identifier_validation.py / test_tool_server_scoping.py;
P1-10 ContextVar hygiene lives in test_tool_server_scoping.py /
test_llm_agent.py; P1-17/P3-11 replay hardening lives in test_replay.py --
see the Task 4.5 brief for the full map.)
"""

import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import config as config_module
import agent.tools as tools
from storage.connection import reset_connections


def _iso(dt=None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


@contextlib.contextmanager
def _config_for(db_path):
    """Point get_mon_reader()/get_mon_writer() at the seeded temp DB."""
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
    }
    reset_connections()
    try:
        yield
    finally:
        config_module._config = prev
        reset_connections()


def _seed_digest(conn, digest, server_id, snapshot_time, query_sample_text=None,
                  digest_text="SELECT * FROM t WHERE x=?", schema_name="db",
                  avg_time_sec=0.05, exec_count=10, rows_examined=100, rows_sent=10):
    conn.execute(
        """INSERT INTO query_digest_snapshots
           (snapshot_time, server_id, digest, digest_text, query_sample_text, schema_name,
            exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
            rows_examined, rows_sent, rows_affected)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (snapshot_time, server_id, digest, digest_text, query_sample_text, schema_name,
         exec_count, avg_time_sec * exec_count, avg_time_sec, avg_time_sec, avg_time_sec,
         rows_examined, rows_sent, 0),
    )


# ---------------------------------------------------------------------------
# P1-14: get_query_history clamps (days -> [1,90], QUERY_HISTORY LIMIT 2000)
# ---------------------------------------------------------------------------

class TestGetQueryHistoryClamp:
    def test_days_clamped_to_90(self, mon_db):
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=100)).isoformat()      # outside a 90-day clamp
        recent = (now - timedelta(days=5)).isoformat()      # inside
        _seed_digest(conn, "0xHIST", "server_a", old, query_sample_text="SELECT 1")
        _seed_digest(conn, "0xHIST", "server_a", recent, query_sample_text="SELECT 1")
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_query_history({"digest": "0xHIST", "days": 3650})
            finally:
                tools.set_current_server(None)

        assert result["data_points"] == 1, (
            "days=3650 must clamp to 90 -- the 100-day-old row should be excluded"
        )

    def test_rows_capped_at_2000(self, mon_db):
        """The 2000-row cap must keep the NEWEST rows, not the oldest.

        An `ORDER BY snapshot_time ASC ... LIMIT 2000` truncates to the FIRST
        (oldest) 2000 rows -- for a persistently-hot digest at days=90 that
        silently drops the most recent weeks, i.e. exactly "now", the period
        that shows WHEN a query degraded. Seed >2000 rows spanning many days
        (one per hour, ~87.5 days back, all inside the 90-day window) and
        prove the retained rows are the newest, still displayed ascending.
        """
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        n = 2100  # > the 2000 cap, so the cap must drop ~100 rows
        # i=0 -> now (newest); i=n-1 -> ~87.5 days ago (window's oldest).
        timestamps = [(now - timedelta(hours=i)).isoformat() for i in range(n)]
        newest_ts = timestamps[0]
        oldest_ts = timestamps[-1]
        rows = [
            (
                ts, "server_a", "0xMANY",
                "SELECT * FROM t WHERE x=?", "SELECT * FROM t WHERE x=1", "db",
                1, 0.01, 0.01, 0.01, 0.01, 10, 1, 0,
            )
            for ts in timestamps
        ]
        conn.executemany(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, query_sample_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
                rows_examined, rows_sent, rows_affected)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_query_history({"digest": "0xMANY", "days": 90})
            finally:
                tools.set_current_server(None)

        assert result["data_points"] <= 2000, (
            f"QUERY_HISTORY must cap at 2000 rows, got {result['data_points']}"
        )
        returned = [r["snapshot_time"] for r in result["history"]]
        # history is returned ascending: [-1] is newest, [0] is oldest.
        assert returned[-1] == newest_ts, (
            "cap must KEEP the newest sample (incl. 'now') -- an ASC LIMIT "
            f"would drop it. newest returned={returned[-1]} expected={newest_ts}"
        )
        assert returned[0] != oldest_ts, (
            "cap must DROP the window's oldest rows, not the newest -- "
            f"oldest returned should not be the window's oldest ({oldest_ts})"
        )
        assert returned == sorted(returned), "history must still be ascending"


# ---------------------------------------------------------------------------
# P1-14: search_slow_log / get_recent_analyses limit clamps
# ---------------------------------------------------------------------------

class TestSearchSlowLogClamp:
    def test_limit_clamped_to_50(self, mon_db):
        conn, db_path = mon_db
        now = _iso()
        rows = [
            (now, "server_a", "app", "10.0.0.1", float(i), 0.0, 1, 100,
             f"SELECT * FROM members WHERE id={i}")
            for i in range(60)
        ]
        conn.executemany(
            """INSERT INTO slow_query_log
               (snapshot_time, server_id, user, host, query_time_sec, lock_time_sec,
                rows_sent, rows_examined, sql_text)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_search_slow_log({"keyword": "members", "limit": 10000})
            finally:
                tools.set_current_server(None)

        assert result["match_count"] <= 50
        assert len(result["entries"]) <= 50


class TestGetRecentAnalysesClamp:
    def test_limit_clamped_to_50(self, mon_db):
        conn, db_path = mon_db
        now = _iso()
        rows = [
            (now, "server_a", "routine", "info", json.dumps([f"f{i}"]), json.dumps([]))
            for i in range(60)
        ]
        conn.executemany(
            """INSERT INTO agent_analyses
               (analyzed_at, server_id, analysis_type, severity, findings, recommendations)
               VALUES (?,?,?,?,?,?)""",
            rows,
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_recent_analyses({"hours": 24, "limit": 10000})
            finally:
                tools.set_current_server(None)

        assert result["count"] <= 50


# ---------------------------------------------------------------------------
# P1-14: execute_tool result cap (16KB)
# ---------------------------------------------------------------------------

class TestExecuteToolResultCap:
    def test_large_handler_result_truncated(self, monkeypatch):
        big = {"rows": ["x" * 200 for _ in range(1000)]}  # ~200KB serialized
        monkeypatch.setattr(tools, "_tool_get_live_processlist", lambda input_data: big)

        out = tools.execute_tool("get_live_processlist", {})

        assert len(out) <= 16384 + 100, f"expected <=16KB-ish, got {len(out)} chars"
        assert "truncated" in out.lower()

    def test_small_result_not_truncated(self, monkeypatch):
        monkeypatch.setattr(
            tools, "_tool_get_live_processlist",
            lambda input_data: {"active_threads": 0, "processes": []},
        )
        out = tools.execute_tool("get_live_processlist", {})
        assert "truncated" not in out.lower()
        assert json.loads(out) == {"active_threads": 0, "processes": []}


# ---------------------------------------------------------------------------
# P1-14: get_live_locks / get_live_transactions LIMIT 100
# ---------------------------------------------------------------------------

class TestLiveLocksAndTransactionsCapped:
    def test_get_live_locks_query_has_limit(self, monkeypatch):
        captured = {}

        class FakeCursor:
            def execute(self, sql, *a, **k):
                captured["sql"] = sql

            def fetchall(self):
                return []

        class FakeConn:
            def cursor(self, dictionary=True):
                return FakeCursor()

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id=None):
            yield FakeConn()

        monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)
        tools._tool_get_live_locks({})
        assert "LIMIT 100" in captured["sql"]
        # The LIMIT must be paired with an ORDER BY so the 100 that survive
        # are deterministic (longest-waiting conflicts first), not arbitrary.
        assert "ORDER BY wait_seconds DESC" in captured["sql"]

    def test_get_live_transactions_query_has_limit(self, monkeypatch):
        captured = {}

        class FakeCursor:
            def execute(self, sql, *a, **k):
                captured["sql"] = sql

            def fetchall(self):
                return []

        class FakeConn:
            def cursor(self, dictionary=True):
                return FakeCursor()

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id=None):
            yield FakeConn()

        monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)
        tools._tool_get_live_transactions({})
        assert "LIMIT 100" in captured["sql"]


# ---------------------------------------------------------------------------
# P1-15: run_explain live path safety
# ---------------------------------------------------------------------------

class TestRunExplainLivePathSafety:
    def test_digest_text_only_fallback_rejected_without_hitting_prod(self, mon_db, monkeypatch):
        """No captured query_sample_text -> sql_text falls back to
        digest_text, a normalized fingerprint with `?` placeholders. Must be
        rejected locally, never sent to prod."""
        conn, db_path = mon_db
        _seed_digest(
            conn, "0xNOSAMPLE", "server_a", _iso(),
            query_sample_text=None, digest_text="SELECT * FROM members WHERE id = ?",
        )
        conn.commit()

        called = {"flag": False}

        def _sentinel(*a, **k):
            called["flag"] = True
            raise AssertionError("must NOT hit prod for a digest-text-only fallback")

        with _config_for(db_path):
            monkeypatch.setattr(tools, "get_prod_connection", _sentinel)
            tools.set_current_server("server_a")
            try:
                result = tools._tool_run_explain({"digest": "0xNOSAMPLE"})
            finally:
                tools.set_current_server(None)

        assert "error" in result
        assert called["flag"] is False

    def test_live_explain_sets_max_execution_time(self, mon_db, monkeypatch):
        conn, db_path = mon_db
        _seed_digest(
            conn, "0xREAL", "server_a", _iso(),
            query_sample_text="SELECT * FROM members WHERE id = 5",
            digest_text="SELECT * FROM members WHERE id = ?",
        )
        conn.commit()

        executed = []

        class FakeCursor:
            def execute(self, sql, *a, **k):
                executed.append(sql)

            def fetchone(self):
                return {"EXPLAIN": '{"query_block": {}}'}

        class FakeConn:
            def cursor(self, dictionary=True):
                return FakeCursor()

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id=None):
            yield FakeConn()

        with _config_for(db_path):
            monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)
            tools.set_current_server("server_a")
            try:
                result = tools._tool_run_explain({"digest": "0xREAL"})
            finally:
                tools.set_current_server(None)

        assert result["source"] == "live"
        assert any("MAX_EXECUTION_TIME" in s for s in executed), (
            f"expected a SET SESSION MAX_EXECUTION_TIME call, got: {executed}"
        )


# ---------------------------------------------------------------------------
# P1-16: _run_live_query retries only transient MySQL errnos
# ---------------------------------------------------------------------------

class TestLiveQueryRetryOnlyTransient:
    def test_transient_errno_is_retried(self, monkeypatch):
        calls = {"n": 0}

        class TransientError(Exception):
            errno = 2013  # Lost connection during query

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise TransientError("lost connection")

            class FakeCursor:
                def execute(self, *a, **k):
                    pass

                def fetchall(self):
                    return [{"a": 1}]

            class FakeConn:
                def cursor(self, dictionary=True):
                    return FakeCursor()

            yield FakeConn()

        monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)
        monkeypatch.setattr(tools.time, "sleep", lambda s: None)

        rows = tools._run_live_query("SELECT 1")
        assert rows == [{"a": 1}]
        assert calls["n"] == 3

    def test_deterministic_error_not_retried(self, monkeypatch):
        calls = {"n": 0}

        class DeterministicError(Exception):
            errno = 1064  # syntax error -- NOT in the transient set

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id=None):
            calls["n"] += 1
            raise DeterministicError("you have an error in your SQL syntax")
            yield  # pragma: no cover -- unreachable, keeps this a generator

        monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)

        with pytest.raises(Exception):
            tools._run_live_query("SELECT bad syntax")

        assert calls["n"] == 1, "a deterministic (non-transient) error must not be retried"

    def test_error_with_no_errno_not_retried(self, monkeypatch):
        """A plain exception with no `errno` (e.g. a bug, not a MySQL error)
        must default to non-retryable -- fail closed, not fail open."""
        calls = {"n": 0}

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id=None):
            calls["n"] += 1
            raise ValueError("unexpected")
            yield  # pragma: no cover

        monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)

        with pytest.raises(ValueError):
            tools._run_live_query("SELECT 1")

        assert calls["n"] == 1
