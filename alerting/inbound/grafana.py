"""
Grafana Alertmanager receiver adapter.

Grafana and stock Prometheus Alertmanager share the same webhook payload:

    {
      "version": "4",
      "status": "firing" | "resolved",
      "receiver": "seeql",
      "groupLabels": { ... },
      "commonLabels": { ... },
      "commonAnnotations": { ... },
      "alerts": [
        {
          "status": "firing",
          "fingerprint": "...",
          "startsAt": "2026-04-23T12:34:56Z",
          "labels": { "alertname": "...", "severity": "...", "instance": "..." },
          "annotations": { "summary": "...", "description": "..." }
        }
      ]
    }

Authentication: bearer token in Authorization or shared-secret HMAC in
`X-Grafana-Signature` over the raw body. Accept whichever is configured.

Each webhook may carry multiple alerts; we normalize the first firing alert.
The full alert array is kept in `raw_payload` for forensics.
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


SIGNATURE_HEADER = "x-grafana-signature"
AUTHORIZATION_HEADER = "authorization"


class GrafanaAdapter:
    provider = "grafana"

    def verify_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        if not secret:
            return False
        # Bearer token match (plain comparison, constant time).
        bearer = _bearer_token(headers)
        if bearer and hmac.compare_digest(bearer, secret):
            return True
        # HMAC over raw body.
        provided = _header(headers, SIGNATURE_HEADER)
        if not provided:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if provided.startswith("sha256="):
            provided = provided[len("sha256="):]
        return hmac.compare_digest(provided.strip().lower(), expected.lower())

    def normalize(
        self,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        provider_default_server_id: str | None = None,
    ) -> InboundAlert:
        alerts = payload.get("alerts") or []
        # Prefer the first firing alert; fall back to first in the list.
        primary = next(
            (a for a in alerts if str(a.get("status") or "").lower() == "firing"),
            alerts[0] if alerts else {},
        )

        labels = primary.get("labels") or {}
        annotations = primary.get("annotations") or {}

        alertname = str(labels.get("alertname") or labels.get("alert_name") or "")
        summary = str(
            annotations.get("summary")
            or annotations.get("description")
            or alertname
            or "(no summary)"
        )

        alert_type = map_alert_type(
            alertname or summary,
            hints={"alert_type": labels.get("alert_type")},
        )

        severity = coerce_severity(labels.get("severity") or payload.get("status"))

        external_id = str(primary.get("fingerprint") or "").strip()
        if not external_id:
            # Group fingerprint fallback
            external_id = f"grafana:{alertname}:{primary.get('startsAt')}"

        fired_at = str(primary.get("startsAt") or "").strip()

        # server_id resolution: labels first, then commonLabels.
        merged_view: dict[str, Any] = {"labels": dict(labels)}
        common = payload.get("commonLabels") or {}
        if isinstance(common, dict):
            for k, v in common.items():
                merged_view["labels"].setdefault(k, v)
        server_id = resolve_server_id(
            merged_view, self.provider, provider_default_server_id
        )

        return InboundAlert(
            provider=self.provider,
            external_id=external_id,
            alert_type=alert_type,
            severity=severity,
            summary=summary,
            fired_at=fired_at,
            server_id=server_id,
            callback_url=annotations.get("callback_url") if isinstance(annotations, dict) else None,
            context={
                "alertname": alertname,
                "status": payload.get("status"),
                "receiver": payload.get("receiver"),
                "labels": dict(labels),
            },
            raw_payload=dict(payload),
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None


def _bearer_token(headers: Mapping[str, str]) -> str | None:
    auth = _header(headers, AUTHORIZATION_HEADER)
    if not auth:
        return None
    parts = auth.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None
