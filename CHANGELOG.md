# Changelog

All notable changes to SeeQL will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Incident replay** (`seeql replay --from X --to Y`, `--incident N`, `--latest`)
  — chronological timeline reconstruction + optional LLM root cause narration
  with graceful timeline-only fallback when no LLM backend is configured.
- **Incident windowing** (`alerting/incidents.py`) — gap-based clustering of
  anomaly events with a max-duration cap. Configurable via
  `alerting.incident_gap_minutes` (default 15) and
  `alerting.incident_max_duration_minutes` (default 120).
- **Anomaly event persistence** — anomaly events now land in a new
  `anomaly_events` table, grouped into `incident_windows`. Backfills
  gracefully on upgrade via the idempotent migration.
- **`seeql incidents list`** — browse detected incidents with status/server/
  limit filtering.
- **Dashboard incidents widget** on the overview page with HTMX auto-refresh
  and ARIA live regions.
- **Slack notifications** for newly-detected incidents (not for extensions).
- **Argparse subparsers** — `seeql check / run / serve / replay / incidents /
  init-db` with legacy `--check / --once / --api` flags still working for
  backward compatibility (deprecation warning emitted).
- **`seeql` console script** via `pyproject.toml` entry point — `pip install
  -e .` gives you a real `seeql` command.
- **LICENSE (Apache 2.0)**, **CHANGELOG.md**, metadata polish in `pyproject.toml`.
- Recent Incidents section in the LLM state report so routine analyses see
  unresolved incident windows from the last 24h.

### Fixed
- **Buffer Pool Hit Ratio was always 0.** Root cause:
  `information_schema.INNODB_BUFFER_POOL_STATS.HIT_RATE` is an instantaneous
  sample over the last ~1 second and returns 0 when no page gets occur in
  that window. Replaced with a cumulative calculation from
  `Innodb_buffer_pool_reads` / `Innodb_buffer_pool_read_requests` (already
  collected via `SHOW GLOBAL STATUS`). Fix applied to the dashboard,
  Prometheus gauge, and LLM state report — all now show the correct ratio on
  any warm workload.
- **Query detail pane showed parameterized queries with `?` placeholders.**
  The collector was already storing `query_sample_text` (the real SQL with
  actual parameter values), but the dashboard only selected `digest_text`.
  Fixed `partial_query_detail` to prefer `query_sample_text` with a visual
  "sample (real values)" vs "pattern (placeholders)" label. The "Copy
  EXPLAIN" button now produces a runnable statement.
- **Graceful SIGTERM shutdown** — previous `run_scheduler` only caught
  `KeyboardInterrupt` and hard-exited on SIGTERM, risking WAL truncation.
  Now a module-level `threading.Event` + `PRAGMA wal_checkpoint(TRUNCATE)`
  guarantees pending SQLite writes are flushed before exit.
- **Multi-server alert isolation** — `detect_anomalies(server_id)` and all 6
  rule evaluators now accept `server_id`, preventing cross-server false
  positives. Cooldowns are namespaced by server so an alert on one server
  can't suppress the same alert on another.
- **Per-cycle anomaly cache** — `detect_anomalies()` was previously called
  twice per medium loop (once by `state_builder`, once by
  `evaluate_anomaly`) and recomputed all baselines. Now cached by
  `(server_id, z_override, cycle_minute)` with a natural wall-clock eviction.

### Schema
- Two new tables: `anomaly_events`, `incident_windows`. Brings the monitoring
  schema from 24 to 26 tables. Upgrades from older installs get the new
  tables via the idempotent migration on startup.
- Per-table retention overrides in `storage/retention.py`. `incident_windows`
  defaults to 365 days, `anomaly_events` to 90, `ddl_changes` to 365, etc.
  Configurable via `retention.overrides` in `settings.yaml`.

## [0.1.0] — 2026-04-11

Initial public release.

### Collection layer
- 19 collectors across fast (30s) / medium (5m) / slow (30m) loops.
- MySQL connection pooling with multi-server `ServerContext`.
- SQLite monitoring storage (WAL mode).
- `SHOW ENGINE INNODB STATUS` parser.
- GCP Cloud Monitoring API + Cloud Logging slow-query collectors.

### Agent layer
- Structured state builder for LLM consumption.
- LLM agent with tool-use (Gemini via Vertex AI + Claude via Anthropic).
- 8 agent tools for autonomous investigation.

### Alerting layer
- 6 built-in rules (lock_cascade, threads_running_spike, query_regression,
  ddl_change, high_cpu, deadlock_detected).
- 3 channels: Slack, webhook, log.
- Z-score anomaly detection with same-hour-same-weekday baselines.

### API + dashboard
- FastAPI routes + sketch-aesthetic HTMX dashboard.
- Prometheus `/metrics` endpoint with ~20 gauges/counters.
- Index analysis (unused + redundant index detection).
