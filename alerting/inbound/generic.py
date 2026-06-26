"""
Generic HMAC webhook adapter.

Expected payload shape:

    {
      "alert_type": "missing_index",           // optional; heuristic mapping otherwise
      "severity":   "warning",                  // critical|warning|info
      "summary":    "slow query on members",
      "external_id":"unique-id-per-alert",      // required for dedup
      "server_id":  "prod-primary",             // optional; falls back to config
      "fired_at":   "2026-04-23T12:34:56Z",     // optional; defaults to now
      "callback_url": "https://...",            // optional
      "context":    { ... }                     // optional free-form
    }

Expected authentication:

    X-SeeQL-Signature: sha256=<lowercase hex HMAC-SHA256 of the raw body>
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


SIGNATURE_HEADER = "x-seeql-signature"


class GenericAdapter:
    provider = "generic"

    def verify_signature(
        self,
        body: bytes,
        headers: Mapping[str, str],
        secret: str,
    ) -> bool:
        if not secret:
            return False
        # Header lookup is case-insensitive (HTTP headers are not case-sensitive).
        provided = _header(headers, SIGNATURE_HEADER)
        if not provided:
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        # Accept either "sha256=<hex>" or bare "<hex>"
        if provided.startswith("sha256="):
            provided = provided[len("sha256="):]
        return hmac.compare_digest(provided.strip().lower(), expected.lower())

    def normalize(
        self,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
        provider_default_server_id: str | None = None,
    ) -> InboundAlert:
        external_id = str(payload.get("external_id") or "").strip()
        if not external_id:
            # Synthesize one if the sender didn't provide. Dedup still works
            # per-request, just not across repeated POSTs of the same event.
            external_id = f"generic:{datetime.now(timezone.utc).isoformat()}"

        alert_type = map_alert_type(
            payload.get("alert_type") or payload.get("title") or payload.get("summary"),
            hints={"alert_type": payload.get("alert_type")},
        )

        return InboundAlert(
            provider=self.provider,
            external_id=external_id,
            alert_type=alert_type,
            severity=coerce_severity(payload.get("severity")),
            summary=str(payload.get("summary") or payload.get("title") or "(no summary)"),
            fired_at=str(payload.get("fired_at") or "").strip(),
            server_id=resolve_server_id(payload, self.provider, provider_default_server_id),
            callback_url=payload.get("callback_url"),
            context=payload.get("context") or {},
            raw_payload=dict(payload),
        )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup."""
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None
