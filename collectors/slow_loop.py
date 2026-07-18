"""
Slow Loop Collector — runs every 30 minutes.

Captures slowly-changing structural data:
    - Table sizes and row counts
    - Schema fingerprints (for DDL change detection)
    - Full DDL when changes are detected
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from collectors.base import BaseCollector
from collectors import queries
from config import get_config, get_excluded_schemas_sql
from storage.connection import get_mon_reader
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)

DEFAULT_MAX_TABLES_PER_CYCLE = 2000


def _cap_tables_by_recency(
    rows: list[dict], max_tables: int
) -> tuple[list[tuple[str, str]], int]:
    """Order discovered tables by UPDATE_TIME DESC with NULLS LAST, and cap
    to at most `max_tables` (P1b-7 large-schema guardrail).

    NULLS LAST: a table with no UPDATE_TIME (MEMORY tables, or any storage
    engine/case that doesn't track it) sorts after every timestamped table
    instead of winning "most recently changed" purely by missing data.

    Splitting into two lists + concatenating avoids trying to encode both a
    descending sort (recent first) AND an ascending "is-it-null" sort in a
    single tuple key/reverse flag, which conflict; it's also agnostic to
    whether update_time is a real datetime (production) or an
    ISO-formatted string (test fixtures) — both support plain `<`/`>`.

    Returns (selected_keys, deferred_count): `selected_keys` is the ordered,
    capped list of (table_schema, table_name); `deferred_count` is how many
    discovered tables did NOT make the cut this cycle.
    """
    with_ts = [r for r in rows if r.get("update_time") is not None]
    without_ts = [r for r in rows if r.get("update_time") is None]
    with_ts.sort(key=lambda r: r["update_time"], reverse=True)
    ordered = with_ts + without_ts

    selected = ordered[:max_tables]
    deferred_count = max(0, len(ordered) - max_tables)
    selected_keys = [(r["table_schema"], r["table_name"]) for r in selected]
    return selected_keys, deferred_count


def _table_filter_clause(selected_keys: list[tuple[str, str]]) -> tuple[str, list]:
    """Build a parameterized `(TABLE_SCHEMA, TABLE_NAME) IN (...)` clause
    restricting a query to an explicit table subset, plus its flat param
    list (schema, table, schema, table, ...).

    Returns `("", [])` for an empty subset — the common case where the
    schema is under the cap and no restriction is needed at all — so the
    caller can render `{table_filter}` as a no-op blank line and leave the
    query byte-for-byte what it was before this guardrail existed.
    """
    if not selected_keys:
        return "", []
    placeholders = ", ".join(["(%s, %s)"] * len(selected_keys))
    params = [value for pair in selected_keys for value in pair]
    return f"AND (TABLE_SCHEMA, TABLE_NAME) IN ({placeholders})", params


def _run_filtered_query(cursor, template: str, excluded: str, table_filter: str, params: list):
    """Render `template` with the excluded-schemas + table-filter clauses
    and execute it, passing `params` only when the filter is non-empty —
    keeps the common (unfiltered) call an exact `cursor.execute(sql)`, same
    as before this guardrail existed."""
    sql = template.format(excluded_schemas=excluded, table_filter=table_filter)
    if params:
        cursor.execute(sql, params)
    else:
        cursor.execute(sql)
    return cursor.fetchall()


class SchemaSnapshotCollector(BaseCollector):
    """
    Captures schema fingerprints and detects DDL changes.

    Workflow:
        1. Load previous hashes from monitoring DB (first run only, per server).
        2. Discover the full table population and cap it to at most
           `slow_loop.max_tables_per_cycle` tables, ordered by UPDATE_TIME
           DESC / NULLS LAST (P1b-7 — large-schema guardrail; deferred
           tables are logged, never silently dropped).
        3. Compute MD5 hash of column + index definitions per selected table.
        4. Compare with previous hashes.
        5. If changed → capture full SHOW CREATE TABLE and log the change.
        6. Update cache for next run.

    Multi-server: hashes are keyed by (server_id, schema, table).
    """

    def __init__(self):
        super().__init__()
        # Keyed by server_id → {(schema, table): hash_info}
        self._previous_hashes: dict[str, dict[tuple[str, str], dict]] = {}
        self._initialized: set[str] = set()

    @property
    def name(self) -> str:
        return "schema_snapshot"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        excluded = get_excluded_schemas_sql()
        sid = ctx.server_id
        max_tables = get_config().get("slow_loop", {}).get(
            "max_tables_per_cycle", DEFAULT_MAX_TABLES_PER_CYCLE
        )

        # On first run for this server, load previous hashes from monitoring DB
        if sid not in self._initialized:
            self._load_previous_hashes(sid)
            self._initialized.add(sid)

        prev_hashes = self._previous_hashes.get(sid, {})

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)

            # MySQL's default group_concat_max_len (1024) silently truncates the
            # column/index fingerprint on wide tables — a column added past the
            # boundary would never change the hash (missed DDL). 1 MiB is enough
            # for any real table definition. Session-scoped; no privilege needed.
            cursor.execute("SET SESSION group_concat_max_len = 1048576")

            # 0. Discover the full table population first (P1b-7 large-schema
            # guardrail): a cheap, non-aggregating scan (no GROUP_CONCAT) whose
            # row count tells us whether the fingerprint/size queries below —
            # the genuinely expensive, per-column/per-index aggregations — need
            # to be capped for this server this cycle.
            cursor.execute(queries.TABLE_DISCOVERY.format(excluded_schemas=excluded))
            discovered = cursor.fetchall()
            selected_keys, deferred_count = _cap_tables_by_recency(discovered, max_tables)
            if deferred_count:
                logger.warning(
                    f"slow_loop[{sid}]: {len(discovered)} tables found, processing "
                    f"{len(selected_keys)} this cycle (slow_loop.max_tables_per_cycle="
                    f"{max_tables}), deferring {deferred_count} to a later cycle"
                )
            # Only restrict the queries below when capping actually happened —
            # the common case (schema at or under the cap) runs the exact same
            # SQL as before this guardrail existed.
            table_filter, table_filter_params = _table_filter_clause(
                selected_keys if deferred_count else []
            )

            # 1. Schema fingerprints
            schema_fps = {
                (r["table_schema"], r["table_name"]): r["schema_hash"]
                for r in _run_filtered_query(
                    cursor, queries.SCHEMA_FINGERPRINT, excluded,
                    table_filter, table_filter_params,
                )
            }

            # 2. Index fingerprints
            index_fps = {
                (r["table_schema"], r["table_name"]): r["index_hash"]
                for r in _run_filtered_query(
                    cursor, queries.INDEX_FINGERPRINT, excluded,
                    table_filter, table_filter_params,
                )
            }

            # 3. Table sizes
            table_sizes = {
                (r["table_schema"], r["table_name"]): r
                for r in _run_filtered_query(
                    cursor, queries.TABLE_SIZES, excluded,
                    table_filter, table_filter_params,
                )
            }

            # 4. Detect changes and capture DDL
            changes = []
            snapshot_rows = []

            all_tables = set(schema_fps.keys()) | set(index_fps.keys())

            for key in all_tables:
                schema, table = key
                s_hash = schema_fps.get(key, "")
                i_hash = index_fps.get(key, "")
                size_info = table_sizes.get(key, {})

                snapshot_row = {
                    "snapshot_time": now,
                    "server_id": sid,
                    "table_schema": schema,
                    "table_name": table,
                    "schema_hash": s_hash,
                    "index_hash": i_hash,
                    "create_stmt": None,
                    "table_rows": size_info.get("table_rows", 0) or 0,
                    "data_mb": size_info.get("data_mb", 0) or 0,
                    "index_mb": size_info.get("index_mb", 0) or 0,
                }

                # Compare against previous snapshot
                if key in prev_hashes:
                    prev = prev_hashes[key]
                    schema_changed = prev.get("schema_hash") != s_hash
                    index_changed = prev.get("index_hash") != i_hash

                    if schema_changed or index_changed:
                        ddl = self._get_create_table(conn, schema, table)
                        snapshot_row["create_stmt"] = ddl

                        change_type = (
                            "both" if (schema_changed and index_changed) else
                            "schema" if schema_changed else "index"
                        )

                        changes.append({
                            "detected_at": now,
                            "server_id": sid,
                            "table_schema": schema,
                            "table_name": table,
                            "change_type": change_type,
                            "old_schema_hash": prev.get("schema_hash"),
                            "new_schema_hash": s_hash,
                            "old_index_hash": prev.get("index_hash"),
                            "new_index_hash": i_hash,
                            "old_ddl": prev.get("create_stmt"),
                            "new_ddl": ddl,
                        })

                        logger.warning(
                            f"DDL change detected on `{schema}`.`{table}`: "
                            f"{change_type} changed"
                        )

                # New table (not in previous snapshot)
                elif prev_hashes:
                    ddl = self._get_create_table(conn, schema, table)
                    snapshot_row["create_stmt"] = ddl
                    logger.info(f"New table detected: `{schema}`.`{table}`")

                snapshot_rows.append(snapshot_row)

        # 5. Build the next-cycle hash cache, but do NOT install it yet.
        # (P1b-6) self._previous_hashes must only advance once store() has
        # durably written these rows — installing it here, before store()
        # even runs, means a store() failure (crash, disk full, SQLite
        # error) still leaves the cache pointing at the new hashes, so the
        # next cycle diffs against a change it never actually recorded and
        # the DDL change is lost forever. store() assigns this after a
        # successful write.
        new_hashes = {
            (r["table_schema"], r["table_name"]): {
                "schema_hash": r["schema_hash"],
                "index_hash": r["index_hash"],
                "create_stmt": r.get("create_stmt"),
            }
            for r in snapshot_rows
        }

        return {
            "snapshots": snapshot_rows,
            "changes": changes,
            "new_hashes": new_hashes,
            "sid": sid,
        }

    def store(self, data: dict) -> None:
        # Single transaction for both tables (P1b-6) — see
        # storage.writer.write_schema_and_changes for why.
        writer.write_schema_and_changes(data["snapshots"], data["changes"])
        # Only advance the cache after the write above returns successfully
        # — if it raised, this line never runs and self._previous_hashes
        # stays at its pre-store value, so the next collect() re-detects
        # the same change instead of silently losing it.
        self._previous_hashes[data["sid"]] = data["new_hashes"]
        if data["changes"]:
            logger.info(f"Logged {len(data['changes'])} DDL change(s)")

    def _get_create_table(self, conn, schema: str, table: str) -> str | None:
        """Fetch SHOW CREATE TABLE. Returns None on error."""
        try:
            cursor = conn.cursor()
            cursor.execute(f"SHOW CREATE TABLE `{schema}`.`{table}`")
            row = cursor.fetchone()
            return row[1] if row else None
        except Exception as e:
            logger.warning(f"Failed to get CREATE TABLE for `{schema}`.`{table}`: {e}")
            return None

    def _load_previous_hashes(self, server_id: str) -> None:
        """Load the most recent hash snapshot from the monitoring SQLite DB."""
        try:
            with get_mon_reader() as conn:
                cursor = conn.execute("""
                    SELECT table_schema, table_name, schema_hash, index_hash, create_stmt
                    FROM schema_snapshots
                    WHERE server_id = ?
                      AND snapshot_time = (
                        SELECT MAX(snapshot_time) FROM schema_snapshots WHERE server_id = ?
                    )
                """, (server_id, server_id))
                rows = cursor.fetchall()
                hashes = {}
                for row in rows:
                    key = (row["table_schema"], row["table_name"])
                    hashes[key] = {
                        "schema_hash": row["schema_hash"],
                        "index_hash": row["index_hash"],
                        "create_stmt": row["create_stmt"],
                    }
                self._previous_hashes[server_id] = hashes
                logger.info(
                    f"Loaded {len(hashes)} previous schema hashes "
                    f"from monitoring DB for server '{server_id}'"
                )
        except Exception as e:
            logger.warning(f"Could not load previous hashes for '{server_id}' (first run?): {e}")


# ---------------------------------------------------------------------------
# Composite runner
# ---------------------------------------------------------------------------

_schema_collector = SchemaSnapshotCollector()

# Import slow-loop collectors from other modules
from collectors.index_analysis import _unused_index_collector, _redundant_index_collector
from collectors.global_variables import _global_variable_collector

SLOW_COLLECTORS = [
    _schema_collector,
    _unused_index_collector,
    _redundant_index_collector,
    _global_variable_collector,
]


def run_slow_loop(ctx: ServerContext | None = None) -> dict[str, bool]:
    """Run all slow-loop collectors independently."""
    results = {}
    for collector in SLOW_COLLECTORS:
        results[collector.name] = collector.run(ctx)
    return results
