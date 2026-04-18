"""
Medium Loop Collector — runs every 5 minutes.

Captures aggregated performance data:
    - Query digest statistics (the core data for the LLM agent)
    - Wait events (what MySQL is waiting on)
    - Table IO stats (which tables are hottest)
    - InnoDB internal metrics
    - InnoDB buffer pool stats
    - Global status counters (as deltas)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from collectors.base import BaseCollector
from collectors import queries
from config import get_excluded_schemas_sql, get_limits
from parsers.global_status import GlobalStatusDeltaCalculator
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)


class QueryDigestCollector(BaseCollector):
    """
    Captures top-N query digests from performance_schema.

    The single most important data source for the LLM agent. Each row
    is a normalized query pattern with accumulated stats.
    """

    @property
    def name(self) -> str:
        return "query_digests"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        limits = get_limits()
        excluded = get_excluded_schemas_sql()
        sql = queries.QUERY_DIGESTS.format(
            excluded_schemas=excluded,
            max_digest_len=limits.get("digest_text_max_len", 1024),
            limit=limits.get("top_queries", 50),
        )

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"digests": rows}

    def store(self, data: dict) -> None:
        writer.write_query_digests(data["digests"])


class WaitEventCollector(BaseCollector):
    """What MySQL is spending time waiting on (locks, IO, mutexes)."""

    @property
    def name(self) -> str:
        return "wait_events"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.WAIT_EVENTS)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"wait_events": rows}

    def store(self, data: dict) -> None:
        writer.write_wait_events(data["wait_events"])


class TableIOCollector(BaseCollector):
    """Read/write IO per table — which tables are hottest."""

    @property
    def name(self) -> str:
        return "table_io"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        excluded = get_excluded_schemas_sql()
        sql = queries.TABLE_IO.format(excluded_schemas=excluded)

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"table_io": rows}

    def store(self, data: dict) -> None:
        writer.write_table_io(data["table_io"])


class InnoDBMetricCollector(BaseCollector):
    """InnoDB internal metrics from information_schema.INNODB_METRICS."""

    @property
    def name(self) -> str:
        return "innodb_metrics"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.INNODB_METRICS)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"innodb_metrics": rows}

    def store(self, data: dict) -> None:
        writer.write_innodb_metrics(data["innodb_metrics"])


class BufferPoolCollector(BaseCollector):
    """InnoDB buffer pool statistics — cache effectiveness."""

    @property
    def name(self) -> str:
        return "buffer_pool"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.BUFFER_POOL_STATS)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"buffer_pool": rows}

    def store(self, data: dict) -> None:
        writer.write_buffer_pool(data["buffer_pool"])


class GlobalStatusCollector(BaseCollector):
    """
    Captures SHOW GLOBAL STATUS and computes deltas.

    Uses GlobalStatusDeltaCalculator to compute rate of change
    between snapshots for key counters like QPS, lock waits, etc.
    Per-server delta calculators ensure multi-server state isolation.
    """

    def __init__(self):
        super().__init__()
        self._delta_calcs: dict[str, GlobalStatusDeltaCalculator] = {}

    @property
    def name(self) -> str:
        return "global_status"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.GLOBAL_STATUS)
            raw_rows = cursor.fetchall()

        # Per-server delta calculator
        if ctx.server_id not in self._delta_calcs:
            self._delta_calcs[ctx.server_id] = GlobalStatusDeltaCalculator()
        delta_calc = self._delta_calcs[ctx.server_id]

        processed = delta_calc.process(raw_rows, now)

        for row in processed:
            row["server_id"] = ctx.server_id

        return {"global_status": processed}

    def store(self, data: dict) -> None:
        writer.write_global_status(data["global_status"])


# ---------------------------------------------------------------------------
# Composite runner
# ---------------------------------------------------------------------------

_global_status_collector = GlobalStatusCollector()

# GCP collectors — imported here to keep all medium-loop collectors together
from collectors.gcp_metrics import _gcp_metric_collector
from collectors.gcp_slow_log import _gcp_slow_log_collector
from collectors.innodb_status import _innodb_status_collector
from collectors.execution_stages import _execution_stage_collector
from collectors.explain_capture import _explain_capture_collector

MEDIUM_COLLECTORS = [
    QueryDigestCollector(),
    WaitEventCollector(),
    TableIOCollector(),
    InnoDBMetricCollector(),
    BufferPoolCollector(),
    _global_status_collector,
    _gcp_metric_collector,
    _gcp_slow_log_collector,
    _innodb_status_collector,
    _execution_stage_collector,
    _explain_capture_collector,
]


def run_medium_loop(ctx: ServerContext | None = None) -> dict[str, bool]:
    """Run all medium-loop collectors independently."""
    results = {}
    for collector in MEDIUM_COLLECTORS:
        results[collector.name] = collector.run(ctx)
    return results
