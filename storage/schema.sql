-- =============================================================================
-- SeeQL — Monitoring Database Schema (SQLite)
-- =============================================================================
-- Run via: python main.py init-db  (or the legacy `python main.py --init-db`)
--
-- This file is the canonical schema for a FRESH install. All snapshot tables
-- include `server_id` natively. For upgrades from older SeeQL installs that
-- don't have server_id columns, `storage/migrations.py` adds them via ALTER
-- TABLE. The migration is idempotent and a no-op when this schema is used.
--
-- SQLite differences from MySQL:
--   - INTEGER PRIMARY KEY = auto-increment
--   - No ENUM → TEXT
--   - Indexes created separately (not inline)
--   - DATETIME stored as TEXT (ISO format)
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. Query Digest Snapshots
--    Stores periodic snapshots of performance_schema digest stats.
--    This is the most important table — tracks query performance over time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_digest_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',

    digest          TEXT NOT NULL,
    digest_text     TEXT,
    query_sample_text TEXT,              -- Real SQL with actual values (from QUERY_SAMPLE_TEXT)
    schema_name     TEXT,

    exec_count      INTEGER NOT NULL DEFAULT 0,
    total_time_sec  REAL    NOT NULL DEFAULT 0,
    avg_time_sec    REAL    NOT NULL DEFAULT 0,
    max_time_sec    REAL    NOT NULL DEFAULT 0,
    min_time_sec    REAL    NOT NULL DEFAULT 0,

    rows_examined   INTEGER NOT NULL DEFAULT 0,
    rows_sent       INTEGER NOT NULL DEFAULT 0,
    rows_affected   INTEGER NOT NULL DEFAULT 0,

    tmp_tables          INTEGER NOT NULL DEFAULT 0,
    tmp_disk_tables     INTEGER NOT NULL DEFAULT 0,
    full_joins          INTEGER NOT NULL DEFAULT 0,
    full_scans          INTEGER NOT NULL DEFAULT 0,
    no_index_used       INTEGER NOT NULL DEFAULT 0,
    no_good_index_used  INTEGER NOT NULL DEFAULT 0,
    sort_merge_passes   INTEGER NOT NULL DEFAULT 0,
    sum_errors          INTEGER NOT NULL DEFAULT 0,
    sum_warnings        INTEGER NOT NULL DEFAULT 0,

    first_seen      TEXT,
    last_seen       TEXT
);

CREATE INDEX IF NOT EXISTS idx_qds_time ON query_digest_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_qds_digest_time ON query_digest_snapshots(digest, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_qds_total_time ON query_digest_snapshots(snapshot_time, total_time_sec);
CREATE INDEX IF NOT EXISTS idx_qds_sid_time ON query_digest_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 2. Processlist Snapshots
--    Active (non-sleeping) queries every 30 seconds.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processlist_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    thread_id       INTEGER,
    pid             INTEGER,
    user            TEXT,
    db              TEXT,
    command         TEXT,
    state           TEXT,
    time_sec        INTEGER NOT NULL DEFAULT 0,
    query           TEXT
);

CREATE INDEX IF NOT EXISTS idx_ps_time ON processlist_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ps_time_sec ON processlist_snapshots(snapshot_time, time_sec);
CREATE INDEX IF NOT EXISTS idx_ps_sid_time ON processlist_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 3. Lock Wait Snapshots
--    InnoDB lock waits — who is blocking whom.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lock_wait_snapshots (
    id                      INTEGER PRIMARY KEY,
    snapshot_time           TEXT NOT NULL,
    server_id               TEXT NOT NULL DEFAULT 'default',

    waiting_trx_id          TEXT,
    waiting_pid             INTEGER,
    waiting_query           TEXT,
    wait_seconds            INTEGER NOT NULL DEFAULT 0,

    blocking_trx_id         TEXT,
    blocking_pid            INTEGER,
    blocking_query          TEXT,
    blocking_trx_age_sec    INTEGER NOT NULL DEFAULT 0,
    blocking_rows_locked    INTEGER NOT NULL DEFAULT 0,
    blocking_rows_modified  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_lws_time ON lock_wait_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_lws_sid_time ON lock_wait_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 4. Active Transaction Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transaction_snapshots (
    id                  INTEGER PRIMARY KEY,
    snapshot_time       TEXT NOT NULL,
    server_id           TEXT NOT NULL DEFAULT 'default',
    trx_id              TEXT,
    trx_state           TEXT,
    trx_started         TEXT,
    age_sec             INTEGER NOT NULL DEFAULT 0,
    pid                 INTEGER,
    trx_query           TEXT,
    operation_state     TEXT,
    tables_in_use       INTEGER NOT NULL DEFAULT 0,
    tables_locked       INTEGER NOT NULL DEFAULT 0,
    lock_structs        INTEGER NOT NULL DEFAULT 0,
    rows_locked         INTEGER NOT NULL DEFAULT 0,
    rows_modified       INTEGER NOT NULL DEFAULT 0,
    isolation_level     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ts_time ON transaction_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ts_age ON transaction_snapshots(snapshot_time, age_sec);
CREATE INDEX IF NOT EXISTS idx_ts_sid_time ON transaction_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 5. Metadata Lock Snapshots
--    Critical for detecting DDL blocking.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metadata_lock_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    object_type     TEXT,
    object_schema   TEXT,
    object_name     TEXT,
    lock_type       TEXT,
    lock_duration   TEXT,
    lock_status     TEXT,
    owner_thread_id INTEGER
);

CREATE INDEX IF NOT EXISTS idx_mls_time ON metadata_lock_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_mls_sid_time ON metadata_lock_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 6. Global Status Snapshots (Deltas)
--    Computed deltas for key server counters.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS global_status_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    variable_name   TEXT NOT NULL,
    raw_value       INTEGER NOT NULL DEFAULT 0,
    delta_value     INTEGER,
    per_second      REAL
);

CREATE INDEX IF NOT EXISTS idx_gss_time ON global_status_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_gss_var_time ON global_status_snapshots(variable_name, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_gss_sid_time ON global_status_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 7. InnoDB Metrics Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS innodb_metric_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    metric_name     TEXT NOT NULL,
    subsystem       TEXT,
    count_value     INTEGER NOT NULL DEFAULT 0,
    metric_type     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ims_time ON innodb_metric_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ims_name_time ON innodb_metric_snapshots(metric_name, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ims_sid_time ON innodb_metric_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 8. Wait Event Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS wait_event_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    event_name      TEXT NOT NULL,
    count_star      INTEGER NOT NULL DEFAULT 0,
    total_wait_sec  REAL NOT NULL DEFAULT 0,
    avg_wait_sec    REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wes_time ON wait_event_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_wes_sid_time ON wait_event_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 9. Table IO Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS table_io_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    object_schema   TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    count_read      INTEGER NOT NULL DEFAULT 0,
    count_write     INTEGER NOT NULL DEFAULT 0,
    count_fetch     INTEGER NOT NULL DEFAULT 0,
    count_insert    INTEGER NOT NULL DEFAULT 0,
    count_update    INTEGER NOT NULL DEFAULT 0,
    count_delete    INTEGER NOT NULL DEFAULT 0,
    total_io_sec    REAL NOT NULL DEFAULT 0,
    read_io_sec     REAL NOT NULL DEFAULT 0,
    write_io_sec    REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tis_time ON table_io_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_tis_sid_time ON table_io_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 10. Schema Snapshots (DDL change detection)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    table_schema    TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    schema_hash     TEXT NOT NULL,
    index_hash      TEXT NOT NULL,
    create_stmt     TEXT,
    table_rows      INTEGER NOT NULL DEFAULT 0,
    data_mb         REAL NOT NULL DEFAULT 0,
    index_mb        REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ss_table_time ON schema_snapshots(table_schema, table_name, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ss_time ON schema_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ss_sid_time ON schema_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 11. DDL Change Log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ddl_changes (
    id              INTEGER PRIMARY KEY,
    detected_at     TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    table_schema    TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    change_type     TEXT NOT NULL,  -- 'schema', 'index', or 'both'
    old_schema_hash TEXT,
    new_schema_hash TEXT,
    old_index_hash  TEXT,
    new_index_hash  TEXT,
    old_ddl         TEXT,
    new_ddl         TEXT
);

CREATE INDEX IF NOT EXISTS idx_dc_detected ON ddl_changes(detected_at);
CREATE INDEX IF NOT EXISTS idx_dc_table ON ddl_changes(table_schema, table_name);
CREATE INDEX IF NOT EXISTS idx_dc_sid_time ON ddl_changes(server_id, detected_at);


-- ---------------------------------------------------------------------------
-- 12. InnoDB Buffer Pool Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS buffer_pool_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    pool_id         INTEGER NOT NULL DEFAULT 0,
    pool_size       INTEGER NOT NULL DEFAULT 0,
    free_buffers    INTEGER NOT NULL DEFAULT 0,
    database_pages  INTEGER NOT NULL DEFAULT 0,
    dirty_pages     INTEGER NOT NULL DEFAULT 0,
    pending_reads   INTEGER NOT NULL DEFAULT 0,
    pages_read      INTEGER NOT NULL DEFAULT 0,
    pages_written   INTEGER NOT NULL DEFAULT 0,
    hit_ratio       REAL  -- NOTE: stale/unreliable in MySQL 8.0 (HIT_RATE is an instantaneous sample).
                          -- See 0.5 — API reads cumulative ratio from global_status_snapshots instead.
);

CREATE INDEX IF NOT EXISTS idx_bps_time ON buffer_pool_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_bps_sid_time ON buffer_pool_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 13. Agent Analysis Log (Week 3+ — LLM agent output)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_analyses (
    id                  INTEGER PRIMARY KEY,
    analyzed_at         TEXT NOT NULL,
    server_id           TEXT NOT NULL DEFAULT 'default',
    analysis_type       TEXT NOT NULL,
    severity            TEXT NOT NULL,
    input_summary       TEXT,
    findings            TEXT,      -- JSON string
    recommendations     TEXT,      -- JSON string
    applied             INTEGER NOT NULL DEFAULT 0,
    applied_at          TEXT,
    outcome_notes       TEXT
);

CREATE INDEX IF NOT EXISTS idx_aa_time_type ON agent_analyses(analyzed_at, analysis_type);
CREATE INDEX IF NOT EXISTS idx_aa_severity ON agent_analyses(severity, analyzed_at);
CREATE INDEX IF NOT EXISTS idx_aa_sid_time ON agent_analyses(server_id, analyzed_at);


-- ---------------------------------------------------------------------------
-- 14. GCP Cloud Monitoring Metrics
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gcp_metric_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    metric_name     TEXT NOT NULL,
    metric_type     TEXT NOT NULL,
    value           REAL,
    unit            TEXT
);

CREATE INDEX IF NOT EXISTS idx_gms_time ON gcp_metric_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_gms_name_time ON gcp_metric_snapshots(metric_name, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_gms_sid_time ON gcp_metric_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 15. GCP Slow Query Logs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS slow_query_log (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    user            TEXT,
    host            TEXT,
    query_time_sec  REAL NOT NULL DEFAULT 0,
    lock_time_sec   REAL NOT NULL DEFAULT 0,
    rows_sent       INTEGER NOT NULL DEFAULT 0,
    rows_examined   INTEGER NOT NULL DEFAULT 0,
    sql_text        TEXT
);

CREATE INDEX IF NOT EXISTS idx_sql_time ON slow_query_log(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_sql_query_time ON slow_query_log(query_time_sec);
CREATE INDEX IF NOT EXISTS idx_sql_sid_time ON slow_query_log(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 16. Unused Indexes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS unused_index_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    object_schema   TEXT NOT NULL,
    table_name      TEXT NOT NULL,
    index_name      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_uis_time ON unused_index_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_uis_sid_time ON unused_index_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 17. Redundant Indexes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS redundant_index_snapshots (
    id                      INTEGER PRIMARY KEY,
    snapshot_time           TEXT NOT NULL,
    server_id               TEXT NOT NULL DEFAULT 'default',
    table_schema            TEXT NOT NULL,
    table_name              TEXT NOT NULL,
    redundant_index_name    TEXT NOT NULL,
    redundant_index_columns TEXT,
    dominant_index_name     TEXT,
    dominant_index_columns  TEXT,
    subpart_exists          INTEGER NOT NULL DEFAULT 0,
    sql_drop_index          TEXT
);

CREATE INDEX IF NOT EXISTS idx_ris_time ON redundant_index_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ris_sid_time ON redundant_index_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 18. Global Variables Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS global_variable_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    variable_name   TEXT NOT NULL,
    variable_value  TEXT
);

CREATE INDEX IF NOT EXISTS idx_gvs_time ON global_variable_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_gvs_var_time ON global_variable_snapshots(variable_name, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_gvs_sid_time ON global_variable_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 19. InnoDB Status Snapshots (parsed sections)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS innodb_status_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    section_name    TEXT NOT NULL,
    section_data    TEXT,
    parsed_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_iss_time ON innodb_status_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_iss_section ON innodb_status_snapshots(section_name, snapshot_time);
CREATE INDEX IF NOT EXISTS idx_iss_sid_time ON innodb_status_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 20. Execution Stage Snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_stage_snapshots (
    id              INTEGER PRIMARY KEY,
    snapshot_time   TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    stage_name      TEXT NOT NULL,
    count_star      INTEGER NOT NULL DEFAULT 0,
    total_time_sec  REAL NOT NULL DEFAULT 0,
    avg_time_sec    REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ess_time ON execution_stage_snapshots(snapshot_time);
CREATE INDEX IF NOT EXISTS idx_ess_sid_time ON execution_stage_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 21. EXPLAIN Plan Captures
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS explain_captures (
    id              INTEGER PRIMARY KEY,
    captured_at     TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    digest          TEXT NOT NULL,
    digest_text     TEXT,
    schema_name     TEXT,
    explain_json    TEXT,
    total_time_sec  REAL,
    avg_time_sec    REAL,
    exec_count      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_ec_time ON explain_captures(captured_at);
CREATE INDEX IF NOT EXISTS idx_ec_digest ON explain_captures(digest, captured_at);
CREATE INDEX IF NOT EXISTS idx_ec_sid_time ON explain_captures(server_id, captured_at);


-- ---------------------------------------------------------------------------
-- 22. Alert History
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alert_history (
    id              INTEGER PRIMARY KEY,
    fired_at        TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    rule_name       TEXT NOT NULL,
    severity        TEXT NOT NULL,
    message         TEXT,
    context_json    TEXT,
    channel         TEXT,
    delivered       INTEGER NOT NULL DEFAULT 0,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_ah_fired ON alert_history(fired_at);
CREATE INDEX IF NOT EXISTS idx_ah_rule ON alert_history(rule_name, fired_at);
CREATE INDEX IF NOT EXISTS idx_ah_sid_time ON alert_history(server_id, fired_at);


-- ---------------------------------------------------------------------------
-- 23. Server Registry
--     Tracks all monitored MySQL servers with their roles and tags.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS servers (
    server_id       TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    environment     TEXT NOT NULL DEFAULT 'production',
    role            TEXT NOT NULL DEFAULT 'primary',  -- 'primary' or 'replica'
    cluster_id      TEXT,                             -- Groups primary + replicas
    tags            TEXT,                             -- JSON string
    host            TEXT,
    port            INTEGER DEFAULT 3306,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);


-- ---------------------------------------------------------------------------
-- 24. Replication Lag Snapshots
--     Tracks lag between primary and replicas over time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS replication_lag_snapshots (
    id              INTEGER PRIMARY KEY,
    server_id       TEXT NOT NULL,
    snapshot_time   TEXT NOT NULL,
    lag_seconds     REAL,
    source_server_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_rls_server_time ON replication_lag_snapshots(server_id, snapshot_time);


-- ---------------------------------------------------------------------------
-- 25. Anomaly Events (Phase 1.1)
--     Every individual anomaly detected by alerting/anomaly.py. The foundation
--     for incident windowing and replay. `incident_id` is set by
--     alerting/incidents.py after grouping.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS anomaly_events (
    id              INTEGER PRIMARY KEY,
    detected_at     TEXT NOT NULL,
    server_id       TEXT NOT NULL DEFAULT 'default',
    metric_name     TEXT NOT NULL,
    current_value   REAL NOT NULL,
    baseline_mean   REAL NOT NULL,
    baseline_stddev REAL NOT NULL,
    z_score         REAL NOT NULL,
    pct_change      REAL,
    direction       TEXT NOT NULL,          -- 'high' or 'low'
    severity        TEXT NOT NULL,          -- 'warning' or 'critical'
    incident_id     INTEGER                 -- FK → incident_windows, set by grouping
);

CREATE INDEX IF NOT EXISTS idx_anomaly_detected_at ON anomaly_events(detected_at);
CREATE INDEX IF NOT EXISTS idx_anomaly_incident    ON anomaly_events(incident_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_server      ON anomaly_events(server_id, detected_at);


-- ---------------------------------------------------------------------------
-- 26. Incident Windows (Phase 1.1)
--     Groups of related anomaly events. Built by gap-based clustering with a
--     max-duration cap. Each incident gets an optional LLM root-cause analysis.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incident_windows (
    id               INTEGER PRIMARY KEY,
    server_id        TEXT NOT NULL DEFAULT 'default',
    start_time       TEXT NOT NULL,
    end_time         TEXT NOT NULL,
    severity         TEXT NOT NULL,          -- max severity of constituent events
    involved_metrics TEXT NOT NULL,          -- JSON array
    event_count      INTEGER NOT NULL DEFAULT 0,
    analysis_id      INTEGER,                -- FK → agent_analyses
    status           TEXT DEFAULT 'detected' -- detected | analyzed | resolved
);

CREATE INDEX IF NOT EXISTS idx_incident_status ON incident_windows(status, start_time);
CREATE INDEX IF NOT EXISTS idx_incident_server ON incident_windows(server_id, start_time);
