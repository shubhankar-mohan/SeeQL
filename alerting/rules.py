"""
Built-in alert rules. Each rule queries the monitoring DB and evaluates a
condition *for a specific server*.

Multi-server: every evaluator takes `server_id` so a lock cascade on server A
doesn't fire alerts for server B. The cooldown tracker in `alerting.engine`
keys on rule_name, so each rule namespaces its alert by server
(`rule_name:server_id`) to avoid cross-server suppression.
"""

import json
import logging

from alerting.models import Alert, Severity
from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)


def _ns(rule_name: str, server_id: str) -> str:
    """Namespace a rule name by server for cooldown isolation."""
    return f"{rule_name}:{server_id}"


def evaluate_lock_cascade(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire if multiple lock waits with long max wait — scoped to one server."""
    min_count = rule_config.get("min_count", 3)
    min_wait = rule_config.get("min_wait_seconds", 10)

    with get_mon_reader() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as cnt, MAX(wait_seconds) as max_wait
            FROM lock_wait_snapshots
            WHERE datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now', '-2 minutes')
              AND server_id = ?
        """, (server_id,)).fetchone()

    if not row or row["cnt"] < min_count or (row["max_wait"] or 0) < min_wait:
        return None

    return Alert(
        rule_name=_ns("lock_cascade", server_id),
        severity=Severity(rule_config.get("severity", "critical")),
        message=f"[{server_id}] {row['cnt']} lock waits active, longest waiting {row['max_wait']}s",
        context={
            "server_id": server_id,
            "lock_count": row["cnt"],
            "max_wait_seconds": row["max_wait"],
        },
    )


def evaluate_threads_running_spike(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire if Threads_running is N times above baseline."""
    multiplier = rule_config.get("multiplier", 4)

    with get_mon_reader() as conn:
        current = conn.execute("""
            SELECT raw_value FROM global_status_snapshots
            WHERE variable_name = 'Threads_running'
              AND server_id = ?
            ORDER BY snapshot_time DESC LIMIT 1
        """, (server_id,)).fetchone()

        # Baseline EXCLUDES the last hour. If it didn't, the ongoing spike
        # would be folded into its own baseline: the longer a sustained
        # incident runs, the more the "last 24h" average creeps toward the
        # spike value, until current/baseline drops below the multiplier and
        # the alert self-silences (P1c-7).
        baseline = conn.execute("""
            SELECT AVG(raw_value) as avg_val FROM global_status_snapshots
            WHERE variable_name = 'Threads_running'
              AND server_id = ?
              AND datetime(REPLACE(snapshot_time,'T',' ')) BETWEEN datetime('now', '-25 hours') AND datetime('now', '-1 hour')
        """, (server_id,)).fetchone()

    if not current or not baseline or not baseline["avg_val"]:
        return None

    current_val = current["raw_value"]
    baseline_val = baseline["avg_val"]

    if baseline_val > 0 and current_val >= baseline_val * multiplier:
        return Alert(
            rule_name=_ns("threads_running_spike", server_id),
            severity=Severity(rule_config.get("severity", "warning")),
            message=(
                f"[{server_id}] Threads_running={current_val} "
                f"({current_val / baseline_val:.1f}x above 24h avg of {baseline_val:.0f})"
            ),
            context={
                "server_id": server_id,
                "current": current_val,
                "baseline": baseline_val,
                "multiplier": current_val / baseline_val,
            },
        )
    return None


def evaluate_query_regression(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire if any query has regressed beyond threshold."""
    threshold = rule_config.get("threshold", 5.0)
    # Absolute floor (P1c-8): a ratio-only gate reports "5x slower" for
    # 0.1ms -> 0.5ms, which is noise, not a regression worth paging anyone.
    min_recent_avg = rule_config.get("min_recent_avg_sec", 0.01)

    with get_mon_reader() as conn:
        rows = conn.execute("""
            WITH recent AS (
                SELECT digest, digest_text, AVG(avg_time_sec) as recent_avg
                FROM query_digest_snapshots
                WHERE datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now', '-1 hour')
                  AND server_id = ?
                GROUP BY digest
            ),
            baseline AS (
                SELECT digest, AVG(avg_time_sec) as baseline_avg
                FROM query_digest_snapshots
                WHERE datetime(REPLACE(snapshot_time,'T',' ')) BETWEEN datetime('now', '-7 days') AND datetime('now', '-1 hour')
                  AND server_id = ?
                GROUP BY digest
            )
            SELECT r.digest, r.digest_text, r.recent_avg, b.baseline_avg,
                   r.recent_avg / NULLIF(b.baseline_avg, 0) as factor
            FROM recent r JOIN baseline b ON r.digest = b.digest
            WHERE b.baseline_avg > 0 AND r.recent_avg / b.baseline_avg >= ?
              AND r.recent_avg >= ?
            ORDER BY factor DESC LIMIT 5
        """, (server_id, server_id, threshold, min_recent_avg)).fetchall()

    if not rows:
        return None

    top = rows[0]
    return Alert(
        rule_name=_ns("query_regression", server_id),
        severity=Severity(rule_config.get("severity", "warning")),
        message=(
            f"[{server_id}] Query regression: `{(top['digest_text'] or '?')[:60]}` is "
            f"{top['factor']:.1f}x slower than 7d baseline"
        ),
        context={
            "server_id": server_id,
            "digest": top["digest"],
            "recent_avg": top["recent_avg"],
            "baseline_avg": top["baseline_avg"],
            "factor": top["factor"],
            "regressions_count": len(rows),
        },
    )


def evaluate_ddl_change(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire on any DDL change on this server."""
    with get_mon_reader() as conn:
        rows = conn.execute("""
            SELECT table_schema, table_name, change_type, detected_at
            FROM ddl_changes
            WHERE datetime(REPLACE(detected_at,'T',' ')) >= datetime('now', '-35 minutes')
              AND server_id = ?
        """, (server_id,)).fetchall()

    if not rows:
        return None

    changes = [f"{r['table_schema']}.{r['table_name']} ({r['change_type']})" for r in rows]
    return Alert(
        rule_name=_ns("ddl_change", server_id),
        severity=Severity(rule_config.get("severity", "info")),
        message=f"[{server_id}] DDL changes detected: {', '.join(changes)}",
        context={"server_id": server_id, "changes": changes, "count": len(rows)},
    )


def evaluate_high_cpu(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire if CPU utilization exceeds threshold."""
    threshold = rule_config.get("threshold", 0.85)

    with get_mon_reader() as conn:
        row = conn.execute("""
            SELECT value FROM gcp_metric_snapshots
            WHERE metric_name = 'cpu_utilization'
              AND server_id = ?
              AND datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now', '-15 minutes')
            ORDER BY snapshot_time DESC LIMIT 1
        """, (server_id,)).fetchone()

    if not row or row["value"] is None or row["value"] < threshold:
        return None

    return Alert(
        rule_name=_ns("high_cpu", server_id),
        severity=Severity(rule_config.get("severity", "warning")),
        message=(
            f"[{server_id}] CPU utilization at {row['value'] * 100:.1f}% "
            f"(threshold: {threshold * 100:.0f}%)"
        ),
        context={
            "server_id": server_id,
            "cpu_utilization": row["value"],
            "threshold": threshold,
        },
    )


def evaluate_high_memory(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire if memory utilization exceeds an absolute threshold."""
    threshold = rule_config.get("threshold", 0.85)

    with get_mon_reader() as conn:
        row = conn.execute("""
            SELECT value FROM gcp_metric_snapshots
            WHERE metric_name = 'memory_utilization'
              AND server_id = ?
              AND datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now', '-15 minutes')
            ORDER BY snapshot_time DESC LIMIT 1
        """, (server_id,)).fetchone()

    if not row or row["value"] is None or row["value"] < threshold:
        return None

    return Alert(
        rule_name=_ns("high_memory", server_id),
        severity=Severity(rule_config.get("severity", "warning")),
        message=(f"[{server_id}] Memory utilization at {row['value']*100:.1f}% "
                 f"(threshold: {threshold*100:.0f}%)"),
        context={"server_id": server_id, "memory_utilization": row["value"], "threshold": threshold},
    )


def evaluate_deadlock(rule_config: dict, server_id: str = "default") -> Alert | None:
    """Fire if a NEW deadlock was detected on this server.

    InnoDB reprints the same LATEST DETECTED DEADLOCK section verbatim on
    every SHOW ENGINE INNODB STATUS call until the server restarts, so a
    fresh snapshot_time does not mean a fresh deadlock (P1b-3: without this,
    a single old deadlock re-fires a critical alert every evaluation cycle
    forever). We key off the deadlock's own header timestamp (deadlock_at,
    parsed by parsers.innodb_status._parse_deadlock) and only fire when it
    is newer than the last deadlock_at we already alerted on. That "last
    alerted" value is read back from alert_history rather than kept in
    memory so a process restart doesn't re-alert an old deadlock.
    """
    with get_mon_reader() as conn:
        row = conn.execute("""
            SELECT snapshot_time, parsed_json
            FROM innodb_status_snapshots
            WHERE section_name = 'LATEST DETECTED DEADLOCK'
              AND datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now', '-10 minutes')
              AND server_id = ?
              AND parsed_json IS NOT NULL
            ORDER BY snapshot_time DESC LIMIT 1
        """, (server_id,)).fetchone()

    if not row:
        return None

    details = {}
    try:
        details = json.loads(row["parsed_json"])
    except (json.JSONDecodeError, TypeError):
        pass

    if not details.get("has_deadlock"):
        return None

    current_deadlock_at = details.get("deadlock_at")
    if not current_deadlock_at:
        return None

    rule_name = _ns("deadlock_detected", server_id)
    with get_mon_reader() as conn:
        last_row = conn.execute("""
            SELECT context_json
            FROM alert_history
            WHERE rule_name = ?
            ORDER BY fired_at DESC, id DESC LIMIT 1
        """, (rule_name,)).fetchone()

    last_deadlock_at = None
    if last_row and last_row["context_json"]:
        try:
            last_deadlock_at = json.loads(last_row["context_json"]).get("deadlock_at")
        except (json.JSONDecodeError, TypeError):
            last_deadlock_at = None

    if last_deadlock_at is not None and current_deadlock_at <= last_deadlock_at:
        return None

    tables = details.get("tables_involved", [])
    return Alert(
        rule_name=rule_name,
        severity=Severity(rule_config.get("severity", "critical")),
        message=(
            f"[{server_id}] Deadlock detected involving tables: "
            f"{', '.join(tables) if tables else 'unknown'}"
        ),
        context={"server_id": server_id, **details},
    )


# Registry of rule evaluators. Each function takes (rule_config, server_id).
RULE_EVALUATORS = {
    "lock_cascade": evaluate_lock_cascade,
    "threads_running_spike": evaluate_threads_running_spike,
    "query_regression": evaluate_query_regression,
    "ddl_change": evaluate_ddl_change,
    "high_cpu": evaluate_high_cpu,
    "high_memory": evaluate_high_memory,
    "deadlock_detected": evaluate_deadlock,
}

# Anomaly detection rule (imported separately to avoid circular imports)
try:
    from alerting.anomaly import evaluate_anomaly
    RULE_EVALUATORS["anomaly_detection"] = evaluate_anomaly
except ImportError:
    pass
