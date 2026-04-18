# SeeQL — LLM-powered MySQL DBA agent

Continuously observes any MySQL 8.0+ database, detects anomalies and incidents,
and uses an LLM (Claude or Gemini) to explain what's wrong and how to fix it.
Runs as a single container with a Prometheus `/metrics` endpoint and a
sketch-aesthetic dashboard. Works out of the box against local MySQL, GCP
Cloud SQL, AWS RDS/Aurora, or self-hosted.

<!-- Screenshot placeholder — capture and drop in docs/screenshots/dashboard.png -->

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-blue?logo=docker)](https://github.com/shubhankar-mohan/SeeQL/pkgs/container/seeql)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)
[![Build status](https://img.shields.io/github/actions/workflow/status/shubhankar-mohan/SeeQL/docker-publish.yml?branch=main)](https://github.com/shubhankar-mohan/SeeQL/actions/workflows/docker-publish.yml)

---

## Table of contents

- [Quickstart](#quickstart)
- [Install via Docker](#install-via-docker)
- [Install from source](#install-from-source)
- [MySQL prerequisites](#mysql-prerequisites)
- [Configuration](#configuration)
- [CLI](#cli)
- [Dashboard](#dashboard)
- [Prometheus](#prometheus)
- [Alerting](#alerting)
- [API](#api)
- [GCP / Cloud SQL extras](#gcp--cloud-sql-extras)
- [FAQ](#faq)
- [Contributing](#contributing)
- [License](#license)

---

## Quickstart

One `docker run` against any MySQL 8.0+ — no GCP account required.

```bash
docker run -d --name seeql \
  -p 8080:8080 \
  -e PROD_DB_HOST=your-mysql-host \
  -e PROD_DB_USER=dba_agent \
  -e PROD_DB_PASSWORD=your_password \
  -e PROD_DB_DATABASE=your_database \
  -v seeql-data:/app/data \
  -v seeql-logs:/app/logs \
  ghcr.io/shubhankar-mohan/seeql:latest
```

Then:

```bash
curl http://localhost:8080/health          # health probe
curl http://localhost:8080/metrics | head  # Prometheus metrics
open http://localhost:8080                 # dashboard
```

> **LLM agent is opt-in.** The quickstart above collects metrics and runs
> anomaly detection without any LLM. Add `-e ANTHROPIC_API_KEY=sk-ant-...`
> and `-e SEEQL_AGENT_ENABLED=true` to get Claude-written root-cause
> narrations on detected incidents.

---

## Install via Docker

**Pull:**

```bash
docker pull ghcr.io/shubhankar-mohan/seeql:latest
# or the GCP variant (adds Cloud Monitoring + Cloud Logging collectors)
docker pull ghcr.io/shubhankar-mohan/seeql:latest-gcp
```

Images are built for `linux/amd64` and `linux/arm64` (works on Apple Silicon,
Graviton, Raspberry Pi).

**docker-compose (recommended):**

```bash
# Generic
cp .env.example .env             # fill in PROD_DB_* values
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
| `slow_query_log` | `on` | Slow query log collector |
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

---

## Configuration

Every knob can be set via environment variable OR `settings.local.yaml`.
Env vars win over file config. See
[docs/config.md](docs/config.md) for the full matrix.

Most common env vars:

| Variable | Required | Default | What |
|----------|----------|---------|------|
| `PROD_DB_HOST` | yes | — | MySQL host |
| `PROD_DB_PORT` | | 3306 | MySQL port |
| `PROD_DB_USER` | | `dba_agent` | Monitoring user |
| `PROD_DB_PASSWORD` | yes | — | Monitoring password |
| `PROD_DB_DATABASE` | yes | — | Default schema (for EXPLAIN) |
| `SEEQL_AGENT_ENABLED` | | `false` | Enable LLM root-cause analysis |
| `SEEQL_AGENT_MODEL` | | `claude-sonnet-4-6` | LLM model name |
| `ANTHROPIC_API_KEY` | | — | Claude API key (if using Claude) |
| `SEEQL_ALERTING_ENABLED` | | `false` | Evaluate alert rules |
| `SLACK_WEBHOOK_URL` | | — | Slack incoming webhook |
| `SEEQL_FAST_INTERVAL` | | `30` | Fast loop interval (seconds) |
| `SEEQL_MEDIUM_INTERVAL` | | `300` | Medium loop interval (seconds) |
| `SEEQL_SLOW_INTERVAL` | | `1800` | Slow loop interval (seconds) |
| `SEEQL_DB_MAX_SIZE_MB` | | `5000` | Max SQLite DB size (MB) |
| `SEEQL_RETENTION_DAYS` | | `90` | Data retention (days) |
| `SEEQL_LOG_LEVEL` | | `INFO` | Log level |

GCP-specific vars only apply when you're using the `-gcp` image
or `[gcp]` extra — see [GCP extras](#gcp--cloud-sql-extras).

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
```

Full reference in [docs/cli.md](docs/cli.md).

---

## Dashboard

Served at `http://<host>:8080/` — overview, queries, locks, schema, server,
and incidents pages. HTMX auto-refresh, no SPA build step, ARIA live regions
on auto-updating widgets.

See [docs/dashboard.md](docs/dashboard.md) for a per-page tour.

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

Six deterministic rules plus one statistical anomaly rule, all configurable:

| Rule | Default trigger | Severity |
|------|----------------|----------|
| `lock_cascade` | ≥3 lock waits, max wait >10s | critical |
| `threads_running_spike` | 4× above 24h baseline | warning |
| `query_regression` | Any query 5× slower than 7d baseline | warning |
| `ddl_change` | Any schema change detected | info |
| `high_cpu` | CPU > 85% | warning |
| `deadlock_detected` | Deadlock in `SHOW ENGINE INNODB STATUS` | critical |
| `anomaly_detection` | z-score > 3 on same-hour-same-weekday baseline | warning |

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

---

## GCP / Cloud SQL extras

The `[gcp]` optional extra (and the `-gcp` image variant) add:

- Cloud Monitoring collector — CPU, memory, disk, network metrics for
  Cloud SQL instances
- Cloud Logging slow-query collector — pulls from
  `cloudsql.googleapis.com/mysql-slow.log`
- Google GenAI SDK — Gemini via Vertex AI, and Claude via Vertex AI
  (`AnthropicVertex`)

**Service account roles required:**

- `roles/monitoring.viewer` — Cloud Monitoring API
- `roles/logging.viewer` — Cloud Logging API
- `roles/aiplatform.user` — optional, only if using Vertex AI for the LLM

**Compose:**

```bash
export PROD_DB_HOST=... PROD_DB_PASSWORD=... PROD_DB_DATABASE=...
export GCP_PROJECT_ID=... GCP_CLOUD_SQL_INSTANCE=...
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

**Is my data ever sent to the LLM?** Only on explicit analysis runs
(`/api/v1/agent/analyze`, the scheduled agent, or `seeql replay`), and
only the structured state report — no raw rows or customer data. Metrics
stay in SQLite on the host that runs SeeQL.

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
