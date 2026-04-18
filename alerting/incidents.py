"""
Incident window builder (Phase 1.4).

Groups rows in `anomaly_events` into `incident_windows` using gap-based
clustering with a max-duration cap:

1. Query ungrouped events (incident_id IS NULL) for a server, ordered by time.
2. For each event, find the most recent OPEN incident for this server whose
   `end_time` is within `incident_gap_minutes` of the event AND whose total
   duration is under `incident_max_duration_minutes`.
3. If found, extend that incident (push end_time, merge metric, bump count,
   upgrade severity). Otherwise create a new one.
4. Set `incident_id` on the event.

All writes happen inside a single connection so `event_count` and
`anomaly_events.incident_id` can't drift on crash — SQLite's default
autocommit-on-exit gives us transaction semantics for the whole batch.

Returns the list of newly-created incident IDs so the scheduler can fire a
Slack notification (see Phase 1.11) only for *new* incidents — not for
extensions of existing ones.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from config import get_config
from storage.connection import get_mon_connection

logger = logging.getLogger(__name__)


# Severity ordering: higher rank overrides lower when merging events
_SEVERITY_RANK = {"warning": 1, "critical": 2}


def _gap_minutes() -> int:
    """Max gap (minutes) between consecutive events in one incident."""
    return int(
        get_config().get("alerting", {}).get("incident_gap_minutes", 15)
    )


def _max_duration_minutes() -> int:
    """Cap on total incident duration. Events beyond this start a new incident."""
    return int(
        get_config()
        .get("alerting", {})
        .get("incident_max_duration_minutes", 120)
    )


def update_windows(server_id: str) -> list[int]:
    """
    Process ungrouped anomaly events for one server.

    Returns:
        IDs of any NEWLY-CREATED incident windows (empty list if all events
        were attached to existing open incidents). Used by the scheduler to
        fire Slack notifications only on new incidents.
    """
    gap_min = _gap_minutes()
    max_dur_min = _max_duration_minutes()
    new_ids: list[int] = []

    with get_mon_connection() as conn:
        # Ordered oldest → newest so each event sees any incidents already
        # extended by earlier events in this batch.
        events = conn.execute(
            """
            SELECT id, detected_at, metric_name, severity
            FROM anomaly_events
            WHERE incident_id IS NULL AND server_id = ?
            ORDER BY detected_at ASC
            """,
            (server_id,),
        ).fetchall()

        for event in events:
            incident_id, created = _attach_or_create(
                conn, server_id, event, gap_min, max_dur_min
            )
            if created and incident_id not in new_ids:
                new_ids.append(incident_id)

    if new_ids:
        _notify_slack(new_ids, server_id)

    return new_ids


def _attach_or_create(
    conn, server_id: str, event: Any, gap_min: int, max_dur_min: int
) -> tuple[int, bool]:
    """
    Attach an event to an open incident, or create a new one.

    Returns:
        (incident_id, was_created) — `was_created` is True when a new window
        was created for this event.
    """
    # Find the most recent open incident that:
    #   - belongs to this server
    #   - has end_time within gap_min of our event's detected_at (the event is
    #     not too far after the last event in the incident)
    #   - total span start→end is under the max duration cap
    row = conn.execute(
        """
        SELECT id, start_time, end_time, severity, involved_metrics, event_count
        FROM incident_windows
        WHERE server_id = ?
          AND status = 'detected'
          AND datetime(end_time) >= datetime(?, ?)
          AND (julianday(?) - julianday(start_time)) * 1440.0 < ?
        ORDER BY end_time DESC
        LIMIT 1
        """,
        (
            server_id,
            event["detected_at"],
            f"-{gap_min} minutes",
            event["detected_at"],
            max_dur_min,
        ),
    ).fetchone()

    if row:
        # Extend the open incident
        metrics = json.loads(row["involved_metrics"])
        if event["metric_name"] not in metrics:
            metrics.append(event["metric_name"])

        new_severity = row["severity"]
        if _SEVERITY_RANK.get(event["severity"], 0) > _SEVERITY_RANK.get(
            new_severity, 0
        ):
            new_severity = event["severity"]

        conn.execute(
            """
            UPDATE incident_windows
            SET end_time = ?,
                severity = ?,
                involved_metrics = ?,
                event_count = event_count + 1
            WHERE id = ?
            """,
            (event["detected_at"], new_severity, json.dumps(metrics), row["id"]),
        )
        incident_id = row["id"]
        created = False
    else:
        # Create a new incident window
        cursor = conn.execute(
            """
            INSERT INTO incident_windows
              (server_id, start_time, end_time, severity, involved_metrics,
               event_count, status)
            VALUES (?, ?, ?, ?, ?, 1, 'detected')
            """,
            (
                server_id,
                event["detected_at"],
                event["detected_at"],
                event["severity"],
                json.dumps([event["metric_name"]]),
            ),
        )
        incident_id = cursor.lastrowid
        created = True

    # Tag the event so it won't be picked up again on the next pipeline run
    conn.execute(
        "UPDATE anomaly_events SET incident_id = ? WHERE id = ?",
        (incident_id, event["id"]),
    )

    return incident_id, created


# ---------------------------------------------------------------------------
# Slack notification (Phase 1.11)
# ---------------------------------------------------------------------------
def _notify_slack(new_ids: list[int], server_id: str) -> None:
    """
    Fire a Slack message for each newly-created incident window.

    Uses the existing SlackChannel wiring. Guarded on
    `alerting.channels.slack.enabled` — silently skipped if disabled or
    webhook is unresolved.
    """
    cfg = (
        get_config()
        .get("alerting", {})
        .get("channels", {})
        .get("slack", {})
    )
    if not cfg.get("enabled"):
        return
    webhook = cfg.get("webhook_url") or ""
    if not webhook or webhook.startswith("${"):
        return

    try:
        from alerting.channels import SlackChannel
        from alerting.models import Alert, Severity
        from storage.connection import get_mon_reader
    except ImportError as e:
        logger.debug(f"Slack modules unavailable: {e}")
        return

    channel = SlackChannel(webhook)

    with get_mon_reader() as conn:
        for incident_id in new_ids:
            row = conn.execute(
                """
                SELECT id, start_time, severity, involved_metrics, event_count
                FROM incident_windows
                WHERE id = ?
                """,
                (incident_id,),
            ).fetchone()
            if not row:
                continue

            metrics = ", ".join(json.loads(row["involved_metrics"]))
            sev = (
                Severity.CRITICAL if row["severity"] == "critical" else Severity.WARNING
            )
            alert = Alert(
                rule_name=f"incident_detected:{server_id}",
                severity=sev,
                message=(
                    f":rotating_light: Incident detected — {row['severity']} "
                    f"on `{server_id}`\n"
                    f"Metrics: {metrics}\n"
                    f"Started: {row['start_time']}\n"
                    f"Events: {row['event_count']}\n"
                    f"Run: `python main.py replay --incident {row['id']}`"
                ),
                context={
                    "server_id": server_id,
                    "incident_id": row["id"],
                    "severity": row["severity"],
                    "metrics": metrics,
                },
            )
            try:
                channel.send(alert)
            except Exception as e:
                logger.warning(f"Slack send failed for incident {row['id']}: {e}")
