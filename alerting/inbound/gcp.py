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
import logging
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

logger = logging.getLogger(__name__)
_oidc_misconfig_warned = False


def _warn_oidc_misconfig() -> None:
    """Warn once when an OIDC token arrives but OIDC isn't fully configured."""
    global _oidc_misconfig_warned
    if not _oidc_misconfig_warned:
        logger.warning(
            "GCP webhook received an OIDC bearer token but OIDC is not fully "
            "configured (need both webhooks.providers.gcp.oidc_audience and "
            "oidc_allowed_sa); falling back to HMAC. Set both to accept OIDC."
        )
        _oidc_misconfig_warned = True


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

    def _allowed_sa_emails(self) -> set[str]:
        """Lower-cased set of service-account emails allowed via OIDC, from
        `webhooks.providers.gcp.oidc_allowed_sa` in config. The audience match
        alone is forgeable, so this allow-list is the real trust anchor. An
        empty set disables OIDC (HMAC-only)."""
        try:
            from config import get_config
            providers = (get_config().get("webhooks") or {}).get("providers") or {}
            raw = (providers.get("gcp") or {}).get("oidc_allowed_sa") or []
            if isinstance(raw, str):
                raw = [raw]
            return {str(e).strip().lower() for e in raw if str(e).strip()}
        except Exception:
            return set()

    def verify_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        # Preferred: OIDC JWT verification. We keep the heavyweight
        # google-auth import optional so the base install stays light.
        #
        # SECURITY: OIDC requires BOTH a configured audience AND an allow-list
        # of accepted service-account emails. The audience (the public webhook
        # URL) is not secret, so an audience match alone is forgeable by any
        # Google identity; the SA-email allow-list is the real trust anchor.
        # If either is unset, OIDC is disabled and we fall back to HMAC.
        oidc_token = _bearer_token(headers)
        if oidc_token:
            audience = self._expected_audience()
            allowed = self._allowed_sa_emails()
            if audience and allowed:
                try:
                    if _verify_oidc_token(oidc_token, audience, allowed):
                        return True
                except Exception:
                    # Fall through to HMAC fallback rather than failing outright.
                    pass
            else:
                _warn_oidc_misconfig()
            # OIDC needs both an audience and an allow-listed SA; otherwise it
            # is disabled and we fall back to HMAC rather than trust a
            # forgeable audience-only token.

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


def _verify_oidc_token(token: str, audience: str, allowed_emails: set[str]) -> bool:
    """Verify a Google-issued OIDC JWT against the expected audience AND the
    allow-listed service-account email(s).

    The audience match alone is NOT authentication: the audience is the public
    webhook URL, so any Google identity can mint a token with that `aud`. We
    therefore also require the token's `email` claim to be one of the configured
    Cloud Monitoring service accounts. Returns True only when the token is
    Google-signed (issuer + signature + expiry enforced by verify_oauth2_token),
    `aud` equals `audience`, AND `email` is allow-listed. Raises on any failure
    — the caller treats exceptions as "not verified".
    """
    from google.oauth2 import id_token  # type: ignore
    from google.auth.transport import requests as grequests  # type: ignore

    # `audience=` enforces the aud-claim match; verify_oauth2_token also enforces
    # the Google issuer, signature, and expiry checks and returns the claims.
    claims = id_token.verify_oauth2_token(token, grequests.Request(), audience=audience)
    email = str(claims.get("email") or "").lower()
    return bool(email) and email in allowed_emails


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
