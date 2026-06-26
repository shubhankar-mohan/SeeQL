"""
Tests for the streamable-HTTP transport's bearer-token middleware (MCP-6).

Uses Starlette's TestClient — no real network required.
"""

import pytest
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route


@pytest.fixture
def base_app():
    """A minimal Starlette app with one route — used to verify middleware
    independently of FastMCP's streamable_http wiring (which has its own
    session requirements)."""
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])


class TestBearerMiddleware:
    def test_missing_token_rejected(self, base_app):
        from mcp_server.server import _wrap_with_auth
        app = _wrap_with_auth(base_app, "bearer", "s3cret")
        client = TestClient(app)
        r = client.get("/mcp")
        assert r.status_code == 401
        assert "invalid_token" in r.text

    def test_wrong_token_rejected(self, base_app):
        from mcp_server.server import _wrap_with_auth
        app = _wrap_with_auth(base_app, "bearer", "s3cret")
        client = TestClient(app)
        r = client.get("/mcp", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_correct_token_passes(self, base_app):
        from mcp_server.server import _wrap_with_auth
        app = _wrap_with_auth(base_app, "bearer", "s3cret")
        client = TestClient(app)
        r = client.get("/mcp", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200
        assert r.text == "ok"

    def test_case_insensitive_bearer_token(self, base_app):
        from mcp_server.server import _wrap_with_auth
        app = _wrap_with_auth(base_app, "bearer", "s3cret")
        client = TestClient(app)
        r = client.get("/mcp", headers={"Authorization": "bearer s3cret"})
        assert r.status_code == 200

    def test_none_mode_passes_without_token(self, base_app):
        from mcp_server.server import _wrap_with_auth
        wrapped = _wrap_with_auth(base_app, "none", None)
        # `none` mode returns the app unchanged — auth is a no-op.
        client = TestClient(wrapped)
        r = client.get("/mcp")
        assert r.status_code == 200

    def test_bearer_without_secret_raises(self, base_app):
        from mcp_server.server import _wrap_with_auth
        with pytest.raises(ValueError, match="non-empty auth_token"):
            _wrap_with_auth(base_app, "bearer", "")

    def test_unknown_mode_raises(self, base_app):
        from mcp_server.server import _wrap_with_auth
        with pytest.raises(ValueError, match="unsupported mcp.http.auth"):
            _wrap_with_auth(base_app, "basic", "x")

    def test_malformed_bearer_header_rejected(self, base_app):
        from mcp_server.server import _wrap_with_auth
        app = _wrap_with_auth(base_app, "bearer", "s3cret")
        client = TestClient(app)
        # Missing "Bearer " prefix
        r = client.get("/mcp", headers={"Authorization": "s3cret"})
        assert r.status_code == 401
        # Three tokens
        r = client.get("/mcp", headers={"Authorization": "Bearer extra s3cret"})
        assert r.status_code == 401


class TestInsecureBindingWarning:
    def test_loopback_with_none_no_warning(self, caplog):
        from mcp_server.server import _warn_if_insecure_binding
        import logging
        caplog.set_level(logging.WARNING)
        _warn_if_insecure_binding("127.0.0.1", "none")
        assert not any("auth=none" in r.message for r in caplog.records)

    def test_public_bind_with_none_warns(self, caplog):
        from mcp_server.server import _warn_if_insecure_binding
        import logging
        caplog.set_level(logging.WARNING)
        _warn_if_insecure_binding("0.0.0.0", "none")
        assert any("auth=none" in r.message for r in caplog.records)
