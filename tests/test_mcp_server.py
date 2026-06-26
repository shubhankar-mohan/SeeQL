"""
Tests for mcp_server — MCP-1 surface.

Two layers:

1. **Direct tool invocation** — the @mcp.tool-decorated functions are also
   callable Python functions. We call them directly through FastMCP's
   `call_tool` so every code path (safety, impl, serialization) runs, but
   we skip the stdio handshake to keep tests fast and deterministic.

2. **Safety smoke** — separate test file `test_mcp_safety.py` covers
   allowlist, budget, rate limit, action gate.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import config as config_module
from storage.connection import reset_connections
from storage import writer


@contextmanager
def _config_for(db_path, mcp_cfg=None):
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "mcp": mcp_cfg or {},
    }
    reset_connections()
    try:
        yield
    finally:
        config_module._config = prev
        reset_connections()


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _call(mcp, tool_name, arguments=None):
    """Call an MCP tool through FastMCP's dispatcher and return the parsed payload."""
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(mcp.call_tool(tool_name, arguments or {}))
        # FastMCP.call_tool returns (content, structured_content) in 1.27.
        # Older versions return just content. Handle both.
        if isinstance(result, tuple):
            content, structured = result
        else:
            content, structured = result, None
        if structured is not None:
            return structured
        # Fall back to parsing the first text block as JSON.
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


class TestServerBuild:
    def test_create_server_registers_three_tools(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            loop = asyncio.new_event_loop()
            try:
                tools = loop.run_until_complete(mcp.list_tools())
            finally:
                loop.close()
            names = {t.name for t in tools}
            assert "seeql_list_servers" in names
            assert "seeql_get_state_report" in names
            assert "seeql_list_investigations" in names


class TestListServers:
    def test_empty(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_list_servers", {})
            # FastMCP wraps list returns — accept either list or {"result": [...]}
            rows = res if isinstance(res, list) else res.get("result", res)
            assert isinstance(rows, list)
            assert rows == []

    def test_returns_seeded_servers(self, mon_db):
        conn, db_path = mon_db
        conn.execute(
            "INSERT INTO servers (server_id, display_name, environment, role, "
            "host, port, is_active, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("prod-primary", "Prod Primary", "production", "primary",
             "10.0.0.1", 3306, 1, _iso(), _iso()),
        )
        conn.execute(
            "INSERT INTO servers (server_id, display_name, environment, role, "
            "host, port, is_active, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("prod-replica", "Prod Replica", "production", "replica",
             "10.0.0.2", 3306, 1, _iso(), _iso()),
        )
        conn.commit()

        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_list_servers", {})
            rows = res if isinstance(res, list) else res.get("result", res)
            ids = sorted(r["server_id"] for r in rows)
            assert ids == ["prod-primary", "prod-replica"]


class TestListInvestigations:
    def test_empty(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_list_investigations", {"limit": 10})
            rows = res if isinstance(res, list) else res.get("result", res)
            assert rows == []

    def test_rows_returned(self, mon_db):
        conn, db_path = mon_db
        with _config_for(db_path):
            # Seed one inbound_alert + investigation
            alert_id = writer.write_inbound_alert({
                "provider": "generic", "received_at": _iso(),
                "server_id": "prod", "external_id": "e-1",
                "alert_type": "missing_index", "severity": "warning",
                "summary": "mcp test", "payload": "{}", "signature_verified": 1,
            })
            writer.write_investigation({
                "inbound_alert_id": alert_id, "server_id": "prod",
                "started_at": _iso(), "status": "phase3",
                "confidence": 0.5,
            })
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_list_investigations", {"limit": 10})
            rows = res if isinstance(res, list) else res.get("result", res)
            assert len(rows) == 1
            r = rows[0]
            assert r["provider"] == "generic"
            assert r["alert_type"] == "missing_index"
            assert r["status"] == "phase3"
            assert r["external_id"] == "e-1"

    def test_status_filter(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            for status in ("phase3", "completed"):
                aid = writer.write_inbound_alert({
                    "provider": "generic", "received_at": _iso(),
                    "server_id": "prod", "external_id": f"e-{status}",
                    "alert_type": "default", "severity": "info",
                    "summary": status, "payload": "{}", "signature_verified": 1,
                })
                writer.write_investigation({
                    "inbound_alert_id": aid, "server_id": "prod",
                    "started_at": _iso(), "status": status,
                })
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_list_investigations",
                        {"status": "completed", "limit": 10})
            rows = res if isinstance(res, list) else res.get("result", res)
            assert len(rows) == 1
            assert rows[0]["status"] == "completed"


class TestGetStateReport:
    def test_returns_markdown(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path):
            # Stub resolve_server so we don't need a ServerRegistry
            import mcp_server.tools.state as state_mod
            monkeypatch.setattr(state_mod, "_resolve_server", lambda s: s or "default")

            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_get_state_report", {"server": "default"})
            assert isinstance(res, dict)
            assert "markdown" in res
            assert "data" in res
            assert res["server_id"] == "default"


class TestSafetyAppliesToTools:
    def test_disallowed_server_rejected(self, mon_db, monkeypatch):
        conn, db_path = mon_db
        # Seed a server so list_servers has something to work with.
        conn.execute(
            "INSERT INTO servers (server_id, display_name, environment, role, "
            "host, port, is_active, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("allowed", "A", "production", "primary", "h", 3306, 1, _iso(), _iso()),
        )
        conn.commit()
        with _config_for(db_path, mcp_cfg={"allowed_servers": ["allowed"]}):
            import mcp_server.tools.state as state_mod
            monkeypatch.setattr(state_mod, "_resolve_server", lambda s: s or "allowed")

            from mcp_server.server import create_server
            mcp = create_server()
            # Explicitly target a non-allowed server
            res = _call(mcp, "seeql_get_state_report", {"server": "NOT-ALLOWED"})
            assert isinstance(res, dict)
            assert res.get("rejected_by") == "mcp_safety"
            assert "allowed_servers" in res.get("error", "")

    def test_allowed_server_passes(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path, mcp_cfg={"allowed_servers": ["allowed"]}):
            import mcp_server.tools.state as state_mod
            monkeypatch.setattr(state_mod, "_resolve_server", lambda s: s or "allowed")

            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_get_state_report", {"server": "allowed"})
            assert isinstance(res, dict)
            # Should reach the impl, not the rejection path
            assert res.get("rejected_by") is None
            assert "markdown" in res
