# Collectors

SeeQL ships 19 collectors split across three schedulers. Each collector
extends `BaseCollector` (`collectors/base.py`), implements `name`,
`collect()`, and `store()`, and runs in its own try/except so one
failure doesn't cascade.

All collector SQL lives in [`collectors/queries.py`](../collectors/queries.py)
— nothing inline. Audit-friendly by design.

## Fast loop — every 30 s (4 collectors)

"Is the server on fire right now?" checks.

| Collector | Reads | Writes to | Purpose |
|-----------|-------|-----------|---------|
| `processlist` | `performance_schema.threads` | `processlist_snapshots` | Active threads, who's running what |
| `lock_waits` | `performance_schema.data_lock_waits` | `lock_wait_snapshots` | Current InnoDB lock waits with the waiting + blocking queries |
| `transactions` | `information_schema.innodb_trx` | `active_transactions` | Long-running transactions, rows locked |
| `metadata_locks` | `performance_schema.metadata_locks` | `metadata_locks` | DDL blocking detection |

## Medium loop — every 5 min (11 collectors)

Aggregated stats; the bulk of what the LLM agent sees.

| Collector | Reads | Writes to | Purpose |
|-----------|-------|-----------|---------|
| `query_digests` | `performance_schema.events_statements_summary_by_digest` | `query_digest_snapshots` | Query fingerprints with timing/row stats — the single most important data |
| `wait_events` | `performance_schema.events_waits_summary_global_by_event_name` | `wait_event_snapshots` | What MySQL is waiting on |
| `table_io` | `performance_schema.table_io_waits_summary_by_table` | `table_io_snapshots` | Per-table read/write IO |
| `innodb_metrics` | `information_schema.INNODB_METRICS` | `innodb_metric_snapshots` | 300+ InnoDB counters |
| `buffer_pool` | `information_schema.INNODB_BUFFER_POOL_STATS` | `buffer_pool_snapshots` | Cache effectiveness |
| `global_status` | `SHOW GLOBAL STATUS` | `global_status_snapshots` | Cumulative counters → deltas for QPS / lock waits |
| `innodb_status` | `SHOW ENGINE INNODB STATUS` | `innodb_status_snapshots` | Deadlock parsing ([parsers/innodb_status.py](../parsers/innodb_status.py)) |
| `execution_stages` | `performance_schema.events_stages_summary_global_by_event_name` | `execution_stage_snapshots` | Time breakdown per execution stage |
| `explain_capture` | `EXPLAIN FORMAT=JSON <top queries>` | `explain_plans` | Auto-EXPLAIN for the top-N expensive queries |
| `gcp_metrics` (opt.) | Cloud Monitoring API | `gcp_metric_snapshots` | CPU, memory, disk, network for Cloud SQL |
| `gcp_slow_log` (opt.) | Cloud Logging API | `slow_query_samples` | Cloud SQL slow query log |

GCP collectors require the `[gcp]` extra AND a configured
`gcp.project_id` — see [E003](errors/E003.md) for fallbacks.

## Slow loop — every 30 min (4 collectors)

Schema + index analysis — rare events, heavy queries.

| Collector | Reads | Writes to | Purpose |
|-----------|-------|-----------|---------|
| `schema_snapshot` | `information_schema.COLUMNS` + `STATISTICS` + `SHOW CREATE TABLE` | `schema_snapshots`, `ddl_changes` | Schema + index fingerprints; diff against last snapshot to detect DDL changes |
| `unused_indexes` | `performance_schema.table_io_waits_summary_by_index_usage` | `unused_indexes` | Indexes with zero reads |
| `redundant_indexes` | `information_schema.STATISTICS` | `redundant_indexes` | Prefix-duplicate indexes |
| `global_variables` | `SHOW GLOBAL VARIABLES` | `global_variable_snapshots` | Config drift detection |

## Retention loop — daily

Not a collector — a maintenance job. Deletes rows older than
`retention.days` (default 90) per-table, respecting overrides:

| Table | Default retention |
|-------|-------------------|
| `incident_windows` | 365 d |
| `ddl_changes` | 365 d |
| `schema_snapshots` | 180 d |
| `anomaly_events` | 90 d |
| `agent_analyses` | 90 d |
| `lock_wait_snapshots` | 30 d |
| `processlist_snapshots` | 7 d |

Auto-shrink: when the DB exceeds `monitoring_db.max_size_mb`
(default 5 GB), retention temporarily tightens table-by-table from
highest-volume to lowest until size is under the limit.

## Permissions

All MySQL collectors run as the `dba_agent` user with:

```sql
GRANT SELECT, PROCESS ON *.* TO 'dba_agent'@'%';
```

That's it. No writes, no `SUPER`, no DBA-level access.

## Adding a new collector

1. Subclass `BaseCollector` in the appropriate loop module
   (`fast_loop.py`, `medium_loop.py`, `slow_loop.py`).
2. Implement `name`, `collect()`, and `store()`.
3. Put any SQL in `collectors/queries.py` — never inline.
4. Add the schema to `storage/schema.sql` and a writer in
   `storage/writer.py`.
5. Register the instance in the loop's `*_COLLECTORS` list.
6. Write a test in `tests/test_collectors.py`.

See `collectors/query_digests.py` for a minimal reference.
