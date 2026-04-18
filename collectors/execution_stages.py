"""
Execution Stages Collector — runs in the medium loop.

Captures where MySQL spends time during query execution:
    - Parsing
    - Optimizing
    - Executing
    - Sending data
    - Sorting
    - Creating tmp table

Requires stage instrumentation to be enabled:
    UPDATE performance_schema.setup_instruments
    SET ENABLED = 'YES', TIMED = 'YES'
    WHERE NAME LIKE 'stage/%';
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from collectors.base import BaseCollector
from collectors import queries
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)


class ExecutionStageCollector(BaseCollector):
    """Captures execution stage timing from performance_schema."""

    @property
    def name(self) -> str:
        return "execution_stages"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.EXECUTION_STAGES)
            rows = cursor.fetchall()

        for row in rows:
            row["snapshot_time"] = now
            row["server_id"] = ctx.server_id

        return {"execution_stages": rows}

    def store(self, data: dict) -> None:
        writer.write_execution_stages(data["execution_stages"])


_execution_stage_collector = ExecutionStageCollector()
