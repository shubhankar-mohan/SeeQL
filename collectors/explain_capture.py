"""
Auto-EXPLAIN Collector — runs in the medium loop.

Automatically captures EXPLAIN JSON for the top-N most expensive
queries by total time. This gives the LLM agent the ability to:
    - See full scan vs index scan
    - Detect missing indexes
    - Identify suboptimal join strategies
    - Track plan changes over time

Safety:
    - Uses EXPLAIN only (read-only, never executes the query)
    - Skips DDL, DML, and non-SELECT statements
    - Truncates large plans to 64KB
    - Has its own error handling per query
"""

from __future__ import annotations

import json
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

# Statements safe to EXPLAIN
_EXPLAINABLE_PREFIXES = ("SELECT", "WITH")

# Max EXPLAIN JSON size to store
_MAX_EXPLAIN_SIZE = 65536


class ExplainCaptureCollector(BaseCollector):
    """Captures EXPLAIN JSON for top-N expensive queries."""

    @property
    def name(self) -> str:
        return "explain_capture"

    def collect(self, now: datetime, ctx: ServerContext) -> dict:
        limits = get_limits()
        excluded = get_excluded_schemas_sql()
        top_n = limits.get("explain_top_n", 10)

        # Get top-N queries by total time
        digest_sql = queries.QUERY_DIGESTS.format(
            excluded_schemas=excluded,
            max_digest_len=limits.get("digest_text_max_len", 200),
            limit=top_n,
        )

        with ctx.get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute(digest_sql)
            top_queries = cursor.fetchall()

            rows = []
            for q in top_queries:
                # Prefer query_sample_text (real SQL with values) over digest_text
                # digest_text has ? placeholders and is truncated — can't be EXPLAINed
                sql_text = q.get("query_sample_text") or q.get("digest_text") or ""
                if not sql_text:
                    continue

                # Only EXPLAIN SELECT/WITH statements
                normalized = sql_text.strip().upper()
                if not normalized.startswith(_EXPLAINABLE_PREFIXES):
                    continue

                explain_json = self._run_explain(conn, q, sql_text)
                if explain_json:
                    rows.append({
                        "captured_at": now,
                        "server_id": ctx.server_id,
                        "digest": q.get("digest"),
                        "digest_text": q.get("digest_text", ""),
                        "schema_name": q.get("schema_name"),
                        "explain_json": explain_json,
                        "total_time_sec": q.get("total_time_sec"),
                        "avg_time_sec": q.get("avg_time_sec"),
                        "exec_count": q.get("exec_count"),
                    })

        return {"explain_captures": rows}

    def store(self, data: dict) -> None:
        if data["explain_captures"]:
            writer.write_explain_captures(data["explain_captures"])

    def _run_explain(self, conn, query_info: dict, sql_text: str) -> str | None:
        """Run EXPLAIN FORMAT=JSON on a query. Returns JSON string or None."""
        schema = query_info.get("schema_name")

        try:
            cursor = conn.cursor(dictionary=True)

            # Switch to the query's schema if known
            if schema:
                cursor.execute(f"USE `{schema}`")

            explain_sql = queries.EXPLAIN_PREFIX + sql_text
            cursor.execute(explain_sql)
            result = cursor.fetchone()

            if not result:
                return None

            # EXPLAIN FORMAT=JSON returns a single column named 'EXPLAIN'
            explain_output = result.get("EXPLAIN", "")
            if len(explain_output) > _MAX_EXPLAIN_SIZE:
                explain_output = explain_output[:_MAX_EXPLAIN_SIZE]

            # Validate it's actual JSON
            json.loads(explain_output)
            return explain_output

        except Exception as e:
            logger.debug(
                f"EXPLAIN failed for digest {query_info.get('digest', '?')[:16]}: {e}"
            )
            return None


_explain_capture_collector = ExplainCaptureCollector()
