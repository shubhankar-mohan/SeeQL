# Changelog

All notable changes to SeeQL will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Configuration is now a single mounted YAML file (Prometheus-style).** Mount
  your config at `/etc/seeql/seeql.yml` (or pass `--config` / set `SEEQL_CONFIG`);
  copy `seeql.example.yml` to start. Connections and the multi-server list live
  only in this file — use a `servers:` block for multiple hosts — with secrets
  injected via `${VAR}`. **Breaking:** the `PROD_DB_*` and `SEEQL_SERVER_*`
  environment overrides were removed; move those into the config file. A small
  set of operational env vars remains (`SEEQL_CONFIG`, `SEEQL_MON_DB_PATH`,
  `SEEQL_DB_MAX_SIZE_MB`, `SEEQL_LOG_MAX_SIZE_MB`, `SEEQL_RETENTION_DAYS`,
  `SEEQL_LOG_LEVEL`, `SEEQL_ENV`).
- **`seeql run`/`serve` now self-initialize the monitoring schema.** Startup
  previously ran only migrations, which never created the 30-table base schema,
  so a fresh deployment needed a separate `seeql init-db`. The idempotent
  `schema.sql` is now applied on every startup, so the default `seeql serve`
  works against an empty monitoring DB with no manual init step.

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
- **`/dashboard/todo` returned a 500 on an empty or sparse database.** The
  route formatted SQL aggregates (`AVG`/`SUM`/`MAX` over query digests, table
  IO, slow-log repeaters, and storage) with format specs like `:.2f` / `:,`;
  when those aggregates came back `NULL` it raised `TypeError: unsupported
  format string passed to NoneType`. Every formatted aggregate is now
  None-coalesced.
- **NULL `digest_text` crashed several views and the state report.**
  `digest_text` is the only nullable column in `query_digest_snapshots`, and
  multiple call sites used `row.get('digest_text', default)[:N]` — where `.get`
  returns `None` for an explicit-NULL value (the default only applies to a
  *missing* key) and the slice then raised `TypeError: 'NoneType' object is not
  subscriptable`. Fixed across the state report (`/api/v1/state-report`), the
  to-do regression list (`/dashboard/todo`), the query-detail partial, and the
  EXPLAIN-failure log line by coalescing before slicing. Added regression tests
  that seed a NULL-`digest_text` row for each surface.
- **LLM agent provider layer hardened.** (1) Gemini responses with no
  `candidates` (safety/recitation blocks, or `MAX_TOKENS` with no content) raised
  `IndexError` and silently killed the analysis — now guarded. (2) Claude-via-
  Vertex failed with a cryptic `No module named 'google.auth'` on a core-only
  install (google-auth ships with the `[gcp]` extra) — now an actionable error
  pointing at `pip install 'seeql[gcp]'`. (3) An unsupported model (e.g. `gpt-4o`)
  was silently swapped for Claude/Gemini — now logs a warning. (4) Replay/RCA
  analyses stored an empty `recommendations` column because the prompt uses a
  singular `### Recommendation` header the parser didn't match — parsing now
  handles both formats. Added the first direct tests for the provider loops.
- **`seeql doctor` reported the wrong MySQL host.** The "Config loads" and
  "Production MySQL reachable" lines read the legacy `production_db` section for
  display while the actual reachability test used the `servers:` registry, so an
  install configured the documented way showed the stock default `10.0.0.1`. Both
  lines now read the registry's default server.
- **`seeql serve` / `seeql mcp` without their extras printed a raw traceback**
  instead of a friendly "install the `[api]` / `[mcp]` extra" message. Now a
  clear, actionable error.
- **Startup crash where `cryptography`'s native bindings can't load.**
  `collectors/__init__.py` probed GCP availability with a module-level
  `import google.oauth2.service_account`, which eagerly loads the cryptography
  Rust OpenSSL bindings. Because every collection loop imports the package,
  `import collectors` — and therefore the scheduler, `seeql serve`, and the
  `/status` route — pulled in that crypto stack on startup, crashing anywhere
  those bindings can't initialize (e.g. some emulated arm64 environments).
  Availability is now probed with `importlib.util.find_spec` (no import) and
  the `service_account` import is deferred to the only path that needs it
  (loading a service-account key file); pure-MySQL and ADC deployments no
  longer load the crypto stack at all.
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
