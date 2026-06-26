"""
MCP resources — read-only URI-addressable content the client can fetch
without going through a tool-call round-trip. Every resource here is a
thin wrapper over an existing tool impl that returns JSON or markdown.

URI space (all under the `seeql://` scheme):

  seeql://investigations/recent          → JSON list
  seeql://investigations/{id}            → JSON detail
  seeql://incidents/recent               → JSON list
  seeql://incidents/{id}                 → JSON detail
  seeql://incidents/{id}/replay.md       → markdown replay
  seeql://state/{server}.md              → current state markdown
  seeql://servers                        → JSON server list
"""

import json
import logging

logger = logging.getLogger(__name__)


def register(mcp) -> None:
    """Register every resource handler on the FastMCP instance."""

    @mcp.resource(
        "seeql://servers",
        description="List of all MySQL servers SeeQL is monitoring.",
        mime_type="application/json",
    )
    def servers_resource() -> str:
        from mcp_server.tools.state import _list_servers_impl
        return json.dumps(_list_servers_impl(), default=str)

    @mcp.resource(
        "seeql://investigations/recent",
        description="20 most recent webhook investigations as JSON.",
        mime_type="application/json",
    )
    def investigations_recent_resource() -> str:
        from mcp_server.tools.investigations import _list_investigations_impl
        return json.dumps(
            _list_investigations_impl(None, None, 20),
            default=str,
        )

    @mcp.resource(
        "seeql://investigations/{investigation_id}",
        description="Full detail for an investigation (row + findings + samples).",
        mime_type="application/json",
    )
    def investigation_detail_resource(investigation_id: int) -> str:
        from mcp_server.tools.investigations import _get_investigation_impl
        return json.dumps(_get_investigation_impl(investigation_id), default=str)

    @mcp.resource(
        "seeql://incidents/recent",
        description="20 most recent anomaly-cluster incidents as JSON.",
        mime_type="application/json",
    )
    def incidents_recent_resource() -> str:
        from mcp_server.tools.incidents import _list_incidents_impl
        return json.dumps(_list_incidents_impl(None, None, 20), default=str)

    @mcp.resource(
        "seeql://incidents/{incident_id}",
        description="Incident window detail + constituent anomaly_events.",
        mime_type="application/json",
    )
    def incident_detail_resource(incident_id: int) -> str:
        from mcp_server.tools.incidents import _get_incident_impl
        return json.dumps(_get_incident_impl(incident_id), default=str)

    @mcp.resource(
        "seeql://incidents/{incident_id}/replay.md",
        description="Full incident replay rendered as markdown.",
        mime_type="text/markdown",
    )
    def incident_replay_resource(incident_id: int) -> str:
        from mcp_server.tools.replay import _replay_incident_impl
        data = _replay_incident_impl(incident_id)
        if "error" in data:
            return f"# Error\n\n{data['error']}\n"
        return data.get("markdown") or ""

    @mcp.resource(
        "seeql://state/{server_id}.md",
        description="Current Structured State Report for a server, rendered as markdown.",
        mime_type="text/markdown",
    )
    def state_resource(server_id: str) -> str:
        from mcp_server.tools.state import _state_report_impl
        data = _state_report_impl(server_id)
        return data.get("markdown") or ""
