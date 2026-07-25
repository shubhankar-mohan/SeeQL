"""Optional bearer-token auth for the HTTP API (P0-8).

The API ships with no auth by default and binds 0.0.0.0 — fine on a private
network, a liability once the container's port is published. `resolve_auth_token`
reads `api.auth_token` (treating an unsubstituted ${VAR} placeholder as unset,
the project-wide convention — see agent/llm_agent.py::_cfg_value). Until a
real token is configured, `BearerTokenMiddleware` is never installed and every
route behaves exactly as it does today.

Once configured, every POST (collection triggers, LLM analysis, alert tests)
requires `Authorization: Bearer <token>`. `/health` and `/metrics` always
stay open for probes and Prometheus scrapers, and `/webhooks/*` stays open
because it has its own per-provider HMAC authentication (external providers
never send a bearer token). GET reads under `/api/v1` are additionally gated
when `api.protect_reads` is set — the dashboard and `/data/*` stay open
regardless, since those are meant for same-host/reverse-proxy consumption.
"""

import hmac
import logging

from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

OPEN_PATHS = {"/health", "/metrics"}


def resolve_auth_token(api_config: dict | None) -> str | None:
    """Read `auth_token` from an `api:` config dict, treating an
    unsubstituted ${VAR} placeholder (or an empty string) as unset.

    Guards against a non-string value: an unquoted all-numeric YAML token
    (`auth_token: 483920104857`) parses as an int, which would later blow up
    `hmac.compare_digest(str, int)` with a TypeError (500 on every request).
    Anything that isn't a str is treated as unset."""
    token = (api_config or {}).get("auth_token")
    if not isinstance(token, str):
        return None
    if token.startswith("${"):
        return None
    return token or None


class BearerTokenMiddleware:
    """Optional bearer auth. Enforced on all POSTs (+ /api/v1 reads when
    protect_reads) once api.auth_token is configured. /health and /metrics
    stay open for probes and Prometheus."""

    def __init__(self, app, token: str, protect_reads: bool = False):
        self.app, self.token, self.protect_reads = app, token, protect_reads

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self._needs_auth(scope):
            headers = dict(scope.get("headers") or [])
            # Compare raw bytes, never str: a malformed (non-UTF-8)
            # Authorization header must yield a clean 401, never a 500. A
            # str path can't do this safely — .decode() raises
            # UnicodeDecodeError on bad bytes, and even .decode(errors=...)
            # then feeds a non-ASCII str to hmac.compare_digest, which
            # rejects non-ASCII str with a TypeError. compare_digest handles
            # arbitrary bytes with no such restriction, in constant time.
            supplied = headers.get(b"authorization") or b""
            expected = b"Bearer " + self.token.encode()
            if not hmac.compare_digest(supplied, expected):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _needs_auth(self, scope) -> bool:
        path = scope.get("path", "")
        if path in OPEN_PATHS:
            return False
        # /webhooks/* authenticates via per-provider HMAC signatures
        # (api/webhook_routes.py) — external providers never send a bearer
        # token, so layering the bearer gate on top only 401s real webhooks
        # with no security gain.
        if path.startswith("/webhooks"):
            return False
        if scope.get("method") == "POST":
            return True
        return self.protect_reads and path.startswith("/api/v1")
