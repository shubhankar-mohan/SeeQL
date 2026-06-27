"""
Action tools — all gated behind `mcp.action_tools_enabled` plus a
per-tool flag. Off by default; operators must explicitly opt in.

- seeql_trigger_investigation  — enqueue a new investigation (CLI-equivalent)
- seeql_abort_investigation    — abort a running investigation
- seeql_explain_query          — EXPLAIN an arbitrary SELECT against production
"""

import json
import logging
import threading
from datetime import datetime, timezone

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_trigger_investigation",
        description=(
            "Trigger a new webhook-style investigation. Creates an inbound_alerts "
            "row (provider=mcp) + investigations row, then schedules the "
            "orchestrator. Useful when a human or the MCP client wants RCA "
            "for a specific alert type without going through a real external "
            "provider. Gated behind mcp.action_tools_enabled + mcp.allow_trigger."
        ),
    )
    def trigger_tool(
        alert_type: str,
        severity: str = "warning",
        server: str | None = None,
        summary: str = "Triggered via MCP",
    ) -> dict:
        def _impl(
            alert_type=alert_type,
            severity=severity,
            server=server,
            summary=summary,
        ):
            return _trigger_impl(alert_type, severity, server, summary)
        return wrap_tool(safety, "seeql_trigger_investigation", _impl)(
            alert_type=alert_type,
            severity=severity,
            server=server,
            summary=summary,
        )

    @mcp.tool(
        name="seeql_abort_investigation",
        description=(
            "Abort a running investigation. Sets status='aborted' with the "
            "given reason. Gated behind mcp.action_tools_enabled + "
            "mcp.allow_abort."
        ),
    )
    def abort_tool(id: int, reason: str = "mcp_abort") -> dict:
        def _impl(id=id, reason=reason):
            return _abort_impl(id, reason)
        return wrap_tool(safety, "seeql_abort_investigation", _impl)(
            id=id, reason=reason,
        )

    @mcp.tool(
        name="seeql_explain_query",
        description=(
            "Run EXPLAIN (or EXPLAIN FORMAT=JSON) on an arbitrary SELECT "
            "against the production database. High risk — gated behind "
            "mcp.action_tools_enabled + mcp.allow_explain_query, and counts "
            "against the per-session explain_calls budget. Prefer "
            "seeql_run_explain (cached) for digests SeeQL already tracks."
        ),
    )
    def explain_query_tool(
        sql: str,
        schema: str | None = None,
        server: str | None = None,
    ) -> dict:
        def _impl(sql=sql, schema=schema, server=server):
            return _explain_query_impl(sql, schema, server)
        return wrap_tool(safety, "seeql_explain_query", _impl)(
            sql=sql, schema=schema, server=server,
        )


# ---------------------------------------------------------------------------
# Impls
# ---------------------------------------------------------------------------

def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trigger_impl(
    alert_type: str, severity: str, server: str | None, summary: str,
) -> dict:
    from storage import writer
    from alerting.investigator import run_investigation
    sid = server or _default_server()

    now = _iso()
    alert_id = writer.write_inbound_alert({
        "provider": "mcp",
        "received_at": now,
        "server_id": sid,
        "external_id": f"mcp:{now}",
        "alert_type": alert_type,
        "severity": severity,
        "summary": summary,
        "payload": json.dumps({"origin": "mcp"}),
        "signature_verified": 0,
    })
    inv_id = writer.write_investigation({
        "inbound_alert_id": alert_id,
        "server_id": sid,
        "started_at": now,
        "status": "queued",
    })

    # Try to schedule via the running APScheduler; fall back to a daemon
    # thread (same pattern as api/webhook_routes._enqueue_investigation).
    _enqueue(inv_id)

    return {
        "investigation_id": inv_id,
        "inbound_alert_id": alert_id,
        "alert_type": alert_type,
        "server_id": sid,
        "status": "accepted",
    }


def _abort_impl(investigation_id: int, reason: str) -> dict:
    from storage import writer
    # Guarded update: only non-terminal rows are aborted, so a completed or
    # already-aborted investigation is never clobbered (rc == 0).
    rc = writer.abort_investigation(
        investigation_id,
        reason=reason,
        ended_at=_iso(),
    )
    if rc == 0:
        return {
            "aborted": False,
            "investigation_id": investigation_id,
            "error": (
                f"investigation {investigation_id} not found or already "
                "in a terminal state"
            ),
        }
    return {"aborted": True, "investigation_id": investigation_id, "reason": reason}


def _explain_query_impl(
    sql: str, schema: str | None, server: str | None,
) -> dict:
    # Delegate to the existing agent.tools handler, which already enforces
    # SELECT/WITH-only validation, MAX_EXECUTION_TIME, retries, etc.
    from agent import tools as agent_tools
    sid = server or _default_server()
    agent_tools.set_current_server(sid)
    payload = {"query": sql}
    if schema:
        payload["schema"] = schema
    return agent_tools._tool_explain_query(payload)


def _enqueue(investigation_id: int) -> None:
    try:
        from scheduler.runner import _scheduler_instance
        from alerting.investigator import run_investigation
    except Exception as e:
        logger.warning(f"MCP trigger: orchestrator import failed: {e}")
        return

    if _scheduler_instance is None:
        def _bg():
            try:
                run_investigation(investigation_id)
            except Exception as e:
                logger.exception(f"inline MCP investigation {investigation_id}: {e}")
        t = threading.Thread(target=_bg, daemon=True,
                             name=f"mcp-inv-{investigation_id}")
        t.start()
        return

    try:
        from apscheduler.triggers.date import DateTrigger
        _scheduler_instance.add_job(
            run_investigation,
            trigger=DateTrigger(run_date=datetime.now(timezone.utc)),
            args=[investigation_id],
            id=f"investigation:{investigation_id}",
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(f"MCP trigger: scheduler add_job failed: {e}")


def _default_server() -> str:
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
