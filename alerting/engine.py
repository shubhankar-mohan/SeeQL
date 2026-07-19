"""
Alert engine — evaluates rules and dispatches to channels.

Called after each collection loop. Manages cooldowns to prevent
alert storms.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from alerting.models import Alert
from alerting.rules import RULE_EVALUATORS
from alerting.channels import AlertChannel, LogChannel, SlackChannel, WebhookChannel
from config import get_config
from storage.connection import get_mon_connection, get_mon_reader

logger = logging.getLogger(__name__)

# In-memory cooldown tracker: {rule_name: last_fired_iso}
_cooldowns: dict[str, str] = {}
_initialized = False


def _init_cooldowns():
    """Load last fire times from alert_history on first run.

    Only seeds from rows where delivered=1. Without this filter, a restart
    during a channel outage (delivered=0 stored, no live cooldown -- see the
    `delivered` gate in evaluate()) would re-suppress the rule for a full
    cooldown window on the very next process start, silently defeating the
    live-path fix the moment the process bounces (P1c-6 across restart).
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    try:
        with get_mon_reader() as conn:
            rows = conn.execute("""
                SELECT rule_name, MAX(fired_at) as last_fired
                FROM alert_history
                WHERE delivered = 1
                GROUP BY rule_name
            """).fetchall()
            for row in rows:
                _cooldowns[row["rule_name"]] = row["last_fired"]
    except Exception:
        pass  # Table might not exist yet


def _build_channels(config: dict) -> dict[str, AlertChannel]:
    """Build channel instances from config."""
    channels = {}
    ch_config = config.get("channels", {})

    # Log channel is always available
    channels["log"] = LogChannel()

    slack = ch_config.get("slack", {})
    if slack.get("enabled") and slack.get("webhook_url"):
        url = slack["webhook_url"]
        if not url.startswith("${"):
            channels["slack"] = SlackChannel(url)

    webhook = ch_config.get("webhook", {})
    if webhook.get("enabled") and webhook.get("url"):
        url = webhook["url"]
        if not url.startswith("${"):
            channels["webhook"] = WebhookChannel(url, webhook.get("headers", {}))

    return channels


def evaluate(loop_name: str = "fast") -> list[Alert]:
    """
    Evaluate all enabled alert rules across all servers and dispatch fired
    alerts.

    Args:
        loop_name: Which loop just completed ("fast" or "medium").
                   Used to select which rules to evaluate.

    Returns:
        List of alerts that were fired across all servers.

    Multi-server: each rule is evaluated once per active server. Cooldowns
    are tracked per (rule_name, server_id) via the rule-name namespacing the
    rules themselves apply — e.g. "lock_cascade:prod-primary" and
    "lock_cascade:prod-replica" have independent cooldowns.
    """
    alert_config = get_config().get("alerting", {})
    if not alert_config.get("enabled", False):
        return []

    _init_cooldowns()

    default_cooldown = alert_config.get("default_cooldown_minutes", 15)
    rules_config = alert_config.get("rules", {})
    channels = _build_channels(alert_config)

    # Fan out across all active servers. One server failing does not stop
    # evaluation for others.
    from config.server_registry import get_server_registry
    try:
        servers = get_server_registry().get_active_servers()
    except Exception as e:
        logger.warning(f"Failed to load server registry; falling back to 'default': {e}")
        servers = []

    if not servers:
        class _DefaultServer:
            server_id = "default"
        servers = [_DefaultServer()]

    fired = []
    for server in servers:
        sid = server.server_id
        for rule_name, evaluator in RULE_EVALUATORS.items():
            rule_cfg = rules_config.get(rule_name, {})
            if not rule_cfg.get("enabled", True):
                continue

            # Cooldown key includes the server (rules namespace their alerts
            # as "rule_name:sid", so the tracker effectively scopes per pair).
            scoped_key = f"{rule_name}:{sid}"
            cooldown_min = rule_cfg.get("cooldown_minutes", default_cooldown)
            if _in_cooldown(scoped_key, cooldown_min):
                continue

            try:
                alert = evaluator(rule_cfg, sid)
            except Exception as e:
                logger.warning(f"Rule {rule_name} evaluation failed for {sid}: {e}")
                continue

            if alert is None:
                continue

            # Set channels from config
            alert.channels = rule_cfg.get("channels", ["log"])

            # Dispatch to channels. `delivered` gates the cooldown and must
            # reflect ONLY real notification channels -- never "log". The log
            # channel always succeeds and is a *record*, not a delivery; the
            # shipped config uses `channels: [slack, log]`, so if "log"
            # counted as delivery the cooldown would be set even when Slack is
            # down, suppressing every retry for the whole cooldown window
            # (the exact P1c-6 bug).
            delivered = False
            for ch_name in alert.channels:
                channel = channels.get(ch_name)
                if channel and channel.send(alert):
                    if ch_name != "log":
                        delivered = True

            # If no real channel delivered and "log" wasn't already in the
            # rule's channel list, still log the alert as a visibility
            # fallback (LogChannel never fails). This does NOT count towards
            # `delivered` -- the cooldown stays unset so the real channels get
            # retried next cycle instead of going quiet.
            if not delivered and "log" not in alert.channels:
                log_channel = channels.get("log")
                if log_channel:
                    log_channel.send(alert)

            alert.delivered = delivered

            # Only start the cooldown once a real (non-log) channel actually
            # delivered the alert. If delivery failed everywhere (e.g. Slack
            # is down), leave the cooldown unset so the next evaluation cycle
            # retries immediately instead of waiting out the full cooldown
            # while the outage continues.
            if delivered:
                _cooldowns[scoped_key] = alert.fired_at

            # Store in alert_history either way -- delivered=0 is recorded
            # when no real channel succeeded, so the failure stays visible.
            _store_alert(alert)
            fired.append(alert)

    if fired:
        logger.info(f"Fired {len(fired)} alert(s): {[a.rule_name for a in fired]}")

    return fired


def _in_cooldown(rule_name: str, cooldown_minutes: int) -> bool:
    """Check if a rule is in its cooldown period."""
    if cooldown_minutes <= 0:
        return False

    last_fired = _cooldowns.get(rule_name)
    if not last_fired:
        return False

    try:
        last_dt = datetime.fromisoformat(last_fired)
        return datetime.now(timezone.utc).replace(tzinfo=None) - last_dt < timedelta(minutes=cooldown_minutes)
    except (ValueError, TypeError):
        return False


def _store_alert(alert: Alert):
    """Write alert to alert_history table."""
    try:
        # Extract server_id from context (rules populate it). Fallback to
        # 'default' so the NOT NULL column is always satisfied.
        sid = (alert.context or {}).get("server_id", "default")
        with get_mon_connection() as conn:
            conn.execute(
                """INSERT INTO alert_history
                   (fired_at, server_id, rule_name, severity, message,
                    context_json, channel, delivered)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    alert.fired_at,
                    sid,
                    alert.rule_name,
                    alert.severity.value,
                    alert.message,
                    json.dumps(alert.context, default=str),
                    ",".join(alert.channels),
                    1 if alert.delivered else 0,
                ),
            )
    except Exception as e:
        logger.error(f"Failed to store alert: {e}")
