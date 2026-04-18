# Configuration reference

SeeQL reads config from, in precedence order (lowest → highest):

1. [`config/settings.yaml`](../config/settings.yaml) — stock defaults, shipped
2. `settings.local.yaml` — your overrides (gitignored)
3. Environment variables — override any YAML value at runtime

Env vars win. Secrets should go through env vars, not YAML.

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

Env overrides:

| Variable | YAML key |
|----------|----------|
| `PROD_DB_HOST` | `production_db.host` |
| `PROD_DB_PORT` | `production_db.port` |
| `PROD_DB_USER` | `production_db.user` |
| `PROD_DB_PASSWORD` | `production_db.password` |
| `PROD_DB_DATABASE` | `production_db.database` |

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
