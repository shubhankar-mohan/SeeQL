# PLAN.md — SeeQL Unified Execution Plan

> **Status:** Single source of truth. Consolidates TODOS.md, the active CEO plan (`~/.gstack/projects/SeeQL/ceo-plans/2026-04-11-incident-replay.md`), the eng review test plan, design review decisions, and the DX review.
>
> **Last synced:** 2026-04-11

---

## 1. Where We Actually Are

### 1.1 What's shipped (ground truth, not CLAUDE.md)

- **Collection layer — complete.** 19 collectors across 3 loops (fast/medium/slow) + daily retention job. Files: `collectors/*.py`, `scheduler/`, `storage/schema.sql` (24 tables).
- **Agent layer — complete.** `agent/state_builder.py` (485 lines, includes anomaly section at lines 81-98), `agent/llm_agent.py` (413 lines, supports Gemini via Vertex AI and Claude via Anthropic API), `agent/tools.py`, `agent/prompts.py`.
- **Anomaly detection — complete** (CLAUDE.md is wrong). `alerting/anomaly.py` is 464 lines: z-score, same-hour-same-weekday baselines over 28 days, 24h rolling + all-data fallbacks, 8 metrics, zero-stddev guard, cold-start handling, fires alerts via `evaluate_anomaly()`.
- **Alerting — complete.** 6 rules (`alerting/rules.py`), 3 channels (`alerting/channels.py`: Slack, webhook, log), engine in `alerting/engine.py`.
- **API + Dashboard — complete.** `api/prometheus.py` (Prometheus `/metrics`), `api/agent_routes.py`, `api/dashboard_routes.py`, `api/dashboard_api.py`. Templates live in `templates/` using the sketch-on-paper aesthetic.
- **CLI — flat flags today.** `main.py` uses `--check`, `--init-db`, `--once`, `--api`, `--api-only`. No subparsers yet.

### 1.2 What is NOT yet built

> **Update (shipped):** This plan has since landed in full. Anomaly events are persisted to `anomaly_events`, grouped into `incident_windows` (`alerting/anomaly_store.py`, `alerting/incidents.py`), and replayable via `seeql replay` (`agent/replay.py`). The gap description below is preserved for historical context.

The anomaly detector produces `AnomalyResult` objects that are consumed by alerts and then thrown away — nothing is persisted, nothing is grouped into incidents, and there is no replay entry point.

### 1.3 Known defects (from reviews + TODOS)

| # | Defect | Source | Severity |
|---|--------|--------|----------|
| 1 | Not a git repo (one accidental delete = lost agent prompts / schema) | TODOS P1 | High |
| 2 | CLAUDE.md says anomaly detection is "Not Yet Built"; it is built | TODOS P1 | Low (doc) |
| 3 | `detect_anomalies()` runs twice per cycle (state_builder + alerting engine) — no cache | eng review | Medium |
| 4 | `detect_anomalies()` queries globally; ignores `server_id` even though it's in snapshot tables | CEO plan #8, TODOS P1 | Medium |
| 5 | No SIGTERM handler → Docker restart can lose pending SQLite writes | TODOS P1 | Medium |
| 6 | Dashboard auto-refresh regions have zero ARIA landmarks; screen readers silent | TODOS P2, design review | Medium |
| 7 | Fresh deploy shows "All clear — no emergencies" before any data is collected (false-positive healthy) | TODOS P2, design review | Medium |

---

## 2. Execution Phases

Four sequential phases. Each phase lists the **exact files touched** and the **verification step** that proves it works. Nothing in a later phase may be started before the gate for the current phase passes, except for items explicitly marked `[parallel-safe]`.

Phase gates are cheap on purpose — this is a small team, not a release train.

---

## Phase 0 — Hygiene & Prerequisites (P1)

**Goal:** Make subsequent work safe to do. All items are small and unblock everything else.

### 0.1 Initialize git repository `[parallel-safe]`
- Run `git init`, add `.gitignore` (venv/, data/, logs/, __pycache__/, *.egg-info, settings.local.yaml, .env).
- Create initial commit of the full tree.
- **Verification:** `git log --oneline` shows the initial commit; `settings.local.yaml` is not tracked.

### 0.2 Update CLAUDE.md to reflect reality `[parallel-safe]`
- Move "Anomaly detection" out of "Not Yet Built" into the Done list.
- Update table count from "22 tables" to the real count (currently 24; will become 26 after Phase 1.1 lands).
- Add `alerting/anomaly.py` to the project structure list.
- **Verification:** Diff CLAUDE.md against `ls alerting/` and `grep -c 'CREATE TABLE' storage/schema.sql` — they should agree.

### 0.3 Graceful SIGTERM handler `[parallel-safe]`
- In `scheduler/runner.py`, install `signal.signal(SIGTERM, handler)` and `SIGINT` too.
- Handler calls `scheduler.shutdown(wait=True)`, then closes the SQLite writer (`storage/writer.py`) to flush WAL.
- **Verification:** `docker stop <container>` exits in <5s with no "pending writes" log warning. Manual test: send SIGTERM with `kill -TERM $(pgrep -f main.py)` while a medium loop is running; confirm the loop completes and DB closes cleanly.

### 0.4 Multi-server alert filtering
- Add a `server_id` parameter to all 6 evaluators in `alerting/rules.py`; use it in the WHERE clause of every snapshot query.
- Also add `server_id` to `detect_anomalies(server_id)` in `alerting/anomaly.py` — this is a CEO-plan prerequisite for incident grouping to be meaningful across servers.
- **Verification:** Seed two rows in `query_digest_snapshots` with different `server_id`s; confirm each rule fires only for its own server.

### 0.5 Buffer pool hit ratio — fix the "always 0" bug `[parallel-safe]`

**Bug:** The dashboard shows Buffer Pool Hit Ratio = 0 even on warm production databases.

**Root cause:** `collectors/queries.py:176` uses `HIT_RATE / 1000.0 AS hit_ratio` from `information_schema.INNODB_BUFFER_POOL_STATS`. MySQL computes `HIT_RATE` over the **last ~1-second interval** as "hits per 1000 gets." If no page gets occurred in that window (common on read-cached workloads or during quiet moments), the column returns `0` — not because the cache is cold, but because the sample was empty. SeeQL snapshots this every 5 minutes, so it catches an empty-interval `0` most of the time.

**Correct computation** uses the cumulative counters that `parsers/global_status.py:41` is **already collecting**:

```
hit_ratio = 1 - (Innodb_buffer_pool_reads / Innodb_buffer_pool_read_requests)
```

- `Innodb_buffer_pool_read_requests` = total logical reads (cache + disk).
- `Innodb_buffer_pool_reads` = reads that missed the cache and had to hit disk.
- Cumulative since server start → stable, always meaningful.

**Fix (read-side, zero migration):**
- In `api/dashboard_api.py:189` (`/api/v1/metrics/buffer-pool`), compute `hit_ratio` by joining `global_status_snapshots` on the two counters per timestamp bucket instead of reading the broken column from `buffer_pool_snapshots`.
- Keep the `buffer_pool_snapshots.hit_ratio` column populated as-is (avoid a migration); leave it for historical rows but stop displaying it.
- `agent/state_builder.py` should switch to the same computation wherever it renders the hit ratio into the LLM prompt.

**Follow-up (collector-side, later):** when we next touch `collectors/medium_loop.py:139` `BufferPoolCollector`, compute `hit_ratio` from the global-status counters at collection time and store that. Deprecate the `HIT_RATE / 1000` path.

**Verification:**
- On a database with any recent activity, `/api/v1/metrics/buffer-pool` returns `hit_ratio > 0` (typically 0.95–0.999).
- Cross-check: `SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read%'` on production, compute `1 - reads/read_requests` by hand, assert the dashboard value matches within 0.001.
- Overview KPI card "Buffer Pool Hit %" shows a believable value on a warm DB (not 0, not 100).

### 0.6 Query Performance: show real SQL, not `?` placeholders `[parallel-safe]`

**Gap:** The Query detail pane shows the parameterized `digest_text` (`WHERE user_id = ?`) instead of the real query with actual values (`WHERE user_id = 4829172`), even though the real SQL is already being collected and stored.

**Evidence the data is already there:**
- `collectors/queries.py:94` — `LEFT(QUERY_SAMPLE_TEXT, 2000) AS query_sample_text` is collected on every medium loop.
- `storage/schema.sql:25` — stored as `query_sample_text TEXT  -- Real SQL with actual values (from QUERY_SAMPLE_TEXT)`.
- `storage/writer.py:82` — persisted.
- `agent/tools.py:365-376` and `collectors/explain_capture.py:68-70` — both backends correctly prefer `query_sample_text` over `digest_text`. Only the dashboard doesn't.

**The bug:**
- `api/dashboard_routes.py:817` — `partial_query_detail()` selects `digest_text` only, not `query_sample_text`.
- `templates/partials/query_detail.html:10` — renders `{{ query_info.digest_text }}`.
- `templates/partials/query_detail.html:82` — the "Run EXPLAIN on this query" copy-button also uses `digest_text[:200]`, which produces `EXPLAIN SELECT ... WHERE user_id = ?` — not a runnable query.

**Fix (~10 lines):**
1. `api/dashboard_routes.py:817` — add `query_sample_text` to the SELECT and `MAX(query_sample_text) as query_sample_text` so GROUP BY still works.
2. `templates/partials/query_detail.html:10` — render `{{ query_info.query_sample_text or query_info.digest_text }}` so old rows and rows without a sample still display.
3. `templates/partials/query_detail.html:82` — same fallback for the EXPLAIN copy-button so it produces a runnable statement.
4. Show a small label above the query pane: "sample" (real values) vs "pattern" (parameterized) so the user knows what they're looking at. Green for sample, grey for pattern.

**Verification:**
- Expand a query in the UI that has a recent `query_sample_text`: the detail pane shows actual parameter values, not `?`.
- The "Copy EXPLAIN" button produces a runnable `EXPLAIN SELECT ...` with literal values.
- Expand an old query whose `query_sample_text` is NULL: falls back to `digest_text` with a "pattern" label, no crash.

### Phase 0 gate
- Repo is a git repo with a clean first commit.
- CLAUDE.md matches reality.
- Restarting the process (SIGTERM) does not corrupt or drop data.
- `detect_anomalies()` and every alert rule accept a `server_id` argument.
- Buffer pool hit ratio reads a real number on a warm DB (no more `0`).
- Query detail pane shows `query_sample_text` with a "pattern/sample" label and a working EXPLAIN button.

---

## Phase 1 — Incident Replay + Anomaly Persistence (✅ COMPLETE)

**Goal:** Persist anomaly events, group them into incident windows, expose a `seeql replay` CLI, and surface incidents in the dashboard and Slack. This is the differentiator — no other open-source MySQL monitor does incident replay.

**Order matters.** Each step is independently testable against the step before it. Do not reorder.

### 1.1 Schema + retention for two new tables
**Files:** `storage/schema.sql`, `storage/retention.py`

Add to `storage/schema.sql` (brings table count from 24 → 26):

```sql
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
```

Add retention: `anomaly_events` uses the global `retention.days` (default 90). `incident_windows` uses 365 days — requires the thin per-table retention override in `storage/retention.py` (TODOS P2 item is pulled forward here because incident replay needs it).

**Verification:** `python main.py --init-db` runs cleanly; `sqlite3 data/mysql_monitor.db ".schema anomaly_events"` and `.schema incident_windows` show the tables and indexes.

### 1.2 Anomaly result model + caching
**Files:** `alerting/anomaly.py`

- Add `detected_at: str` to the `AnomalyResult` dataclass (ISO8601 UTC).
- Add an in-memory per-cycle cache so `detect_anomalies(server_id)` called twice in the same cycle reuses the first result. Key: `(server_id, cycle_epoch_minute)`. Invalidate on new cycle.
- `state_builder.py` and `alerting/engine.py` already call `detect_anomalies()` independently — the cache prevents them computing baselines twice.

**Verification:** Unit test calls `detect_anomalies("default")` twice in a row and asserts the second call short-circuits (mock the baseline query and assert it runs once).

### 1.3 Anomaly event persistence
**Files:** `alerting/anomaly_store.py` (new, thin wrapper)

- Public function: `persist(results: list[AnomalyResult], conn) -> list[int]` — writes each result to `anomaly_events`, returns inserted IDs. No incident_id yet (that comes in 1.4).
- Called from the **medium loop scheduler hook**, not from inside the alerting engine, to keep concerns separated.
- Refactor call site: in `scheduler/runner.py` medium loop, call `detect_anomalies()` once, then pass the results into both `evaluate_anomaly()` (for alerting) and `anomaly_store.persist()` (for storage). Do not refactor the alert engine itself.

**Verification:** Run `python main.py --once`; confirm rows appear in `anomaly_events` with correct `detected_at`, `metric_name`, `z_score`.

### 1.4 Incident window builder
**Files:** `alerting/incidents.py` (new)

- Public function: `update_windows(server_id: str, conn) -> list[int]` — returns IDs of any newly created incidents (used by 1.11 Slack notification).
- Algorithm (gap-based clustering with duration cap):
  1. Query ungrouped events (`incident_id IS NULL`) for `server_id`, oldest first.
  2. For each event, look up the most recent open incident for this server where `end_time` is within `alerting.incident_gap_minutes` (default 15) of the event's `detected_at` AND `(end_time − start_time) < alerting.incident_max_duration_minutes` (default 120).
  3. **Extend** → update `end_time`, merge metric into `involved_metrics`, bump `event_count`, upgrade severity if higher.
  4. **Or create** → new incident row.
  5. Set `incident_id` on the event.
- **All of step 3–5 in a single SQLite transaction** per event so `event_count` and `anomaly_events.incident_id` can't drift on crash.
- Call site: medium loop, right after `anomaly_store.persist()`.
- Config additions in `settings.yaml`:
  ```yaml
  alerting:
    incident_gap_minutes: 15
    incident_max_duration_minutes: 120
  ```

**Verification (from eng review test plan):**
- Empty `anomaly_events` → 0 incidents.
- Single event → 1 incident with 1 event.
- Two events within 10 min → 1 incident.
- Two events >15 min apart → 2 incidents.
- 130 minutes of continuous events → 2+ incidents (duration cap kicks in at 120).
- Crash-mid-transaction smoke test: kill between `persist()` and `update_windows()`, restart, confirm ungrouped events get picked up on next cycle.

### 1.5 CLI refactor to argparse subparsers
**Files:** `main.py`

- Migrate to subparsers while keeping the existing `--check`, `--init-db`, `--once`, `--api`, `--api-only` flags on the root parser for one version. Deprecate but don't remove.
- New subcommands: `check`, `run`, `run --once`, `serve`, `init`, `doctor`, `demo`, `replay`, `incidents`.
- Root behavior (`python main.py` with no args) stays the same: start the continuous collector.

**Verification:** `python main.py check`, `python main.py --check`, and `python main.py run --once` all behave identically to their current counterparts. Add CLI smoke tests in `tests/`.

### 1.6 Replay module + incident investigator prompt
**Files:** `agent/replay.py` (new), `agent/prompts.py` (append incident investigator prompt)

- Public function: `run_replay(from_ts: str, to_ts: str, incident_id: int | None) -> ReplayResult`
- Constructs a chronological timeline from all relevant snapshot tables for the window:
  - `anomaly_events` (primary narrative spine)
  - `lock_wait_snapshots`, `transaction_snapshots`, `metadata_lock_snapshots`
  - `ddl_changes`
  - `query_digest_snapshots` (top-N before/during/after)
  - `global_status_snapshots` (as rate-of-change deltas)
  - `gcp_metric_snapshots`
  - `innodb_status_snapshots` (deadlock sections if any)
- Output format: Markdown to stdout (see CEO plan §Replay Output Format).
- **Timeline-only fallback:** if `_detect_backend()` in `llm_agent.py` returns None, print just the timeline with a one-line note explaining that LLM analysis requires credentials. This makes the open-source demo valuable without an API key.

**Verification:**
- Seed a fixture SQLite DB with a synthetic lock cascade incident.
- Run `python main.py replay --from <ts> --to <ts>`.
- Assert output contains the timeline markers in chronological order.
- Run the same command with `ANTHROPIC_API_KEY` unset — assert fallback message appears and timeline still renders.

### 1.7 Public LLM wrapper for replay analysis
**Files:** `agent/llm_agent.py`

- New public function: `run_llm_analysis(prompt: str, tools: list, analysis_type: str, server_id: str) -> dict`
- Handles backend detection (Gemini/Claude), calls the appropriate private loop, writes to `agent_analyses` with the supplied `analysis_type`, returns `{"text": ..., "analysis_id": ...}`.
- `agent/replay.py` calls this with `analysis_type="replay"` and then updates `incident_windows.analysis_id`.
- Live tools remain available during replay (per user decision), but the incident investigator prompt instructs the LLM to prioritize historical data from the window.

**Verification:** `run_llm_analysis(...)` with a stub backend returns a dict with both keys; the `agent_analyses` row is written with `analysis_type='replay'`.

### 1.8 `seeql replay` CLI wiring
**Files:** `main.py`

- Subparser flags:
  - `python main.py replay --from "2026-04-10T03:00" --to "2026-04-10T05:00"`
  - `python main.py replay --incident 42`
  - `python main.py replay --latest` (queries `incident_windows` ordered by `start_time DESC LIMIT 1`)
- Error handling (ties into Phase 2.5 error codes): invalid timestamps → `E010` (invalid time range for replay).

**Verification:** Each variant produces output; `--latest` with no incidents prints a friendly "No incidents detected yet" message.

### 1.9 `seeql incidents list` CLI
**Files:** `main.py`

- `python main.py incidents list` — table of recent incidents (id, start, end, severity, metrics, status).
- `--status detected` / `--status analyzed` / `--status resolved` filter flags.
- `--limit N` (default 20).
- Uses plain `print` + aligned columns — no new rendering dep.

**Verification:** `incidents list` shows seeded rows with correct status filtering.

### 1.10 Dashboard incident timeline widget
**Files:** `api/dashboard_api.py`, `templates/partials/incidents_timeline.html` (new), `templates/overview.html`

- New API endpoint: `GET /api/v1/incidents/recent?limit=10&status=detected` returning the contract from the CEO plan:
  ```json
  [{"id": 42, "start_time": "...", "end_time": "...", "severity": "critical",
    "involved_metrics": ["threads_running","lock_frequency"], "event_count": 8,
    "status": "detected", "duration_minutes": 35}]
  ```
- Partial template uses the sketch aesthetic (see Appendix B): wobbly borders, hard shadows, severity colors. Each card shows time range, severity badge, metric tags. Click to expand event list.
- HTMX `hx-trigger="every 30s"` + `aria-live="polite"` (Phase 3.1 requirement satisfied eagerly here).
- Empty state: "No incidents detected. Your database is behaving."

**Verification:** Hit `/api/v1/incidents/recent` and confirm JSON shape. Load `/overview`, confirm widget renders, confirm it updates on auto-refresh.

### 1.11 Slack notification for new incidents
**Files:** `alerting/incidents.py`, `alerting/channels.py`

- When `update_windows()` creates a **new** incident (not when extending one), emit a Slack message via the existing Slack channel:
  ```
  :rotating_light: Incident detected — critical
  Metrics: threads_running, lock_frequency
  Started: 2026-04-10 03:12 UTC
  Run: python main.py replay --incident 42
  ```
- Uses the same webhook configured for existing alerts. Gated on `alerting.slack.enabled`.
- Does NOT fire for extensions — only for newly created windows.

**Verification:** Force a new incident in a test harness; capture the webhook POST body; assert the command line in the message resolves to the correct incident id.

### 1.12 State builder: Recent Incidents section
**Files:** `agent/state_builder.py`

- Add a "Recent Incidents" section (alongside the existing anomaly section) that queries `incident_windows WHERE status != 'resolved' AND start_time > NOW() - 24h`.
- For each, list id, start/end, severity, metrics, event_count.
- Link each incident to its constituent anomaly events for the periodic LLM analysis to see the pattern.

**Verification:** With a seeded incident, run `state_builder.build()` and assert the section appears in the output.

### Phase 1 gate (matches CEO plan success criteria + eng review test plan)

1. Anomaly detection catches the synthetic lock cascade fixture ≥5 minutes before max_connections would be exhausted.
2. `python main.py replay --from X --to Y` produces a timeline + root cause that a DevOps engineer can read in <2 minutes.
3. Eng review end-to-end path passes: `seed → detect_anomalies() → persist → group → replay`.
4. Anomaly cache does not bleed across cycles.
5. Retention job correctly preserves `incident_windows` for 365 days while aging `anomaly_events` at 90.
6. Slack fires once per new incident (not per extension).
7. Dashboard widget renders, auto-refreshes, and is keyboard-navigable.

---

## Phase 2 — DX Hardening for Open-Source Launch

**Goal:** Take the product from "3/10 DX, ~45 min TTHW" (DX review baseline) to "8/10, ~2-4 min TTHW" — the **Competitive tier** for a startup CTO evaluating the tool. This is the 19 DX fixes from the DX review, grouped for coherent landings.

Prerequisite: Phase 1 complete (several DX items reference `seeql replay`, `seeql demo`, `seeql doctor`).

### 2.1 Packaging & distribution
- **`seeql` console_scripts entry point** in `pyproject.toml` (`[project.scripts] seeql = "main:main"`). Prerequisite for `seeql replay` shorthand — up to now we've typed `python main.py replay`.
- **LICENSE (Apache 2.0)** at repo root.
- **CONTRIBUTING.md** — basic: fork, dev setup, test, PR checklist.
- **CHANGELOG.md** (Keep a Changelog format) + semver strategy. Start at `0.1.0`.
- **Verification:** `pip install -e . && seeql check` works; `LICENSE` file present; `CHANGELOG.md` references the unreleased section.

### 2.2 Docker Hub publication
- **GitHub Actions:** `.github/workflows/ci.yml` (pytest + ruff on every PR) and `.github/workflows/release.yml` (build + push `seeql/seeql:latest` and `seeql/seeql:<tag>` to Docker Hub on version tags).
- Requires Docker Hub credentials as GitHub secrets (manual setup step — document in CONTRIBUTING.md).
- **Verification:** Tag `v0.1.0`, confirm image appears at `hub.docker.com/r/seeql/seeql`.

### 2.3 Magical moment — `seeql demo`
- Bundle a small SQLite fixture (`examples/demo.db`, ~5 MB) containing ~48h of synthetic data with one embedded lock cascade incident.
- `seeql demo` subcommand:
  1. Copies `examples/demo.db` to a temp path.
  2. Starts `uvicorn` on `localhost:8080` pointed at the demo DB.
  3. Prints the incident replay narrative for the bundled incident to stdout.
  4. Opens the browser to `/overview` (optional, cross-platform via `webbrowser.open`).
- Zero external dependencies — no MySQL, no GCP, no API keys. Timeline-only fallback from 1.6 means even without LLM credentials the demo is useful.
- **Verification:** `docker run -p 8080:8080 seeql/seeql demo` works on a clean machine with no config. Open `http://localhost:8080` in the browser — the incident timeline widget shows the bundled incident.

### 2.4 Onboarding — `seeql init` + `seeql doctor`
- **`seeql init`** — interactive wizard. 4 questions: Cloud SQL host, instance name, dba_agent password, LLM provider (gemini/claude/none). Writes `settings.local.yaml`, validates connectivity before exiting, generates `docker-compose.yml` from template.
- **`seeql doctor`** — diagnostic: MySQL reachable? `performance_schema` on? GCP ADC configured? LLM credentials valid? SQLite writeable? Disk space OK? Each check is a row in a pass/fail table with a fix suggestion for failures.
- **Graceful degradation without `performance_schema`.** Several collectors depend on it. On startup, probe; if off, skip dependent collectors and log "Running in limited mode — enable performance_schema for full observability." The product must be evaluable without restarting a production DB.
- **Verification:** `seeql init` on a clean machine produces a runnable `docker-compose.yml`; `seeql doctor` on a broken config reports which check failed and how to fix it; starting against a DB with `performance_schema=off` does not crash — the log clearly states limited-mode collectors.

### 2.5 Error message catalog (E001–E010)
- Add `errors.py` (or extend config) with a dict of top-10 error codes in **Tier 2 (Rust-style)** format: problem + cause + fix + docs URL.
- Catalog:
  - `E001` MySQL auth failed
  - `E002` `performance_schema` disabled
  - `E003` GCP ADC missing
  - `E004` Bad config (schema validation)
  - `E005` Required Cloud SQL flag missing
  - `E006` MySQL connection timeout
  - `E007` Permission denied on grants
  - `E008` SQLite disk full
  - `E009` LLM API key invalid
  - `E010` Invalid time range for replay
- Wire into the CLI so these codes appear on failure.
- **Verification:** Trigger each error in a fixture; confirm the code, message, cause, fix, and URL all render.

### 2.6 API contract + docs
- **Unified API paths under `/api/v1/*`.** Keep `/metrics` (Prometheus) and `/dashboard/*` (HTML) unchanged. Deprecate old `/data/*` and `/collect/*` for one version — keep them working but log a deprecation warning.
- **Document FastAPI `/docs`** (Swagger UI) in README. Add route-handler docstrings + `tags` + `summary` args so the generated docs are useful.
- **Config deprecation shim** — `@deprecated_env(old, new, remove_in)` decorator in `config.py` so future env-var renames don't break existing users.
- **Verification:** `/api/v1/incidents/recent` (from 1.10) is reachable; `/docs` renders a usable page; starting with a deprecated env var logs a one-line warning with the new name.

### 2.7 Dashboard onboarding status page
- New route `/onboarding` shown automatically when `SELECT COUNT(*) FROM query_digest_snapshots = 0`.
- Shows: "Collecting baselines: 23/48 hours." with a progress bar, a collector health list (14/19 active), data freshness per collector.
- Redirects to `/overview` once data exists.
- **Verification:** Fresh `data/mysql_monitor.db` → first `seeql serve` lands on `/onboarding` with honest progress; data arrives → next auto-refresh bounces to `/overview`.

### 2.8 Developer environment
- `docker-compose.dev.yml` — local MySQL 8.0 (perf_schema on, sample data seeded) + SeeQL + Prometheus. `make dev` starts it.
- **Verification:** `make dev` on a clean clone brings up the full contributor stack in <2 minutes.

### 2.9 Docs restructure + tutorial
- Move `docs/` + `examples/` into their own directories:
  - `docs/getting-started.md`
  - `docs/tutorial-first-incident.md` — walk the reader through `seeql demo`, the lock cascade scenario, and the replay output. Doubles as launch content.
  - `docs/configuration.md`, `docs/cloud-sql-setup.md`, `docs/architecture.md`, `docs/errors/E001.md` … `E010.md`
  - `examples/docker-compose.yml`, `examples/prometheus.yml`
- Move `CLAUDE.md`, `FRONTEND_CLAUDE.md`, `PLAN.md` → `internal/` (or leave at root — user preference, this is cosmetic).
- **Verification:** All relative links resolve; `docs/tutorial-first-incident.md` can be followed end-to-end with only the `seeql/seeql:latest` image.

### 2.10 README overhaul
- **Above the fold:** 3-line quickstart (`docker run -p 8080:8080 seeql/seeql demo`).
- Animated GIF of `seeql demo` running.
- Prerequisites pushed into a collapsible `<details>` section.
- **Verification:** Time a fresh reader from opening the README to seeing the dashboard — target is under 2 minutes (DX review TTHW goal).

### 2.11 Data Visibility & Correctness Fixes

Three gaps found during a user walk-through of the dashboard. Grouped here because none are launch-blockers but all three materially improve how useful the stored data is.

#### 2.11.1 Global time range picker + custom date range
**Gap:** The time-range pills (1h / 6h / 24h / 7d) exist only on the Queries page (`templates/dashboard/queries.html:14-21`). Overview, Locks, Schema, and Server pages silently use hard-coded defaults and give no way to ask for a different window. There is no custom-range picker anywhere — so "show me 03:00–05:00 last Tuesday" is impossible from the UI.

**Fix:**
- Lift the range-pill component out of `queries.html` into a shared partial `templates/partials/time_range_picker.html`.
- Include it in `templates/base.html` so it appears once in the global header. The picked range writes to a `?range=...` query string and pages re-read it on load.
- Add two states beyond presets: `30d` (already in `RANGE_MAP` but unused in UI) and **custom**. Custom opens a lightweight date-time picker that submits `?from=<iso>&to=<iso>` instead of `?range=`.
- Extend `api/query_helpers.py:49` `parse_time_range` to accept either a preset OR an explicit `from`/`to` pair. Every `/api/v1/*` endpoint that currently takes `range` should transparently accept `from`/`to` as well.
- Dashboard JS refetches charts on range change without a full page reload (HTMX `hx-get` + `hx-target` already the norm).

**Verification:**
- Switching range on Overview updates all charts (QPS, threads, buffer pool, incidents widget) without a page reload.
- Custom range `from=2026-04-10T03:00&to=2026-04-10T05:00` returns correct data on every dashboard page.
- Anomaly detection and incident replay are the only paths that bypass this picker — and they already take explicit timestamps.

#### 2.11.2 Schema & table filter
**Gap:** There is no schema or table filter anywhere in the dashboard or API. `query_digest_snapshots.schema_name`, `table_io_snapshots.object_schema`, `schema_snapshots.table_schema` + `table_name` are all stored but the APIs don't expose them as filters. There's no way to ask "show me queries hitting a specific hot table" — a common DBA workflow.

**Fix:**
- New endpoint `GET /api/v1/schemas` returns `{schemas: [{name, table_count}], tables: [{schema, name, last_seen}]}` by querying `DISTINCT schema_name` from `query_digest_snapshots` and `DISTINCT object_schema, object_name` from `table_io_snapshots`. Cache for 60s.
- Add `?schema=<name>&table=<name>` params to:
  - `/api/v1/queries/top` (filter `query_digest_snapshots.schema_name`; for `table=` do a `LIKE` match on `digest_text` since digests don't carry per-table info directly — flag this as a heuristic in the response metadata).
  - `/api/v1/queries/regressions` (same).
  - `/api/v1/locks/history` — tricky: `lock_wait_snapshots` doesn't store the table name directly. Option: parse the table out of `waiting_query`/`blocking_query` via a simple regex, best-effort. Label results as "approximate" in the UI when filtered.
  - `/api/v1/schema/table-sizes` (already has schema/table in the table, trivial to filter).
  - A new `/api/v1/tables/{schema}/{table}/io` endpoint returning the `table_io_snapshots` time series for one table.
- UI: two dropdowns (schema, then dependent table) in the shared header next to the time-range picker. Default both to "All." Persist selection across pages via query string.
- "Pin this table" affordance on the Schema page — clicking a row sets the filter and navigates to the filtered Queries view.

**Verification:**
- Select schema=`shop`, table=`loyalty_members` → Queries page shows only queries referencing that table, Locks page shows only locks on that table (labeled "approximate match"), Schema page shows the row highlighted.
- Deselecting (set to "All") returns the full unfiltered view.
- `/api/v1/schemas` returns a real list against a seeded DB and caches for 60s (second hit in same window doesn't re-query).

#### 2.11.3 Threads chart: highlight `running` as the load-bearing metric
**Gap:** `/api/v1/metrics/threads` (`api/dashboard_api.py:167-186`) already returns both `Threads_running` and `Threads_connected`, but the chart renders them as equal peers. `Threads_running` is the pre-crash indicator for lock cascades — `Threads_connected` is mostly context because connection pooling keeps it high regardless of health.

**Fix:**
- Overview KPI card labeled "Active Threads" should show `running` as the bold headline number and `connected` as small subtext ("12 running / 287 connected"). Today it likely shows one or the other without the contrast.
- Threads chart renders `running` in `marker` red (bold, 2px) and `connected` in `pen` blue (thin, 1px, dashed). Legend: "running (load) / connected (pool)".
- On hover, tooltip explains: "Threads_running is the count of threads currently executing SQL. It climbs during lock cascades. Threads_connected counts all open connections — normal to be high with connection pooling."
- When `running > 3 * baseline_avg` (same threshold as the anomaly trigger), draw a red horizontal band on the chart at the baseline so the spike is visually obvious.

**Verification:**
- Overview KPI shows two numbers with clear visual hierarchy.
- Threads chart in red-vs-blue with legend labels.
- Synthetic spike test: inject rows where `Threads_running = 5 * baseline` → red band appears, running line crosses it.

### Phase 2 gate
- Clean-machine install: `docker run seeql/seeql demo` → dashboard visible at localhost:8080 + incident narrative in terminal, in <2 minutes.
- `seeql doctor` exits non-zero on a broken config with actionable messages.
- `seeql init` on a fresh machine produces a running `docker-compose.yml`.
- GitHub Actions CI is green on a test PR; a test tag publishes an image to Docker Hub.
- `/docs` (FastAPI Swagger UI) loads.
- Time range picker present on every dashboard page, custom `from`/`to` works end-to-end.
- Schema + table filter works on Queries, Locks, and Schema pages.
- Threads chart visually distinguishes `running` (load-bearing) from `connected` (pool context).

---

## Phase 3 — Dashboard Polish & Accessibility

**Goal:** Close the design-review gaps and the TODOS P2 dashboard items. These are independent of Phases 1/2 and can be done in parallel with Phase 2 if there's a spare person. Listed after Phase 2 because the shared gating concern is "open-source launch quality."

### 3.1 Accessibility pass
Every sub-item is independent and small.
- `aria-live="polite"` on every HTMX auto-refresh container (health bar, alerts list, lock-waits card, incidents widget from 1.10).
- `<a href="#main-content" class="sr-only focus:not-sr-only">Skip to content</a>` before nav in `templates/base.html`.
- `aria-label` on every Chart.js `<canvas>` summarizing the current data (e.g., "QPS chart showing 2,847 queries per second over the last hour"). Update the label when data re-renders.
- `.info-tip` gets `tabindex="0"`, `role="tooltip"`, and a `:focus` CSS rule mirroring `:hover` so tooltips work on keyboard.
- Audit pagination buttons (`px-3 py-1`) for 44×44px minimum touch targets; bump padding where needed.

**Verification:** axe-core or Lighthouse accessibility audit passes ≥90; VoiceOver/NVDA can announce the health bar on status change.

### 3.2 First-run onboarding state (per-component)
This is the dashboard-level mirror of the Phase 2.7 `/onboarding` page — it covers the case where someone lands directly on `/overview` before data exists.
- Health bar: "WAITING" neutral color + "Waiting for first data collection..."
- KPI cards: already show "—"; add subtext "data arrives after first collection cycle (~30s)".
- Charts: "Collecting baseline data..." instead of "No data yet".
- Active alerts: "Agent is starting up. First collection in ~30 seconds." instead of "All quiet."
- Action Center: "No data yet. The agent needs a few collection cycles before it can analyze your database." instead of "All clear — no emergencies."

**Verification:** Reset `data/mysql_monitor.db`, load `/overview`, confirm every component shows the waiting state (no false-positive "healthy").

### 3.3 Chart loading + error states
- Pulsing sketch-line skeleton (3 wavy dashed lines animating) inside every Chart.js container during fetch.
- Error state: "Couldn't load chart — retry?" with a retry link, logs to console.
- Empty state already implemented (`fetchAndChart`) — just verify it's called.

**Verification:** Throttle network in DevTools, confirm skeleton shows. Kill the API, confirm error state shows with a working retry.

### 3.4 Action Center — applied recommendations UI
- "Mark as applied" button on each recommendation → records timestamp, moves item to a collapsible "Applied" section.
- "Dismiss" button → hides without marking applied.
- Applied items show before/after metric comparison when the backend has it (ties to the feedback tracking hook in the Week 4 roadmap).

**Verification:** Apply a recommendation → it moves to the collapsed section → before/after metrics render if available.

### 3.5 Resolve the 4 deferred design decisions
From the design review (currently unresolved):

| Decision | Proposed resolution | Priority |
|---|---|---|
| Mobile chart height (220px too tall on mobile?) | Reduce to 160px below `md` breakpoint; verify alerts stay above the fold on iPhone SE | Medium |
| Health bar severity transition animation | 200ms background-color transition on severity-class change; no motion on `prefers-reduced-motion` | Low |
| Dashboard behavior when switching servers | Reload full dashboard state on server switch; flash a "Switched to server: X" toast; no cross-server state leakage | Medium (blocks multi-instance) |
| Chart flicker on time-range change (Server page) | Refactor Server page to update one Chart.js instance per canvas instead of re-creating; debounce by 150ms | Low |

**Verification:** Each decision landed with a short ADR comment in the relevant template/JS file pointing back to this line item.

### 3.6 DESIGN.md
- Extract the sketch design system from `templates/base.html` and scattered templates into `DESIGN.md` via `/design-consultation`: color tokens, typography, border-radius variants, shadow hierarchy, decorative elements, rotation system, component patterns.
- Appendix B of this plan is a stop-gap — DESIGN.md replaces it.

**Verification:** A new contributor can style a new page matching the aesthetic using only `DESIGN.md`.

### Phase 3 gate
- Lighthouse a11y score ≥90 on `/overview`, `/action-center`, `/queries`, `/locks`, `/schema`, `/server`.
- Fresh `data/mysql_monitor.db` → every dashboard component shows a correct waiting state.
- All 4 deferred design decisions landed.
- DESIGN.md exists and is referenced from CONTRIBUTING.md.

---

## Phase 4 — Post-Launch / Deferred (P3)

Do these only after Phases 0–3 are in production and the OSS launch has traction. Each depends on data or signal that doesn't exist yet.

| # | Item | Unblocks when |
|---|---|---|
| 4.1 | Auto-generated postmortem Markdown files (`reports/incident-{id}-{date}.md`) | Incident replay proven useful in real incidents |
| 4.2 | `seeql incidents compare 3 7` — diff two incidents side by side | ≥5 real incidents have accumulated |
| 4.3 | Counterfactual analysis in replay ("what if we had killed PID 812 at T+15s?") | Replay output quality is stable and trusted |
| 4.4 | Per-table retention overrides (fully general, beyond the incident_windows special case in 1.1) | Users ask for it |
| 4.5 | Hosted demo playground (`seeql.dev/demo`) | ≥100 GitHub stars or observed usage |
| 4.6 | Opt-in anonymous telemetry | Meaningful adoption volume |
| 4.7 | Hosted docs site (`docs.seeql.dev`, Algolia DocSearch) | `docs/` content stabilized post-launch |
| 4.8 | Stripe-tier JSON error format for API | First external API consumer appears |
| 4.9 | Multi-instance support (read replicas alongside primary) | `server_id` plumbing from Phase 0.4 proves out |
| 4.10 | Query rewrite suggestions (not just "add index" but "rewrite this subquery as a JOIN") | LLM tool coverage is sufficient |
| 4.11 | Automated safe actions (kill queries running > X min with safeguards) | Recommendation quality is validated |

---

## 3. Cross-Cutting Concerns

### 3.1 Testing strategy (from eng review test plan)
- **Affected code paths** for Phase 1: `alerting/anomaly.py`, `alerting/incidents.py`, `alerting/anomaly_store.py`, `agent/replay.py`, `main.py`, `agent/state_builder.py`, `storage/schema.sql`, `storage/retention.py`.
- **Key interactions to verify:**
  - Anomaly cache returns same result on back-to-back calls in one cycle.
  - After detection, results appear in `anomaly_events` with correct fields.
  - Events within 10 min → one incident; events >15 min apart → two incidents.
  - Replay on a time range queries all relevant tables and produces chronological output.
  - Replay via `incident_id` constructs timeline from incident window boundaries.
  - `python main.py replay --from <ts> --to <ts>` invokes replay and exits cleanly.
  - State builder "Recent Incidents" section appears when incidents exist.
- **Edge cases to cover:**
  - Empty `anomaly_events` → 0 incidents.
  - Single event → 1 incident with 1 event.
  - Replay on time range with no data → informative message, not a crash.
  - Invalid incident_id → clear error.
  - `--replay` without `--from` or `--to` → usage error.
  - Retention cleans `anomaly_events` at 90 days, preserves `incident_windows` at 365.
- **Critical paths:**
  - End-to-end: seed anomaly data → `detect_anomalies()` → persist → group into incidents → replay.
  - Anomaly cache invalidation between cycles N and N+1.

### 3.2 Cost watch
- Claude API spend should stay at the current "~50 analyses/day" baseline. `seeql replay` adds one analysis per incident replayed — low volume by definition.
- Periodic analysis cadence stays at 30 minutes unless a deep-dive triggers.
- If Phase 1 adds visible cost, evaluate Haiku 4.5 for periodic checks and keep Claude Opus 4.6 (via Vertex AI) for incident replay.
- Cache EXPLAIN results by `(digest, schema_hash)` — same query + same schema = same plan.

### 3.3 Operational hardening (carry-over from Week 6 roadmap)
- Systemd service file with auto-restart.
- Daily SQLite backup to GCS (single `sqlite3 .backup` + `gsutil cp`).
- "Monitor the monitor" alert: if no row has been written to `query_digest_snapshots` in the last 10 minutes, fire a Slack notification.
- Log rotation check.
- Memory profiling over a 7-day run.

---

## 4. Success Criteria

### 4.1 Product success (from original PLAN.md + CEO plan)
1. **Lock cascade detection** within 60 seconds of onset; agent explains cause.
2. **DDL → regression correlation** within 30 minutes of the change.
3. **Specific index suggestions** for the top 5 full-scan queries with exact `CREATE INDEX` statements.
4. **Pre-crash alert** on climbing `Threads_running` + rising lock waits.
5. **2+ hours/week saved** vs. manual DBA investigation.
6. **Incident replay** — `seeql replay` produces a root cause a DevOps engineer can read in <2 minutes.
7. **False positive rate** for anomaly alerts under 10% after 14 days of baseline data.
8. **Zero collection overhead** from anomaly computation (runs on stored data, no extra MySQL queries).

### 4.2 DX success (from DX review)
- TTHW ≤ 4 minutes (from ~45 min).
- Clean-machine install via `docker run seeql/seeql demo`.
- All top-10 error codes produce Tier-2 messages (problem + cause + fix + docs URL).
- `seeql doctor` catches the 7 common misconfigurations.

### 4.3 Dashboard success (from design review)
- Lighthouse a11y ≥90 on all dashboard pages.
- Fresh-deploy state cannot read as "healthy" when no data exists.
- Action Center "applied" UI closes the feedback loop on recommendations.

---

## Appendix A — Implementation Order Cheat Sheet

Ordered by "next thing to touch":

```
Phase 0:
  [0.1] git init                                            ← start here
  [0.2] update CLAUDE.md
  [0.3] SIGTERM handler in scheduler/runner.py
  [0.4] server_id on detect_anomalies + all rules
  [0.5] buffer pool hit ratio: compute from global_status counters
  [0.6] query detail: render query_sample_text with pattern/sample label

Phase 1 (ACTIVE CEO plan):
  [1.1] schema.sql: anomaly_events, incident_windows + retention override
  [1.2] AnomalyResult.detected_at + per-cycle cache in alerting/anomaly.py
  [1.3] alerting/anomaly_store.py + scheduler hook
  [1.4] alerting/incidents.py gap-based windowing
  [1.5] main.py argparse subparsers (backward-compat)
  [1.6] agent/replay.py + incident investigator prompt + timeline-only fallback
  [1.7] agent/llm_agent.py: run_llm_analysis() public wrapper
  [1.8] main.py: replay --from/--to/--incident/--latest
  [1.9] main.py: incidents list
  [1.10] api/dashboard_api.py + incidents_timeline.html partial
  [1.11] Slack webhook for new incidents in alerting/incidents.py
  [1.12] state_builder.py: Recent Incidents section

Phase 2 (DX for OSS launch):
  [2.1] pyproject scripts, LICENSE, CONTRIBUTING, CHANGELOG
  [2.2] .github/workflows/{ci,release}.yml → Docker Hub
  [2.3] seeql demo + examples/demo.db
  [2.4] seeql init + seeql doctor + graceful perf_schema degradation
  [2.5] E001–E010 error catalog
  [2.6] /api/v1/* unification + /docs + config deprecation shim
  [2.7] /onboarding page
  [2.8] docker-compose.dev.yml + make dev
  [2.9] docs/ + examples/ restructure + tutorial-first-incident.md
  [2.10] README overhaul with GIF
  [2.11.1] global time range picker + custom from/to
  [2.11.2] schema + table filter across dashboard + /api/v1/schemas endpoint
  [2.11.3] threads chart: highlight running vs connected

Phase 3 (dashboard polish):
  [3.1] ARIA pass (aria-live, skip link, canvas labels, tooltip focus, touch targets)
  [3.2] First-run onboarding per-component
  [3.3] Chart loading/error states
  [3.4] Action Center applied UI
  [3.5] Resolve 4 deferred design decisions
  [3.6] DESIGN.md via /design-consultation

Phase 4: deferred until signal arrives
```

---

## Appendix B — Dashboard Design Specifications (preserved)

The SeeQL web dashboard is built and serves as the primary interface for monitoring and acting on database health. Phase 3.6 replaces this appendix with a standalone `DESIGN.md`; until then this is the reference.

### Design System — "Sketch on Paper" Aesthetic

**Color Tokens:**
| Token | Hex | Usage |
|-------|-----|-------|
| `paper` | `#fdfbf7` | Page background, card backgrounds |
| `pencil` | `#2d2d2d` | Primary text, borders, dark UI elements |
| `erased` | `#e5e0d8` | Dashed borders, subtle dividers, disabled state |
| `marker` | `#ff4d4d` | Danger/critical: lock alerts, regressions, emergency items |
| `pen` | `#2d5da1` | Information/action: links, optimization suggestions, DDL changes |
| `postit` | `#fff9c4` | Highlights, hover states, post-it note decorations |

**Semantic Severity Colors:**
- `.severity-red` — `bg: #fee2e2, border: #ff4d4d` — active danger (lock cascades, health=red)
- `.severity-yellow` — `bg: #fef9c3, border: #eab308` — warning (long transactions, elevated metrics)
- `.severity-green` — `bg: #dcfce7, border: #22c55e` — healthy/resolved

**Typography:**
- Headings: `Kalam` (cursive, bold) — page titles, KPI numbers, section headers, chart labels
- Body: `Patrick Hand` (cursive) — paragraph text, table cells, nav links, tooltips
- Code/SQL: system monospace — EXPLAIN output, DDL diffs, copy-able SQL commands
- No other fonts. Two typefaces max.

**Border-Radius (Wobbly Variants):**
| Class | Radius | Use For |
|-------|--------|---------|
| `.wobbly` | `255px 15px 225px 15px / 15px 225px 15px 255px` | Primary containers, KPI cards |
| `.wobbly-md` | `15px 225px 15px 255px / 255px 15px 225px 15px` | Alert cards, chart containers |
| `.wobbly-sm` | `225px 15px 255px 15px / 15px 255px 15px 225px` | Buttons, tags, small elements |

**Shadows:**
| Class | Value | Use For |
|-------|-------|---------|
| `shadow-hard` | `4px 4px 0px 0px #2d2d2d` | Primary interactive cards (KPI, alerts) |
| `shadow-hard-sm` | `2px 2px 0px 0px #2d2d2d` | Buttons, pagination, small elements |
| `shadow-hard-lg` | `8px 8px 0px 0px #2d2d2d` | Dropdowns, modals |
| `shadow-hard-subtle` | `3px 3px 0px 0px rgba(45,45,45,0.1)` | Charts, data tables (non-interactive) |

**Decorative Elements:**
- `.tape` — centered semi-transparent strip above element (use on primary KPI cards)
- `.tack` — red pushpin dot at top center (use on pinned/important sections)
- Element rotation: `-1deg` to `+1.5deg` range, applied via inline `style="transform: rotate(Xdeg)"`. Use consistently per element type.

**Interactive Patterns:**
- Copy button: SVG clipboard icon, opacity 0→100 on parent hover, green checkmark flash on success
- Expand/collapse: right-pointing triangle (`&#9654;`) that rotates 90deg when open
- Info tooltip: `data-tip` attribute, appears on hover (Phase 3.1 adds keyboard focus support)
- HTMX auto-refresh: 30s interval, opacity:0.6 during request

### Screen Inventory

**Navigation Order:** Overview → Action Center → Queries → Locks → Schema → Server

```
SeeQL Dashboard
├── Overview              "Is the server OK?"
│   ├── Health bar        HEALTHY/WARNING/ALERT
│   ├── 4 KPI cards       threads, locks, buffer pool, top query
│   ├── 2 charts          QPS trend, threads trend
│   ├── Active alerts     current issues with expandable detail
│   └── Incidents widget  (new — Phase 1.10) recent detected incidents
├── Action Center         "What should I do?"
│   ├── Emergency         stop-a-crash items (red)
│   ├── Diagnostics       investigate-and-fix items (yellow)
│   ├── Optimization      queries to tune, indexes to add/drop (blue)
│   ├── System insights   informational
│   └── Applied           items marked done, with before/after (Phase 3.4)
├── Queries               "Which queries are hurting us?"
│   ├── Regression banner queries 3x+ slower than baseline
│   ├── Sortable table    sort by execs, avg time, total time, scans
│   └── Expandable detail full query, stats, EXPLAIN, trend chart
├── Locks                 "Who is blocking who?"
├── Schema                "What changed?"
└── Server                "How are resources doing?"
```

---

## Appendix C — Original 6-Week Roadmap (historical)

Preserved for context. Weeks 1–2 are complete; weeks 3–6 landed as the Agent + Alerting + API layers currently in the repo. Phase 1 of this plan builds directly on top of that foundation.

### Week 1 — Data Collection Foundation  ✅ COMPLETE
- Project structure + modular collector architecture
- MySQL connection pooling for production DB (read-only `dba_agent` user)
- SQLite monitoring storage (WAL mode)
- Three collection loops via APScheduler (fast 30s / medium 5m / slow 30m)
- Global status delta calculator
- Retry logic for transient MySQL failures
- CLI with `--check`, `--init-db`, `--once`, continuous run
- Dockerfile with healthcheck

### Week 2 — GCP Integration + Data Enrichment  ✅ COMPLETE
- GCP Cloud Monitoring collector (CPU, memory, disk IO, connections)
- Cloud Logging slow query collector
- `SHOW ENGINE INNODB STATUS` parser
- Daily data retention cleanup
- EXPLAIN plan auto-capture for top-N expensive queries

### Week 3 — State Builder + Basic LLM Agent  ✅ COMPLETE
- Structured state builder (`agent/state_builder.py`, 485 lines)
- LLM agent with Gemini (Vertex AI) and Claude (Anthropic API) support
- Periodic 30-min analysis producing structured findings
- `agent_analyses` table

### Week 4 — Agent Tools + Deep Dive  ✅ COMPLETE
- 8 agent tool functions (`agent/tools.py`)
- Anomaly-triggered deep dives
- Feedback tracking scaffolding for applied recommendations (UI surface deferred to Phase 3.4)

### Week 5 — Alerting + Reporting  ✅ COMPLETE
- 6 built-in alerting rules (`alerting/rules.py`)
- 3 channels: Slack, webhook, log
- Anomaly detection layer (`alerting/anomaly.py`)

### Week 6 — Hardening  ◐ PARTIAL
- Prometheus `/metrics` endpoint  ✅
- Index analysis collectors  ✅
- API layer (agent routes, dashboard routes, dashboard API)  ✅
- Remaining hardening items → §3.3 of this plan

---

## Cost Estimate (unchanged)

| Item | Monthly Cost |
|------|-------------|
| GCE VM (e2-small) | ~₹1,200 |
| Claude API (Opus 4.6 via Vertex, ~50 analyses/day + replay) | ~₹2,000–4,000 |
| SQLite storage | ₹0 (local disk) |
| **Total** | **~₹3,200–5,200/month** |

Compare to ₹1–2L/month for Datadog DBM, or the cost of one 3 AM incident caused by a missed lock cascade.
