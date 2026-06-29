# Configuration reference

SeeQL is configured by a **single YAML file you mount** (Prometheus-style).
Built-in defaults are baked into the image; your file overrides only what you set.

Load order (lowest → highest precedence):

1. [`config/settings.yaml`](../config/settings.yaml) — built-in operational
   defaults (intervals, retention, anomaly thresholds, alert rules), shipped in
   the image; you rarely touch it.
2. Your **config file** — resolved from `--config <path>` → `SEEQL_CONFIG` env →
   `/etc/seeql/seeql.yml` → (legacy) `settings.local.yaml`. Deep-merged over the
   defaults. Copy [`seeql.example.yml`](../seeql.example.yml) to start.
3. `${VAR}` substitution — secrets only, pulled from the environment.

Connections and the server list live **only** in the config file — there are no
`PROD_DB_*` / `SEEQL_SERVER_*` env overrides. Pass secrets into the file as
`${VAR}` (e.g. `password: ${PROD_DB_PASSWORD}`).

A few **operational** knobs stay as env vars (like Prometheus's `--storage.*` /
`--log.level` flags): `SEEQL_CONFIG`, `SEEQL_MON_DB_PATH`, `SEEQL_DB_MAX_SIZE_MB`,
`SEEQL_LOG_MAX_SIZE_MB`, `SEEQL_RETENTION_DAYS`, `SEEQL_LOG_LEVEL`, `SEEQL_ENV`.

## Production database (required)

```yaml
production_db:
  host: "10.0.0.1"                    # Cloud SQL private IP, RDS endpoint, or localhost
  port: 3306
  user: "dba_agent"
  password: "${PROD_DB_PASSWORD}"     # Resolved from env
  database: "your_database"           # Default schema for EXPLAIN
  pool_size: 5                        # Collectors + agent live-tools need headroom
  connect_timeout: 10
```

This `production_db:` block configures a single host inside your config file.
Secrets come from the environment via `${VAR}` — there are no `PROD_DB_*`
overrides. For more than one host, use a `servers:` block instead (below).

> One MySQL **instance** with many databases needs only the single block above —
> all schemas in the instance are monitored automatically (use `excluded_schemas`
> to skip any). `database` is only the default schema for `EXPLAIN`.

## Multiple hosts

One entry per MySQL **instance** (host:port). All databases/schemas inside an
instance are monitored automatically — you do not need an entry per database.
Add a second entry only for a second host.

```yaml
servers:
  prod-primary:
    display_name: "Prod Primary"
    environment: production           # production | staging | dev (UI grouping)
    role: primary                     # primary | replica
    host: 10.0.0.1
    user: dba_agent
    password: ${PROD_DB_PASSWORD}     # from the environment
    database: app_db                  # optional: default schema for EXPLAIN
    # Per-server GCP (Cloud SQL metrics + slow log); omit for non-GCP MySQL:
    # gcp: { project_id: my-proj, cloud_sql_instance_id: prod-primary, region: asia-south1 }
  analytics-replica:
    role: replica
    cluster_id: prod
    primary_server_id: prod-primary
    host: 10.0.0.2
    user: dba_agent
    password: ${ANALYTICS_DB_PASSWORD}
```

Per-server fields: `display_name, environment, role, cluster_id,
primary_server_id, host, port, user, password, database, pool_size,
connect_timeout, gcp`. The scheduler runs every active server and the dashboard
gets a per-server dropdown. (A single host may use a top-level `production_db:`
block instead of `servers:`.)

## Monitoring database

SQLite — local disk, WAL mode.

```yaml
monitoring_db:
  path: "data/mysql_monitor.db"
  wal_mode: true
  busy_timeout_ms: 5000
  max_size_mb: 5000                    # 5 GB — see retention auto-shrink below
```

Env: `SEEQL_DB_MAX_SIZE_MB`.

## Collection intervals

```yaml
intervals:
  fast_loop: 30           # seconds — processlist, locks, transactions
  medium_loop: 300        # query digests, wait events, global status, InnoDB
  slow_loop: 1800         # schema fingerprints, index analysis, variables
  retention_loop: 86400   # daily cleanup
```

Env: `SEEQL_FAST_INTERVAL`, `SEEQL_MEDIUM_INTERVAL`, `SEEQL_SLOW_INTERVAL`.

## Collection limits

```yaml
limits:
  top_queries: 50                     # How many query digests to capture each cycle
  explain_top_n: 10                   # How many to auto-EXPLAIN
  processlist_query_max_len: 500      # Truncate long queries in processlist
  digest_text_max_len: 1024
  max_batch_size: 500                 # Rows per SQLite batch insert
```

## Retention

Global default + per-table overrides. The `max_size_mb` auto-shrink
temporarily shortens retention if the DB exceeds the configured size.

```yaml
retention:
  days: 90                            # global default
  overrides:
    ddl_changes: 365                  # schema history is high-value, keep longer
    incident_windows: 365
    anomaly_events: 90
    lock_wait_snapshots: 30
    processlist_snapshots: 7          # very high volume, keep shorter
```

Env: `SEEQL_RETENTION_DAYS`.

## Schemas to ignore

```yaml
excluded_schemas:
  - mysql
  - performance_schema
  - sys
  - information_schema
```

System schemas are always excluded from query digest / table-IO /
schema-snapshot collectors.

## GCP (optional — `[gcp]` extra)

```yaml
gcp:
  project_id: "your-gcp-project-id"
  cloud_sql_instance_id: "your-instance-id"
  region: "us-central1"
  vertex_region: "us-east5"                                 # Claude on Vertex lives in us-east5
  monitoring_credentials_file: "${MONITORING_APPLICATION_CREDENTIALS}"
```

Env:

| Variable | YAML key |
|----------|----------|
| `GCP_PROJECT_ID` | `gcp.project_id` |
| `GCP_REGION` | `gcp.region` |
| `GCP_CLOUD_SQL_INSTANCE` | `gcp.cloud_sql_instance_id` |
| `GOOGLE_APPLICATION_CREDENTIALS` | (SDK-native) |
| `MONITORING_APPLICATION_CREDENTIALS` | dedicated SA for Monitoring/Logging only |

When `gcp.project_id` is empty or left as the default placeholder
(`your-gcp-project-id`), GCP collectors register as no-ops.

## LLM agent (optional)

```yaml
agent:
  enabled: false                      # Set true after configuring a backend
  model: "claude-sonnet-4-6"          # or "gemini-2.0-flash", "claude-opus-4-6", etc.
  max_tokens: 8192
  max_tool_rounds: 10
  schedule_seconds: 900               # How often the routine analysis runs
  skip_quiet: true                    # Skip cycles with nothing interesting
  anthropic_api_key: "${ANTHROPIC_API_KEY}"
  state_builder:
    trend_threshold: 0.2              # 20% change = "up" / "down"
    regression_threshold: 3.0         # 3× slowdown = regression
    long_transaction_sec: 30
    lookback_minutes: 5
```

Env: `SEEQL_AGENT_ENABLED`, `SEEQL_AGENT_MODEL`, `ANTHROPIC_API_KEY`.

Backend selection is model-name-driven — see [agent.md](agent.md) for
the matrix.

## Alerting

```yaml
alerting:
  enabled: false
  default_cooldown_minutes: 15
  incident_gap_minutes: 15            # Anomaly-to-incident window clustering
  incident_max_duration_minutes: 120
  channels:
    slack:
      enabled: false
      webhook_url: "${SLACK_WEBHOOK_URL}"
    webhook:
      enabled: false
      url: "https://your-endpoint.com/alerts"
      headers:
        Authorization: "Bearer ${WEBHOOK_TOKEN}"
    log:
      enabled: true                   # Always-on fallback
  rules:
    lock_cascade:
      enabled: true
      severity: critical
      min_count: 3
      min_wait_seconds: 10
      cooldown_minutes: 5
      channels: [slack, log]
    # ... 6 more rules, see alerting.md
```

Env: `SEEQL_ALERTING_ENABLED`, `SLACK_WEBHOOK_URL`.

Full rule-by-rule tuning in [alerting.md](alerting.md).

## Prometheus

```yaml
prometheus:
  enabled: true
  cache_ttl_seconds: 10               # Match your scrape interval
```

Env: `SEEQL_PROM_CACHE_TTL`.

## Logging

```yaml
logging:
  level: "INFO"                       # DEBUG / INFO / WARNING / ERROR
  file: "logs/dba_agent.log"
  max_bytes: 10485760                 # 10 MB per log file
  backup_count: 5
  max_total_mb: 500                   # Total across rotating files
```

Env: `SEEQL_LOG_LEVEL`, `SEEQL_LOG_MAX_SIZE_MB`.

## Precedence examples

```bash
# 1. YAML default in config/settings.yaml: host: "10.0.0.1"
# 2. Local override in settings.local.yaml: host: "10.0.5.5"
# 3. Env var at runtime: PROD_DB_HOST=10.0.9.9

docker run -e PROD_DB_HOST=10.0.9.9 ...   # → SeeQL uses 10.0.9.9
```

Env wins. Local YAML wins over the stock config. The stock config is
the last-resort fallback.
