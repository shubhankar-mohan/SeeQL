"""
Query / tool budgeting for the webhook investigator.

Two concerns:

1. **Phase 2 tool budget** — cap the LLM's calls to live-MySQL tools and to
   the particularly expensive `explain_query` tool. Snapshot tools (which read
   the local SQLite) are unlimited.

2. **Phase 3 query budget + load-guard** — bound the investigator's extra
   load on the production server while it samples. Two protections:
     - Rolling per-minute query count against `investigation_samples.query_count`.
     - A Threads_running load-guard so we pause sampling when the server is
       already stressed.

Both guards are designed to fail safe: exceptions are caught and interpreted
as "don't allow the call / don't sample." The investigator treats a False
from any guard as "skip this sample / return a budget-exhausted tool result."
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)


# Tool name classification. Must be kept in sync with agent.tools —
# anything not listed here is treated as "snapshot" (free).
LIVE_TOOLS: set[str] = {
    "get_live_processlist",
    "get_live_locks",
    "get_live_innodb_status",
    "get_live_transactions",
    "get_index_stats",
    "get_table_status",
    # get_table_schema reads snapshots first but falls through to a live
    # SHOW CREATE TABLE on a miss, so its live path is budgeted too.
    "get_table_schema",
}
EXPENSIVE_TOOL = "explain_query"
# run_explain falls through to a LIVE `EXPLAIN FORMAT=JSON` against production
# when no cached plan exists (agent.tools._tool_run_explain), so it must be
# budgeted like explain_query — not treated as a free snapshot read. We can't
# tell cache-hit from cache-miss at budget-check time, so cached reads also
# count; that is the safe direction (bounds live load on a stressed server).
EXPENSIVE_TOOLS: set[str] = {EXPENSIVE_TOOL, "run_explain"}


@dataclass
class Budget:
    """
    Per-investigation budget tracker. Instances are short-lived and owned
    by the investigator; they hold mutable counters so they are NOT thread
    safe — do not share across concurrent investigations.
    """

    investigation_id: int
    live_tool_cap: int = 10
    explain_cap: int = 2
    queries_per_minute: int = 20
    _live_tool_used: int = field(default=0, init=False)
    _explain_used: int = field(default=0, init=False)
    _live_tool_log: list[str] = field(default_factory=list, init=False)

    def can_call_live(self) -> bool:
        return self._live_tool_used < self.live_tool_cap

    def can_call_explain(self) -> bool:
        return self._explain_used < self.explain_cap

    def can_call(self, tool_name: str) -> bool:
        """Single entry point used by the tool-execution wrapper."""
        if tool_name in EXPENSIVE_TOOLS:
            return self.can_call_explain()
        if tool_name in LIVE_TOOLS:
            return self.can_call_live()
        return True  # snapshot tools are free

    def record(self, tool_name: str) -> None:
        """Increment the counter after a successful tool call."""
        if tool_name in EXPENSIVE_TOOLS:
            self._explain_used += 1
        elif tool_name in LIVE_TOOLS:
            self._live_tool_used += 1
        self._live_tool_log.append(tool_name)

    def snapshot(self) -> dict:
        return {
            "investigation_id": self.investigation_id,
            "live_tool_used": self._live_tool_used,
            "live_tool_cap": self.live_tool_cap,
            "explain_used": self._explain_used,
            "explain_cap": self.explain_cap,
            "queries_per_minute": self.queries_per_minute,
        }

    def rejection_message(self, tool_name: str) -> str:
        """Message the LLM sees when a call is budget-rejected."""
        if tool_name in EXPENSIVE_TOOLS:
            return (
                "Budget exhausted for EXPLAIN queries "
                f"({self._explain_used}/{self.explain_cap}). "
                "Rely on EXPLAIN plans already captured in the snapshot data; "
                "do not run further live EXPLAINs."
            )
        if tool_name in LIVE_TOOLS:
            return (
                "Budget exhausted for live-MySQL tools "
                f"({self._live_tool_used}/{self.live_tool_cap}). "
                "Continue with snapshot tools only."
            )
        return "Unexpected budget rejection."


# ---------------------------------------------------------------------------
# Phase 3 rolling-window guard
# ---------------------------------------------------------------------------

def queries_used_in_last_minute(investigation_id: int) -> int:
    """
    Sum `query_count` from `investigation_samples` for this investigation
    in the last 60 seconds. Returns 0 on error (fail-safe for sampling).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(query_count), 0) AS n
                FROM investigation_samples
                WHERE investigation_id = ? AND sampled_at >= ?
                """,
                (investigation_id, cutoff),
            ).fetchone()
            return int(row["n"]) if row else 0
    except Exception as e:
        logger.warning(f"queries_used_in_last_minute: {e}")
        return 0


# ---------------------------------------------------------------------------
# Load-guard — prefer the cheap SQLite path; only query MySQL directly as a
# last resort.
# ---------------------------------------------------------------------------

def threads_running_from_snapshot(
    server_id: str,
    max_age_seconds: int = 90,
) -> int | None:
    """
    Return the latest `Threads_running` value collected by the fast loop,
    or None if the snapshot is older than `max_age_seconds` (the fast loop
    runs every 30s, so a 90s cutoff catches two consecutive misses).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).isoformat()
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT raw_value, snapshot_time
                FROM global_status_snapshots
                WHERE server_id = ?
                  AND variable_name = 'Threads_running'
                  AND snapshot_time >= ?
                ORDER BY snapshot_time DESC
                LIMIT 1
                """,
                (server_id, cutoff),
            ).fetchone()
            if row is None:
                return None
            return int(row["raw_value"])
    except Exception as e:
        logger.warning(f"threads_running_from_snapshot({server_id}): {e}")
        return None


def threads_running_ok(
    server_id: str,
    threshold: int,
    max_age_seconds: int = 90,
) -> bool:
    """
    Return True if the server's Threads_running is at or below `threshold`.
    Prefers the SQLite snapshot (zero MySQL cost). If the snapshot is stale,
    returns True conservatively — the investigator will catch genuine load
    problems via its other clearance checks and the explicit live probe run
    at the top of each Phase-3 sample.
    """
    current = threads_running_from_snapshot(server_id, max_age_seconds)
    if current is None:
        # Stale / missing data: don't block sampling; the caller's explicit
        # live probe is still authoritative.
        return True
    return current <= threshold
