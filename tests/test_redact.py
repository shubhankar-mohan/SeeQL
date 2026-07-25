"""Tests for agent/redact.py — SQL-literal redaction (P0-9, P2-7).

`redact_sql` masks literal values (strings, numbers, hex blobs) in raw SQL
text while preserving structure (keywords, identifiers, table/column names).
`maybe_redact` wraps it with the `agent.redact_sql_literals` config gate
(default: on).

The `Test*Redaction`/`TestSearchSlowLog`/`TestExplainQueryRedaction` classes
below are apply-site tests: they prove `maybe_redact` is actually wired into
state_builder's trx_query render and the live/slow-log/explain tool result
fields — not just that the helper function works in isolation.
"""

from unittest.mock import MagicMock

import config as config_module
from agent.redact import maybe_redact, redact_sql


class TestRedactSql:
    def test_redacts_literals(self):
        assert redact_sql("SELECT * FROM users WHERE email = 'a@b.com' AND id = 42") == \
            "SELECT * FROM users WHERE email = '?' AND id = ?"

    def test_preserves_structure(self):
        assert "JOIN pirates" in redact_sql(
            "UPDATE bounties b JOIN pirates p ON p.id=b.pirate_id SET b.amount = 100"
        )

    def test_redacts_hex_blob(self):
        redacted = redact_sql("SELECT * FROM sessions WHERE token = 0x1A2B3F")
        assert "0x1A2B3F" not in redacted
        assert "SELECT * FROM sessions WHERE token = ?" == redacted

    def test_redacts_multiple_string_literals(self):
        redacted = redact_sql("SELECT * FROM t WHERE a = 'foo' AND b = 'bar'")
        assert redacted == "SELECT * FROM t WHERE a = '?' AND b = '?'"

    def test_redacts_double_quoted_string(self):
        # In default MySQL sql_mode "..." is a string literal, not an
        # identifier — so it must be masked too (IMPORTANT-3).
        assert redact_sql('SELECT * FROM t WHERE email = "a@b.com"') == \
            "SELECT * FROM t WHERE email = '?'"

    def test_handles_none(self):
        assert redact_sql(None) is None

    def test_handles_empty_string(self):
        assert redact_sql("") == ""


class TestMaybeRedact:
    def _set_agent_config(self, **agent_overrides):
        config_module._config = {"agent": agent_overrides}

    def test_default_on_when_key_absent(self):
        """No redact_sql_literals key at all -> still redacts (privacy-first default)."""
        self._set_agent_config()
        assert maybe_redact("SELECT * FROM t WHERE id = 42") == "SELECT * FROM t WHERE id = ?"

    def test_enabled_explicit_true_masks(self):
        self._set_agent_config(redact_sql_literals=True)
        assert maybe_redact("SELECT * FROM t WHERE id = 42") == "SELECT * FROM t WHERE id = ?"

    def test_disabled_explicit_false_passes_through(self):
        self._set_agent_config(redact_sql_literals=False)
        assert maybe_redact("SELECT * FROM t WHERE id = 42") == "SELECT * FROM t WHERE id = 42"

    def test_none_passthrough_regardless_of_config(self):
        self._set_agent_config(redact_sql_literals=True)
        assert maybe_redact(None) is None


# ---------------------------------------------------------------------------
# Apply-site tests
# ---------------------------------------------------------------------------

def _mock_prod_cursor(rows):
    """Build a context-manager mock for get_prod_connection returning `rows`."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.execute.return_value = None
    mock_cursor.fetchall.return_value = list(rows)
    mock_cursor.fetchone.return_value = rows[0] if rows else None
    mock_conn.cursor.return_value = mock_cursor

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=mock_conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


class TestStateBuilderTrxQueryRedaction:
    """agent/state_builder.py — trx_query is redacted at CONSTRUCTION time
    (_build_current_state), not at render time, so BOTH to_markdown() and
    to_dict() inherit the mask (P0-9: to_dict() previously returned the raw
    statement text verbatim even though to_markdown() was already masked —
    mcp_server/tools/state.py::seeql_get_state_report and
    GET /api/v1/state-report both return to_dict() to external callers).

    These tests seed transaction_snapshots and call the real
    _build_current_state() (not a hand-built StateReport) precisely because
    the bug/fix lives at that construction site, not in the renderer.
    """

    @staticmethod
    def _seed_long_transaction(conn, trx_query):
        conn.execute(
            "INSERT INTO transaction_snapshots "
            "(snapshot_time, server_id, trx_id, trx_state, age_sec, pid, "
            " trx_query, rows_locked, rows_modified, isolation_level) "
            "VALUES ('2026-07-20T00:00:00', 'default', '1', 'RUNNING', 40, 99, "
            " ?, 3, 1, 'REPEATABLE-READ')",
            (trx_query,),
        )
        conn.commit()

    def test_masked_by_default_in_dict_and_markdown(self, mon_db):
        conn, db_path = mon_db
        self._seed_long_transaction(
            conn, "SELECT * FROM users WHERE email = 'leak@example.com'"
        )
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {},
        }
        from agent.state_builder import StateReport, _build_current_state
        current_state = _build_current_state(conn, long_txn_sec=10, server_id="default")

        # Construction-time redaction: the dict itself is already masked,
        # before it's ever wrapped in a StateReport.
        trx_query = current_state["long_transactions"][0]["trx_query"]
        assert "leak@example.com" not in trx_query
        assert "'?'" in trx_query

        report = StateReport(current_state=current_state, changes={}, historical={})

        # Both representations inherit the same masked value.
        md = report.to_markdown()
        assert "leak@example.com" not in md
        assert "'?'" in md

        dict_trx_query = report.to_dict()["current_state"]["long_transactions"][0]["trx_query"]
        assert "leak@example.com" not in dict_trx_query
        assert "'?'" in dict_trx_query

    def test_raw_when_disabled(self, mon_db):
        conn, db_path = mon_db
        self._seed_long_transaction(
            conn, "SELECT * FROM users WHERE email = 'leak@example.com'"
        )
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {"redact_sql_literals": False},
        }
        from agent.state_builder import StateReport, _build_current_state
        current_state = _build_current_state(conn, long_txn_sec=10, server_id="default")
        assert "leak@example.com" in current_state["long_transactions"][0]["trx_query"]

        report = StateReport(current_state=current_state, changes={}, historical={})
        assert "leak@example.com" in report.to_markdown()
        assert "leak@example.com" in \
            report.to_dict()["current_state"]["long_transactions"][0]["trx_query"]

    def test_digest_text_not_redacted(self, mon_db):
        """digest_text (performance_schema DIGEST_TEXT) is an already-
        parameterized fingerprint (`?` placeholders), not raw statement
        text with literals — construction-time redaction must leave it
        alone, or the LLM loses the query-shape structure it needs for
        RCA (acceptance-grounding: masking literals must not strip
        structure)."""
        conn, db_path = mon_db
        conn.execute(
            "INSERT INTO query_digest_snapshots "
            "(snapshot_time, server_id, digest, digest_text, schema_name, "
            " exec_count, total_time_sec, avg_time_sec, rows_examined, rows_sent) "
            "VALUES ('2026-07-20T00:00:00', 'default', 'abc123', "
            " 'SELECT * FROM users WHERE id = ?', 'shop', 10, 1.0, 0.1, 100, 10)"
        )
        conn.commit()
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {},
        }
        from agent.state_builder import _build_current_state
        current_state = _build_current_state(conn, long_txn_sec=10, server_id="default")
        assert current_state["top_queries"][0]["digest_text"] == "SELECT * FROM users WHERE id = ?"
        assert current_state["top_queries"][0]["digest"] == "abc123"


class TestLiveToolRedaction:
    """agent/tools.py — get_live_processlist/get_live_locks/get_live_transactions."""

    def test_live_transactions_trx_query_masked_by_default(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([
                {"trx_id": "1", "trx_state": "RUNNING", "age_sec": 5, "pid": 10,
                 "trx_query": "SELECT * FROM users WHERE ssn = 42",
                 "operation_state": None, "tables_in_use": 1, "tables_locked": 1,
                 "rows_locked": 0, "rows_modified": 0, "isolation_level": "REPEATABLE-READ"},
            ]),
        )
        result = agent_tools._tool_get_live_transactions({})
        assert result["transactions"][0]["trx_query"] == "SELECT * FROM users WHERE ssn = ?"

    def test_live_transactions_trx_query_raw_when_disabled(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {"redact_sql_literals": False}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([
                {"trx_id": "1", "trx_state": "RUNNING", "age_sec": 5, "pid": 10,
                 "trx_query": "SELECT * FROM users WHERE ssn = 42",
                 "operation_state": None, "tables_in_use": 1, "tables_locked": 1,
                 "rows_locked": 0, "rows_modified": 0, "isolation_level": "REPEATABLE-READ"},
            ]),
        )
        result = agent_tools._tool_get_live_transactions({})
        assert result["transactions"][0]["trx_query"] == "SELECT * FROM users WHERE ssn = 42"

    def test_live_processlist_query_masked_by_default(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([
                {"pid": 10, "user": "app", "db": "shop", "command": "Query",
                 "state": "executing", "time_sec": 5,
                 "query": "SELECT * FROM cards WHERE pan = '4111111111111111'"},
            ]),
        )
        result = agent_tools._tool_get_live_processlist({})
        assert "4111111111111111" not in result["processes"][0]["query"]
        assert result["processes"][0]["query"] == "SELECT * FROM cards WHERE pan = '?'"

    def test_live_locks_waiting_and_blocking_query_masked_by_default(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([
                {"waiting_trx_id": "1", "waiting_pid": 10,
                 "waiting_query": "SELECT * FROM orders WHERE id = 55",
                 "wait_seconds": 3, "blocking_trx_id": "2", "blocking_pid": 20,
                 "blocking_query": "UPDATE orders SET status = 'shipped' WHERE id = 55",
                 "blocking_trx_age_sec": 10, "blocking_rows_locked": 1,
                 "blocking_rows_modified": 1},
            ]),
        )
        result = agent_tools._tool_get_live_locks({})
        row = result["lock_waits"][0]
        assert row["waiting_query"] == "SELECT * FROM orders WHERE id = ?"
        assert row["blocking_query"] == "UPDATE orders SET status = '?' WHERE id = ?"


class TestSearchSlowLog:
    """agent/tools.py — search_slow_log sql field + slow_log_tool_enabled gate."""

    @staticmethod
    def _seed(conn, sql_text):
        conn.execute(
            "INSERT INTO slow_query_log "
            "(snapshot_time, server_id, user, host, query_time_sec, lock_time_sec, "
            " rows_sent, rows_examined, sql_text) "
            "VALUES ('2026-07-01T00:00:00', 'default', 'app', '10.0.0.1', "
            " 2.5, 0.1, 1, 500, ?)",
            (sql_text,),
        )
        conn.commit()

    def test_masks_sql_by_default(self, mon_db):
        conn, db_path = mon_db
        self._seed(conn, "SELECT * FROM t WHERE email = 'x@y.com'")
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_search_slow_log({"keyword": "email"})
        assert result["entries"][0]["sql"] == "SELECT * FROM t WHERE email = '?'"

    def test_raw_when_disabled(self, mon_db):
        conn, db_path = mon_db
        self._seed(conn, "SELECT * FROM t WHERE email = 'x@y.com'")
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {"redact_sql_literals": False},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_search_slow_log({"keyword": "email"})
        assert result["entries"][0]["sql"] == "SELECT * FROM t WHERE email = 'x@y.com'"

    def test_disabled_tool_returns_explanatory_error(self, mon_db):
        conn, db_path = mon_db
        self._seed(conn, "SELECT * FROM t WHERE email = 'x@y.com'")
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {"slow_log_tool_enabled": False},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_search_slow_log({"keyword": "email"})
        assert "error" in result
        assert "slow_log_tool_enabled" in result["error"]

    def test_enabled_by_default_when_key_absent(self, mon_db):
        conn, db_path = mon_db
        self._seed(conn, "SELECT * FROM t WHERE email = 'x@y.com'")
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_search_slow_log({"keyword": "email"})
        assert "error" not in result
        assert result["match_count"] == 1


class TestRunExplainRedaction:
    """agent/tools.py — _tool_run_explain's non-SELECT rejection echoes sql_text."""

    @staticmethod
    def _seed_digest(conn, digest, query_sample_text):
        conn.execute(
            "INSERT INTO query_digest_snapshots "
            "(snapshot_time, digest, digest_text, query_sample_text, schema_name) "
            "VALUES ('2026-07-01T00:00:00', ?, ?, ?, 'shop')",
            (digest, query_sample_text, query_sample_text),
        )
        conn.commit()

    def test_non_select_rejection_redacts_echoed_sql(self, mon_db):
        conn, db_path = mon_db
        self._seed_digest(conn, "abc123", "UPDATE users SET pwd = 'hunter2' WHERE id = 42")
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_run_explain({"digest": "abc123"})
        assert "hunter2" not in result["error"]
        assert "= 42" not in result["error"]

    def test_non_select_rejection_raw_when_disabled(self, mon_db):
        conn, db_path = mon_db
        self._seed_digest(conn, "abc123", "UPDATE users SET pwd = 'hunter2' WHERE id = 42")
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {"redact_sql_literals": False},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_run_explain({"digest": "abc123"})
        assert "hunter2" in result["error"]


class TestLockGraphRedaction:
    """agent/tools.py — _tool_get_lock_graph snapshot lock/txn query fields (CRITICAL-1)."""

    @staticmethod
    def _seed(conn):
        conn.execute(
            "INSERT INTO lock_wait_snapshots "
            "(snapshot_time, server_id, waiting_pid, blocking_pid, wait_seconds, "
            " waiting_query, blocking_query) "
            "VALUES ('2026-07-20T00:00:00', 'default', 10, 20, 5, ?, ?)",
            ("SELECT * FROM orders WHERE id = 55",
             "UPDATE orders SET status = 'shipped' WHERE customer = 'alice@x.com'"),
        )
        conn.execute(
            "INSERT INTO transaction_snapshots "
            "(snapshot_time, server_id, trx_id, trx_state, age_sec, pid, trx_query, "
            " rows_locked, rows_modified, isolation_level) "
            "VALUES ('2026-07-20T00:00:00', 'default', '1', 'RUNNING', 5, 10, ?, "
            " 0, 0, 'REPEATABLE-READ')",
            ("DELETE FROM sessions WHERE token = 'sekret'",),
        )
        conn.commit()

    def test_lock_and_txn_queries_masked_by_default(self, mon_db):
        conn, db_path = mon_db
        self._seed(conn)
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_get_lock_graph({})
        lock = result["lock_waits"][0]
        assert lock["waiting_query"] == "SELECT * FROM orders WHERE id = ?"
        assert lock["blocking_query"] == "UPDATE orders SET status = '?' WHERE customer = '?'"
        assert "alice@x.com" not in lock["blocking_query"]
        txn = result["active_transactions"][0]
        assert txn["trx_query"] == "DELETE FROM sessions WHERE token = '?'"
        assert "sekret" not in txn["trx_query"]

    def test_raw_when_disabled(self, mon_db):
        conn, db_path = mon_db
        self._seed(conn)
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
            "agent": {"redact_sql_literals": False},
        }
        from agent import tools as agent_tools
        result = agent_tools._tool_get_lock_graph({})
        assert "alice@x.com" in result["lock_waits"][0]["blocking_query"]
        assert "sekret" in result["active_transactions"][0]["trx_query"]


_INNODB_STATUS_TEXT = """=====================================
2026-07-20 12:00:00 INNODB MONITOR OUTPUT
=====================================
------------
TRANSACTIONS
------------
Trx id counter 42000
UPDATE t SET x='secret' WHERE id=42
------------
FILE I/O
------------
I/O thread 0 state: waiting
"""


class TestInnodbStatusRedaction:
    """agent/tools.py — _tool_get_live_innodb_status section text (CRITICAL-2)."""

    def test_section_text_masked_but_names_survive(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([("InnoDB", "", _INNODB_STATUS_TEXT)]),
        )
        result = agent_tools._tool_get_live_innodb_status({})
        sections = result["sections"]
        # Structure (section names) survives.
        assert "TRANSACTIONS" in sections
        assert "FILE I/O" in sections
        # The planted literal is masked.
        assert "secret" not in sections["TRANSACTIONS"]
        assert "'?'" in sections["TRANSACTIONS"]
        # Query structure still readable.
        assert "UPDATE t SET x=" in sections["TRANSACTIONS"]

    def test_section_text_raw_when_disabled(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {"redact_sql_literals": False}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([("InnoDB", "", _INNODB_STATUS_TEXT)]),
        )
        result = agent_tools._tool_get_live_innodb_status({})
        assert "secret" in result["sections"]["TRANSACTIONS"]


class TestExplainQueryRedaction:
    """agent/tools.py — _tool_explain_query's returned `query` echo field.

    The EXPLAIN itself (cursor.execute) always runs against MySQL with the
    real, unredacted query — only the echoed `query` field in the return
    dict is masked.
    """

    def test_query_echo_masked_by_default(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([
                {"EXPLAIN": '{"query_block": {}}'},
            ]),
        )
        result = agent_tools._tool_explain_query(
            {"query": "SELECT * FROM users WHERE id = 42"}
        )
        assert result["query"] == "SELECT * FROM users WHERE id = ?"

    def test_query_echo_raw_when_disabled(self, monkeypatch):
        from agent import tools as agent_tools
        config_module._config = {"agent": {"redact_sql_literals": False}}
        monkeypatch.setattr(
            agent_tools, "get_prod_connection",
            lambda server_id=None: _mock_prod_cursor([
                {"EXPLAIN": '{"query_block": {}}'},
            ]),
        )
        result = agent_tools._tool_explain_query(
            {"query": "SELECT * FROM users WHERE id = 42"}
        )
        assert result["query"] == "SELECT * FROM users WHERE id = 42"
