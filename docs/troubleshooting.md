# Troubleshooting

> **TL;DR:** Run `seeql doctor` first. It's a 7-check preflight with
> remediation hints for every failure.

## "My logs say error[E00X]..."

Every `E00X` code has a dedicated page in [docs/errors/](errors/) with
verification commands and fixes. The error message itself dumps the
URL — follow it.

## Nothing collects

### Symptom: `/api/v1/queries/top` returns `[]` on a real database

**Check 1 — is `performance_schema` on?**

```sql
SHOW VARIABLES LIKE 'performance_schema';
```

If OFF → [E002](errors/E002.md).

**Check 2 — has a medium loop actually run yet?**

The medium loop runs every 5 min by default. Freshly started SeeQL has
no data until the first cycle completes. Trigger one manually:

```bash
curl -X POST http://localhost:8080/collect/medium
```

**Check 3 — is the user connecting from a reachable host?**

```sql
SELECT user, host FROM mysql.user WHERE user='dba_agent';
```

`%` is fine for most setups. If locked to a specific host, make sure
it matches SeeQL's source IP.

## Alerts never fire

### Symptom: `alerting.enabled: true` but no alerts in `/api/v1/alerts`

**Check 1 — is the rule enabled?**

```bash
curl http://localhost:8080/api/v1/alerts/rules | jq
```

Each rule has an `enabled` field.

**Check 2 — cooldown in effect?**

`cooldown_minutes` suppresses the same rule on the same server for
the duration. See [alerting.md](alerting.md#cooldowns).

**Check 3 — threshold too strict?**

Fire a manual test to verify your channels work:

```bash
curl -X POST http://localhost:8080/api/v1/alerts/test
```

Then lower the rule's threshold temporarily and watch.

## LLM analysis returns nothing

### Symptom: `/api/v1/agent/analyze` returns null / empty

**Check 1 — is the agent enabled?**

```bash
curl http://localhost:8080/api/v1/state-report | jq -r .summary
```

If `agent.enabled: false`, analyses return null immediately.

**Check 2 — is `skip_quiet` short-circuiting?**

By default, routine analyses skip when the state report has nothing
interesting. Set `agent.skip_quiet: false` temporarily, or force an
incident-mode analysis:

```bash
curl -X POST http://localhost:8080/api/v1/agent/analyze \
  -H 'content-type: application/json' \
  -d '{"trigger_type":"lock_cascade"}'
```

**Check 3 — are credentials reachable?**

See [E009 — LLM credentials invalid](errors/E009.md).

## Docker container won't start

### Symptom: Exits immediately with non-zero code

**Check 1 — required env vars set?**

```bash
docker run --rm ghcr.io/shubhankar-mohan/seeql:latest seeql check
```

This surfaces the specific `E00X` that's blocking startup.

**Check 2 — volume permissions (host bind mounts)?**

The image runs as uid `seeql` inside the container. Host bind mounts
need to be writable by that uid:

```bash
# On the host
chown -R 1000:1000 /opt/seeql/data       # 1000 = seeql uid
```

Or prefer a Docker-managed volume (auto-perms):

```yaml
volumes:
  - seeql-data:/app/data                 # instead of ./data:/app/data
```

## Dashboard is empty

### Symptom: `/` loads but all panels say "no data"

**Cold start:** no medium loop has run yet. Wait 5 min or trigger
manually.

**Volume not mounted:** `data/mysql_monitor.db` lives on the container's
overlay fs and gets wiped on restart. Mount `/app/data` to a volume.

**Time range mismatch:** most panels default to the last 24 h. If you
just started collecting, switch the range picker to "last 1 h".

## Connection keeps dropping

### Symptom: Repeated "MySQL server has gone away" warnings

Network hiccup between SeeQL and the target. SeeQL retries transient
errors (`2003`, `2006`, `2013`, `2055`) twice with exponential backoff
— persistent failures mean the network is broken, not SeeQL.

**Cloud SQL maintenance:** `wait_timeout` defaults vary. Set pool
settings in `settings.local.yaml`:

```yaml
production_db:
  pool_size: 5
  connect_timeout: 10
  pool_recycle_seconds: 3600             # Recycle before MySQL drops
```

## Disk fills up

### Symptom: `data/mysql_monitor.db` keeps growing past 5 GB

**Retention not running:** check `logs/dba_agent.log` for the daily
retention job. It should log "Retention cleanup completed" once per
day.

**Auto-shrink disabled:** verify `monitoring_db.max_size_mb` is set.
When the DB exceeds it, the retention job temporarily tightens
retention on the highest-volume tables until size is under the limit.

**Manual cleanup:**

```sql
sqlite3 data/mysql_monitor.db
DELETE FROM processlist_snapshots WHERE snapshot_time < '2026-04-01';
VACUUM;
```

## Still stuck?

- Run `seeql doctor` — most setup problems produce a clear fix.
- Check the [error catalog](errors/) for the code you saw.
- Enable DEBUG logging: `SEEQL_LOG_LEVEL=DEBUG seeql serve`.
- File an issue at
  <https://github.com/shubhankar-mohan/SeeQL/issues> with the output
  of `seeql doctor` and relevant log lines.
