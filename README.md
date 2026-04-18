# SeeQL — LLM-Powered MySQL DBA Agent

Continuously collects metrics from a production MySQL 8.0 database (GCP Cloud SQL), stores them in SQLite, runs anomaly detection, and feeds everything to an LLM agent (Gemini/Claude) that acts as a senior DBA — detecting problems, correlating root causes, and suggesting optimizations before they become outages.

Exposes a Prometheus `/metrics` endpoint so it runs alongside your existing Prometheus + Grafana stack.

## Quick Start (Docker)

### 1. Prerequisites

- Docker
- Network access to your Cloud SQL instance (private IP or Cloud SQL Auth Proxy)
- A GCP service account JSON for Cloud Monitoring + Vertex AI
- A dedicated MySQL monitoring user:

```sql
CREATE USER 'dba_agent'@'%' IDENTIFIED BY 'strong_password_here';
GRANT SELECT ON *.* TO 'dba_agent'@'%';
GRANT PROCESS ON *.* TO 'dba_agent'@'%';
FLUSH PRIVILEGES;
```

- Cloud SQL flags (requires instance restart):

| Flag | Value | Why |
|------|-------|-----|
| `performance_schema` | `on` | All perf_schema queries |
| `slow_query_log` | `on` | Cloud Logging slow query collector |
| `long_query_time` | `1` | Log queries >1s |
| `innodb_monitor_enable` | `all` | 300+ InnoDB internal metrics |

### 2. Build

```bash
docker build -t seeql .
```

### 3. Create persistent directories

```bash
mkdir -p /opt/seeql/data /opt/seeql/logs
```

### 4. Run

```bash
docker run -d \
  --name seeql \
  --restart unless-stopped \
  --network monitoring \
  -p 8080:8080 \
  \
  # --- MySQL Production DB ---
  -e PROD_DB_HOST=10.0.0.1 \
  -e PROD_DB_PORT=3306 \
  -e PROD_DB_USER=dba_agent \
  -e PROD_DB_PASSWORD=your_password \
  -e PROD_DB_DATABASE=your_database \
  \
  # --- GCP ---
  -e GCP_PROJECT_ID=your-project \
  -e GCP_REGION=asia-south1 \
  -e GCP_CLOUD_SQL_INSTANCE=your-instance-id \
  -v /path/to/gcp-sa.json:/app/gcp-sa.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-sa.json \
  \
  # --- Persistent storage (REQUIRED) ---
  -v /opt/seeql/data:/app/data \
  -v /opt/seeql/logs:/app/logs \
  \
  # --- Size limits ---
  -e SEEQL_DB_MAX_SIZE_MB=5000 \
  -e SEEQL_LOG_MAX_SIZE_MB=500 \
  -e SEEQL_RETENTION_DAYS=90 \
  \
  # --- Collection intervals (seconds) ---
  -e SEEQL_FAST_INTERVAL=30 \
  -e SEEQL_MEDIUM_INTERVAL=300 \
  -e SEEQL_SLOW_INTERVAL=1800 \
  \
  # --- LLM Agent (Gemini via Vertex AI) ---
  -e SEEQL_AGENT_ENABLED=true \
  -e SEEQL_AGENT_MODEL=gemini-2.0-flash \
  \
  # --- Alerting ---
  -e SEEQL_ALERTING_ENABLED=true \
  -e SLACK_WEBHOOK_URL=https://hooks.slack.com/services/xxx \
  \
  # --- Logging ---
  -e SEEQL_LOG_LEVEL=INFO \
  \
  seeql
```

### 5. Verify

```bash
# Health check
curl http://localhost:8080/health

# Prometheus metrics
curl http://localhost:8080/metrics

# Trigger a collection cycle
curl -X POST http://localhost:8080/collect/fast

# Check anomalies
curl http://localhost:8080/api/v1/anomalies

# View state report
curl http://localhost:8080/api/v1/state-report

# Trigger LLM analysis
curl -X POST http://localhost:8080/api/v1/agent/analyze
```

---

## Docker Compose

For running alongside Prometheus and Grafana on a monitoring VM:

```bash
# Create the shared network first (if not exists)
docker network create monitoring

# Start SeeQL
docker compose up -d seeql
```

All config is passed via env vars in `docker-compose.yml` or a `.env` file. See `.env.example` for the full list.

---

## Persistent Storage

| Mount | Container Path | Purpose |
|-------|---------------|---------|
| `/opt/seeql/data` | `/app/data` | SQLite database (all collected metrics) |
| `/opt/seeql/logs` | `/app/logs` | Rotating log files |
| `gcp-sa.json` | `/app/gcp-sa.json` | GCP service account (read-only) |

**Data never lives inside the container.** If the container is removed and recreated, all historical data and logs persist on the host.

### Size Limits

| Env Var | Default | What it controls |
|---------|---------|-----------------|
| `SEEQL_DB_MAX_SIZE_MB` | 5000 (5 GB) | Max SQLite database size. When exceeded, retention is automatically shortened until size is under limit. |
| `SEEQL_LOG_MAX_SIZE_MB` | 500 (500 MB) | Max total log size. Distributed across rotating log files. |
| `SEEQL_RETENTION_DAYS` | 90 | Default data retention. Oldest data deleted daily. |

---

## Prometheus Integration

SeeQL exposes a Prometheus-compatible `/metrics` endpoint at `http://<host>:8080/metrics`.

### Scrape Config

Add to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'seeql'
    scrape_interval: 15s
    static_configs:
      - targets: ['seeql:8080']
    # Or if not on the same Docker network:
    # - targets: ['<vm-ip>:8080']
```

### Available Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mysql_threads_running` | Gauge | Active threads (executing queries) |
| `mysql_threads_connected` | Gauge | Total connected threads |
| `mysql_queries_per_second` | Gauge | QPS from SHOW GLOBAL STATUS delta |
| `mysql_slow_queries_per_second` | Gauge | Slow queries per second |
| `mysql_lock_waits_current` | Gauge | Active InnoDB lock waits |
| `mysql_lock_wait_max_seconds` | Gauge | Longest lock wait duration |
| `mysql_buffer_pool_hit_ratio` | Gauge | InnoDB buffer pool hit ratio (target: >0.99) |
| `mysql_buffer_pool_dirty_pages` | Gauge | Dirty pages in buffer pool |
| `mysql_buffer_pool_free_buffers` | Gauge | Free buffers available |
| `mysql_cpu_utilization` | Gauge | Cloud SQL CPU (0-1) |
| `mysql_memory_utilization` | Gauge | Cloud SQL memory (0-1) |
| `mysql_disk_utilization` | Gauge | Cloud SQL disk (0-1) |
| `mysql_disk_read_ops` | Gauge | Disk read operations |
| `mysql_disk_write_ops` | Gauge | Disk write operations |
| `mysql_network_connections` | Gauge | Network connections from Cloud Monitoring |
| `mysql_unused_indexes_count` | Gauge | Detected unused indexes |
| `mysql_redundant_indexes_count` | Gauge | Detected redundant indexes |
| `mysql_innodb_rows_read_per_sec` | Gauge | InnoDB rows read/s |
| `mysql_innodb_row_lock_waits_per_sec` | Gauge | InnoDB row lock waits/s |
| `seeql_alerts_fired_total` | Counter | Total alerts fired (by rule) |

### Recommended Grafana Dashboard Panels

- **QPS + Threads_running** — timeseries, alert on spike
- **Lock waits** — timeseries + stat panel for current count
- **Buffer pool hit ratio** — gauge (red < 0.99)
- **CPU / Memory / Disk** — standard resource panels
- **Unused/Redundant indexes** — stat panels

The `SEEQL_PROM_CACHE_TTL` env var (default 10s) controls how often SeeQL re-reads SQLite for metric updates. Set to match your Prometheus scrape interval.

---

## All Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROD_DB_HOST` | Yes | — | MySQL host (Cloud SQL private IP) |
| `PROD_DB_PORT` | No | 3306 | MySQL port |
| `PROD_DB_USER` | No | dba_agent | MySQL user |
| `PROD_DB_PASSWORD` | Yes | — | MySQL password |
| `PROD_DB_DATABASE` | Yes | — | Default database for EXPLAIN |
| `GCP_PROJECT_ID` | Yes | — | GCP project ID |
| `GCP_REGION` | No | asia-south1 | Cloud SQL region |
| `GCP_CLOUD_SQL_INSTANCE` | Yes | — | Cloud SQL instance ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes | — | Path to GCP SA JSON (inside container) |
| `SEEQL_API_PORT` | No | 8080 | API server port |
| `SEEQL_DB_MAX_SIZE_MB` | No | 5000 | Max SQLite DB size (MB) |
| `SEEQL_LOG_MAX_SIZE_MB` | No | 500 | Max total log size (MB) |
| `SEEQL_RETENTION_DAYS` | No | 90 | Data retention (days) |
| `SEEQL_FAST_INTERVAL` | No | 30 | Fast loop interval (seconds) |
| `SEEQL_MEDIUM_INTERVAL` | No | 300 | Medium loop interval (seconds) |
| `SEEQL_SLOW_INTERVAL` | No | 1800 | Slow loop interval (seconds) |
| `SEEQL_AGENT_ENABLED` | No | true | Enable LLM analysis |
| `SEEQL_AGENT_MODEL` | No | gemini-2.0-flash | LLM model name |
| `ANTHROPIC_API_KEY` | No | — | For Claude (instead of Gemini) |
| `SEEQL_ALERTING_ENABLED` | No | false | Enable alert evaluation |
| `SLACK_WEBHOOK_URL` | No | — | Slack incoming webhook |
| `SEEQL_PROM_CACHE_TTL` | No | 10 | Prometheus metrics cache (seconds) |
| `SEEQL_LOG_LEVEL` | No | INFO | Log level (DEBUG, INFO, WARNING) |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check (MySQL + SQLite) |
| `GET` | `/metrics` | Prometheus metrics |
| `GET` | `/status` | Scheduler job status + next run times |
| `POST` | `/collect/fast` | Trigger fast loop manually |
| `POST` | `/collect/medium` | Trigger medium loop manually |
| `POST` | `/collect/slow` | Trigger slow loop manually |
| `POST` | `/collect/all` | Trigger all loops |
| `GET` | `/data/queries?limit=50` | Latest query digest snapshots |
| `GET` | `/data/locks` | Latest lock wait snapshots |
| `GET` | `/data/schema-changes` | DDL change history |
| `GET` | `/data/global-status` | Latest global status deltas |
| `GET` | `/api/v1/state-report` | Full state report (Markdown + JSON) |
| `POST` | `/api/v1/agent/analyze` | Trigger LLM analysis |
| `GET` | `/api/v1/agent/analyses` | List recent analyses |
| `GET` | `/api/v1/anomalies` | Current statistical anomalies |
| `GET` | `/api/v1/alerts` | Alert history |
| `GET` | `/api/v1/alerts/rules` | Configured alert rules |
| `POST` | `/api/v1/alerts/test` | Fire test alert to all channels |
| `GET` | `/` | Web dashboard |

---

## Alert Rules

7 built-in rules, all configurable:

| Rule | Default Trigger | Severity |
|------|----------------|----------|
| `lock_cascade` | 3+ lock waits, max wait >10s | critical |
| `threads_running_spike` | 4x above 24h average | warning |
| `query_regression` | Any query 5x slower than 7d baseline | warning |
| `ddl_change` | Any schema change detected | info |
| `high_cpu` | CPU >85% | warning |
| `deadlock_detected` | Deadlock in InnoDB status | critical |
| `anomaly_detection` | Any metric >3 standard deviations from baseline | warning |

---

## What It Collects (19 Collectors)

### Fast Loop (30s) — 4 collectors
Processlist, lock waits, active transactions, metadata locks

### Medium Loop (5min) — 11 collectors
Query digests, wait events, table IO, InnoDB metrics, buffer pool, global status, GCP metrics, slow query logs, InnoDB status, execution stages, EXPLAIN capture

### Slow Loop (30min) — 4 collectors
Schema snapshots + DDL detection, unused indexes, redundant indexes, global variables

### Retention Loop (daily)
Deletes data older than `SEEQL_RETENTION_DAYS`. Enforces `SEEQL_DB_MAX_SIZE_MB` by aggressively shortening retention if DB exceeds limit.

---

## Local Development

```bash
# Install with dev dependencies
pip install -e ".[dev,api]"

# Run tests
make test

# Single collection cycle
python main.py --once

# Start with API
python main.py --api
```

---

## License

Internal project.
