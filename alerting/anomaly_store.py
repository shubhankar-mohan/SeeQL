"""
Anomaly event persistence (Phase 1.3).

Bridges the ephemeral `AnomalyResult` objects produced by `alerting.anomaly`
into the persistent `anomaly_events` table. Separate from `alerting.anomaly`
itself so the detector stays pure (zero SQLite writes) and the scheduler
owns the storage lifecycle.
"""

from __future__ import annotations

import logging
from typing import Iterable

from alerting.anomaly import AnomalyResult
from storage import writer

logger = logging.getLogger(__name__)


def persist(results: Iterable[AnomalyResult]) -> list[int]:
    """
    Write a list of AnomalyResult objects to the anomaly_events table.

    Returns the list of inserted row IDs in the same order as the input —
    used by `alerting.incidents.update_windows` to know which rows need an
    `incident_id` update after grouping.
    """
    rows: list[dict] = []
    for r in results:
        rows.append(
            {
                "detected_at": r.detected_at,
                "server_id": r.server_id,
                "metric_name": r.metric,
                "current_value": float(r.current),
                "baseline_mean": float(r.baseline_mean),
                "baseline_stddev": float(r.baseline_stddev),
                "z_score": float(r.z_score),
                "pct_change": float(r.pct_change),
                "direction": r.direction,
                "severity": r.severity,
                "incident_id": None,
            }
        )

    if not rows:
        return []

    try:
        return writer.write_anomaly_events(rows)
    except Exception as e:
        logger.warning(f"Failed to persist {len(rows)} anomaly events: {e}")
        return []
