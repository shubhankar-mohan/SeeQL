"""
MCP-5 smoke tests: resources + prompts register correctly and render
valid content.
"""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

import config as config_module
from storage.connection import reset_connections


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestResourcesRegistered:
    def test_concrete_and_templated(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            resources = _loop_run(mcp.list_resources())
            templates = _loop_run(mcp.list_resource_templates())
            uris = {str(r.uri) for r in resources}
            tmpls = {t.uriTemplate for t in templates}
            assert "seeql://servers" in uris
            assert "seeql://investigations/recent" in uris
            assert "seeql://incidents/recent" in uris
            assert "seeql://investigations/{investigation_id}" in tmpls
            assert "seeql://incidents/{incident_id}" in tmpls
            assert "seeql://state/{server_id}.md" in tmpls

    def test_read_servers_resource(self, mon_db):
        conn, db_path = mon_db
        conn.execute(
            "INSERT INTO servers (server_id, display_name, environment, role, "
            "host, port, is_active, created_at, updated_at) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("prod-a", "A", "production", "primary", "h", 3306, 1, _iso(), _iso()),
        )
        conn.commit()
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            contents = _loop_run(mcp.read_resource("seeql://servers"))
            # read_resource returns list of ReadResourceContents
            # (namedtuple-like) — extract the text/json payload.
            blobs = list(contents)
            assert len(blobs) >= 1
            first = blobs[0]
            body = getattr(first, "content", None) or getattr(first, "text", None)
            parsed = json.loads(body)
            ids = [r["server_id"] for r in parsed]
            assert "prod-a" in ids


class TestPromptsRegistered:
    def test_five_prompts(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            prompts = _loop_run(mcp.list_prompts())
            names = {p.name for p in prompts}
            assert names == {
                "seeql/rca",
                "seeql/review_investigation",
                "seeql/explain_digest",
                "seeql/schema_audit",
                "seeql/investigate_window",
            }

    def test_rca_prompt_renders_without_server(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            result = _loop_run(mcp.get_prompt("seeql/rca", {}))
            msgs = result.messages
            assert len(msgs) >= 1
            body = msgs[0].content.text
            # No server pre-specified → prompt must tell the LLM to discover
            assert "seeql_list_servers" in body

    def test_rca_prompt_with_server(self, mon_db):
        _, db_path = mon_db
        with _config_for(db_path):
            from mcp_server.server import create_server
            mcp = create_server()
            result = _loop_run(mcp.get_prompt("seeql/rca", {"server": "prod-a"}))
            body = result.messages[0].content.text
            assert "prod-a" in body
