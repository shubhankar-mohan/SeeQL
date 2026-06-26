"""
Batch writer for the monitoring database (SQLite).

All collectors produce lists of dicts. This module handles conversion
to batch INSERT statements.

SQLite-specific notes:
    - Uses ? placeholders (not %s like MySQL).
    - datetime objects are converted to ISO format strings.
    - executemany() works well for batch inserts.
    - All writes go through the thread-locked get_mon_connection().
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from storage.connection import get_mon_connection

logger = logging.getLogger(__name__)


def _serialize_value(value: Any) -> Any:
    """
    Convert Python values to SQLite-compatible types.

    SQLite doesn't have a native datetime type — we store as ISO strings.
    Decimal values (returned by MySQL) are converted to float.
    Everything else passes through as-is (int, float, str, None).
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _batch_insert(table: str, columns: list[str], rows: list[dict]) -> int:
    """
    Generic batch insert helper.

    Args:
        table:   Target table name.
        columns: List of column names (must match keys in each row dict).
        rows:    List of dicts, each containing values for the columns.

    Returns:
        Number of rows inserted.

    Note:
        For rows that don't supply a `server_id`, a 'default' value is
        injected. Production collectors always set it (via ServerContext),
        but legacy test fixtures and ad-hoc callers don't — and a NULL would
        violate the NOT NULL constraint even though the column has a DEFAULT,
        because SQLite only applies the default when the column is omitted
        from the INSERT statement.
    """
    if not rows:
        return 0

    placeholders = ", ".join(["?"] * len(columns))
    col_names = ", ".join(columns)
    sql = f"INSERT INTO {table} ({col_names}) VALUES ({placeholders})"

    def _get(row: dict, col: str):
        val = row.get(col)
        if val is None and col == "server_id":
            return "default"
        return _serialize_value(val)

    # Convert dicts to tuples in column order, serializing values.
    values = [
        tuple(_get(row, col) for col in columns)
        for row in rows
    ]

    try:
        with get_mon_connection() as conn:
            conn.executemany(sql, values)
            logger.debug(f"Inserted {len(values)} rows into {table}")
            return len(values)
    except Exception as e:
        logger.error(f"Failed to insert into {table}: {e}")
        raise


# ---------------------------------------------------------------------------
# Table-specific writers
# Each defines the exact columns it expects, so collectors have a
# clear contract of what dict keys to produce.
# ---------------------------------------------------------------------------

def write_query_digests(rows: list[dict]) -> int:
    return _batch_insert("query_digest_snapshots", [
        "server_id", "snapshot_time", "digest", "digest_text", "query_sample_text", "schema_name",
        "exec_count", "total_time_sec", "avg_time_sec", "max_time_sec", "min_time_sec",
        "rows_examined", "rows_sent", "rows_affected",
        "tmp_tables", "tmp_disk_tables", "full_joins", "full_scans",
        "no_index_used", "no_good_index_used", "sort_merge_passes",
        "sum_errors", "sum_warnings",
        "first_seen", "last_seen",
    ], rows)


def write_processlist(rows: list[dict]) -> int:
    return _batch_insert("processlist_snapshots", [
        "server_id", "snapshot_time", "thread_id", "pid", "user", "db",
        "command", "state", "time_sec", "query",
    ], rows)


def write_lock_waits(rows: list[dict]) -> int:
    return _batch_insert("lock_wait_snapshots", [
        "server_id", "snapshot_time",
        "waiting_trx_id", "waiting_pid", "waiting_query", "wait_seconds",
        "blocking_trx_id", "blocking_pid", "blocking_query",
        "blocking_trx_age_sec", "blocking_rows_locked", "blocking_rows_modified",
    ], rows)


def write_transactions(rows: list[dict]) -> int:
    return _batch_insert("transaction_snapshots", [
        "server_id", "snapshot_time", "trx_id", "trx_state", "trx_started", "age_sec",
        "pid", "trx_query", "operation_state",
        "tables_in_use", "tables_locked", "lock_structs",
        "rows_locked", "rows_modified", "isolation_level",
    ], rows)


def write_metadata_locks(rows: list[dict]) -> int:
    return _batch_insert("metadata_lock_snapshots", [
        "server_id", "snapshot_time", "object_type", "object_schema", "object_name",
        "lock_type", "lock_duration", "lock_status", "owner_thread_id",
    ], rows)


def write_global_status(rows: list[dict]) -> int:
    return _batch_insert("global_status_snapshots", [
        "server_id", "snapshot_time", "variable_name", "raw_value", "delta_value", "per_second",
    ], rows)


def write_innodb_metrics(rows: list[dict]) -> int:
    return _batch_insert("innodb_metric_snapshots", [
        "server_id", "snapshot_time", "metric_name", "subsystem", "count_value", "metric_type",
    ], rows)


def write_wait_events(rows: list[dict]) -> int:
    return _batch_insert("wait_event_snapshots", [
        "server_id", "snapshot_time", "event_name", "count_star", "total_wait_sec", "avg_wait_sec",
    ], rows)


def write_table_io(rows: list[dict]) -> int:
    return _batch_insert("table_io_snapshots", [
        "server_id", "snapshot_time", "object_schema", "table_name",
        "count_read", "count_write", "count_fetch",
        "count_insert", "count_update", "count_delete",
        "total_io_sec", "read_io_sec", "write_io_sec",
    ], rows)


def write_buffer_pool(rows: list[dict]) -> int:
    return _batch_insert("buffer_pool_snapshots", [
        "server_id", "snapshot_time", "pool_id", "pool_size", "free_buffers",
        "database_pages", "dirty_pages", "pending_reads",
        "pages_read", "pages_written", "hit_ratio",
    ], rows)


def write_schema_snapshots(rows: list[dict]) -> int:
    return _batch_insert("schema_snapshots", [
        "server_id", "snapshot_time", "table_schema", "table_name",
        "schema_hash", "index_hash", "create_stmt",
        "table_rows", "data_mb", "index_mb",
    ], rows)


def write_ddl_changes(rows: list[dict]) -> int:
    return _batch_insert("ddl_changes", [
        "detected_at", "server_id", "table_schema", "table_name", "change_type",
        "old_schema_hash", "new_schema_hash",
        "old_index_hash", "new_index_hash",
        "old_ddl", "new_ddl",
    ], rows)


def write_gcp_metrics(rows: list[dict]) -> int:
    return _batch_insert("gcp_metric_snapshots", [
        "server_id", "snapshot_time", "metric_name", "metric_type", "value", "unit",
    ], rows)


def write_slow_queries(rows: list[dict]) -> int:
    return _batch_insert("slow_query_log", [
        "server_id", "snapshot_time", "user", "host",
        "query_time_sec", "lock_time_sec",
        "rows_sent", "rows_examined", "sql_text",
    ], rows)


def write_unused_indexes(rows: list[dict]) -> int:
    return _batch_insert("unused_index_snapshots", [
        "server_id", "snapshot_time", "object_schema", "table_name", "index_name",
    ], rows)


def write_redundant_indexes(rows: list[dict]) -> int:
    return _batch_insert("redundant_index_snapshots", [
        "server_id", "snapshot_time", "table_schema", "table_name",
        "redundant_index_name", "redundant_index_columns",
        "dominant_index_name", "dominant_index_columns",
        "subpart_exists", "sql_drop_index",
    ], rows)


def write_global_variables(rows: list[dict]) -> int:
    return _batch_insert("global_variable_snapshots", [
        "server_id", "snapshot_time", "variable_name", "variable_value",
    ], rows)


def write_innodb_status(rows: list[dict]) -> int:
    return _batch_insert("innodb_status_snapshots", [
        "server_id", "snapshot_time", "section_name", "section_data", "parsed_json",
    ], rows)


def write_execution_stages(rows: list[dict]) -> int:
    return _batch_insert("execution_stage_snapshots", [
        "server_id", "snapshot_time", "stage_name", "count_star", "total_time_sec", "avg_time_sec",
    ], rows)


def write_explain_captures(rows: list[dict]) -> int:
    return _batch_insert("explain_captures", [
        "captured_at", "server_id", "digest", "digest_text", "schema_name",
        "explain_json", "total_time_sec", "avg_time_sec", "exec_count",
    ], rows)


def write_agent_analysis(rows: list[dict]) -> int:
    return _batch_insert("agent_analyses", [
        "analyzed_at", "server_id", "analysis_type", "severity", "input_summary",
        "findings", "recommendations", "applied", "outcome_notes",
    ], rows)


def write_agent_analysis_one(row: dict) -> int:
    """
    Insert a single agent_analyses row and return its lastrowid.

    Used by agent.llm_agent.run_llm_analysis so the caller can link an
    incident_windows row to its analysis via the returned id.
    """
    cols = [
        "analyzed_at", "server_id", "analysis_type", "severity", "input_summary",
        "findings", "recommendations", "applied", "outcome_notes",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT INTO agent_analyses ({col_names}) VALUES ({placeholders})"

    def _get(col):
        val = row.get(col)
        if val is None and col == "server_id":
            return "default"
        return _serialize_value(val)

    values = tuple(_get(c) for c in cols)
    with get_mon_connection() as conn:
        cursor = conn.execute(sql, values)
        return cursor.lastrowid


def write_inbound_alert(row: dict) -> int:
    """
    Insert a single inbound_alerts row and return its lastrowid.

    Used by the webhook router so the caller can reference the stored alert
    from the investigations row it creates immediately after.
    """
    cols = [
        "provider", "received_at", "server_id", "external_id",
        "alert_type", "severity", "summary", "payload",
        "signature_verified", "callback_url", "processed_at",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT INTO inbound_alerts ({col_names}) VALUES ({placeholders})"

    def _get(col: str):
        val = row.get(col)
        if val is None and col == "server_id":
            return "default"
        return _serialize_value(val)

    values = tuple(_get(c) for c in cols)
    with get_mon_connection() as conn:
        cursor = conn.execute(sql, values)
        return cursor.lastrowid


def write_investigation(row: dict) -> int:
    """
    Insert a single investigations row and return its lastrowid.

    Used when the webhook router accepts a new alert. The returned id is
    the job key used when scheduling the ad-hoc investigator job.
    """
    cols = [
        "inbound_alert_id", "incident_window_id", "server_id",
        "started_at", "ended_at", "status", "phase3_next_run_at",
        "root_cause_summary", "confidence", "analysis_id",
        "query_count_total", "abort_reason",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT INTO investigations ({col_names}) VALUES ({placeholders})"

    def _get(col: str):
        val = row.get(col)
        if val is None and col == "server_id":
            return "default"
        if val is None and col == "status":
            return "queued"
        if val is None and col == "query_count_total":
            return 0
        return _serialize_value(val)

    values = tuple(_get(c) for c in cols)
    with get_mon_connection() as conn:
        cursor = conn.execute(sql, values)
        return cursor.lastrowid


def update_investigation(investigation_id: int, **fields) -> int:
    """
    Update selected fields on an investigations row.

    Only known columns are updated; unknown keys are silently ignored so
    callers can pass a free-form dict without worrying about typos blowing
    up at runtime. Returns rowcount (0 or 1).
    """
    allowed = {
        "incident_window_id", "ended_at", "status", "phase3_next_run_at",
        "root_cause_summary", "confidence", "analysis_id",
        "query_count_total", "abort_reason",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return 0

    set_clause = ", ".join(f"{k} = ?" for k in updates)
    sql = f"UPDATE investigations SET {set_clause} WHERE id = ?"
    values = tuple(_serialize_value(updates[k]) for k in updates) + (investigation_id,)

    with get_mon_connection() as conn:
        cursor = conn.execute(sql, values)
        return cursor.rowcount


def write_investigation_samples(rows: list[dict]) -> int:
    return _batch_insert("investigation_samples", [
        "investigation_id", "sampled_at", "sample_type", "query_count", "data",
    ], rows)


def write_investigation_findings(rows: list[dict]) -> int:
    return _batch_insert("investigation_findings", [
        "investigation_id", "created_at", "phase", "kind", "severity", "content",
    ], rows)


def write_anomaly_events(rows: list[dict]) -> list[int]:
    """
    Insert anomaly_events rows one at a time so the caller can track each
    row's `lastrowid` — needed by `alerting.incidents.update_windows` to set
    `incident_id` on exactly the rows it just grouped.

    Returns:
        List of inserted row IDs in the same order as `rows`.
    """
    if not rows:
        return []

    cols = [
        "detected_at", "server_id", "metric_name", "current_value",
        "baseline_mean", "baseline_stddev", "z_score", "pct_change",
        "direction", "severity", "incident_id",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    col_names = ", ".join(cols)
    sql = f"INSERT INTO anomaly_events ({col_names}) VALUES ({placeholders})"

    def _get(row: dict, col: str):
        val = row.get(col)
        if val is None and col == "server_id":
            return "default"
        return _serialize_value(val)

    ids: list[int] = []
    with get_mon_connection() as conn:
        for row in rows:
            values = tuple(_get(row, col) for col in cols)
            cursor = conn.execute(sql, values)
            ids.append(cursor.lastrowid)
    return ids
