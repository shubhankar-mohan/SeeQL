# CLAUDE.md — Project Context for LLM Sessions

This file captures the full context of the MySQL DBA Agent project. Feed this to an LLM (Claude, GPT, etc.) when continuing development so it understands the vision, decisions, and constraints without re-explaining everything.

---

## What This Project Is

A MySQL DBA Agent — a continuously running service that collects metrics from a production MySQL database and feeds them to an LLM agent that acts as a senior DBA. The LLM doesn't just monitor — it reasons, correlates, and suggests optimizations.

This is NOT a dashboarding tool. It's NOT Prometheus + Grafana. It's an autonomous reasoning agent that happens to use MySQL metrics as its input.

## The Problem It Solves

Any team running a high-traffic MySQL 8.0 deployment at scale — especially on managed services like Cloud SQL, RDS, or Aurora — hits recurring pain points that traditional monitoring tools don't address:

1. **Lock cascading failures:** A long-running batch aggregation query on a hot table takes row locks, blocking transactional queries, which pile up, which exhaust `max_connections`, which crashes the app. By the time a human notices, it's too late.

2. **DDL changes causing silent regression:** Someone adds a column or modifies an index, and a previously-fast query starts doing full table scans. Nobody notices until users complain or the server is on fire. The gap between "DDL changed" and "query got slow" is the key correlation nobody is tracking.

3. **No proactive optimization:** Small teams without dedicated DBAs optimize reactively — after an incident. The goal is to shift this to proactive: detect emerging problems before they cascade.

4. **Context fragmentation:** A human DBA would look at slow log + EXPLAIN + schema + locks + server status simultaneously. No single tool gives this combined view. An LLM can ingest all of it and reason across it in one shot.

## Infrastructure Context

Designed primarily for managed MySQL services (GCP Cloud SQL, AWS RDS/Aurora, Azure Database for MySQL), which share these constraints:

- **Target:** MySQL 8.0+
- **Managed means:** No SSH, no OS access, no `my.cnf` edits, no filesystem access, limited `SET GLOBAL`
- **Available:** `performance_schema`, `information_schema`, `sys` schema, most `SHOW` commands, cloud-provider monitoring APIs, cloud-provider slow query log pipelines
- **Agent runs on:** Any VM or container with network access to the target MySQL (private IP / VPC peering recommended)
- **Monitoring storage:** SQLite on the agent's local disk (WAL mode)

## Architecture Decisions Made

### Why LLM and Not Rules/ML

Rules-based monitoring is what Prometheus alerts do. ML-based tuning is what OtterTune did (RIP, acquired by AWS). Both have a ceiling:

- Rules: "CPU > 80%" tells you nothing about why. You still need a human to investigate.
- ML knob tuning: Optimizes `innodb_buffer_pool_size` and similar params. Doesn't help when the problem is a missing index or a bad query.

LLMs excel at:
- Interpreting EXPLAIN output and suggesting indexes (pattern matching on well-documented knowledge)
- Correlating events across time ("this query got slow when that DDL happened")
- Ingesting the full context a DBA would look at (schema + query + locks + config) in one prompt
- Producing actionable, human-readable recommendations with reasoning

### Why SQLite for Monitoring Storage

- Single writer process → SQLite's limitation doesn't matter
- Zero network latency (local disk vs Cloud SQL hop)
- No connection pool, no credentials, no monthly cost
- WAL mode gives concurrent reads during writes
- ~1 GB/month at current collection rates, grows to 3-4 GB at 90-day retention
- The LLM agent runs on the same VM and reads the same file

If we ever need remote access to monitoring data or multi-node writes, we migrate to ClickHouse or BigQuery. Not needed now.

### Why Not PMM (Percona Monitoring and Management)

PMM is excellent and does ~70% of what the collection layer does. The reason we're building custom:

1. PMM doesn't have an LLM agent layer. That's our entire point.
2. PMM's QAN (Query Analytics) is good but we need raw data in a format the LLM can consume.
3. PMM adds operational overhead (it's a full server stack — Prometheus, Grafana, ClickHouse, API server).
4. The collection code is straightforward. The value is in the agent layer, not the collection layer.

That said, PMM can coexist. If the team wants dashboards, install PMM alongside. The agent is complementary, not competitive.

### Tiered Collection Frequency

Not everything changes at the same rate. Running heavy queries every 30 seconds wastes resources. 19 collectors across 3 loops:

| Loop | Interval | Collectors (count) | What |
|------|----------|--------------------|------|
| Fast | 30s | 4: processlist, lock_waits, transactions, metadata_locks | "Is the server on fire right now?" checks. Must be near-real-time. |
| Medium | 5 min | 11: query_digests, wait_events, table_io, innodb_metrics, buffer_pool, global_status, gcp_metrics, gcp_slow_log, innodb_status, execution_stages, explain_capture | Aggregated stats that change over minutes, not seconds. Heavier queries. |
| Slow | 30 min | 4: schema_snapshot, unused_indexes, redundant_indexes, global_variables | Schema changes and index analysis are rare events. Running `information_schema` queries is slow on servers with many tables. |

A 4th scheduler job runs daily for data retention cleanup.

### Error Isolation

Each collector is independent. If `performance_schema.data_lock_waits` throws an error (rare but possible during Cloud SQL maintenance), the processlist collector still runs. This is implemented via the `BaseCollector` pattern where each collector's `run()` method has its own try/except.

### Retry Logic

Transient MySQL errors (connection lost, server gone away, lock wait timeout) are retried up to 2 times with exponential backoff. Non-transient errors (syntax error, permission denied) fail immediately. This handles Cloud SQL's brief maintenance windows without false-positive failures.

## Data Sources — Complete Map

### From MySQL (via SQL queries to production)

All queries are centralized in `collectors/queries.py` for easy audit.

**performance_schema (must be enabled via Cloud SQL flags):**
- `events_statements_summary_by_digest` — query fingerprints with timing/row stats
- `events_waits_summary_global_by_event_name` — what MySQL waits on
- `events_stages_summary_global_by_event_name` — where time is spent per execution stage
- `table_io_waits_summary_by_table` — IO per table
- `data_lock_waits` — current InnoDB lock waits (MySQL 8.0+)
- `metadata_locks` — DDL blocking detection
- `threads` — active processlist (better than `SHOW PROCESSLIST`)

**information_schema:**
- `innodb_trx` — active transactions
- `INNODB_METRICS` — 300+ InnoDB internal counters
- `INNODB_BUFFER_POOL_STATS` — cache effectiveness
- `TABLES` — table sizes, row counts
- `COLUMNS` — column definitions (for schema fingerprinting)
- `STATISTICS` — index definitions (for index fingerprinting)

**Server commands:**
- `SHOW GLOBAL STATUS` — cumulative counters (converted to deltas)
- `SHOW GLOBAL VARIABLES` — server configuration snapshot
- `SHOW CREATE TABLE` — full DDL (captured on change detection)
- `SHOW ENGINE INNODB STATUS` — dense text blob with deadlock info (parsed by `parsers/innodb_status.py`)

### From GCP APIs

Cloud SQL gives no OS access. CPU/memory/disk metrics come from:
- `cloudsql.googleapis.com/database/cpu/utilization`
- `cloudsql.googleapis.com/database/memory/utilization`
- `cloudsql.googleapis.com/database/disk/utilization`
- `cloudsql.googleapis.com/database/disk/read_ops_count` / `write_ops_count`
- `cloudsql.googleapis.com/database/network/connections`

Slow query logs come from Cloud Logging:
- `log_id("cloudsql.googleapis.com/mysql-slow.log")`

### What the LLM Agent Receives

Not raw metrics. A **Structured State Report** — pre-processed narrative (built by `agent/state_builder.py`):

```
## Current State (last 5 min)
- Top 10 queries by total_latency (with avg_time trend: up/down/stable)
- Top 5 queries by rows_examined/rows_sent ratio (missing index signals)
- Current lock waits: 3 transactions waiting, longest for 12 seconds
- Buffer pool hit ratio: 99.2% (normal)
- Threads_running: 47 (4x above baseline of 8-12)

## Changes Since Last Analysis
- NEW query fingerprint 0xABCD appeared 8 minutes ago (not seen before)
- DDL change on `loyalty_members`: column `reward_tier` added
- Query 0xEF01 avg_time increased from 0.02s to 0.18s (9x regression)

## Historical Context (7-day comparison)
- Same hour last week: Threads_running avg was 10
- Query 0xEF01 was stable at 0.02s for the past 30 days until today
```

The agent has tools it can call (defined in `agent/tools.py`):
- `run_explain(query)` → get EXPLAIN output
- `get_table_schema(table)` → DDL + indexes
- `get_query_history(fingerprint)` → performance trend
- `get_lock_graph()` → current lock tree

## Project Structure

Key packages and files beyond the original collector scaffolding:

- **`collectors/`** — All metric collectors extending `BaseCollector`. Includes: `processlist.py`, `lock_waits.py`, `transactions.py`, `metadata_locks.py`, `query_digests.py`, `wait_events.py`, `table_io.py`, `innodb_metrics.py`, `buffer_pool.py`, `global_status.py`, `schema_snapshot.py`, `index_analysis.py`, `global_variables.py`, `execution_stages.py`, `innodb_status.py`, `explain_capture.py`, `gcp_metrics.py`, `gcp_slow_log.py`, `queries.py`, plus loop drivers `fast_loop.py`, `medium_loop.py`, `slow_loop.py`
- **`agent/`** — LLM agent layer. `state_builder.py` (structured state reports), `llm_agent.py` (agent orchestration, supports Gemini, Claude, and OpenAI / OpenAI-compatible), `tools.py` (tool definitions the LLM can call), `prompts.py` (system/user prompts), `queries.py` (agent-specific SQL), `replay.py` (incident replay + LLM root cause narration)
- **`alerting/`** — Rule-based alerting engine. `engine.py` (evaluation loop), `rules.py` (6 built-in rules: lock_cascade, threads_running_spike, query_regression, ddl_change, high_cpu, deadlock_detected), `anomaly.py` (z-score anomaly detection — separate layer, not a rule), `anomaly_store.py` (anomaly event persistence), `incidents.py` (gap-based incident windowing), `channels.py` (3 channels: Slack, webhook, log), `models.py` (alert data models)
- **`api/`** — HTTP endpoints. `prometheus.py` (Prometheus `/metrics` endpoint, ~20 gauges/counters), `agent_routes.py`, `dashboard_routes.py`, `dashboard_api.py`, `query_helpers.py`
- **`parsers/`** — Output parsers. `innodb_status.py` (parses `SHOW ENGINE INNODB STATUS`)
- **`storage/`** — SQLite layer. `schema.sql` (30 tables), `retention.py` (daily cleanup of old rows, per-table overrides), `writer.py`
- **`seeql/`** — CLI-support package. `doctor.py` (preflight checks for `seeql doctor`), `errors.py` (E001-E010 error catalog)
- **`scheduler/`** — APScheduler wiring (`runner.py`)
- **`templates/`**, **`static/`** — HTMX dashboard views (`dashboard/overview.html`, partials including `incidents_timeline.html`)
- **`reports/`** — Auto-generated incident postmortems (written by `seeql replay`; `.md` files, not checked in)
- **`scripts/`** — One-off maintenance scripts (backfills, migrations)

## Code Conventions

- **Python 3.12**, type hints where helpful but not obsessive
- **No ORM** — we're writing time-series data, not modeling a domain
- **All SQL in `queries.py`** — never inline SQL in collector code (collector queries in `collectors/queries.py`, agent queries in `agent/queries.py`)
- **Config via YAML** with env var substitution for secrets
- **Logging:** structured format with collector name, timing
- **Error handling:** fail independently, log, continue
- **LLM provider:** configurable across Gemini (Vertex AI), Claude (Anthropic API or Vertex AI), and OpenAI — plus any OpenAI-compatible endpoint (Azure OpenAI, Ollama, vLLM, Groq, OpenRouter, LM Studio, …) via `agent.openai_base_url`. Backend is chosen from the model name (`gemini-*`/`claude-*`/`gpt-*`/`o*`) or forced with `agent.provider`. Default (shipped in `settings.yaml`): `gemini-2.0-flash`. Selection + each provider loop live in `agent/llm_agent.py` (`_detect_backend`, `_run_gemini_loop`/`_run_claude_loop`/`_run_openai_loop`).

## Current State

### Done
- [x] Project structure and config system
- [x] MySQL connection pooling for production DB (multi-server via `ServerContext`)
- [x] SQLite monitoring storage with WAL mode (30 tables)
- [x] Fast loop (4 collectors): processlist, lock waits, transactions, metadata locks
- [x] Medium loop (11 collectors): query digests, wait events, table IO, InnoDB metrics, buffer pool, global status deltas, GCP metrics, GCP slow log, InnoDB status, execution stages, EXPLAIN capture
- [x] Slow loop (4 collectors): schema snapshots with DDL change detection, unused indexes, redundant indexes, global variables
- [x] Global status delta calculator (cumulative → rate-of-change)
- [x] APScheduler-based collection orchestration (4 jobs: fast, medium, slow, retention)
- [x] Retry logic for transient MySQL errors
- [x] Dockerfile with healthcheck
- [x] CLI with argparse subparsers: `check`, `init-db`, `run`, `serve`, `doctor`, `replay`, `incidents`, `investigations`, `mcp` (legacy `--check`/`--once`/`--api` flags still supported with deprecation warning)
- [x] GCP Cloud Monitoring API collector (CPU, memory, disk IO)
- [x] Cloud Logging slow query log collector
- [x] `SHOW ENGINE INNODB STATUS` parser (`parsers/innodb_status.py`)
- [x] EXPLAIN plan auto-capture for top-N expensive queries
- [x] Data retention cleanup (runs daily via scheduler, with per-table overrides)
- [x] Structured State Builder (pre-processor for LLM input)
- [x] LLM Agent layer with tool-use (Gemini via Vertex AI, Claude via Anthropic API / Vertex AI, OpenAI + any OpenAI-compatible endpoint; default model: gemini-2.0-flash)
- [x] Alerting with 6 built-in rules and 3 channels (Slack, webhook, log)
- [x] **Anomaly detection layer** (`alerting/anomaly.py`, 615 lines): z-score, same-hour-same-weekday baselines over 28 days with 24h + all-data fallbacks, 7 active metrics (query-latency per-digest planned), zero-stddev guard, cold-start handling, integrated with alerting engine and state builder
- [x] Prometheus endpoint at `/metrics` (~20 gauges/counters)
- [x] Index analysis collectors (unused and redundant index detection)
- [x] API layer: agent routes, dashboard routes, dashboard API
- [x] **Anomaly event persistence + incident windowing** (`alerting/anomaly_store.py`, `alerting/incidents.py`): gap-based clustering with duration cap, two new tables (`anomaly_events`, `incident_windows`)
- [x] **Incident replay** (`agent/replay.py`): `seeql replay --from X --to Y` / `--incident N` / `--latest` with chronological timeline builder + LLM root cause analysis + timeline-only fallback
- [x] **Dashboard incidents timeline widget** with HTMX auto-refresh + ARIA live regions
- [x] **Inbound webhooks + 3-phase root-cause investigator**:
  - `POST /webhooks/{provider}` with HMAC verification (`api/webhook_routes.py`).
  - Four adapters: generic, GCP Cloud Monitoring, PagerDuty, Grafana Alertmanager (`alerting/inbound/`).
  - Investigator orchestrator (`alerting/investigator.py`): Phase 1 triage (zero new MySQL queries — state builder + timeline + missing-index correlator), Phase 2 LLM with tool-budget enforcement via `run_llm_analysis(tool_budget=...)`, Phase 3 continuous sampling (`alerting/phase3.py`) with load-guard on `Threads_running` + per-minute query budget + alert-type-specific clearance conditions.
  - Missing-index correlator (`alerting/correlators/missing_index.py`) joins `query_digest_snapshots` + `explain_captures` + `ddl_changes` + `unused_index_snapshots` + `redundant_index_snapshots` into structured evidence.
  - Four new SQLite tables: `inbound_alerts`, `investigations`, `investigation_samples`, `investigation_findings`.
  - CLI: `seeql investigations list | show <id> | trigger | abort <id>`.
  - Dashboard partial `templates/partials/investigations_panel.html` + JSON API `/api/v1/investigations/recent`.
  - Config sections `webhooks:` and `investigator:` in `settings.yaml`.
- [x] **MCP server for external Claude clients** (`mcp_server/`):
  - 28 tools exposed via Model Context Protocol covering investigations, incidents, state reports, query history, cached EXPLAINs, missing-index correlator, live MySQL reads (processlist/locks/transactions/innodb_status/index_stats/table_status), plus gated action tools (`seeql_trigger_investigation`, `seeql_abort_investigation`, `seeql_explain_query`).
  - 7 resources under `seeql://` scheme, 5 prompts (`seeql/rca`, `seeql/review_investigation`, `seeql/explain_digest`, `seeql/schema_audit`, `seeql/investigate_window`).
  - Safety layer (`mcp_server/safety.py`): server allowlist, per-session budget (live_calls, explain_calls), per-tool rate limiter, action gate.
  - Two transports: stdio (for Claude Desktop / Claude Code subprocess mode) and streamable HTTP/SSE (for remote clients) with bearer-token middleware.
  - CLI: `seeql mcp [--http] [--port] [--bind]`. Config section `mcp:` in `settings.yaml`. Requires `pip install 'seeql[mcp]'` or `pip install 'mcp>=1.2'`.
  - Setup guide: `docs/mcp.md`.

### Not Yet Built
- [ ] (Nothing at this level — Phase 4 items in PLAN.md are deferred until real-world signal)

## Bugs Found and Fixed

1. **Lock waits query incompatible with MySQL 8.0** — `information_schema.innodb_lock_waits` was removed in 8.0. Fixed to use `performance_schema.data_lock_waits`.

2. **Config dict mutation** — connection pool creation was using `.pop()` which mutated the config. Fixed to `.get()` with explicit kwarg building.

3. **No retry on transient failures** — added exponential backoff for MySQL error codes 2003, 2006, 2013, 2055, 1205.

4. **DDL detection missed changes on agent restart** — previous hash loading from SQLite was happening after the first comparison instead of before. Fixed ordering so changes during downtime are detected on first run.

5. **SQL init used naive string split** — `cmd_init_db()` was splitting on `;` which breaks on semicolons in comments. Fixed to use `conn.executescript()` (SQLite's native multi-statement executor).

## Key Tables the Agent Monitors

The agent discovers hot tables automatically from `performance_schema` aggregates. Typical patterns the agent is built to detect:

- High-write hot tables (often named `users`, `members`, `orders`, `transactions`) that see both OLTP writes and occasional batch OLAP aggregations — this is the classic setup for lock cascades.
- Schema size and row counts vary widely; SeeQL has been validated against ~100-table schemas with tables up to ~10M rows.

The monitoring SQLite database has 30 tables (see `storage/schema.sql`):
- `query_digest_snapshots` — The most important. Performance per query fingerprint over time.
- `ddl_changes` — The unique value-add. Schema change history with before/after DDL.
- `lock_wait_snapshots` — Lock contention history for incident investigation.
- `global_status_snapshots` — Server-wide counters as deltas for trend analysis.
- `agent_analyses` — LLM agent findings and recommendations.
- `anomaly_events`, `incident_windows` — anomaly event stream + gap-clustered incident windows (see `alerting/incidents.py`).
- Plus tables for: explain plans, GCP metrics, slow query logs, InnoDB status, execution stages, index analysis, alerts, global variables, and more.

## How to Continue Development

1. Read this file first.
2. Read `PLAN.md` for the week-by-week execution plan.
3. Check the "Not Yet Built" section above — the main collection/agent/alerting/replay pipeline is done. Remaining work lives in `TODOS.md`.
4. Collector SQL lives in `collectors/queries.py`, agent SQL in `agent/queries.py` — modify there, not inline.
5. New collectors extend `BaseCollector` and implement `name`, `collect()`, `store()`.
6. New alert rules go in `alerting/rules.py`, new channels in `alerting/channels.py`.
7. Test with `seeql check` (single cycle, dry-run friendly) before running `seeql run` or `seeql serve` continuously. Legacy `python main.py --once` still works with a deprecation warning.
