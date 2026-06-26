"""
Phase 3 — continuous, budgeted sampling of the production MySQL server.

APScheduler fires `phase3_sample(investigation_id)` on a DateTrigger every
`investigator.phase3_sampling_interval_seconds`. Each tick:

    1. Load the investigation row. If terminal → exit.
    2. Hard-timeout check against `phase3_max_duration_seconds`.
    3. Load-guard: Threads_running from SQLite snapshot (fast loop).
       Over threshold → status='load_guard_paused', retry in N seconds.
    4. Query budget (rolling 60s from investigation_samples.query_count).
       Over cap → skip sample, reschedule.
    5. Take a narrow sample (≤3 MySQL queries) + one SQLite-only index_delta
       for missing-index/query-regression alerts.
    6. Check clearance condition for the alert's alert_type.
    7. Reschedule next tick OR transition to completed / time_capped.

All MySQL queries are read-only with a SET SESSION MAX_EXECUTION_TIME
guard so a pathological server can't hang the sampler.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import mysql.connector

from storage import writer
from storage.connection import get_mon_reader, get_prod_connection
from alerting.budget import (
    queries_used_in_last_minute,
    threads_running_from_snapshot,
)

logger = logging.getLogger(__name__)


_LIVE_TIMEOUT_MS = 5000   # 5s per sample query
TERMINAL_STATUSES = {"completed", "aborted"}


# ---------------------------------------------------------------------------
# Config helper
# ---------------------------------------------------------------------------

def _cfg() -> dict:
    try:
        from config import get_config
        return dict(get_config().get("investigator") or {})
    except Exception:
        return {}


def _clearance_cfg() -> dict:
    c = _cfg().get("clearance") or {}
    return {
        "lock_waits": int(c.get("lock_waits", 1) or 1),
        "max_wait_seconds": int(c.get("max_wait_seconds", 5) or 5),
        "cpu_pct": float(c.get("cpu_pct", 0.75) or 0.75),
        "rows_examined_ratio": float(c.get("rows_examined_ratio", 100) or 100),
    }


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def phase3_sample(investigation_id: int) -> dict:
    inv = _load_inv(investigation_id)
    if inv is None:
        logger.info(f"phase3_sample({investigation_id}): not found")
        return {"status": "missing"}
    if inv["status"] in TERMINAL_STATUSES:
        logger.info(
            f"phase3_sample({investigation_id}): already {inv['status']}, no-op"
        )
        return {"status": inv["status"]}

    cfg = _cfg()
    max_duration = int(cfg.get("phase3_max_duration_seconds", 480) or 480)
    interval_s = int(cfg.get("phase3_sampling_interval_seconds", 20) or 20)
    load_guard_threshold = int(cfg.get("load_guard_threads_running_threshold", 40) or 40)
    load_guard_pause = int(cfg.get("load_guard_pause_seconds", 60) or 60)
    query_budget = int(cfg.get("query_budget_per_minute", 20) or 20)

    # Hard timeout
    started = _parse_dt(inv["started_at"])
    if started and (datetime.now(timezone.utc) - started).total_seconds() > max_duration:
        _terminate(investigation_id, "completed", abort_reason=None, note="max_duration_reached")
        return {"status": "completed", "reason": "max_duration"}

    # Load-guard
    current_threads = threads_running_from_snapshot(inv["server_id"])
    if current_threads is not None and current_threads > load_guard_threshold:
        _transition(
            investigation_id,
            status="load_guard_paused",
            phase3_next_run_at=_iso(_now() + timedelta(seconds=load_guard_pause)),
        )
        _reschedule(investigation_id, load_guard_pause)
        return {
            "status": "load_guard_paused",
            "threads_running": current_threads,
            "threshold": load_guard_threshold,
        }
    # If we were paused and the load cleared, resume as phase3.
    if inv["status"] == "load_guard_paused":
        _transition(investigation_id, status="phase3")

    # Budget check
    used = queries_used_in_last_minute(investigation_id)
    if used >= query_budget:
        # Skip this tick, try again after the interval.
        _transition(
            investigation_id,
            phase3_next_run_at=_iso(_now() + timedelta(seconds=interval_s)),
        )
        _reschedule(investigation_id, interval_s)
        return {"status": "budget_skipped", "used": used, "cap": query_budget}

    # Take samples
    suspect_digests = _suspect_digests(investigation_id)
    sampled = _take_samples(
        investigation_id=investigation_id,
        server_id=inv["server_id"],
        suspect_digests=suspect_digests,
    )

    # Clearance
    alert_type = _alert_type_for(inv["inbound_alert_id"])
    if _cleared(
        alert_type=alert_type,
        server_id=inv["server_id"],
        investigation_id=investigation_id,
        suspect_digests=suspect_digests,
        current_threads=current_threads,
    ):
        _terminate(
            investigation_id, "completed", abort_reason=None,
            note=f"cleared:{alert_type}",
        )
        return {"status": "completed", "reason": "cleared", "alert_type": alert_type}

    # Continue — reschedule
    _transition(
        investigation_id,
        status="phase3",
        phase3_next_run_at=_iso(_now() + timedelta(seconds=interval_s)),
        query_count_total=(inv["query_count_total"] or 0) + sampled.get("query_count", 0),
    )
    _reschedule(investigation_id, interval_s)
    return {"status": "sampled", "queries": sampled.get("query_count", 0)}


# ---------------------------------------------------------------------------
# Sampling — narrow queries against production MySQL
# ---------------------------------------------------------------------------

@dataclass
class _SampleBatch:
    rows: list[dict]
    query_count: int = 0


_PROCESSLIST_SQL = """
SELECT pid, user, db, command, state, time_sec, LEFT(query, 500) AS query
FROM (
    SELECT
        processlist_id AS pid,
        processlist_user AS user,
        processlist_db AS db,
        processlist_command AS command,
        processlist_state AS state,
        COALESCE(processlist_time, 0) AS time_sec,
        processlist_info AS query
    FROM performance_schema.threads
    WHERE type = 'FOREGROUND'
      AND processlist_command IS NOT NULL
      AND processlist_command <> 'Sleep'
    ORDER BY time_sec DESC
    LIMIT 20
) t
"""


_LOCK_WAITS_SQL = """
SELECT
    COUNT(*) AS lock_count,
    COALESCE(MAX(TIMESTAMPDIFF(SECOND,
            (SELECT trx_started FROM information_schema.innodb_trx
             WHERE trx_id = REQUESTING_ENGINE_TRANSACTION_ID),
            NOW())), 0) AS max_wait_seconds
FROM performance_schema.data_lock_waits
"""


def _take_samples(
    investigation_id: int,
    server_id: str,
    suspect_digests: list[str],
) -> dict:
    """
    Run up to 3 narrow MySQL queries + one SQLite-only index_delta and
    persist as investigation_samples rows. Returns {query_count, samples}.
    """
    query_count = 0
    now = _iso(_now())

    # 1) Processlist
    pl_rows = _safe_live_query(server_id, _PROCESSLIST_SQL)
    if pl_rows is not None:
        query_count += 1
        _persist_sample(investigation_id, now, "processlist", 1, pl_rows)

    # 2) Lock waits
    lw_rows = _safe_live_query(server_id, _LOCK_WAITS_SQL)
    if lw_rows is not None:
        query_count += 1
        _persist_sample(investigation_id, now, "locks", 1, lw_rows)

    # 3) Digest delta (only when we have suspect digests)
    if suspect_digests:
        placeholders = ", ".join(["%s"] * len(suspect_digests))
        sql = (
            "SELECT DIGEST AS digest, "
            "       SUM_ROWS_EXAMINED AS rows_examined, "
            "       SUM_ROWS_SENT AS rows_sent, "
            "       COUNT_STAR AS exec_count, "
            "       SUM_TIMER_WAIT/1e12 AS total_time_sec "
            "FROM performance_schema.events_statements_summary_by_digest "
            f"WHERE DIGEST IN ({placeholders}) "
            "LIMIT 50"
        )
        dig_rows = _safe_live_query(server_id, sql, tuple(suspect_digests))
        if dig_rows is not None:
            query_count += 1
            _persist_sample(investigation_id, now, "digest_delta", 1, dig_rows)

    # 4) SQLite-only index_delta for completeness — tracks suspect digests'
    #    rows_examined/rows_sent ratio using the most recent snapshot the
    #    medium loop already captured. Zero MySQL cost.
    if suspect_digests:
        index_delta = _sqlite_index_delta(server_id, suspect_digests)
        _persist_sample(investigation_id, now, "index_delta", 0, index_delta)

    # 5) Threads_running snapshot — also SQLite-only; Phase 3 already read it.
    tr = threads_running_from_snapshot(server_id)
    if tr is not None:
        _persist_sample(
            investigation_id, now, "threads_running", 0, {"threads_running": tr}
        )

    return {"query_count": query_count}


def _persist_sample(
    investigation_id: int, sampled_at: str, sample_type: str,
    query_count: int, data: Any,
) -> None:
    try:
        writer.write_investigation_samples([{
            "investigation_id": investigation_id,
            "sampled_at": sampled_at,
            "sample_type": sample_type,
            "query_count": query_count,
            "data": json.dumps(data, default=str),
        }])
    except Exception as e:
        logger.warning(f"persist sample {sample_type} failed: {e}")


def _safe_live_query(
    server_id: str, sql: str, params: tuple = ()
) -> list[dict] | None:
    try:
        with get_prod_connection(server_id) as conn:
            cursor = conn.cursor(dictionary=True)
            try:
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {_LIVE_TIMEOUT_MS}")
            except Exception:
                pass
            cursor.execute(sql, params)
            rows = cursor.fetchall() or []
            return [dict(r) for r in rows]
    except mysql.connector.Error as e:
        logger.warning(f"Phase 3 live query failed on {server_id}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Phase 3 live query errored on {server_id}: {e}")
        return None


def _sqlite_index_delta(server_id: str, suspect_digests: list[str]) -> list[dict]:
    try:
        placeholders = ", ".join(["?"] * len(suspect_digests))
        sql = f"""
            SELECT digest, schema_name, exec_count,
                   rows_examined, rows_sent,
                   CASE WHEN rows_sent > 0
                        THEN CAST(rows_examined AS REAL) / rows_sent
                        ELSE rows_examined
                   END AS ratio,
                   snapshot_time
            FROM query_digest_snapshots
            WHERE server_id = ?
              AND digest IN ({placeholders})
              AND snapshot_time = (
                  SELECT MAX(snapshot_time) FROM query_digest_snapshots
                  WHERE server_id = ? AND digest = query_digest_snapshots.digest
              )
        """
        with get_mon_reader() as conn:
            rows = conn.execute(
                sql, (server_id, *suspect_digests, server_id)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.debug(f"_sqlite_index_delta: {e}")
        return []


# ---------------------------------------------------------------------------
# Clearance evaluation
# ---------------------------------------------------------------------------

def _cleared(
    alert_type: str,
    server_id: str,
    investigation_id: int,
    suspect_digests: list[str],
    current_threads: int | None,
) -> bool:
    clearance = _clearance_cfg()
    if alert_type == "lock_cascade":
        return _cleared_lock_cascade(investigation_id, clearance)
    if alert_type == "high_cpu":
        return _cleared_high_cpu(server_id, clearance)
    if alert_type == "threads_running_spike":
        return _cleared_threads(current_threads, server_id)
    if alert_type == "deadlock_detected":
        return _cleared_deadlock(server_id, investigation_id)
    if alert_type in ("missing_index", "query_regression"):
        return _cleared_missing_index(server_id, suspect_digests, clearance)
    return _cleared_default(investigation_id)


def _cleared_lock_cascade(investigation_id: int, clearance: dict) -> bool:
    """Latest locks sample: count below threshold AND wait below threshold."""
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT data FROM investigation_samples
                WHERE investigation_id = ? AND sample_type = 'locks'
                ORDER BY id DESC LIMIT 1
                """,
                (investigation_id,),
            ).fetchone()
            if row is None:
                return False
            data = json.loads(row["data"])
            if isinstance(data, list) and data:
                data = data[0]
            lock_count = int(data.get("lock_count", 0) or 0)
            max_wait = int(data.get("max_wait_seconds", 0) or 0)
            return (
                lock_count < clearance["lock_waits"]
                and max_wait < clearance["max_wait_seconds"]
            )
    except Exception as e:
        logger.debug(f"_cleared_lock_cascade: {e}")
        return False


def _cleared_high_cpu(server_id: str, clearance: dict) -> bool:
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT value FROM gcp_metric_snapshots
                WHERE server_id = ? AND metric_name LIKE '%cpu%'
                ORDER BY snapshot_time DESC LIMIT 1
                """,
                (server_id,),
            ).fetchone()
            if row is None:
                return False
            return float(row["value"] or 0) < clearance["cpu_pct"]
    except Exception as e:
        logger.debug(f"_cleared_high_cpu: {e}")
        return False


def _cleared_threads(current_threads: int | None, server_id: str) -> bool:
    # Compare against a stable floor — if the fast-loop baseline is
    # unavailable, use 15 as a reasonable "normal" ceiling.
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT AVG(raw_value) AS baseline
                FROM global_status_snapshots
                WHERE server_id = ?
                  AND variable_name = 'Threads_running'
                  AND snapshot_time >= datetime('now', '-7 days')
                """,
                (server_id,),
            ).fetchone()
            baseline = float(row["baseline"] or 15.0) if row else 15.0
    except Exception:
        baseline = 15.0
    if current_threads is None:
        return False
    return current_threads <= baseline * 1.5


def _cleared_deadlock(server_id: str, investigation_id: int) -> bool:
    """Two consecutive samples since phase3 start with no new deadlocks."""
    try:
        with get_mon_reader() as conn:
            start = conn.execute(
                "SELECT started_at FROM investigations WHERE id = ?",
                (investigation_id,),
            ).fetchone()
            if start is None:
                return False
            row = conn.execute(
                """
                SELECT COUNT(*) AS c FROM innodb_status_snapshots
                WHERE server_id = ?
                  AND section_name = 'LATEST DETECTED DEADLOCK'
                  AND snapshot_time >= ?
                """,
                (server_id, start["started_at"]),
            ).fetchone()
            return int(row["c"] or 0) == 0
    except Exception as e:
        logger.debug(f"_cleared_deadlock: {e}")
        return False


def _cleared_missing_index(
    server_id: str, suspect_digests: list[str], clearance: dict
) -> bool:
    if not suspect_digests:
        return False
    try:
        placeholders = ", ".join(["?"] * len(suspect_digests))
        sql = f"""
            SELECT digest,
                   CASE WHEN rows_sent > 0
                        THEN CAST(rows_examined AS REAL) / rows_sent
                        ELSE rows_examined
                   END AS ratio
            FROM query_digest_snapshots
            WHERE server_id = ?
              AND digest IN ({placeholders})
              AND snapshot_time = (
                  SELECT MAX(snapshot_time) FROM query_digest_snapshots
                  WHERE server_id = ? AND digest = query_digest_snapshots.digest
              )
        """
        with get_mon_reader() as conn:
            rows = conn.execute(sql, (server_id, *suspect_digests, server_id)).fetchall()
            if not rows:
                return False
            ratios = [float(r["ratio"] or 0) for r in rows]
            return all(r < clearance["rows_examined_ratio"] for r in ratios)
    except Exception as e:
        logger.debug(f"_cleared_missing_index: {e}")
        return False


def _cleared_default(investigation_id: int) -> bool:
    """3 consecutive samples with no evidence of anomalies in investigation_samples."""
    try:
        with get_mon_reader() as conn:
            rows = conn.execute(
                """
                SELECT sample_type, data FROM investigation_samples
                WHERE investigation_id = ?
                ORDER BY id DESC LIMIT 3
                """,
                (investigation_id,),
            ).fetchall()
            if len(rows) < 3:
                return False
            for r in rows:
                if r["sample_type"] == "locks":
                    try:
                        d = json.loads(r["data"])
                        if isinstance(d, list) and d:
                            d = d[0]
                        if int(d.get("lock_count", 0) or 0) > 0:
                            return False
                    except Exception:
                        continue
            return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _suspect_digests(investigation_id: int) -> list[str]:
    """Read suspect digests from the most recent phase=1 correlation finding."""
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT content FROM investigation_findings
                WHERE investigation_id = ?
                  AND phase = 1
                  AND kind = 'correlation'
                ORDER BY id DESC LIMIT 1
                """,
                (investigation_id,),
            ).fetchone()
            if row is None:
                return []
            content = json.loads(row["content"])
            digests = content.get("suspect_digests") or []
            return [str(d) for d in digests if isinstance(d, (str, int))][:5]
    except Exception:
        return []


def _alert_type_for(inbound_alert_id: int) -> str:
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                "SELECT alert_type FROM inbound_alerts WHERE id = ?",
                (inbound_alert_id,),
            ).fetchone()
            return row["alert_type"] if row else "default"
    except Exception:
        return "default"


def _load_inv(investigation_id: int) -> dict | None:
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                "SELECT * FROM investigations WHERE id = ?", (investigation_id,),
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.warning(f"_load_inv({investigation_id}): {e}")
        return None


def _transition(investigation_id: int, **fields) -> None:
    try:
        writer.update_investigation(investigation_id, **fields)
    except Exception as e:
        logger.warning(f"_transition({investigation_id}, {fields}): {e}")


def _terminate(
    investigation_id: int, status: str, abort_reason: str | None, note: str | None = None
) -> None:
    fields: dict = {"status": status, "ended_at": _iso(_now())}
    if abort_reason:
        fields["abort_reason"] = abort_reason
    _transition(investigation_id, **fields)

    # Final finding row so operators can see why we stopped.
    try:
        writer.write_investigation_findings([{
            "investigation_id": investigation_id,
            "created_at": _iso(_now()),
            "phase": 3,
            "kind": "root_cause" if status == "completed" else "evidence",
            "severity": "info",
            "content": json.dumps({"terminal_status": status, "note": note}),
        }])
    except Exception as e:
        logger.debug(f"_terminate finding failed: {e}")


def _reschedule(investigation_id: int, delay_seconds: int) -> None:
    """Schedule the next phase3_sample on the existing APScheduler."""
    try:
        from scheduler.runner import _scheduler_instance
    except Exception:
        return
    if _scheduler_instance is None:
        return
    try:
        from apscheduler.triggers.date import DateTrigger
        run_at = _now() + timedelta(seconds=delay_seconds)
        _scheduler_instance.add_job(
            phase3_sample,
            trigger=DateTrigger(run_date=run_at),
            args=[investigation_id],
            id=f"investigation:{investigation_id}:phase3",
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(f"reschedule failed ({investigation_id}): {e}")


# ---------------------------------------------------------------------------
# Startup sweep — abort stuck investigations after a restart
# ---------------------------------------------------------------------------

def sweep_stale_investigations(max_age_minutes: int = 10) -> int:
    """
    On scheduler startup, mark any non-terminal investigation that started
    more than `max_age_minutes` ago as aborted. Without this, a process
    restart would leave them forever in phase1/phase2/phase3.
    """
    cutoff = _iso(_now() - timedelta(minutes=max_age_minutes))
    aborted = 0
    try:
        with get_mon_reader() as conn:
            rows = conn.execute(
                """
                SELECT id FROM investigations
                WHERE status NOT IN ('completed', 'aborted')
                  AND started_at < ?
                """,
                (cutoff,),
            ).fetchall()
            ids = [r["id"] for r in rows]
    except Exception as e:
        logger.warning(f"sweep_stale_investigations scan failed: {e}")
        return 0

    for inv_id in ids:
        try:
            writer.update_investigation(
                inv_id,
                status="aborted",
                abort_reason="scheduler_restart",
                ended_at=_iso(_now()),
            )
            aborted += 1
        except Exception as e:
            logger.warning(f"sweep: abort of {inv_id} failed: {e}")

    if aborted:
        logger.info(f"sweep_stale_investigations: aborted {aborted} stuck row(s)")
    return aborted


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
