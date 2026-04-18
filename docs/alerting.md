# Alerting

SeeQL ships **6 deterministic rules** plus **1 statistical anomaly
detection rule** = 7 configured. All are individually toggleable,
tunable, and per-channel routable.

Evaluation runs at the end of every medium loop (default every 5 min).
Firings land in `alerts` and optionally fan out to Slack, a generic
webhook, or the log.

## Rule catalog

### `lock_cascade` — critical

**Triggers when:** `lock_wait_snapshots.lock_count >= min_count`
**AND** `max_wait_seconds >= min_wait_seconds`.

```yaml
lock_cascade:
  enabled: true
  severity: critical
  min_count: 3
  min_wait_seconds: 10
  cooldown_minutes: 5
  channels: [slack, log]
```

Tune `min_count` up if you normally run with many concurrent
short-lived locks; tune `min_wait_seconds` down to catch cascades
earlier (but risk noise).

### `threads_running_spike` — warning

**Triggers when:** current `Threads_running` exceeds `multiplier` ×
the 24-hour baseline average.

```yaml
threads_running_spike:
  enabled: true
  severity: warning
  multiplier: 4                       # 4× baseline
  cooldown_minutes: 15
  channels: [slack, log]
```

### `query_regression` — warning

**Triggers when:** any query digest's recent avg_time is `threshold` ×
slower than its 7-day baseline avg_time, with minimum exec count to
avoid noise on rare queries.

```yaml
query_regression:
  enabled: true
  severity: warning
  threshold: 5.0                      # 5× slower
  cooldown_minutes: 30
  channels: [slack, log]
```

### `ddl_change` — info

**Triggers when:** schema snapshot detects a change in any table —
column added/removed, index added/removed, type changed. Stored in
`ddl_changes` with before/after DDL.

```yaml
ddl_change:
  enabled: true
  severity: info
  cooldown_minutes: 0                 # Every DDL is notable
  channels: [slack, log]
```

### `high_cpu` — warning

**Triggers when:** Cloud SQL CPU utilization exceeds `threshold` (0–1).
Requires the `[gcp]` extra + `gcp.project_id` configured; otherwise
this rule is a no-op.

```yaml
high_cpu:
  enabled: true
  severity: warning
  threshold: 0.85                     # 85 %
  cooldown_minutes: 15
  channels: [slack, log]
```

### `deadlock_detected` — critical

**Triggers when:** the `SHOW ENGINE INNODB STATUS` parser finds a new
"LATEST DETECTED DEADLOCK" section since the last cycle.

```yaml
deadlock_detected:
  enabled: true
  severity: critical
  cooldown_minutes: 5
  channels: [slack, log]
```

### `anomaly_detection` — warning

**Triggers when:** any of 8 tracked metrics has a z-score above
`z_threshold` vs its same-hour-same-weekday baseline over 28 days
(with fallbacks to 24-hour and all-data baselines for cold starts).

```yaml
anomaly_detection:
  enabled: true
  severity: warning
  z_threshold: 3.0                    # Standard deviations
  cooldown_minutes: 30
  channels: [slack, log]
```

Tracked metrics (from `alerting/anomaly.py`):

- `threads_running`
- `threads_connected`
- `queries_per_second`
- `slow_queries_per_second`
- `lock_frequency`
- `cpu_utilization`
- `memory_utilization`
- `buffer_pool_hit_ratio`

Anomaly events persist to `anomaly_events` and feed the incident
windowing logic — see [incidents.md](incidents.md).

## Channels

### Slack

```yaml
slack:
  enabled: true
  webhook_url: "${SLACK_WEBHOOK_URL}"
```

Messages use block-kit formatting with severity-coloured sidebars. New
incidents get their own Slack message; incident extensions do not.

### Generic webhook

```yaml
webhook:
  enabled: true
  url: "https://your-endpoint.com/alerts"
  headers:
    Authorization: "Bearer ${WEBHOOK_TOKEN}"
```

POST body:

```json
{
  "rule": "lock_cascade",
  "severity": "critical",
  "server_id": "default",
  "triggered_at": "2026-04-10T03:12:00",
  "summary": "3 lock waits, max wait 14s",
  "details": { ... }
}
```

### Log

```yaml
log:
  enabled: true                       # Always-on fallback
```

Writes to `logs/dba_agent.log` at WARNING level. Useful as a
belt-and-suspenders when Slack / webhook channels fail silently.

## Cooldowns

Each rule's `cooldown_minutes` suppresses the same rule firing for the
same server within the window. Multi-server deployments track
cooldowns per `(rule, server_id)`, so one server's alert storm doesn't
silence another server.

## Testing

```bash
curl -X POST http://localhost:8080/api/v1/alerts/test
```

Fires a fake "test" alert to every enabled channel. Use this to verify
Slack webhooks, Bearer tokens, etc. without waiting for a real event.

## Disabling

Any rule can be `enabled: false`. The entire alerting layer can be
turned off with:

```yaml
alerting:
  enabled: false
```

Collectors, anomaly detection, and incident windowing continue running
— only the rule-firing / channel-fanout layer is skipped.
