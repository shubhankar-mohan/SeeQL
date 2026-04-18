"""
GCP Cloud Monitoring Collector — runs in the medium loop.

Fetches infrastructure metrics from Cloud SQL that aren't available
via MySQL queries (no OS access on Cloud SQL):
    - CPU utilization
    - Memory utilization
    - Disk utilization, read/write ops
    - Network connections
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from google.cloud import monitoring_v3

from collectors import get_monitoring_credentials
from collectors.base import BaseCollector
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)

# Metrics we care about — metric_type suffix -> friendly name
METRICS = {
    "cloudsql.googleapis.com/database/cpu/utilization": "cpu_utilization",
    "cloudsql.googleapis.com/database/memory/utilization": "memory_utilization",
    "cloudsql.googleapis.com/database/disk/utilization": "disk_utilization",
    "cloudsql.googleapis.com/database/disk/read_ops_count": "disk_read_ops",
    "cloudsql.googleapis.com/database/disk/write_ops_count": "disk_write_ops",
    "cloudsql.googleapis.com/database/disk/bytes_used": "disk_bytes_used",
    "cloudsql.googleapis.com/database/network/connections": "network_connections",
    "cloudsql.googleapis.com/database/network/received_bytes_count": "network_received_bytes",
    "cloudsql.googleapis.com/database/network/sent_bytes_count": "network_sent_bytes",
    "cloudsql.googleapis.com/database/mysql/threads": "mysql_threads",
    "cloudsql.googleapis.com/database/mysql/questions": "mysql_questions",
    "cloudsql.googleapis.com/database/mysql/innodb/row_lock_waits_count": "innodb_row_lock_waits",
    "cloudsql.googleapis.com/database/mysql/innodb/deadlocks_count": "innodb_deadlocks",
    # Phase A4: Additional metrics
    "cloudsql.googleapis.com/database/replication/replica_lag": "replication_lag",
}


class GCPMetricCollector(BaseCollector):
    """Fetches Cloud SQL metrics from GCP Cloud Monitoring API."""

    def __init__(self):
        super().__init__()
        self._client = None

    @property
    def name(self) -> str:
        return "gcp_metrics"

    def _get_client(self):
        if self._client is None:
            creds = get_monitoring_credentials()
            if creds is None:
                return None
            self._client = monitoring_v3.MetricServiceClient(credentials=creds)
        return self._client

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        gcp_config = ctx.gcp_config or {}
        project_id = gcp_config.get("project_id")
        instance_id = gcp_config.get("cloud_sql_instance_id")
        if not project_id or not instance_id:
            return {"gcp_metrics": []}

        client = self._get_client()
        if client is None:
            return {"gcp_metrics": []}
        project_name = f"projects/{project_id}"

        # Query last 5 minutes of data
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=5)

        interval = monitoring_v3.TimeInterval(
            start_time=start_time,
            end_time=end_time,
        )

        rows = []
        for metric_type, metric_name in METRICS.items():
            try:
                results = client.list_time_series(
                    request={
                        "name": project_name,
                        "filter": (
                            f'metric.type = "{metric_type}" '
                            f'AND resource.labels.database_id = "{project_id}:{instance_id}"'
                        ),
                        "interval": interval,
                        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    }
                )

                for ts in results:
                    # value_type: 1=BOOL, 2=INT64, 3=DOUBLE, 4=STRING, 5=DISTRIBUTION
                    value_type = ts.value_type
                    for point in ts.points:
                        if value_type == 3:  # DOUBLE
                            value = point.value.double_value
                        elif value_type == 2:  # INT64
                            value = point.value.int64_value
                        else:
                            value = point.value.double_value or point.value.int64_value

                        rows.append({
                            "snapshot_time": now,
                            "server_id": ctx.server_id,
                            "metric_name": metric_name,
                            "metric_type": metric_type,
                            "value": value,
                            "unit": str(ts.unit),
                        })
                        break  # Only take the latest point

            except Exception as e:
                logger.warning(f"Failed to fetch GCP metric {metric_name}: {e}")
                continue

        return {"gcp_metrics": rows}

    def store(self, data: dict) -> None:
        writer.write_gcp_metrics(data["gcp_metrics"])


_gcp_metric_collector = GCPMetricCollector()
