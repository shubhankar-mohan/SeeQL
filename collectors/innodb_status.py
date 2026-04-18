"""
InnoDB Status Collector — runs in the medium loop.

Captures and parses SHOW ENGINE INNODB STATUS output.
Key value: deadlock detection, semaphore contention, row operation rates.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from collectors.base import BaseCollector
from collectors import queries
from parsers.innodb_status import parse_innodb_status
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)


class InnoDBStatusCollector(BaseCollector):
    """Captures and parses SHOW ENGINE INNODB STATUS."""

    @property
    def name(self) -> str:
        return "innodb_status"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        with ctx.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(queries.INNODB_STATUS)
            result = cursor.fetchone()

        if not result:
            return {"innodb_status": []}

        # SHOW ENGINE INNODB STATUS returns (Type, Name, Status)
        raw_text = result[2] if len(result) > 2 else ""

        sections = parse_innodb_status(raw_text)
        for section in sections:
            section["snapshot_time"] = now
            section["server_id"] = ctx.server_id

        return {"innodb_status": sections}

    def store(self, data: dict) -> None:
        if data["innodb_status"]:
            writer.write_innodb_status(data["innodb_status"])


_innodb_status_collector = InnoDBStatusCollector()
