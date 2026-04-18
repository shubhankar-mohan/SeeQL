# System architecture

SeeQL is a read-only observer: collectors pull from the target MySQL
(and optionally cloud-provider APIs), persist into a local SQLite DB,
and feed a pre-processed "state report" to an LLM that reasons about
what's happening.

```
┌─────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
│  Target MySQL   │◄────│   Collectors (19)    │────►│  SQLite (26     │
│  8.0+           │     │  Fast  /  Medium  /  │     │  tables, WAL)   │
│                 │     │  Slow  schedulers    │     │                 │
└─────────────────┘     └──────────────────────┘     └────────┬────────┘
                                                              │
                                                              │
                           ┌──────────────────────┐           │
                           │  Cloud APIs (opt.)   │           │
                           │  Cloud Monitoring /  │───────────┤
                           │  Cloud Logging       │           │
                           └──────────────────────┘           │
                                                              │
                                                              ▼
                           ┌──────────────────────┐     ┌──────────────┐
                           │  State Builder       │────►│  LLM Agent   │
                           │  (pre-processed      │     │  Claude /    │
                           │   narrative report)  │     │  Gemini      │
                           └──────────────────────┘     └──────┬───────┘
                                     ▲                        │
                                     │                        ▼
┌──────────────────────┐     ┌───────┴──────┐          ┌──────────────┐
│  Alerting Engine     │────►│  Channels:   │          │  agent_      │
│  6 deterministic     │     │  Slack /     │          │  analyses    │
│  + anomaly rule      │     │  webhook /   │          │  table       │
└──────────────────────┘     │  log         │          └──────────────┘
                             └──────────────┘
```

## Why this shape

### LLM, not rules or ML

Rule-based monitoring ("CPU > 80%") tells you *what* but not *why*.
OtterTune-style ML tunes innodb knobs but can't spot a missing index.
LLMs excel at three things SeeQL relies on:

- Interpreting EXPLAIN output and proposing indexes (pattern-matching
  on well-documented SQL patterns)
- Correlating events across time ("this query regressed when that DDL
  landed")
- Ingesting everything a DBA would look at (slow log + EXPLAIN + schema
  + locks + server status) in one prompt

### SQLite for monitoring storage

One process is the single writer; SQLite's writer-lock limitation
doesn't bite. Benefits:

- Zero network latency (same VM as the agent)
- No connection pool, no credentials, no monthly cost
- WAL mode gives concurrent reads while writing
- ~1 GB/month at default collection rates; 3–4 GB at 90-day retention
- The LLM agent runs on the same host and reads the same file

Scale-out path: migrate to ClickHouse or BigQuery when one VM can't
keep up. Not needed at typical deployments.

### Tiered collection

Not every metric changes at the same rate. Running heavy
`information_schema` queries every 30 seconds wastes cycles and adds
load to the target. Three loops split the work:

| Loop | Interval | Purpose |
|------|----------|---------|
| Fast | 30 s | "Is the server on fire right now?" |
| Medium | 5 min | Aggregated stats + heavier queries |
| Slow | 30 min | Schema fingerprints, index analysis |

See [collectors.md](collectors.md) for the per-loop breakdown.

## Error isolation

Each collector's `run()` has its own try/except. One collector failing
(say, `data_lock_waits` during Cloud SQL maintenance) does not stop
the others. See `collectors/base.py::BaseCollector.run`.

## Retry logic

Transient MySQL errors (2003 connection lost, 2006 server gone,
2013 broken pipe, 2055 socket, 1205 lock wait timeout) retry twice
with exponential backoff. Non-transient errors (syntax, permissions)
fail immediately.

## Data sources

### From MySQL (`dba_agent` with `SELECT, PROCESS`)

- `performance_schema.events_statements_summary_by_digest` — query
  fingerprints with timing / row stats
- `performance_schema.events_waits_summary_global_by_event_name` —
  what MySQL waits on
- `performance_schema.events_stages_summary_global_by_event_name` —
  time spent per execution stage
- `performance_schema.table_io_waits_summary_by_table` — IO per table
- `performance_schema.data_lock_waits` — current InnoDB lock waits
- `performance_schema.metadata_locks` — DDL blocking
- `performance_schema.threads` — active processlist (better than
  `SHOW PROCESSLIST`)
- `information_schema.innodb_trx` — active transactions
- `information_schema.INNODB_METRICS` — 300+ InnoDB counters
- `information_schema.INNODB_BUFFER_POOL_STATS` — cache stats
- `information_schema.TABLES` — table sizes
- `information_schema.COLUMNS` / `STATISTICS` — schema + index
  fingerprints
- `SHOW GLOBAL STATUS` — cumulative counters → deltas
- `SHOW GLOBAL VARIABLES` — config snapshot
- `SHOW CREATE TABLE` — full DDL (captured on change detection)
- `SHOW ENGINE INNODB STATUS` — deadlock parsing

### From GCP APIs (optional — `[gcp]` extra)

- `cloudsql.googleapis.com/database/cpu/utilization`
- `cloudsql.googleapis.com/database/memory/utilization`
- `cloudsql.googleapis.com/database/disk/utilization`
- `cloudsql.googleapis.com/database/disk/read_ops_count`
- `cloudsql.googleapis.com/database/network/connections`
- Slow query log via `cloudsql.googleapis.com/mysql-slow.log`

## What the LLM receives

Not raw metrics — a **Structured State Report** built by
`agent/state_builder.py`. Example:

```
## Current state (last 5 min)
- Top 10 queries by total_latency (with up/down/stable trend)
- Top 5 by rows_examined/rows_sent ratio (missing index signals)
- Current lock waits: 3 transactions waiting, longest for 12 s
- Buffer pool hit ratio: 99.2 % (normal)
- Threads_running: 47 (4× above baseline of 8-12)

## Changes since last analysis
- NEW query fingerprint 0xABCD appeared 8 min ago
- DDL change on `orders`: column added
- Query 0xEF01 avg_time 0.02 → 0.18 s (9× regression)

## Historical context (7-day comparison)
- Same hour last week: Threads_running avg 10
- Query 0xEF01 was stable until today
```

Plus eight tools the agent can call: `run_explain`, `get_table_schema`,
`get_query_history`, `get_lock_graph`, `get_live_processlist`,
`get_live_locks`, `get_live_innodb_status`, `explain_query`. See
[agent.md](agent.md).

## Anomaly detection & incident replay

Anomaly detection (`alerting/anomaly.py`) runs same-hour-same-weekday
z-score baselines over 28 days with 24h and all-data fallbacks.
Detected anomalies persist to `anomaly_events`. Gap-based clustering
(`alerting/incidents.py`) groups anomalies within
`incident_gap_minutes` (default 15) into `incident_windows`, capped at
`incident_max_duration_minutes` (default 120).

`seeql replay` replays a window by assembling a chronological timeline
from every stored table, then optionally has the LLM narrate the root
cause. See [incidents.md](incidents.md).
