"""
MCP server safety rails.

Four complementary guards:

1. **Server allowlist** — tools accepting a `server` param reject servers
   not in `mcp.allowed_servers`. Empty list = any server SeeQL knows
   about is allowed.

2. **Session budget** — counts live-MySQL and explain_query calls against
   caps. Reuses the same mental model as `alerting.budget.Budget` (used by
   the webhook investigator). For MCP stdio there's exactly one session;
   for HTTP we use a module-level budget for MVP (per-session is a
   follow-up).

3. **Action-tool gate** — trigger/abort/explain_query are off by default.
   Individual flags allow enabling each without unlocking everything.

4. **Rate limiter** — per-tool-name token bucket to stop a runaway Claude
   loop from hammering any single tool (including snapshot tools).

All guards return structured error messages the MCP client sees as normal
tool errors — the LLM naturally backs off, same pattern `agent.tools`
already uses for other tool failures.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


# Same classification as alerting.budget — kept in sync manually (tools
# don't change often). Snapshot tools are always free (SQLite-only).
LIVE_TOOLS: set[str] = {
    "seeql_get_live_processlist",
    "seeql_get_live_locks",
    "seeql_get_live_transactions",
    "seeql_get_live_innodb_status",
    "seeql_get_index_stats",
    "seeql_get_table_status",
}
EXPENSIVE_TOOL = "seeql_explain_query"
ACTION_TOOLS: set[str] = {
    "seeql_trigger_investigation",
    "seeql_abort_investigation",
}


# ---------------------------------------------------------------------------
# MCPSafety — single object, constructed once per server instance
# ---------------------------------------------------------------------------

@dataclass
class MCPSafety:
    """Runtime-configurable safety context for the MCP server."""

    allowed_servers: list[str] = field(default_factory=list)
    live_calls_per_session: int = 30
    explain_calls_per_session: int = 5
    tools_per_minute: int = 60
    action_tools_enabled: bool = False
    allow_trigger: bool = False
    allow_abort: bool = False
    allow_explain_query: bool = False

    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _live_used: int = field(default=0, init=False)
    _explain_used: int = field(default=0, init=False)
    _buckets: dict[str, dict] = field(default_factory=dict, init=False)
    _session_started_at: float = field(default_factory=time.monotonic, init=False)

    # -----------------------------------------------------------------
    # Server allowlist
    # -----------------------------------------------------------------

    def check_server(self, server: str | None) -> None:
        """Raise ToolRejected if `server` isn't allowed."""
        if not self.allowed_servers:
            return  # empty = any server OK
        if server is None:
            return  # None means "use registry default"; resolver handles it
        if server not in self.allowed_servers:
            raise ToolRejected(
                f"server '{server}' is not in mcp.allowed_servers. "
                f"Allowed: {sorted(self.allowed_servers)}"
            )

    # -----------------------------------------------------------------
    # Session budget
    # -----------------------------------------------------------------

    def check_budget(self, tool_name: str) -> None:
        """Raise ToolRejected if `tool_name` is budget-exhausted."""
        with self._lock:
            if tool_name == EXPENSIVE_TOOL and self._explain_used >= self.explain_calls_per_session:
                raise ToolRejected(
                    f"session budget exhausted for EXPLAIN calls "
                    f"({self._explain_used}/{self.explain_calls_per_session}). "
                    f"Use seeql_run_explain (cached) instead."
                )
            if tool_name in LIVE_TOOLS and self._live_used >= self.live_calls_per_session:
                raise ToolRejected(
                    f"session budget exhausted for live-MySQL tools "
                    f"({self._live_used}/{self.live_calls_per_session}). "
                    f"Use read-only SQLite tools instead."
                )

    def record_call(self, tool_name: str) -> None:
        with self._lock:
            if tool_name == EXPENSIVE_TOOL:
                self._explain_used += 1
            elif tool_name in LIVE_TOOLS:
                self._live_used += 1

    # -----------------------------------------------------------------
    # Action-tool gate
    # -----------------------------------------------------------------

    def check_action(self, tool_name: str) -> None:
        if tool_name not in ACTION_TOOLS and tool_name != EXPENSIVE_TOOL:
            return
        if tool_name == EXPENSIVE_TOOL:
            if not (self.action_tools_enabled and self.allow_explain_query):
                raise ToolRejected(
                    "seeql_explain_query is disabled. Set "
                    "mcp.action_tools_enabled=true and mcp.allow_explain_query=true "
                    "to enable (arbitrary-SQL EXPLAIN is high-risk)."
                )
            return
        if not self.action_tools_enabled:
            raise ToolRejected(
                f"{tool_name} is disabled. Set mcp.action_tools_enabled=true "
                f"in settings.yaml to allow action tools."
            )
        if tool_name == "seeql_trigger_investigation" and not self.allow_trigger:
            raise ToolRejected("trigger_investigation is disabled (mcp.allow_trigger=false).")
        if tool_name == "seeql_abort_investigation" and not self.allow_abort:
            raise ToolRejected("abort_investigation is disabled (mcp.allow_abort=false).")

    # -----------------------------------------------------------------
    # Rate limiter (per-tool-name token bucket)
    # -----------------------------------------------------------------

    def check_rate(self, tool_name: str) -> None:
        if self.tools_per_minute <= 0:
            return
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(tool_name)
            if b is None:
                b = {"tokens": float(self.tools_per_minute), "last": now}
                self._buckets[tool_name] = b
            else:
                elapsed = now - b["last"]
                refill = elapsed * (self.tools_per_minute / 60.0)
                b["tokens"] = min(float(self.tools_per_minute), b["tokens"] + refill)
                b["last"] = now
            if b["tokens"] >= 1.0:
                b["tokens"] -= 1.0
                return
        raise ToolRejected(
            f"rate limit exceeded for {tool_name} "
            f"({self.tools_per_minute}/min). Slow down."
        )

    # -----------------------------------------------------------------
    # Combined gate — called from the tool wrapper
    # -----------------------------------------------------------------

    def authorize(self, tool_name: str, server: str | None) -> None:
        """
        Run all four checks in order. Raises ToolRejected on first failure.
        """
        self.check_rate(tool_name)
        self.check_action(tool_name)
        self.check_server(server)
        self.check_budget(tool_name)

    # -----------------------------------------------------------------
    # Introspection (used by tests + a `seeql_mcp_status` debug tool)
    # -----------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "live_used": self._live_used,
                "live_cap": self.live_calls_per_session,
                "explain_used": self._explain_used,
                "explain_cap": self.explain_calls_per_session,
                "action_tools_enabled": self.action_tools_enabled,
                "allow_trigger": self.allow_trigger,
                "allow_abort": self.allow_abort,
                "allow_explain_query": self.allow_explain_query,
                "allowed_servers": list(self.allowed_servers),
                "tools_per_minute": self.tools_per_minute,
                "session_age_seconds": int(time.monotonic() - self._session_started_at),
            }


# ---------------------------------------------------------------------------
# ToolRejected — the single shape tools raise when a guard fails
# ---------------------------------------------------------------------------

class ToolRejected(Exception):
    """Raised by MCPSafety when a tool call is refused.

    The MCP tool wrapper catches this and returns the message as a normal
    tool error result, so the client sees it like any other failure.
    """

    pass


# ---------------------------------------------------------------------------
# Wrapper helper — used by every tool registration
# ---------------------------------------------------------------------------

def wrap_tool(
    safety: MCPSafety,
    tool_name: str,
    fn: Callable[..., Any],
) -> Callable[..., Any]:
    """
    Wrap a tool callable so every invocation goes through MCPSafety.

    On ToolRejected: returns a dict `{"error": <msg>, "rejected_by": "mcp_safety"}`
    so the MCP client/LLM receives a structured error.
    """
    from functools import wraps

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        # Safety pre-checks. `server` may be either positional or keyword.
        server = kwargs.get("server")
        if server is None and args:
            # Don't assume positional; only use if the function's first param
            # is literally named 'server'. We leave that to introspection.
            pass
        try:
            safety.authorize(tool_name, server)
        except ToolRejected as e:
            logger.info(f"MCP tool {tool_name} rejected: {e}")
            return {"error": str(e), "rejected_by": "mcp_safety"}

        try:
            result = fn(*args, **kwargs)
            safety.record_call(tool_name)
            return result
        except ToolRejected as e:
            return {"error": str(e), "rejected_by": "mcp_safety"}
        except Exception as e:
            logger.exception(f"MCP tool {tool_name} raised: {e}")
            return {"error": str(e), "rejected_by": "tool_exception"}

    return _wrapped


# ---------------------------------------------------------------------------
# Config loader — consumed by server.create_server()
# ---------------------------------------------------------------------------

def load_safety_from_config() -> MCPSafety:
    try:
        from config import get_config
        cfg = dict((get_config().get("mcp") or {}))
    except Exception:
        cfg = {}

    budget = cfg.get("budget") or {}
    return MCPSafety(
        allowed_servers=list(cfg.get("allowed_servers") or []),
        live_calls_per_session=int(budget.get("live_calls_per_session", 30) or 30),
        explain_calls_per_session=int(budget.get("explain_calls_per_session", 5) or 5),
        tools_per_minute=int(budget.get("tools_per_minute", 60) or 60),
        action_tools_enabled=bool(cfg.get("action_tools_enabled", False)),
        allow_trigger=bool(cfg.get("allow_trigger", False)),
        allow_abort=bool(cfg.get("allow_abort", False)),
        allow_explain_query=bool(cfg.get("allow_explain_query", False)),
    )
