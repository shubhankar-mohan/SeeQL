"""
Coverage for MCP-2 read-only tools. Each test seeds the SQLite fixture,
then invokes the tool via FastMCP's call_tool dispatcher.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import pytest

import config as config_module
from storage.connection import reset_connections
from storage import writer


def _iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat()


@contextmanager
def _config_for(db_path):
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "mcp": {},
    }
    reset_connections()
    try:
        yield
    finally:
        config_module._config = prev
        reset_connections()


def _call(mcp, tool_name, arguments=None):
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(mcp.call_tool(tool_name, arguments or {}))
        if isinstance(result, tuple):
            content, structured = result
        else:
            content, structured = result, None
        if structured is not None:
            return structured
        for block in content or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    return json.loads(text)
                except Exception:
                    return text
        return None
    finally:
        loop.close()


def _unwrap(result):
    """FastMCP sometimes wraps list results in {'result': [...]}."""
    if isinstance(result, dict) and "result" in result and isinstance(result["result"], list):
        return result["result"]
    return result


# ---------------------------------------------------------------------------
# Investigations / incidents
# ---------------------------------------------------------------------------

class TestGetInvestigation:
    def test_detail_has_all_sections(self, mon_db):
        conn, db_path = mon_db
        with _config_for(db_path):
            alert_id = writer.write_inbound_alert({
                "provider": "generic", "received_at": _iso(),
                "server_id": "prod", "external_id": "x1",
                "alert_type": "missing_index", "severity": "warning",
                "summary": "test", "payload": "{}", "signature_verified": 1,
            })
            inv_id = writer.write_investigation({
                "inbound_alert_id": alert_id, "server_id": "prod",
                "started_at": _iso(), "status": "phase3",
            })
            writer.write_investigation_findings([{
                "investigation_id": inv_id, "created_at": _iso(),
                "phase": 1, "kind": "hypothesis", "severity": "info",
                "content": json.dumps({"hypothesis": "x"}),
            }])
            writer.write_investigation_samples([{
                "investigation_id": inv_id, "sampled_at": _iso(),
                "sample_type": "processlist", "query_count": 1, "data": "[]",
            }])

            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_get_investigation", {"id": inv_id})
            assert "investigation" in res
            assert res["investigation"]["status"] == "phase3"
            assert len(res["findings"]) == 1
            assert res["findings"][0]["kind"] == "hypothesis"
            assert res["findings"][0]["content_parsed"]["hypothesis"] == "x"
            assert len(res["samples"]) == 1
            assert res["samples"][0]["sample_type"] == "processlist"

    def test_missing_returns_error(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_get_investigation", {"id": 99999})
            assert "error" in res


class TestIncidents:
    def test_list_and_get(self, mon_db):
        conn, db_path = mon_db
        with _config_for(db_path):
            # Seed incident + anomaly_event
            conn.execute(
                "INSERT INTO incident_windows (server_id, start_time, end_time, "
                "severity, involved_metrics, event_count, status) VALUES "
                "(?,?,?,?,?,?,?)",
                ("prod", _iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
                 _iso(), "warning", json.dumps(["cpu_utilization"]), 3, "detected"),
            )
            incident_id = conn.execute(
                "SELECT id FROM incident_windows ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO anomaly_events (detected_at, server_id, metric_name, "
                "current_value, baseline_mean, baseline_stddev, z_score, pct_change, "
                "direction, severity, incident_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (_iso(), "prod", "cpu_utilization", 0.95, 0.4, 0.1, 5.5, 137.5,
                 "high", "warning", incident_id),
            )
            conn.commit()

            from mcp_server.server import create_server
            mcp = create_server()

            lst = _unwrap(_call(mcp, "seeql_list_incidents", {"server": "prod"}))
            assert isinstance(lst, list) and len(lst) == 1
            assert lst[0]["involved_metrics"] == ["cpu_utilization"]

            detail = _call(mcp, "seeql_get_incident", {"id": incident_id})
            assert detail["window"]["status"] == "detected"
            assert len(detail["anomaly_events"]) == 1
            assert detail["anomaly_events"][0]["metric_name"] == "cpu_utilization"


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

class TestQueries:
    def _seed_digest(self, conn, digest, rows_examined, rows_sent,
                     server_id="prod", snapshot_time=None):
        conn.execute(
            """INSERT INTO query_digest_snapshots
               (server_id, snapshot_time, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
                rows_examined, rows_sent, rows_affected,
                full_scans, no_index_used)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (server_id, snapshot_time or _iso(), digest,
             f"SELECT * FROM members WHERE x=?", "db",
             100, 10.0, 0.1, 1.0, 0.01,
             rows_examined, rows_sent, 0, 0, 0),
        )

    def test_top_queries_by_ratio(self, mon_db, monkeypatch):
        conn, db_path = mon_db
        with _config_for(db_path):
            # Stub default server — otherwise resolver needs a registry entry.
            import mcp_server.tools.query as qmod
            monkeypatch.setattr(qmod, "_default_server", lambda: "prod")

            # Identical snapshot_time so MAX() returns the same timestamp and
            # both rows are visible to the top_queries query.
            ts = _iso()
            self._seed_digest(conn, "0xHIGH", 10000, 10, snapshot_time=ts)   # ratio 1000
            self._seed_digest(conn, "0xLOW",   10,    10, snapshot_time=ts)  # ratio 1
            conn.commit()
            from mcp_server.server import create_server
            mcp = create_server()
            res = _unwrap(_call(mcp, "seeql_top_queries",
                                {"metric": "ratio", "server": "prod", "limit": 5}))
            assert isinstance(res, list) and len(res) >= 1
            assert res[0]["digest"] == "0xHIGH"

    def test_top_queries_unknown_metric(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.query as qmod
            monkeypatch.setattr(qmod, "_default_server", lambda: "prod")
            from mcp_server.server import create_server
            mcp = create_server()
            res = _unwrap(_call(mcp, "seeql_top_queries",
                                {"metric": "nonexistent", "server": "prod"}))
            # Returns a list with a single error record
            assert isinstance(res, list) and "error" in res[0]

    def test_search_slow_log_empty(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_search_slow_log",
                        {"keyword": "SELECT", "limit": 5})
            # agent.tools._tool_search_slow_log returns a dict with "queries"
            assert isinstance(res, dict)


# ---------------------------------------------------------------------------
# Schema / DDL / indexes
# ---------------------------------------------------------------------------

class TestSchema:
    def test_list_unused_indexes(self, mon_db, monkeypatch):
        conn, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.schema as smod
            monkeypatch.setattr(smod, "_default_server", lambda: "prod")
            conn.execute(
                "INSERT INTO unused_index_snapshots (server_id, snapshot_time, "
                "object_schema, table_name, index_name) VALUES (?,?,?,?,?)",
                ("prod", _iso(), "db", "members", "idx_old"),
            )
            conn.commit()
            from mcp_server.server import create_server
            mcp = create_server()
            res = _unwrap(_call(mcp, "seeql_list_unused_indexes", {"server": "prod"}))
            assert isinstance(res, list) and len(res) == 1
            assert res[0]["index_name"] == "idx_old"

    def test_recent_ddl_changes(self, mon_db, monkeypatch):
        conn, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.schema as smod
            monkeypatch.setattr(smod, "_default_server", lambda: "prod")
            conn.execute(
                "INSERT INTO ddl_changes (detected_at, server_id, table_schema, "
                "table_name, change_type, old_schema_hash, new_schema_hash, "
                "old_index_hash, new_index_hash, old_ddl, new_ddl) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?)",
                (_iso(), "prod", "db", "members", "index",
                 "oh", "nh", "oih", "nih", "CREATE TABLE m (a int)", "CREATE TABLE m (a int, b int)"),
            )
            conn.commit()
            from mcp_server.server import create_server
            mcp = create_server()
            res = _unwrap(_call(mcp, "seeql_get_recent_ddl_changes",
                                {"hours": 24, "server": "prod"}))
            assert isinstance(res, list) and len(res) == 1
            assert res[0]["table_name"] == "members"
            assert res[0]["change_type"] == "index"


# ---------------------------------------------------------------------------
# Correlator
# ---------------------------------------------------------------------------

class TestCorrelators:
    def test_find_missing_index_empty(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.correlators as cmod
            monkeypatch.setattr(cmod, "_default_server", lambda: "prod")
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_find_missing_index_candidates",
                        {"server": "prod", "top_n": 3})
            assert isinstance(res, dict)
            assert res.get("has_findings") is False
            assert "No missing-index signals" in res["markdown"]
