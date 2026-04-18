"""API routes for SeeQL."""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from storage.connection import check_prod_connection, check_mon_connection, get_mon_reader

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
def health():
    """Check production DB and SQLite connectivity."""
    prod_ok = check_prod_connection()
    mon_ok = check_mon_connection()
    status = "healthy" if (prod_ok and mon_ok) else "degraded"
    return {
        "status": status,
        "production_db": "ok" if prod_ok else "error",
        "monitoring_db": "ok" if mon_ok else "error",
    }


@router.post("/collect/fast")
def collect_fast():
    """Trigger fast loop collection."""
    from collectors.fast_loop import run_fast_loop
    results = run_fast_loop()
    return {"loop": "fast", "results": results}


@router.post("/collect/medium")
def collect_medium():
    """Trigger medium loop collection."""
    from collectors.medium_loop import run_medium_loop
    results = run_medium_loop()
    return {"loop": "medium", "results": results}


@router.post("/collect/slow")
def collect_slow():
    """Trigger slow loop collection."""
    from collectors.slow_loop import run_slow_loop
    results = run_slow_loop()
    return {"loop": "slow", "results": results}


@router.post("/collect/all")
def collect_all():
    """Trigger all collection loops."""
    from collectors.fast_loop import run_fast_loop
    from collectors.medium_loop import run_medium_loop
    from collectors.slow_loop import run_slow_loop

    return {
        "fast": run_fast_loop(),
        "medium": run_medium_loop(),
        "slow": run_slow_loop(),
    }


@router.get("/status")
def status():
    """Scheduler job status and next run times."""
    try:
        from scheduler.runner import _scheduler_instance
        if _scheduler_instance is None:
            return {"scheduler": "not running"}
        jobs = []
        for job in _scheduler_instance.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
            })
        return {"scheduler": "running", "jobs": jobs}
    except Exception as e:
        return {"scheduler": "unknown", "error": str(e)}


def _query_table(table: str, limit: int, order_col: str = "snapshot_time") -> list[dict]:
    """Read rows from a monitoring table."""
    with get_mon_reader() as conn:
        cursor = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order_col} DESC LIMIT ?", (limit,)
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


@router.get("/data/queries")
def data_queries(limit: int = Query(default=50, le=500)):
    """Latest query digest snapshots."""
    return _query_table("query_digest_snapshots", limit)


@router.get("/data/locks")
def data_locks(limit: int = Query(default=50, le=500)):
    """Latest lock wait snapshots."""
    return _query_table("lock_wait_snapshots", limit)


@router.get("/data/schema-changes")
def data_schema_changes(limit: int = Query(default=50, le=500)):
    """DDL change history."""
    return _query_table("ddl_changes", limit, order_col="detected_at")


@router.get("/data/global-status")
def data_global_status(limit: int = Query(default=100, le=1000)):
    """Latest global status deltas."""
    return _query_table("global_status_snapshots", limit)
