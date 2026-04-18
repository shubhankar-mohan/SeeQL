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
from config import get_excluded_schemas_sql
from storage.connection import get_mon_reader
from storage import writer

if TYPE_CHECKING:
    from config.server_context import ServerContext

logger = logging.getLogger(__name__)


class SchemaSnapshotCollector(BaseCollector):
    """
    Captures schema fingerprints and detects DDL changes.

    Workflow:
        1. Load previous hashes from monitoring DB (first run only, per server).
        2. Compute MD5 hash of column + index definitions per table.
        3. Compare with previous hashes.
        4. If changed → capture full SHOW CREATE TABLE and log the change.
        5. Update cache for next run.

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

        # On first run for this server, load previous hashes from monitoring DB
        if sid not in self._initialized:
            self._load_previous_hashes(sid)
            self._initialized.add(sid)

        prev_hashes = self._previous_hashes.get(sid, {})

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)

            # 1. Schema fingerprints
            cursor.execute(queries.SCHEMA_FINGERPRINT.format(excluded_schemas=excluded))
            schema_fps = {
                (r["table_schema"], r["table_name"]): r["schema_hash"]
                for r in cursor.fetchall()
            }

            # 2. Index fingerprints
            cursor.execute(queries.INDEX_FINGERPRINT.format(excluded_schemas=excluded))
            index_fps = {
                (r["table_schema"], r["table_name"]): r["index_hash"]
                for r in cursor.fetchall()
            }

            # 3. Table sizes
            cursor.execute(queries.TABLE_SIZES.format(excluded_schemas=excluded))
            table_sizes = {
                (r["table_schema"], r["table_name"]): r
                for r in cursor.fetchall()
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

        # 5. Update cache for next run
        self._previous_hashes[sid] = {
            (r["table_schema"], r["table_name"]): {
                "schema_hash": r["schema_hash"],
                "index_hash": r["index_hash"],
                "create_stmt": r.get("create_stmt"),
            }
            for r in snapshot_rows
        }

        return {"snapshots": snapshot_rows, "changes": changes}

    def store(self, data: dict) -> None:
        writer.write_schema_snapshots(data["snapshots"])
        if data["changes"]:
            writer.write_ddl_changes(data["changes"])
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
