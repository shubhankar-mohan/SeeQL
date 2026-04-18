"""
Alert delivery channels: Slack, Webhook, Log.
"""

import json
import logging
import urllib.request
import urllib.error
from abc import ABC, abstractmethod

from alerting.models import Alert

logger = logging.getLogger(__name__)


class AlertChannel(ABC):
    @abstractmethod
    def send(self, alert: Alert) -> bool:
        """Send an alert. Returns True if delivered successfully."""
        ...


class LogChannel(AlertChannel):
    """Always-on fallback: logs alerts."""

    def send(self, alert: Alert) -> bool:
        level = {
            "critical": logging.CRITICAL,
            "warning": logging.WARNING,
            "info": logging.INFO,
        }.get(alert.severity.value, logging.INFO)

        logger.log(level, f"[ALERT] [{alert.severity.value.upper()}] {alert.rule_name}: {alert.message}")
        return True


class SlackChannel(AlertChannel):
    """Send alerts to Slack via incoming webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, alert: Alert) -> bool:
        emoji = {"critical": ":red_circle:", "warning": ":warning:", "info": ":information_source:"}.get(
            alert.severity.value, ":bell:"
        )

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} SeeQL Alert: {alert.rule_name}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Severity:* {alert.severity.value.upper()}\n*Message:* {alert.message}"},
            },
        ]

        if alert.context:
            context_str = "\n".join(f"• {k}: {v}" for k, v in list(alert.context.items())[:10])
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Context:*\n{context_str}"},
            })

        payload = json.dumps({"blocks": blocks}).encode("utf-8")

        try:
            req = urllib.request.Request(
                self.webhook_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Slack delivery failed: {e}")
            return False


class WebhookChannel(AlertChannel):
    """Send alerts to a generic webhook endpoint."""

    def __init__(self, url: str, headers: dict | None = None):
        self.url = url
        self.headers = headers or {}

    def send(self, alert: Alert) -> bool:
        payload = json.dumps({
            "rule_name": alert.rule_name,
            "severity": alert.severity.value,
            "message": alert.message,
            "context": alert.context,
            "fired_at": alert.fired_at,
        }).encode("utf-8")

        try:
            headers = {"Content-Type": "application/json", **self.headers}
            req = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            logger.error(f"Webhook delivery failed: {e}")
            return False
