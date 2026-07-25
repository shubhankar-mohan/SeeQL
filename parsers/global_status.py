"""
Global Status Delta Calculator.

MySQL's SHOW GLOBAL STATUS returns cumulative counters since server restart.
This module stores the last raw snapshot, computes delta (new - old) and
rate (delta / seconds) on each new snapshot.

Only tracks a curated list of "interesting" variables to avoid bloat.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Curated list of status variables worth tracking, grouped by category.
TRACKED_VARIABLES = {
    # Query throughput
    "Questions", "Queries", "Com_select", "Com_insert", "Com_update",
    "Com_delete", "Com_replace", "Slow_queries",

    # Connections
    "Threads_connected", "Threads_running", "Threads_created",
    "Connections", "Aborted_connects", "Aborted_clients",
    "Max_used_connections",

    # Temp tables & sorts
    "Created_tmp_tables", "Created_tmp_disk_tables", "Sort_merge_passes",

    # Handler stats (row-level operations)
    "Handler_read_rnd_next", "Handler_read_first", "Handler_read_key",
    "Handler_write",

    # Table locks
    "Table_locks_waited", "Table_locks_immediate",

    # InnoDB
    "Innodb_row_lock_waits", "Innodb_row_lock_time",
    "Innodb_rows_read", "Innodb_rows_inserted",
    "Innodb_rows_updated", "Innodb_rows_deleted",
    "Innodb_buffer_pool_reads", "Innodb_buffer_pool_read_requests",
    "Innodb_buffer_pool_write_requests",
    "Innodb_data_reads", "Innodb_data_writes",
    "Innodb_log_waits", "Innodb_deadlocks",

    # Network
    "Bytes_received", "Bytes_sent",

    # Select types
    "Select_full_join", "Select_scan", "Select_range",
}

# Point-in-time gauges: unlike the counters above, these rise AND fall
# during normal operation, so a decrease is not a "possible server restart"
# and a per-second rate would be meaningless. Stored as raw_value only
# (delta_value/per_second always None).
#
# Audited against TRACKED_VARIABLES (P1b-5): only Threads_connected,
# Threads_running, and Max_used_connections are in TRACKED_VARIABLES today;
# the rest are included for forward-compatibility if they're ever added
# there (harmless no-ops until then — the process() loop only ever sees
# names already filtered through TRACKED_VARIABLES). Every other entry in
# TRACKED_VARIABLES is a monotonic cumulative counter that only resets on
# an actual server restart, so none of them belong here.
GAUGE_VARIABLES = {
    "Threads_connected", "Threads_running", "Threads_cached",
    "Max_used_connections", "Open_tables", "Open_files",
    "Innodb_row_lock_current_waits",
}


class GlobalStatusDeltaCalculator:
    """
    Computes deltas between consecutive SHOW GLOBAL STATUS snapshots.

    First call returns delta_value=None (no previous data).
    Subsequent calls compute delta and per_second rate.
    If a counter decreases (server restart), delta is skipped.
    Gauges (see GAUGE_VARIABLES) are point-in-time values rather than
    cumulative counters: only raw_value is stored, and a decrease is
    normal, not a restart signal.
    """

    def __init__(self):
        self._last_snapshot: dict[str, int] | None = None
        self._last_time: datetime | None = None

    def process(self, raw_rows: list[dict], now: datetime) -> list[dict]:
        """
        Process raw SHOW GLOBAL STATUS rows into delta rows.

        Args:
            raw_rows: List of {"Variable_name": str, "Value": str} dicts.
            now:      Current snapshot timestamp.

        Returns:
            List of dicts with: snapshot_time, variable_name, raw_value,
            delta_value, per_second.
        """
        current = {}
        for row in raw_rows:
            name = row["Variable_name"]
            if name not in TRACKED_VARIABLES:
                continue
            try:
                current[name] = int(row["Value"])
            except (ValueError, TypeError):
                continue

        elapsed_sec = None
        if self._last_time is not None:
            elapsed_sec = (now - self._last_time).total_seconds()
            if elapsed_sec <= 0:
                elapsed_sec = None

        output = []
        for name, value in current.items():
            row = {
                "snapshot_time": now,
                "variable_name": name,
                "raw_value": value,
                "delta_value": None,
                "per_second": None,
            }

            if name in GAUGE_VARIABLES:
                # Gauges are stored as raw_value only. A drop is normal
                # operation, not a restart, so the decrease check below is
                # intentionally skipped for these variables.
                output.append(row)
                continue

            if self._last_snapshot is not None and name in self._last_snapshot:
                prev = self._last_snapshot[name]
                delta = value - prev

                if delta < 0:
                    logger.warning(
                        f"Counter '{name}' decreased ({prev} → {value}), "
                        f"possible server restart. Skipping delta."
                    )
                else:
                    row["delta_value"] = delta
                    if elapsed_sec:
                        row["per_second"] = round(delta / elapsed_sec, 4)

            output.append(row)

        self._last_snapshot = current
        self._last_time = now
        return output
