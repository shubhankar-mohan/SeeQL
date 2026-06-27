"""
GCP Cloud Monitoring webhook adapter.

GCP Monitoring can deliver alerts via Pub/Sub push or direct webhook. The
Pub/Sub push flavour signs requests with a Google-issued OIDC JWT in the
Authorization header. For environments where verifying the Google cert chain
isn't viable (local dev, air-gapped), a shared-secret HMAC fallback is
accepted — configure via `webhooks.providers.gcp.secret` in settings.yaml.

Expected payload (Cloud Monitoring notification schema):

    {
      "incident": {
        "incident_id": "...",
        "policy_name": "High CPU — Cloud SQL prod",
        "condition_name": "...",
        "state": "OPEN" | "CLOSED",
        "started_at": 1735000000,
        "summary": "...",
        "resource": { "labels": { "database_id": "prj:region:instance" } }
      },
      "version": "1.2"
    }

`incident.state == 'CLOSED'` is still accepted (stored as a record) but the
investigator treats CLOSED-only incidents as informational.
"""

import hmac
import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping

from alerting.inbound.models import InboundAlert
from alerting.inbound.normalizer import (
    map_alert_type,
    resolve_server_id,
    coerce_severity,
)


SIGNATURE_HEADER = "x-seeql-signature"  # HMAC fallback
AUTHORIZATION_HEADER = "authorization"   # OIDC JWT path


class GCPAdapter:
    provider = "gcp"

    def __init__(self, oidc_audience: str | None = None):
        # Expected `aud` claim for Google-signed OIDC JWTs. When None it is
        # read lazily from `webhooks.providers.gcp.oidc_audience` in config.
        self._oidc_audience = oidc_audience

    def _expected_audience(self) -> str | None:
        """Resolve the expected OIDC audience: an explicit ctor arg wins,
        otherwise read `webhooks.providers.gcp.oidc_audience` from config.
        Returns None when no audience is configured (OIDC is then disabled)."""
        if self._oidc_audience:
            return self._oidc_audience
        try:
            from config import get_config
            providers = (get_config().get("webhooks") or {}).get("providers") or {}
            aud = (providers.get("gcp") or {}).get("oidc_audience")
            return str(aud) if aud else None
        except Exception:
            return None

    def verify_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        # Preferred: OIDC JWT verification. We keep the heavyweight
        # google-auth import optional so the base install stays light.
        #
        # SECURITY: an audience MUST be configured. verify_oauth2_token with
        # no `audience` accepts ANY Google-signed token (any GCP account) —
        # an auth bypass. When no audience is configured we skip OIDC entirely
        # and fall back to HMAC rather than trusting a bare Google-signed token.
        oidc_token = _bearer_token(headers)
        if oidc_token:
            audience = self._expected_audience()
            if audience:
                try:
                    if _verify_oidc_token(oidc_token, audience):
                        return True
                except Exception:
                    # Fall through to HMAC fallback rather than failing outright.
                    pass
            # No audience configured → OIDC disabled; fall through to HMAC.

        # Fallback: shared-secret HMAC over raw body (same scheme as generic).
        if not secret:
            return False
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
        policy_map: Mapping[str, str] | None = None,
    ) -> InboundAlert:
        incident = payload.get("incident") or {}
        policy_name = str(incident.get("policy_name") or "")
        state = str(incident.get("state") or "").upper()

        # Policy-name → canonical mapping, with heuristic fallback.
        explicit = None
        if policy_map:
            for needle, canonical in policy_map.items():
                if str(needle).lower() in policy_name.lower():
                    explicit = canonical
                    break
        alert_type = (
            explicit
            if explicit
            else map_alert_type(policy_name, hints={"alert_type": explicit})
        )

        # Severity: OPEN defaults to warning (criticality lives in policy
        # thresholds, which we don't know). CLOSED incidents downgrade to info.
        if state == "CLOSED":
            severity = "info"
        else:
            severity = coerce_severity(incident.get("severity") or "warning")

        external_id = str(incident.get("incident_id") or "").strip()
        if not external_id:
            # Fall back to (policy_name, started_at) — still idempotent enough.
            external_id = f"gcp:{policy_name}:{incident.get('started_at')}"

        fired_at = _coerce_ts(incident.get("started_at")) or ""

        resource = incident.get("resource") or {}
        resource_labels = resource.get("labels") or {}
        # Merge resource labels into the payload view for server_id resolution.
        merged_view = {
            "labels": {**resource_labels, **(payload.get("labels") or {})},
            **{k: v for k, v in payload.items() if k != "labels"},
        }
        server_id = resolve_server_id(
            merged_view, self.provider, provider_default_server_id
        )

        summary = str(
            incident.get("summary") or incident.get("condition_name") or policy_name or "(no summary)"
        )

        return InboundAlert(
            provider=self.provider,
            external_id=external_id,
            alert_type=alert_type,
            severity=severity,
            summary=summary,
            fired_at=fired_at,
            server_id=server_id,
            callback_url=payload.get("callback_url"),
            context={"policy_name": policy_name, "state": state, "raw_severity": incident.get("severity")},
            raw_payload=dict(payload),
        )


def _verify_oidc_token(token: str, audience: str) -> bool:
    """Verify a Google-issued OIDC JWT against the expected audience.

    Returns True only when the token is Google-signed (issuer + signature +
    expiry enforced by verify_oauth2_token) AND its `aud` claim equals
    `audience`. Raises on any failure — the caller treats exceptions as
    "not verified". The google-auth import is local so the base install
    stays light.
    """
    from google.oauth2 import id_token  # type: ignore
    from google.auth.transport import requests as grequests  # type: ignore

    # `audience=` enforces the aud-claim match; verify_oauth2_token also
    # enforces the Google issuer, signature, and expiry checks.
    id_token.verify_oauth2_token(token, grequests.Request(), audience=audience)
    return True


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


def _coerce_ts(value: Any) -> str | None:
    """GCP sends epoch seconds (int) or ISO strings — normalize to ISO UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
