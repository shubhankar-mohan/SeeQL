"""
End-to-end stdio transport test: spawn `venv/bin/python main.py mcp` as a
subprocess and exercise the MCP protocol via the official client.

Verifies the full wire shape — `initialize`, `list_tools`, `call_tool` —
which the direct call_tool tests in test_mcp_server.py don't exercise.

Uses the `SEEQL_MON_DB_PATH` env override so the subprocess reads from
a temp DB instead of the repo default — no shared state.
"""

import asyncio
import json
import os
import pathlib
import sqlite3

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
PY = REPO_ROOT / "venv" / "bin" / "python"


pytestmark = pytest.mark.skipif(
    not PY.exists(),
    reason="venv python not present — run venv/bin/pip install first",
)


def _prepare_db(tmp_path: pathlib.Path) -> pathlib.Path:
    """Build a freshly schema'd SQLite DB with one seeded server row."""
    db_path = tmp_path / "mcp_e2e.db"
    schema = (REPO_ROOT / "storage" / "schema.sql").read_text()
    conn = sqlite3.connect(str(db_path))
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO servers (server_id, display_name, environment, role, "
        "host, port, is_active, created_at, updated_at) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("e2e-prod", "E2E Prod", "production", "primary",
         "1.2.3.4", 3306, 1,
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    return db_path


async def _run_stdio_exchange(db_path: pathlib.Path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client, StdioServerParameters

    env = dict(os.environ)
    env["SEEQL_MON_DB_PATH"] = str(db_path)
    env["SEEQL_LOG_LEVEL"] = "WARNING"  # keep stderr quiet

    params = StdioServerParameters(
        command=str(PY),
        args=[str(REPO_ROOT / "main.py"), "mcp"],
        env=env,
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}

            res = await session.call_tool("seeql_list_servers", {})
            payload = res.structuredContent
            if payload is None:
                for b in res.content:
                    t = getattr(b, "text", None)
                    if t:
                        try:
                            payload = json.loads(t)
                        except Exception:
                            payload = t
                        break
            return tool_names, payload


def test_stdio_transport_roundtrip(tmp_path):
    """Spawn seeql mcp as a subprocess, list tools, call seeql_list_servers."""
    db_path = _prepare_db(tmp_path)
    tool_names, payload = asyncio.run(_run_stdio_exchange(db_path))

    # Tool inventory
    assert "seeql_list_servers" in tool_names
    assert "seeql_get_state_report" in tool_names
    assert "seeql_list_investigations" in tool_names

    # Tool call returned the seeded server
    rows = payload if isinstance(payload, list) else payload.get("result", payload)
    ids = [r["server_id"] for r in rows]
    assert "e2e-prod" in ids
