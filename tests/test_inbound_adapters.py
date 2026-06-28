"""
Tests for alerting/inbound adapters.

Covers per-adapter normalize() behavior, HMAC / bearer signature verification
(accept valid, reject tampered, reject missing), and the shared normalizer
helpers (canonical type mapping, server_id resolution, severity coercion).
"""

import hmac
import hashlib
import json

import pytest

from alerting.inbound import (
    InboundAlert,
    GenericAdapter,
    GCPAdapter,
    PagerDutyAdapter,
    GrafanaAdapter,
    get_adapter,
)
from alerting.inbound.normalizer import (
    map_alert_type,
    resolve_server_id,
    coerce_severity,
)

from tests.fixtures.webhook_payloads import (
    GENERIC_MISSING_INDEX,
    GENERIC_MINIMAL,
    GCP_HIGH_CPU_OPEN,
    GCP_SLOW_QUERY_CLOSED,
    PAGERDUTY_HIGH,
    PAGERDUTY_LOW,
    GRAFANA_QUERY_REGRESSION,
    GRAFANA_MULTI_ALERT,
)


# ---------------------------------------------------------------------------
# Normalizer helpers
# ---------------------------------------------------------------------------

class TestNormalizerHelpers:
    def test_map_alert_type_respects_canonical_hint(self):
        assert map_alert_type("random text", {"alert_type": "lock_cascade"}) == "lock_cascade"

    def test_map_alert_type_heuristic_missing_index_variants(self):
        for raw in [
            "Slow query detected",
            "Full table scan",
            "no_index_used > threshold",
            "MISSING INDEX on members",
        ]:
            assert map_alert_type(raw) == "missing_index", raw

    def test_map_alert_type_heuristic_lock(self):
        assert map_alert_type("Lock wait cascade") == "lock_cascade"
        assert map_alert_type("Deadlock detected") == "deadlock_detected"

    def test_map_alert_type_heuristic_cpu(self):
        assert map_alert_type("High CPU on prod") == "high_cpu"
        assert map_alert_type("cpu_util > 90") == "high_cpu"

    def test_map_alert_type_fallback(self):
        assert map_alert_type("Totally unrelated") == "default"
        assert map_alert_type(None) == "default"
        assert map_alert_type("") == "default"

    def test_map_alert_type_ignores_invalid_hint(self):
        # Hint that isn't in canonical vocab falls through to heuristic.
        assert map_alert_type("High CPU spiked", {"alert_type": "fake_type"}) == "high_cpu"

    def test_resolve_server_id_labels_first(self):
        payload = {"labels": {"server_id": "from-labels"}, "server_id": "from-top"}
        assert resolve_server_id(payload, "generic") == "from-labels"

    def test_resolve_server_id_top_level(self):
        assert resolve_server_id({"server_id": "from-top"}, "generic") == "from-top"

    def test_resolve_server_id_falls_back_to_config_default(self):
        assert resolve_server_id({}, "generic", "cfg-default") == "cfg-default"

    def test_resolve_server_id_hardcoded_default(self):
        assert resolve_server_id({}, "generic") == "default"

    def test_coerce_severity_variants(self):
        assert coerce_severity("CRITICAL") == "critical"
        assert coerce_severity("p1") == "critical"
        assert coerce_severity("warn") == "warning"
        assert coerce_severity("info") == "info"
        assert coerce_severity(None) == "warning"
        assert coerce_severity("garbage") == "warning"


# ---------------------------------------------------------------------------
# Generic adapter
# ---------------------------------------------------------------------------

class TestGenericAdapter:
    SECRET = "supersecret"

    def _sign(self, body: bytes) -> str:
        return "sha256=" + hmac.new(
            self.SECRET.encode(), body, hashlib.sha256
        ).hexdigest()

    def test_verify_valid_signature(self):
        a = GenericAdapter()
        body = json.dumps(GENERIC_MISSING_INDEX).encode()
        headers = {"X-SeeQL-Signature": self._sign(body)}
        assert a.verify_signature(body, headers, self.SECRET) is True

    def test_verify_bare_hex_signature_accepted(self):
        a = GenericAdapter()
        body = b"{}"
        bare = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert a.verify_signature(body, {"X-SeeQL-Signature": bare}, self.SECRET) is True

    def test_verify_tampered_body_rejected(self):
        a = GenericAdapter()
        body = json.dumps(GENERIC_MISSING_INDEX).encode()
        sig = self._sign(body)
        tampered = body + b"x"
        assert a.verify_signature(tampered, {"X-SeeQL-Signature": sig}, self.SECRET) is False

    def test_verify_missing_header_rejected(self):
        a = GenericAdapter()
        assert a.verify_signature(b"{}", {}, self.SECRET) is False

    def test_verify_empty_secret_rejected(self):
        a = GenericAdapter()
        body = b"{}"
        sig = self._sign(body)
        assert a.verify_signature(body, {"X-SeeQL-Signature": sig}, "") is False

    def test_verify_header_case_insensitive(self):
        a = GenericAdapter()
        body = b"{}"
        sig = self._sign(body)
        assert a.verify_signature(body, {"x-SEEQL-signature": sig}, self.SECRET) is True

    def test_normalize_full_payload(self):
        a = GenericAdapter()
        alert = a.normalize(GENERIC_MISSING_INDEX)
        assert isinstance(alert, InboundAlert)
        assert alert.provider == "generic"
        assert alert.alert_type == "missing_index"
        assert alert.severity == "warning"
        assert alert.external_id == "generic-test-1"
        assert alert.server_id == "prod-primary"
        assert alert.summary.startswith("Query 0xABC")
        assert alert.context == {"digest": "0xABC", "table": "members"}
        assert alert.raw_payload["alert_type"] == "missing_index"

    def test_normalize_minimal_payload_synthesizes_external_id(self):
        a = GenericAdapter()
        alert = a.normalize(GENERIC_MINIMAL)
        assert alert.external_id.startswith("generic:")
        assert alert.alert_type == "default"  # no hints
        assert alert.severity == "critical"
        assert alert.server_id == "default"

    def test_normalize_uses_provider_default_server_id(self):
        a = GenericAdapter()
        alert = a.normalize(GENERIC_MINIMAL, provider_default_server_id="my-server")
        assert alert.server_id == "my-server"

    def test_normalize_collapses_unknown_alert_type_to_default(self):
        a = GenericAdapter()
        payload = dict(GENERIC_MISSING_INDEX)
        payload["alert_type"] = "not_a_real_type"
        payload["summary"] = "totally unrelated text"
        payload["title"] = "still unrelated"
        alert = a.normalize(payload)
        assert alert.alert_type == "default"


# ---------------------------------------------------------------------------
# GCP adapter
# ---------------------------------------------------------------------------

class TestGCPAdapter:
    SECRET = "gcp-hmac-secret"

    def test_hmac_fallback_valid(self):
        a = GCPAdapter()
        body = json.dumps(GCP_HIGH_CPU_OPEN).encode()
        sig = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert a.verify_signature(body, {"X-SeeQL-Signature": sig}, self.SECRET) is True

    def test_hmac_fallback_missing_rejected(self):
        a = GCPAdapter()
        assert a.verify_signature(b"{}", {}, self.SECRET) is False

    def test_oidc_accepted_when_audience_and_sa_match(self, monkeypatch):
        # Token Google-signed, aud matches, AND email allow-listed → accept
        # without touching HMAC.
        import alerting.inbound.gcp as gcpmod
        monkeypatch.setattr(gcpmod, "_verify_oidc_token",
                            lambda token, audience, allowed: True)
        monkeypatch.setattr(gcpmod.GCPAdapter, "_allowed_sa_emails",
                            lambda self: {"svc@x.iam.gserviceaccount.com"})
        a = GCPAdapter(oidc_audience="https://seeql.example.com/webhooks/gcp")
        headers = {"Authorization": "Bearer good.jwt.token"}
        assert a.verify_signature(b"{}", headers, self.SECRET) is True

    def test_oidc_rejected_when_sa_not_allowlisted(self, monkeypatch):
        # aud matches but the token's SA email is NOT allow-listed → the
        # audience-only bypass is closed; with no HMAC sig the request fails.
        import alerting.inbound.gcp as gcpmod
        monkeypatch.setattr(
            gcpmod, "_verify_oidc_token",
            lambda token, audience, allowed: "attacker@evil.gserviceaccount.com" in allowed,
        )
        monkeypatch.setattr(gcpmod.GCPAdapter, "_allowed_sa_emails",
                            lambda self: {"svc@x.iam.gserviceaccount.com"})
        a = GCPAdapter(oidc_audience="https://seeql.example.com/webhooks/gcp")
        headers = {"Authorization": "Bearer attacker.but.google.signed.token"}
        assert a.verify_signature(b"{}", headers, self.SECRET) is False

    def test_oidc_rejected_when_verification_raises(self, monkeypatch):
        # verify_oauth2_token raises (bad signature / wrong aud). With no HMAC
        # signature present the request must be rejected outright (no bypass).
        import alerting.inbound.gcp as gcpmod

        def _raise(token, audience, allowed):
            raise ValueError("Token has wrong audience")

        monkeypatch.setattr(gcpmod, "_verify_oidc_token", _raise)
        monkeypatch.setattr(gcpmod.GCPAdapter, "_allowed_sa_emails",
                            lambda self: {"svc@x.iam.gserviceaccount.com"})
        a = GCPAdapter(oidc_audience="https://seeql.example.com/webhooks/gcp")
        headers = {"Authorization": "Bearer attacker.jwt.token"}
        assert a.verify_signature(b"{}", headers, self.SECRET) is False

    def test_oidc_skipped_without_full_config_falls_back_to_hmac(self, monkeypatch):
        # Audience set but NO allow-list → OIDC must NOT be attempted (an
        # audience-only token is forgeable); HMAC fallback still works.
        import alerting.inbound.gcp as gcpmod
        called = {"oidc": False}

        def _boom(token, audience, allowed):  # pragma: no cover - must never run
            called["oidc"] = True
            return True

        monkeypatch.setattr(gcpmod, "_verify_oidc_token", _boom)
        monkeypatch.setattr(gcpmod.GCPAdapter, "_allowed_sa_emails", lambda self: set())

        a = GCPAdapter(oidc_audience="https://seeql.example.com/webhooks/gcp")
        body = json.dumps(GCP_HIGH_CPU_OPEN).encode()
        sig = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers = {"Authorization": "Bearer google.signed.token", "X-SeeQL-Signature": sig}
        assert a.verify_signature(body, headers, self.SECRET) is True
        assert called["oidc"] is False

    def test_oidc_token_without_config_and_no_hmac_rejected(self, monkeypatch):
        # Bare Google-signed token, OIDC not fully configured, no HMAC sig →
        # rejected (no bypass).
        import alerting.inbound.gcp as gcpmod
        monkeypatch.setattr(gcpmod.GCPAdapter, "_expected_audience", lambda self: None)
        monkeypatch.setattr(gcpmod.GCPAdapter, "_allowed_sa_emails", lambda self: set())
        a = GCPAdapter()
        headers = {"Authorization": "Bearer google.signed.token"}
        assert a.verify_signature(b"{}", headers, self.SECRET) is False

    def test_normalize_open_incident(self):
        a = GCPAdapter()
        alert = a.normalize(
            GCP_HIGH_CPU_OPEN,
            policy_map={"high-cpu-policy": "high_cpu"},
        )
        assert alert.provider == "gcp"
        assert alert.alert_type == "high_cpu"
        assert alert.severity == "warning"   # OPEN, no explicit severity → default warning
        assert alert.external_id == "gcp-incident-123"
        assert alert.server_id == "demo-prj:asia-south1:prod-primary"
        assert alert.context["state"] == "OPEN"
        assert alert.fired_at.startswith("20")  # ISO timestamp

    def test_normalize_closed_incident_downgrades_severity(self):
        a = GCPAdapter()
        alert = a.normalize(
            GCP_SLOW_QUERY_CLOSED,
            policy_map={"slow-query-policy": "missing_index"},
        )
        assert alert.alert_type == "missing_index"
        assert alert.severity == "info"  # CLOSED → info
        assert alert.context["state"] == "CLOSED"

    def test_normalize_heuristic_when_no_policy_map(self):
        a = GCPAdapter()
        alert = a.normalize(GCP_HIGH_CPU_OPEN)  # no policy_map
        assert alert.alert_type == "high_cpu"   # falls back to heuristic


# ---------------------------------------------------------------------------
# PagerDuty adapter
# ---------------------------------------------------------------------------

class TestPagerDutyAdapter:
    SECRET = "pd-secret"

    def _sign(self, body: bytes) -> str:
        return "v1=" + hmac.new(
            self.SECRET.encode(), body, hashlib.sha256
        ).hexdigest()

    def test_verify_valid(self):
        a = PagerDutyAdapter()
        body = json.dumps(PAGERDUTY_HIGH).encode()
        headers = {"X-PagerDuty-Signature": self._sign(body)}
        assert a.verify_signature(body, headers, self.SECRET) is True

    def test_verify_multiple_signatures_one_valid(self):
        a = PagerDutyAdapter()
        body = json.dumps(PAGERDUTY_HIGH).encode()
        bad = "v1=" + "0" * 64
        headers = {"X-PagerDuty-Signature": f"{bad}, {self._sign(body)}"}
        assert a.verify_signature(body, headers, self.SECRET) is True

    def test_verify_all_bad_rejected(self):
        a = PagerDutyAdapter()
        body = json.dumps(PAGERDUTY_HIGH).encode()
        headers = {"X-PagerDuty-Signature": "v1=" + "0" * 64}
        assert a.verify_signature(body, headers, self.SECRET) is False

    def test_verify_missing_rejected(self):
        a = PagerDutyAdapter()
        assert a.verify_signature(b"{}", {}, self.SECRET) is False

    def test_normalize_high_urgency_maps_to_critical(self):
        a = PagerDutyAdapter()
        alert = a.normalize(PAGERDUTY_HIGH)
        assert alert.provider == "pagerduty"
        assert alert.alert_type == "lock_cascade"   # from custom_details hint
        assert alert.severity == "critical"         # high urgency + triggered
        assert alert.external_id == "PXYZ123"
        assert alert.server_id == "prod-primary"

    def test_normalize_low_urgency_maps_to_warning(self):
        a = PagerDutyAdapter()
        alert = a.normalize(PAGERDUTY_LOW)
        assert alert.severity == "warning"
        # No hint, no matching heuristic → default
        assert alert.alert_type == "default"


# ---------------------------------------------------------------------------
# Grafana adapter
# ---------------------------------------------------------------------------

class TestGrafanaAdapter:
    SECRET = "grafana-secret"

    def test_bearer_token_accepted(self):
        a = GrafanaAdapter()
        headers = {"Authorization": f"Bearer {self.SECRET}"}
        assert a.verify_signature(b"{}", headers, self.SECRET) is True

    def test_bearer_token_wrong_rejected(self):
        a = GrafanaAdapter()
        headers = {"Authorization": "Bearer wrong-token"}
        assert a.verify_signature(b"{}", headers, self.SECRET) is False

    def test_hmac_fallback_valid(self):
        a = GrafanaAdapter()
        body = json.dumps(GRAFANA_QUERY_REGRESSION).encode()
        sig = hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert a.verify_signature(body, {"X-Grafana-Signature": sig}, self.SECRET) is True

    def test_normalize_single_alert(self):
        a = GrafanaAdapter()
        alert = a.normalize(GRAFANA_QUERY_REGRESSION)
        assert alert.provider == "grafana"
        assert alert.alert_type == "query_regression"
        assert alert.severity == "critical"
        assert alert.external_id == "grafana-fp-999"
        assert alert.server_id == "prod-primary"

    def test_normalize_picks_firing_over_resolved(self):
        a = GrafanaAdapter()
        alert = a.normalize(GRAFANA_MULTI_ALERT)
        # Picks the firing alert, not the resolved one.
        assert alert.external_id == "fp-firing"
        assert alert.alert_type == "high_cpu"
        assert alert.severity == "critical"
        assert alert.server_id == "prod-replica"


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestAdapterRegistry:
    def test_get_adapter_known_providers(self):
        assert isinstance(get_adapter("generic"), GenericAdapter)
        assert isinstance(get_adapter("gcp"), GCPAdapter)
        assert isinstance(get_adapter("pagerduty"), PagerDutyAdapter)
        assert isinstance(get_adapter("grafana"), GrafanaAdapter)

    def test_get_adapter_unknown_returns_none(self):
        assert get_adapter("splunk") is None
        assert get_adapter("") is None
