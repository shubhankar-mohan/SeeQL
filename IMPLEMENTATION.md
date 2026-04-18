# IMPLEMENTATION.md — Step-by-Step Guide

> **Companion to PLAN.md.** PLAN.md is the *what* and *why*. This doc is the *how* — exact files, signatures, step order, tests, and verification commands. Every section here maps 1:1 to a PLAN.md item.
>
> **Last synced with PLAN.md:** 2026-04-11

## How to read this doc

Each item has a consistent structure:

- **Files** — exact paths (and line numbers for edits)
- **Dependencies** — which earlier items must land first
- **Steps** — numbered, sequential, each one a testable unit of work
- **Code** — just the load-bearing snippets, not full rewrites
- **Tests** — what to add to `tests/` and where
- **Verify** — a single command that proves it works

Phase 0–1 are specced in full detail. Phase 2–3 have moderate detail where subtlety matters. Phase 4 is a sketch until unblocking conditions fire.

---

## Phase 0 — Hygiene & Prerequisites

---

### 0.1 Initialize git repository

**Files:** `.gitignore` (new), `.git/` (created by `git init`)

**Dependencies:** none — do this first.

**Steps:**

1. Create `.gitignore` at the repo root with:
   ```
   # Python
   __pycache__/
   *.py[cod]
   *.egg-info/
   venv/
   .venv/
   .pytest_cache/

   # SeeQL data + logs
   data/
   logs/
   *.db
   *.db-journal
   *.db-wal
   *.db-shm

   # Local secrets
   settings.local.yaml
   .env
   .env.*

   # IDE
   .vscode/
   .idea/
   *.swp
   .DS_Store
   ```
2. Run `git init` at the repo root.
3. Check nothing sensitive is about to be committed: `git status --short | grep -E '\.env|settings\.local|credentials|key'` should return empty.
4. `git add .gitignore && git commit -m "Initial commit: .gitignore"`.
5. `git add -A && git status` — confirm `settings.local.yaml`, `data/`, `logs/`, `venv/`, `__pycache__/` are **not** listed. Abort and adjust `.gitignore` if any of these are staged.
6. `git commit -m "Initial import of SeeQL (collectors, agent, alerting, dashboard)"`.

**Tests:** none (infra change).

**Verify:**
```bash
git log --oneline && git ls-files | grep -E 'settings\.local|\.env$|\.db$' ; [ $? -ne 0 ] && echo "OK: no secrets tracked"
```

---

### 0.2 Update CLAUDE.md to reflect current state

**Files:** `CLAUDE.md`

**Dependencies:** none.

**Steps:**

1. In the "Done" section, add:
   - `[x] Anomaly detection layer (alerting/anomaly.py, 464 lines, z-score, same-hour-same-weekday baselines, integrated with alerting engine and state builder)`
2. In the "Not Yet Built" section, remove the "Anomaly detection (statistical baseline + deviation)" line. Replace it with:
   - `[ ] Incident window persistence + seeql replay (Phase 1 of PLAN.md)`
3. Update the project structure list — confirm `alerting/anomaly.py` is mentioned in the `alerting/` bullet.
4. Update table count. Currently reads "22 tables (see storage/schema.sql)". Change to "24 tables today; 26 after Phase 1.1 ships (`anomaly_events` + `incident_windows`)".
5. Re-read the whole file and fix any other stale claims (search for "not yet", "TODO", "upcoming" and reconcile).

**Tests:** none.

**Verify:**
```bash
# Count should match the table count claim
grep -c '^CREATE TABLE' storage/schema.sql
# Should match the string in CLAUDE.md
grep -n 'tables (see' CLAUDE.md
```

---

### 0.3 Graceful SIGTERM handler

**Files:** `scheduler/runner.py:173-207` (`run_scheduler`), `main.py:163-190` (`cmd_api`)

**Dependencies:** none.

**The gap:** Today, `run_scheduler()` only catches `KeyboardInterrupt` / `SystemExit`. Docker sends `SIGTERM` on `docker stop`. Python doesn't map `SIGTERM` to `KeyboardInterrupt` by default — the process exits hard, which can truncate a SQLite WAL write if the medium loop was mid-flush.

**Steps:**

1. Add a module-level event in `scheduler/runner.py`:
   ```python
   import signal
   import threading
   _shutdown_event = threading.Event()
   ```
2. Replace the `try/except KeyboardInterrupt` block in `run_scheduler()` with an event-driven loop:
   ```python
   def _handle_signal(signum, frame):
       logger.info(f"Received signal {signum}, initiating graceful shutdown...")
       _shutdown_event.set()

   signal.signal(signal.SIGTERM, _handle_signal)
   signal.signal(signal.SIGINT, _handle_signal)

   scheduler.start()
   logger.info("Scheduler started. Press Ctrl+C to stop.")

   try:
       while not _shutdown_event.wait(timeout=60):
           pass
   finally:
       logger.info("Shutting down scheduler (waiting for in-flight jobs)...")
       scheduler.shutdown(wait=True)
       _flush_sqlite()
       logger.info("Scheduler stopped cleanly.")
   ```
3. Implement `_flush_sqlite()` — call `PRAGMA wal_checkpoint(TRUNCATE)` on the monitoring DB to guarantee the WAL is flushed:
   ```python
   def _flush_sqlite():
       try:
           from storage.connection import get_mon_connection
           with get_mon_connection() as conn:
               conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
       except Exception as e:
           logger.warning(f"WAL checkpoint failed: {e}")
   ```
4. Mirror the same signal handling in `main.py:cmd_api()` — the `uvicorn.run()` path doesn't go through `run_scheduler()`. Uvicorn handles its own signals, but the scheduler started at line 187 needs to be shut down on SIGTERM. Register a `signal_handlers` callback on uvicorn's `Server` config, OR wrap `uvicorn.run` in a try/finally that calls `scheduler.shutdown(wait=True)` and `_flush_sqlite()`.

**Tests:** `tests/test_scheduler_shutdown.py` (new)
- Spawn `python main.py` as a subprocess.
- Wait 5 seconds for the first medium loop to start.
- Send `SIGTERM`.
- Assert the process exits with code 0 within 10 seconds.
- Assert no `.db-wal` file is left behind (`PRAGMA wal_checkpoint(TRUNCATE)` collapsed it).
- Assert the last log line is "Scheduler stopped cleanly."

**Verify:**
```bash
python main.py &
PID=$!
sleep 5
kill -TERM $PID
wait $PID
echo "exit=$?"  # expect 0
ls data/*.db-wal 2>/dev/null || echo "OK: WAL flushed"
```

---

### 0.4 Multi-server alert filtering (`server_id` plumbing)

**Files:**
- `alerting/anomaly.py:359` (`detect_anomalies`)
- `alerting/anomaly.py:417` (`evaluate_anomaly`)
- `alerting/anomaly.py` METRIC_CONFIGS (every `current_query` and baseline query)
- `alerting/rules.py:15-end` (all 6 rule evaluators)
- `alerting/engine.py:68` (`evaluate`)
- `scheduler/runner.py:39` (`_run_alerts`)
- `agent/state_builder.py:83` (where `detect_anomalies()` is called)

**Dependencies:** none, but this is a prerequisite for 1.2 and every Phase 1 step.

**The gap:** `detect_anomalies()` takes no `server_id`. Every rule evaluator in `alerting/rules.py` queries globally (e.g. `SELECT COUNT(*) FROM lock_wait_snapshots WHERE snapshot_time >= ...`), ignoring `server_id`. In a multi-server deployment, a lock storm on server A fires the rule for server B.

**Steps:**

1. **Signature change — `detect_anomalies`:**
   ```python
   def detect_anomalies(
       server_id: str | None = None,
       z_threshold_override: float | None = None,
   ) -> list[AnomalyResult]:
   ```
   If `server_id` is None, resolve to the default via `get_server_registry().get_default_server_id()`.

2. **Inject `server_id` into every METRIC_CONFIG query.** The baseline query templates (`_BASELINE_SAME_HOUR_DOW`, `_BASELINE_24H`, `_BASELINE_ALL`) already have an `{extra_where}` slot — expand it to include `AND server_id = ?` and pass `(server_id,)` as a param. The `current_query` fields need the same treatment.

3. **Signature change — `evaluate_anomaly`:** accept `server_id` and thread it into `detect_anomalies()`. The alerting engine will pass it in.

4. **Rules.py — every evaluator.** Each of the 6 `evaluate_*` functions in `alerting/rules.py` needs:
   ```python
   def evaluate_lock_cascade(rule_config: dict, server_id: str) -> Alert | None:
       ...
       with get_mon_reader() as conn:
           row = conn.execute("""
               SELECT COUNT(*) as cnt, MAX(wait_seconds) as max_wait
               FROM lock_wait_snapshots
               WHERE snapshot_time >= datetime('now', '-2 minutes')
                 AND server_id = ?
           """, (server_id,)).fetchone()
       ...
       if alert is not None:
           alert.context["server_id"] = server_id
           alert.rule_name = f"{alert.rule_name}:{server_id}"  # namespace cooldowns
       return alert
   ```
   The cooldown namespace change (`rule_name:server_id`) prevents a cooldown on one server from suppressing an alert on another.

5. **Engine.py — iterate over servers.**
   ```python
   def evaluate(loop_name: str = "fast") -> list[Alert]:
       ...
       from config.server_registry import get_server_registry
       servers = get_server_registry().get_active_servers()
       fired = []
       for server in servers:
           for rule_name, evaluator in RULE_EVALUATORS.items():
               ...
               alert = evaluator(rule_cfg, server.server_id)
               ...
       return fired
   ```

6. **Scheduler.py — no change needed** (it already calls `evaluate(loop_name)`). The engine handles the fanout.

7. **state_builder.py:83** — pass `server_id` that's already available in the calling context:
   ```python
   anomalies = detect_anomalies(server_id=server_id)
   ```

**Tests:** `tests/test_multiserver_alerts.py` (new)
- Seed `lock_wait_snapshots` with 5 rows for `server_id='A'` and 0 rows for `server_id='B'`.
- Run `evaluate_lock_cascade(rule_config, server_id='A')` → expect an Alert.
- Run `evaluate_lock_cascade(rule_config, server_id='B')` → expect None.
- Seed `global_status_snapshots` with `Threads_running` baseline data for server A only.
- Run `detect_anomalies(server_id='B')` → expect no anomalies (no baseline exists for B).
- Run `detect_anomalies(server_id='A')` with an elevated current row → expect 1+ anomalies.

**Verify:**
```bash
pytest tests/test_multiserver_alerts.py -v
```

---

### 0.5 Buffer pool hit ratio — fix the "always 0" bug

**Files:**
- `api/dashboard_api.py:189-204` (`/api/v1/metrics/buffer-pool`)
- `agent/state_builder.py` (wherever `hit_ratio` is surfaced to the LLM prompt — check current impl)
- `api/prometheus.py` (if it publishes a buffer-pool-hit-ratio gauge)

**Dependencies:** none.

**The gap:** `collectors/queries.py:176` uses `HIT_RATE / 1000.0` from `INNODB_BUFFER_POOL_STATS`. `HIT_RATE` is an instantaneous value computed over the last ~1-second interval. If no page gets happened, it's 0 — which is what the dashboard is displaying on every sample.

**The fix:** compute cumulative hit ratio from two counters already collected in `global_status_snapshots`:
```
hit_ratio = 1 - (Innodb_buffer_pool_reads / Innodb_buffer_pool_read_requests)
```

**Steps:**

1. **API fix (zero migration).** In `api/dashboard_api.py`, rewrite `/api/v1/metrics/buffer-pool`:
   ```python
   @router.get("/metrics/buffer-pool")
   def metrics_buffer_pool(
       range: str = QueryParam(default="1h"),
       server: str = QueryParam(default=None),
   ):
       """Buffer pool hit ratio from cumulative global status counters."""
       server = resolve_server_id(server)
       start, end = parse_time_range(range)
       sql = f"""
           WITH bucketed AS (
               SELECT snapshot_time,
                      MAX(CASE WHEN variable_name='Innodb_buffer_pool_reads' THEN raw_value END) AS reads,
                      MAX(CASE WHEN variable_name='Innodb_buffer_pool_read_requests' THEN raw_value END) AS requests
               FROM global_status_snapshots
               WHERE variable_name IN ('Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests')
                 AND snapshot_time BETWEEN ? AND ?
                 {_sf(server)}
               GROUP BY snapshot_time
           )
           SELECT snapshot_time,
                  CASE WHEN requests > 0
                       THEN 1.0 - (CAST(reads AS REAL) / requests)
                       ELSE NULL END AS hit_ratio
           FROM bucketed
           WHERE reads IS NOT NULL AND requests IS NOT NULL
           ORDER BY snapshot_time ASC
       """
       return query_rows(sql, (start, end, *_sp(server)))
   ```
   Note: `dirty_pages`, `free_buffers`, `database_pages` columns in the old response are dropped from this endpoint. If any chart needs them, add a separate `/metrics/buffer-pool-pages` endpoint reading from `buffer_pool_snapshots`.

2. **Double-check the Overview KPI card.** Find the template/template-context that renders the "Buffer Pool Hit %" KPI and confirm it consumes the JSON above or switches to the same computation. Grep: `rg -n 'hit_ratio|Buffer Pool Hit' templates/` and `rg -n 'hit_ratio' api/dashboard_routes.py`.

3. **state_builder.py.** Wherever it computes/reports buffer pool hit ratio for the LLM, switch to the same formula. Grep: `rg -n 'hit_ratio|buffer_pool_reads' agent/state_builder.py`.

4. **Prometheus gauge.** If `api/prometheus.py` exposes a `seeql_buffer_pool_hit_ratio` gauge, it should also switch to the new formula. Grep: `rg -n 'hit_ratio' api/prometheus.py`.

5. **Collector fix (deferred but noted).** Leave `collectors/queries.py:176` alone for now (don't break historical rows). File a follow-up to compute `hit_ratio` from the two counters at collection time and deprecate the `HIT_RATE / 1000` path.

**Tests:** `tests/test_buffer_pool_hit_ratio.py` (new)
- Seed `global_status_snapshots` with `reads=500, requests=100000` (expected ratio 0.995).
- Call `metrics_buffer_pool` directly → assert `hit_ratio ≈ 0.995`.
- Seed a row with `reads=0, requests=0` (cold start) → assert `hit_ratio` is NULL/None and the row is filtered out.

**Verify:**
```bash
# Start the server and hit the endpoint
curl -s 'http://localhost:8080/api/v1/metrics/buffer-pool?range=1h' | jq '.[-1].hit_ratio'
# Expect a number in [0.90, 1.00] on a warm DB
```

Also cross-check against a live MySQL:
```sql
SHOW GLOBAL STATUS WHERE Variable_name LIKE 'Innodb_buffer_pool_read%';
-- 1 - (Innodb_buffer_pool_reads / Innodb_buffer_pool_read_requests) should match the chart
```

---

### 0.6 Query Performance: show real SQL, not `?` placeholders

**Files:**
- `api/dashboard_routes.py:815-843` (`partial_query_detail`)
- `templates/partials/query_detail.html:10` (the query `<pre>` block)
- `templates/partials/query_detail.html:82` (the EXPLAIN copy-button)

**Dependencies:** none. The data is already stored.

**Steps:**

1. **dashboard_routes.py:817** — add `query_sample_text` to the SELECT:
   ```python
   query_info = query_single("""
       SELECT digest, digest_text, schema_name,
              MAX(query_sample_text) as query_sample_text,
              SUM(exec_count) as exec_count,
              AVG(avg_time_sec) as avg_time_sec,
              SUM(total_time_sec) as total_time_sec,
              SUM(rows_examined) as rows_examined,
              SUM(rows_sent) as rows_sent
       FROM query_digest_snapshots
       WHERE digest = ?
       GROUP BY digest
   """, (digest,))
   ```
   `MAX(query_sample_text)` is arbitrary — any non-null sample is fine. If you want the most recent sample, do a separate subquery with `ORDER BY snapshot_time DESC LIMIT 1`.

2. **query_detail.html:10** — render with fallback and a label:
   ```html
   <div class="flex items-center justify-between mb-1">
       <div class="text-xs font-heading text-pencil/50">Full Query</div>
       {% if query_info.query_sample_text %}
           <span class="text-xs px-2 py-0.5 border border-green-500 bg-green-50 wobbly-sm font-body">sample (real values)</span>
       {% else %}
           <span class="text-xs px-2 py-0.5 border border-pencil/30 bg-erased wobbly-sm font-body">pattern (placeholders)</span>
       {% endif %}
   </div>
   <div class="relative group">
       <pre class="bg-white border border-dashed border-erased p-3 text-sm overflow-x-auto font-mono wobbly-sm"
            style="font-family: monospace; white-space: pre-wrap; word-break: break-all;">{{ query_info.query_sample_text or query_info.digest_text }}</pre>
       ...
   </div>
   ```

3. **query_detail.html:82** — EXPLAIN copy-button should produce a runnable statement:
   ```html
   EXPLAIN {{ (query_info.query_sample_text or query_info.digest_text)[:500] }}
   ```
   (Raised from 200 to 500 chars; long WHERE clauses were getting truncated.)

**Tests:** `tests/test_query_detail.py` (new)
- Seed a `query_digest_snapshots` row with `query_sample_text='SELECT * FROM t WHERE id = 42'` and `digest_text='SELECT * FROM t WHERE id = ?'`.
- Call `partial_query_detail()` as a Starlette TestClient GET to `/dashboard/partials/query-detail/{digest}`.
- Assert the response HTML contains `id = 42` and contains `sample (real values)`.
- Seed another row with `query_sample_text=None` → assert it contains `id = ?` and `pattern (placeholders)`.

**Verify:**
```bash
pytest tests/test_query_detail.py -v
# Then visually: open /dashboard/queries, expand any row, check for real values
```

---

## Phase 1 — Incident Replay + Anomaly Persistence

---

### 1.1 Schema + retention for two new tables

**Files:**
- `storage/schema.sql` (append)
- `storage/retention.py` (add override support)
- `storage/migrations.py` (new migration)

**Dependencies:** 0.4 (server_id plumbing).

**Steps:**

1. **Append to `storage/schema.sql`:**
   ```sql
   -- ---------------------------------------------------------------------------
   -- 25. Anomaly Events
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
       direction       TEXT NOT NULL,
       severity        TEXT NOT NULL,
       incident_id     INTEGER
   );
   CREATE INDEX IF NOT EXISTS idx_anomaly_detected_at ON anomaly_events(detected_at);
   CREATE INDEX IF NOT EXISTS idx_anomaly_incident    ON anomaly_events(incident_id);
   CREATE INDEX IF NOT EXISTS idx_anomaly_server      ON anomaly_events(server_id, detected_at);

   -- ---------------------------------------------------------------------------
   -- 26. Incident Windows
   -- ---------------------------------------------------------------------------
   CREATE TABLE IF NOT EXISTS incident_windows (
       id               INTEGER PRIMARY KEY,
       server_id        TEXT NOT NULL DEFAULT 'default',
       start_time       TEXT NOT NULL,
       end_time         TEXT NOT NULL,
       severity         TEXT NOT NULL,
       involved_metrics TEXT NOT NULL,
       event_count      INTEGER NOT NULL DEFAULT 0,
       analysis_id      INTEGER,
       status           TEXT DEFAULT 'detected'
   );
   CREATE INDEX IF NOT EXISTS idx_incident_status ON incident_windows(status, start_time);
   CREATE INDEX IF NOT EXISTS idx_incident_server ON incident_windows(server_id, start_time);
   ```

2. **Add migration in `storage/migrations.py`.** Look at existing migrations in that file for the pattern. Add `migration_004_incident_tables()` that runs the two `CREATE TABLE IF NOT EXISTS` statements and registers in the migration log.

3. **Retention override in `storage/retention.py`.** Current impl likely takes a single `retention_days` from config. Add a per-table override dict:
   ```python
   PER_TABLE_OVERRIDES = {
       # Longer retention — incidents are postmortem evidence
       "incident_windows": 365,
       "anomaly_events": 90,
       "ddl_changes": 365,
       "agent_analyses": 180,
       "alert_history": 180,
   }

   def _retention_for(table: str, default: int) -> int:
       return PER_TABLE_OVERRIDES.get(table, default)
   ```
   Then in the cleanup loop, use `_retention_for(table, default_days)` when computing the cutoff.

4. **Config hook (optional).** Let YAML override the defaults:
   ```yaml
   retention:
     days: 90
     overrides:
       incident_windows: 365
       anomaly_events: 90
   ```
   Merge config values into `PER_TABLE_OVERRIDES` at startup.

**Tests:** `tests/test_retention_overrides.py` (new)
- Insert `anomaly_events` rows at timestamps 100 days ago and 30 days ago. Run retention with default 90 days. Assert the 100-day row is gone, 30-day row stays.
- Insert `incident_windows` rows at timestamps 200 days ago and 400 days ago. Run retention. Assert the 200-day row stays, 400-day row is gone.

**Verify:**
```bash
python main.py --init-db
sqlite3 data/mysql_monitor.db ".schema anomaly_events"
sqlite3 data/mysql_monitor.db ".schema incident_windows"
sqlite3 data/mysql_monitor.db ".indices anomaly_events"
```

---

### 1.2 `AnomalyResult.detected_at` + per-cycle cache

**Files:** `alerting/anomaly.py`

**Dependencies:** 0.4.

**The gap:** `detect_anomalies()` is called by both `agent/state_builder.py:83` and `alerting/anomaly.py:424` (`evaluate_anomaly`). Each call re-runs all baseline queries — duplicated work per medium cycle.

**Steps:**

1. **Add `detected_at` to `AnomalyResult`:**
   ```python
   @dataclass
   class AnomalyResult:
       metric: str
       current: float
       baseline_mean: float
       baseline_stddev: float
       z_score: float
       pct_change: float
       direction: str
       severity: str
       detected_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
       server_id: str = "default"
   ```

2. **Add module-level cache:**
   ```python
   # key: (server_id, z_threshold, cycle_minute) → list[AnomalyResult]
   _detect_cache: dict[tuple, list[AnomalyResult]] = {}
   _cache_lock = threading.Lock()

   def _cache_key(server_id: str, z_override: float | None) -> tuple:
       cycle_minute = datetime.utcnow().strftime("%Y-%m-%dT%H:%M")
       return (server_id, z_override, cycle_minute)
   ```

3. **Wrap `detect_anomalies` to consult the cache first:**
   ```python
   def detect_anomalies(
       server_id: str | None = None,
       z_threshold_override: float | None = None,
   ) -> list[AnomalyResult]:
       if server_id is None:
           from config.server_registry import get_server_registry
           server_id = get_server_registry().get_default_server_id()

       key = _cache_key(server_id, z_threshold_override)
       with _cache_lock:
           if key in _detect_cache:
               return _detect_cache[key]

       results = _detect_anomalies_uncached(server_id, z_threshold_override)

       with _cache_lock:
           # Trim old cache entries (keep last 3 cycle minutes only)
           if len(_detect_cache) > 10:
               _detect_cache.clear()
           _detect_cache[key] = results
       return results
   ```
   Move the current body of `detect_anomalies` into `_detect_anomalies_uncached`.

4. **Invalidation:** the cycle-minute key naturally invalidates when the wall clock ticks. No explicit invalidation needed. Clearing when the dict grows past 10 is a cheap guard against memory growth if someone calls with many different z-overrides.

**Tests:** `tests/test_anomaly_cache.py` (new)
- Mock `_detect_anomalies_uncached` with a counter.
- Call `detect_anomalies("A")` twice back-to-back → assert uncached impl called exactly once.
- Patch `datetime.utcnow` to advance by 2 minutes → call again → assert uncached impl called twice total.
- Call `detect_anomalies("B")` → assert uncached impl called again (different key).

**Verify:**
```bash
pytest tests/test_anomaly_cache.py -v
```

---

### 1.3 Anomaly event persistence

**Files:**
- `alerting/anomaly_store.py` (new)
- `storage/writer.py` (new writer)
- `scheduler/runner.py:70` (`_run_medium`)

**Dependencies:** 1.1, 1.2.

**Steps:**

1. **Create `alerting/anomaly_store.py`:**
   ```python
   """Persists AnomalyResult objects to the anomaly_events table."""

   import logging
   from alerting.anomaly import AnomalyResult
   from storage import writer

   logger = logging.getLogger(__name__)

   def persist(results: list[AnomalyResult]) -> list[int]:
       """
       Write anomaly results to the anomaly_events table.

       Returns the list of inserted row IDs for downstream grouping.
       """
       if not results:
           return []
       rows = [
           {
               "detected_at": r.detected_at,
               "server_id": r.server_id,
               "metric_name": r.metric,
               "current_value": r.current,
               "baseline_mean": r.baseline_mean,
               "baseline_stddev": r.baseline_stddev,
               "z_score": r.z_score,
               "pct_change": r.pct_change,
               "direction": r.direction,
               "severity": r.severity,
               "incident_id": None,
           }
           for r in results
       ]
       return writer.write_anomaly_events(rows)
   ```

2. **Add `write_anomaly_events` to `storage/writer.py`:**
   ```python
   def write_anomaly_events(rows: list[dict]) -> list[int]:
       if not rows:
           return []
       cols = ["detected_at", "server_id", "metric_name", "current_value",
               "baseline_mean", "baseline_stddev", "z_score", "pct_change",
               "direction", "severity", "incident_id"]
       placeholders = ", ".join(["?"] * len(cols))
       col_names = ", ".join(cols)
       sql = f"INSERT INTO anomaly_events ({col_names}) VALUES ({placeholders})"
       ids = []
       with get_mon_connection() as conn:
           for row in rows:
               values = tuple(_serialize_value(row.get(col)) for col in cols)
               cursor = conn.execute(sql, values)
               ids.append(cursor.lastrowid)
       return ids
   ```
   Individual inserts (not `executemany`) so we can capture `lastrowid` for each — required for 1.4 which needs to update `incident_id` on specific rows.

3. **Scheduler hook — `scheduler/runner.py:_run_medium`:**
   ```python
   def _run_medium():
       for ctx in _get_server_contexts():
           try:
               results = run_medium_loop(ctx)
               failures = [name for name, ok in results.items() if not ok]
               if failures:
                   logger.warning(f"Medium loop failures [{ctx.server_id}]: {failures}")
               # Anomaly detection + persistence (post-collection, pre-alert)
               _run_anomaly_pipeline(ctx.server_id)
           except Exception as e:
               logger.error(f"Medium loop failed for {ctx.server_id}: {e}")
       _run_alerts("medium")
       _update_prom_metrics()

   def _run_anomaly_pipeline(server_id: str):
       try:
           from alerting.anomaly import detect_anomalies
           from alerting.anomaly_store import persist
           from alerting.incidents import update_windows  # Phase 1.4
           results = detect_anomalies(server_id=server_id)
           for r in results:
               r.server_id = server_id
           persist(results)
           update_windows(server_id)
       except Exception as e:
           logger.warning(f"Anomaly pipeline failed for {server_id}: {e}")
   ```
   Note: `evaluate_anomaly` inside `_run_alerts` will call `detect_anomalies()` again, but 1.2's cache short-circuits it — zero duplicate cost.

**Tests:** `tests/test_anomaly_store.py` (new)
- Seed the mon DB with one `AnomalyResult` → call `persist()` → assert one row in `anomaly_events` with `incident_id IS NULL`.
- Seed N results → call `persist()` → assert `len(ids) == N` and all rows have correct values.

**Verify:**
```bash
python main.py --once
sqlite3 data/mysql_monitor.db "SELECT COUNT(*), MIN(detected_at), MAX(detected_at) FROM anomaly_events;"
```

---

### 1.4 Incident window builder

**Files:**
- `alerting/incidents.py` (new)
- `config/settings.yaml` (new config keys)

**Dependencies:** 1.3.

**Algorithm:** gap-based clustering with duration cap.

**Steps:**

1. **Config additions in `config/settings.yaml`:**
   ```yaml
   alerting:
     incident_gap_minutes: 15
     incident_max_duration_minutes: 120
   ```

2. **Create `alerting/incidents.py`:**
   ```python
   """Incident window builder — groups anomaly events into incidents."""

   import json
   import logging
   from typing import Any
   from config import get_config
   from storage.connection import get_mon_connection

   logger = logging.getLogger(__name__)

   _SEVERITY_RANK = {"warning": 1, "critical": 2}

   def update_windows(server_id: str) -> list[int]:
       """
       Process ungrouped anomaly events for a server.

       Returns IDs of any NEW incident windows created (for Slack notification).
       """
       cfg = get_config().get("alerting", {})
       gap_min = cfg.get("incident_gap_minutes", 15)
       max_dur_min = cfg.get("incident_max_duration_minutes", 120)

       new_ids = []
       with get_mon_connection() as conn:
           events = conn.execute("""
               SELECT id, detected_at, metric_name, severity
               FROM anomaly_events
               WHERE incident_id IS NULL AND server_id = ?
               ORDER BY detected_at ASC
           """, (server_id,)).fetchall()

           for event in events:
               incident_id = _attach_or_create(
                   conn, server_id, event, gap_min, max_dur_min
               )
               if incident_id not in new_ids and _is_new(conn, incident_id):
                   new_ids.append(incident_id)

       return new_ids

   def _attach_or_create(conn, server_id, event, gap_min, max_dur_min) -> int:
       """Attach event to an open incident or create a new one. All in one txn."""
       row = conn.execute("""
           SELECT id, start_time, end_time, severity, involved_metrics, event_count
           FROM incident_windows
           WHERE server_id = ?
             AND status = 'detected'
             AND datetime(end_time) >= datetime(?, ?)
             AND (julianday(end_time) - julianday(start_time)) * 1440 < ?
           ORDER BY end_time DESC
           LIMIT 1
       """, (server_id, event["detected_at"], f"-{gap_min} minutes", max_dur_min)).fetchone()

       if row:
           # Extend
           metrics = json.loads(row["involved_metrics"])
           if event["metric_name"] not in metrics:
               metrics.append(event["metric_name"])
           new_severity = row["severity"]
           if _SEVERITY_RANK[event["severity"]] > _SEVERITY_RANK[new_severity]:
               new_severity = event["severity"]
           conn.execute("""
               UPDATE incident_windows
               SET end_time = ?, severity = ?, involved_metrics = ?, event_count = event_count + 1
               WHERE id = ?
           """, (event["detected_at"], new_severity, json.dumps(metrics), row["id"]))
           incident_id = row["id"]
       else:
           # Create new
           cursor = conn.execute("""
               INSERT INTO incident_windows
                 (server_id, start_time, end_time, severity, involved_metrics, event_count, status)
               VALUES (?, ?, ?, ?, ?, 1, 'detected')
           """, (
               server_id,
               event["detected_at"],
               event["detected_at"],
               event["severity"],
               json.dumps([event["metric_name"]]),
           ))
           incident_id = cursor.lastrowid

       # Attach the event
       conn.execute(
           "UPDATE anomaly_events SET incident_id = ? WHERE id = ?",
           (incident_id, event["id"]),
       )
       return incident_id
   ```

   The entire `update_windows` runs inside one connection. SQLite is auto-transactioned by default — the `with get_mon_connection() as conn:` block commits at the end. For stronger atomicity per event, wrap `_attach_or_create` in an explicit `BEGIN IMMEDIATE` / `COMMIT`.

3. **`_is_new(conn, incident_id)`** — returns True if the incident has `event_count == 1` after the update (i.e. was just created). Needed so 1.11 Slack only fires once.

**Tests:** `tests/test_incidents.py` (new, comprehensive)
- **Empty:** no ungrouped events → `update_windows` returns `[]`, no rows in `incident_windows`.
- **Single event:** one ungrouped event → 1 incident with `event_count=1`, returned in `new_ids`.
- **Within gap:** two events 5 min apart → 1 incident with `event_count=2`, `new_ids` has 1 element.
- **Outside gap:** two events 20 min apart (gap=15) → 2 incidents.
- **Severity upgrade:** warning event then critical event within gap → incident severity = critical.
- **Duration cap:** events at 0min, 30min, 60min, 90min, 130min — with max_duration=120, the 130min event should NOT attach to the first incident (it exceeds the cap) → 2 incidents, not 1.
- **Multi-server isolation:** events on server A and server B → each gets its own incidents, no cross-contamination.
- **Metric merge:** 3 events with metrics `[qps, qps, lock_frequency]` → incident `involved_metrics = ["qps", "lock_frequency"]` (deduped).

**Verify:**
```bash
pytest tests/test_incidents.py -v
```

---

### 1.5 CLI refactor to argparse subparsers

**Files:** `main.py:193-226` (`main()` function)

**Dependencies:** none. Do this before 1.8/1.9 so `replay` and `incidents` have somewhere to live.

**Constraint:** backward compatibility. Every existing `--flag` must keep working for one release.

**Steps:**

1. **Rewrite `main()` to use subparsers with dual-path compatibility:**
   ```python
   def main():
       parser = argparse.ArgumentParser(prog="seeql", description="SeeQL — LLM-powered MySQL DBA agent")

       # Legacy flags (deprecated but still work)
       parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
       parser.add_argument("--init-db", action="store_true", help=argparse.SUPPRESS)
       parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
       parser.add_argument("--api", action="store_true", help=argparse.SUPPRESS)
       parser.add_argument("--api-only", action="store_true", help=argparse.SUPPRESS)

       subparsers = parser.add_subparsers(dest="cmd", metavar="<command>")

       subparsers.add_parser("check", help="Run health checks and exit")
       subparsers.add_parser("init-db", help="Initialize the monitoring database schema")

       run_p = subparsers.add_parser("run", help="Start the continuous collector")
       run_p.add_argument("--once", action="store_true", help="Run one cycle and exit")

       serve_p = subparsers.add_parser("serve", help="Start the API server")
       serve_p.add_argument("--no-scheduler", action="store_true", help="API only, no collection")

       subparsers.add_parser("init", help="Interactive setup wizard")        # Phase 2.4
       subparsers.add_parser("doctor", help="Diagnostic check")              # Phase 2.4
       subparsers.add_parser("demo", help="Run the bundled demo")            # Phase 2.3

       replay_p = subparsers.add_parser("replay", help="Replay an incident")  # Phase 1.8
       replay_p.add_argument("--from", dest="from_ts", help="Start timestamp (ISO8601)")
       replay_p.add_argument("--to", dest="to_ts", help="End timestamp (ISO8601)")
       replay_p.add_argument("--incident", type=int, help="Replay a specific incident_id")
       replay_p.add_argument("--latest", action="store_true", help="Replay the most recent incident")

       inc_p = subparsers.add_parser("incidents", help="Incident management")  # Phase 1.9
       inc_sub = inc_p.add_subparsers(dest="inc_cmd")
       list_p = inc_sub.add_parser("list")
       list_p.add_argument("--status", choices=["detected","analyzed","resolved"])
       list_p.add_argument("--limit", type=int, default=20)

       args = parser.parse_args()
       setup_logging()

       # Legacy flags win if set (deprecation warning)
       if args.check or args.init_db or args.once or args.api or args.api_only:
           logger = logging.getLogger(__name__)
           logger.warning("DEPRECATED: flag-style invocation. Use `seeql <cmd>` instead (e.g. `seeql check`). Flags will be removed in v0.2.0.")
           if args.check: return cmd_check()
           if args.init_db: return cmd_init_db()
           if args.once: return cmd_once()
           if args.api: return cmd_api(with_scheduler=True)
           if args.api_only: return cmd_api(with_scheduler=False)

       # Subcommand dispatch
       if args.cmd == "check": return cmd_check()
       if args.cmd == "init-db": return cmd_init_db()
       if args.cmd == "run":
           return cmd_once() if getattr(args, "once", False) else cmd_run()
       if args.cmd == "serve":
           return cmd_api(with_scheduler=not args.no_scheduler)
       if args.cmd == "replay": return cmd_replay(args)            # Phase 1.8
       if args.cmd == "incidents": return cmd_incidents(args)      # Phase 1.9
       if args.cmd == "demo": return cmd_demo()                    # Phase 2.3
       if args.cmd == "init": return cmd_init()                    # Phase 2.4
       if args.cmd == "doctor": return cmd_doctor()                # Phase 2.4

       # No subcommand → continuous run (same as before)
       cmd_run()
   ```

2. **Stub out `cmd_replay`, `cmd_incidents`, `cmd_demo`, `cmd_init`, `cmd_doctor`** — each just prints `"not yet implemented"` and exits 2. They get real bodies in 1.8, 1.9, 2.3, 2.4.

**Tests:** `tests/test_cli.py` (new)
- Invoke `main()` via `argparse.parse_args(['check'])` → expect `cmd_check` called.
- Invoke with `['--check']` → expect `cmd_check` called AND a deprecation warning logged.
- Invoke with `['run', '--once']` → expect `cmd_once` called.
- Invoke with `['serve', '--no-scheduler']` → expect `cmd_api(with_scheduler=False)`.
- Invoke with `['replay']` (no args) → expect usage error (argparse exits).

**Verify:**
```bash
python main.py check              # should behave like --check
python main.py --check            # should behave and log deprecation warning
python main.py run --once         # should behave like --once
python main.py replay --help      # should show replay subcommand help
```

---

### 1.6 Replay module + incident investigator prompt

**Files:**
- `agent/replay.py` (new)
- `agent/prompts.py` (append)
- `agent/queries.py` (append timeline queries)

**Dependencies:** 1.1, 1.4.

**Steps:**

1. **Append incident investigator prompt to `agent/prompts.py`:**
   ```python
   INCIDENT_INVESTIGATOR_PROMPT = """You are a senior MySQL DBA investigating a past incident.
   You receive:
     1. A chronological timeline of events in the incident window
     2. Access to tool calls that query historical data from that window

   Your job:
     1. Identify the **triggering event** — what was the first anomaly, and what caused it?
     2. Trace the **cascade** — how did the initial problem amplify into a larger incident?
     3. Identify the **specific query, lock, or DDL change** that was the root cause.
     4. Produce a **recommendation** with exact SQL or config changes that would have prevented it.
     5. Focus on historical data FIRST. Live tool calls are for gap-filling only.

   Output a Markdown report with sections: Timeline | Root Cause | Recommendation.
   Be specific. Cite event times and metric values. No hedging.

   Incident window:
     from: {from_ts}
     to:   {to_ts}
     server: {server_id}

   Timeline:
   {timeline}
   """
   ```

2. **Add timeline queries in `agent/queries.py`:**
   ```python
   TIMELINE_ANOMALIES = """
   SELECT detected_at, metric_name, current_value, baseline_mean, z_score, severity, direction
   FROM anomaly_events
   WHERE server_id = ? AND detected_at BETWEEN ? AND ?
   ORDER BY detected_at ASC
   """

   TIMELINE_LOCK_WAITS = """
   SELECT snapshot_time, waiting_pid, blocking_pid, wait_seconds, waiting_query, blocking_query
   FROM lock_wait_snapshots
   WHERE server_id = ? AND snapshot_time BETWEEN ? AND ?
   ORDER BY snapshot_time ASC
   """

   TIMELINE_DDL = """
   SELECT detected_at, table_schema, table_name, change_type, old_ddl, new_ddl
   FROM ddl_changes
   WHERE server_id = ? AND detected_at BETWEEN ? AND ?
   ORDER BY detected_at ASC
   """

   TIMELINE_GLOBAL_STATUS = """
   SELECT snapshot_time, variable_name, raw_value, per_second
   FROM global_status_snapshots
   WHERE server_id = ?
     AND variable_name IN ('Threads_running','Threads_connected','Questions','Innodb_row_lock_waits','Innodb_deadlocks')
     AND snapshot_time BETWEEN ? AND ?
   ORDER BY snapshot_time ASC
   """

   TIMELINE_TOP_QUERIES = """
   SELECT digest, digest_text, AVG(avg_time_sec) AS avg, SUM(exec_count) AS execs
   FROM query_digest_snapshots
   WHERE server_id = ? AND snapshot_time BETWEEN ? AND ?
   GROUP BY digest
   ORDER BY SUM(total_time_sec) DESC
   LIMIT 10
   """
   ```

3. **Create `agent/replay.py`:**
   ```python
   """Incident replay — chronological timeline + LLM root cause analysis."""

   import json
   import logging
   from dataclasses import dataclass, field
   from datetime import datetime
   from storage.connection import get_mon_reader
   from agent import queries as aq
   from agent.prompts import INCIDENT_INVESTIGATOR_PROMPT

   logger = logging.getLogger(__name__)

   @dataclass
   class ReplayResult:
       from_ts: str
       to_ts: str
       server_id: str
       incident_id: int | None
       timeline_md: str
       analysis_md: str | None = None
       analysis_id: int | None = None

       def to_markdown(self) -> str:
           hdr = f"# Incident Replay: {self.from_ts} — {self.to_ts} ({self.server_id})\n\n"
           out = hdr + "## Timeline\n\n" + self.timeline_md + "\n\n"
           if self.analysis_md:
               out += "## Root Cause Analysis\n\n" + self.analysis_md
           else:
               out += "## Root Cause Analysis\n\n*LLM analysis unavailable. Configure GCP credentials or ANTHROPIC_API_KEY for root cause narration.*"
           return out

   def _build_timeline(server_id: str, from_ts: str, to_ts: str) -> str:
       events = []
       with get_mon_reader() as conn:
           for row in conn.execute(aq.TIMELINE_ANOMALIES, (server_id, from_ts, to_ts)):
               events.append((row["detected_at"],
                   f"ANOMALY [{row['severity']}] {row['metric_name']}={row['current_value']:.2f} "
                   f"(baseline {row['baseline_mean']:.2f}, z={row['z_score']:.1f})"))
           for row in conn.execute(aq.TIMELINE_LOCK_WAITS, (server_id, from_ts, to_ts)):
               events.append((row["snapshot_time"],
                   f"LOCK pid={row['waiting_pid']} waiting {row['wait_seconds']}s for pid={row['blocking_pid']}"))
           for row in conn.execute(aq.TIMELINE_DDL, (server_id, from_ts, to_ts)):
               events.append((row["detected_at"],
                   f"DDL {row['change_type']} on {row['table_schema']}.{row['table_name']}"))

       events.sort(key=lambda e: e[0])
       if not events:
           return "*No events recorded in this window.*"
       return "\n".join(f"- `{ts}` — {msg}" for ts, msg in events)

   def run_replay(
       from_ts: str,
       to_ts: str,
       server_id: str | None = None,
       incident_id: int | None = None,
   ) -> ReplayResult:
       if server_id is None:
           from config.server_registry import get_server_registry
           server_id = get_server_registry().get_default_server_id()

       timeline_md = _build_timeline(server_id, from_ts, to_ts)
       result = ReplayResult(from_ts=from_ts, to_ts=to_ts, server_id=server_id,
                             incident_id=incident_id, timeline_md=timeline_md)

       # LLM analysis (Phase 1.7 wrapper)
       try:
           from agent.llm_agent import run_llm_analysis, _detect_backend
           from config import get_config
           if _detect_backend(get_config().get("agent", {})) is None:
               logger.info("No LLM backend configured — timeline-only replay")
               return result

           prompt = INCIDENT_INVESTIGATOR_PROMPT.format(
               from_ts=from_ts, to_ts=to_ts, server_id=server_id, timeline=timeline_md
           )
           llm = run_llm_analysis(
               prompt=prompt,
               tools=[],  # Start with no tools; enable after stability
               analysis_type="replay",
               server_id=server_id,
           )
           result.analysis_md = llm.get("text")
           result.analysis_id = llm.get("analysis_id")

           # Link analysis to incident window
           if incident_id and result.analysis_id:
               from storage.connection import get_mon_connection
               with get_mon_connection() as conn:
                   conn.execute(
                       "UPDATE incident_windows SET analysis_id = ?, status = 'analyzed' WHERE id = ?",
                       (result.analysis_id, incident_id),
                   )
       except Exception as e:
           logger.warning(f"LLM replay analysis failed: {e}")

       return result
   ```

**Tests:** `tests/test_replay.py` (new)
- **Fixture:** seed a synthetic lock cascade into a test DB (anomaly events, lock_waits, ddl_changes, global_status).
- **Timeline-only path:** mock `_detect_backend` to return None → run_replay → assert `analysis_md is None` and timeline contains all seeded events in chronological order.
- **LLM path:** mock `run_llm_analysis` to return `{"text": "...", "analysis_id": 42}` → run_replay → assert analysis_id set and `incident_windows.status = 'analyzed'`.
- **Empty window:** run_replay on a time range with no data → timeline contains `*No events recorded*`, no crash.

**Verify:**
```bash
pytest tests/test_replay.py -v
```

---

### 1.7 Public LLM wrapper for replay

**Files:** `agent/llm_agent.py`

**Dependencies:** 1.6.

**The gap:** `_run_claude_loop` and `_run_gemini_loop` are private. Replay needs a callable that accepts a prompt + tools, runs the right backend, writes to `agent_analyses`, and returns `{text, analysis_id}`.

**Steps:**

1. **Extract the backend-dispatch logic from `run_analysis()` into a reusable function.** Look at the existing `run_analysis()` around line 98 — it builds the user message, then dispatches to Gemini or Claude. Refactor so both `run_analysis` and a new `run_llm_analysis` share the dispatch.

2. **Add `run_llm_analysis`:**
   ```python
   def run_llm_analysis(
       prompt: str,
       tools: list | None = None,
       analysis_type: str = "replay",
       server_id: str | None = None,
   ) -> dict:
       """
       Public wrapper: run any LLM analysis with a custom prompt.

       Used by replay and (later) any other code that wants to invoke the LLM
       outside the scheduled `run_analysis` path.

       Returns:
           {"text": str, "analysis_id": int}
       """
       config = get_config().get("agent", {})
       backend = _detect_backend(config)
       if backend is None:
           raise RuntimeError("No LLM backend configured")

       if server_id is None:
           from config.server_registry import get_server_registry
           server_id = get_server_registry().get_default_server_id()

       from agent.tools import set_current_server
       set_current_server(server_id)

       max_tokens = config.get("max_tokens", 8192)
       max_tool_rounds = config.get("max_tool_rounds", 15)

       if backend["type"] == "claude":
           text = _run_claude_loop(
               client=backend["client"],
               model=backend["model"],
               system=SYSTEM_PROMPT,
               user_message=prompt,
               tools=tools or [],
               max_tokens=max_tokens,
               max_rounds=max_tool_rounds,
           )
       else:
           text = _run_gemini_loop(
               model=backend["model"],
               system=SYSTEM_PROMPT,
               user_message=prompt,
               tools=tools or [],
               max_rounds=max_tool_rounds,
           )

       # Parse findings (optional — replay doesn't need structured output)
       findings_json, recs_json = _try_parse_json(text)

       analysis_id = writer.write_agent_analysis({
           "analyzed_at": datetime.utcnow().isoformat(),
           "analysis_type": analysis_type,
           "severity": "info",  # Replay severity comes from the incident, not the analysis
           "input_summary": prompt[:500],
           "findings": findings_json,
           "recommendations": recs_json,
           "applied": 0,
       })

       return {"text": text, "analysis_id": analysis_id}
   ```

3. **`writer.write_agent_analysis`** — check if this exists. If not, add a thin wrapper that INSERTs into `agent_analyses` and returns `lastrowid`.

**Tests:** `tests/test_llm_wrapper.py` (new)
- Mock `_detect_backend` to return a stub with `type='claude'`.
- Mock `_run_claude_loop` to return a deterministic string.
- Call `run_llm_analysis(prompt="test")` → assert returns `{text, analysis_id}` and a row exists in `agent_analyses` with `analysis_type='replay'`.

**Verify:**
```bash
pytest tests/test_llm_wrapper.py -v
```

---

### 1.8 `seeql replay` CLI wiring

**Files:** `main.py` (`cmd_replay` stub from 1.5)

**Dependencies:** 1.5, 1.6.

**Steps:**

1. **Implement `cmd_replay(args)`:**
   ```python
   def cmd_replay(args):
       from agent.replay import run_replay
       from datetime import datetime, timedelta
       from storage.connection import get_mon_reader

       # Resolve from_ts/to_ts
       if args.latest:
           with get_mon_reader() as conn:
               row = conn.execute(
                   "SELECT id, start_time, end_time FROM incident_windows "
                   "ORDER BY start_time DESC LIMIT 1"
               ).fetchone()
           if not row:
               print("No incidents detected yet. Nothing to replay.")
               sys.exit(0)
           from_ts, to_ts, incident_id = row["start_time"], row["end_time"], row["id"]
       elif args.incident:
           with get_mon_reader() as conn:
               row = conn.execute(
                   "SELECT id, start_time, end_time FROM incident_windows WHERE id = ?",
                   (args.incident,),
               ).fetchone()
           if not row:
               print(f"ERROR: incident {args.incident} not found", file=sys.stderr)
               sys.exit(1)
           from_ts, to_ts, incident_id = row["start_time"], row["end_time"], row["id"]
       elif args.from_ts and args.to_ts:
           from_ts, to_ts, incident_id = args.from_ts, args.to_ts, None
       else:
           print("ERROR: must provide --from/--to, --incident, or --latest", file=sys.stderr)
           sys.exit(2)

       result = run_replay(from_ts, to_ts, incident_id=incident_id)
       print(result.to_markdown())
   ```

**Tests:** `tests/test_cli_replay.py` (new)
- Seed an incident window → invoke `main.py replay --latest` as a subprocess → assert exit 0, stdout contains the window timestamps.
- Invoke `main.py replay --incident 9999` (doesn't exist) → assert exit 1, stderr mentions "not found".
- Invoke `main.py replay` (no args) → assert exit 2.

**Verify:**
```bash
python main.py replay --latest
python main.py replay --from "2026-04-10T03:00" --to "2026-04-10T05:00"
```

---

### 1.9 `seeql incidents list`

**Files:** `main.py` (`cmd_incidents` stub from 1.5)

**Dependencies:** 1.5, 1.1.

**Steps:**

1. **Implement `cmd_incidents(args)`:**
   ```python
   def cmd_incidents(args):
       if args.inc_cmd != "list":
           print("Usage: seeql incidents list [--status STATUS] [--limit N]", file=sys.stderr)
           sys.exit(2)

       from storage.connection import get_mon_reader
       import json

       where = []
       params = []
       if args.status:
           where.append("status = ?")
           params.append(args.status)
       where_sql = ("WHERE " + " AND ".join(where)) if where else ""
       params.append(args.limit)

       sql = f"""
           SELECT id, server_id, start_time, end_time, severity,
                  involved_metrics, event_count, status
           FROM incident_windows
           {where_sql}
           ORDER BY start_time DESC
           LIMIT ?
       """
       with get_mon_reader() as conn:
           rows = conn.execute(sql, params).fetchall()

       if not rows:
           print("No incidents.")
           return

       # Aligned columns
       print(f"{'ID':<5} {'SEVERITY':<10} {'STATUS':<10} {'START':<25} {'EVENTS':<8} {'METRICS'}")
       print("-" * 100)
       for row in rows:
           metrics = ", ".join(json.loads(row["involved_metrics"]))
           print(f"{row['id']:<5} {row['severity']:<10} {row['status']:<10} "
                 f"{row['start_time']:<25} {row['event_count']:<8} {metrics}")
   ```

**Tests:** `tests/test_cli_incidents.py` (new)
- Seed 3 incidents with mixed statuses → `seeql incidents list` → assert all 3 in output.
- `seeql incidents list --status detected` → assert only detected rows.
- `seeql incidents list --limit 1` → assert only 1 row.

**Verify:**
```bash
python main.py incidents list
python main.py incidents list --status detected --limit 5
```

---

### 1.10 Dashboard incident timeline widget

**Files:**
- `api/dashboard_api.py` (new endpoint)
- `templates/partials/incidents_timeline.html` (new)
- `templates/dashboard/overview.html` (include the partial)

**Dependencies:** 1.1.

**Steps:**

1. **New endpoint in `api/dashboard_api.py`:**
   ```python
   @router.get("/incidents/recent")
   def incidents_recent(
       limit: int = QueryParam(default=10, le=50),
       status: str = QueryParam(default=None),
       server: str = QueryParam(default=None),
   ):
       """Recent incidents for the overview widget."""
       import json
       server = resolve_server_id(server)
       where = ["server_id = ?"]
       params: list = [server]
       if status:
           where.append("status = ?")
           params.append(status)
       sql = f"""
           SELECT id, start_time, end_time, severity, involved_metrics, event_count, status,
                  CAST((julianday(end_time) - julianday(start_time)) * 1440 AS INTEGER) AS duration_minutes
           FROM incident_windows
           WHERE {' AND '.join(where)}
           ORDER BY start_time DESC
           LIMIT ?
       """
       params.append(limit)
       rows = query_rows(sql, tuple(params))
       for row in rows:
           row["involved_metrics"] = json.loads(row["involved_metrics"])
       return rows
   ```

2. **New partial `templates/partials/incidents_timeline.html`:**
   ```html
   <div id="incidents-widget"
        hx-get="/api/v1/incidents/recent?limit=5"
        hx-trigger="load, every 30s"
        hx-swap="innerHTML"
        aria-live="polite"
        aria-label="Recent incidents">
     <!-- JS renders cards from the JSON response -->
   </div>

   <script>
     // Replace the naive HTMX JSON-to-HTML approach with a small JS renderer
     document.body.addEventListener('htmx:afterRequest', function(evt) {
       if (evt.target.id !== 'incidents-widget') return;
       const data = JSON.parse(evt.detail.xhr.responseText);
       if (!data.length) {
         evt.target.innerHTML = '<div class="text-pencil/40 font-body p-4">No incidents detected. Your database is behaving.</div>';
         return;
       }
       evt.target.innerHTML = data.map(i => `
         <div class="border-[3px] border-pencil p-3 wobbly-md shadow-hard-sm severity-${i.severity === 'critical' ? 'red' : 'yellow'}"
              style="transform: rotate(${(Math.random() * 0.6 - 0.3).toFixed(2)}deg)">
           <div class="flex justify-between items-start">
             <div>
               <div class="font-heading text-lg">Incident #${i.id}</div>
               <div class="text-xs text-pencil/60">${i.start_time} → ${i.end_time} (${i.duration_minutes} min)</div>
             </div>
             <div class="text-xs px-2 py-1 bg-pencil text-white wobbly-sm">${i.severity}</div>
           </div>
           <div class="mt-2 flex flex-wrap gap-1 text-xs font-body">
             ${i.involved_metrics.map(m => `<span class="px-2 py-0.5 border border-pencil wobbly-sm">${m}</span>`).join('')}
           </div>
           <div class="mt-2 font-mono text-xs text-pencil/50">${i.event_count} events</div>
         </div>
       `).join('');
     });
   </script>
   ```

3. **Include in `templates/dashboard/overview.html`:** add `{% include "partials/incidents_timeline.html" %}` in the incident widget position (after active alerts).

**Tests:** `tests/test_incidents_api.py` (new)
- Seed 3 incidents → GET `/api/v1/incidents/recent` → assert 3 items with correct shape (`involved_metrics` is a list, not a string).
- GET with `?status=detected` → assert filtering works.
- GET with `?server=other` → assert no results (seeded for default server).

**Verify:**
```bash
curl -s http://localhost:8080/api/v1/incidents/recent | jq '.'
# Open /dashboard/overview in a browser, confirm the widget renders and refreshes
```

---

### 1.11 Slack notification for new incidents

**Files:**
- `alerting/incidents.py` (modify `update_windows`)
- `alerting/channels.py` (reuse existing SlackChannel)

**Dependencies:** 1.4.

**Steps:**

1. **Modify `update_windows` to call Slack for new incidents:**
   ```python
   def update_windows(server_id: str) -> list[int]:
       # ... existing code returning new_ids ...

       # Notify Slack for new incidents
       if new_ids:
           _notify_slack(new_ids, server_id)

       return new_ids

   def _notify_slack(new_ids: list[int], server_id: str):
       from alerting.channels import SlackChannel
       from alerting.models import Alert, Severity
       from config import get_config

       cfg = get_config().get("alerting", {}).get("channels", {}).get("slack", {})
       if not cfg.get("enabled") or not cfg.get("webhook_url"):
           return
       if cfg["webhook_url"].startswith("${"):
           return  # Unresolved env var

       channel = SlackChannel(cfg["webhook_url"])

       with get_mon_reader() as conn:
           for incident_id in new_ids:
               row = conn.execute("""
                   SELECT id, start_time, severity, involved_metrics
                   FROM incident_windows WHERE id = ?
               """, (incident_id,)).fetchone()
               if not row:
                   continue
               metrics = ", ".join(json.loads(row["involved_metrics"]))
               severity = Severity.CRITICAL if row["severity"] == "critical" else Severity.WARNING
               alert = Alert(
                   rule_name=f"incident_detected:{server_id}",
                   severity=severity,
                   message=(f":rotating_light: Incident detected — {row['severity']}\n"
                            f"Metrics: {metrics}\n"
                            f"Started: {row['start_time']}\n"
                            f"Run: `python main.py replay --incident {row['id']}`"),
                   context={"incident_id": row["id"], "server_id": server_id},
               )
               channel.send(alert)
   ```

2. **Verify only-on-new semantics.** The current `update_windows` returns `new_ids` only when `event_count == 1` immediately after insert. Make sure `_is_new` from 1.4 correctly distinguishes "freshly created" from "extended with 1 event."

**Tests:** `tests/test_incidents_slack.py` (new)
- Monkeypatch `SlackChannel.send` to capture calls.
- Seed one anomaly event → run `update_windows` → assert `send()` called once with the expected message body.
- Seed another event within the gap → run `update_windows` → assert `send()` NOT called (extension, not new).
- Seed another event after the gap → `send()` called once more.

**Verify:**
```bash
pytest tests/test_incidents_slack.py -v
```

---

### 1.12 State builder — Recent Incidents section

**Files:** `agent/state_builder.py`

**Dependencies:** 1.1, 1.4.

**Steps:**

1. **Add a section to the state report.** Find the section-builder pattern (grep `_build_current_state`, `_build_changes`, `_build_historical`) and add `_build_incidents`:
   ```python
   def _build_incidents(conn, server_id: str) -> list[dict]:
       rows = conn.execute("""
           SELECT id, start_time, end_time, severity, involved_metrics, event_count, status
           FROM incident_windows
           WHERE server_id = ?
             AND status != 'resolved'
             AND datetime(start_time) >= datetime('now', '-24 hours')
           ORDER BY start_time DESC
       """, (server_id,)).fetchall()
       return [dict(r) for r in rows]
   ```

2. **Include in the report.** Add an `incidents` field to the state report dataclass and populate it in `build_state_report`. Render in the `to_markdown()` output:
   ```
   ## Recent Incidents (last 24h, unresolved)
   - #42 [critical] 03:12—03:47 (35min), metrics: threads_running, lock_frequency, 8 events [detected]
   - #41 [warning]  01:05—01:18 (13min), metrics: qps, 4 events [analyzed]
   ```

**Tests:** update `tests/test_state_builder.py`
- Seed one incident in the last hour → assert the report includes "Recent Incidents" section with the row.
- No incidents → section says "No unresolved incidents in the last 24 hours."

**Verify:**
```bash
python -c "from agent.state_builder import build_state_report; print(build_state_report().to_markdown())" | grep -A 5 "Recent Incidents"
```

---

## Phase 2 — DX Hardening for OSS Launch

Items 2.1–2.10 are grouped as "shippable bundles." Each bundle has a one-PR scope.

---

### 2.1 Packaging & distribution

**Files:** `pyproject.toml`, `LICENSE`, `CONTRIBUTING.md`, `CHANGELOG.md`

**Steps:**

1. **`pyproject.toml` — add entry point:**
   ```toml
   [project.scripts]
   seeql = "main:main"
   ```
2. **`LICENSE`** — Apache 2.0 full text (copy from https://www.apache.org/licenses/LICENSE-2.0.txt). Update the copyright line with the year and holder.
3. **`CONTRIBUTING.md`** — keep it simple:
   - Dev setup (`make dev` or `pip install -e .[dev]`)
   - Run tests (`pytest`)
   - Lint (`ruff check .`)
   - Commit format + PR checklist
   - Docker Hub credentials note (for maintainers doing releases)
4. **`CHANGELOG.md`** — Keep a Changelog format, start at `## [Unreleased]`:
   ```
   # Changelog
   All notable changes to SeeQL will be documented here.
   The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
   and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

   ## [Unreleased]

   ## [0.1.0] — 2026-MM-DD
   ### Added
   - Initial public release
   - 19 collectors across 3 loops
   - LLM agent layer with Gemini (Vertex AI) and Claude (Anthropic) backends
   - Anomaly detection + incident replay
   ```

**Verify:**
```bash
pip install -e .
seeql check  # should work via the entry point
```

---

### 2.2 Docker Hub publication

**Files:** `.github/workflows/ci.yml`, `.github/workflows/release.yml`

**Steps:**

1. **`.github/workflows/ci.yml`:**
   ```yaml
   name: CI
   on:
     push:
       branches: [main]
     pull_request:
   jobs:
     test:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: actions/setup-python@v5
           with:
             python-version: '3.12'
         - run: pip install -e .[dev]
         - run: ruff check .
         - run: pytest -v
   ```
2. **`.github/workflows/release.yml`:**
   ```yaml
   name: Release
   on:
     push:
       tags: ['v*']
   jobs:
     docker:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: docker/setup-buildx-action@v3
         - uses: docker/login-action@v3
           with:
             username: ${{ secrets.DOCKERHUB_USERNAME }}
             password: ${{ secrets.DOCKERHUB_TOKEN }}
         - uses: docker/build-push-action@v5
           with:
             push: true
             tags: |
               seeql/seeql:latest
               seeql/seeql:${{ github.ref_name }}
   ```
3. **Maintainer setup** (document in CONTRIBUTING.md): create `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` secrets in the repo settings.

**Verify:**
```bash
git tag v0.1.0-rc1
git push origin v0.1.0-rc1
# Watch GitHub Actions; confirm image pushed to hub.docker.com/r/seeql/seeql
```

---

### 2.3 Magical moment — `seeql demo`

**Files:**
- `main.py` (`cmd_demo`)
- `examples/demo.db` (new, ~5 MB fixture)
- `scripts/build_demo_db.py` (new, regenerates the fixture)

**Dependencies:** 1.5 (CLI subparsers), 1.8 (`replay` already works), 2.1 (entry point).

**Steps:**

1. **Fixture generator `scripts/build_demo_db.py`** — seeds 48h of synthetic data into a temp SQLite DB, including one embedded lock cascade incident. Key shapes:
   - `global_status_snapshots`: `Threads_running` walking through a normal-looking baseline 8–15, then climbing to 85 over 15 minutes.
   - `lock_wait_snapshots`: 0 rows for 47h45m, then 20 rows in 15 minutes with max wait climbing from 2s to 45s.
   - `query_digest_snapshots`: one "batch aggregation" query appearing with growing exec_count.
   - `ddl_changes`: one DDL change 2h before the incident.
   - `anomaly_events`: ~10 events clustered in the 15-minute window.
   - `incident_windows`: 1 row with `status='detected'`.
2. **Commit `examples/demo.db`** to the repo. ~5 MB is acceptable.
3. **`cmd_demo()`** in `main.py`:
   ```python
   def cmd_demo():
       import shutil
       import tempfile
       import webbrowser
       from pathlib import Path

       demo_src = Path(__file__).parent / "examples" / "demo.db"
       if not demo_src.exists():
           print("ERROR: examples/demo.db missing. Run scripts/build_demo_db.py.", file=sys.stderr)
           sys.exit(1)

       tmp = Path(tempfile.mkdtemp()) / "seeql_demo.db"
       shutil.copy(demo_src, tmp)
       os.environ["SEEQL_MON_DB_PATH"] = str(tmp)
       os.environ["SEEQL_DEMO_MODE"] = "1"

       # Print the replay for the bundled incident
       print("=" * 60)
       print("SeeQL Demo — Incident Replay")
       print("=" * 60)
       from agent.replay import run_replay
       result = run_replay(from_ts="<bundled_start>", to_ts="<bundled_end>", incident_id=1)
       print(result.to_markdown())
       print("=" * 60)
       print("\nStarting dashboard at http://localhost:8080")
       print("=" * 60)

       try:
           webbrowser.open("http://localhost:8080")
       except Exception:
           pass

       cmd_api(with_scheduler=False)  # Serve the demo, no collection
   ```
4. **Guard against running against real data.** Check `SEEQL_DEMO_MODE` in any code path that could write to MySQL. Demo mode must never issue an outbound MySQL query.

**Tests:** `tests/test_demo.py`
- Spawn `seeql demo` as a subprocess → wait 3s → curl `/api/v1/incidents/recent` → assert 1 incident.
- Assert stdout contains "Incident Replay" and "Timeline".

**Verify:**
```bash
seeql demo
# In a browser: http://localhost:8080 should show the incident widget
```

---

### 2.4 Onboarding — `seeql init` + `seeql doctor` + graceful perf_schema

**Files:**
- `main.py` (`cmd_init`, `cmd_doctor`)
- `seeql/init.py` (new module, interactive wizard)
- `seeql/doctor.py` (new module, diagnostic checks)
- `collectors/base.py` (graceful perf_schema check)

**Steps:**

1. **`seeql/init.py`** — 4-question interactive wizard:
   - Cloud SQL host?
   - Cloud SQL instance name (project:region:instance)?
   - MySQL username + password (stored in `settings.local.yaml`, NOT env)?
   - LLM provider (gemini / claude / none)?
   
   Writes `settings.local.yaml`, probes MySQL connectivity, generates `docker-compose.yml` from a template, prints next steps.

2. **`seeql/doctor.py`** — 7 checks:
   | Check | How |
   |-------|-----|
   | MySQL reachable | `SELECT 1` |
   | performance_schema on | `SHOW VARIABLES LIKE 'performance_schema'` |
   | dba_agent user exists | `SHOW GRANTS FOR 'dba_agent'@...` |
   | GCP ADC configured | `google.auth.default()` |
   | LLM credentials valid | provider-specific ping |
   | SQLite writeable | `touch data/.doctor_test` |
   | Disk space | `shutil.disk_usage(data/)` |
   
   Each emits a row: `[PASS] check name` or `[FAIL] check name → hint`. Exit code = number of failures.

3. **Graceful perf_schema degradation.** In `collectors/base.py` or `BaseCollector.run()`, probe `performance_schema` on startup. If disabled, set a module flag `PERF_SCHEMA_AVAILABLE=False` and skip dependent collectors (query_digests, wait_events, table_io, execution_stages) with a warning: `"Running in limited mode — enable performance_schema for full observability."` Non-perf_schema collectors (processlist via `information_schema`, InnoDB metrics, global status) keep working.

**Tests:**
- `tests/test_doctor.py`: mock each check, assert exit codes.
- `tests/test_init.py`: drive the wizard via stdin, assert `settings.local.yaml` gets written correctly.
- `tests/test_limited_mode.py`: mock `performance_schema=off` → assert dependent collectors are skipped, others run.

**Verify:**
```bash
seeql init        # follow prompts on a clean machine
seeql doctor      # should be green after init
```

---

### 2.5 Error catalog (E001–E010)

**Files:** `seeql/errors.py` (new), `docs/errors/E001.md` through `E010.md`

**Steps:**

1. **`seeql/errors.py`** — structured error class + catalog:
   ```python
   @dataclass
   class SeeQLError(Exception):
       code: str            # E001, E002, ...
       problem: str
       cause: str
       fix: str
       docs_url: str

       def format(self) -> str:
           return (
               f"\nerror[{self.code}]: {self.problem}\n"
               f"  = cause: {self.cause}\n"
               f"  = fix:   {self.fix}\n"
               f"  = docs:  {self.docs_url}\n"
           )

   ERRORS = {
       "E001": SeeQLError("E001",
           "MySQL authentication failed",
           "The dba_agent user cannot log in (wrong password or host restriction).",
           "Verify PROD_DB_PASSWORD env var and `SHOW GRANTS FOR 'dba_agent'@<host>`.",
           "https://docs.seeql.dev/errors/E001"),
       # ...
   }
   ```
2. **Wire into existing code paths.** Find every `print("ERROR:")` and `raise Exception(...)` in `main.py`, `storage/connection.py`, `collectors/base.py` and replace with `raise ERRORS["EXYZ"]`. Catch in `main()` and print the formatted block before exiting non-zero.
3. **`docs/errors/EXYZ.md`** — one page per code, 100–200 words, with the full problem + cause + fix + concrete example.

**Tests:**
- `tests/test_errors.py`: for each code, trigger the error condition in a fixture and assert the formatted output contains the code + fix.

**Verify:**
```bash
PROD_DB_PASSWORD=wrong seeql check
# Should print error[E001]: ... with the fix suggestion, exit non-zero
```

---

### 2.6 API contract + docs

**Files:** `api/app.py`, `api/dashboard_routes.py`, `api/dashboard_api.py`, `config.py`

**Steps:**

1. **Unified `/api/v1/*` paths.** Phase 1.10 already uses this prefix (`api/dashboard_api.py:8`). Audit `api/dashboard_routes.py` for any `/data/*` or `/collect/*` routes and add v1 aliases. Log a deprecation warning via middleware when old paths are hit.
2. **Document FastAPI `/docs`.** Add `tags`, `summary`, and docstrings to every route handler:
   ```python
   @router.get("/queries/top",
       summary="Top-N queries by metric",
       response_description="Array of query digest records",
       tags=["queries"])
   def queries_top(...):
       """
       Returns the top-N queries in a time range, sorted by the chosen metric.

       Example:
           GET /api/v1/queries/top?range=1h&sort=total_time_sec&limit=10
       """
   ```
   Then mention `http://localhost:8080/docs` in the README.
3. **Config deprecation shim in `config.py`:**
   ```python
   def deprecated_env(old: str, new: str, remove_in: str):
       """Warn if old env var is set; copy to new if new is unset."""
       if old in os.environ:
           logger.warning(
               f"DEPRECATED: env var {old} will be removed in {remove_in}; use {new} instead"
           )
           if new not in os.environ:
               os.environ[new] = os.environ[old]

   # At startup
   deprecated_env("DBA_AGENT_MYSQL_HOST", "SEEQL_MYSQL_HOST", "v0.3.0")
   ```

**Tests:**
- `tests/test_api_docs.py`: GET `/docs` → assert 200 and contains "queries/top".
- `tests/test_deprecation.py`: set old env var → assert warning logged and new env var populated.

**Verify:**
```bash
curl -s http://localhost:8080/docs | grep -q "SeeQL" && echo OK
```

---

### 2.7 Dashboard onboarding status page

**Files:** `api/dashboard_routes.py`, `templates/dashboard/onboarding.html` (new)

**Steps:**

1. **Middleware or route guard.** On every dashboard GET, check `SELECT COUNT(*) FROM query_digest_snapshots`. If 0, redirect to `/onboarding`.
2. **`/onboarding` endpoint:**
   - Query collector health (last successful run per collector from logs or a new `collector_health` table).
   - Compute progress: `(hours_collected / 48) * 100` for baseline completeness.
   - Render `templates/dashboard/onboarding.html` with the sketch aesthetic.
3. **Template:** progress bar, collector checklist (14/19 active → green checkmarks), data freshness per collector ("query_digests: 3 min ago").
4. **Bounce-back to `/overview`** when data arrives — HTMX poll `/api/v1/onboarding/status` every 5s, redirect when `ready == true`.

**Tests:**
- `tests/test_onboarding.py`: empty DB → GET `/` redirects to `/onboarding`. Seed one row → GET `/onboarding/status` returns `ready=true`.

**Verify:**
```bash
rm data/mysql_monitor.db && seeql init-db && seeql serve
# Browser: should land on /onboarding
```

---

### 2.8 Dev environment

**Files:** `docker-compose.dev.yml`, `Makefile`

**Steps:**

1. **`docker-compose.dev.yml`** — MySQL 8.0 with `performance_schema=on` + SeeQL + Prometheus:
   ```yaml
   services:
     mysql:
       image: mysql:8.0
       command: >
         --performance_schema=on
         --innodb_monitor_enable=all
         --slow_query_log=on
         --long_query_time=1
       environment:
         MYSQL_ROOT_PASSWORD: devroot
         MYSQL_DATABASE: shop
       volumes:
         - ./examples/sample_schema.sql:/docker-entrypoint-initdb.d/01.sql
       ports: ["3306:3306"]
     seeql:
       build: .
       depends_on: [mysql]
       environment:
         PROD_DB_PASSWORD: devroot
         SEEQL_MYSQL_HOST: mysql
       ports: ["8080:8080"]
     prometheus:
       image: prom/prometheus
       volumes:
         - ./examples/prometheus.yml:/etc/prometheus/prometheus.yml
       ports: ["9090:9090"]
   ```
2. **`Makefile` targets:**
   ```makefile
   dev:
       docker compose -f docker-compose.dev.yml up

   dev-reset:
       docker compose -f docker-compose.dev.yml down -v
   ```

**Verify:**
```bash
make dev     # Everything up in <2 minutes on a clean clone
```

---

### 2.9 Docs restructure + tutorial

**Files:** `docs/` subdirectory, `examples/`

**Steps:**

1. Create directory structure:
   ```
   docs/
     getting-started.md
     tutorial-first-incident.md
     configuration.md
     cloud-sql-setup.md
     architecture.md
     errors/
       E001.md ... E010.md
   examples/
     docker-compose.yml
     prometheus.yml
     sample_schema.sql
     demo.db
   ```
2. **`docs/tutorial-first-incident.md`** — walk through `seeql demo`, the bundled lock cascade, the replay output. Include expected output snippets so readers can check their progress.
3. Move `CLAUDE.md`, `FRONTEND_CLAUDE.md`, `PLAN.md`, `IMPLEMENTATION.md`, `TODOS.md` into `internal/` (or leave at root — ask user).

**Verify:**
Every markdown link resolves: run a link checker like `lychee docs/ README.md`.

---

### 2.10 README overhaul

**Files:** `README.md`

**Steps:**

1. **Top of README (above the fold):**
   ```markdown
   # SeeQL
   LLM-powered MySQL DBA agent that reasons about performance and replays incidents.

   ![Demo](docs/assets/demo.gif)

   ## Try it in 30 seconds
   ```bash
   docker run -p 8080:8080 seeql/seeql demo
   ```
   Then open http://localhost:8080.

   <details>
   <summary>Running against your own MySQL</summary>
   ...
   </details>
   ```
2. Record the demo GIF after 2.3 is complete. Tool: `asciinema` + `agg`, or a screen recorder.

**Verify:**
A fresh reader times themselves from README to dashboard — target <2 minutes.

---

### 2.11.1 Global time range picker + custom from/to

**Files:**
- `templates/partials/time_range_picker.html` (new)
- `templates/base.html` (include)
- `api/query_helpers.py:49` (`parse_time_range`)
- Every `/api/v1/*` endpoint that takes `range`

**Dependencies:** none.

**Steps:**

1. **Extract the pills** from `templates/dashboard/queries.html:14-21` into `templates/partials/time_range_picker.html`. Make the target URL a Jinja variable so it works on any page.
2. **Include in `templates/base.html`** header so it appears on every dashboard page.
3. **Extend `parse_time_range`** in `api/query_helpers.py`:
   ```python
   def parse_time_range(
       range_str: str | None = None,
       from_ts: str | None = None,
       to_ts: str | None = None,
   ) -> tuple[str, str]:
       if from_ts and to_ts:
           return from_ts, to_ts
       delta = RANGE_MAP.get(range_str or "24h", RANGE_MAP["24h"])
       now = datetime.now(timezone.utc)
       return (now - delta).isoformat(), now.isoformat()
   ```
4. **Every endpoint in `api/dashboard_api.py`** that takes `range: str = QueryParam(...)` must also accept `from_ts` / `to_ts`:
   ```python
   @router.get("/queries/top")
   def queries_top(
       range: str = QueryParam(default="24h"),
       from_ts: str = QueryParam(default=None, alias="from"),
       to_ts: str = QueryParam(default=None, alias="to"),
       ...
   ):
       start, end = parse_time_range(range, from_ts, to_ts)
   ```
5. **Custom picker UI** — two datetime inputs + a "Custom" pill button. On submit, push to URL and reload.

**Tests:**
- `tests/test_time_range.py`: assert `parse_time_range(range='1h')` returns ~1h ago, `parse_time_range(from_ts='2026-04-10T03:00', to_ts='2026-04-10T05:00')` returns exactly those strings.
- UI test (Playwright or manual): switch range on Overview → charts refresh without page reload.

**Verify:**
```bash
curl -s 'http://localhost:8080/api/v1/queries/top?from=2026-04-01T00:00&to=2026-04-02T00:00' | jq length
```

---

### 2.11.2 Schema & table filter

**Files:**
- `api/dashboard_api.py` (new `/schemas` endpoint + filter params on existing endpoints)
- `api/query_helpers.py` (filter helper)
- `templates/partials/schema_picker.html` (new)
- `templates/base.html` (include next to time picker)

**Dependencies:** 2.11.1 (shared header pattern).

**Steps:**

1. **New endpoint `/api/v1/schemas`:**
   ```python
   @router.get("/schemas")
   def list_schemas(server: str = QueryParam(default=None)):
       server = resolve_server_id(server)
       sql = """
           SELECT DISTINCT schema_name AS name
           FROM query_digest_snapshots
           WHERE server_id = ? AND schema_name IS NOT NULL
           UNION
           SELECT DISTINCT object_schema AS name
           FROM table_io_snapshots
           WHERE server_id = ? AND object_schema IS NOT NULL
       """
       schemas = [r["name"] for r in query_rows(sql, (server, server))]

       sql2 = """
           SELECT DISTINCT object_schema AS schema, object_name AS name
           FROM table_io_snapshots
           WHERE server_id = ?
           UNION
           SELECT DISTINCT table_schema AS schema, table_name AS name
           FROM schema_snapshots
           WHERE server_id = ?
       """
       tables = query_rows(sql2, (server, server))
       return {"schemas": schemas, "tables": tables}
   ```
   Cache result in-process for 60s via `functools.lru_cache` + a ttl wrapper.

2. **Filter helper in `query_helpers.py`:**
   ```python
   def _schema_filter(schema: str | None, table: str | None,
                      schema_col: str = "schema_name",
                      table_col: str | None = None) -> tuple[str, list]:
       clauses = []
       params = []
       if schema:
           clauses.append(f"{schema_col} = ?")
           params.append(schema)
       if table and table_col:
           clauses.append(f"{table_col} = ?")
           params.append(table)
       return (" AND " + " AND ".join(clauses)) if clauses else "", params
   ```

3. **Add filter to `queries_top`, `query_regressions`, `locks_history`, `table_sizes`.** For `locks_history`, filter is "approximate" — use `waiting_query LIKE '%table_name%'` as a best-effort. Label the response with `"approximate": true`.

4. **New endpoint `/api/v1/tables/{schema}/{table}/io`** — returns `table_io_snapshots` time series for a single table.

5. **UI component** `templates/partials/schema_picker.html` — two dependent dropdowns (schema → tables for that schema). AlpineJS for the cascade.

**Tests:** `tests/test_schema_filter.py` (new)
- Seed 2 schemas with distinct query digests → GET `/api/v1/queries/top?schema=a` → assert only schema-a rows.
- GET `/api/v1/schemas` → assert both schemas listed.
- GET `/api/v1/tables/shop/loyalty_members/io` → assert time series.

**Verify:**
```bash
curl -s 'http://localhost:8080/api/v1/queries/top?schema=shop&table=loyalty_members' | jq length
```

---

### 2.11.3 Threads chart — highlight `running`

**Files:**
- `templates/dashboard/overview.html` (KPI card)
- Overview chart JS (find the Chart.js config for the threads chart)

**Steps:**

1. **KPI card HTML:**
   ```html
   <div class="kpi-card">
     <div class="label">Active Threads</div>
     <div class="flex items-baseline gap-2">
       <div class="text-5xl font-heading font-bold text-marker">{{ threads_running }}</div>
       <div class="text-lg font-body text-pencil/60">running</div>
     </div>
     <div class="text-sm font-body text-pencil/60">
       / {{ threads_connected }} connected (pool)
     </div>
   </div>
   ```

2. **Chart.js config:** two datasets with different visual weight:
   ```js
   datasets: [
     { label: 'running (load)', data: running, borderColor: '#ff4d4d', borderWidth: 2.5, tension: 0.3 },
     { label: 'connected (pool)', data: connected, borderColor: '#2d5da1', borderWidth: 1, borderDash: [4, 4], tension: 0.3 }
   ]
   ```

3. **Baseline band.** Fetch baseline avg from the last 24h, compute `3 * baseline`, draw a horizontal annotation line (`chartjs-plugin-annotation`) at that value.

4. **Hover tooltip explainer** — custom tooltip callback that appends the explanation text for `running` only.

**Tests:** manual visual check — snapshot the chart after seeding an elevated `running` series and confirm the red band + bold red line.

**Verify:**
Open `/overview` during traffic; confirm running is visually dominant.

---

## Phase 3 — Dashboard Polish & Accessibility

Each item is small and independent. One PR per item is fine.

---

### 3.1 Accessibility pass

**Files:** `templates/base.html`, every partial with HTMX auto-refresh, all Chart.js usage sites.

**Steps:**

1. **Skip-to-content** — add to `base.html` before `<nav>`:
   ```html
   <a href="#main-content" class="sr-only focus:not-sr-only absolute top-2 left-2 bg-postit px-3 py-1 border-2 border-pencil wobbly-sm z-50">
     Skip to content
   </a>
   ```
   Wrap the main content in `<main id="main-content">`.

2. **ARIA live regions** — grep `hx-trigger="every` and add `aria-live="polite"` to each parent container.

3. **Canvas labels** — for every `<canvas>`, add `aria-label` that JS updates when data re-renders:
   ```js
   canvas.setAttribute('aria-label', `QPS chart showing ${latest} queries per second over the last ${range}`);
   ```

4. **Info tooltip keyboard support** — add `tabindex="0"`, `role="tooltip"`, and a `:focus` CSS rule mirroring `:hover` display. Find all `.info-tip` usages.

5. **44×44 touch targets** — audit pagination buttons (`px-3 py-1`) and bump to `px-4 py-3` where under 44×44.

**Tests:** manual axe-core run, target ≥90 Lighthouse a11y on `/overview`.

**Verify:**
```bash
# Run Lighthouse CLI
lighthouse http://localhost:8080/overview --only-categories=accessibility --chrome-flags="--headless"
```

---

### 3.2 First-run onboarding state (per-component)

**Files:** every partial that shows "All quiet" / "All clear" defaults.

**Steps:**

1. **Health bar** — check `query_digest_snapshots` count. If 0, render `waiting` state (neutral gray):
   ```html
   {% if zero_data %}
     <div class="severity-neutral">WAITING — Waiting for first data collection...</div>
   {% endif %}
   ```
2. **KPI cards** — add subtext "data arrives after first collection cycle (~30s)" when value is `—`.
3. **Charts** — replace "No data yet" with "Collecting baseline data..." when zero_data.
4. **Active alerts** — show "Agent is starting up. First collection in ~30 seconds."
5. **Action Center** — show "No data yet. The agent needs a few collection cycles..."

Pass `zero_data` into every template via a shared context injector in `api/dashboard_routes.py`.

**Tests:**
- `tests/test_first_run.py`: seed empty DB → GET `/overview` → assert response contains "WAITING" and "Collecting baseline data".

**Verify:**
```bash
rm data/mysql_monitor.db && seeql init-db && seeql serve
# Every dashboard page should show waiting states, never a false "healthy"
```

---

### 3.3 Chart loading + error states

**Files:** wherever `fetchAndChart` (or similar) is defined.

**Steps:**

1. **Loading skeleton** — 3 wavy dashed lines animating via CSS keyframes. Show while fetch is in-flight.
2. **Error state** — catch fetch errors, render "Couldn't load chart — retry?" with a click handler that re-fetches.

**Tests:** manual — throttle network in DevTools, kill the API, confirm both states show.

---

### 3.4 Action Center — applied recommendations UI

**Files:**
- `templates/dashboard/action_center.html` (or `todo.html`)
- `api/dashboard_routes.py` (new endpoint `/api/v1/actions/{id}/apply`)

**Steps:**

1. **New endpoint** — POST `/api/v1/actions/{id}/apply` sets `agent_analyses.applied = 1` and records `applied_at`.
2. **UI** — "Mark as applied" and "Dismiss" buttons on each recommendation.
3. **Applied section** — collapsible at the bottom, shows before/after metrics when available. Before/after comparison queries `query_digest_snapshots` for the affected query, computing avg time 1h before apply vs 1h after.

**Tests:** `tests/test_apply_recommendation.py` — POST to apply, assert DB column updated.

---

### 3.5 Resolve 4 deferred design decisions

Each is small:

1. **Mobile chart height:** edit chart container CSS — `h-[160px] md:h-[220px]`.
2. **Severity transition:** `transition: background-color 200ms ease-out` on `.severity-red / -yellow / -green`, with `@media (prefers-reduced-motion: reduce) { transition: none }`.
3. **Server switch:** add a `hx-vals='{"server":"..."}'` include on every auto-refresh; on switch, HTMX reloads the entire dashboard state.
4. **Chart flicker:** in the Server page JS, cache Chart instances in a module-level dict and call `chart.update()` instead of destroy+recreate on range change.

Each lands as a single-file PR with a comment linking back to IMPLEMENTATION.md §3.5.

---

### 3.6 DESIGN.md

**Files:** `DESIGN.md`

**Steps:** Run `/design-consultation` via Claude Code to extract the sketch design system from `templates/base.html` and partials into a standalone doc. Appendix B of PLAN.md is a stop-gap — DESIGN.md replaces it.

**Verify:** A new contributor can style a new page matching the aesthetic using only DESIGN.md.

---

## Phase 4 — Post-Launch / Deferred

These are explicitly blocked on signal that doesn't exist yet (real incidents, star count, user reports). Implementation plans are stubs until unblocking conditions fire.

| # | Item | Implementation sketch |
|---|---|---|
| 4.1 | Auto-generated postmortem MD | In `agent/replay.py`, after LLM analysis, write to `reports/incident-{id}-{date}.md`. ~20 lines. |
| 4.2 | `seeql incidents compare` | New subcommand, fetches two incidents, diffs `involved_metrics` + timelines, emits unified diff. |
| 4.3 | Counterfactual replay | Agent tool `simulate_action(action, at_time)` that re-runs the state builder with the simulated effect. Hard; defer. |
| 4.4 | General per-table retention config | Already partially implemented in 1.1; generalize to take arbitrary tables from YAML. |
| 4.5 | Hosted demo playground | Fly.io or Railway deployment of `seeql demo`. DNS + TLS + rate limiting. |
| 4.6 | Opt-in telemetry | Single endpoint (Cloudflare Worker or similar) counting `seeql check` / `seeql demo` runs. First-run consent prompt. |
| 4.7 | Hosted docs site | Docusaurus in `docs/`, deploy to `docs.seeql.dev`, Algolia DocSearch free tier. |
| 4.8 | Tier-3 JSON API errors | Stripe-style error objects. Middleware in `api/app.py` that converts `SeeQLError` exceptions to JSON. |
| 4.9 | Multi-instance read replicas | Already unblocked by 0.4 `server_id` plumbing. Just needs UI to add replicas to the registry. |
| 4.10 | Query rewrite suggestions | New agent tool `suggest_rewrite(query)` that prompts the LLM with the query + schema + EXPLAIN. |
| 4.11 | Automated safe actions | New module `automation/` with gated kill-query support. Needs `dba_agent` PROCESS and SUPER grants and an allowlist. |

---

## Cross-cutting: testing infrastructure

All the new tests listed above assume:

1. **`tests/conftest.py`** provides a `fresh_mon_db` fixture that creates an in-memory SQLite DB loaded with `storage/schema.sql`.
2. **`tests/fixtures/`** has helpers to seed anomaly events, incident windows, query digests, lock waits.
3. **`tests/test_integration.py`** runs a full end-to-end: seed synthetic data → run medium loop → detect → persist → group → replay → assert.

Add a new `tests/fixtures/incident_builder.py` with a `build_lock_cascade_scenario()` helper that seeds all the tables needed for a realistic test. This doubles as the generator for `examples/demo.db`.

---

## Work order summary

The implementation order in PLAN.md Appendix A is authoritative. Recommended working sequence:

**Week 1 (today):**
1. 0.1, 0.2 — git + docs (30 min)
2. 0.5, 0.6 — buffer pool + real SQL (~1 hour — highest user-visible impact per minute)
3. 0.3 — SIGTERM (1 hour)
4. 0.4 — server_id plumbing (2-3 hours — prerequisite for everything)

**Week 2:**
5. 1.1 — schema + retention
6. 1.2 — cache + detected_at
7. 1.3 — persistence
8. 1.4 — incident grouping
9. Cut v0.1.0-alpha tag after the Phase 1 gate passes

**Week 3:**
10. 1.5 — CLI refactor
11. 1.6, 1.7 — replay + LLM wrapper
12. 1.8, 1.9 — replay & incidents CLI
13. 1.10, 1.11, 1.12 — dashboard + Slack + state builder

**Week 4 (OSS launch prep):**
14. 2.1, 2.2 — packaging + CI
15. 2.3 — seeql demo (the magical moment)
16. 2.4 — init/doctor + graceful perf_schema
17. 2.5 — error catalog
18. 2.10 — README
19. Cut v0.1.0 tag and publish to Docker Hub

**Week 5 (post-launch polish):**
20. 2.6, 2.7, 2.8, 2.9 — API docs, onboarding page, dev env, docs restructure
21. 2.11.1, 2.11.2, 2.11.3 — visibility fixes

**Week 6:**
22. 3.1 — a11y pass
23. 3.2–3.5 — first-run, charts, action center, design decisions
24. 3.6 — DESIGN.md

**After that:** Phase 4 is fully driven by real-world signal.
