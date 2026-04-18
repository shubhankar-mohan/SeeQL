"""
Index Analysis Collectors — runs in the slow loop.

Detects unused and redundant indexes using the sys schema.
These are the low-hanging fruit for the LLM agent to recommend
index cleanup.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from collectors.base import BaseCollector
from collectors import queries
from config import get_excluded_schemas_sql
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)


class UnusedIndexCollector(BaseCollector):
    """Indexes that exist but have never been used since server start."""

    @property
    def name(self) -> str:
        return "unused_indexes"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        excluded = get_excluded_schemas_sql()
        sql = queries.UNUSED_INDEXES.format(excluded_schemas=excluded)

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"unused_indexes": rows}

    def store(self, data: dict) -> None:
        writer.write_unused_indexes(data["unused_indexes"])


class RedundantIndexCollector(BaseCollector):
    """Indexes that are fully covered by another index on the same table."""

    @property
    def name(self) -> str:
        return "redundant_indexes"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        excluded = get_excluded_schemas_sql()
        sql = queries.REDUNDANT_INDEXES.format(excluded_schemas=excluded)

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(sql)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id
            # Convert boolean to int for SQLite
            row["subpart_exists"] = 1 if row.get("subpart_exists") else 0

        return {"redundant_indexes": rows}

    def store(self, data: dict) -> None:
        writer.write_redundant_indexes(data["redundant_indexes"])


_unused_index_collector = UnusedIndexCollector()
_redundant_index_collector = RedundantIndexCollector()
