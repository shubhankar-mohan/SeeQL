"""
Tests for MCP live-MySQL tools (MCP-3).

Strategy: mock `get_prod_connection` through `agent.tools` — same
mocking path used by the investigator tests. Verify:
  - each live tool dispatches to the correct agent.tools handler
  - the MCPSafety budget is consumed
  - the budget cap rejects once exhausted
  - the rate limiter rejects per-tool flooding
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

import config as config_module
from storage.connection import reset_connections


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _config_for(db_path, mcp_cfg=None):
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "mcp": mcp_cfg or {"budget": {"live_calls_per_session": 3, "tools_per_minute": 100}},
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


class TestLiveTools:
    def test_processlist_dispatches_and_records_budget(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.live as lmod
            monkeypatch.setattr(lmod, "_default_server", lambda: "prod")

            # Mock the live MySQL path at the source.
            from agent import tools as agent_tools
            monkeypatch.setattr(
                agent_tools, "get_prod_connection",
                lambda server_id=None: _mock_prod_cursor([
                    {"thread_id": 1, "pid": 10, "user": "app", "db": "mydb",
                     "command": "Query", "state": "executing",
                     "time_sec": 5, "query": "SELECT 1"},
                ]),
            )

            from mcp_server.safety import MCPSafety
            from mcp_server.server import create_server
            safety = MCPSafety(live_calls_per_session=5, tools_per_minute=100)
            mcp = create_server(safety=safety)

            res = _call(mcp, "seeql_get_live_processlist", {"server": "prod"})
            # Handler returns a dict with the result structure
            assert isinstance(res, dict)
            # Budget should have been incremented
            assert safety.snapshot()["live_used"] == 1

    def test_budget_exhaustion_rejects(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.live as lmod
            monkeypatch.setattr(lmod, "_default_server", lambda: "prod")
            from agent import tools as agent_tools
            monkeypatch.setattr(
                agent_tools, "get_prod_connection",
                lambda server_id=None: _mock_prod_cursor([]),
            )
            from mcp_server.safety import MCPSafety
            from mcp_server.server import create_server
            safety = MCPSafety(live_calls_per_session=1, tools_per_minute=100)
            mcp = create_server(safety=safety)

            ok = _call(mcp, "seeql_get_live_locks", {"server": "prod"})
            assert not (isinstance(ok, dict) and ok.get("rejected_by") == "mcp_safety")

            rejected = _call(mcp, "seeql_get_live_processlist", {"server": "prod"})
            assert isinstance(rejected, dict)
            assert rejected.get("rejected_by") == "mcp_safety"
            assert "live-MySQL tools" in rejected["error"]

    def test_rate_limit_rejects_floods(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(db_path):
            import mcp_server.tools.live as lmod
            monkeypatch.setattr(lmod, "_default_server", lambda: "prod")
            from agent import tools as agent_tools
            monkeypatch.setattr(
                agent_tools, "get_prod_connection",
                lambda server_id=None: _mock_prod_cursor([]),
            )
            from mcp_server.safety import MCPSafety
            from mcp_server.server import create_server
            safety = MCPSafety(
                live_calls_per_session=100, tools_per_minute=2,
            )
            mcp = create_server(safety=safety)

            # Two are allowed, the third hits the per-tool rate bucket.
            for _ in range(2):
                r = _call(mcp, "seeql_get_live_processlist", {"server": "prod"})
                assert r.get("rejected_by") is None
            third = _call(mcp, "seeql_get_live_processlist", {"server": "prod"})
            assert third.get("rejected_by") == "mcp_safety"
            assert "rate limit" in third["error"].lower()

    def test_server_allowlist_blocks_live_tool(self, mon_db, monkeypatch):
        _, db_path = mon_db
        with _config_for(
            db_path,
            mcp_cfg={
                "allowed_servers": ["allowed-only"],
                "budget": {"live_calls_per_session": 10, "tools_per_minute": 100},
            },
        ):
            import mcp_server.tools.live as lmod
            monkeypatch.setattr(lmod, "_default_server", lambda: "allowed-only")
            from mcp_server.server import create_server
            mcp = create_server()
            res = _call(mcp, "seeql_get_live_locks", {"server": "OTHER"})
            assert isinstance(res, dict)
            assert res.get("rejected_by") == "mcp_safety"
            assert "allowed_servers" in res["error"]
