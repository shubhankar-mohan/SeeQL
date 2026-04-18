"""
Global Variables Collector — runs in the slow loop.

Captures SHOW GLOBAL VARIABLES for a curated set of important
server configuration parameters. Useful for:
    - Detecting config changes between snapshots
    - Giving the LLM agent context about server tuning
    - Correlating config changes with performance shifts
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


class GlobalVariableCollector(BaseCollector):
    """Captures curated SHOW GLOBAL VARIABLES."""

    @property
    def name(self) -> str:
        return "global_variables"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(queries.GLOBAL_VARIABLES)
            raw_rows = cursor.fetchall()

        # Filter to only tracked variables
        tracked = set(queries.TRACKED_VARIABLES)
        rows = []
        for row in raw_rows:
            var_name = row.get("Variable_name", "").lower()
            if var_name in tracked:
                rows.append({
                    "snapshot_time": now,
                    "server_id": ctx.server_id,
                    "variable_name": var_name,
                    "variable_value": row.get("Value"),
                })

        return {"global_variables": rows}

    def store(self, data: dict) -> None:
        writer.write_global_variables(data["global_variables"])


_global_variable_collector = GlobalVariableCollector()
