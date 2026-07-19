"""
Task 4.1 (P1-1): every snapshot tool in agent/tools.py must scope its
monitoring-DB reads to the CURRENT server.

Before the fix, six queries in agent/queries.py (EXPLAIN_FOR_DIGEST,
SCHEMA_FOR_TABLE, QUERY_HISTORY, LOCK_GRAPH, ACTIVE_TRANSACTIONS,
RECENT_ANALYSES) plus run_explain's inline digest lookup and the inline
slow-log query had no `server_id` filter at all. LOCK_GRAPH and
ACTIVE_TRANSACTIONS took the GLOBALLY-latest snapshot regardless of server,
so an agent analyzing server_a could be handed server_b's lock graph;
run_explain could EXPLAIN another server's captured SQL against the wrong
server's production connection. Multi-server data bleed.

Each test below seeds BOTH `server_a` and `server_b` into the mon-DB fixture.
Wherever a query does a MAX(snapshot_time)/ORDER BY ... LIMIT 1 "latest"
lookup, server_b is deliberately given the NEWER timestamp so that a bug
which ignores server_id would prefer server_b's row -- making the assertion
a real trap, not a tautology. Then each tool is called with server_a set as
the current server, and the test asserts ONLY server_a's data comes back.
"""

import contextlib
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config as config_module
import agent.tools as tools
from storage.connection import reset_connections


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


@contextmanager
def _config_for(db_path):
    """Point get_mon_reader() at the seeded temp DB (mirrors test_mcp_tools_*.py)."""
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


def _seed_digest(conn, digest, server_id, snapshot_time, avg_time_sec, query_sample_text,
                  exec_count=10, rows_examined=100, rows_sent=10):
    conn.execute(
        """INSERT INTO query_digest_snapshots
           (snapshot_time, server_id, digest, digest_text, query_sample_text, schema_name,
            exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
            rows_examined, rows_sent, rows_affected)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (snapshot_time, server_id, digest, "SELECT * FROM members WHERE x=?",
         query_sample_text, "db",
         exec_count, avg_time_sec * exec_count, avg_time_sec, avg_time_sec, avg_time_sec,
         rows_examined, rows_sent, 0),
    )


class TestLockGraphServerScoping:
    def test_lock_graph_excludes_other_server(self, mon_db):
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        older = _iso(now - timedelta(minutes=5))    # server_a
        newer = _iso(now)                           # server_b -- NEWER: a global
                                                      # MAX(snapshot_time) bug picks this.
        conn.execute(
            """INSERT INTO lock_wait_snapshots
               (snapshot_time, server_id, waiting_trx_id, waiting_pid, waiting_query,
                wait_seconds, blocking_trx_id, blocking_pid, blocking_query,
                blocking_trx_age_sec, blocking_rows_locked, blocking_rows_modified)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (older, "server_a", "trxA1", 101, "UPDATE members SET x=1",
             5, "trxA2", 102, "SELECT * FROM members FOR UPDATE", 10, 3, 1),
        )
        conn.execute(
            """INSERT INTO lock_wait_snapshots
               (snapshot_time, server_id, waiting_trx_id, waiting_pid, waiting_query,
                wait_seconds, blocking_trx_id, blocking_pid, blocking_query,
                blocking_trx_age_sec, blocking_rows_locked, blocking_rows_modified)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (newer, "server_b", "trxB1", 901, "UPDATE orders SET y=1",
             50, "trxB2", 902, "SELECT * FROM orders FOR UPDATE", 100, 30, 10),
        )
        conn.execute(
            """INSERT INTO transaction_snapshots
               (snapshot_time, server_id, trx_id, trx_state, trx_started, age_sec, pid,
                trx_query, rows_locked, rows_modified, isolation_level)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (older, "server_a", "trxA2", "RUNNING", older, 12, 102,
             "SELECT * FROM members FOR UPDATE", 3, 1, "REPEATABLE-READ"),
        )
        conn.execute(
            """INSERT INTO transaction_snapshots
               (snapshot_time, server_id, trx_id, trx_state, trx_started, age_sec, pid,
                trx_query, rows_locked, rows_modified, isolation_level)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (newer, "server_b", "trxB2", "RUNNING", newer, 120, 902,
             "SELECT * FROM orders FOR UPDATE", 30, 10, "REPEATABLE-READ"),
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_lock_graph({})
            finally:
                tools.set_current_server(None)

        waiting_pids = {r["waiting_pid"] for r in result["lock_waits"]}
        assert waiting_pids == {101}, f"server_b leaked into lock_waits: {result['lock_waits']}"

        txn_pids = {r["pid"] for r in result["active_transactions"]}
        assert txn_pids == {102}, f"server_b leaked into active_transactions: {result['active_transactions']}"


class TestQueryHistoryServerScoping:
    def test_query_history_excludes_other_server(self, mon_db):
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        t1 = _iso(now - timedelta(minutes=10))
        t2 = _iso(now - timedelta(minutes=5))
        _seed_digest(conn, "0xHIST", "server_a", t1, 0.05, "SELECT * FROM members WHERE id=1")
        _seed_digest(conn, "0xHIST", "server_a", t2, 0.06, "SELECT * FROM members WHERE id=2")
        _seed_digest(conn, "0xHIST", "server_b", t1, 99.0, "SELECT * FROM members WHERE id=3")
        _seed_digest(conn, "0xHIST", "server_b", t2, 98.0, "SELECT * FROM members WHERE id=4")
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_query_history({"digest": "0xHIST"})
            finally:
                tools.set_current_server(None)

        assert result["data_points"] == 2
        avgs = {row["avg_time_sec"] for row in result["history"]}
        assert avgs == {0.05, 0.06}, f"server_b leaked into query history: {result['history']}"


class TestRunExplainServerScoping:
    def test_run_explain_cache_hit_scoped_to_server(self, mon_db):
        """EXPLAIN_FOR_DIGEST cache lookup must not cross servers."""
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        older = _iso(now - timedelta(minutes=10))   # server_a
        newer = _iso(now)                           # server_b -- NEWER captured_at:
                                                      # an unscoped ORDER BY captured_at
                                                      # DESC LIMIT 1 would return this.
        conn.execute(
            """INSERT INTO explain_captures
               (captured_at, server_id, digest, digest_text, schema_name, explain_json)
               VALUES (?,?,?,?,?,?)""",
            (older, "server_a", "0xCACHED", "SELECT ...", "db",
             json.dumps({"marker": "server_a_plan"})),
        )
        conn.execute(
            """INSERT INTO explain_captures
               (captured_at, server_id, digest, digest_text, schema_name, explain_json)
               VALUES (?,?,?,?,?,?)""",
            (newer, "server_b", "0xCACHED", "SELECT ...", "db",
             json.dumps({"marker": "server_b_plan"})),
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_run_explain({"digest": "0xCACHED"})
            finally:
                tools.set_current_server(None)

        assert result["source"] == "cached"
        assert result["explain"]["marker"] == "server_a_plan"

    def test_run_explain_live_fallback_uses_own_server_sample_text(self, mon_db, monkeypatch):
        """No cached capture -> the digest-text fallback + live EXPLAIN must use
        server_a's query_sample_text and server_a's production connection, never
        server_b's, even though both servers share the same digest."""
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        t1 = _iso(now - timedelta(minutes=10))
        _seed_digest(conn, "0xNOCACHE", "server_a", t1, 0.02,
                     "SELECT * FROM members WHERE server_a_marker = 1")
        _seed_digest(conn, "0xNOCACHE", "server_b", t1, 0.03,
                     "SELECT * FROM members WHERE server_b_marker = 1")
        conn.commit()

        captured = {}

        @contextlib.contextmanager
        def fake_get_prod_connection(server_id):
            captured["server_id"] = server_id

            class FakeCursor:
                def execute(self, sql, *a, **k):
                    captured["sql"] = sql

                def fetchone(self):
                    return {"EXPLAIN": json.dumps({"query_block": {}})}

            class FakeConn:
                def cursor(self, dictionary=True):
                    return FakeCursor()

            yield FakeConn()

        monkeypatch.setattr(tools, "get_prod_connection", fake_get_prod_connection)

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_run_explain({"digest": "0xNOCACHE"})
            finally:
                tools.set_current_server(None)

        assert result["source"] == "live"
        assert captured["server_id"] == "server_a"
        assert "server_a_marker" in captured["sql"]
        assert "server_b_marker" not in captured["sql"]


class TestGetTableSchemaServerScoping:
    def test_get_table_schema_excludes_other_server(self, mon_db):
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        older = _iso(now - timedelta(minutes=10))   # server_a
        newer = _iso(now)                           # server_b -- NEWER snapshot_time
        conn.execute(
            """INSERT INTO schema_snapshots
               (snapshot_time, server_id, table_schema, table_name, schema_hash,
                index_hash, create_stmt)
               VALUES (?,?,?,?,?,?,?)""",
            (older, "server_a", "db1", "t1", "ha", "ia",
             "CREATE TABLE t1 (id INT, a_marker INT)"),
        )
        conn.execute(
            """INSERT INTO schema_snapshots
               (snapshot_time, server_id, table_schema, table_name, schema_hash,
                index_hash, create_stmt)
               VALUES (?,?,?,?,?,?,?)""",
            (newer, "server_b", "db1", "t1", "hb", "ib",
             "CREATE TABLE t1 (id INT, b_marker INT)"),
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_table_schema({"schema_name": "db1", "table_name": "t1"})
            finally:
                tools.set_current_server(None)

        assert result["source"] == "snapshot"
        assert "a_marker" in result["create_statement"]
        assert "b_marker" not in result["create_statement"]


class TestSearchSlowLogServerScoping:
    def test_search_slow_log_excludes_other_server(self, mon_db):
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO slow_query_log
               (snapshot_time, server_id, user, host, query_time_sec, lock_time_sec,
                rows_sent, rows_examined, sql_text)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_iso(now), "server_a", "app", "10.0.0.1", 1.5, 0.1, 1, 1000,
             "SELECT * FROM members WHERE server_a_marker = 1"),
        )
        conn.execute(
            """INSERT INTO slow_query_log
               (snapshot_time, server_id, user, host, query_time_sec, lock_time_sec,
                rows_sent, rows_examined, sql_text)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_iso(now), "server_b", "app", "10.0.0.2", 9.5, 0.9, 1, 90000,
             "SELECT * FROM members WHERE server_b_marker = 1"),
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_search_slow_log({"keyword": "members"})
            finally:
                tools.set_current_server(None)

        assert result["match_count"] == 1
        assert "server_a_marker" in result["entries"][0]["sql"]


class TestRecentAnalysesServerScoping:
    def test_recent_analyses_excludes_other_server(self, mon_db):
        conn, db_path = mon_db
        now = datetime.now(timezone.utc)
        conn.execute(
            """INSERT INTO agent_analyses
               (analyzed_at, server_id, analysis_type, severity, findings, recommendations)
               VALUES (?,?,?,?,?,?)""",
            (_iso(now), "server_a", "routine", "info",
             json.dumps(["server_a finding"]), json.dumps([])),
        )
        conn.execute(
            """INSERT INTO agent_analyses
               (analyzed_at, server_id, analysis_type, severity, findings, recommendations)
               VALUES (?,?,?,?,?,?)""",
            (_iso(now), "server_b", "routine", "critical",
             json.dumps(["server_b finding"]), json.dumps([])),
        )
        conn.commit()

        with _config_for(db_path):
            tools.set_current_server("server_a")
            try:
                result = tools._tool_get_recent_analyses({})
            finally:
                tools.set_current_server(None)

        assert result["count"] == 1
        assert result["analyses"][0]["findings"] == ["server_a finding"]
