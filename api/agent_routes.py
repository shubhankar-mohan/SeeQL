"""API routes for the LLM Agent and Alerting systems."""

import logging
from fastapi import APIRouter, Query as QueryParam

from api.query_helpers import query_rows, resolve_server_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agent"])


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
):
    """Trigger an on-demand LLM analysis."""
    server = resolve_server_id(server)
    from agent.llm_agent import run_analysis
    result = run_analysis(analysis_type, server_id=server)
    if result is None:
        return {"status": "skipped", "reason": "Agent disabled or state is quiet"}
    return {
        "status": "completed",
        "server_id": server,
        "severity": result.get("severity"),
        "findings": result.get("findings"),
        "recommendations": result.get("recommendations"),
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
    return query_rows(sql, (server, limit))


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


# ---------------------------------------------------------------------------
# Anomaly Detection
# ---------------------------------------------------------------------------

@router.get("/api/v1/anomalies")
def check_anomalies():
    """Run anomaly detection and return current anomalies."""
    from alerting.anomaly import detect_anomalies, METRIC_CONFIGS
    anomalies = detect_anomalies()
    return {
        "anomaly_count": len(anomalies),
        "anomalies": [
            {
                "metric": a.metric,
                "description": METRIC_CONFIGS.get(a.metric, {}).get("description", a.metric),
                "current": round(a.current, 4),
                "baseline_mean": round(a.baseline_mean, 4),
                "baseline_stddev": round(a.baseline_stddev, 4),
                "z_score": round(a.z_score, 2),
                "pct_change": round(a.pct_change, 1),
                "direction": a.direction,
                "severity": a.severity,
            }
            for a in anomalies
        ],
    }
