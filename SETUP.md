# SeeQL Setup Guide — Permissions & Access

## 1. MySQL User Setup

SeeQL needs a dedicated read-only MySQL user. It never writes to your production database.

### Create the user

```sql
CREATE USER 'dba_agent'@'%' IDENTIFIED BY '<strong_password>';
```

### Grant permissions

```sql
-- Read access to all databases (needed to see schemas, tables, query digests)
GRANT SELECT ON *.* TO 'dba_agent'@'%';

-- PROCESS privilege (needed for processlist, InnoDB status, active transactions)
GRANT PROCESS ON *.* TO 'dba_agent'@'%';

-- Remove per-hour query limit (default may be 100, agent needs ~80 queries per cycle)
ALTER USER 'dba_agent'@'%' WITH MAX_QUERIES_PER_HOUR 0;

FLUSH PRIVILEGES;
```

| Permission | Why | What breaks without it |
|-----------|-----|----------------------|
| `SELECT ON *.*` | Read table schemas, column definitions, index stats across all databases | Schema snapshots only see system schemas. Can't fingerprint tables or detect DDL changes. |
| `PROCESS` | Access `SHOW ENGINE INNODB STATUS`, see all threads in processlist, view active transactions | Processlist only shows own queries. No deadlock detection. No InnoDB internals. |
| `MAX_QUERIES_PER_HOUR 0` | Agent runs ~80 queries per collection cycle (fast+medium+slow loops). At 30s fast loop, that's thousands/hour. | `ERROR 1226: User has exceeded the 'max_questions' resource` — agent stops collecting. |

### What is NOT needed

| Permission | Why not |
|-----------|---------|
| `INSERT, UPDATE, DELETE` | Agent never writes to production |
| `SUPER` | Not available on Cloud SQL anyway |
| `REPLICATION CLIENT` | We use GCP Cloud Monitoring for replication lag instead |
| `CREATE, ALTER, DROP` | Agent is read-only |

### Verify setup

```sql
-- Check grants
SHOW GRANTS FOR 'dba_agent'@'%';
-- Expected: GRANT SELECT, PROCESS ON *.* TO `dba_agent`@`%`

-- Check query limit removed
SELECT User, Host, max_questions FROM mysql.user WHERE User = 'dba_agent';
-- Expected: max_questions = 0

-- Verify database visibility (run as dba_agent)
SHOW DATABASES;
-- Should list all user databases, not just information_schema/performance_schema
```

---

## 2. Cloud SQL Flags

Set via: **GCP Console > Cloud SQL > Instance > Edit > Flags**

| Flag | Value | Requires Restart | Why |
|------|-------|-----------------|-----|
| `performance_schema` | `on` | **YES** | All perf_schema queries depend on this. Without it, processlist/locks/digests/waits all fail. |
| `slow_query_log` | `on` | no | Enables slow query logging to Cloud Logging. SeeQL reads these via Cloud Logging API. |
| `long_query_time` | `1` | no | Queries taking >1 second get logged. Lower = more data. Recommended: 1 for production, 0.5 for investigation. |
| `innodb_monitor_enable` | `all` | no | Enables all 300+ InnoDB internal counters in `information_schema.INNODB_METRICS`. |

### Enable stage instrumentation (runtime, no restart)

Run as root or admin. This enables "where time is spent" breakdown (parsing, optimizing, sorting, sending data):

```sql
UPDATE performance_schema.setup_instruments
SET ENABLED = 'YES', TIMED = 'YES'
WHERE NAME LIKE 'stage/%';

UPDATE performance_schema.setup_consumers
SET ENABLED = 'YES'
WHERE NAME LIKE 'events_stages%';
```

**Note:** This does NOT persist across MySQL restarts. To make it permanent, add the `performance-schema-instrument` flag in Cloud SQL:
- Flag: `performance-schema-instrument`
- Value: `stage/%=ON`

### Verify perf_schema

```sql
-- Check performance_schema is on
SHOW VARIABLES LIKE 'performance_schema';

-- Check metadata lock instrument (critical for DDL blocking detection)
SELECT ENABLED FROM performance_schema.setup_instruments
WHERE NAME = 'wait/lock/metadata/sql/mdl';

-- Check stages are enabled
SELECT COUNT(*) FROM performance_schema.setup_instruments
WHERE NAME LIKE 'stage/%' AND ENABLED = 'YES';
-- Expected: 132+

-- Check InnoDB metrics
SELECT COUNT(*) FROM information_schema.INNODB_METRICS WHERE STATUS = 'enabled';
-- Expected: 300+
```

---

## 3. GCP API Access

SeeQL uses two GCP APIs to get infrastructure metrics and slow query logs.

### Required APIs

Enable via: **GCP Console > APIs & Services > Enable APIs**

| API | Why |
|-----|-----|
| Cloud Monitoring API (`monitoring.googleapis.com`) | CPU, memory, disk, network metrics for the Cloud SQL instance |
| Cloud Logging API (`logging.googleapis.com`) | Slow query log entries |

### Authentication

SeeQL uses Application Default Credentials. Options:

**Option A: Service Account Key (explicit)**
1. GCP Console > IAM & Admin > Service Accounts
2. Create a service account (e.g., `seeql-agent@your-project.iam.gserviceaccount.com`)
3. Grant roles:
   - `roles/monitoring.viewer` (read Cloud Monitoring metrics)
   - `roles/logging.viewer` (read Cloud Logging entries)
4. Create a JSON key and download it
5. Set in `.env`: `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json`

**Option B: GCE VM Default Credentials (recommended for production)**
If SeeQL runs on a GCE VM in the same project:
1. Set the VM's service account to have `monitoring.viewer` + `logging.viewer` roles
2. No key file needed — credentials are automatic

**Option C: `gcloud auth application-default login` (for local dev)**
```bash
gcloud auth application-default login
```

### Required IAM Roles

| Role | Why |
|------|-----|
| `roles/monitoring.viewer` | Read-only access to Cloud Monitoring time series data (CPU, memory, disk, network) |
| `roles/logging.viewer` | Read-only access to Cloud Logging (slow query logs) |

### Verify GCP access

```bash
# Check Cloud Monitoring
gcloud monitoring metrics-descriptors list \
  --filter='metric.type = starts_with("cloudsql.googleapis.com/database/cpu")' \
  --project=your-project-id

# Check Cloud Logging
gcloud logging read \
  'resource.type="cloudsql_database" log_id("cloudsql.googleapis.com/mysql-slow.log")' \
  --project=your-project-id --limit=1
```

---

## 4. Local Environment

### `.env` file (create from `.env.example`)

```env
PROD_DB_PASSWORD=your_mysql_password
SEEQL_ENV=production          # or "dev" for local MySQL
SEEQL_API_PORT=8080
GOOGLE_APPLICATION_CREDENTIALS=/path/to/sa-key.json  # only if using Option A
```

### `settings.local.yaml` (create in project root, gitignored)

```yaml
production_db:
  host: "your.cloud.sql.ip"
  database: "your_default_db"

gcp:
  project_id: "your-gcp-project"
  cloud_sql_instance_id: "your-instance-id"
  region: "your-region"
```

### Quick start

```bash
python -m venv venv
source venv/bin/activate
pip install -e ".[dev,api]"
python main.py --init-db
python main.py --once        # test single collection
python main.py --api          # run with API + scheduler
```
