"""
Replay tools — thin wrappers over agent.replay.run_replay.

- seeql_replay_incident  — replay an existing incident_window
- seeql_replay_window    — replay an arbitrary [from_ts, to_ts] window
"""

import logging

from mcp_server.safety import MCPSafety, wrap_tool

logger = logging.getLogger(__name__)


def register(mcp, safety: MCPSafety) -> None:
    @mcp.tool(
        name="seeql_replay_incident",
        description=(
            "Replay a specific incident window: build a chronological "
            "timeline of anomalies, lock waits, DDL changes, deadlocks, "
            "and thread samples in the window. Optionally invokes the LLM "
            "agent for root-cause narration (falls back to timeline-only "
            "when no LLM backend is configured)."
        ),
    )
    def replay_incident_tool(id: int) -> dict:
        def _impl(id: int):
            return _replay_incident_impl(id)
        return wrap_tool(safety, "seeql_replay_incident", _impl)(id=id)

    @mcp.tool(
        name="seeql_replay_window",
        description=(
            "Replay an arbitrary time window (ISO8601 timestamps). Same "
            "output shape as seeql_replay_incident but for free-form "
            "windows — useful when investigating something that didn't "
            "cluster into a formal incident."
        ),
    )
    def replay_window_tool(
        from_ts: str,
        to_ts: str,
        server: str | None = None,
    ) -> dict:
        def _impl(from_ts: str, to_ts: str, server=None):
            return _replay_window_impl(from_ts, to_ts, server)
        return wrap_tool(safety, "seeql_replay_window", _impl)(
            from_ts=from_ts, to_ts=to_ts, server=server,
        )


def _replay_incident_impl(incident_id: int) -> dict:
    from storage.connection import get_mon_reader
    with get_mon_reader() as conn:
        row = conn.execute(
            "SELECT server_id, start_time, end_time FROM incident_windows WHERE id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return {"error": f"incident {incident_id} not found"}

    return _invoke_replay(
        from_ts=row["start_time"],
        to_ts=row["end_time"],
        server_id=row["server_id"],
        incident_id=incident_id,
    )


def _replay_window_impl(
    from_ts: str, to_ts: str, server: str | None,
) -> dict:
    server_id = server or _default_server()
    return _invoke_replay(from_ts=from_ts, to_ts=to_ts, server_id=server_id)


def _invoke_replay(
    from_ts: str, to_ts: str, server_id: str, incident_id: int | None = None,
) -> dict:
    try:
        from agent.replay import run_replay
    except Exception as e:
        return {"error": f"replay module unavailable: {e}"}
    try:
        result = run_replay(
            from_ts=from_ts, to_ts=to_ts,
            server_id=server_id, incident_id=incident_id,
        )
    except Exception as e:
        return {"error": str(e)}

    return {
        "from_ts": result.from_ts,
        "to_ts": result.to_ts,
        "server_id": result.server_id,
        "incident_id": result.incident_id,
        "severity": result.severity,
        "analysis_id": result.analysis_id,
        "events_by_category": result.events_by_category,
        "timeline_md": result.timeline_md,
        "analysis_md": result.analysis_md,
        "markdown": result.to_markdown(),
    }


def _default_server() -> str:
    from config.server_registry import get_server_registry
    return get_server_registry().get_default_server_id()
