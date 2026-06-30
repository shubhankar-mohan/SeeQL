"""
Anomaly detection — statistical baseline comparison.

Computes baselines from historical data and detects deviations.
Uses same-hour-same-weekday comparisons and rolling averages to
account for natural traffic patterns.

Metrics monitored:
  - QPS (queries per second)
  - Threads_running
  - Threads_connected
  - CPU utilization
  - Memory utilization
  - Lock wait frequency
  - Buffer pool hit ratio (drop detection)

(Query latency per-digest is planned but not yet implemented; 7 metrics
are active in METRIC_CONFIGS.)
"""

import json
import logging
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from alerting.models import Alert, Severity
from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-cycle cache (Phase 1.2)
# ---------------------------------------------------------------------------
# detect_anomalies() is called from two places every medium loop:
#   1. agent/state_builder.py — to render anomalies into the state report
#   2. alerting/engine.py via evaluate_anomaly() — to fire anomaly alerts
# Computing baselines twice per cycle is wasted work. The cache key combines
# server_id + z-threshold + the current wall-clock minute, so a cycle sees
# the same result and the next cycle sees fresh computation.
_detect_cache: dict[tuple, list] = {}
_cache_lock = threading.Lock()


def _cache_key(server_id: str, z_override: float | None) -> tuple:
    minute = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    return (server_id, z_override, minute)


def _clear_cache():
    """Used by tests."""
    with _cache_lock:
        _detect_cache.clear()


@dataclass
class Baseline:
    """Statistical baseline for a metric."""
    metric: str
    mean: float
    stddev: float
    sample_count: int
    current: float = 0.0

    @property
    def z_score(self) -> float:
        """How many standard deviations from the mean."""
        if self.stddev == 0:
            return 0.0 if self.current == self.mean else float('inf')
        return (self.current - self.mean) / self.stddev

    @property
    def pct_change(self) -> float:
        """Percentage change from baseline mean."""
        if self.mean == 0:
            return 0.0
        return ((self.current - self.mean) / self.mean) * 100


@dataclass
class AnomalyResult:
    """A detected anomaly."""
    metric: str
    current: float
    baseline_mean: float
    baseline_stddev: float
    z_score: float
    pct_change: float
    direction: str  # "high" or "low"
    severity: str   # "warning" or "critical"
    server_id: str = "default"
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Baseline computation queries
# ---------------------------------------------------------------------------

# Helper: normalize ISO timestamps with 'T' separator for SQLite datetime()
# All baseline queries use _TS() macro to handle both '2026-03-08T13:29:14' and '2026-03-08 13:29:14'
_TS = "REPLACE({time_col}, 'T', ' ')"

# Same hour, same day-of-week, last 4 weeks
_BASELINE_SAME_HOUR_DOW = """
SELECT AVG(val) as mean,
       CASE WHEN COUNT(*) > 1
            THEN SQRT(SUM((val - avg_sub.avg_val) * (val - avg_sub.avg_val)) / (COUNT(*) - 1))
            ELSE 0 END as stddev,
       COUNT(*) as cnt
FROM (
    SELECT {value_col} as val
    FROM {table}
    WHERE datetime(""" + _TS + """) >= datetime('now', '-28 days')
      AND datetime(""" + _TS + """) < datetime('now', '-10 minutes')
      AND strftime('%w', """ + _TS + """) = strftime('%w', 'now')
      AND strftime('%H', """ + _TS + """) = strftime('%H', 'now')
      {extra_where}
) sub
CROSS JOIN (
    SELECT AVG({value_col}) as avg_val
    FROM {table}
    WHERE datetime(""" + _TS + """) >= datetime('now', '-28 days')
      AND datetime(""" + _TS + """) < datetime('now', '-10 minutes')
      AND strftime('%w', """ + _TS + """) = strftime('%w', 'now')
      AND strftime('%H', """ + _TS + """) = strftime('%H', 'now')
      {extra_where}
) avg_sub
"""

# Rolling 24-hour baseline (for metrics without strong weekly patterns)
_BASELINE_24H = """
SELECT AVG(val) as mean,
       CASE WHEN COUNT(*) > 1
            THEN SQRT(SUM((val - avg_sub.avg_val) * (val - avg_sub.avg_val)) / (COUNT(*) - 1))
            ELSE 0 END as stddev,
       COUNT(*) as cnt
FROM (
    SELECT {value_col} as val
    FROM {table}
    WHERE datetime(""" + _TS + """) >= datetime('now', '-24 hours')
      AND datetime(""" + _TS + """) < datetime('now', '-30 minutes')
      {extra_where}
) sub
CROSS JOIN (
    SELECT AVG({value_col}) as avg_val
    FROM {table}
    WHERE datetime(""" + _TS + """) >= datetime('now', '-24 hours')
      AND datetime(""" + _TS + """) < datetime('now', '-30 minutes')
      {extra_where}
) avg_sub
"""

# Fallback: all available data excluding last 10 minutes (for bootstrapping)
# Uses REPLACE to normalize ISO 'T' separator to space for datetime() comparison
_BASELINE_ALL = """
SELECT AVG(val) as mean,
       CASE WHEN COUNT(*) > 1
            THEN SQRT(SUM((val - avg_sub.avg_val) * (val - avg_sub.avg_val)) / (COUNT(*) - 1))
            ELSE 0 END as stddev,
       COUNT(*) as cnt
FROM (
    SELECT {value_col} as val
    FROM {table}
    WHERE datetime(REPLACE({time_col}, 'T', ' ')) < datetime('now', '-10 minutes')
      {extra_where}
) sub
CROSS JOIN (
    SELECT AVG({value_col}) as avg_val
    FROM {table}
    WHERE datetime(REPLACE({time_col}, 'T', ' ')) < datetime('now', '-10 minutes')
      {extra_where}
) avg_sub
"""


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

#
# METRIC_CONFIGS — every query includes `AND server_id = ?` so a multi-server
# deployment computes independent baselines per server. The `extra_where`
# strings are used TWICE in the baseline templates (once in `sub`, once in
# `avg_sub`), so any caller of compute_baseline passes (server_id, server_id).
# current_query / baseline_query take a single (server_id,).
#
METRIC_CONFIGS = {
    "qps": {
        "table": "global_status_snapshots",
        "time_col": "snapshot_time",
        "value_col": "per_second",
        "extra_where": "AND server_id = ? AND variable_name = 'Queries' AND per_second IS NOT NULL",
        "current_query": """
            SELECT per_second as val FROM global_status_snapshots
            WHERE variable_name = 'Queries' AND per_second IS NOT NULL
              AND server_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """,
        "baseline_type": "same_hour",
        "high_z": 3.0,
        "low_z": -3.0,
        "min_samples": 3,
        "description": "Queries per second",
    },
    "threads_running": {
        "table": "global_status_snapshots",
        "time_col": "snapshot_time",
        "value_col": "raw_value",
        "extra_where": "AND server_id = ? AND variable_name = 'Threads_running'",
        "current_query": """
            SELECT raw_value as val FROM global_status_snapshots
            WHERE variable_name = 'Threads_running'
              AND server_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """,
        "baseline_type": "same_hour",
        "high_z": 3.0,
        "low_z": None,  # low threads_running is fine
        "min_samples": 3,
        "description": "Active threads",
    },
    "threads_connected": {
        "table": "global_status_snapshots",
        "time_col": "snapshot_time",
        "value_col": "raw_value",
        "extra_where": "AND server_id = ? AND variable_name = 'Threads_connected'",
        "current_query": """
            SELECT raw_value as val FROM global_status_snapshots
            WHERE variable_name = 'Threads_connected'
              AND server_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """,
        "baseline_type": "24h",
        "high_z": 3.0,
        "low_z": None,
        "min_samples": 5,
        "description": "Connected threads",
    },
    "cpu_utilization": {
        "table": "gcp_metric_snapshots",
        "time_col": "snapshot_time",
        "value_col": "value",
        "extra_where": "AND server_id = ? AND metric_name = 'cpu_utilization'",
        "current_query": """
            SELECT value as val FROM gcp_metric_snapshots
            WHERE metric_name = 'cpu_utilization'
              AND server_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """,
        "baseline_type": "same_hour",
        "high_z": 2.5,
        "low_z": None,
        "min_samples": 3,
        "description": "CPU utilization",
    },
    "memory_utilization": {
        "table": "gcp_metric_snapshots",
        "time_col": "snapshot_time",
        "value_col": "value",
        "extra_where": "AND server_id = ? AND metric_name = 'memory_utilization'",
        "current_query": """
            SELECT value as val FROM gcp_metric_snapshots
            WHERE metric_name = 'memory_utilization'
              AND server_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """,
        "baseline_type": "24h",
        "high_z": 2.5,
        "low_z": None,
        "min_samples": 5,
        "description": "Memory utilization",
    },
    "lock_frequency": {
        "table": "lock_wait_snapshots",
        "time_col": "snapshot_time",
        "value_col": "cnt",
        "extra_where": "AND server_id = ?",
        "current_query": """
            SELECT COUNT(*) as val FROM lock_wait_snapshots
            WHERE snapshot_time >= datetime('now', '-5 minutes')
              AND server_id = ?
        """,
        "baseline_query": """
            SELECT AVG(val) as mean,
                   CASE WHEN COUNT(*) > 1
                        THEN SQRT(SUM((val - avg_sub.avg_val) * (val - avg_sub.avg_val)) / (COUNT(*) - 1))
                        ELSE 0 END as stddev,
                   COUNT(*) as cnt
            FROM (
                SELECT COUNT(*) as val
                FROM lock_wait_snapshots
                WHERE snapshot_time >= datetime('now', '-7 days')
                  AND snapshot_time < datetime('now', '-30 minutes')
                  AND server_id = ?
                GROUP BY strftime('%Y-%m-%d %H', snapshot_time)
            ) sub
            CROSS JOIN (
                SELECT AVG(val) as avg_val FROM (
                    SELECT COUNT(*) as val
                    FROM lock_wait_snapshots
                    WHERE snapshot_time >= datetime('now', '-7 days')
                      AND snapshot_time < datetime('now', '-30 minutes')
                      AND server_id = ?
                    GROUP BY strftime('%Y-%m-%d %H', snapshot_time)
                )
            ) avg_sub
        """,
        "baseline_type": "custom",
        "high_z": 2.5,
        "low_z": None,
        "min_samples": 3,
        "description": "Lock waits per interval",
    },
    # IMPORTANT: buffer_pool_hit_ratio deliberately bypasses the stale
    # buffer_pool_snapshots.hit_ratio column (see Phase 0.5 / audit finding #4).
    # HIT_RATE in INNODB_BUFFER_POOL_STATS is an instantaneous ~1-second
    # sample and returns 0 on idle intervals — broken for anomaly detection.
    # Both current_query and baseline_query compute the cumulative ratio
    # from Innodb_buffer_pool_reads / _read_requests in global_status_snapshots,
    # matching what api.query_helpers.latest_hit_ratio_pct does for the UI.
    "buffer_pool_hit_ratio": {
        "table": "global_status_snapshots",
        "time_col": "snapshot_time",
        "value_col": "val",  # unused; custom queries compute their own val
        "extra_where": "",   # unused; baseline_type=custom skips templates
        "current_query": """
            SELECT
                1.0 - (CAST(reads.raw_value AS REAL) / NULLIF(requests.raw_value, 0))
                AS val
            FROM global_status_snapshots reads
            JOIN global_status_snapshots requests
              ON requests.snapshot_time = reads.snapshot_time
             AND requests.server_id = reads.server_id
            WHERE reads.variable_name = 'Innodb_buffer_pool_reads'
              AND requests.variable_name = 'Innodb_buffer_pool_read_requests'
              AND reads.server_id = ?
              AND requests.raw_value > 0
            ORDER BY reads.snapshot_time DESC LIMIT 1
        """,
        "baseline_query": """
            WITH ratios AS (
                SELECT
                    1.0 - (CAST(reads.raw_value AS REAL) / NULLIF(requests.raw_value, 0))
                        AS val
                FROM global_status_snapshots reads
                JOIN global_status_snapshots requests
                  ON requests.snapshot_time = reads.snapshot_time
                 AND requests.server_id = reads.server_id
                WHERE reads.variable_name = 'Innodb_buffer_pool_reads'
                  AND requests.variable_name = 'Innodb_buffer_pool_read_requests'
                  AND reads.server_id = ?
                  AND requests.raw_value > 0
                  AND datetime(reads.snapshot_time) >= datetime('now', '-24 hours')
                  AND datetime(reads.snapshot_time) < datetime('now', '-30 minutes')
            )
            SELECT AVG(val) as mean,
                   CASE WHEN COUNT(*) > 1
                        THEN SQRT(SUM((val - avg_sub.avg_val) * (val - avg_sub.avg_val)) / (COUNT(*) - 1))
                        ELSE 0 END as stddev,
                   COUNT(*) as cnt
            FROM ratios
            CROSS JOIN (SELECT AVG(val) as avg_val FROM ratios) avg_sub
        """,
        "baseline_type": "custom",  # escapes the format-string templates
        "high_z": None,  # high hit ratio is good
        "low_z": -2.5,   # detect drops
        "min_samples": 5,
        "description": "Buffer pool hit ratio",
    },
}


# ---------------------------------------------------------------------------
# Core detection logic
# ---------------------------------------------------------------------------

def _resolve_server_id(server_id: str | None) -> str:
    if server_id:
        return server_id
    try:
        from config.server_registry import get_server_registry
        return get_server_registry().get_default_server_id()
    except Exception:
        return "default"


def compute_baseline(
    metric_name: str,
    config: dict,
    server_id: str | None = None,
) -> Baseline | None:
    """Compute statistical baseline for a metric on a specific server.

    Tries the preferred baseline window first, then falls back to all
    available data if there aren't enough samples (bootstrapping phase).
    """
    sid = _resolve_server_id(server_id)
    min_samples = config.get("min_samples", 3)

    try:
        with get_mon_reader() as conn:
            # Get current value (current_query has exactly one `?` for server_id)
            row = conn.execute(config["current_query"], (sid,)).fetchone()
            if not row or row["val"] is None:
                return None
            current = float(row["val"])

            # Try primary baseline
            brow = _query_baseline(conn, config, sid)

            # Fall back to all available data if insufficient samples
            if not brow or brow["cnt"] < min_samples:
                if config["baseline_type"] != "custom":
                    query = _BASELINE_ALL.format(**config)
                    # _BASELINE_ALL uses {extra_where} twice → two server_id params
                    brow = conn.execute(query, (sid, sid)).fetchone()

            if not brow or brow["cnt"] < min_samples:
                return None

            mean = float(brow["mean"] or 0)
            stddev = float(brow["stddev"] or 0)

            # Guard: if stddev is 0 and we have few samples, skip
            # (not enough variance data to detect anomalies).
            # With many samples and zero stddev, use a small epsilon
            # so any meaningful deviation is detected.
            if stddev == 0:
                if brow["cnt"] < 10:
                    return None
                # All historical values identical — use 1% of mean as pseudo-stddev
                stddev = abs(mean) * 0.01 if mean != 0 else 0.001

            return Baseline(
                metric=metric_name,
                mean=mean,
                stddev=stddev,
                sample_count=int(brow["cnt"]),
                current=current,
            )
    except Exception as e:
        logger.debug(f"Baseline computation failed for {metric_name}/{sid}: {e}")
        return None


def _query_baseline(conn, config: dict, server_id: str):
    """Run the primary baseline query for a specific server.

    For custom baseline_query, we count placeholders and pass server_id
    that many times — different custom queries have different numbers of
    `server_id = ?` clauses (lock_frequency has 2, buffer_pool_hit_ratio
    has 1 via a CTE).
    """
    if config["baseline_type"] == "custom":
        q = config["baseline_query"]
        n = q.count("?")
        params = tuple([server_id] * n)
        return conn.execute(q, params).fetchone()
    elif config["baseline_type"] == "same_hour":
        query = _BASELINE_SAME_HOUR_DOW.format(**config)
        # {extra_where} is inlined twice in the template
        return conn.execute(query, (server_id, server_id)).fetchone()
    else:  # 24h
        query = _BASELINE_24H.format(**config)
        return conn.execute(query, (server_id, server_id)).fetchone()


def _detect_anomalies_uncached(
    server_id: str,
    z_threshold_override: float | None,
) -> list[AnomalyResult]:
    """The uncached version of detect_anomalies — call through the public
    function so the per-cycle cache applies."""
    anomalies = []
    ts = datetime.now(timezone.utc).isoformat()

    for metric_name, config in METRIC_CONFIGS.items():
        baseline = compute_baseline(metric_name, config, server_id=server_id)
        if baseline is None:
            continue

        # A metric with high_z/low_z = None has opted OUT of that side of
        # detection (e.g. buffer_pool_hit_ratio disables high-side because a high
        # hit ratio is good). An alert-path z override must not re-enable it —
        # only tighten/loosen a side that is already active.
        high_z = config.get("high_z")
        low_z = config.get("low_z")
        if z_threshold_override and high_z is not None:
            high_z = z_threshold_override
        if z_threshold_override and low_z is not None:
            low_z = -z_threshold_override

        z = baseline.z_score
        pct = baseline.pct_change

        # Check for high anomaly
        if high_z is not None and z >= high_z:
            severity = "critical" if z >= high_z * 1.5 else "warning"
            anomalies.append(AnomalyResult(
                metric=metric_name,
                current=baseline.current,
                baseline_mean=baseline.mean,
                baseline_stddev=baseline.stddev,
                z_score=z,
                pct_change=pct,
                direction="high",
                severity=severity,
                server_id=server_id,
                detected_at=ts,
            ))

        # Check for low anomaly
        elif low_z is not None and z <= low_z:
            severity = "critical" if z <= low_z * 1.5 else "warning"
            anomalies.append(AnomalyResult(
                metric=metric_name,
                current=baseline.current,
                baseline_mean=baseline.mean,
                baseline_stddev=baseline.stddev,
                z_score=z,
                pct_change=pct,
                direction="low",
                severity=severity,
                server_id=server_id,
                detected_at=ts,
            ))

    # Sort: critical first, then by absolute z-score
    anomalies.sort(key=lambda a: (0 if a.severity == "critical" else 1, -abs(a.z_score)))
    return anomalies


def detect_anomalies(
    server_id: str | None = None,
    z_threshold_override: float | None = None,
) -> list[AnomalyResult]:
    """
    Run anomaly detection across all configured metrics for a specific server.

    Results are cached per (server_id, z_override, cycle_minute) — back-to-back
    calls in the same wall-clock minute return the same list, so state_builder
    and the alert engine can each call this without duplicate baseline queries.
    """
    sid = _resolve_server_id(server_id)
    key = _cache_key(sid, z_threshold_override)

    with _cache_lock:
        cached = _detect_cache.get(key)
        if cached is not None:
            return cached

    results = _detect_anomalies_uncached(sid, z_threshold_override)

    with _cache_lock:
        # Trim old cache entries to keep memory bounded
        if len(_detect_cache) > 32:
            _detect_cache.clear()
        _detect_cache[key] = results

    return results


# ---------------------------------------------------------------------------
# Alert rule integration
# ---------------------------------------------------------------------------

def evaluate_anomaly(rule_config: dict, server_id: str | None = None) -> Alert | None:
    """
    Alert rule: fire if any metric has a statistical anomaly.

    Integrates with the existing alerting engine via RULE_EVALUATORS.
    """
    sid = _resolve_server_id(server_id)
    z_threshold = rule_config.get("z_threshold", 3.0)
    anomalies = detect_anomalies(server_id=sid, z_threshold_override=z_threshold)

    if not anomalies:
        return None

    # Build message from worst anomaly
    worst = anomalies[0]
    direction_word = "above" if worst.direction == "high" else "below"
    desc = METRIC_CONFIGS.get(worst.metric, {}).get("description", worst.metric)

    message_parts = []
    for a in anomalies[:3]:  # Top 3 in message
        d = METRIC_CONFIGS.get(a.metric, {}).get("description", a.metric)
        dir_w = "above" if a.direction == "high" else "below"
        message_parts.append(
            f"{d}: {a.current:.2f} ({a.pct_change:+.0f}% {dir_w} baseline, z={a.z_score:.1f})"
        )

    sev = Severity.CRITICAL if worst.severity == "critical" else Severity.WARNING

    # Namespace the rule_name by server so cooldowns on one server don't
    # suppress alerts on another. The alerting engine's cooldown tracker keys
    # on rule_name, so the namespace has to happen here.
    return Alert(
        rule_name=f"anomaly_detection:{sid}",
        severity=sev,
        message=f"Anomaly detected on {sid} — {'; '.join(message_parts)}",
        context={
            "server_id": sid,
            "anomaly_count": len(anomalies),
            "anomalies": [
                {
                    "metric": a.metric,
                    "current": a.current,
                    "baseline_mean": a.baseline_mean,
                    "baseline_stddev": a.baseline_stddev,
                    "z_score": a.z_score,
                    "pct_change": a.pct_change,
                    "direction": a.direction,
                    "severity": a.severity,
                }
                for a in anomalies
            ],
        },
    )
