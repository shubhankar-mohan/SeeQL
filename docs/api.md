# HTTP API reference

SeeQL serves a FastAPI app at `http://<host>:8080/` — dashboard at `/`,
Prometheus metrics at `/metrics`, programmatic endpoints under
`/api/v1/*`. All responses are JSON unless stated otherwise.

## Health & ops

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness + MySQL and SQLite connectivity probe |
| `GET` | `/metrics` | Prometheus scrape endpoint |
| `GET` | `/status` | Scheduler job status + next run times |

### `GET /health`

```json
{
  "status": "healthy",          // "healthy" | "degraded" | "unhealthy"
  "mysql": true,
  "monitoring_db": true
}
```

`degraded` = one of MySQL / SQLite is unreachable but the process is
still running. `unhealthy` = the process itself is broken (rare — the
healthcheck usually can't respond in that state).

## Collection triggers

Mostly for testing, debugging, or external schedulers.

| Method | Path | Runs |
|--------|------|------|
| `POST` | `/collect/fast` | Fast loop (processlist + locks + transactions) |
| `POST` | `/collect/medium` | Medium loop (digests, wait events, InnoDB, etc.) |
| `POST` | `/collect/slow` | Slow loop (schema, indexes, variables) |
| `POST` | `/collect/all` | All three in sequence |

Response:

```json
{
  "loop": "medium",
  "server_id": "default",
  "results": {
    "query_digests": true,
    "wait_events": true,
    "gcp_metrics": false,          // skipped (no GCP creds) or failed
    ...
  }
}
```

## Query / metric reads

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/data/queries?limit=50` | Latest query digest snapshots |
| `GET` | `/data/locks` | Latest lock wait snapshots |
| `GET` | `/data/schema-changes` | DDL change history |
| `GET` | `/data/global-status` | Latest global status deltas |
| `GET` | `/api/v1/queries/top` | Top queries by total time |
| `GET` | `/api/v1/queries/regressions` | Queries with recent regression factor > threshold |
| `GET` | `/api/v1/locks/history` | Lock waits over a time window |
| `GET` | `/api/v1/schema/table-sizes` | Table sizes + row counts |
| `GET` | `/api/v1/schemas` | Distinct schemas + tables observed in digests / table-IO |
| `GET` | `/api/v1/metrics/threads` | `Threads_running` / `Threads_connected` time series |

Most query endpoints accept:

- `range=1h|24h|7d|30d` — preset window
- `from=<ISO>&to=<ISO>` — custom window
- `server=<id>` — multi-server deployments
- `schema=<name>&table=<name>` — filter (approximate for lock queries)
- `limit=<n>` — row cap

## Anomalies, incidents, alerts

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/anomalies` | Current statistical anomalies (z-score > threshold) |
| `GET` | `/api/v1/incidents/recent` | Detected incident windows (gap-clustered) |
| `GET` | `/api/v1/alerts` | Alert history (rule evaluations that fired) |
| `GET` | `/api/v1/alerts/rules` | Configured rules (with current thresholds) |
| `POST` | `/api/v1/alerts/test` | Fire a test alert to every enabled channel |

### `GET /api/v1/incidents/recent`

```json
[
  {
    "id": 42,
    "start_time": "2026-04-10T03:12:00+00:00",
    "end_time": "2026-04-10T03:47:00+00:00",
    "duration_minutes": 35,
    "severity": "critical",
    "involved_metrics": ["threads_running", "lock_frequency"],
    "event_count": 8,
    "status": "detected"              // detected | analyzed | resolved
  }
]
```

## Agent

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/state-report` | Full state report the LLM would receive (Markdown + JSON) |
| `POST` | `/api/v1/agent/analyze` | Trigger an LLM analysis now |
| `GET` | `/api/v1/agent/analyses` | Recent analysis history |

### `POST /api/v1/agent/analyze`

Runs a synchronous analysis (up to `agent.max_tool_rounds` tool calls
— can take 30–120 s). Accepts an optional body:

```json
{
  "trigger_type": "lock_cascade",   // triggers the incident prompt template
  "server_id": "db-prod-west"
}
```

Returns the parsed result:

```json
{
  "id": 108,
  "started_at": "2026-04-10T03:15:12",
  "severity": "critical",
  "findings": [ ... ],
  "recommendations": [ ... ],
  "raw_response": "### Severity: critical\n..."
}
```

## Dashboard

`GET /` renders the overview page. Dashboard pages live under `/` as
server-rendered HTMX templates:

- `/` — overview
- `/queries` — top queries + regressions
- `/locks` — current + historical locks
- `/schema` — DDL changes + table sizes + indexes
- `/server` — system metrics
- `/incidents` — incident window list

HTMX partials live under `/partials/*` and are not intended for direct
API consumption.

## Prometheus metrics

`GET /metrics` exposes ~20 gauges + counters:

| Metric | Type | Description |
|--------|------|-------------|
| `mysql_threads_running` | Gauge | Active threads |
| `mysql_threads_connected` | Gauge | Total connections |
| `mysql_queries_per_second` | Gauge | QPS from SHOW GLOBAL STATUS delta |
| `mysql_slow_queries_per_second` | Gauge | Slow queries per second |
| `mysql_lock_waits_current` | Gauge | Active InnoDB lock waits |
| `mysql_lock_wait_max_seconds` | Gauge | Longest lock wait duration |
| `mysql_buffer_pool_hit_ratio` | Gauge | Cumulative hit ratio, target > 0.99 |
| `mysql_buffer_pool_dirty_pages` | Gauge | Dirty pages |
| `mysql_buffer_pool_free_buffers` | Gauge | Free buffers |
| `mysql_cpu_utilization` | Gauge | Cloud SQL CPU (0-1) — requires `[gcp]` |
| `mysql_memory_utilization` | Gauge | Cloud SQL memory (0-1) — requires `[gcp]` |
| `mysql_disk_utilization` | Gauge | Cloud SQL disk (0-1) — requires `[gcp]` |
| `mysql_disk_read_ops` | Gauge | Disk read ops — requires `[gcp]` |
| `mysql_disk_write_ops` | Gauge | Disk write ops — requires `[gcp]` |
| `mysql_network_connections` | Gauge | Connections — requires `[gcp]` |
| `mysql_unused_indexes_count` | Gauge | Detected unused indexes |
| `mysql_redundant_indexes_count` | Gauge | Detected redundant indexes |
| `mysql_innodb_rows_read_per_sec` | Gauge | InnoDB rows read/s |
| `mysql_innodb_row_lock_waits_per_sec` | Gauge | InnoDB row lock waits/s |
| `seeql_alerts_fired_total` | Counter | Alerts fired, labelled by rule |

`SEEQL_PROM_CACHE_TTL` (default 10 s) controls the re-read cadence
from SQLite.

## OpenAPI / generated reference

FastAPI serves the OpenAPI schema at `/openapi.json` and Swagger UI at
`/docs` (disable in production by setting `SEEQL_ENV=production`
— the Swagger path is the only difference). Use the generated schema
as the authoritative reference when writing clients:

```bash
curl -s http://localhost:8080/openapi.json | jq '.paths | keys'
```
