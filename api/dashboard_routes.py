"""Dashboard HTML routes and HTMX partials."""

import json
import logging
from fastapi import APIRouter, Request, Query as QueryParam
from fastapi.responses import HTMLResponse
from api.query_helpers import query_rows, query_single, resolve_server_id, get_all_servers_for_ui
from api.remediation import get_remediation

logger = logging.getLogger(__name__)
router = APIRouter(tags=["dashboard"])

PAGE_SIZE = 25


def _server_context(request: Request, server: str | None = None) -> dict:
    """Build template context dict with server selector data.

    Inject into every TemplateResponse context via: **_server_context(request, server)
    """
    sid = resolve_server_id(server)
    server_data = get_all_servers_for_ui()

    # Find current server info
    current = next((s for s in server_data["servers"] if s["server_id"] == sid), None)

    return {
        "current_server_id": sid,
        "current_server_name": current["display_name"] if current else sid,
        "current_server_role": current["role"] if current else "primary",
        "servers_list": server_data["servers"],
        "servers_grouped": server_data["grouped"],
    }


# ---------------------------------------------------------------------------
# Full page routes
# ---------------------------------------------------------------------------

@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_overview(request: Request, server: str = None):
    """Overview page — is the server on fire?"""
    active_threads = query_rows("""
        SELECT COUNT(*) as cnt FROM processlist_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM processlist_snapshots)
    """)
    thread_count = active_threads[0]["cnt"] if active_threads else 0

    # Threads_running is the LOAD-BEARING metric during lock cascades
    # (see PLAN.md Phase 2.11.3). Threads_connected is context only —
    # connection pooling keeps it high regardless of health.
    threads_row = query_rows("""
        SELECT variable_name, raw_value FROM global_status_snapshots
        WHERE variable_name IN ('Threads_running', 'Threads_connected')
          AND snapshot_time = (
              SELECT MAX(snapshot_time) FROM global_status_snapshots
              WHERE variable_name = 'Threads_running'
          )
    """)
    threads_running = None
    threads_connected = None
    for r in threads_row:
        if r["variable_name"] == "Threads_running":
            threads_running = r["raw_value"]
        elif r["variable_name"] == "Threads_connected":
            threads_connected = r["raw_value"]

    current_locks = query_rows("""
        SELECT * FROM lock_wait_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
        ORDER BY wait_seconds DESC
    """)

    # Compute hit ratio from cumulative global_status_snapshots counters
    # (see api.query_helpers.latest_hit_ratio_pct docstring for rationale)
    from api.query_helpers import latest_hit_ratio_pct
    bp_pages = query_single("""
        SELECT dirty_pages, free_buffers, database_pages
        FROM buffer_pool_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM buffer_pool_snapshots)
    """) or {}
    bp = {
        "hit_ratio": latest_hit_ratio_pct(),
        "dirty_pages": bp_pages.get("dirty_pages", 0),
        "free_buffers": bp_pages.get("free_buffers", 0),
        "database_pages": bp_pages.get("database_pages", 0),
    }

    top_query = query_single("""
        SELECT digest_text, avg_time_sec, total_time_sec, exec_count
        FROM query_digest_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM query_digest_snapshots)
        ORDER BY total_time_sec DESC LIMIT 1
    """)

    ddl_changes = query_rows("""
        SELECT detected_at, table_schema, table_name, change_type
        FROM ddl_changes
        WHERE detected_at >= datetime('now', '-24 hours')
        ORDER BY detected_at DESC LIMIT 10
    """)

    long_txns = query_rows("""
        SELECT trx_id, age_sec, trx_query, pid
        FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
          AND age_sec > 60
        ORDER BY age_sec DESC
    """)

    anomaly_count = _get_anomaly_count()

    # First-run detection (Phase 3.2) — if we've never collected anything,
    # show a neutral WAITING state instead of a false-positive HEALTHY.
    zero_data_row = query_single("SELECT COUNT(*) as c FROM query_digest_snapshots")
    zero_data = (zero_data_row or {}).get("c", 0) == 0

    lock_count = len(current_locks)
    has_long_locks = any(l["wait_seconds"] > 10 for l in current_locks)
    has_long_txns = len(long_txns) > 0
    if zero_data:
        health = "waiting"
    elif has_long_locks or lock_count > 5:
        health = "red"
    elif has_long_txns or lock_count > 0 or ddl_changes or anomaly_count > 0:
        health = "yellow"
    else:
        health = "green"

    return request.app.state.templates.TemplateResponse(request, "dashboard/overview.html", {
        "request": request,
        "health": health,
        "zero_data": zero_data,
        "thread_count": thread_count,
        "threads_running": threads_running,
        "threads_connected": threads_connected,
        "lock_count": lock_count,
        "current_locks": current_locks,
        "bp": bp or {"hit_ratio": None, "dirty_pages": 0},
        "top_query": top_query,
        "ddl_changes": ddl_changes,
        "long_txns": long_txns,
        "anomaly_count": anomaly_count,
        "page": "overview",
        **_server_context(request, server),
    })


@router.get("/dashboard/queries", response_class=HTMLResponse)
def dashboard_queries(request: Request, range: str = "24h", sort: str = "total_time_sec",
                      pg: int = 1, server: str = None):
    """Query performance page with pagination."""
    from api.query_helpers import parse_time_range
    start, end = parse_time_range(range)

    allowed_sorts = {"total_time_sec", "avg_time_sec", "exec_count", "rows_examined", "full_scans"}
    sort_col = sort if sort in allowed_sorts else "total_time_sec"

    # Count total
    total_row = query_single("""
        SELECT COUNT(DISTINCT digest) as cnt
        FROM query_digest_snapshots
        WHERE snapshot_time BETWEEN ? AND ?
    """, (start, end))
    total_count = total_row["cnt"] if total_row else 0
    total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
    pg = max(1, min(pg, total_pages))
    offset = (pg - 1) * PAGE_SIZE

    queries = query_rows(f"""
        SELECT
            digest,
            SUBSTR(digest_text, 1, 120) as digest_text,
            schema_name,
            SUM(exec_count) as exec_count,
            SUM(total_time_sec) as total_time_sec,
            AVG(avg_time_sec) as avg_time_sec,
            MAX(max_time_sec) as max_time_sec,
            SUM(rows_examined) as rows_examined,
            SUM(rows_sent) as rows_sent,
            SUM(full_scans) as full_scans,
            SUM(no_index_used) as no_index_used,
            MAX(last_seen) as last_seen
        FROM query_digest_snapshots
        WHERE snapshot_time BETWEEN ? AND ?
        GROUP BY digest
        ORDER BY {sort_col} DESC
        LIMIT ? OFFSET ?
    """, (start, end, PAGE_SIZE, offset))

    regressions = query_rows("""
        WITH recent AS (
            SELECT digest, AVG(avg_time_sec) as recent_avg
            FROM query_digest_snapshots
            WHERE snapshot_time >= datetime('now', '-1 hour')
            GROUP BY digest
        ),
        baseline AS (
            SELECT digest, AVG(avg_time_sec) as baseline_avg
            FROM query_digest_snapshots
            WHERE snapshot_time BETWEEN datetime('now', '-7 days') AND datetime('now', '-1 hour')
            GROUP BY digest
        )
        SELECT r.digest, r.recent_avg / NULLIF(b.baseline_avg, 0) as factor
        FROM recent r JOIN baseline b ON r.digest = b.digest
        WHERE b.baseline_avg > 0 AND r.recent_avg / b.baseline_avg >= 3.0
    """)
    regression_digests = {r["digest"] for r in regressions}

    return request.app.state.templates.TemplateResponse(request, "dashboard/queries.html", {
        "request": request,
        "queries": queries,
        "regression_digests": regression_digests,
        "current_range": range,
        "current_sort": sort_col,
        "page": "queries",
        "pg": pg,
        "total_pages": total_pages,
        "total_count": total_count,
        **_server_context(request, server),
    })


@router.get("/dashboard/locks", response_class=HTMLResponse)
def dashboard_locks(request: Request, server: str = None):
    """Locks & transactions page."""
    current_locks = query_rows("""
        SELECT * FROM lock_wait_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
        ORDER BY wait_seconds DESC
    """)
    active_txns = query_rows("""
        SELECT * FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
        ORDER BY age_sec DESC
    """)
    metadata_locks = query_rows("""
        SELECT * FROM metadata_lock_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM metadata_lock_snapshots)
    """)
    return request.app.state.templates.TemplateResponse(request, "dashboard/locks.html", {
        "request": request,
        "current_locks": current_locks,
        "active_txns": active_txns,
        "metadata_locks": metadata_locks,
        "page": "locks",
        **_server_context(request, server),
    })


@router.get("/dashboard/schema", response_class=HTMLResponse)
def dashboard_schema(request: Request, pg_unused: int = 1, pg_redundant: int = 1, server: str = None):
    """Schema changes page with paginated index lists."""
    ddl_changes = query_rows("""
        SELECT id, detected_at, table_schema, table_name, change_type,
               SUBSTR(old_ddl, 1, 500) as old_ddl,
               SUBSTR(new_ddl, 1, 500) as new_ddl
        FROM ddl_changes
        ORDER BY detected_at DESC LIMIT 20
    """)
    table_sizes = query_rows("""
        SELECT table_schema, table_name, table_rows, data_mb, index_mb,
               data_mb + index_mb as total_mb
        FROM schema_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM schema_snapshots)
        ORDER BY data_mb + index_mb DESC LIMIT 50
    """)

    # Paginated unused indexes
    unused_total_row = query_single("""
        SELECT COUNT(*) as cnt FROM unused_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM unused_index_snapshots)
    """)
    unused_total = unused_total_row["cnt"] if unused_total_row else 0
    unused_pages = max(1, (unused_total + PAGE_SIZE - 1) // PAGE_SIZE)
    pg_unused = max(1, min(pg_unused, unused_pages))
    unused_indexes = query_rows("""
        SELECT object_schema, table_name, index_name
        FROM unused_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM unused_index_snapshots)
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, (pg_unused - 1) * PAGE_SIZE))

    # Paginated redundant indexes
    red_total_row = query_single("""
        SELECT COUNT(*) as cnt FROM redundant_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM redundant_index_snapshots)
    """)
    red_total = red_total_row["cnt"] if red_total_row else 0
    red_pages = max(1, (red_total + PAGE_SIZE - 1) // PAGE_SIZE)
    pg_redundant = max(1, min(pg_redundant, red_pages))
    redundant_indexes = query_rows("""
        SELECT table_schema, table_name, redundant_index_name,
               redundant_index_columns, dominant_index_name,
               dominant_index_columns, sql_drop_index
        FROM redundant_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM redundant_index_snapshots)
        LIMIT ? OFFSET ?
    """, (PAGE_SIZE, (pg_redundant - 1) * PAGE_SIZE))

    return request.app.state.templates.TemplateResponse(request, "dashboard/schema.html", {
        "request": request,
        "ddl_changes": ddl_changes,
        "table_sizes": table_sizes,
        "unused_indexes": unused_indexes,
        "redundant_indexes": redundant_indexes,
        "unused_total": unused_total,
        "unused_pages": unused_pages,
        "pg_unused": pg_unused,
        "red_total": red_total,
        "red_pages": red_pages,
        "pg_redundant": pg_redundant,
        "page": "schema",
        **_server_context(request, server),
    })


@router.get("/dashboard/server", response_class=HTMLResponse)
def dashboard_server(request: Request, server: str = None):
    """Server metrics page."""
    wait_events = query_rows("""
        SELECT event_name, count_star, total_wait_sec, avg_wait_sec
        FROM wait_event_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM wait_event_snapshots)
          AND total_wait_sec > 0
        ORDER BY total_wait_sec DESC LIMIT 10
    """)
    return request.app.state.templates.TemplateResponse(request, "dashboard/server.html", {
        "request": request,
        "wait_events": wait_events,
        "page": "server",
        **_server_context(request, server),
    })


@router.get("/dashboard/todo", response_class=HTMLResponse)
def dashboard_todo(request: Request, server: str = None):
    """Action center — organized around the 3 DBA purposes."""
    from api.remediation import suggest_indexes, get_queries_to_optimize

    # =========================================================================
    # SECTION 1: Emergency — stop a crash
    # =========================================================================
    emergency_items = []

    # Active long lock waits
    long_locks = query_rows("""
        SELECT waiting_pid, blocking_pid, wait_seconds, waiting_query, blocking_query,
               snapshot_time
        FROM lock_wait_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
          AND wait_seconds > 10
        ORDER BY wait_seconds DESC
    """)
    lock_rem = get_remediation("lock_wait")
    for lock in long_locks:
        emergency_items.append({
            "icon": "&#128680;",
            "title": f"Lock wait {lock['wait_seconds']}s — KILL {lock['blocking_pid']}",
            "detail": f"PID {lock['waiting_pid']} blocked by PID {lock['blocking_pid']}",
            "query_preview": (lock.get("waiting_query") or "")[:100],
            "timestamp": lock.get("snapshot_time", ""),
            "diagnosis": lock_rem.get("diagnosis", ""),
            "steps": lock_rem.get("steps", []),
            "queries": [q.replace("<blocking_pid>", str(lock["blocking_pid"]))
                        for q in lock_rem.get("queries", [])],
        })

    # Deadlocks
    deadlocks = query_rows("""
        SELECT parsed_json, snapshot_time FROM innodb_status_snapshots
        WHERE section_name = 'LATEST DETECTED DEADLOCK'
          AND snapshot_time >= datetime('now', '-10 minutes')
        ORDER BY snapshot_time DESC LIMIT 1
    """)
    if deadlocks and deadlocks[0].get("parsed_json"):
        emergency_items.append({
            "icon": "&#128128;",
            "title": "Deadlock detected — check lock ordering",
            "detail": "InnoDB rolled back one transaction. Recurring = lock ordering bug.",
            "query_preview": "",
            "timestamp": deadlocks[0].get("snapshot_time", ""),
            "diagnosis": "Two transactions formed a circular lock dependency.",
            "steps": ["SHOW ENGINE INNODB STATUS for details.",
                      "Fix lock ordering in application code.",
                      "Shorten transactions to reduce lock window."],
            "queries": ["SHOW ENGINE INNODB STATUS\\G"],
        })

    # Long transactions > 120s (critical threshold)
    critical_txns = query_rows("""
        SELECT trx_id, age_sec, trx_query, pid, rows_locked, rows_modified,
               snapshot_time
        FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
          AND age_sec > 120
        ORDER BY age_sec DESC
    """)
    txn_rem = get_remediation("long_transaction")
    for txn in critical_txns:
        emergency_items.append({
            "icon": "&#9203;",
            "title": f"Transaction {txn['age_sec']}s — PID {txn['pid']}",
            "detail": f"Rows locked: {txn.get('rows_locked', 0)}, modified: {txn.get('rows_modified', 0)}",
            "query_preview": (txn.get("trx_query") or "idle")[:100],
            "timestamp": txn.get("snapshot_time", ""),
            "diagnosis": txn_rem.get("diagnosis", ""),
            "steps": txn_rem.get("steps", []),
            "queries": [q.replace("<pid>", str(txn["pid"])) for q in txn_rem.get("queries", [])],
        })

    # Connection near max
    threads_connected = query_single("""
        SELECT raw_value as val FROM global_status_snapshots
        WHERE variable_name = 'Threads_connected'
        ORDER BY snapshot_time DESC LIMIT 1
    """)
    max_connections = query_single("""
        SELECT variable_value as val FROM global_variable_snapshots
        WHERE variable_name = 'max_connections'
        ORDER BY snapshot_time DESC LIMIT 1
    """)
    if threads_connected and max_connections:
        try:
            tc = int(threads_connected["val"])
            mc = int(max_connections["val"])
            if mc > 0 and tc / mc > 0.8:
                pct = tc / mc * 100
                emergency_items.append({
                    "icon": "&#128268;",
                    "title": f"Connections {tc}/{mc} ({pct:.0f}%) — near max",
                    "detail": "New connections will be refused at max_connections.",
                    "query_preview": "",
                    "timestamp": "",
                    "diagnosis": "Connection pool may be leaking or load is spiking.",
                    "steps": ["Check for idle connections (SLEEP state).",
                              "Review app pool max size.",
                              "If legitimate load, increase max_connections."],
                    "queries": [
                        "SELECT * FROM performance_schema.threads\nWHERE type = 'FOREGROUND' AND processlist_command = 'Sleep'\nORDER BY processlist_time DESC LIMIT 20;",
                    ],
                })
        except (ValueError, TypeError):
            pass

    # =========================================================================
    # SECTION 2: Diagnostics — what's wrong and how to fix
    # =========================================================================
    diagnostic_items = []

    # Query regressions with frequency
    regressions = query_rows("""
        WITH recent AS (
            SELECT digest, digest_text, schema_name,
                   AVG(avg_time_sec) as recent_avg,
                   SUM(exec_count) as recent_execs
            FROM query_digest_snapshots
            WHERE snapshot_time >= datetime('now', '-1 hour')
            GROUP BY digest
        ),
        baseline AS (
            SELECT digest, AVG(avg_time_sec) as baseline_avg
            FROM query_digest_snapshots
            WHERE snapshot_time BETWEEN datetime('now', '-7 days') AND datetime('now', '-1 hour')
            GROUP BY digest
        )
        SELECT r.digest, SUBSTR(r.digest_text, 1, 150) as digest_text,
               r.recent_avg, b.baseline_avg, r.recent_execs,
               r.recent_avg / NULLIF(b.baseline_avg, 0) as factor
        FROM recent r JOIN baseline b ON r.digest = b.digest
        WHERE b.baseline_avg > 0 AND r.recent_avg / b.baseline_avg >= 3.0
        ORDER BY factor DESC LIMIT 10
    """)
    reg_rem = get_remediation("query_regression")
    for reg in regressions:
        diagnostic_items.append({
            "icon": "&#9650;",
            "title": f"{reg.get('factor', 0):.1f}x regression: {reg['baseline_avg']:.4f}s -> {reg['recent_avg']:.4f}s",
            "detail": f"{(reg.get('digest_text') or '')[:100]}",
            "query_preview": "",
            "timestamp": "",
            "frequency": f"{reg.get('recent_execs', 0):,} execs in last hour",
            "diagnosis": reg_rem.get("diagnosis", ""),
            "steps": reg_rem.get("steps", []),
            "queries": [f"EXPLAIN {reg.get('digest_text', '')[:200]}"],
        })

    # DDL changes — only show if they correlate with query regressions or are recent
    ddl_changes = query_rows("""
        SELECT d.detected_at, d.table_schema, d.table_name, d.change_type,
               SUBSTR(d.old_ddl, 1, 300) as old_ddl,
               SUBSTR(d.new_ddl, 1, 300) as new_ddl
        FROM ddl_changes d
        WHERE d.detected_at >= datetime('now', '-24 hours')
        ORDER BY d.detected_at DESC LIMIT 10
    """)
    # Check if any regressed queries reference DDL-changed tables
    regressed_tables = set()
    for reg in regressions:
        txt = (reg.get("digest_text") or "").lower()
        for change in ddl_changes:
            if change["table_name"].lower() in txt:
                regressed_tables.add(change["table_name"].lower())

    ddl_rem = get_remediation("ddl_change")
    for change in ddl_changes:
        tbl = f"{change['table_schema']}.{change['table_name']}"
        is_impactful = change["table_name"].lower() in regressed_tables
        if not is_impactful and change["change_type"] == "schema":
            # Skip pure schema changes that aren't causing regressions
            # (new tables, column adds that don't affect existing queries)
            continue
        diagnostic_items.append({
            "icon": "&#128221;",
            "title": f"DDL: {tbl} ({change['change_type']})" + (" — queries regressed!" if is_impactful else ""),
            "detail": "Queries referencing this table got slower" if is_impactful else "Index or schema change — verify query plans",
            "query_preview": "",
            "timestamp": change["detected_at"],
            "frequency": "",
            "diagnosis": ddl_rem.get("diagnosis", ""),
            "steps": ddl_rem.get("steps", []),
            "queries": [f"EXPLAIN SELECT * FROM {tbl} WHERE 1 LIMIT 1;"],
            "old_ddl": change.get("old_ddl", ""),
            "new_ddl": change.get("new_ddl", ""),
        })

    # Anomalies
    anomalies = _get_anomalies()
    anom_rem = get_remediation("anomaly")
    for a in anomalies:
        dir_word = "above" if a.direction == "high" else "below"
        diagnostic_items.append({
            "icon": "&#128200;",
            "title": f"Anomaly: {a.metric} ({a.severity})",
            "detail": f"Current: {a.current:.2f} vs baseline: {a.baseline_mean:.2f} ({a.pct_change:+.0f}% {dir_word}, z={a.z_score:.1f})",
            "query_preview": "",
            "timestamp": "",
            "frequency": "",
            "diagnosis": anom_rem.get("diagnosis", ""),
            "steps": anom_rem.get("steps", []),
            "queries": [],
            "anomaly": a,
        })

    # Moderate long transactions (60-120s)
    moderate_txns = query_rows("""
        SELECT trx_id, age_sec, trx_query, pid, rows_locked, rows_modified,
               snapshot_time
        FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
          AND age_sec BETWEEN 60 AND 120
        ORDER BY age_sec DESC
    """)
    for txn in moderate_txns:
        diagnostic_items.append({
            "icon": "&#9203;",
            "title": f"Long transaction: {txn['age_sec']}s (PID {txn['pid']})",
            "detail": f"Rows locked: {txn.get('rows_locked', 0)}, modified: {txn.get('rows_modified', 0)}",
            "query_preview": (txn.get("trx_query") or "idle")[:100],
            "timestamp": txn.get("snapshot_time", ""),
            "frequency": "",
            "diagnosis": txn_rem.get("diagnosis", ""),
            "steps": txn_rem.get("steps", []),
            "queries": [f"KILL {txn['pid']};"],
        })

    # =========================================================================
    # SECTION 3: Optimization — better utilize the system
    # =========================================================================

    # Queries to optimize (with specific recommendations)
    queries_to_optimize = get_queries_to_optimize()

    # Index suggestions
    index_suggestions = suggest_indexes()

    # Unused indexes (top 25 with DROP SQL)
    unused_indexes = query_rows("""
        SELECT object_schema, table_name, index_name
        FROM unused_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM unused_index_snapshots)
        LIMIT 25
    """)

    # Redundant indexes (top 25 with DROP SQL)
    redundant_indexes = query_rows("""
        SELECT table_schema, table_name, redundant_index_name,
               redundant_index_columns, dominant_index_name,
               dominant_index_columns, sql_drop_index
        FROM redundant_index_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM redundant_index_snapshots)
        LIMIT 25
    """)

    # Tables without PK
    no_pk = query_rows("""
        SELECT DISTINCT table_schema, table_name
        FROM schema_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM schema_snapshots)
          AND create_stmt NOT LIKE '%PRIMARY KEY%'
        LIMIT 10
    """)

    # =========================================================================
    # SECTION 4: Insights — what an engineer should know
    # =========================================================================
    insights = []

    # Table hotspots: most IO
    table_io = query_rows("""
        SELECT object_schema, table_name,
               SUM(count_read) as reads, SUM(count_write) as writes,
               SUM(total_io_sec) as io_sec
        FROM table_io_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM table_io_snapshots)
        ORDER BY SUM(total_io_sec) DESC LIMIT 5
    """)
    if table_io:
        hotspot_lines = []
        for t in table_io:
            rw_ratio = t["reads"] / t["writes"] if t["writes"] and t["writes"] > 0 else float('inf')
            hotspot_lines.append(
                f"{t['object_schema']}.{t['table_name']}: "
                f"{t['reads']:,} reads, {t['writes']:,} writes "
                f"({'read-heavy' if rw_ratio > 10 else 'write-heavy' if rw_ratio < 0.5 else 'mixed'}, "
                f"{t['io_sec']:.2f}s IO)"
            )
        insights.append({
            "icon": "&#128293;",
            "title": "Table Hotspots (by IO)",
            "lines": hotspot_lines,
        })

    # Slow query log repeaters
    slow_repeaters = query_rows("""
        SELECT SUBSTR(sql_text, 1, 120) as sql_text,
               COUNT(*) as cnt,
               AVG(query_time_sec) as avg_time,
               MAX(query_time_sec) as max_time
        FROM slow_query_log
        WHERE snapshot_time >= datetime('now', '-24 hours')
        GROUP BY SUBSTR(sql_text, 1, 120)
        HAVING COUNT(*) >= 3
        ORDER BY COUNT(*) DESC LIMIT 5
    """)
    if slow_repeaters:
        lines = [f"{r['cnt']}x | avg {r['avg_time']:.2f}s | max {r['max_time']:.2f}s | {r['sql_text'][:80]}"
                 for r in slow_repeaters]
        insights.append({
            "icon": "&#128034;",
            "title": "Slow Query Log Repeaters (24h)",
            "lines": lines,
        })

    # Storage growth: largest tables with index overhead
    storage = query_rows("""
        SELECT table_schema, table_name, table_rows, data_mb, index_mb,
               CASE WHEN data_mb > 0 THEN index_mb / data_mb ELSE 0 END as idx_ratio
        FROM schema_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM schema_snapshots)
          AND data_mb > 10
        ORDER BY data_mb + index_mb DESC LIMIT 5
    """)
    if storage:
        lines = []
        for s in storage:
            total = (s["data_mb"] or 0) + (s["index_mb"] or 0)
            lines.append(
                f"{s['table_schema']}.{s['table_name']}: "
                f"{total:.0f}MB ({s['data_mb']:.0f} data + {s['index_mb']:.0f} idx), "
                f"~{s['table_rows']:,} rows"
                + (f" — high index overhead ({s['idx_ratio']:.1f}x)" if s["idx_ratio"] and s["idx_ratio"] > 1.5 else "")
            )
        insights.append({
            "icon": "&#128190;",
            "title": "Largest Tables",
            "lines": lines,
        })

    # Peak hour detection
    peak = query_single("""
        SELECT strftime('%H', snapshot_time) as hour,
               AVG(per_second) as avg_qps
        FROM global_status_snapshots
        WHERE variable_name = 'Queries' AND per_second IS NOT NULL
          AND snapshot_time >= datetime('now', '-7 days')
        GROUP BY strftime('%H', snapshot_time)
        ORDER BY AVG(per_second) DESC LIMIT 1
    """)
    trough = query_single("""
        SELECT strftime('%H', snapshot_time) as hour,
               AVG(per_second) as avg_qps
        FROM global_status_snapshots
        WHERE variable_name = 'Queries' AND per_second IS NOT NULL
          AND snapshot_time >= datetime('now', '-7 days')
        GROUP BY strftime('%H', snapshot_time)
        ORDER BY AVG(per_second) ASC LIMIT 1
    """)
    if peak and trough and peak.get("avg_qps") and trough.get("avg_qps"):
        insights.append({
            "icon": "&#128200;",
            "title": "Traffic Pattern (7d)",
            "lines": [
                f"Peak hour: {peak['hour']}:00 UTC ({peak['avg_qps']:.0f} QPS avg)",
                f"Trough hour: {trough['hour']}:00 UTC ({trough['avg_qps']:.0f} QPS avg)",
                f"Peak/trough ratio: {peak['avg_qps']/trough['avg_qps']:.1f}x" if trough["avg_qps"] > 0 else "",
                "Schedule maintenance and batch jobs during trough hours.",
            ],
        })

    return request.app.state.templates.TemplateResponse(request, "dashboard/todo.html", {
        "request": request,
        "emergency_items": emergency_items,
        "diagnostic_items": diagnostic_items,
        "queries_to_optimize": queries_to_optimize,
        "index_suggestions": index_suggestions,
        "unused_indexes": unused_indexes,
        "redundant_indexes": redundant_indexes,
        "no_pk": no_pk,
        "insights": insights,
        "page": "todo",
        **_server_context(request, server),
    })


# ---------------------------------------------------------------------------
# Query frequency trend (for todo page charts)
# ---------------------------------------------------------------------------

@router.get("/api/v1/queries/{digest}/frequency")
def api_query_frequency(digest: str, range: str = "24h"):
    """Return hourly execution counts for a query digest."""
    from api.query_helpers import parse_time_range
    start, end = parse_time_range(range)
    rows = query_rows("""
        SELECT strftime('%Y-%m-%dT%H:00:00', snapshot_time) as hour,
               SUM(exec_count) as execs,
               AVG(avg_time_sec) as avg_time
        FROM query_digest_snapshots
        WHERE digest = ? AND snapshot_time BETWEEN ? AND ?
        GROUP BY strftime('%Y-%m-%dT%H:00:00', snapshot_time)
        ORDER BY hour
    """, (digest, start, end))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# HTMX Partials
# ---------------------------------------------------------------------------

@router.get("/dashboard/partials/health-bar", response_class=HTMLResponse)
def partial_health_bar(request: Request):
    active_threads = query_rows("""
        SELECT COUNT(*) as cnt FROM processlist_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM processlist_snapshots)
    """)
    thread_count = active_threads[0]["cnt"] if active_threads else 0
    current_locks = query_rows("""
        SELECT wait_seconds FROM lock_wait_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
    """)
    lock_count = len(current_locks)
    has_long_locks = any(l["wait_seconds"] > 10 for l in current_locks)
    long_txns = query_rows("""
        SELECT COUNT(*) as cnt FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
          AND age_sec > 60
    """)
    has_long_txns = (long_txns[0]["cnt"] if long_txns else 0) > 0
    from api.query_helpers import latest_hit_ratio_pct
    bp = {"hit_ratio": latest_hit_ratio_pct()}
    anomaly_count = _get_anomaly_count()

    if has_long_locks or lock_count > 5:
        health = "red"
    elif has_long_txns or lock_count > 0 or anomaly_count > 0:
        health = "yellow"
    else:
        health = "green"

    return request.app.state.templates.TemplateResponse(request, "partials/health_bar.html", {
        "request": request, "health": health, "thread_count": thread_count,
        "lock_count": lock_count, "bp": bp or {"hit_ratio": None},
        "anomaly_count": anomaly_count,
    })


@router.get("/dashboard/partials/active-alerts", response_class=HTMLResponse)
def partial_active_alerts(request: Request):
    # First-run detection for Phase 3.2 — show a waiting state instead of
    # "All quiet" when the monitoring DB has no query_digest_snapshots yet.
    zero_data_row = query_single("SELECT COUNT(*) as c FROM query_digest_snapshots")
    zero_data = (zero_data_row or {}).get("c", 0) == 0

    ddl_changes = query_rows("""
        SELECT detected_at, table_schema, table_name, change_type,
               SUBSTR(old_ddl, 1, 300) as old_ddl,
               SUBSTR(new_ddl, 1, 300) as new_ddl
        FROM ddl_changes
        WHERE detected_at >= datetime('now', '-24 hours')
        ORDER BY detected_at DESC LIMIT 10
    """)
    long_txns = query_rows("""
        SELECT trx_id, age_sec, trx_query, pid
        FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
          AND age_sec > 60
        ORDER BY age_sec DESC
    """)
    long_locks = query_rows("""
        SELECT waiting_pid, blocking_pid, wait_seconds, waiting_query, blocking_query
        FROM lock_wait_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
          AND wait_seconds > 10
        ORDER BY wait_seconds DESC
    """)
    anomalies = _get_anomalies()
    lock_rem = get_remediation("lock_wait")
    txn_rem = get_remediation("long_transaction")
    ddl_rem = get_remediation("ddl_change")
    anom_rem = get_remediation("anomaly")

    return request.app.state.templates.TemplateResponse(request, "partials/active_alerts.html", {
        "request": request,
        "zero_data": zero_data,
        "ddl_changes": ddl_changes, "long_txns": long_txns, "long_locks": long_locks,
        "anomalies": anomalies,
        "lock_rem": lock_rem, "txn_rem": txn_rem, "ddl_rem": ddl_rem, "anom_rem": anom_rem,
    })


@router.get("/dashboard/partials/current-locks", response_class=HTMLResponse)
def partial_current_locks(request: Request):
    current_locks = query_rows("""
        SELECT * FROM lock_wait_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
        ORDER BY wait_seconds DESC
    """)
    return request.app.state.templates.TemplateResponse(request, "partials/current_locks.html", {
        "request": request, "current_locks": current_locks,
    })


@router.get("/dashboard/partials/active-transactions", response_class=HTMLResponse)
def partial_active_transactions(request: Request):
    active_txns = query_rows("""
        SELECT * FROM transaction_snapshots
        WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
        ORDER BY age_sec DESC
    """)
    return request.app.state.templates.TemplateResponse(request, "partials/active_transactions.html", {
        "request": request, "active_txns": active_txns,
    })


@router.get("/dashboard/partials/query-detail/{digest}", response_class=HTMLResponse)
def partial_query_detail(request: Request, digest: str):
    # Grab the most recent query_sample_text (real SQL with actual parameter
    # values) via a subquery. The LEFT JOIN ensures we still return aggregate
    # stats even for digests that never captured a sample.
    query_info = query_single("""
        SELECT
            q.digest,
            q.digest_text,
            q.schema_name,
            SUM(q.exec_count) as exec_count,
            AVG(q.avg_time_sec) as avg_time_sec,
            SUM(q.total_time_sec) as total_time_sec,
            SUM(q.rows_examined) as rows_examined,
            SUM(q.rows_sent) as rows_sent,
            (SELECT query_sample_text
               FROM query_digest_snapshots
              WHERE digest = ?
                AND query_sample_text IS NOT NULL
              ORDER BY snapshot_time DESC LIMIT 1) as query_sample_text
        FROM query_digest_snapshots q
        WHERE q.digest = ?
        GROUP BY q.digest
    """, (digest, digest))

    explain = query_single("""
        SELECT explain_json, captured_at
        FROM explain_captures
        WHERE digest = ?
        ORDER BY captured_at DESC LIMIT 1
    """, (digest,))

    explain_signals = _analyze_explain(explain)

    return request.app.state.templates.TemplateResponse(request, "partials/query_detail.html", {
        "request": request,
        "query_info": query_info,
        "explain": explain,
        "explain_signals": explain_signals,
    })


# ---------------------------------------------------------------------------
# Anomaly API endpoint
# ---------------------------------------------------------------------------

@router.get("/api/v1/anomalies")
def api_anomalies():
    anomalies = _get_anomalies()
    return [
        {"metric": a.metric, "current": a.current, "baseline_mean": a.baseline_mean,
         "baseline_stddev": a.baseline_stddev, "z_score": a.z_score,
         "pct_change": a.pct_change, "direction": a.direction, "severity": a.severity}
        for a in anomalies
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_anomalies():
    try:
        from alerting.anomaly import detect_anomalies
        return detect_anomalies()
    except Exception as e:
        logger.debug(f"Anomaly detection unavailable: {e}")
        return []


def _get_anomaly_count() -> int:
    return len(_get_anomalies())


def _analyze_explain(explain: dict | None) -> list[dict]:
    """Parse EXPLAIN JSON and return human-readable signals."""
    if not explain or not explain.get("explain_json"):
        return []
    signals = []
    try:
        data = json.loads(explain["explain_json"])
    except (json.JSONDecodeError, TypeError):
        return []

    rows = []
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        qb = data.get("query_block", data)
        table = qb.get("table", {})
        if table:
            rows = [table]
        ordering = qb.get("ordering_operation", qb)
        nested = ordering.get("nested_loop", [])
        for nl in nested:
            if isinstance(nl, dict) and "table" in nl:
                rows.append(nl["table"])

    for row in rows:
        access_type = row.get("access_type", row.get("type", ""))
        table_name = row.get("table_name", row.get("table", ""))
        rows_examined = row.get("rows_examined_per_scan", row.get("rows", 0))
        key = row.get("key", row.get("used_key_parts", ""))
        using_filesort = row.get("using_filesort", False)
        using_temporary = row.get("using_temporary_table", False)

        if access_type == "ALL":
            signals.append({
                "type": "error", "icon": "&#9888;",
                "title": f"Full table scan on `{table_name}`",
                "detail": f"Examining ~{rows_examined:,} rows without index. Add index on WHERE/JOIN columns.",
            })
        elif access_type == "index":
            signals.append({
                "type": "warning", "icon": "&#128269;",
                "title": f"Full index scan on `{table_name}`",
                "detail": "Scanning entire index. Need more selective WHERE or covering index.",
            })
        elif access_type in ("range", "ref", "eq_ref", "const", "system"):
            signals.append({
                "type": "good", "icon": "&#9989;",
                "title": f"Good: `{access_type}` on `{table_name}`",
                "detail": f"Using key: {key}" if key else "Efficient access.",
            })
        if using_filesort:
            signals.append({
                "type": "warning", "icon": "&#128203;",
                "title": f"Filesort on `{table_name}`",
                "detail": "Extra sort pass. Consider index on ORDER BY columns.",
            })
        if using_temporary:
            signals.append({
                "type": "warning", "icon": "&#128203;",
                "title": f"Temp table for `{table_name}`",
                "detail": "Temp table for GROUP BY/DISTINCT. Consider covering index.",
            })
    return signals
