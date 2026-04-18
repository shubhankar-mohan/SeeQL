"""
Fast Loop Collector — runs every 30 seconds.

Captures the "right now" state of the database:
    - Active queries (non-sleeping processlist entries)
    - InnoDB lock waits (who is blocking whom)
    - Active transactions (especially long-running ones)
    - Metadata locks (DDL blocking detection)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from collectors.base import BaseCollector
from collectors import queries
from config import get_excluded_schemas_sql, get_limits
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)


class ProcesslistCollector(BaseCollector):
    """Captures active (non-sleeping) entries from the processlist."""

    @property
    def name(self) -> str:
        return "processlist"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        limits = get_limits()
        sql = queries.ACTIVE_PROCESSLIST.format(
            max_query_len=limits.get("processlist_query_max_len", 500),
        )

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"processlist": rows}

    def store(self, data: dict) -> None:
        writer.write_processlist(data["processlist"])


class LockWaitCollector(BaseCollector):
    """
    Captures current InnoDB lock waits via performance_schema.data_lock_waits.

    Early warning system for cascading lock failures. Even a single row
    here means one transaction is actively blocking another.
    """

    @property
    def name(self) -> str:
        return "lock_waits"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.LOCK_WAITS)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"lock_waits": rows}

    def store(self, data: dict) -> None:
        writer.write_lock_waits(data["lock_waits"])


class TransactionCollector(BaseCollector):
    """
    Captures all active InnoDB transactions.

    Long-running transactions are dangerous even when idle: they hold locks,
    prevent purge, and cause undo log bloat.
    """

    @property
    def name(self) -> str:
        return "transactions"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.ACTIVE_TRANSACTIONS)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"transactions": rows}

    def store(self, data: dict) -> None:
        writer.write_transactions(data["transactions"])


class MetadataLockCollector(BaseCollector):
    """
    Captures metadata locks on user tables.

    Metadata locks are the #1 cause of "ALTER TABLE hangs and then everything
    hangs." When an ALTER TABLE is waiting for a metadata lock, ALL subsequent
    queries on that table also wait — cascading failure.
    """

    @property
    def name(self) -> str:
        return "metadata_locks"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        excluded = get_excluded_schemas_sql()
        sql = queries.METADATA_LOCKS.format(excluded_schemas=excluded)

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"metadata_locks": rows}

    def store(self, data: dict) -> None:
        writer.write_metadata_locks(data["metadata_locks"])


# ---------------------------------------------------------------------------
# Composite runner
# ---------------------------------------------------------------------------

FAST_COLLECTORS = [
    ProcesslistCollector(),
    LockWaitCollector(),
    TransactionCollector(),
    MetadataLockCollector(),
]


def run_fast_loop(ctx: ServerContext | None = None) -> dict[str, bool]:
    """Run all fast-loop collectors independently."""
    results = {}
    for collector in FAST_COLLECTORS:
        results[collector.name] = collector.run(ctx)
    return results
