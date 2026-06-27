"""
SeeQL MCP server entry point.

`create_server()` builds a FastMCP app with all tools / resources / prompts
registered. `run_stdio()` runs it as a stdio subprocess (the shape Claude
Desktop expects). `run_http()` runs it over streamable HTTP/SSE.

stdio and HTTP share the same tool surface — the transport just changes
how bytes move. Safety rails apply identically.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from mcp_server.safety import MCPSafety, load_safety_from_config

logger = logging.getLogger(__name__)


SERVER_INSTRUCTIONS = """\
SeeQL — MySQL RCA over Model Context Protocol.

This server exposes SeeQL's monitoring data so you can investigate MySQL
issues without leaving your chat session. Typical flow:

  1. seeql_list_servers                  → discover which servers are monitored
  2. seeql_get_state_report(server=X)    → current state snapshot (rich)
  3. seeql_list_investigations(...)      → any ongoing RCA work?
  4. (more tools in later phases of build-out)

Read-only tools are free. Live-MySQL tools are budgeted per session.
Action tools (trigger_investigation, etc.) are off by default.
"""


def create_server(
    name: str = "seeql",
    safety: MCPSafety | None = None,
) -> Any:
    """
    Build a FastMCP app with all registered tools, resources, and prompts.

    Returns the FastMCP instance (callers should not assume a specific
    class — just that it has `.run()`, `.run_stdio_async()`,
    `.run_streamable_http_async()`, `.host`, `.port`, etc.).
    """
    from mcp.server.fastmcp import FastMCP

    if safety is None:
        safety = load_safety_from_config()

    mcp = FastMCP(
        name=name,
        instructions=SERVER_INSTRUCTIONS,
    )

    # Tool registration
    from mcp_server.tools import register_all as register_all_tools
    register_all_tools(mcp, safety)

    # Resources + prompts (MCP-5)
    from mcp_server import resources as _resources
    from mcp_server import prompts as _prompts
    _resources.register(mcp)
    _prompts.register(mcp)

    return mcp


def run_stdio(name: str = "seeql") -> None:
    """Run the server on stdio. Blocks until the client disconnects."""
    mcp = create_server(name=name)
    logger.info("Starting SeeQL MCP server (stdio)")
    mcp.run()


def run_http(
    name: str = "seeql",
    host: str = "127.0.0.1",
    port: int = 8765,
    auth: str | None = None,
    auth_token: str | None = None,
) -> None:
    """
    Run the server over streamable HTTP.

    `auth` is one of: `"bearer"` (require `Authorization: Bearer <token>`
    where <token> must match `auth_token`) or `"none"` (no auth — a loud
    warning is logged if `host` isn't a loopback).

    When called without explicit kwargs, values are read from the `mcp.http`
    config section.
    """
    mcp = create_server(name=name)

    # Config overrides (only where caller didn't supply explicit values)
    try:
        from config import get_config
        http_cfg = dict((get_config().get("mcp") or {}).get("http") or {})
    except Exception:
        http_cfg = {}
    if auth is None:
        auth = str(http_cfg.get("auth", "bearer"))
    if auth_token is None:
        auth_token = http_cfg.get("auth_token")

    _warn_if_insecure_binding(host, auth)

    mcp.host = host
    mcp.port = port

    app = _wrap_with_auth(mcp.streamable_http_app(), auth, auth_token)
    logger.info(f"Starting SeeQL MCP server (http) on {host}:{port} auth={auth}")
    _serve_starlette(app, host, port)


# ---------------------------------------------------------------------------
# Auth + serving helpers
# ---------------------------------------------------------------------------

def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def _warn_if_insecure_binding(host: str, auth: str) -> None:
    if auth != "none":
        return
    if _is_loopback(host):
        return
    logger.warning(
        f"MCP HTTP bound to {host} with auth=none — anyone on the network "
        f"can call SeeQL tools. Enable bearer auth for non-loopback binds."
    )


def _wrap_with_auth(app, auth: str, token: str | None):
    """Starlette middleware — verifies bearer token on every MCP request."""
    if auth == "none":
        return app
    if auth != "bearer":
        raise ValueError(f"unsupported mcp.http.auth: {auth!r} (use 'bearer' or 'none')")
    if not token:
        raise ValueError(
            "mcp.http.auth=bearer requires a non-empty auth_token. "
            "Set SEEQL_MCP_TOKEN in the environment or mcp.http.auth_token "
            "in settings.yaml."
        )

    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    class _BearerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # Static HTTP endpoints like /health could be carved out here;
            # FastMCP's streamable_http_app currently only exposes /mcp so
            # every request goes through the token check.
            auth_header = request.headers.get("authorization") or ""
            parts = auth_header.split()
            # Constant-time compare — `==` short-circuits and leaks the token
            # via response-timing differences.
            ok = (
                len(parts) == 2
                and parts[0].lower() == "bearer"
                and hmac.compare_digest(parts[1], token)
            )
            if not ok:
                return JSONResponse(
                    {"error": "invalid_token"},
                    status_code=401,
                    headers={"WWW-Authenticate": 'Bearer realm="seeql-mcp"'},
                )
            return await call_next(request)

    app.add_middleware(_BearerMiddleware)
    return app


def _serve_starlette(app, host: str, port: int) -> None:
    """Run a Starlette app under uvicorn. Blocking."""
    import uvicorn
    uvicorn.run(app, host=host, port=port, log_level="info")
