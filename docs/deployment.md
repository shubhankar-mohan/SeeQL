# Deployment

SeeQL runs as a single long-lived process. The "right" way depends on
your operational setup.

- [Docker (recommended)](#docker-recommended)
- [Docker Compose](#docker-compose)
- [GCP Cloud SQL](#gcp-cloud-sql)
- [Kubernetes](#kubernetes)
- [systemd](#systemd)
- [Multi-architecture notes](#multi-architecture-notes)

---

## Docker (recommended)

One command:

```bash
docker run -d --name seeql \
  -p 8080:8080 \
  -e PROD_DB_HOST=your-mysql-host \
  -e PROD_DB_USER=dba_agent \
  -e PROD_DB_PASSWORD=your_password \
  -e PROD_DB_DATABASE=your_database \
  -v seeql-data:/app/data \
  -v seeql-logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/shubhankar-mohan/seeql:latest
```

Image variants:

| Tag | Contents |
|-----|----------|
| `latest` | Generic — any MySQL 8.0+ |
| `latest-gcp` | Adds Cloud Monitoring + Cloud Logging + google-genai |
| `vX.Y.Z` / `vX.Y.Z-gcp` | Pinned version |
| `sha-<short>` | Specific commit |

Both variants are multi-arch (`linux/amd64` + `linux/arm64`).

## Docker Compose

Two compose files ship in-repo:

- [`docker-compose.yml`](../docker-compose.yml) — generic, any MySQL
- [`docker-compose.gcp.yml`](../docker-compose.gcp.yml) — GCP variant

```bash
# Generic
cp .env.example .env                  # edit PROD_DB_* values
docker compose up -d

# GCP
cp .env.example .env                  # also set GCP_PROJECT_ID etc.
docker compose -f docker-compose.gcp.yml up -d
```

Includes a `--profile dev` MySQL 8.0 for local testing:

```bash
docker compose --profile dev up
# Then export PROD_DB_HOST=mysql-dev and re-run seeql container
```

## GCP Cloud SQL

### 1. Cloud SQL flags (one-time, requires instance restart)

Via `gcloud`:

```bash
gcloud sql instances patch <instance-name> \
  --database-flags=performance_schema=on,slow_query_log=on,long_query_time=1,innodb_monitor_enable=all
```

Or via console: Instance → Edit → Flags and parameters.

### 2. Monitoring user

```sql
CREATE USER 'dba_agent'@'%' IDENTIFIED BY '<strong_password>';
GRANT SELECT, PROCESS ON *.* TO 'dba_agent'@'%';
FLUSH PRIVILEGES;
```

### 3. Service account

```bash
SA=seeql-monitoring@<project>.iam.gserviceaccount.com

gcloud iam service-accounts create seeql-monitoring \
  --display-name="SeeQL monitoring agent"

gcloud projects add-iam-policy-binding <project> \
  --member="serviceAccount:$SA" --role="roles/monitoring.viewer"
gcloud projects add-iam-policy-binding <project> \
  --member="serviceAccount:$SA" --role="roles/logging.viewer"
gcloud projects add-iam-policy-binding <project> \
  --member="serviceAccount:$SA" --role="roles/aiplatform.user"  # optional, for LLM

gcloud iam service-accounts keys create vertex-sa.json \
  --iam-account="$SA"
```

### 4. Run

```bash
cp .env.example .env
# fill in PROD_DB_*, GCP_PROJECT_ID, GCP_CLOUD_SQL_INSTANCE
docker compose -f docker-compose.gcp.yml up -d
```

Network: SeeQL needs IP reachability to Cloud SQL. Use private IP +
VPC peering, or run the [Cloud SQL Auth
Proxy](https://cloud.google.com/sql/docs/mysql/sql-proxy) alongside
SeeQL.

## Kubernetes

Minimal sketch — adapt to your cluster / secrets story.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: seeql
spec:
  replicas: 1                         # SeeQL writes SQLite; don't scale
  selector:
    matchLabels: { app: seeql }
  template:
    metadata:
      labels: { app: seeql }
    spec:
      containers:
      - name: seeql
        image: ghcr.io/shubhankar-mohan/seeql:latest
        ports:
        - containerPort: 8080
        envFrom:
        - secretRef: { name: seeql-secrets }     # PROD_DB_PASSWORD, ANTHROPIC_API_KEY
        - configMapRef: { name: seeql-config }   # PROD_DB_HOST, etc.
        volumeMounts:
        - name: data
          mountPath: /app/data
        readinessProbe:
          httpGet: { path: /health, port: 8080 }
          periodSeconds: 10
        livenessProbe:
          httpGet: { path: /health, port: 8080 }
          periodSeconds: 30
          failureThreshold: 5
        resources:
          requests: { cpu: 100m, memory: 256Mi }
          limits:   { cpu: 500m, memory: 1Gi }
      volumes:
      - name: data
        persistentVolumeClaim: { claimName: seeql-data }
---
apiVersion: v1
kind: Service
metadata:
  name: seeql
spec:
  selector: { app: seeql }
  ports:
  - port: 8080
    targetPort: 8080
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: seeql-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
```

**Do not run more than 1 replica.** SeeQL writes SQLite — concurrent
writers will corrupt the DB.

## systemd

Simple venv install:

```ini
# /etc/systemd/system/seeql.service
[Unit]
Description=SeeQL — LLM-powered MySQL DBA agent
After=network-online.target

[Service]
Type=simple
User=seeql
WorkingDirectory=/opt/seeql
Environment="PATH=/opt/seeql/venv/bin"
EnvironmentFile=/etc/seeql/seeql.env
ExecStart=/opt/seeql/venv/bin/seeql serve
Restart=on-failure
RestartSec=10s

[Install]
WantedBy=multi-user.target
```

## Multi-architecture notes

- **Apple Silicon (M1/M2/M3)** — pull `ghcr.io/shubhankar-mohan/seeql:latest`,
  Docker picks the `linux/arm64` manifest automatically.
- **AWS Graviton** — same; arm64 image works out of the box.
- **Raspberry Pi 4/5** — arm64 image works. Lightweight target:
  ~200 MB RAM, 10 % CPU at default intervals.
- **Local multi-arch build** — if you're developing the image:

  ```bash
  docker buildx create --name seeql-builder --use --bootstrap
  docker buildx build \
    --platform=linux/amd64,linux/arm64 \
    --build-arg SEEQL_VERSION=$(git describe --tags --always) \
    --build-arg VCS_REF=$(git rev-parse --short HEAD) \
    --build-arg BUILD_DATE=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
    -t seeql:dev --load .
  ```

  `--load` works for single-platform builds only; for dual-arch local
  testing use `--output=type=oci,dest=./out.tar` or `--push` to a
  throwaway registry.

## Related

- [Troubleshooting](troubleshooting.md)
- [Configuration](config.md)
- [MySQL prerequisites](../README.md#mysql-prerequisites)
