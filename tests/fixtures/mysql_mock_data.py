"""Realistic mock data for MySQL query results."""

from datetime import datetime

MOCK_PROCESSLIST = [
    {
        "thread_id": 100,
        "pid": 10,
        "user": "app_user",
        "db": "mydb",
        "command": "Query",
        "state": "executing",
        "time_sec": 2,
        "query": "SELECT * FROM loyalty_members WHERE user_id = 123",
    },
    {
        "thread_id": 101,
        "pid": 11,
        "user": "batch_user",
        "db": "mydb",
        "command": "Query",
        "state": "Sending data",
        "time_sec": 45,
        "query": "SELECT COUNT(*) FROM loyalty_members GROUP BY reward_tier",
    },
]

MOCK_LOCK_WAITS = [
    {
        "waiting_trx_id": "TRX_001",
        "waiting_pid": 10,
        "waiting_query": "UPDATE loyalty_members SET points = 100 WHERE user_id = 5",
        "wait_seconds": 12,
        "blocking_trx_id": "TRX_002",
        "blocking_pid": 11,
        "blocking_query": "SELECT * FROM loyalty_members FOR UPDATE",
        "blocking_trx_age_sec": 30,
        "blocking_rows_locked": 500,
        "blocking_rows_modified": 0,
    },
]

MOCK_TRANSACTIONS = [
    {
        "trx_id": "TRX_002",
        "trx_state": "RUNNING",
        "trx_started": "2025-01-01 10:00:00",
        "age_sec": 30,
        "pid": 11,
        "trx_query": "SELECT * FROM loyalty_members FOR UPDATE",
        "operation_state": "sending data",
        "tables_in_use": 1,
        "tables_locked": 1,
        "lock_structs": 5,
        "rows_locked": 500,
        "rows_modified": 0,
        "isolation_level": "REPEATABLE READ",
    },
]

MOCK_METADATA_LOCKS = [
    {
        "object_type": "TABLE",
        "object_schema": "mydb",
        "object_name": "loyalty_members",
        "lock_type": "SHARED_READ",
        "lock_duration": "TRANSACTION",
        "lock_status": "GRANTED",
        "owner_thread_id": 100,
    },
]

MOCK_QUERY_DIGESTS = [
    {
        "digest": "abc123def456",
        "digest_text": "SELECT * FROM `loyalty_members` WHERE `user_id` = ?",
        "schema_name": "mydb",
        "exec_count": 1500,
        "total_time_sec": 3.5,
        "avg_time_sec": 0.0023,
        "max_time_sec": 0.15,
        "min_time_sec": 0.0001,
        "rows_examined": 1500,
        "rows_sent": 1500,
        "rows_affected": 0,
        "tmp_tables": 0,
        "tmp_disk_tables": 0,
        "full_joins": 0,
        "full_scans": 0,
        "no_index_used": 0,
        "no_good_index_used": 0,
        "sort_merge_passes": 0,
        "sum_errors": 0,
        "sum_warnings": 2,
        "first_seen": "2025-01-01 00:00:00",
        "last_seen": "2025-01-01 12:00:00",
    },
]

MOCK_WAIT_EVENTS = [
    {
        "event_name": "wait/io/file/innodb/innodb_data_file",
        "count_star": 50000,
        "total_wait_sec": 1.234,
        "avg_wait_sec": 0.000025,
    },
]

MOCK_TABLE_IO = [
    {
        "object_schema": "mydb",
        "table_name": "loyalty_members",
        "count_read": 100000,
        "count_write": 5000,
        "count_fetch": 100000,
        "count_insert": 1000,
        "count_update": 3000,
        "count_delete": 1000,
        "total_io_sec": 5.5,
        "read_io_sec": 4.0,
        "write_io_sec": 1.5,
    },
]

MOCK_INNODB_METRICS = [
    {
        "metric_name": "buffer_pool_reads",
        "subsystem": "buffer",
        "count_value": 12345,
        "metric_type": "status_counter",
    },
]

MOCK_BUFFER_POOL = [
    {
        "pool_id": 0,
        "pool_size": 65536,
        "free_buffers": 1000,
        "database_pages": 64000,
        "dirty_pages": 100,
        "pending_reads": 0,
        "pages_read": 50000,
        "pages_written": 30000,
        "hit_ratio": 0.9923,
    },
]

MOCK_GLOBAL_STATUS = [
    {"Variable_name": "Questions", "Value": "1000"},
    {"Variable_name": "Queries", "Value": "1200"},
    {"Variable_name": "Com_select", "Value": "800"},
    {"Variable_name": "Threads_connected", "Value": "10"},
    {"Variable_name": "Threads_running", "Value": "3"},
    {"Variable_name": "Slow_queries", "Value": "5"},
    {"Variable_name": "Innodb_row_lock_waits", "Value": "2"},
    {"Variable_name": "Innodb_buffer_pool_reads", "Value": "500"},
    {"Variable_name": "Innodb_buffer_pool_read_requests", "Value": "100000"},
    {"Variable_name": "Some_untracked_var", "Value": "999"},
]

MOCK_GLOBAL_STATUS_SECOND = [
    {"Variable_name": "Questions", "Value": "2500"},
    {"Variable_name": "Queries", "Value": "2700"},
    {"Variable_name": "Com_select", "Value": "2000"},
    {"Variable_name": "Threads_connected", "Value": "12"},
    {"Variable_name": "Threads_running", "Value": "5"},
    {"Variable_name": "Slow_queries", "Value": "8"},
    {"Variable_name": "Innodb_row_lock_waits", "Value": "3"},
    {"Variable_name": "Innodb_buffer_pool_reads", "Value": "600"},
    {"Variable_name": "Innodb_buffer_pool_read_requests", "Value": "200000"},
    {"Variable_name": "Some_untracked_var", "Value": "1500"},
]

MOCK_SCHEMA_FINGERPRINT = [
    {
        "table_schema": "mydb",
        "table_name": "loyalty_members",
        "schema_hash": "abc123hash",
    },
    {
        "table_schema": "mydb",
        "table_name": "users",
        "schema_hash": "def456hash",
    },
]

MOCK_INDEX_FINGERPRINT = [
    {
        "table_schema": "mydb",
        "table_name": "loyalty_members",
        "index_hash": "idx_abc123",
    },
    {
        "table_schema": "mydb",
        "table_name": "users",
        "index_hash": "idx_def456",
    },
]

MOCK_TABLE_SIZES = [
    {
        "table_schema": "mydb",
        "table_name": "loyalty_members",
        "table_rows": 8000000,
        "data_mb": 1500.0,
        "index_mb": 400.0,
        "ENGINE": "InnoDB",
        "ROW_FORMAT": "Dynamic",
        "AUTO_INCREMENT": 8000001,
        "CREATE_TIME": "2024-01-01 00:00:00",
        "UPDATE_TIME": "2025-01-01 12:00:00",
    },
    {
        "table_schema": "mydb",
        "table_name": "users",
        "table_rows": 100000,
        "data_mb": 50.0,
        "index_mb": 10.0,
        "ENGINE": "InnoDB",
        "ROW_FORMAT": "Dynamic",
        "AUTO_INCREMENT": 100001,
        "CREATE_TIME": "2024-01-01 00:00:00",
        "UPDATE_TIME": "2025-01-01 12:00:00",
    },
]
