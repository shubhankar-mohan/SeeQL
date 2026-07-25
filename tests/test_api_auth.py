"""Tests for optional bearer-auth on the HTTP API (Task 5.1 / P0-8).

The bug: the API ships with zero auth and binds 0.0.0.0, and the Docker
quickstart publishes the port. An unauthenticated POST /collect/* (loads
prod MySQL), POST /api/v1/agent/analyze (spends LLM budget), POST
/api/v1/alerts/test (spams Slack), and the full read surface are exposed.

These tests pin the fix: with `api.auth_token` configured, every POST
requires `Authorization: Bearer <token>`, while `/health` and `/metrics`
stay open for probes/scrapers and dashboard GETs stay open by default
(`api.protect_reads: false`). With no token configured (the shipped
default), behavior is unchanged from today, but `main.py::cmd_api` logs a
startup warning when bound off-loopback.
"""

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import config as config_module
from api.app import create_app
from storage.connection import reset_connections

SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"
TOKEN = "s3cret-test-token"


def _init_schema(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL_PATH.read_text())
    conn.commit()
    conn.close()


def _mock_prod_connection_cm():
    """A context-manager MagicMock production connection whose cursor
    returns no rows — enough for /collect/fast to run all four fast
    collectors cleanly (mirrors TestCollectEndpoints in test_api.py)."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.cursor.return_value = mock_cursor
    cm = MagicMock()
    cm.__enter__.return_value = mock_conn
    cm.__exit__.return_value = False
    return cm


@pytest.fixture
def make_client(tmp_path, test_config):
    """Factory: build a TestClient from `create_app()` with an `api:` config
    override, so each test controls auth_token / protect_reads directly.

    Passing `api_cfg=None` omits the `api:` key entirely, to guard the case
    where a deployment's config predates this feature.
    """
    counter = {"n": 0}

    def _make(api_cfg=None):
        cfg = dict(test_config)
        cfg["monitoring_db"] = dict(cfg["monitoring_db"])
        counter["n"] += 1
        db_path = tmp_path / f"auth_test_{counter['n']}.db"
        cfg["monitoring_db"]["path"] = str(db_path)
        if api_cfg is not None:
            cfg["api"] = api_cfg
        _init_schema(db_path)
        config_module._config = cfg
        app = create_app()
        return TestClient(app)

    yield _make
    reset_connections()


class TestTokenConfiguredPostsGated:
    """api.auth_token set -> every POST needs `Authorization: Bearer <token>`."""

    @pytest.fixture
    def client(self, make_client):
        return make_client({"auth_token": TOKEN, "protect_reads": False})

    def test_post_without_header_rejected(self, client):
        resp = client.post("/collect/fast")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_post_with_wrong_token_rejected(self, client):
        resp = client.post("/collect/fast", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_post_with_malformed_header_rejected(self, client):
        # Missing the "Bearer " prefix entirely.
        resp = client.post("/collect/fast", headers={"Authorization": TOKEN})
        assert resp.status_code == 401


    @patch("collectors.fast_loop.writer")
    @patch("storage.connection.get_prod_connection")
    def test_post_with_correct_token_allowed(self, mock_get_conn, mock_writer, client):
        import config.server_registry as sr
        sr._registry = None
        mock_get_conn.return_value = _mock_prod_connection_cm()

        resp = client.post("/collect/fast", headers={"Authorization": f"Bearer {TOKEN}"})
        assert resp.status_code == 200
        assert resp.json()["loop"] == "fast"


class TestTokenConfiguredProbesStayOpen:
    """/health and /metrics must stay reachable with no header at all —
    liveness probes and Prometheus scrapers don't carry a bearer token."""

    @pytest.fixture
    def client(self, make_client):
        return make_client({"auth_token": TOKEN, "protect_reads": True})

    @patch("api.routes.check_prod_connection", return_value=True)
    @patch("api.routes.check_mon_connection", return_value=True)
    def test_health_open(self, mock_mon, mock_prod, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_metrics_open(self, client):
        resp = client.get("/metrics")
        assert resp.status_code != 401


class TestWebhooksExemptFromBearer:
    """`POST /webhooks/{provider}` authenticates via per-provider HMAC
    signatures (api/webhook_routes.py), not the API bearer token. External
    providers (GCP/PagerDuty/Grafana) never send `Authorization: Bearer`, so
    layering the bearer gate on top would silently 401 every real webhook
    once an operator sets api.auth_token (review IMPORTANT 2). The middleware
    must exempt /webhooks entirely."""

    @pytest.fixture
    def client(self, make_client):
        # protect_reads True to prove the exemption isn't just the POST rule.
        return make_client({"auth_token": TOKEN, "protect_reads": True})

    def test_webhook_post_not_bearer_gated(self, client):
        # No Authorization header at all. If the bearer gate applied, this
        # would be a 401 with {"error": "unauthorized"}. Instead it must reach
        # the route, which (webhooks disabled by default) returns 404.
        resp = client.post("/webhooks/generic", json={"any": "payload"})
        assert resp.status_code != 401
        assert resp.json() == {"detail": "webhooks disabled"}

    def test_webhook_post_reaches_route_with_bearer_too(self, client):
        # Sending a valid bearer must also not interfere — still reaches route.
        resp = client.post(
            "/webhooks/generic",
            json={"any": "payload"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert resp.status_code == 404


class TestProtectReadsDefaultFalse:
    """protect_reads: false (the shipped default) -> GETs, including
    /api/v1/*, are not gated even though a token is configured."""

    @pytest.fixture
    def client(self, make_client):
        return make_client({"auth_token": TOKEN, "protect_reads": False})

    def test_api_v1_get_open_without_header(self, client):
        resp = client.get("/api/v1/incidents/recent")
        assert resp.status_code == 200

    def test_dashboard_get_open_without_header(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_data_get_open_without_header(self, client):
        resp = client.get("/data/queries")
        assert resp.status_code == 200


class TestProtectReadsTrue:
    """protect_reads: true -> /api/v1/* GETs also require the token. Other
    GET surfaces (dashboard, /data/*) are unaffected — the middleware's
    _needs_auth only ever gates paths under /api/v1."""

    @pytest.fixture
    def client(self, make_client):
        return make_client({"auth_token": TOKEN, "protect_reads": True})

    def test_api_v1_get_without_header_rejected(self, client):
        resp = client.get("/api/v1/incidents/recent")
        assert resp.status_code == 401
        assert resp.json() == {"error": "unauthorized"}

    def test_api_v1_get_with_header_allowed(self, client):
        resp = client.get(
            "/api/v1/incidents/recent", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert resp.status_code == 200

    def test_dashboard_get_still_open(self, client):
        resp = client.get("/dashboard")
        assert resp.status_code == 200


class TestNoTokenConfigured:
    """The shipped default: api.auth_token resolves to None (an unset
    ${SEEQL_API_TOKEN} placeholder, an empty string, or no `api:` section at
    all) -> the middleware is never installed and every route behaves
    exactly as it does today."""

    @patch("collectors.fast_loop.writer")
    @patch("storage.connection.get_prod_connection")
    def test_unset_placeholder_leaves_api_open(self, mock_get_conn, mock_writer, make_client):
        import config.server_registry as sr
        sr._registry = None
        mock_get_conn.return_value = _mock_prod_connection_cm()

        client = make_client({"auth_token": "${SEEQL_API_TOKEN}", "protect_reads": False})
        resp = client.post("/collect/fast")
        assert resp.status_code == 200

    @patch("collectors.fast_loop.writer")
    @patch("storage.connection.get_prod_connection")
    def test_empty_string_token_leaves_api_open(self, mock_get_conn, mock_writer, make_client):
        import config.server_registry as sr
        sr._registry = None
        mock_get_conn.return_value = _mock_prod_connection_cm()

        client = make_client({"auth_token": ""})
        resp = client.post("/collect/fast")
        assert resp.status_code == 200

    def test_missing_api_section_leaves_api_open(self, make_client):
        """No `api:` key in config at all — create_app() must not crash."""
        client = make_client(None)
        resp = client.get("/data/queries")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_protect_reads_alone_without_token_has_no_effect(self, make_client):
        """protect_reads: true with no token configured must NOT lock down
        /api/v1 reads — the middleware is only installed once a token
        resolves, regardless of protect_reads."""
        client = make_client({"protect_reads": True})
        resp = client.get("/api/v1/incidents/recent")
        assert resp.status_code == 200


class TestMiddlewareRawBytes:
    """ASGI-level coverage: a client can put arbitrary bytes in a header, and
    the high-level TestClient/httpx can't even encode a non-UTF-8 str header
    to send it — so the decode-crash path (review IMPORTANT 1) has to be
    exercised by driving the middleware directly with a synthetic scope whose
    `authorization` header carries raw non-UTF-8 bytes."""

    def _run(self, auth_bytes):
        import asyncio

        from api.auth import BearerTokenMiddleware

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        mw = BearerTokenMiddleware(downstream, token="s3cret")
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/collect/fast",
            "headers": [(b"authorization", auth_bytes)],
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        asyncio.run(mw(scope, receive, send))
        start = next(m for m in sent if m["type"] == "http.response.start")
        return start["status"]

    def test_non_utf8_authorization_returns_401_not_crash(self):
        # \xff\xfe is invalid UTF-8; an unguarded .decode() would raise
        # UnicodeDecodeError (→ 500). errors="replace" must yield a clean 401.
        assert self._run(b"Bearer \xff\xfe") == 401

    def test_valid_token_still_passes_at_asgi_level(self):
        assert self._run(b"Bearer s3cret") == 200


class TestResolveAuthToken:
    """Unit coverage for resolve_auth_token's guards."""

    def test_placeholder_is_unset(self):
        from api.auth import resolve_auth_token

        assert resolve_auth_token({"auth_token": "${SEEQL_API_TOKEN}"}) is None

    def test_empty_string_is_unset(self):
        from api.auth import resolve_auth_token

        assert resolve_auth_token({"auth_token": ""}) is None

    def test_missing_key_is_unset(self):
        from api.auth import resolve_auth_token

        assert resolve_auth_token({}) is None
        assert resolve_auth_token(None) is None

    def test_non_string_token_treated_as_unset(self):
        """An unquoted all-numeric YAML token parses as int; comparing it via
        hmac.compare_digest(str, int) would 500 on every request. A non-str
        token must resolve to None (review MINOR 3)."""
        from api.auth import resolve_auth_token

        assert resolve_auth_token({"auth_token": 483920104857}) is None

    def test_real_string_token_passes_through(self):
        from api.auth import resolve_auth_token

        assert resolve_auth_token({"auth_token": "real-token"}) == "real-token"


class TestStartupBindWarning:
    """main.py::cmd_api must warn loudly when bound off-loopback with no
    token configured — mirrors the MCP HTTP transport's equivalent check
    (mcp_server/server.py::_warn_if_insecure_binding)."""

    def test_public_bind_no_token_warns(self, caplog):
        from main import _warn_if_api_bind_insecure

        caplog.set_level(logging.WARNING)
        _warn_if_api_bind_insecure("0.0.0.0", None)
        assert any("api.auth_token" in r.message for r in caplog.records)

    def test_loopback_bind_no_token_no_warning(self, caplog):
        from main import _warn_if_api_bind_insecure

        caplog.set_level(logging.WARNING)
        _warn_if_api_bind_insecure("127.0.0.1", None)
        assert not any("api.auth_token" in r.message for r in caplog.records)

    def test_public_bind_with_token_no_warning(self, caplog):
        from main import _warn_if_api_bind_insecure

        caplog.set_level(logging.WARNING)
        _warn_if_api_bind_insecure("0.0.0.0", "some-token")
        assert not any("api.auth_token" in r.message for r in caplog.records)
