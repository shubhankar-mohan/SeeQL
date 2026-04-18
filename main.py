"""
MySQL DBA Agent — Main Entry Point

Usage:
    python main.py                  # Start the collection agent
    python main.py --check          # Run health checks only
    python main.py --once           # Run one collection cycle and exit
    python main.py --init-db        # Initialize the monitoring database schema
    python main.py --api            # Start scheduler + API server
    python main.py --api-only       # Start API server only (no scheduled collection)

Environment variables:
    PROD_DB_PASSWORD    Password for the production MySQL monitoring user
    SEEQL_ENV           Environment name (production, dev, test)
    SEEQL_API_PORT      API server port (default: 8080)
"""

import os
import sys
import argparse
import logging
import logging.handlers
from pathlib import Path

from config import get_config


def setup_logging():
    """Configure logging with console + rotating file output."""
    config = get_config()
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO").upper(), logging.INFO)

    log_file = log_config.get("file", "logs/dba_agent.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    backup_count = log_config.get("backup_count", 5)

    # Env var wins over config for max total log size
    max_total_mb = int(os.environ.get(
        "SEEQL_LOG_MAX_SIZE_MB",
        log_config.get("max_total_mb", 500),
    ))
    # Distribute total budget across main file + backups
    max_bytes = (max_total_mb * 1024 * 1024) // (backup_count + 1)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(console)
    root.addHandler(file_handler)

    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("mysql.connector").setLevel(logging.WARNING)


def cmd_check():
    """Run health checks against both databases — surfaces E001–E008 on failure."""
    from storage.connection import check_prod_connection, check_mon_connection
    from seeql import errors

    print("Checking production database connection...", end=" ")
    try:
        ok = check_prod_connection()
    except Exception as e:
        print("✗ FAILED")
        # Map common MySQL connection error messages to the right catalog entry.
        msg = str(e).lower()
        if "access denied" in msg or "authentication" in msg or "1045" in msg:
            raise errors.get("E001", details=str(e))
        if "timed out" in msg or "timeout" in msg or "can't connect" in msg or "2003" in msg:
            raise errors.get("E006", details=str(e))
        raise errors.get("E006", details=str(e))
    if not ok:
        print("✗ FAILED")
        raise errors.get("E006", details="check_prod_connection returned False")
    print("✓ OK")

    print("Checking monitoring database (SQLite)...", end=" ")
    try:
        ok = check_mon_connection()
    except Exception as e:
        print("✗ FAILED")
        raise errors.get("E008", details=str(e))
    if not ok:
        print("✗ FAILED")
        raise errors.get("E008", details="check_mon_connection returned False")
    print("✓ OK")

    print("\nAll checks passed.")


def cmd_init_db():
    """Initialize the monitoring SQLite database schema."""
    from storage.connection import get_mon_connection
    from storage.migrations import run_all_migrations

    schema_path = Path(__file__).parent / "storage" / "schema.sql"
    if not schema_path.exists():
        print(f"Schema file not found: {schema_path}")
        sys.exit(1)

    sql = schema_path.read_text()

    print("Initializing monitoring database schema (SQLite)...")
    with get_mon_connection() as conn:
        conn.executescript(sql)

    # Run migrations (adds server_id columns, creates servers table)
    run_all_migrations()

    # Sync server registry to DB
    from config.server_registry import get_server_registry
    get_server_registry().sync_to_db()

    print("✓ Schema initialized successfully.")


def cmd_once():
    """Run one collection cycle (all loops) and exit."""
    _run_startup_migrations()

    from collectors.fast_loop import run_fast_loop
    from collectors.medium_loop import run_medium_loop
    from collectors.slow_loop import run_slow_loop

    logger = logging.getLogger(__name__)
    logger.info("Running single collection cycle...")

    print("\n--- Fast Loop ---")
    results = run_fast_loop()
    for name, ok in results.items():
        print(f"  {name}: {'✓' if ok else '✗'}")

    print("\n--- Medium Loop ---")
    results = run_medium_loop()
    for name, ok in results.items():
        print(f"  {name}: {'✓' if ok else '✗'}")

    print("\n--- Slow Loop ---")
    results = run_slow_loop()
    for name, ok in results.items():
        print(f"  {name}: {'✓' if ok else '✗'}")

    print("\nDone.")


def cmd_run():
    """Start the continuous collection agent."""
    _run_startup_migrations()
    from scheduler.runner import run_scheduler
    run_scheduler()


def _run_startup_migrations():
    """Run schema migrations and sync server registry on startup."""
    from storage.migrations import run_all_migrations
    from config.server_registry import get_server_registry

    run_all_migrations()
    get_server_registry().sync_to_db()


def cmd_doctor():
    """Run the seeql diagnostic — 7 checks against the local env."""
    from seeql import doctor
    failures = doctor.run()
    sys.exit(failures)


def cmd_replay(args):
    """Replay a past incident window — print timeline + LLM root cause to stdout."""
    import json
    from datetime import datetime
    from storage.connection import get_mon_reader
    from seeql import errors

    def _validate_ts(name: str, value: str) -> str:
        """Parse an ISO8601 timestamp or raise E010."""
        try:
            # datetime.fromisoformat accepts '2026-04-10T03:00:00', with or without tz
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        except (ValueError, TypeError) as e:
            raise errors.get(
                "E010",
                details=f"--{name} value {value!r} is not valid ISO 8601: {e}",
            )

    # Resolve from_ts, to_ts, incident_id from the args
    incident_id = None
    if args.latest:
        with get_mon_reader() as conn:
            row = conn.execute(
                """SELECT id, start_time, end_time FROM incident_windows
                   ORDER BY start_time DESC LIMIT 1"""
            ).fetchone()
        if not row:
            print("No incidents detected yet. Nothing to replay.")
            return
        incident_id = row["id"]
        from_ts, to_ts = row["start_time"], row["end_time"]
    elif args.incident is not None:
        with get_mon_reader() as conn:
            row = conn.execute(
                """SELECT id, start_time, end_time FROM incident_windows
                   WHERE id = ?""",
                (args.incident,),
            ).fetchone()
        if not row:
            print(f"ERROR: incident {args.incident} not found", file=sys.stderr)
            sys.exit(1)
        incident_id = row["id"]
        from_ts, to_ts = row["start_time"], row["end_time"]
    elif args.from_ts and args.to_ts:
        from_ts = _validate_ts("from", args.from_ts)
        to_ts = _validate_ts("to", args.to_ts)
        # Also validate that the window is not inverted or zero-width
        if from_ts >= to_ts:
            raise errors.get(
                "E010",
                details=f"--from ({from_ts}) must be strictly before --to ({to_ts})",
            )
    else:
        raise errors.get(
            "E010",
            details="must provide one of --from/--to, --incident <id>, or --latest",
        )

    from agent.replay import run_replay
    result = run_replay(
        from_ts=from_ts,
        to_ts=to_ts,
        server_id=args.server,
        incident_id=incident_id,
    )
    print(result.to_markdown())


def cmd_incidents(args):
    """Browse detected incident windows."""
    import json
    from storage.connection import get_mon_reader

    if args.inc_cmd != "list":
        print(
            "Usage: seeql incidents list [--status STATUS] [--limit N] [--server SID]",
            file=sys.stderr,
        )
        sys.exit(2)

    where = []
    params: list = []
    if getattr(args, "server", None):
        where.append("server_id = ?")
        params.append(args.server)
    if getattr(args, "status", None):
        where.append("status = ?")
        params.append(args.status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(args.limit)

    sql = f"""
        SELECT id, server_id, start_time, end_time, severity,
               involved_metrics, event_count, status
        FROM incident_windows
        {where_sql}
        ORDER BY start_time DESC
        LIMIT ?
    """
    with get_mon_reader() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    if not rows:
        print("No incidents.")
        return

    print(f"{'ID':<5} {'SERVER':<15} {'SEVERITY':<10} {'STATUS':<10} "
          f"{'START':<20} {'EVENTS':<7} {'METRICS'}")
    print("-" * 110)
    for row in rows:
        metrics = ", ".join(json.loads(row["involved_metrics"]))
        start = (row["start_time"] or "")[:19]
        print(f"{row['id']:<5} {row['server_id']:<15} {row['severity']:<10} "
              f"{row['status']:<10} {start:<20} {row['event_count']:<7} {metrics}")


def cmd_api(with_scheduler: bool = True):
    """Start the API server, optionally with the scheduler."""
    import uvicorn
    from api.app import create_app

    port = int(os.environ.get("SEEQL_API_PORT", "8080"))
    app = create_app()

    _run_startup_migrations()

    scheduler = None
    if with_scheduler:
        from scheduler.runner import create_scheduler, _run_fast, _run_medium, _run_slow
        import scheduler.runner as runner_module

        scheduler = create_scheduler()
        runner_module._scheduler_instance = scheduler

        logger = logging.getLogger(__name__)
        logger.info("Running initial collection...")
        _run_fast()
        _run_medium()
        _run_slow()
        logger.info("Initial collection complete.")

        scheduler.start()
        logger.info("Scheduler started alongside API server.")

    try:
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    finally:
        # Uvicorn has its own SIGTERM/SIGINT handling; when it returns, tear
        # down the scheduler and flush the SQLite WAL so we don't lose
        # pending writes on Docker restart.
        if scheduler is not None:
            logger = logging.getLogger(__name__)
            logger.info("Shutting down scheduler (API exited)...")
            try:
                scheduler.shutdown(wait=True)
            except Exception as e:
                logger.warning(f"Scheduler shutdown error: {e}")
        try:
            from scheduler.runner import _flush_sqlite
            _flush_sqlite()
        except Exception:
            pass


def main():
    """CLI entry point. Wraps the dispatcher so SeeQLError exceptions print the
    canonical Rust-style block and exit non-zero with a stable code."""
    try:
        _main_inner()
    except Exception as e:
        # Lazy import so a truly broken environment still prints something.
        try:
            from seeql.errors import SeeQLError
        except Exception:
            SeeQLError = tuple()  # type: ignore
        if isinstance(e, SeeQLError):
            print(e.format(), file=sys.stderr)
            # Derive a stable exit code from the numeric suffix of the code
            try:
                sys.exit(int(e.code[1:]))
            except (ValueError, AttributeError):
                sys.exit(1)
        raise  # unknown error — let Python print the full traceback


def _main_inner():
    parser = argparse.ArgumentParser(
        prog="seeql",
        description="SeeQL — LLM-powered MySQL DBA agent",
    )

    # Legacy flags: kept working for one release (suppressed from --help but
    # accepted at runtime). Use `seeql <cmd>` instead.
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--init-db", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--api", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--api-only", action="store_true", help=argparse.SUPPRESS)

    # Subcommand architecture
    sub = parser.add_subparsers(dest="cmd", metavar="<command>")

    sub.add_parser("check", help="Run health checks and exit")
    sub.add_parser("init-db", help="Initialize the monitoring database schema")

    run_p = sub.add_parser("run", help="Start the continuous collection agent")
    run_p.add_argument("--once", dest="run_once", action="store_true",
                       help="Run one cycle and exit")

    serve_p = sub.add_parser("serve", help="Start the API + dashboard server")
    serve_p.add_argument("--no-scheduler", action="store_true",
                         help="Serve API only — don't start the collector")

    sub.add_parser("doctor", help="Diagnose the local environment (7 checks)")

    replay_p = sub.add_parser("replay", help="Replay a past incident")
    replay_p.add_argument("--from", dest="from_ts",
                          help="Start timestamp (ISO8601)")
    replay_p.add_argument("--to", dest="to_ts",
                          help="End timestamp (ISO8601)")
    replay_p.add_argument("--incident", type=int,
                          help="Replay a specific incident_id")
    replay_p.add_argument("--latest", action="store_true",
                          help="Replay the most recent incident")
    replay_p.add_argument("--server", default=None,
                          help="Server ID (defaults to primary)")

    inc_p = sub.add_parser("incidents", help="Browse detected incidents")
    inc_sub = inc_p.add_subparsers(dest="inc_cmd", metavar="<subcommand>")
    list_p = inc_sub.add_parser("list", help="List recent incidents")
    list_p.add_argument("--status", choices=["detected", "analyzed", "resolved"])
    list_p.add_argument("--limit", type=int, default=20)
    list_p.add_argument("--server", default=None)

    args = parser.parse_args()
    setup_logging()

    # Legacy flags win if set (with a one-liner deprecation warning)
    legacy = args.check or args.init_db or args.once or args.api or args.api_only
    if legacy:
        logger = logging.getLogger(__name__)
        logger.warning(
            "DEPRECATED: flag-style invocation. Use `seeql <cmd>` instead "
            "(e.g. `seeql check`). Flags will be removed in v0.2.0."
        )
        if args.check:
            return cmd_check()
        if args.init_db:
            return cmd_init_db()
        if args.once:
            return cmd_once()
        if args.api:
            return cmd_api(with_scheduler=True)
        if args.api_only:
            return cmd_api(with_scheduler=False)

    # Subcommand dispatch
    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "init-db":
        return cmd_init_db()
    if args.cmd == "run":
        return cmd_once() if getattr(args, "run_once", False) else cmd_run()
    if args.cmd == "serve":
        return cmd_api(with_scheduler=not args.no_scheduler)
    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "replay":
        return cmd_replay(args)
    if args.cmd == "incidents":
        return cmd_incidents(args)

    # No subcommand → continuous collector (same as before)
    cmd_run()


if __name__ == "__main__":
    main()
