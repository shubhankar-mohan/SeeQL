"""
Integration tests for api/webhook_routes.py.

Uses FastAPI's TestClient. Each test overrides config and resets the
in-memory rate limiter so tests are independent.
"""

import hmac
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import config as config_module
from storage.connection import reset_connections

from api.app import create_app
from api.webhook_routes import _reset_rate_limiter_for_tests


SECRET = "test-secret"


def _sign_generic(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(mon_db):
    _, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "webhooks": {
            "enabled": True,
            "dedup_window_minutes": 5,
            "rate_limit_per_minute": 60,
            "providers": {
                "generic": {
                    "enabled": True,
                    "secret": SECRET,
                    "default_server_id": "prod-primary",
                },
                "gcp": {"enabled": False, "secret": "unused"},
                "pagerduty": {"enabled": False, "secret": "unused"},
                "grafana": {"enabled": False, "secret": "unused"},
            },
        },
        "investigator": {
            "max_concurrent_per_server": 2,
        },
        "alerting": {"enabled": False},
    }
    reset_connections()
    _reset_rate_limiter_for_tests()

    # Monkey-patch investigator.run_investigation so background threads don't
    # actually invoke the LLM or hit MySQL. We mutate the row in-place so the
    # router's return payload is still meaningful.
    import alerting.investigator as INV
    original = INV.run_investigation
    INV.run_investigation = lambda inv_id: {"status": "stub", "id": inv_id}

    app = create_app()
    with TestClient(app) as c:
        yield c

    INV.run_investigation = original
    config_module._config = prev
    reset_connections()


def _post(client, path, payload):
    body = json.dumps(payload).encode()
    sig = _sign_generic(body)
    return client.post(
        path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-SeeQL-Signature": sig,
        },
    )


class TestWebhookHappyPath:
    def test_accepts_signed_generic(self, client):
        resp = _post(client, "/webhooks/generic", {
            "alert_type": "missing_index",
            "severity": "warning",
            "summary": "Slow query on members",
            "external_id": "ext-happy-1",
        })
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["alert_type"] == "missing_index"
        assert body["server_id"] == "prod-primary"
        assert body["investigation_id"] >= 1
        assert body["inbound_alert_id"] >= 1

    def test_dedup_returns_same_investigation_id(self, client):
        payload = {
            "alert_type": "missing_index",
            "severity": "warning",
            "summary": "Repeated",
            "external_id": "dedup-key-1",
        }
        first = _post(client, "/webhooks/generic", payload)
        second = _post(client, "/webhooks/generic", payload)
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["investigation_id"] == second.json()["investigation_id"]
        assert second.json()["status"] == "dedup"

    def test_different_external_ids_create_distinct_investigations(self, client):
        r1 = _post(client, "/webhooks/generic", {
            "alert_type": "lock_cascade", "severity": "critical",
            "summary": "a", "external_id": "distinct-a",
        })
        r2 = _post(client, "/webhooks/generic", {
            "alert_type": "lock_cascade", "severity": "critical",
            "summary": "b", "external_id": "distinct-b",
        })
        assert r1.json()["investigation_id"] != r2.json()["investigation_id"]


class TestSecurity:
    def test_missing_signature_rejected(self, client):
        body = json.dumps({"external_id": "x"}).encode()
        resp = client.post(
            "/webhooks/generic",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 401

    def test_tampered_body_rejected(self, client):
        body = json.dumps({"external_id": "x", "summary": "orig"}).encode()
        sig = _sign_generic(body)
        tampered = body.replace(b"orig", b"evil")
        resp = client.post(
            "/webhooks/generic",
            data=tampered,
            headers={
                "Content-Type": "application/json",
                "X-SeeQL-Signature": sig,
            },
        )
        assert resp.status_code == 401

    def test_disabled_provider_returns_404(self, client):
        body = json.dumps({"x": 1}).encode()
        sig = _sign_generic(body)
        resp = client.post(
            "/webhooks/pagerduty",
            data=body,
            headers={"Content-Type": "application/json", "X-PagerDuty-Signature": f"v1={sig}"},
        )
        assert resp.status_code == 404

    def test_unknown_provider_returns_404(self, client):
        body = b"{}"
        resp = client.post(
            "/webhooks/splunk",
            data=body,
            headers={"X-SeeQL-Signature": _sign_generic(body)},
        )
        assert resp.status_code == 404


class TestConcurrencyAndRate:
    def test_concurrency_cap_returns_429(self, client):
        # max_concurrent_per_server = 2. Post three alerts with distinct
        # external_ids → first two accepted, third gets 429.
        for i in range(2):
            r = _post(client, "/webhooks/generic", {
                "alert_type": "lock_cascade", "severity": "critical",
                "summary": f"lock {i}", "external_id": f"cap-{i}",
            })
            assert r.status_code == 202
        r3 = _post(client, "/webhooks/generic", {
            "alert_type": "lock_cascade", "severity": "critical",
            "summary": "third", "external_id": "cap-3",
        })
        assert r3.status_code == 429

    def test_rate_limit_returns_429(self, client):
        # Override rate to 1/min to make this deterministic.
        config_module._config["webhooks"]["rate_limit_per_minute"] = 1
        _reset_rate_limiter_for_tests()

        ok = _post(client, "/webhooks/generic", {
            "alert_type": "lock_cascade", "severity": "critical",
            "summary": "first", "external_id": "rate-1",
        })
        assert ok.status_code == 202
        limited = _post(client, "/webhooks/generic", {
            "alert_type": "lock_cascade", "severity": "critical",
            "summary": "second", "external_id": "rate-2",
        })
        assert limited.status_code == 429

    def test_invalid_signature_does_not_consume_rate_budget(self, client):
        # Rate limit must be charged AFTER signature verification, so a flood
        # of unauthenticated garbage cannot drain the bucket and 429 a real
        # signed alert.
        config_module._config["webhooks"]["rate_limit_per_minute"] = 1
        _reset_rate_limiter_for_tests()

        for i in range(5):
            body = json.dumps({"external_id": f"bad-{i}"}).encode()
            r = client.post(
                "/webhooks/generic",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-SeeQL-Signature": "sha256=deadbeef",
                },
            )
            assert r.status_code == 401

        # The single token is still available for the first valid request.
        ok = _post(client, "/webhooks/generic", {
            "alert_type": "lock_cascade", "severity": "critical",
            "summary": "valid", "external_id": "valid-after-flood",
        })
        assert ok.status_code == 202


class TestPersistence:
    def test_payload_persisted_verbatim(self, client, mon_db):
        payload = {
            "alert_type": "missing_index",
            "severity": "warning",
            "summary": "persist test",
            "external_id": "persist-1",
            "context": {"digest": "0xABC"},
        }
        resp = _post(client, "/webhooks/generic", payload)
        assert resp.status_code == 202
        alert_id = resp.json()["inbound_alert_id"]

        conn, _ = mon_db
        row = conn.execute(
            "SELECT provider, alert_type, severity, external_id, payload, signature_verified "
            "FROM inbound_alerts WHERE id = ?", (alert_id,)
        ).fetchone()
        assert row["provider"] == "generic"
        assert row["alert_type"] == "missing_index"
        assert row["external_id"] == "persist-1"
        assert row["signature_verified"] == 1
        stored = json.loads(row["payload"])
        assert stored["context"] == {"digest": "0xABC"}

    def test_webhook_disabled_returns_404(self, client):
        config_module._config["webhooks"]["enabled"] = False
        body = b"{}"
        resp = client.post(
            "/webhooks/generic",
            data=body,
            headers={"X-SeeQL-Signature": _sign_generic(body)},
        )
        assert resp.status_code == 404
