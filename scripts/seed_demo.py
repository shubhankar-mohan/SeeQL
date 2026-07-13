#!/usr/bin/env python3
"""Seed a Grand Line (One Piece) themed SeeQL monitoring DB for screenshots/demo.

Stdlib sqlite3 only. No MySQL, no collectors, no network. Rebuilds the DB from
storage/schema.sql and inserts themed rows that satisfy every dashboard panel's
snapshot/window/server_id filter.

Run:  python scripts/seed_demo.py [--db data/grandline_demo.db]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone

SERVER_ID = "grandline-prod"
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "storage", "schema.sql")
DEFAULT_DB = os.path.join(os.path.dirname(__file__), "..", "data", "grandline_demo.db")

NOW = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


def ts(delta_min: int = 0) -> str:
    """UTC timestamp `delta_min` minutes in the past, 'YYYY-MM-DD HH:MM:SS'."""
    return (NOW - timedelta(minutes=delta_min)).strftime("%Y-%m-%d %H:%M:%S")


NOW_STR = ts(0)


def insert(conn: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    """Column-agnostic bulk insert; every row dict must share the same keys."""
    if not rows:
        return
    cols = list(rows[0].keys())
    placeholders = ",".join("?" * len(cols))
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})"
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])


def _create_schema(conn: sqlite3.Connection) -> None:
    with open(os.path.normpath(SCHEMA_PATH), "r", encoding="utf-8") as fh:
        conn.executescript(fh.read())


def _seed_server(conn: sqlite3.Connection) -> None:
    insert(conn, "servers", [{
        "server_id": SERVER_ID,
        "display_name": "Grand Line — Prod",
        "environment": "production",
        "role": "primary",
        "cluster_id": "grand-line",
        "tags": '["demo","grandline"]',
        "host": "10.0.0.1",
        "port": 3306,
        "is_active": 1,
        "created_at": ts(0),
        "updated_at": ts(0),
    }])


def _seed_current_state(conn: sqlite3.Connection) -> None:
    # --- processlist (drives thread count KPI) ---
    users = ["luffy_svc", "nami_svc", "zoro_batch", "sanji_svc", "usopp_svc"]
    states = ["Sending data", "updating", "statistics", "Sending data", "System lock"]
    proc = []
    for i in range(28):
        proc.append({
            "snapshot_time": NOW_STR, "server_id": SERVER_ID,
            "thread_id": 1000 + i, "pid": 5000 + i,
            "user": users[i % len(users)], "db": "grandline",
            "command": "Query", "state": states[i % len(states)],
            "time_sec": (i % 9), "query": "SELECT bounty FROM pirates WHERE crew_id = ?",
        })
    insert(conn, "processlist_snapshots", proc)

    # --- current global_status counters (latest snapshot) ---
    gstat_now = {
        "Threads_running": 47, "Threads_connected": 210, "Queries": 88123456,
        "Innodb_buffer_pool_reads": 480221, "Innodb_buffer_pool_read_requests": 61200339,
    }
    insert(conn, "global_status_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "variable_name": k,
         "raw_value": v, "delta_value": None, "per_second": None}
        for k, v in gstat_now.items()
    ])

    # --- buffer pool ---
    insert(conn, "buffer_pool_snapshots", [{
        "snapshot_time": NOW_STR, "server_id": SERVER_ID, "pool_id": 0,
        "pool_size": 131072, "free_buffers": 2048, "database_pages": 126000,
        "dirty_pages": 3120, "pending_reads": 0, "pages_read": 91002331,
        "pages_written": 40233110, "hit_ratio": 0.0,
    }])

    # --- lock waits: nightly bounty batch blocks live crew writes ---
    insert(conn, "lock_wait_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "waiting_trx_id": "48210", "waiting_pid": 5007,
         "waiting_query": "UPDATE crews SET last_seen = ? WHERE crew_id = ?",
         "wait_seconds": 38, "blocking_trx_id": "48120", "blocking_pid": 5099,
         "blocking_query": "UPDATE bounties b JOIN pirates p ON p.id=b.pirate_id SET b.amount = ?",
         "blocking_trx_age_sec": 92, "blocking_rows_locked": 41200, "blocking_rows_modified": 38110},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "waiting_trx_id": "48211", "waiting_pid": 5011,
         "waiting_query": "INSERT INTO log_poses (pirate_id, island_id, ts) VALUES (?,?,?)",
         "wait_seconds": 17, "blocking_trx_id": "48120", "blocking_pid": 5099,
         "blocking_query": "UPDATE bounties b JOIN pirates p ON p.id=b.pirate_id SET b.amount = ?",
         "blocking_trx_age_sec": 92, "blocking_rows_locked": 41200, "blocking_rows_modified": 38110},
    ])

    # --- active transactions (ages spanning >60 and >120) ---
    insert(conn, "transaction_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "trx_id": "48120",
         "trx_state": "RUNNING", "trx_started": ts(2), "age_sec": 132, "pid": 5099,
         "trx_query": "UPDATE bounties b JOIN pirates p ON p.id=b.pirate_id SET b.amount = ?",
         "operation_state": "updating", "tables_in_use": 2, "tables_locked": 2,
         "lock_structs": 512, "rows_locked": 41200, "rows_modified": 38110,
         "isolation_level": "REPEATABLE READ"},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "trx_id": "48210",
         "trx_state": "LOCK WAIT", "trx_started": ts(1), "age_sec": 72, "pid": 5007,
         "trx_query": "UPDATE crews SET last_seen = ? WHERE crew_id = ?",
         "operation_state": "starting", "tables_in_use": 1, "tables_locked": 1,
         "lock_structs": 3, "rows_locked": 1, "rows_modified": 0,
         "isolation_level": "REPEATABLE READ"},
    ])

    # --- metadata locks (one PENDING DDL on bounties) ---
    insert(conn, "metadata_lock_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "object_type": "TABLE",
         "object_schema": "grandline", "object_name": "bounties", "lock_type": "EXCLUSIVE",
         "lock_duration": "TRANSACTION", "lock_status": "PENDING", "owner_thread_id": 1099},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "object_type": "TABLE",
         "object_schema": "grandline", "object_name": "pirates", "lock_type": "SHARED_READ",
         "lock_duration": "TRANSACTION", "lock_status": "GRANTED", "owner_thread_id": 1007},
    ])

    # --- wait events (total_wait_sec > 0) ---
    insert(conn, "wait_event_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "event_name": "wait/io/table/sql/handler", "count_star": 8820331,
         "total_wait_sec": 4120.5, "avg_wait_sec": 0.00047},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "event_name": "wait/synch/mutex/innodb/buf_pool_mutex", "count_star": 221004,
         "total_wait_sec": 812.2, "avg_wait_sec": 0.0037},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "event_name": "wait/lock/table/sql/handler", "count_star": 40221,
         "total_wait_sec": 388.9, "avg_wait_sec": 0.0097},
    ])


def build(db_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        _create_schema(conn)
        _seed_server(conn)
        _seed_current_state(conn)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed the Grand Line demo DB.")
    ap.add_argument("--db", default=os.path.normpath(DEFAULT_DB))
    args = ap.parse_args()
    build(args.db)
    print(f"Seeded demo DB at {args.db} (server_id={SERVER_ID}, now={NOW_STR})")


if __name__ == "__main__":
    main()
