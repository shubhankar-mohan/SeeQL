"""
Base collector class for MySQL DBA Agent.

All collectors inherit from BaseCollector. It provides:
    - Standardized collect → store workflow
    - Error isolation (one collector failing doesn't kill others)
    - Retry logic for transient failures (connection drops, timeouts)
    - Timing and logging for each collection run

Subclasses implement:
    - name: Human-readable name for logging.
    - collect(now, ctx): Run queries against production MySQL, return raw data.
    - store(data): Write processed data to the monitoring database.
"""

from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import mysql.connector

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)

MAX_RETRIES = 2
RETRY_BACKOFF_SEC = 1.0

# MySQL error codes that are transient and worth retrying
TRANSIENT_ERRORS = {
    2003,   # Can't connect to MySQL server
    2006,   # MySQL server has gone away
    2013,   # Lost connection during query
    2055,   # Lost connection at reading
    1205,   # Lock wait timeout exceeded
}


class BaseCollector(ABC):

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable collector name, used in logs."""
        ...

    @abstractmethod
    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        """
        Collect metrics from a specific production database server.

        Args:
            now: Current timestamp. Use this for snapshot_time so all
                 metrics in one cycle share the same timestamp.
            ctx: Server context identifying which MySQL server to query.

        Returns:
            Dict of collected data. Structure is collector-specific.
        """
        ...

    @abstractmethod
    def store(self, data: dict) -> None:
        """Write collected data to the monitoring database."""
        ...

    def run(self, ctx: ServerContext | None = None) -> bool:
        """
        Execute one complete collection cycle: collect → store.

        Args:
            ctx: Server context. If None, uses the default server.

        Retries on transient MySQL errors up to MAX_RETRIES times
        with exponential backoff.

        Returns:
            True if successful, False if all attempts failed.
        """
        if ctx is None:
            from config.server_registry import get_server_registry
            registry = get_server_registry()
            default_id = registry.get_default_server_id()
            server = registry.get_server(default_id)
            ctx = server.to_context()

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for attempt in range(MAX_RETRIES + 1):
            start = time.monotonic()
            try:
                data = self.collect(now, ctx)

                if not data:
                    logger.debug(f"[{self.name}] No data collected.")
                    return True

                self.store(data)

                elapsed = time.monotonic() - start
                logger.info(f"[{self.name}] Complete in {elapsed:.2f}s")
                return True

            except mysql.connector.Error as e:
                elapsed = time.monotonic() - start
                is_transient = getattr(e, "errno", None) in TRANSIENT_ERRORS

                if is_transient and attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SEC * (2 ** attempt)
                    logger.warning(
                        f"[{self.name}] Transient error (attempt {attempt + 1}/"
                        f"{MAX_RETRIES + 1}), retrying in {wait}s: {e}"
                    )
                    time.sleep(wait)
                    continue

                logger.error(
                    f"[{self.name}] FAILED after {elapsed:.2f}s: {e}",
                    exc_info=True,
                )
                return False

            except Exception as e:
                elapsed = time.monotonic() - start
                logger.error(
                    f"[{self.name}] FAILED after {elapsed:.2f}s: {e}",
                    exc_info=True,
                )
                return False

        return False
