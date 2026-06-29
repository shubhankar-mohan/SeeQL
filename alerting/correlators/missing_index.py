"""
Missing-index correlator.

Joins SQLite-stored signals to produce structured "this digest is probably
missing an index, here's why" evidence. Zero cost on the production MySQL
server — pure reads from the monitoring DB.

Signals consumed:

    query_digest_snapshots   rows_examined, rows_sent, full_scans, no_index_used
    explain_captures         most recent EXPLAIN for each digest
    ddl_changes              recent index DDL on referenced tables
    unused_index_snapshots   so we never recommend a duplicate of an unused index
    redundant_index_snapshots ditto

The correlator does NOT invent new SQL — it reuses the same signal columns
the collectors and state builder already populate.
"""

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)


# Ratio above which a digest is deemed a strong missing-index signal.
DEFAULT_RATIO_THRESHOLD = 100.0


@dataclass
class MissingIndexEvidence:
    digest: str
    digest_text: str | None
    schema_name: str | None
    table_name: str | None                 # best-effort extraction from EXPLAIN or digest_text
    rows_examined: int
    rows_sent: int
    ratio: float                           # rows_examined / rows_sent (safe-divided)
    full_scans: int
    no_index_used: int
    explain_summary: str | None            # e.g. "type=ALL, key=NULL, rows=1000000"
    explain_captured_at: str | None
    recent_ddl: list[dict] = field(default_factory=list)
    dropped_index_hint: str | None = None  # name of an index that was dropped recently
    unused_indexes: list[dict] = field(default_factory=list)
    redundant_indexes: list[dict] = field(default_factory=list)
    recommended_index: str | None = None   # best-guess CREATE INDEX DDL, if inferrable
    confidence: float = 0.0                # 0.0-1.0 heuristic


@dataclass
class MissingIndexCorrelation:
    server_id: str
    window_start: str
    window_end: str
    suspect_digests: list[str]
    evidence: list[MissingIndexEvidence] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return any(e.ratio >= DEFAULT_RATIO_THRESHOLD for e in self.evidence)

    @property
    def top_evidence(self) -> MissingIndexEvidence | None:
        if not self.evidence:
            return None
        return max(self.evidence, key=lambda e: (e.confidence, e.ratio))

    def to_markdown(self) -> str:
        if not self.evidence:
            return "_No missing-index signals correlated for this window._"

        lines = [
            "**Missing-index evidence** "
            f"(window {self.window_start} → {self.window_end}, server `{self.server_id}`)",
            "",
        ]
        for e in self.evidence:
            header = (
                f"- `{e.digest}`"
                + (f" on `{e.schema_name}.{e.table_name}`" if e.table_name else "")
                + f" — ratio {e.ratio:.0f}x"
                + (f", confidence {e.confidence:.2f}" if e.confidence else "")
            )
            lines.append(header)
            if e.digest_text:
                snippet = e.digest_text.strip().replace("\n", " ")
                if len(snippet) > 140:
                    snippet = snippet[:140] + "…"
                lines.append(f"    - Query: `{snippet}`")
            lines.append(
                f"    - rows_examined={e.rows_examined:,} rows_sent={e.rows_sent:,} "
                f"full_scans={e.full_scans} no_index_used={e.no_index_used}"
            )
            if e.explain_summary:
                lines.append(f"    - EXPLAIN: {e.explain_summary}")
            if e.dropped_index_hint:
                lines.append(
                    f"    - ⚠ Recently dropped index: `{e.dropped_index_hint}`"
                )
            if e.recent_ddl:
                types = ", ".join({d.get("change_type") or "?" for d in e.recent_ddl})
                lines.append(f"    - Recent DDL ({types}) on this table")
            if e.unused_indexes:
                names = ", ".join(u.get("index_name") or "?" for u in e.unused_indexes)
                lines.append(f"    - Pre-existing unused indexes on table: {names}")
            if e.redundant_indexes:
                names = ", ".join(
                    r.get("redundant_index_name") or "?" for r in e.redundant_indexes
                )
                lines.append(f"    - Redundant indexes on table: {names}")
            if e.recommended_index:
                lines.append(f"    - Suggested: `{e.recommended_index}`")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "server_id": self.server_id,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "suspect_digests": list(self.suspect_digests),
            "evidence": [asdict(e) for e in self.evidence],
            "has_findings": self.has_findings,
        }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def correlate_missing_index(
    server_id: str,
    window_start: str,
    window_end: str,
    suspect_digests: Iterable[str] | None = None,
    top_n: int = 5,
    ratio_threshold: float = DEFAULT_RATIO_THRESHOLD,
) -> MissingIndexCorrelation:
    """
    Main entry. Returns structured evidence for digests with suspiciously
    high rows_examined/rows_sent ratio in the given window.

    If `suspect_digests` is provided, only those digests are considered.
    Otherwise the correlator picks the top-N digests by ratio during the
    window.
    """
    suspects = list(suspect_digests) if suspect_digests else []
    evidence: list[MissingIndexEvidence] = []

    try:
        with get_mon_reader() as conn:
            if not suspects:
                suspects = _discover_suspect_digests(
                    conn, server_id, window_start, window_end, top_n
                )
            if not suspects:
                return MissingIndexCorrelation(
                    server_id=server_id,
                    window_start=window_start,
                    window_end=window_end,
                    suspect_digests=[],
                )

            for digest in suspects:
                row = _fetch_latest_digest_stats(
                    conn, server_id, digest, window_start, window_end
                )
                if not row:
                    continue
                rows_examined = int(row["rows_examined"] or 0)
                rows_sent = int(row["rows_sent"] or 0)
                ratio = (
                    rows_examined / rows_sent if rows_sent > 0 else float(rows_examined)
                )

                explain = _fetch_latest_explain(conn, server_id, digest)
                explain_summary, table_hint = _summarize_explain(explain)

                # If EXPLAIN didn't tell us the table, try to pluck it from the
                # digest_text as a best-effort (FROM <schema>.<table> or FROM <table>).
                table_name = table_hint or _guess_table_from_digest(row["digest_text"])
                schema_name = row["schema_name"]

                recent_ddl = []
                dropped_index = None
                unused_rows: list[dict] = []
                redundant_rows: list[dict] = []
                if table_name:
                    recent_ddl = _fetch_recent_ddl(
                        conn, server_id, schema_name, table_name, window_start
                    )
                    dropped_index = _identify_dropped_index(recent_ddl)
                    unused_rows = _fetch_unused_indexes(conn, server_id, schema_name, table_name)
                    redundant_rows = _fetch_redundant_indexes(conn, server_id, schema_name, table_name)

                recommended = _recommend_index(
                    schema_name, table_name, row, unused_rows, redundant_rows
                )
                confidence = _confidence_score(
                    ratio=ratio,
                    ratio_threshold=ratio_threshold,
                    has_explain=explain is not None,
                    has_dropped_index=bool(dropped_index),
                    no_index_used=int(row["no_index_used"] or 0),
                    full_scans=int(row["full_scans"] or 0),
                )

                evidence.append(
                    MissingIndexEvidence(
                        digest=digest,
                        digest_text=row["digest_text"],
                        schema_name=schema_name,
                        table_name=table_name,
                        rows_examined=rows_examined,
                        rows_sent=rows_sent,
                        ratio=ratio,
                        full_scans=int(row["full_scans"] or 0),
                        no_index_used=int(row["no_index_used"] or 0),
                        explain_summary=explain_summary,
                        explain_captured_at=(
                            explain["captured_at"] if explain else None
                        ),
                        recent_ddl=recent_ddl,
                        dropped_index_hint=dropped_index,
                        unused_indexes=unused_rows,
                        redundant_indexes=redundant_rows,
                        recommended_index=recommended,
                        confidence=confidence,
                    )
                )
    except Exception as e:
        logger.warning(f"correlate_missing_index failed: {e}")

    evidence.sort(key=lambda ev: (ev.confidence, ev.ratio), reverse=True)
    return MissingIndexCorrelation(
        server_id=server_id,
        window_start=window_start,
        window_end=window_end,
        suspect_digests=suspects,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _discover_suspect_digests(
    conn, server_id: str, window_start: str, window_end: str, top_n: int
) -> list[str]:
    sql = """
        SELECT digest
        FROM query_digest_snapshots
        WHERE server_id = ?
          AND snapshot_time BETWEEN ? AND ?
          -- Rank by rows_examined per returned row (rows_sent floored at 1, so
          -- a full scan returning zero rows scores on the same scale instead of
          -- being dropped). rows_sent == 0 is kept for BOTH zero-row SELECTs
          -- AND write statements (UPDATE/DELETE ... WHERE unindexed_col):
          -- write-side full scans are a primary lock-cascade cause this project
          -- targets, so they must remain eligible — not filtered out.
          AND CAST(rows_examined AS REAL) / MAX(rows_sent, 1) >= ?
        GROUP BY digest
        ORDER BY MAX(CAST(rows_examined AS REAL) / MAX(rows_sent, 1)) DESC
        LIMIT ?
    """
    rows = conn.execute(
        sql, (server_id, window_start, window_end, DEFAULT_RATIO_THRESHOLD, top_n)
    ).fetchall()
    return [r["digest"] for r in rows]


def _fetch_latest_digest_stats(
    conn, server_id: str, digest: str, window_start: str, window_end: str
) -> dict | None:
    row = conn.execute(
        """
        SELECT digest, digest_text, schema_name,
               rows_examined, rows_sent, exec_count,
               full_scans, no_index_used, avg_time_sec,
               snapshot_time
        FROM query_digest_snapshots
        WHERE server_id = ?
          AND digest = ?
          AND snapshot_time BETWEEN ? AND ?
        ORDER BY snapshot_time DESC
        LIMIT 1
        """,
        (server_id, digest, window_start, window_end),
    ).fetchone()
    if row is None:
        # Widen window — digest may only exist outside the strict window.
        row = conn.execute(
            """
            SELECT digest, digest_text, schema_name,
                   rows_examined, rows_sent, exec_count,
                   full_scans, no_index_used, avg_time_sec,
                   snapshot_time
            FROM query_digest_snapshots
            WHERE server_id = ? AND digest = ?
            ORDER BY snapshot_time DESC
            LIMIT 1
            """,
            (server_id, digest),
        ).fetchone()
    return dict(row) if row else None


def _fetch_latest_explain(conn, server_id: str, digest: str) -> dict | None:
    row = conn.execute(
        """
        SELECT captured_at, schema_name, explain_json
        FROM explain_captures
        WHERE server_id = ? AND digest = ?
        ORDER BY captured_at DESC
        LIMIT 1
        """,
        (server_id, digest),
    ).fetchone()
    return dict(row) if row else None


def _summarize_explain(explain: dict | None) -> tuple[str | None, str | None]:
    """Return (one-line summary, table hint) from an explain_captures row."""
    if not explain or not explain.get("explain_json"):
        return (None, None)
    try:
        data = json.loads(explain["explain_json"])
    except Exception:
        return (None, None)

    # MySQL EXPLAIN FORMAT=JSON nests the plan. Walk for the first
    # query_block / table / table_name, type, key, rows_examined_per_scan.
    plan = data.get("query_block") or data
    table = _find_first(plan, "table")
    if not isinstance(table, dict):
        return (None, None)
    summary = (
        f"type={table.get('access_type', '?')}, "
        f"key={table.get('key', 'NULL') or 'NULL'}, "
        f"rows={table.get('rows_examined_per_scan', '?')}"
    )
    table_name = table.get("table_name")
    return (summary, table_name if isinstance(table_name, str) else None)


def _find_first(tree: Any, key: str) -> Any:
    """Depth-first search for the first value under `key` anywhere in a nested dict/list."""
    if isinstance(tree, dict):
        if key in tree:
            return tree[key]
        for v in tree.values():
            found = _find_first(v, key)
            if found is not None:
                return found
    elif isinstance(tree, list):
        for item in tree:
            found = _find_first(item, key)
            if found is not None:
                return found
    return None


_TABLE_FROM_RE = re.compile(
    r"FROM\s+`?(?:(\w+)`?\.`?)?(\w+)`?",
    re.IGNORECASE,
)


def _guess_table_from_digest(digest_text: str | None) -> str | None:
    """Very best-effort: first FROM clause wins. Works for the common case."""
    if not digest_text:
        return None
    m = _TABLE_FROM_RE.search(digest_text)
    if not m:
        return None
    return m.group(2)


def _fetch_recent_ddl(
    conn, server_id: str, schema_name: str | None, table_name: str, window_start: str
) -> list[dict]:
    # Look back 24 hours from window_start for any DDL on this table. DDL that
    # happened before the current window is the most interesting — it's what
    # could have caused the regression.
    try:
        lookback = (
            datetime.fromisoformat(window_start.replace("Z", "+00:00"))
            - timedelta(hours=24)
        ).isoformat()
    except Exception:
        lookback = window_start

    sql = """
        SELECT detected_at, change_type, old_ddl, new_ddl,
               old_index_hash, new_index_hash
        FROM ddl_changes
        WHERE server_id = ?
          AND table_name = ?
          AND detected_at >= ?
        ORDER BY detected_at DESC
        LIMIT 10
    """
    params = [server_id, table_name, lookback]
    if schema_name:
        sql = sql.replace(
            "WHERE server_id = ?",
            "WHERE server_id = ? AND table_schema = ?",
        )
        params = [server_id, schema_name, table_name, lookback]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


_KEY_LINE_RE = re.compile(r"KEY\s+`?([^`\s(]+)`?\s*\(", re.IGNORECASE)


def _identify_dropped_index(recent_ddl: list[dict]) -> str | None:
    """
    Best-effort: diff old_ddl vs new_ddl line-by-line. Any KEY `name` (...)
    that appears in old but not new → dropped index. Returns the first such
    name found across the most recent DDL rows.
    """
    for change in recent_ddl:
        old = change.get("old_ddl") or ""
        new = change.get("new_ddl") or ""
        if not old or not new:
            continue
        old_keys = set(_KEY_LINE_RE.findall(old))
        new_keys = set(_KEY_LINE_RE.findall(new))
        dropped = old_keys - new_keys
        if dropped:
            return sorted(dropped)[0]
    return None


def _fetch_unused_indexes(
    conn, server_id: str, schema_name: str | None, table_name: str
) -> list[dict]:
    sql = """
        SELECT index_name, snapshot_time
        FROM unused_index_snapshots
        WHERE server_id = ? AND table_name = ?
        ORDER BY snapshot_time DESC
        LIMIT 10
    """
    params = [server_id, table_name]
    if schema_name:
        sql = sql.replace(
            "WHERE server_id = ?",
            "WHERE server_id = ? AND object_schema = ?",
        )
        params = [server_id, schema_name, table_name]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _fetch_redundant_indexes(
    conn, server_id: str, schema_name: str | None, table_name: str
) -> list[dict]:
    sql = """
        SELECT redundant_index_name, redundant_index_columns,
               dominant_index_name, sql_drop_index, snapshot_time
        FROM redundant_index_snapshots
        WHERE server_id = ? AND table_name = ?
        ORDER BY snapshot_time DESC
        LIMIT 10
    """
    params = [server_id, table_name]
    if schema_name:
        sql = sql.replace(
            "WHERE server_id = ?",
            "WHERE server_id = ? AND table_schema = ?",
        )
        params = [server_id, schema_name, table_name]
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _recommend_index(
    schema_name: str | None,
    table_name: str | None,
    digest_row: dict,
    unused_rows: list[dict],
    redundant_rows: list[dict],
) -> str | None:
    """
    Absurdly conservative recommendation — we don't have column access
    patterns to infer the right index. So we only suggest when a previously
    dropped index is a clear candidate (see correlate logic) or when there's
    a redundant index to drop. The LLM layer does the real CREATE INDEX
    recommendation after calling get_table_schema + get_index_stats.
    """
    # Drop candidate from redundant indexes (mechanical recommendation)
    for r in redundant_rows:
        drop = r.get("sql_drop_index")
        if drop and str(drop).strip().upper().startswith("ALTER TABLE"):
            return drop.strip().rstrip(";")
    return None


def _confidence_score(
    *,
    ratio: float,
    ratio_threshold: float,
    has_explain: bool,
    has_dropped_index: bool,
    no_index_used: int,
    full_scans: int,
) -> float:
    """
    Combine signals into a 0.0 - 1.0 confidence score. Tuned so that:
      ratio >= threshold && EXPLAIN shows type=ALL && recently dropped index
        ≈ 0.95
      ratio >= threshold alone ≈ 0.55
      ratio < threshold with other signals ≈ 0.25
    """
    score = 0.0
    if ratio >= ratio_threshold:
        score += 0.55
    elif ratio >= ratio_threshold / 4:
        score += 0.25
    if has_explain:
        score += 0.15
    if has_dropped_index:
        score += 0.25
    if no_index_used > 0:
        score += 0.05
    if full_scans > 0:
        score += 0.05
    return round(min(score, 1.0), 2)
