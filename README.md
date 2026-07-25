# SeeQL — LLM-powered MySQL DBA agent

Continuously observes any MySQL 8.0+ database, detects anomalies and incidents,
and uses an LLM (Claude or Gemini) to explain what's wrong and how to fix it.
Runs as a single container with a Prometheus `/metrics` endpoint and a
sketch-aesthetic dashboard. Works out of the box against local MySQL, GCP
Cloud SQL, AWS RDS/Aurora, or self-hosted.

<!-- Screenshots use absolute URLs so they also render on Docker Hub.
     All data shown is the One Piece-themed "Grand Line" demo dataset
     (scripts/seed_demo.py) — no real workloads. -->
![SeeQL dashboard — overview page during the demo lock-cascade incident on the Grand Line dataset](https://raw.githubusercontent.com/shubhankar-mohan/SeeQL/main/docs/screenshots/overview.png)

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docker Hub](https://img.shields.io/badge/docker%20hub-shubhankarmohan%2Fseeql-blue?logo=docker)](https://hub.docker.com/r/shubhankarmohan/seeql)
[![GHCR](https://img.shields.io/badge/ghcr.io-shubhankar--mohan%2Fseeql-blue?logo=docker)](https://github.com/shubhankar-mohan/SeeQL/pkgs/container/seeql)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Build status](https://img.shields.io/github/actions/workflow/status/shubhankar-mohan/SeeQL/docker-publish.yml?branch=main)](https://github.com/shubhankar-mohan/SeeQL/actions/workflows/docker-publish.yml)

Requires Python 3.12+ (uses modern typing syntax).

---

## Table of contents

- [Quickstart](#quickstart)
- [Install via Docker](#install-via-docker)
- [Install from source](#install-from-source)
- [MySQL prerequisites](#mysql-prerequisites)
- [Configuration](#configuration)
- [CLI](#cli)
- [Dashboard](#dashboard)
- [Use it from Claude (MCP)](#use-it-from-claude-mcp)
- [Alert → automated investigation](#alert--automated-investigation)
- [Incident replay](#incident-replay)
- [Prometheus](#prometheus)
- [Alerting](#alerting)
- [API](#api)
- [GCP / Cloud SQL extras](#gcp--cloud-sql-extras)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

---

## Quickstart

One config file + one `docker run`, against any MySQL 8.0+ — no GCP account required.

```bash
# 1. Create your config (see seeql.example.yml for all options):
cat > seeql.yml <<'YAML'
servers:
  prod:
    host: your-mysql-host
    user: dba_agent
    password: ${PROD_DB_PASSWORD}    # injected via -e below
    database: your_database          # optional (default schema for EXPLAIN)
YAML

# 2. Run it — mount the config, pass the secret:
docker run -d --name seeql \
  -p 8080:8080 \
  -v "$PWD/seeql.yml":/etc/seeql/seeql.yml:ro \
  -e PROD_DB_PASSWORD=your_password \
  -v seeql-data:/app/data \
  -v seeql-logs:/app/logs \
  shubhankarmohan/seeql:latest
```

Then:

```bash
curl http://localhost:8080/health          # health probe
curl http://localhost:8080/metrics | head  # Prometheus metrics
open http://localhost:8080                 # dashboard
```

> **Multiple hosts?** Add more entries under `servers:` — one per MySQL
> instance (all databases inside an instance are monitored automatically).
> See [docs/config.md](docs/config.md).
>
> **LLM agent is opt-in.** Metrics + anomaly detection run without any LLM.
> Enable it with `agent: {enabled: true, model: gemini-2.5-flash}`
> (gemini-2.5-flash is the shipped model default; needs a GCP project +
> Vertex AI credentials) — or use any `claude-*` model +
> `-e ANTHROPIC_API_KEY=sk-ant-...` for a GCP-free setup with Claude-written
> root-cause narrations.

---

## Install via Docker

**Pull:**

```bash
# Docker Hub
docker pull shubhankarmohan/seeql:latest
# or GitHub Container Registry (GHCR)
docker pull ghcr.io/shubhankar-mohan/seeql:latest
# GCP variant (adds Cloud Monitoring + Cloud Logging collectors)
docker pull shubhankarmohan/seeql:latest-gcp
```

Images are built for `linux/amd64` and `linux/arm64` (works on Apple Silicon,
Graviton, Raspberry Pi).

**docker-compose (recommended):**

```bash
# Generic
cp seeql.example.yml seeql.yml   # edit: your servers / hosts
cp .env.example .env             # set PROD_DB_PASSWORD (+ any other secrets)
docker compose up -d

# GCP Cloud SQL
docker compose -f docker-compose.gcp.yml up -d
```

**Tags:**

| Tag | Meaning |
|-----|---------|
| `latest` | Latest release, generic image |
| `latest-gcp` | Latest release, with GCP collectors |
| `vX.Y.Z` / `vX.Y.Z-gcp` | Specific version |
| `sha-<short>` | Specific commit |

---

## Install from source

For contributors, or to run the bleeding edge without waiting for a release.

```bash
# Requires Python 3.12+ and a reachable MySQL 8.0+
git clone https://github.com/shubhankar-mohan/SeeQL.git && cd SeeQL
python3.12 -m venv venv && source venv/bin/activate
pip install -e ".[dev,api]"                # add ',gcp' for Cloud Monitoring

# First-time setup: copy the settings template and fill in your MySQL details
cp config/settings.yaml settings.local.yaml
$EDITOR settings.local.yaml

# Initialize the monitoring SQLite DB and run a preflight check
seeql init-db
seeql doctor

# Start collectors + API + dashboard
seeql serve
```

---

## MySQL prerequisites

### 1. Dedicated read-only monitoring user

```sql
CREATE USER 'dba_agent'@'%' IDENTIFIED BY 'strong_password_here';
GRANT SELECT, PROCESS ON *.* TO 'dba_agent'@'%';
-- Optional: cap resource usage so runaway queries can't spike prod
ALTER USER 'dba_agent'@'%' WITH MAX_QUERIES_PER_HOUR 10000;
FLUSH PRIVILEGES;
```

### 2. MySQL server flags

SeeQL depends on `performance_schema` and the slow query log.

| Flag | Value | Why |
|------|-------|-----|
| `performance_schema` | `on` | Query digests, wait events, lock waits |
| `performance_schema` consumers/instruments | enabled | digests/waits/stages data — granular state isn't individually verified by `seeql doctor` (it only checks the top-level flag above) |
| `slow_query_log` | `on` | Slow query log collector — ingested via Cloud Logging on the `-gcp` image; on other platforms the `Slow_queries` counter is tracked but log *entries* are not collected yet |
| `long_query_time` | `1` | Log queries > 1s |
| `innodb_monitor_enable` | `all` | 300+ InnoDB internal metrics |

On managed services (Cloud SQL, RDS, Aurora) these live in the instance
parameters. For self-hosted MySQL, set them in `my.cnf` under `[mysqld]`:

```ini
[mysqld]
performance_schema=ON
slow_query_log=ON
long_query_time=1
innodb_monitor_enable=all
```

Restart the server after changing these.

### 3. Stage instrumentation (optional, for execution-stage breakdowns)

The `execution_stages` collector reports where query time goes (parsing,
optimizing, sorting, sending data). It needs two `performance_schema`
instruments turned on that are off by default — enable at runtime:

```sql
UPDATE performance_schema.setup_instruments
SET ENABLED = 'YES', TIMED = 'YES'
WHERE NAME LIKE 'stage/%';

UPDATE performance_schema.setup_consumers
SET ENABLED = 'YES'
WHERE NAME LIKE 'events_stages%';
```

This does not persist across a MySQL restart. On Cloud SQL, make it
permanent with the `performance-schema-instrument` flag set to
`stage/%=ON`. On self-hosted MySQL, add the equivalent
`performance-schema-instrument` line to `my.cnf`.

---

## Configuration

SeeQL is configured by **one YAML file** (mounted at `/etc/seeql/seeql.yml` in
Docker, or pointed to by `SEEQL_CONFIG`). Secrets are injected into that file
via `${VAR}` placeholders resolved from the environment / `.env`. Connection
and server settings are **file-only by design** — there are no `PROD_DB_*`
env overrides.

| Variable | What |
|----------|------|
| `SEEQL_CONFIG` | Path to your config file (default `/etc/seeql/seeql.yml`) |
| `PROD_DB_PASSWORD` (via `${…}`) | MySQL password referenced from the config file |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `OPENAI_BASE_URL` | LLM credentials |
| `SLACK_WEBHOOK_URL` (via `${…}`) | Slack alerts channel |
| `SEEQL_AGENT_ENABLED` | Override `agent.enabled` (kill-switch) |
| `SEEQL_API_PORT` | HTTP port (default 8080) |
| `SEEQL_MON_DB_PATH` | Monitoring SQLite path |
| `SEEQL_DB_MAX_SIZE_MB` | SQLite size cap (default 5000) |
| `SEEQL_RETENTION_DAYS` | Data retention (default 90) |
| `SEEQL_LOG_LEVEL` / `SEEQL_LOG_MAX_SIZE_MB` | Logging knobs |
| `SEEQL_PROM_CACHE_TTL` | /metrics re-read cadence seconds (default 10) |

Everything else (`servers:`, intervals, `agent:`, `alerting:`, `webhooks:`,
`mcp:`) lives in the YAML — see [docs/config.md](docs/config.md).

---

## CLI

```bash
seeql check                       # preflight: MySQL + SQLite + config
seeql doctor                      # diagnostic sweep (env, perms, flags)
seeql init-db                     # create the monitoring SQLite schema
seeql run                         # run collectors continuously
seeql run --once                  # run a single cycle of all loops
seeql serve                       # scheduler + API + dashboard
seeql serve --no-scheduler        # API only (e.g. behind a dedicated collector)
seeql replay --latest             # reconstruct + narrate the most recent incident
seeql replay --incident 42        # narrate a specific incident id
seeql replay --from <ts> --to <ts>
seeql incidents list              # browse detected incident windows
seeql investigations list|show|trigger|abort  # webhook-triggered investigations
seeql mcp [--http]                 # run the MCP server (stdio by default)
```

Full reference in [docs/cli.md](docs/cli.md).

---

## Dashboard

Served at `http://<host>:8080/` — overview, queries, locks, schema, server,
and an Action Center page; incidents render as a timeline widget on
Overview. HTMX auto-refresh, no SPA build step, ARIA live regions on
auto-updating widgets.

*(All screenshots show the One Piece-themed **Grand Line** demo dataset —
`pirates`, `crews`, `bounties`, `devil_fruits` — staged mid lock-cascade
incident so every page has something to say. Rebuild it yourself with
`python scripts/seed_demo.py`; capture with `scripts/screenshot_demo.py`.
Not a real production workload.)*

**Query Performance** — per-digest execs, latency, rows examined, and full-table-scan flags:

![Query Performance page — slowest query digests with average time, total time, and scan flags](https://raw.githubusercontent.com/shubhankar-mohan/SeeQL/main/docs/screenshots/queries.png)

**Action Center** — ranked optimization candidates, diagnostics, and emergencies:

![Action Center page — queries to optimize with exec counts, scan ratios, and index suggestions](https://raw.githubusercontent.com/shubhankar-mohan/SeeQL/main/docs/screenshots/action-center.png)

**Locks & Transactions** — live lock waits, 24h lock history, and active transactions:

![Locks and Transactions page — current lock waits and active transactions](https://raw.githubusercontent.com/shubhankar-mohan/SeeQL/main/docs/screenshots/locks.png)

**Server Metrics** — QPS, threads, buffer pool hit ratio, and top wait events:

![Server Metrics page — QPS, threads, buffer pool hit ratio charts](https://raw.githubusercontent.com/shubhankar-mohan/SeeQL/main/docs/screenshots/server.png)

**Schema & Indexes** — table sizes, DDL change history, unused/redundant index analysis:

![Schema and Indexes page — table sizes with row counts and data/index MB](https://raw.githubusercontent.com/shubhankar-mohan/SeeQL/main/docs/screenshots/schema.png)

See [docs/dashboard.md](docs/dashboard.md) for a per-page tour.

---

## Use it from Claude (MCP)

SeeQL ships an MCP server: 28 read-only/gated tools (state reports, query
history, EXPLAINs, live locks — plus opt-in actions) behind a safety layer
with per-session budgets and rate limits.

```bash
seeql mcp            # stdio, for Claude Desktop / Claude Code
seeql mcp --http     # bearer-token HTTP for remote clients
```

Point Claude at your database and ask "why is checkout slow?" —
setup in [docs/mcp.md](docs/mcp.md).

---

## Alert → automated investigation

`POST /webhooks/{provider}` (PagerDuty, Grafana, GCP Monitoring, generic;
HMAC-verified) triggers a 3-phase investigation: zero-cost triage from
collected data → budgeted LLM root-cause analysis → bounded follow-up
sampling with load guards. Results land in the dashboard and
`seeql investigations`. See [docs/incidents.md](docs/incidents.md).

---

## Incident replay

```bash
seeql replay --latest        # timeline + LLM postmortem of the last incident
```

Works without an LLM key too (timeline-only postmortem primer).

---

## Prometheus

Scrape `http://<host>:8080/metrics`:

```yaml
scrape_configs:
  - job_name: seeql
    scrape_interval: 15s
    static_configs:
      - targets: ['seeql:8080']
```

Exposes ~20 gauges/counters covering threads, QPS, lock waits, buffer pool
hit ratio, cloud infrastructure metrics, unused/redundant index counts, and
the SeeQL alert-firings counter. Full list in
[docs/api.md#prometheus-metrics](docs/api.md).

`SEEQL_PROM_CACHE_TTL` (default 10s) controls the re-read cadence from the
monitoring SQLite DB. Match it to your scrape interval.

---

## Alerting

Seven deterministic rules plus one statistical anomaly rule, all configurable:

| Rule | Default trigger | Severity |
|------|----------------|----------|
| `lock_cascade` | ≥3 lock waits, max wait >10s | critical |
| `threads_running_spike` | 4× above 24h baseline | warning |
| `query_regression` | Any query 5× slower than 7d baseline | warning |
| `ddl_change` | Any schema change detected | info |
| `high_cpu` | CPU > 85% | warning |
| `high_memory` | Memory > 85% | warning |
| `deadlock_detected` | Deadlock in `SHOW ENGINE INNODB STATUS` | critical |
| `anomaly_detection` | z-score > 3 on same-hour-same-weekday baseline | warning |

`anomaly_detection` escalates to critical at z ≥ 1.5× threshold (default
z ≥ 4.5, since the default `z_threshold` is 3.0).

Channels: Slack, generic webhook, log. Cooldowns are per-rule and
per-server. See [docs/alerting.md](docs/alerting.md) for tuning.

---

## API

FastAPI app at `http://<host>:8080/`. Full reference:
[docs/api.md](docs/api.md). Most common endpoints:

| Method | Path | What |
|--------|------|------|
| `GET` | `/health` | MySQL + SQLite health |
| `GET` | `/metrics` | Prometheus metrics |
| `POST` | `/collect/fast` (`/medium`, `/slow`, `/all`) | Trigger a cycle manually |
| `GET` | `/api/v1/state-report` | Full state report (Markdown + JSON) |
| `POST` | `/api/v1/agent/analyze` | Trigger an LLM analysis |
| `GET` | `/api/v1/anomalies` | Current statistical anomalies |
| `GET` | `/api/v1/incidents/recent` | Detected incident windows |
| `GET` | `/api/v1/alerts` | Alert history |

**No auth by default — never expose this publicly without a token.** See
[Securing the endpoint](docs/deployment.md#securing-the-endpoint).

---

## GCP / Cloud SQL extras

The `[gcp]` optional extra (and the `-gcp` image variant) add:

- Cloud Monitoring collector — CPU, memory, disk, network metrics for
  Cloud SQL instances
- Cloud Logging slow-query collector — pulls from
  `cloudsql.googleapis.com/mysql-slow.log`
- Google GenAI SDK — Gemini via Vertex AI, and Claude via Vertex AI
  (`AnthropicVertex`)

> **RDS / Aurora / self-hosted:** Infra metrics (CPU/mem/disk) are
> GCP-only today — on RDS/Aurora/self-hosted, MySQL-level monitoring
> works fully but `high_cpu`/`high_memory` and 2 of 7 anomaly-detection
> metrics (`cpu_utilization`, `memory_utilization`) are inactive since
> nothing populates the `gcp_metric_snapshots` table they read from. A
> CloudWatch collector is planned.

**Service account roles required:**

- `roles/monitoring.viewer` — Cloud Monitoring API
- `roles/logging.viewer` — Cloud Logging API
- `roles/aiplatform.user` — optional, only if using Vertex AI for the LLM

**Compose:**

```bash
# seeql.yml: add host/database + a per-server `gcp:` block
#   (project_id, cloud_sql_instance_id) — see docs/config.md
cp seeql.example.yml seeql.yml && $EDITOR seeql.yml
export PROD_DB_PASSWORD=your_password
docker compose -f docker-compose.gcp.yml up -d
```

Full walkthrough in [docs/deployment.md#gcp-cloud-sql](docs/deployment.md).

---

## FAQ

**Can I use Postgres?** No. MySQL 8.0+ only. Postgres has a different
`information_schema` and no `performance_schema` equivalent.

**Does it work without an LLM key?** Yes. Leave `SEEQL_AGENT_ENABLED=false`
and you still get all collectors, anomaly detection, incidents, the
dashboard, Prometheus, and alerting. You lose root-cause narration.

**Is my data ever sent to the LLM?** Only on analysis runs (scheduled agent,
`/api/v1/agent/analyze`, `seeql replay`, webhook investigations). What's sent:
the structured state report plus tool results. Query *fingerprints* are
normalized (`?` placeholders). Live statement text (processlist, transactions,
slow-log samples) can contain literal values from your workload; by default
SeeQL masks those literals (`agent.redact_sql_literals: true`) before they
reach the model. SeeQL never reads or transmits table row data. Metrics stay
in SQLite on the host that runs SeeQL.

**Does SeeQL write to the production MySQL?** No. `SELECT` + `PROCESS`
grants are sufficient. Live tool calls (`run_explain`, `get_live_*`)
execute `EXPLAIN` or read-only `performance_schema`/`information_schema`
queries only.

**What's the performance overhead on the target MySQL?** Roughly 80 queries
per 5-minute medium loop, 4 quick queries per 30-second fast loop. On any
production workload this is unmeasurable.

**Can I run it alongside PMM or other monitoring?** Yes. SeeQL is a
read-only observer. No port conflicts with Prometheus or Grafana — the
monitoring network in `docker-compose.yml` is isolated.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome at
<https://github.com/shubhankar-mohan/SeeQL/issues>.

## License

Apache-2.0 — see [LICENSE](LICENSE).
