"""API routes for the LLM Agent and Alerting systems."""

import json
import logging
from fastapi import APIRouter, Query as QueryParam
from fastapi.responses import JSONResponse

from api.query_helpers import query_rows, resolve_server_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


def _decode_json_field(raw):
    """Decode an agent_analyses findings/recommendations column.

    Both columns are stored as `json.dumps(<parsed markdown text>)` (see
    agent.llm_agent._parse_and_store / run_llm_analysis), so handing the raw
    column value straight back to a JSON API response double-encodes it —
    the client sees an escaped JSON string instead of the plain text (P1-23).
    Falls back to the raw value unchanged if it isn't valid JSON.
    """
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw


# ---------------------------------------------------------------------------
# State Report
# ---------------------------------------------------------------------------

@router.get("/api/v1/state-report")
def state_report(server: str = QueryParam(default=None)):
    """Get the current structured state report."""
    server = resolve_server_id(server)
    from agent.state_builder import build_state_report
    report = build_state_report(server_id=server)
    return {
        "server_id": server,
        "markdown": report.to_markdown(),
        "data": report.to_dict(),
    }


# ---------------------------------------------------------------------------
# Agent Analysis
# ---------------------------------------------------------------------------

@router.post("/api/v1/agent/analyze")
def trigger_analysis(
    analysis_type: str = QueryParam(default="routine"),
    server: str = QueryParam(default=None),
    trigger_type: str = QueryParam(default=None),
):
    """Trigger an on-demand LLM analysis.

    trigger_type (P3-3): for analysis_type="incident", selects a tailored
    INCIDENT_TRIGGERS playbook (e.g. "lock_cascade", "high_cpu") instead of
    the generic "default" instructions. Ignored for "routine" analyses.
    """
    server = resolve_server_id(server)
    from agent.llm_agent import run_analysis
    result = run_analysis(analysis_type, server_id=server, trigger_type=trigger_type)
    if result is None:
        return {"status": "skipped", "reason": "Agent disabled or state is quiet"}
    if isinstance(result, dict) and result.get("status") == "error":
        # Honest failure (P1-6): a real LLM/provider error used to come back
        # as the same `None` as "agent disabled" / "quiet state" and get
        # reported as a 200 "skipped" — indistinguishable from a no-op.
        return JSONResponse(
            status_code=502,
            content={
                "status": "error",
                "server_id": server,
                "error": result.get("error"),
            },
        )
    if isinstance(result, dict) and result.get("stored") is False:
        # The agent ran but produced no usable analysis (max-rounds
        # exhaustion or a safety block — P1-5): nothing was stored and no
        # incident was linked. Report that honestly instead of a
        # "completed" with null severity/findings that reads as success.
        return JSONResponse(
            status_code=502,
            content={
                "status": "no_analysis",
                "server_id": server,
                "reason": "The model produced no usable analysis "
                          "(tool budget exhausted or response blocked).",
            },
        )
    return {
        "status": "completed",
        "server_id": server,
        "severity": result.get("severity"),
        "findings": _decode_json_field(result.get("findings")),
        "recommendations": _decode_json_field(result.get("recommendations")),
    }


@router.get("/api/v1/agent/analyses")
def list_analyses(
    limit: int = QueryParam(default=20, le=100),
    server: str = QueryParam(default=None),
):
    """List recent agent analyses."""
    server = resolve_server_id(server)
    sql = """
        SELECT analyzed_at, analysis_type, severity, input_summary,
               findings, recommendations, applied, outcome_notes
        FROM agent_analyses
        WHERE server_id = ?
        ORDER BY analyzed_at DESC
        LIMIT ?
    """
    rows = query_rows(sql, (server, limit))
    for row in rows:
        row["findings"] = _decode_json_field(row.get("findings"))
        row["recommendations"] = _decode_json_field(row.get("recommendations"))
    return rows


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

@router.get("/api/v1/alerts")
def list_alerts(
    limit: int = QueryParam(default=50, le=200),
    severity: str | None = QueryParam(default=None),
    server: str = QueryParam(default=None),
):
    """List recent alerts."""
    server = resolve_server_id(server)
    if severity:
        sql = """
            SELECT fired_at, rule_name, severity, message, context_json,
                   channel, delivered, resolved_at
            FROM alert_history
            WHERE server_id = ? AND severity = ?
            ORDER BY fired_at DESC LIMIT ?
        """
        return query_rows(sql, (server, severity, limit))
    else:
        sql = """
            SELECT fired_at, rule_name, severity, message, context_json,
                   channel, delivered, resolved_at
            FROM alert_history
            WHERE server_id = ?
            ORDER BY fired_at DESC LIMIT ?
        """
        return query_rows(sql, (server, limit))


@router.get("/api/v1/alerts/rules")
def alert_rules():
    """List configured alert rules and their status."""
    from config import get_config
    config = get_config().get("alerting", {})
    rules = config.get("rules", {})
    return {
        "enabled": config.get("enabled", False),
        "rules": {
            name: {
                "enabled": cfg.get("enabled", True),
                "severity": cfg.get("severity", "info"),
                "cooldown_minutes": cfg.get("cooldown_minutes"),
                "channels": cfg.get("channels", ["log"]),
            }
            for name, cfg in rules.items()
        },
    }


@router.post("/api/v1/alerts/test")
def test_alert():
    """Fire a test alert to verify channel configuration."""
    from alerting.models import Alert, Severity
    from alerting.engine import _build_channels
    from config import get_config

    alert_config = get_config().get("alerting", {})
    channels = _build_channels(alert_config)

    test_alert = Alert(
        rule_name="test_alert",
        severity=Severity.INFO,
        message="This is a test alert from SeeQL",
        context={"test": True},
    )

    results = {}
    for name, channel in channels.items():
        results[name] = channel.send(test_alert)

    return {"channels_tested": results}
