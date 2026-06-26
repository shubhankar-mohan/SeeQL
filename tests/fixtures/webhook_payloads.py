"""Canned webhook payload fixtures per provider — used by adapter tests."""


GENERIC_MISSING_INDEX = {
    "alert_type": "missing_index",
    "severity": "warning",
    "summary": "Query 0xABC scans 1M rows, returns 1",
    "external_id": "generic-test-1",
    "server_id": "prod-primary",
    "fired_at": "2026-04-23T12:00:00Z",
    "context": {"digest": "0xABC", "table": "members"},
}


GENERIC_MINIMAL = {
    "severity": "critical",
    "summary": "Something bad",
    # no alert_type, no external_id
}


GCP_HIGH_CPU_OPEN = {
    "version": "1.2",
    "incident": {
        "incident_id": "gcp-incident-123",
        "policy_name": "high-cpu-policy prod",
        "condition_name": "CPU > 90",
        "state": "OPEN",
        "started_at": 1735000000,
        "summary": "CPU utilization > 90%",
        "resource": {
            "labels": {
                "database_id": "demo-prj:asia-south1:prod-primary",
            }
        },
    },
}


GCP_SLOW_QUERY_CLOSED = {
    "version": "1.2",
    "incident": {
        "incident_id": "gcp-incident-456",
        "policy_name": "slow-query-policy",
        "state": "CLOSED",
        "started_at": "2026-04-23T12:00:00Z",
        "summary": "Slow query rate elevated",
        "resource": {"labels": {"database_id": "prj:r:prod-primary"}},
    },
}


PAGERDUTY_HIGH = {
    "event": {
        "id": "evt-xyz",
        "event_type": "incident.triggered",
        "occurred_at": "2026-04-23T12:00:00Z",
        "data": {
            "incident": {
                "id": "PXYZ123",
                "title": "Lock wait cascade on members table",
                "urgency": "high",
                "status": "triggered",
                "service": {"summary": "prod-primary"},
                "custom_details": {
                    "alert_type": "lock_cascade",
                    "server_id": "prod-primary",
                },
            }
        },
    }
}


PAGERDUTY_LOW = {
    "event": {
        "id": "evt-abc",
        "event_type": "incident.triggered",
        "occurred_at": "2026-04-23T12:00:00Z",
        "data": {
            "incident": {
                "id": "PABC456",
                "title": "Informational",
                "urgency": "low",
                "status": "triggered",
                "service": {"summary": "prod-primary"},
            }
        },
    }
}


GRAFANA_QUERY_REGRESSION = {
    "version": "4",
    "status": "firing",
    "receiver": "seeql",
    "commonLabels": {"server_id": "prod-primary"},
    "alerts": [
        {
            "status": "firing",
            "fingerprint": "grafana-fp-999",
            "startsAt": "2026-04-23T12:00:00Z",
            "labels": {
                "alertname": "Query Regression on digest 0xDEF",
                "severity": "critical",
                "instance": "prod-primary",
            },
            "annotations": {
                "summary": "Query 0xDEF avg_time went from 0.02s to 0.45s",
            },
        }
    ],
}


GRAFANA_MULTI_ALERT = {
    "version": "4",
    "status": "firing",
    "receiver": "seeql",
    "alerts": [
        {
            "status": "resolved",
            "fingerprint": "fp-resolved",
            "startsAt": "2026-04-23T11:00:00Z",
            "labels": {"alertname": "old", "severity": "warning"},
            "annotations": {"summary": "old resolved"},
        },
        {
            "status": "firing",
            "fingerprint": "fp-firing",
            "startsAt": "2026-04-23T12:00:00Z",
            "labels": {
                "alertname": "CPU utilization high",
                "severity": "critical",
                "instance": "prod-replica",
            },
            "annotations": {"summary": "High CPU"},
        },
    ],
}
