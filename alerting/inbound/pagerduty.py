"""
PagerDuty V3 webhook adapter.

PagerDuty signs webhooks with HMAC-SHA256 in a custom header:

    X-PagerDuty-Signature: v1=<hex>,v1=<hex>

Multiple signatures can appear (secret rotation). We accept any that match.

Payload shape (V3):

    {
      "event": {
        "event_type": "incident.triggered",
        "id": "...",
        "occurred_at": "2026-04-23T12:34:56Z",
        "data": {
          "incident": {
            "id": "PXXXXX",
            "title": "High CPU on prod",
            "urgency": "high",
            "status": "triggered",
            "service": { "summary": "prod-primary" },
            "custom_details": { ... }
          }
        }
      }
    }
"""

import hmac
import hashlib
from typing import Any, Mapping

from alerting.inbound.models import InboundAlert
from alerting.inbound.normalizer import (
    map_alert_type,
    resolve_server_id,
    coerce_severity,
)


SIGNATURE_HEADER = "x-pagerduty-signature"


class PagerDutyAdapter:
    provider = "pagerduty"

    def verify_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        if not secret:
            return False
        provided = _header(headers, SIGNATURE_HEADER)
        if not provided:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        for token in str(provided).split(","):
            token = token.strip()
            if token.startswith("v1="):
                token = token[3:]
            if hmac.compare_digest(token.lower(), expected.lower()):
                return True
        return False

    def normalize(
        self,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        provider_default_server_id: str | None = None,
    ) -> InboundAlert:
        event = payload.get("event") or {}
        data = event.get("data") or {}
        incident = data.get("incident") or {}

        title = str(incident.get("title") or event.get("event_type") or "(no title)")
        urgency = str(incident.get("urgency") or "").lower()
        status = str(incident.get("status") or "").lower()

        # PagerDuty urgency is binary (high|low). Map to SeeQL severities.
        if urgency == "high":
            severity = "critical" if status == "triggered" else "warning"
        else:
            severity = "warning"
        # Re-run through coerce in case a custom field provides a richer level.
        custom = incident.get("custom_details") or {}
        if isinstance(custom, dict) and custom.get("severity"):
            severity = coerce_severity(custom.get("severity"))

        alert_type = map_alert_type(
            title,
            hints={"alert_type": (custom or {}).get("alert_type") if isinstance(custom, dict) else None},
        )

        external_id = str(incident.get("id") or event.get("id") or "").strip()
        if not external_id:
            external_id = f"pd:{title}:{event.get('occurred_at')}"

        fired_at = str(event.get("occurred_at") or incident.get("created_at") or "").strip()

        # server_id heuristics for PagerDuty: service.summary or custom_details.server_id
        service = incident.get("service") or {}
        payload_view: dict[str, Any] = {}
        if isinstance(custom, dict):
            payload_view.update(custom)
        if isinstance(service, dict) and service.get("summary"):
            payload_view.setdefault("host", service.get("summary"))
        server_id = resolve_server_id(
            payload_view, self.provider, provider_default_server_id
        )

        return InboundAlert(
            provider=self.provider,
            external_id=external_id,
            alert_type=alert_type,
            severity=severity,
            summary=title,
            fired_at=fired_at,
            server_id=server_id,
            callback_url=(custom or {}).get("callback_url") if isinstance(custom, dict) else None,
            context={
                "event_type": event.get("event_type"),
                "urgency": urgency,
                "status": status,
                "service_summary": (service or {}).get("summary"),
            },
            raw_payload=dict(payload),
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None
