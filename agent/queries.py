"""
SQLite queries used by the State Builder to read monitoring data.

All reads go through get_mon_reader() — separate read-only connection
that doesn't block the writer.

Multi-server: queries use {sid_filter} placeholder which resolves to
'AND server_id = ?' or '' depending on whether server_id is provided.
The state builder passes server_id through all query calls.
"""

# --- Current State (last N minutes) ---

TOP_QUERIES_BY_TIME = """
SELECT digest, digest_text, schema_name,
       exec_count, total_time_sec, avg_time_sec, max_time_sec,
       rows_examined, rows_sent, full_scans, no_index_used,
       sum_errors, sum_warnings
FROM query_digest_snapshots
WHERE snapshot_time = (
    SELECT MAX(snapshot_time) FROM query_digest_snapshots WHERE server_id = ?
)
  AND server_id = ?
ORDER BY total_time_sec DESC
LIMIT ?
"""

TOP_QUERIES_BY_RATIO = """
SELECT digest, digest_text, schema_name,
       rows_examined, rows_sent,
       CASE WHEN rows_sent > 0 THEN CAST(rows_examined AS REAL) / rows_sent ELSE rows_examined END as ratio,
       avg_time_sec, exec_count
FROM query_digest_snapshots
WHERE snapshot_time = (
    SELECT MAX(snapshot_time) FROM query_digest_snapshots WHERE server_id = ?
)
  AND server_id = ?
  AND rows_sent > 0
ORDER BY ratio DESC
LIMIT ?
"""

CURRENT_LOCK_WAITS = """
SELECT COUNT(*) as lock_count,
       MAX(wait_seconds) as max_wait_sec,
       AVG(wait_seconds) as avg_wait_sec
FROM lock_wait_snapshots
WHERE snapshot_time >= datetime('now', ?)
  AND server_id = ?
"""

CURRENT_BUFFER_POOL = """
SELECT hit_ratio, dirty_pages, free_buffers, pool_size, database_pages
FROM buffer_pool_snapshots
WHERE snapshot_time = (
    SELECT MAX(snapshot_time) FROM buffer_pool_snapshots WHERE server_id = ?
)
  AND server_id = ?
LIMIT 1
"""

CURRENT_THREADS = """
SELECT variable_name, raw_value
FROM global_status_snapshots
WHERE snapshot_time = (
    SELECT MAX(snapshot_time) FROM global_status_snapshots WHERE server_id = ?
)
  AND server_id = ?
  AND variable_name IN ('Threads_running', 'Threads_connected')
"""

LONG_TRANSACTIONS = """
SELECT trx_id, age_sec, trx_query, pid, rows_locked, rows_modified
FROM transaction_snapshots
WHERE snapshot_time = (
    SELECT MAX(snapshot_time) FROM transaction_snapshots WHERE server_id = ?
)
  AND server_id = ?
  AND age_sec > ?
ORDER BY age_sec DESC
"""

CURRENT_GCP_METRICS = """
SELECT metric_name, value
FROM gcp_metric_snapshots
WHERE snapshot_time = (
    SELECT MAX(snapshot_time) FROM gcp_metric_snapshots WHERE server_id = ?
)
  AND server_id = ?
"""

CURRENT_QPS = """
SELECT per_second
FROM global_status_snapshots
WHERE variable_name = 'Queries'
  AND server_id = ?
ORDER BY snapshot_time DESC
LIMIT 1
"""

# --- Trend / Changes ---

RECENT_DDL_CHANGES = """
SELECT detected_at, table_schema, table_name, change_type, new_ddl
FROM ddl_changes
WHERE detected_at >= ?
  AND server_id = ?
ORDER BY detected_at DESC
"""

NEW_QUERY_FINGERPRINTS = """
SELECT DISTINCT digest, digest_text, schema_name
FROM query_digest_snapshots
WHERE snapshot_time >= datetime('now', '-1 hour')
  AND server_id = ?
  AND digest NOT IN (
    SELECT DISTINCT digest FROM query_digest_snapshots
    WHERE snapshot_time < datetime('now', '-1 hour')
      AND server_id = ?
  )
"""

QUERY_REGRESSIONS = """
WITH recent AS (
    SELECT digest, digest_text, schema_name,
           AVG(avg_time_sec) as recent_avg,
           SUM(exec_count) as recent_execs
    FROM query_digest_snapshots
    WHERE snapshot_time >= datetime('now', '-1 hour')
      AND server_id = ?
    GROUP BY digest
),
baseline AS (
    SELECT digest, AVG(avg_time_sec) as baseline_avg
    FROM query_digest_snapshots
    WHERE snapshot_time BETWEEN datetime('now', '-7 days') AND datetime('now', '-1 hour')
      AND server_id = ?
    GROUP BY digest
)
SELECT r.digest, r.digest_text, r.schema_name,
       r.recent_avg, b.baseline_avg,
       r.recent_avg / NULLIF(b.baseline_avg, 0) as regression_factor,
       r.recent_execs
FROM recent r
JOIN baseline b ON r.digest = b.digest
WHERE b.baseline_avg > 0
  AND r.recent_avg / b.baseline_avg >= ?
ORDER BY regression_factor DESC
LIMIT 20
"""

RECENT_DEADLOCKS = """
SELECT snapshot_time, parsed_json
FROM innodb_status_snapshots
WHERE section_name = 'LATEST DETECTED DEADLOCK'
  AND snapshot_time >= ?
  AND server_id = ?
ORDER BY snapshot_time DESC
LIMIT 5
"""

# --- Historical Context ---

BASELINE_THREADS_RUNNING = """
SELECT AVG(raw_value) as avg_value
FROM global_status_snapshots
WHERE variable_name = 'Threads_running'
  AND server_id = ?
  AND strftime('%w', snapshot_time) = strftime('%w', 'now', '-7 days')
  AND strftime('%H', snapshot_time) = strftime('%H', 'now')
"""

BASELINE_QPS = """
SELECT AVG(per_second) as avg_qps
FROM global_status_snapshots
WHERE variable_name = 'Queries'
  AND server_id = ?
  AND strftime('%w', snapshot_time) = strftime('%w', 'now', '-7 days')
  AND strftime('%H', snapshot_time) = strftime('%H', 'now')
"""

QUERY_30D_TREND = """
SELECT DATE(snapshot_time) as day, AVG(avg_time_sec) as daily_avg
FROM query_digest_snapshots
WHERE digest = ?
  AND server_id = ?
  AND snapshot_time >= datetime('now', '-30 days')
GROUP BY DATE(snapshot_time)
ORDER BY day ASC
"""

LAST_ANALYSIS_TIME = """
SELECT MAX(analyzed_at) as last_at
FROM agent_analyses
WHERE server_id = ?
"""

# --- For Agent Tools ---

EXPLAIN_FOR_DIGEST = """
SELECT explain_json, captured_at
FROM explain_captures
WHERE digest = ?
ORDER BY captured_at DESC
LIMIT 1
"""

SCHEMA_FOR_TABLE = """
SELECT create_stmt
FROM schema_snapshots
WHERE table_schema = ? AND table_name = ?
ORDER BY snapshot_time DESC
LIMIT 1
"""

QUERY_HISTORY = """
SELECT snapshot_time, avg_time_sec, exec_count, total_time_sec,
       rows_examined, rows_sent
FROM query_digest_snapshots
WHERE digest = ?
  AND snapshot_time >= datetime('now', ?)
ORDER BY snapshot_time ASC
"""

LOCK_GRAPH = """
SELECT waiting_trx_id, waiting_pid, waiting_query, wait_seconds,
       blocking_trx_id, blocking_pid, blocking_query,
       blocking_trx_age_sec, blocking_rows_locked
FROM lock_wait_snapshots
WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM lock_wait_snapshots)
"""

ACTIVE_TRANSACTIONS = """
SELECT trx_id, trx_state, age_sec, pid, trx_query,
       rows_locked, rows_modified, isolation_level
FROM transaction_snapshots
WHERE snapshot_time = (SELECT MAX(snapshot_time) FROM transaction_snapshots)
ORDER BY age_sec DESC
"""

# --- Recent Analyses (for dedup + continuity) ---

RECENT_ANALYSES = """
SELECT analyzed_at, analysis_type, severity, findings, recommendations
FROM agent_analyses
WHERE analyzed_at >= datetime('now', ?)
ORDER BY analyzed_at DESC
LIMIT ?
"""

# --- Richer Historical Context ---

PEAK_THREADS_24H = """
SELECT MAX(CAST(raw_value AS REAL)) as peak_threads
FROM global_status_snapshots
WHERE variable_name = 'Threads_running'
  AND server_id = ?
  AND snapshot_time >= datetime('now', '-1 day')
"""

LONGEST_LOCK_24H = """
SELECT MAX(wait_seconds) as longest_wait_sec, COUNT(*) as total_lock_events
FROM lock_wait_snapshots
WHERE server_id = ?
  AND snapshot_time >= datetime('now', '-1 day')
"""

PREVIOUS_RECOMMENDATIONS = """
SELECT analyzed_at, severity, recommendations
FROM agent_analyses
WHERE analyzed_at >= datetime('now', '-1 day')
  AND server_id = ?
  AND recommendations IS NOT NULL
  AND recommendations != '""'
  AND recommendations != ''
ORDER BY analyzed_at DESC
LIMIT 5
"""
