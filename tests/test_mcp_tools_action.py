"""
Tests for action tools (MCP-4). Verifies the gate rejects by default,
allows when both flags are on, and that the happy path actually wires
through to the investigator / writer layer.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import config as config_module
from storage.connection import reset_connections
from storage import writer


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _config_for(db_path, mcp_cfg):
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "mcp": mcp_cfg,
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


class TestDefaultGate:
    def test_trigger_rejected_by_default(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path, {}):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_trigger_investigation",
                        {"alert_type": "missing_index"})
            assert isinstance(res, dict)
            assert res.get("rejected_by") == "mcp_safety"
            assert "action_tools_enabled" in res["error"].lower() or \
                   "mcp.action_tools_enabled" in res["error"]

    def test_abort_rejected_by_default(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path, {}):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_abort_investigation", {"id": 1})
            assert res.get("rejected_by") == "mcp_safety"

    def test_explain_query_rejected_by_default(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path, {}):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_explain_query", {"sql": "SELECT 1"})
            assert res.get("rejected_by") == "mcp_safety"


class TestPartialGate:
    def test_master_on_sub_off_still_rejected(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path, {
            "action_tools_enabled": True,
            "allow_trigger": False,
        }):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_trigger_investigation",
                        {"alert_type": "missing_index"})
            assert res.get("rejected_by") == "mcp_safety"
            assert "allow_trigger" in res["error"]


class TestTriggerHappyPath:
    def test_creates_rows_and_enqueues(self, mon_db, monkeypatch):
        conn, db_path = mon_db
        with _config_for(db_path, {
            "action_tools_enabled": True,
            "allow_trigger": True,
        }):
            import mcp_server.tools.action as amod
            monkeypatch.setattr(amod, "_default_server", lambda: "prod")
            # Replace run_investigation with a no-op captured.
            captured = []
            import alerting.investigator as INV
            monkeypatch.setattr(
                INV, "run_investigation",
                lambda inv_id: captured.append(inv_id) or {"status": "stubbed"},
            )

            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_trigger_investigation", {
                "alert_type": "missing_index",
                "severity": "warning",
                "server": "prod",
                "summary": "mcp trigger test",
            })
            assert res["status"] == "accepted"
            inv_id = res["investigation_id"]
            assert inv_id >= 1

            # Verify DB rows
            inv = conn.execute(
                "SELECT * FROM investigations WHERE id = ?", (inv_id,)
            ).fetchone()
            assert inv["status"] == "queued"
            alert = conn.execute(
                "SELECT * FROM inbound_alerts WHERE id = ?",
                (inv["inbound_alert_id"],),
            ).fetchone()
            assert alert["provider"] == "mcp"
            assert alert["alert_type"] == "missing_index"
            assert alert["signature_verified"] == 0

            # Daemon thread may or may not have run yet; capture may be empty
            # or contain inv_id. Either way the row was created correctly.


class TestAbortHappyPath:
    def test_abort_sets_status(self, mon_db):
        conn, db_path = mon_db
        with _config_for(db_path, {
            "action_tools_enabled": True,
            "allow_abort": True,
        }):
            # Seed a running investigation
            alert_id = writer.write_inbound_alert({
                "provider": "generic", "received_at": _iso(),
                "server_id": "prod", "external_id": "a1",
                "alert_type": "lock_cascade", "severity": "critical",
                "summary": "test", "payload": "{}", "signature_verified": 1,
            })
            inv_id = writer.write_investigation({
                "inbound_alert_id": alert_id, "server_id": "prod",
                "started_at": _iso(), "status": "phase3",
            })

            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_abort_investigation",
                        {"id": inv_id, "reason": "user_ack"})
            assert res["aborted"] is True
            row = conn.execute(
                "SELECT status, abort_reason, ended_at FROM investigations WHERE id = ?",
                (inv_id,),
            ).fetchone()
            assert row["status"] == "aborted"
            assert row["abort_reason"] == "user_ack"
            assert row["ended_at"] is not None

    def test_abort_missing_returns_error(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path, {
            "action_tools_enabled": True,
            "allow_abort": True,
        }):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_abort_investigation", {"id": 99999})
            assert "error" in res


class TestExplainQueryGate:
    def test_flagged_on_still_budget_capped(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path, {
            "action_tools_enabled": True,
            "allow_explain_query": True,
            "budget": {"explain_calls_per_session": 1, "tools_per_minute": 100},
        }):
            import mcp_server.tools.action as amod
            monkeypatch.setattr(amod, "_default_server", lambda: "prod")
            from agent import tools as agent_tools

            # Stub the handler — we're testing the gate + budget, not MySQL.
            stub_calls = []
            def stub_explain(input_data):
                stub_calls.append(input_data)
                return {"plan": "stub", "query": input_data.get("query")}
            monkeypatch.setattr(agent_tools, "_tool_explain_query", stub_explain)

            from mcp_server.server import create_server
            mcp = create_server()
            ok = _call(mcp, "seeql_explain_query",
                       {"sql": "SELECT 1", "server": "prod"})
            assert ok.get("rejected_by") is None
            assert ok["plan"] == "stub"

            # Second call hits the session cap
            rejected = _call(mcp, "seeql_explain_query",
                             {"sql": "SELECT 2", "server": "prod"})
            assert rejected.get("rejected_by") == "mcp_safety"
            assert "EXPLAIN" in rejected["error"]
