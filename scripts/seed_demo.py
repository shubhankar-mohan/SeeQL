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
    """UTC timestamp `delta_min` minutes in the past, ISO-8601 with a 'T'.

    This MUST match the format the real collectors write
    (``datetime.now(timezone.utc).replace(tzinfo=None).isoformat()`` — see
    ``collectors/base.py`` + ``storage/writer.py``), i.e. ``2026-07-13T18:12:24``.
    The dashboard's time-series endpoints filter ``snapshot_time BETWEEN ? AND ?``
    with ISO-'T' bounds from ``parse_time_range``; a space-separated timestamp
    sorts lexically *below* a 'T' bound on the same date (0x20 < 0x54), which
    silently empties every ``range=1h`` chart. Keep this as ``.isoformat()``.
    """
    return (NOW - timedelta(minutes=delta_min)).isoformat()


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


# Themed query digests. Each: (digest, sql, schema, sample, exec, avg, maxs,
# rows_examined, rows_sent, full_scans, no_index_used)
_DIGESTS = [
    ("7107e33a", "SELECT * FROM `pirates` WHERE `crew_id` = ?", "grandline",
     "SELECT * FROM pirates WHERE crew_id = 4021", 111672, 1.79, 9.2,
     780_000_000, 780_000_000, 213214, 111672),
    ("abf87900", "SELECT `user_id` FROM `bounties` WHERE `amount` > ? AND `active` = ? LIMIT ?",
     "grandline", "SELECT user_id FROM bounties WHERE amount > 100000000 AND active = 1 LIMIT 50",
     84021, 0.42, 3.1, 33_281_383, 3926, 84021, 84021),
    ("0598ca31", "UPDATE `crews` SET `last_seen` = ? WHERE `crew_id` = ?", "grandline",
     "UPDATE crews SET last_seen = NOW() WHERE crew_id = 4021", 939389, 0.006, 0.2,
     939389, 0, 0, 0),
    ("f0998abc", "SELECT b.`amount`, p.`name` FROM `bounties` b JOIN `pirates` p ON p.`id`=b.`pirate_id` WHERE p.`island_id` = ?",
     "grandline", "SELECT b.amount, p.name FROM bounties b JOIN pirates p ON p.id=b.pirate_id WHERE p.island_id = 12",
     22110, 0.09, 1.1, 4_120_004, 22110, 0, 0),
    ("c19a1fa7", "INSERT INTO `log_poses` (`pirate_id`,`island_id`,`ts`) VALUES (?,?,?)", "grandline",
     "INSERT INTO log_poses (pirate_id, island_id, ts) VALUES (4021, 12, NOW())", 1_200_912, 0.002, 0.05,
     1_200_912, 0, 0, 0),
    ("d4410aa2", "SELECT * FROM `devil_fruits` WHERE `rarity` = ? ORDER BY `power` DESC", "grandline",
     "SELECT * FROM devil_fruits WHERE rarity = 'mythical' ORDER BY power DESC", 40122, 0.31, 2.0,
     8_020_100, 12000, 40122, 40122),
    ("e7712bb0", "SELECT COUNT(*) FROM `marine_reports` WHERE `island_id` = ? AND `ts` > ?", "grandline",
     "SELECT COUNT(*) FROM marine_reports WHERE island_id = 12 AND ts > '2026-07-01'", 60233, 0.05, 0.4,
     600_331, 60233, 0, 0),
    ("a8890c14", "SELECT `name`,`bounty` FROM `pirates` WHERE `id` IN (?)", "grandline",
     "SELECT name, bounty FROM pirates WHERE id IN (4021, 4022, 4023)", 300112, 0.02, 0.3,
     900_336, 900_336, 0, 0),
    # regression digest — appears in BOTH windows; slow now, fast historically
    ("b5956bf0", "SELECT `island_id`, SUM(`amount`) FROM `bounties` GROUP BY `island_id`", "grandline",
     "SELECT island_id, SUM(amount) FROM bounties GROUP BY island_id", 8201, 0.20, 1.5,
     20_100_400, 8201, 8201, 8201),
]


def _seed_queries_and_schema(conn: sqlite3.Connection) -> None:
    # Query digests at the "now" snapshot.
    rows = []
    for (dg, sql, sch, sample, ex, avg, mx, rex, rs, fs, niu) in _DIGESTS:
        rows.append({
            "snapshot_time": NOW_STR, "server_id": SERVER_ID, "digest": dg,
            "digest_text": sql, "query_sample_text": sample, "schema_name": sch,
            "exec_count": ex, "total_time_sec": round(avg * ex, 2),
            "avg_time_sec": avg, "max_time_sec": mx, "min_time_sec": max(avg / 4, 0.0001),
            "rows_examined": rex, "rows_sent": rs, "rows_affected": 0,
            "tmp_tables": fs and 1 or 0, "tmp_disk_tables": 0, "full_joins": 0,
            "full_scans": fs, "no_index_used": niu, "no_good_index_used": 0,
            "sort_merge_passes": 0, "sum_errors": 0, "sum_warnings": 0,
            "first_seen": ts(60 * 24 * 30), "last_seen": NOW_STR,
        })
    insert(conn, "query_digest_snapshots", rows)

    # Regression pair for digest b5956bf0: slow in last hour, fast 1h-7d ago.
    reg = "b5956bf0"
    reg_sql = "SELECT `island_id`, SUM(`amount`) FROM `bounties` GROUP BY `island_id`"
    hist = []
    # baseline: fast (0.02s) at several points ~1..7 days ago. All kept > 24h ago
    # (prior calendar dates) on purpose: the regression banner compares
    # snapshot_time (ISO-'T') against space-separated datetime('now','-1 hour')
    # bounds, and SQLite's byte-wise TEXT compare mis-handles a baseline point
    # that shares TODAY's date with the bound (0x54 'T' > 0x20 ' '), leaking it
    # into "recent" and out of the baseline — which would understate the
    # multiplier. Prior-date points differ in the date prefix, so the compare
    # resolves correctly and the regression shows a clean, stable ~10x.
    for d_min in (1500, 1800, 2400, 2880, 4320, 5760, 10080 - 60):
        hist.append({
            "snapshot_time": ts(d_min), "server_id": SERVER_ID, "digest": reg,
            "digest_text": reg_sql, "query_sample_text": None, "schema_name": "grandline",
            "exec_count": 8000, "total_time_sec": 160.0, "avg_time_sec": 0.02,
            "max_time_sec": 0.3, "min_time_sec": 0.005, "rows_examined": 20_000_000,
            "rows_sent": 8000, "rows_affected": 0, "tmp_tables": 1, "tmp_disk_tables": 0,
            "full_joins": 0, "full_scans": 8000, "no_index_used": 8000,
            "no_good_index_used": 0, "sort_merge_passes": 0, "sum_errors": 0,
            "sum_warnings": 0, "first_seen": ts(60 * 24 * 30), "last_seen": ts(d_min),
        })
    # recent: slow (0.20s) within the last hour
    for d_min in (5, 20, 45):
        hist.append({
            "snapshot_time": ts(d_min), "server_id": SERVER_ID, "digest": reg,
            "digest_text": reg_sql, "query_sample_text": None, "schema_name": "grandline",
            "exec_count": 8201, "total_time_sec": 1640.2, "avg_time_sec": 0.20,
            "max_time_sec": 1.5, "min_time_sec": 0.05, "rows_examined": 20_100_400,
            "rows_sent": 8201, "rows_affected": 0, "tmp_tables": 1, "tmp_disk_tables": 1,
            "full_joins": 0, "full_scans": 8201, "no_index_used": 8201,
            "no_good_index_used": 0, "sort_merge_passes": 3, "sum_errors": 0,
            "sum_warnings": 0, "first_seen": ts(60 * 24 * 30), "last_seen": ts(d_min),
        })
    insert(conn, "query_digest_snapshots", hist)

    # EXPLAIN capture for the pirates full-scan digest (valid MySQL EXPLAIN JSON).
    explain_json = (
        '{"query_block":{"select_id":1,"cost_info":{"query_cost":"812004.00"},'
        '"table":{"table_name":"pirates","access_type":"ALL","rows_examined_per_scan":780000000,'
        '"rows_produced_per_join":780000000,"filtered":"10.00",'
        '"cost_info":{"read_cost":"400000.00","eval_cost":"78000000.00",'
        '"prefix_cost":"812004.00","data_read_per_join":"58G"},'
        '"used_columns":["id","crew_id","name","bounty"],'
        '"attached_condition":"(`grandline`.`pirates`.`crew_id` = 4021)"}}}'
    )
    insert(conn, "explain_captures", [{
        "captured_at": NOW_STR, "server_id": SERVER_ID, "digest": "7107e33a",
        "digest_text": "SELECT * FROM `pirates` WHERE `crew_id` = ?",
        "schema_name": "grandline", "explain_json": explain_json,
        "total_time_sec": 199862.0, "avg_time_sec": 1.79, "exec_count": 111672,
    }])

    # Schema snapshot (table sizes).
    tables = [
        ("pirates", 10_240_000, 812.0, 240.0),
        ("bounties", 8_120_000, 512.0, 190.0),
        ("crews", 42_000, 12.0, 4.0),
        ("devil_fruits", 1_400, 2.0, 1.0),
        ("islands", 900, 1.0, 0.5),
        ("log_poses", 220_000_000, 4200.0, 1200.0),
        ("marine_reports", 90_000_000, 2100.0, 700.0),
    ]
    insert(conn, "schema_snapshots", [{
        "snapshot_time": NOW_STR, "server_id": SERVER_ID, "table_schema": "grandline",
        "table_name": t, "schema_hash": f"h_{t}", "index_hash": f"i_{t}",
        "create_stmt": f"CREATE TABLE `{t}` (\n  `id` bigint NOT NULL AUTO_INCREMENT,\n  PRIMARY KEY (`id`)\n) ENGINE=InnoDB",
        "table_rows": rows_, "data_mb": dmb, "index_mb": imb,
    } for (t, rows_, dmb, imb) in tables])

    # Unused + redundant indexes.
    insert(conn, "unused_index_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "object_schema": "grandline",
         "table_name": "marine_reports", "index_name": "idx_old_reporter"},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID, "object_schema": "grandline",
         "table_name": "devil_fruits", "index_name": "idx_legacy_power"},
    ])
    insert(conn, "redundant_index_snapshots", [{
        "snapshot_time": NOW_STR, "server_id": SERVER_ID, "table_schema": "grandline",
        "table_name": "bounties", "redundant_index_name": "idx_pirate",
        "redundant_index_columns": "pirate_id", "dominant_index_name": "idx_pirate_amount",
        "dominant_index_columns": "pirate_id,amount", "subpart_exists": 0,
        "sql_drop_index": "ALTER TABLE `grandline`.`bounties` DROP INDEX `idx_pirate`",
    }])

    # DDL change in last 24h (index added to bounties).
    insert(conn, "ddl_changes", [{
        "detected_at": ts(180), "server_id": SERVER_ID, "table_schema": "grandline",
        "table_name": "bounties", "change_type": "index",
        "old_schema_hash": "h1", "new_schema_hash": "h1",
        "old_index_hash": "i1", "new_index_hash": "i2",
        "old_ddl": "CREATE TABLE `bounties` (\n  `id` bigint,\n  KEY `idx_pirate` (`pirate_id`)\n)",
        "new_ddl": "CREATE TABLE `bounties` (\n  `id` bigint,\n  KEY `idx_pirate` (`pirate_id`),\n  KEY `idx_pirate_amount` (`pirate_id`,`amount`)\n)",
    }])

    # Table IO (for todo page).
    insert(conn, "table_io_snapshots", [{
        "snapshot_time": NOW_STR, "server_id": SERVER_ID, "object_schema": "grandline",
        "table_name": t, "count_read": r, "count_write": w, "count_fetch": r,
        "count_insert": w // 2, "count_update": w // 2, "count_delete": 0,
        "total_io_sec": io, "read_io_sec": io * 0.7, "write_io_sec": io * 0.3,
    } for (t, r, w, io) in [("pirates", 780_000_000, 940_000, 4120.0),
                            ("bounties", 33_000_000, 1_600_000, 1220.0),
                            ("log_poses", 2_000_000, 1_200_000, 900.0)]])

    # Slow query log (last 24h, >=3 occurrences of one shape for the todo aggregation).
    insert(conn, "slow_query_log", [{
        "snapshot_time": ts(m), "server_id": SERVER_ID, "user": "zoro_batch",
        "host": "10.0.0.61", "query_time_sec": 12.4, "lock_time_sec": 8.1,
        "rows_sent": 8201, "rows_examined": 20_100_400,
        "sql_text": "SELECT island_id, SUM(amount) FROM bounties GROUP BY island_id",
    } for m in (30, 90, 150, 600)])

    # Global variables (todo page reads max_connections).
    insert(conn, "global_variable_snapshots", [
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "variable_name": "max_connections", "variable_value": "256"},
        {"snapshot_time": NOW_STR, "server_id": SERVER_ID,
         "variable_name": "innodb_buffer_pool_size", "variable_value": "8589934592"},
    ])

    # InnoDB status: LATEST DETECTED DEADLOCK within last 10 min (todo page).
    deadlock_text = (
        "LATEST DETECTED DEADLOCK\n------------------------\n"
        "2026-07-11 03:14:02 0x7f\n"
        "*** (1) TRANSACTION: UPDATE bounties ... waiting for lock\n"
        "*** (2) TRANSACTION: UPDATE pirates ... holds the lock\n"
        "*** WE ROLL BACK TRANSACTION (1)\n"
    )
    insert(conn, "innodb_status_snapshots", [{
        "snapshot_time": ts(4), "server_id": SERVER_ID,
        "section_name": "LATEST DETECTED DEADLOCK", "section_data": deadlock_text,
        "parsed_json": '{"victim":"trx1","tables":["bounties","pirates"]}',
    }])


def _seed_timeseries_and_insights(conn: sqlite3.Connection) -> None:
    # --- 24h global_status series (every 15 min) for charts + anomaly baseline ---
    gstat_series = []
    innodb_series = []
    lock_hist = []
    queries_cum = 88_000_000
    rr, ri, ru, rd = 5_000_000_000, 900_000_000, 700_000_000, 40_000_000
    points = list(range(1440, 0, -15))  # 24h ago -> ~now, 15-min steps
    for idx, m in enumerate(points):
        stime = ts(m)
        # baseline Threads_running ~ 10 (+/- 1); Threads_connected ~ 120
        tr = 10 + (idx % 3) - 1
        tc = 120 + (idx % 5)
        queries_cum += 500 * 15 * 60  # steady climb
        qps = 500 + (idx % 7) * 3
        gstat_series.extend([
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Threads_running",
             "raw_value": tr, "delta_value": None, "per_second": None},
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Threads_connected",
             "raw_value": tc, "delta_value": None, "per_second": None},
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Queries",
             "raw_value": queries_cum, "delta_value": qps * 15 * 60, "per_second": float(qps)},
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Innodb_buffer_pool_reads",
             "raw_value": 400000 + idx * 200, "delta_value": 200, "per_second": None},
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Innodb_buffer_pool_read_requests",
             "raw_value": 60_000_000 + idx * 5000, "delta_value": 5000, "per_second": None},
        ])
        innodb_series.extend([
            {"snapshot_time": stime, "server_id": SERVER_ID, "metric_name": name,
             "subsystem": "innodb", "count_value": base + idx * step, "metric_type": "counter"}
            for (name, base, step) in [("rows_read", rr, 3_000_000), ("rows_inserted", ri, 400_000),
                                        ("rows_updated", ru, 300_000), ("rows_deleted", rd, 10_000)]
        ])
        # lock rows across the last ~3h for the history chart
        if m <= 180 and idx % 2 == 0:
            lock_hist.append({
                "snapshot_time": stime, "server_id": SERVER_ID, "waiting_trx_id": f"{48000+idx}",
                "waiting_pid": 5007, "waiting_query": "UPDATE crews SET last_seen = ? WHERE crew_id = ?",
                "wait_seconds": 5 + (idx % 20), "blocking_trx_id": "48120", "blocking_pid": 5099,
                "blocking_query": "UPDATE bounties b JOIN pirates p ON p.id=b.pirate_id SET b.amount = ?",
                "blocking_trx_age_sec": 60 + idx, "blocking_rows_locked": 41200, "blocking_rows_modified": 38110,
            })
    insert(conn, "global_status_snapshots", gstat_series)
    insert(conn, "innodb_metric_snapshots", innodb_series)
    insert(conn, "lock_wait_snapshots", lock_hist)

    # Recent Threads_running outlier ~10 min ago (>=3 sigma over the flat baseline).
    insert(conn, "global_status_snapshots", [{
        "snapshot_time": ts(10), "server_id": SERVER_ID, "variable_name": "Threads_running",
        "raw_value": 48, "delta_value": None, "per_second": None,
    }])

    # Dense last-28-min tail: Threads_running ramps 12 -> ~47 into the spike, and
    # Queries/Threads_connected get finer points, so the overview's range=1h charts
    # render a rich line telling the lock-cascade story rather than 4 sparse dots.
    # Kept strictly inside the anomaly baseline's ~30-min exclusion window so it
    # does not inflate the baseline variance the spike is measured against.
    tail = []
    ramp = [(28, 12, 522), (24, 13, 528), (20, 15, 536), (16, 19, 548),
            (14, 24, 561), (12, 31, 575), (8, 39, 590), (6, 44, 601), (2, 47, 612)]
    for m, tr, qps in ramp:
        stime = ts(m)
        tail.extend([
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Threads_running",
             "raw_value": tr, "delta_value": None, "per_second": None},
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Threads_connected",
             "raw_value": 180 + tr * 3, "delta_value": None, "per_second": None},
            {"snapshot_time": stime, "server_id": SERVER_ID, "variable_name": "Queries",
             "raw_value": 89_000_000 + (30 - m) * 30000, "delta_value": qps * 120,
             "per_second": float(qps)},
        ])
    insert(conn, "global_status_snapshots", tail)

    # --- agent analyses (LLM findings) ---
    findings = (
        '["Nightly bounty-recalculation batch (digest b5956bf0) holds row locks on '
        'hot table `pirates`, blocking live crew writes.",'
        '"Query 7107e33a full-scans `pirates` (780M rows) on every call — no index on crew_id."]'
    )
    recs = (
        '["ADD INDEX `idx_crew` (`crew_id`) on `pirates` to remove the 780M-row full scan.",'
        '"Shard the bounty batch by island_id and lower its isolation to reduce lock span."]'
    )
    insert(conn, "agent_analyses", [{
        "analyzed_at": ts(12), "server_id": SERVER_ID, "analysis_type": "routine",
        "severity": "warning", "input_summary": "Threads_running spike + lock cascade from bounty batch.",
        "findings": findings, "recommendations": recs, "applied": 0,
        "applied_at": None, "outcome_notes": None,
    }])

    # --- incident window ---
    cur = conn.execute(
        "INSERT INTO incident_windows (server_id, start_time, end_time, severity, "
        "involved_metrics, event_count, analysis_id, status) VALUES (?,?,?,?,?,?,?,?)",
        (SERVER_ID, ts(45), ts(20), "critical",
         '["threads_running","lock_waits"]', 14, None, "detected"),
    )
    conn.commit()

    # --- inbound alert + two investigations ---
    cur = conn.execute(
        "INSERT INTO inbound_alerts (provider, received_at, server_id, external_id, "
        "alert_type, severity, summary, payload, signature_verified, processed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("grafana", ts(40), SERVER_ID, "GL-7781", "lock_cascade", "critical",
         "Threads_running 48 (4x baseline) on Grand Line — Prod",
         '{"policy":"lock-wait-policy","value":48}', 1, ts(39)),
    )
    alert_id = cur.lastrowid
    conn.execute(
        "INSERT INTO investigations (inbound_alert_id, server_id, started_at, ended_at, "
        "status, root_cause_summary, confidence, query_count_total) VALUES (?,?,?,?,?,?,?,?)",
        (alert_id, SERVER_ID, ts(38), ts(30), "completed",
         "Nightly bounty-recalculation batch on `pirates` created a lock cascade that "
         "backed up crew-update writes; Threads_running spiked to 48. Recommend indexing "
         "`pirates(crew_id)` and sharding the batch by island_id.", 0.82, 11),
    )
    conn.execute(
        "INSERT INTO investigations (inbound_alert_id, server_id, started_at, ended_at, "
        "status, root_cause_summary, confidence, query_count_total) VALUES (?,?,?,?,?,?,?,?)",
        (alert_id, SERVER_ID, ts(6), None, "phase2", None, None, 4),
    )
    conn.commit()


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
        _seed_queries_and_schema(conn)
        _seed_timeseries_and_insights(conn)
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
