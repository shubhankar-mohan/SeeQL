"""Inbound alert model — normalized shape across all adapters."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# Canonical SeeQL trigger vocabulary. Each key must also exist in
# `agent.prompts.INCIDENT_TRIGGERS` (or the investigator will fall back to
# the 'default' trigger). Adapters must map every incoming alert to one of
# these keys or to 'default'.
CANONICAL_ALERT_TYPES = {
    "lock_cascade",
    "high_cpu",
    "deadlock_detected",
    "query_regression",
    "threads_running_spike",
    "missing_index",
    "ddl_change",
    "default",
}

CANONICAL_SEVERITIES = {"critical", "warning", "info"}


@dataclass
class InboundAlert:
    provider: str                                    # generic | gcp | pagerduty | grafana
    external_id: str                                 # provider-supplied idempotency key
    alert_type: str                                  # must be in CANONICAL_ALERT_TYPES
    severity: str                                    # must be in CANONICAL_SEVERITIES
    summary: str
    fired_at: str                                    # ISO UTC
    server_id: str
    callback_url: str | None = None
    context: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    signature_verified: bool = False

    def __post_init__(self):
        if not self.fired_at:
            self.fired_at = datetime.now(timezone.utc).isoformat()
        if self.alert_type not in CANONICAL_ALERT_TYPES:
            self.alert_type = "default"
        if self.severity not in CANONICAL_SEVERITIES:
            # Collapse unknown severities to 'warning' — safer default than
            # silently dropping to info or escalating to critical.
            self.severity = "warning"
