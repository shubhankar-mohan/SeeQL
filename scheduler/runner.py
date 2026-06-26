"""
Scheduler for MySQL DBA Agent.

Uses APScheduler to run collection loops at configured intervals:
    - Fast loop (30s): processlist, locks, transactions, metadata locks
    - Medium loop (5m): query digests, wait events, table IO, InnoDB, global status
    - Slow loop (30m): schema snapshots, DDL detection, indexes, variables
    - Agent analysis (15m): LLM-powered analysis of collected data
    - Retention (24h): delete old data

Multi-server: each loop iterates over all active servers.
One server failing does not stop collection for others.

Shutdown: SIGTERM (Docker) and SIGINT (Ctrl+C) trigger a graceful shutdown
via a threading.Event. In-flight jobs finish, then the SQLite WAL is
checkpointed (TRUNCATE) to guarantee no pending writes are lost on restart.
"""

import logging
import signal
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from collectors.fast_loop import run_fast_loop
from collectors.medium_loop import run_medium_loop
from collectors.slow_loop import run_slow_loop
from config import get_config, get_intervals
from storage.retention import run_retention_cleanup

logger = logging.getLogger(__name__)

_scheduler_instance: BackgroundScheduler | None = None
_shutdown_event = threading.Event()


def _install_signal_handlers():
    """Register SIGTERM + SIGINT handlers that set the shutdown event."""
    def _handle(signum, _frame):
        name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        logger.info(f"Received {name}, initiating graceful shutdown...")
        _shutdown_event.set()

    try:
        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
    except ValueError:
        # signal.signal only works in the main thread. If we're not in the
        # main thread (e.g. pytest, certain uvicorn workers), silently skip —
        # the caller will handle shutdown some other way.
        logger.debug("Not in main thread; skipping signal handler install")


def _flush_sqlite():
    """Checkpoint the SQLite WAL so pending writes are durable before exit."""
    try:
        from storage.connection import get_mon_connection
        with get_mon_connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        logger.info("SQLite WAL checkpointed (TRUNCATE)")
    except Exception as e:
        logger.warning(f"WAL checkpoint failed: {e}")


def request_shutdown():
    """Public entry point for other code paths (e.g. uvicorn lifespan) to
    trigger the same graceful shutdown."""
    _shutdown_event.set()


def _get_server_contexts():
    """Get ServerContext for all active servers."""
    from config.server_registry import get_server_registry
    registry = get_server_registry()
    return [s.to_context() for s in registry.get_active_servers()]


def _run_alerts(loop_name: str = "fast"):
    """Evaluate alert rules after a collection loop."""
    try:
        from alerting.engine import evaluate
        evaluate(loop_name)
    except Exception as e:
        logger.warning(f"Alert evaluation failed: {e}")


def _update_prom_metrics():
    """Update Prometheus metric gauges from latest data."""
    try:
        from api.prometheus import update_metrics
        update_metrics()
    except Exception:
        pass  # prometheus_client may not be installed


def _run_fast():
    for ctx in _get_server_contexts():
        try:
            results = run_fast_loop(ctx)
            failures = [name for name, ok in results.items() if not ok]
            if failures:
                logger.warning(f"Fast loop failures [{ctx.server_id}]: {failures}")
        except Exception as e:
            logger.error(f"Fast loop failed for {ctx.server_id}: {e}")
    _run_alerts("fast")
    _update_prom_metrics()


def _run_medium():
    for ctx in _get_server_contexts():
        try:
            results = run_medium_loop(ctx)
            failures = [name for name, ok in results.items() if not ok]
            if failures:
                logger.warning(f"Medium loop failures [{ctx.server_id}]: {failures}")
            # Anomaly pipeline: detect → persist → group into incidents.
            # Runs per-server so one slow detector doesn't block another.
            _run_anomaly_pipeline(ctx.server_id)
        except Exception as e:
            logger.error(f"Medium loop failed for {ctx.server_id}: {e}")
    _run_alerts("medium")
    _update_prom_metrics()


def _run_anomaly_pipeline(server_id: str):
    """Detect anomalies, persist them, and group them into incident windows.

    The detection call is cached (see alerting.anomaly._detect_cache) so the
    subsequent evaluate_anomaly() call in _run_alerts() is free.
    """
    try:
        from alerting.anomaly import detect_anomalies
        from alerting.anomaly_store import persist
        from alerting.incidents import update_windows
    except ImportError as e:
        logger.debug(f"Anomaly pipeline modules not available: {e}")
        return

    try:
        results = detect_anomalies(server_id=server_id)
    except Exception as e:
        logger.warning(f"Anomaly detection failed for {server_id}: {e}")
        return

    try:
        persist(results)
    except Exception as e:
        logger.warning(f"Anomaly persistence failed for {server_id}: {e}")
        # Even if persistence fails, still try to group any previously-persisted
        # ungrouped events. Windowing is idempotent.

    try:
        new_incident_ids = update_windows(server_id)
        if new_incident_ids:
            logger.info(
                f"Detected {len(new_incident_ids)} new incident(s) on {server_id}: "
                f"{new_incident_ids}"
            )
    except Exception as e:
        logger.warning(f"Incident windowing failed for {server_id}: {e}")


def _run_slow():
    for ctx in _get_server_contexts():
        try:
            results = run_slow_loop(ctx)
            failures = [name for name, ok in results.items() if not ok]
            if failures:
                logger.warning(f"Slow loop failures [{ctx.server_id}]: {failures}")
        except Exception as e:
            logger.error(f"Slow loop failed for {ctx.server_id}: {e}")


def _run_retention():
    try:
        run_retention_cleanup()
    except Exception as e:
        logger.error(f"Retention cleanup failed: {e}")


def _run_agent():
    """Run LLM agent analysis for each active server."""
    from config.server_registry import get_server_registry
    registry = get_server_registry()

    for server in registry.get_active_servers():
        try:
            from agent.llm_agent import run_analysis
            run_analysis("routine", server_id=server.server_id)
        except Exception as e:
            logger.error(f"Agent analysis failed for {server.server_id}: {e}")


def create_scheduler() -> BackgroundScheduler:
    """Create and configure the APScheduler instance (not yet started)."""
    intervals = get_intervals()
    config = get_config()

    scheduler = BackgroundScheduler(
        job_defaults={
            "max_instances": 1,
            "misfire_grace_time": 60,
            "coalesce": True,
        },
        executors={
            "default": {"type": "threadpool", "max_workers": 7},
        },
    )

    scheduler.add_job(
        _run_fast,
        trigger=IntervalTrigger(seconds=intervals.get("fast_loop", 30)),
        id="fast_loop",
        name="Fast Loop (processlist, locks, transactions)",
    )

    scheduler.add_job(
        _run_medium,
        trigger=IntervalTrigger(seconds=intervals.get("medium_loop", 300)),
        id="medium_loop",
        name="Medium Loop (digests, waits, IO, InnoDB, status, EXPLAIN)",
    )

    scheduler.add_job(
        _run_slow,
        trigger=IntervalTrigger(seconds=intervals.get("slow_loop", 1800)),
        id="slow_loop",
        name="Slow Loop (schema snapshots, DDL detection, indexes, variables)",
    )

    # Daily retention cleanup
    scheduler.add_job(
        _run_retention,
        trigger=IntervalTrigger(seconds=intervals.get("retention_loop", 86400)),
        id="retention_loop",
        name="Retention Cleanup (delete old data)",
    )

    # LLM Agent analysis (if enabled)
    agent_config = config.get("agent", {})
    if agent_config.get("enabled", False):
        agent_interval = agent_config.get("schedule_seconds", 900)
        scheduler.add_job(
            _run_agent,
            trigger=IntervalTrigger(seconds=agent_interval),
            id="agent_analysis",
            name="LLM Agent Analysis",
        )

    return scheduler


def run_scheduler():
    """Start the scheduler and block until interrupted (SIGTERM/SIGINT)."""
    global _scheduler_instance
    scheduler = create_scheduler()
    _scheduler_instance = scheduler

    logger.info("=" * 60)
    logger.info("MySQL DBA Agent — Starting collectors")
    logger.info("=" * 60)

    intervals = get_intervals()
    from config.server_registry import get_server_registry
    servers = get_server_registry().get_active_servers()
    logger.info(f"  Monitoring {len(servers)} server(s): {[s.server_id for s in servers]}")
    logger.info(f"  Fast loop:   every {intervals.get('fast_loop', 30)}s")
    logger.info(f"  Medium loop: every {intervals.get('medium_loop', 300)}s")
    logger.info(f"  Slow loop:   every {intervals.get('slow_loop', 1800)}s")

    # Initial run on startup
    logger.info("Running initial collection...")
    _run_fast()
    _run_medium()
    _run_slow()
    logger.info("Initial collection complete.")

    # Abort any investigations that were mid-flight when the process died —
    # otherwise they sit in phase1/phase2/phase3 forever with no scheduler
    # job backing them.
    try:
        from alerting.phase3 import sweep_stale_investigations
        sweep_stale_investigations(max_age_minutes=10)
    except Exception as e:
        logger.debug(f"Skipping investigation sweep: {e}")

    _install_signal_handlers()
    scheduler.start()
    logger.info("Scheduler started. Press Ctrl+C (or send SIGTERM) to stop.")

    try:
        # Wait on the shutdown event. This blocks until either SIGTERM/SIGINT
        # arrives and sets the event, or some other caller invokes
        # request_shutdown(). timeout= avoids dead-locking the main thread
        # in case a signal is missed by the interpreter.
        while not _shutdown_event.wait(timeout=60):
            pass
    finally:
        logger.info("Shutting down scheduler (waiting for in-flight jobs)...")
        try:
            scheduler.shutdown(wait=True)
        except Exception as e:
            logger.warning(f"Scheduler shutdown error: {e}")
        _flush_sqlite()
        logger.info("Scheduler stopped cleanly.")
