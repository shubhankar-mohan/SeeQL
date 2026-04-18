
"""
SQL Queries for MySQL DBA Agent collectors.

All SQL lives in this one file for easy review and audit.
Target: MySQL 8.0.43 on GCP Cloud SQL.

Queries that need dynamic schema exclusion use Python .format()
for the IN clause. The excluded schemas come from config, not user
input, so this is safe.
"""

# =============================================================================
# FAST LOOP QUERIES (every 30 seconds)
# =============================================================================

ACTIVE_PROCESSLIST = """
SELECT
    THREAD_ID                       AS thread_id,
    PROCESSLIST_ID                  AS pid,
    PROCESSLIST_USER                AS user,
    PROCESSLIST_DB                  AS db,
    PROCESSLIST_COMMAND             AS command,
    PROCESSLIST_STATE               AS state,
    PROCESSLIST_TIME                AS time_sec,
    LEFT(PROCESSLIST_INFO, {max_query_len}) AS query
FROM performance_schema.threads
WHERE PROCESSLIST_COMMAND != 'Sleep'
  AND PROCESSLIST_COMMAND != 'Daemon'
  AND PROCESSLIST_INFO IS NOT NULL
  AND TYPE = 'FOREGROUND'
ORDER BY PROCESSLIST_TIME DESC
"""

# MySQL 8.0+ lock waits via performance_schema.data_lock_waits.
LOCK_WAITS = """
SELECT
    r.trx_id                        AS waiting_trx_id,
    r.trx_mysql_thread_id           AS waiting_pid,
    LEFT(r.trx_query, 500)          AS waiting_query,
    TIMESTAMPDIFF(SECOND, r.trx_wait_started, NOW()) AS wait_seconds,
    b.trx_id                        AS blocking_trx_id,
    b.trx_mysql_thread_id           AS blocking_pid,
    LEFT(b.trx_query, 500)          AS blocking_query,
    TIMESTAMPDIFF(SECOND, b.trx_started, NOW())      AS blocking_trx_age_sec,
    b.trx_rows_locked               AS blocking_rows_locked,
    b.trx_rows_modified             AS blocking_rows_modified
FROM performance_schema.data_lock_waits w
JOIN information_schema.innodb_trx r ON r.trx_id = w.REQUESTING_ENGINE_TRANSACTION_ID
JOIN information_schema.innodb_trx b ON b.trx_id = w.BLOCKING_ENGINE_TRANSACTION_ID
"""

ACTIVE_TRANSACTIONS = """
SELECT
    trx_id                          AS trx_id,
    trx_state                       AS trx_state,
    trx_started                     AS trx_started,
    TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_sec,
    trx_mysql_thread_id             AS pid,
    LEFT(trx_query, 500)            AS trx_query,
    trx_operation_state             AS operation_state,
    trx_tables_in_use               AS tables_in_use,
    trx_tables_locked               AS tables_locked,
    trx_lock_structs                AS lock_structs,
    trx_rows_locked                 AS rows_locked,
    trx_rows_modified               AS rows_modified,
    trx_isolation_level             AS isolation_level
FROM information_schema.innodb_trx
ORDER BY trx_started ASC
"""

METADATA_LOCKS = """
SELECT
    OBJECT_TYPE                     AS object_type,
    OBJECT_SCHEMA                   AS object_schema,
    OBJECT_NAME                     AS object_name,
    LOCK_TYPE                       AS lock_type,
    LOCK_DURATION                   AS lock_duration,
    LOCK_STATUS                     AS lock_status,
    OWNER_THREAD_ID                 AS owner_thread_id
FROM performance_schema.metadata_locks
WHERE OBJECT_SCHEMA NOT IN ({excluded_schemas})
ORDER BY OBJECT_SCHEMA, OBJECT_NAME
"""

# =============================================================================
# MEDIUM LOOP QUERIES (every 5 minutes)
# =============================================================================

QUERY_DIGESTS = """
SELECT
    DIGEST                          AS digest,
    LEFT(DIGEST_TEXT, {max_digest_len}) AS digest_text,
    LEFT(QUERY_SAMPLE_TEXT, 2000)   AS query_sample_text,
    SCHEMA_NAME                     AS schema_name,
    COUNT_STAR                      AS exec_count,
    ROUND(SUM_TIMER_WAIT  / 1e12, 6)  AS total_time_sec,
    ROUND(AVG_TIMER_WAIT  / 1e12, 6)  AS avg_time_sec,
    ROUND(MAX_TIMER_WAIT  / 1e12, 6)  AS max_time_sec,
    ROUND(MIN_TIMER_WAIT  / 1e12, 6)  AS min_time_sec,
    SUM_ROWS_EXAMINED               AS rows_examined,
    SUM_ROWS_SENT                   AS rows_sent,
    SUM_ROWS_AFFECTED               AS rows_affected,
    SUM_CREATED_TMP_TABLES          AS tmp_tables,
    SUM_CREATED_TMP_DISK_TABLES     AS tmp_disk_tables,
    SUM_SELECT_FULL_JOIN            AS full_joins,
    SUM_SELECT_SCAN                 AS full_scans,
    SUM_NO_INDEX_USED               AS no_index_used,
    SUM_NO_GOOD_INDEX_USED          AS no_good_index_used,
    SUM_SORT_MERGE_PASSES           AS sort_merge_passes,
    SUM_ERRORS                      AS sum_errors,
    SUM_WARNINGS                    AS sum_warnings,
    FIRST_SEEN                      AS first_seen,
    LAST_SEEN                       AS last_seen
FROM performance_schema.events_statements_summary_by_digest
WHERE SCHEMA_NAME NOT IN ({excluded_schemas})
  AND DIGEST IS NOT NULL
ORDER BY SUM_TIMER_WAIT DESC
LIMIT {limit}
"""

WAIT_EVENTS = """
SELECT
    EVENT_NAME                      AS event_name,
    COUNT_STAR                      AS count_star,
    ROUND(SUM_TIMER_WAIT / 1e12, 6)   AS total_wait_sec,
    ROUND(AVG_TIMER_WAIT / 1e12, 6)   AS avg_wait_sec
FROM performance_schema.events_waits_summary_global_by_event_name
WHERE COUNT_STAR > 0
  AND EVENT_NAME NOT LIKE 'idle%%'
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 30
"""

TABLE_IO = """
SELECT
    OBJECT_SCHEMA                   AS object_schema,
    OBJECT_NAME                     AS table_name,
    COUNT_READ                      AS count_read,
    COUNT_WRITE                     AS count_write,
    COUNT_FETCH                     AS count_fetch,
    COUNT_INSERT                    AS count_insert,
    COUNT_UPDATE                    AS count_update,
    COUNT_DELETE                    AS count_delete,
    ROUND(SUM_TIMER_WAIT  / 1e12, 6)  AS total_io_sec,
    ROUND(SUM_TIMER_READ  / 1e12, 6)  AS read_io_sec,
    ROUND(SUM_TIMER_WRITE / 1e12, 6)  AS write_io_sec
FROM performance_schema.table_io_waits_summary_by_table
WHERE OBJECT_SCHEMA NOT IN ({excluded_schemas})
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 30
"""

INNODB_METRICS = """
SELECT
    NAME                            AS metric_name,
    SUBSYSTEM                       AS subsystem,
    COUNT                           AS count_value,
    TYPE                            AS metric_type
FROM information_schema.INNODB_METRICS
WHERE STATUS = 'enabled'
  AND COUNT > 0
ORDER BY SUBSYSTEM, NAME
"""

BUFFER_POOL_STATS = """
SELECT
    POOL_ID                         AS pool_id,
    POOL_SIZE                       AS pool_size,
    FREE_BUFFERS                    AS free_buffers,
    DATABASE_PAGES                  AS database_pages,
    MODIFIED_DATABASE_PAGES         AS dirty_pages,
    PENDING_READS                   AS pending_reads,
    NUMBER_PAGES_READ               AS pages_read,
    NUMBER_PAGES_WRITTEN            AS pages_written,
    HIT_RATE / 1000.0               AS hit_ratio
FROM information_schema.INNODB_BUFFER_POOL_STATS
"""

GLOBAL_STATUS = "SHOW GLOBAL STATUS"

# =============================================================================
# SLOW LOOP QUERIES (every 30 minutes)
# =============================================================================

TABLE_SIZES = """
SELECT
    TABLE_SCHEMA                    AS table_schema,
    TABLE_NAME                      AS table_name,
    TABLE_ROWS                      AS table_rows,
    ROUND(DATA_LENGTH  / 1024 / 1024, 2) AS data_mb,
    ROUND(INDEX_LENGTH / 1024 / 1024, 2) AS index_mb,
    ENGINE,
    ROW_FORMAT,
    AUTO_INCREMENT,
    CREATE_TIME,
    UPDATE_TIME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA NOT IN ({excluded_schemas})
  AND TABLE_TYPE = 'BASE TABLE'
ORDER BY DATA_LENGTH + INDEX_LENGTH DESC
"""

SCHEMA_FINGERPRINT = """
SELECT
    TABLE_SCHEMA                    AS table_schema,
    TABLE_NAME                      AS table_name,
    MD5(CONCAT_WS('|',
        GROUP_CONCAT(COLUMN_NAME    ORDER BY ORDINAL_POSITION),
        GROUP_CONCAT(COLUMN_TYPE    ORDER BY ORDINAL_POSITION),
        GROUP_CONCAT(IFNULL(COLUMN_KEY, '') ORDER BY ORDINAL_POSITION),
        GROUP_CONCAT(IS_NULLABLE    ORDER BY ORDINAL_POSITION)
    ))                              AS schema_hash
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA NOT IN ({excluded_schemas})
GROUP BY TABLE_SCHEMA, TABLE_NAME
"""

INDEX_FINGERPRINT = """
SELECT
    TABLE_SCHEMA                    AS table_schema,
    TABLE_NAME                      AS table_name,
    MD5(GROUP_CONCAT(
        CONCAT(INDEX_NAME, ':', COLUMN_NAME, ':', SEQ_IN_INDEX, ':', NON_UNIQUE)
        ORDER BY INDEX_NAME, SEQ_IN_INDEX
    ))                              AS index_hash
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA NOT IN ({excluded_schemas})
GROUP BY TABLE_SCHEMA, TABLE_NAME
"""

SHOW_CREATE_TABLE = "SHOW CREATE TABLE `{schema}`.`{table}`"

UNUSED_INDEXES = """
SELECT
    object_schema,
    object_name                     AS table_name,
    index_name
FROM sys.schema_unused_indexes
WHERE object_schema NOT IN ({excluded_schemas})
"""

REDUNDANT_INDEXES = """
SELECT
    table_schema,
    table_name,
    redundant_index_name,
    redundant_index_columns,
    dominant_index_name,
    dominant_index_columns,
    subpart_exists,
    sql_drop_index
FROM sys.schema_redundant_indexes
WHERE table_schema NOT IN ({excluded_schemas})
"""

# =============================================================================
# GLOBAL VARIABLES (slow loop — config rarely changes)
# =============================================================================

GLOBAL_VARIABLES = "SHOW GLOBAL VARIABLES"

# Curated list of variables worth tracking for the LLM agent
TRACKED_VARIABLES = [
    "innodb_buffer_pool_size",
    "innodb_buffer_pool_instances",
    "innodb_log_file_size",
    "innodb_flush_log_at_trx_commit",
    "innodb_io_capacity",
    "innodb_io_capacity_max",
    "innodb_read_io_threads",
    "innodb_write_io_threads",
    "innodb_thread_concurrency",
    "innodb_lock_wait_timeout",
    "innodb_deadlock_detect",
    "innodb_adaptive_hash_index",
    "innodb_change_buffering",
    "innodb_flush_method",
    "innodb_doublewrite",
    "innodb_file_per_table",
    "max_connections",
    "max_connect_errors",
    "thread_cache_size",
    "table_open_cache",
    "table_open_cache_instances",
    "tmp_table_size",
    "max_heap_table_size",
    "sort_buffer_size",
    "join_buffer_size",
    "read_buffer_size",
    "read_rnd_buffer_size",
    "query_cache_type",
    "query_cache_size",
    "long_query_time",
    "slow_query_log",
    "performance_schema",
    "wait_timeout",
    "interactive_timeout",
    "net_read_timeout",
    "net_write_timeout",
    "lock_wait_timeout",
    "tx_isolation",
    "transaction_isolation",
    "binlog_format",
    "sync_binlog",
    "character_set_server",
    "collation_server",
    "optimizer_switch",
]

# =============================================================================
# INNODB STATUS (medium loop — parse for deadlocks, semaphores)
# =============================================================================

INNODB_STATUS = "SHOW ENGINE INNODB STATUS"

# =============================================================================
# EXECUTION STAGES (medium loop — where query time is spent)
# =============================================================================

EXECUTION_STAGES = """
SELECT
    EVENT_NAME                      AS stage_name,
    COUNT_STAR                      AS count_star,
    ROUND(SUM_TIMER_WAIT / 1e12, 6)   AS total_time_sec,
    ROUND(AVG_TIMER_WAIT / 1e12, 6)   AS avg_time_sec
FROM performance_schema.events_stages_summary_global_by_event_name
WHERE COUNT_STAR > 0
ORDER BY SUM_TIMER_WAIT DESC
LIMIT 30
"""

# =============================================================================
# AUTO-EXPLAIN (medium loop — capture EXPLAIN for expensive queries)
# =============================================================================

EXPLAIN_PREFIX = "EXPLAIN FORMAT=JSON "
