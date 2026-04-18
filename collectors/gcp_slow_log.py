"""
GCP Cloud Logging Slow Query Log Collector — runs in the medium loop.

Fetches slow query logs from Cloud Logging (Cloud SQL writes them there
when slow_query_log=ON). Parses the log entries into structured data.
"""

from __future__ import annotations

import re
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from google.cloud import logging as cloud_logging

from collectors import get_monitoring_credentials
from collectors.base import BaseCollector
from config import get_config
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)

# Pattern to parse slow query log entries
# Example: "# Query_time: 1.234 Lock_time: 0.001 Rows_sent: 100 Rows_examined: 50000"
SLOW_LOG_PATTERN = re.compile(
    r"Query_time:\s*([\d.]+)\s+"
    r"Lock_time:\s*([\d.]+)\s+"
    r"Rows_sent:\s*(\d+)\s+"
    r"Rows_examined:\s*(\d+)"
)

USER_HOST_PATTERN = re.compile(
    r"User@Host:\s*(\S+)\[.*?\]\s*@\s*(\S*)"
)


class GCPSlowLogCollector(BaseCollector):
    """Fetches slow query logs from GCP Cloud Logging."""

    def __init__(self):
        super().__init__()
        self._client = None

    @property
    def name(self) -> str:
        return "gcp_slow_log"

    def _get_client(self):
        if self._client is None:
            gcp_config = get_config().get("gcp") or {}
            project_id = gcp_config.get("project_id")
            creds = get_monitoring_credentials()
            if not project_id or creds is None:
                return None
            self._client = cloud_logging.Client(project=project_id, credentials=creds)
        return self._client

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        gcp_config = ctx.gcp_config or {}
        project_id = gcp_config.get("project_id")
        instance_id = gcp_config.get("cloud_sql_instance_id")
        if not project_id or not instance_id:
            return {"slow_queries": []}

        client = self._get_client()
        if client is None:
            return {"slow_queries": []}

        # Look back 10 minutes to catch entries since last medium loop run
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

        log_filter = (
            f'resource.type="cloudsql_database" '
            f'resource.labels.database_id="{project_id}:{instance_id}" '
            f'log_id("cloudsql.googleapis.com/mysql-slow.log") '
            f'timestamp>="{cutoff_str}"'
        )

        rows = []
        try:
            entries = client.list_entries(
                filter_=log_filter,
                order_by=cloud_logging.DESCENDING,
                max_results=100,
            )

            for entry in entries:
                payload = entry.payload if isinstance(entry.payload, str) else str(entry.payload)
                parsed = self._parse_slow_entry(payload, now)
                if parsed:
                    parsed["server_id"] = ctx.server_id
                    rows.append(parsed)

        except Exception as e:
            logger.warning(f"Failed to fetch slow query logs: {e}")

        return {"slow_queries": rows}

    def store(self, data: dict) -> None:
        if data["slow_queries"]:
            writer.write_slow_queries(data["slow_queries"])

    def _parse_slow_entry(self, text: str, now: datetime) -> dict | None:
        """Parse a slow query log entry into structured data."""
        stats_match = SLOW_LOG_PATTERN.search(text)
        if not stats_match:
            return None

        user_match = USER_HOST_PATTERN.search(text)

        # Extract the actual SQL (everything after the stats line)
        lines = text.strip().split("\n")
        sql_lines = [
            line for line in lines
            if not line.startswith("#") and not line.startswith("SET ") and line.strip()
        ]
        sql = " ".join(sql_lines).strip()
        if len(sql) > 1000:
            sql = sql[:1000]

        return {
            "snapshot_time": now,
            "user": user_match.group(1) if user_match else None,
            "host": user_match.group(2) if user_match else None,
            "query_time_sec": float(stats_match.group(1)),
            "lock_time_sec": float(stats_match.group(2)),
            "rows_sent": int(stats_match.group(3)),
            "rows_examined": int(stats_match.group(4)),
            "sql_text": sql or None,
        }


_gcp_slow_log_collector = GCPSlowLogCollector()
