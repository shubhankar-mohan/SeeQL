"""Remediation knowledge base + index suggestion engine."""

import json
import re
import logging
from api.query_helpers import query_rows, query_single

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Remediation knowledge base
# ---------------------------------------------------------------------------

REMEDIATION = {
    "lock_wait": {
        "diagnosis": "Transactions waiting for row locks held by other transactions. Can cascade into connection exhaustion.",
        "steps": [
            "Identify the blocking PID below.",
            "Check if blocker is idle or stuck — kill if safe: KILL <blocking_pid>;",
            "Reduce innodb_lock_wait_timeout to fail fast.",
            "Check index coverage — full table scans hold more locks.",
        ],
        "queries": [
            "KILL <blocking_pid>;",
            "SET SESSION innodb_lock_wait_timeout = 5;",
        ],
    },
    "long_transaction": {
        "diagnosis": "Open > 60s. Holds locks, prevents InnoDB purge, increases replication lag.",
        "steps": [
            "Check if actively running or idle (SLEEP = app bug, missing COMMIT).",
            "If idle and safe: KILL <pid>;",
            "Check app connection pool — ensure auto-commit or timeout.",
        ],
        "queries": [
            "KILL <pid>;",
            "SET GLOBAL innodb_lock_wait_timeout = 10;",
        ],
    },
    "ddl_change": {
        "diagnosis": "Schema change detected. Can invalidate query plans and cause full table scans.",
        "steps": [
            "Review before/after diff.",
            "Run EXPLAIN on queries hitting the affected table.",
            "Check if dropped/changed index causes regressions.",
        ],
    },
    "query_regression": {
        "diagnosis": "Query running significantly slower than baseline. Likely: plan change, data growth, or lock contention.",
        "steps": [
            "Check EXPLAIN plan for type=ALL (full scan).",
            "Look at rows_examined/rows_sent ratio.",
            "Check for recent DDL on referenced tables.",
        ],
    },
    "anomaly": {
        "diagnosis": "Metric deviated beyond statistical baseline. May indicate workload change or emerging problem.",
        "steps": [
            "Check if deviation correlates with a deployment or batch job.",
            "Review Server page for detailed charts.",
            "If expected (load test), can be ignored.",
        ],
    },
}


def get_remediation(alert_type: str) -> dict:
    """Get remediation info for an alert type."""
    return REMEDIATION.get(alert_type, {})


# ---------------------------------------------------------------------------
# Index suggestion engine
# ---------------------------------------------------------------------------

def suggest_indexes() -> list[dict]:
    """Analyze queries and EXPLAIN captures to suggest indexes.

    Returns list of dicts:
        {table, columns, query_digest, query_text, reason, impact, create_sql,
         exec_count, avg_time, rows_examined, rows_sent, ratio, last_seen}
    """
    suggestions = []
    seen_tables = set()  # avoid duplicate suggestions per table+columns

    # 1. Queries with high exam/sent ratio + full scans or no_index_used
    problem_queries = query_rows("""
        SELECT digest, digest_text, schema_name,
               SUM(exec_count) as exec_count,
               AVG(avg_time_sec) as avg_time_sec,
               SUM(total_time_sec) as total_time_sec,
               SUM(rows_examined) as rows_examined,
               SUM(rows_sent) as rows_sent,
               SUM(full_scans) as full_scans,
               SUM(no_index_used) as no_index_used,
               MAX(last_seen) as last_seen
        FROM query_digest_snapshots
        WHERE snapshot_time >= datetime('now', '-24 hours')
        GROUP BY digest
        HAVING (SUM(full_scans) > 0 OR SUM(no_index_used) > 0
                OR (SUM(rows_sent) > 0 AND CAST(SUM(rows_examined) AS REAL) / SUM(rows_sent) > 100))
        ORDER BY SUM(total_time_sec) DESC
        LIMIT 20
    """)

    for q in problem_queries:
        table, columns = _extract_table_and_where_columns(q["digest_text"])
        if not table:
            continue

        ratio = q["rows_examined"] / q["rows_sent"] if q["rows_sent"] and q["rows_sent"] > 0 else 0
        key = f"{table}:{','.join(columns)}" if columns else table

        # Check EXPLAIN for this query
        explain = query_single("""
            SELECT explain_json FROM explain_captures
            WHERE digest = ? ORDER BY captured_at DESC LIMIT 1
        """, (q["digest"],))

        explain_detail = _parse_explain_for_index(explain)

        if columns and key not in seen_tables:
            schema = q.get("schema_name") or _guess_schema(table)
            full_table = f"`{schema}`.`{table}`" if schema else f"`{table}`"
            idx_name = f"idx_{'_'.join(columns[:3])}"
            col_list = ", ".join(f"`{c}`" for c in columns)
            create_sql = f"ALTER TABLE {full_table} ADD INDEX {idx_name} ({col_list});"

            reason_parts = []
            if q["full_scans"] and q["full_scans"] > 0:
                reason_parts.append(f"full table scan ({q['full_scans']:,}x)")
            if ratio > 100:
                reason_parts.append(f"exam/sent ratio {ratio:,.0f}x")
            if explain_detail:
                reason_parts.append(explain_detail)
            reason = "; ".join(reason_parts) or "no index used"

            suggestions.append({
                "table": f"{schema}.{table}" if schema else table,
                "columns": columns,
                "query_digest": q["digest"],
                "query_text": q["digest_text"][:150] if q["digest_text"] else "",
                "reason": reason,
                "impact": f"{q['total_time_sec']:.1f}s total time, {q['exec_count']:,} execs",
                "create_sql": create_sql,
                "exec_count": q["exec_count"],
                "avg_time": q["avg_time_sec"],
                "rows_examined": q["rows_examined"],
                "rows_sent": q["rows_sent"],
                "ratio": ratio,
                "last_seen": q.get("last_seen", ""),
            })
            seen_tables.add(key)

        elif not columns and key not in seen_tables:
            # Can't determine columns but query is problematic
            suggestions.append({
                "table": table,
                "columns": [],
                "query_digest": q["digest"],
                "query_text": q["digest_text"][:150] if q["digest_text"] else "",
                "reason": f"full scan, {ratio:,.0f}x ratio" + (f"; {explain_detail}" if explain_detail else ""),
                "impact": f"{q['total_time_sec']:.1f}s total time, {q['exec_count']:,} execs",
                "create_sql": "",
                "exec_count": q["exec_count"],
                "avg_time": q["avg_time_sec"],
                "rows_examined": q["rows_examined"],
                "rows_sent": q["rows_sent"],
                "ratio": ratio,
                "last_seen": q.get("last_seen", ""),
            })
            seen_tables.add(key)

    # 2. EXPLAIN captures with access_type=ALL that weren't caught above
    full_scan_explains = query_rows("""
        SELECT digest, digest_text, schema_name, explain_json,
               total_time_sec, avg_time_sec, exec_count
        FROM explain_captures
        WHERE explain_json LIKE '%"access_type": "ALL"%'
           OR explain_json LIKE '%"access_type":"ALL"%'
        ORDER BY total_time_sec DESC
        LIMIT 10
    """)

    for ec in full_scan_explains:
        table, columns = _extract_table_and_where_columns(ec["digest_text"])
        if not table:
            continue
        key = f"{table}:{','.join(columns)}" if columns else table
        if key in seen_tables:
            continue

        if columns:
            schema = ec.get("schema_name") or _guess_schema(table)
            full_table = f"`{schema}`.`{table}`" if schema else f"`{table}`"
            idx_name = f"idx_{'_'.join(columns[:3])}"
            col_list = ", ".join(f"`{c}`" for c in columns)
            create_sql = f"ALTER TABLE {full_table} ADD INDEX {idx_name} ({col_list});"

            suggestions.append({
                "table": f"{schema}.{table}" if schema else table,
                "columns": columns,
                "query_digest": ec["digest"],
                "query_text": ec["digest_text"][:150] if ec["digest_text"] else "",
                "reason": "EXPLAIN shows full table scan (type=ALL)",
                "impact": f"{ec['total_time_sec']:.1f}s total, {ec['exec_count']:,} execs",
                "create_sql": create_sql,
                "exec_count": ec["exec_count"],
                "avg_time": ec["avg_time_sec"],
                "rows_examined": 0,
                "rows_sent": 0,
                "ratio": 0,
                "last_seen": "",
            })
            seen_tables.add(key)

    return suggestions


def get_queries_to_optimize() -> list[dict]:
    """Get top queries that need optimization, ranked by total time wasted.

    Returns list of dicts with query info + specific recommendations.
    """
    results = []

    queries = query_rows("""
        SELECT digest, digest_text, schema_name,
               SUM(exec_count) as exec_count,
               AVG(avg_time_sec) as avg_time_sec,
               SUM(total_time_sec) as total_time_sec,
               SUM(rows_examined) as rows_examined,
               SUM(rows_sent) as rows_sent,
               SUM(full_scans) as full_scans,
               SUM(no_index_used) as no_index_used,
               MAX(last_seen) as last_seen,
               MIN(snapshot_time) as first_seen
        FROM query_digest_snapshots
        WHERE snapshot_time >= datetime('now', '-24 hours')
        GROUP BY digest
        ORDER BY SUM(total_time_sec) DESC
        LIMIT 15
    """)

    for q in queries:
        ratio = q["rows_examined"] / q["rows_sent"] if q["rows_sent"] and q["rows_sent"] > 0 else 0
        recommendations = []

        # Check EXPLAIN
        explain = query_single("""
            SELECT explain_json FROM explain_captures
            WHERE digest = ? ORDER BY captured_at DESC LIMIT 1
        """, (q["digest"],))

        has_full_scan = False
        if explain and explain.get("explain_json"):
            ej = explain["explain_json"]
            if '"ALL"' in ej:
                has_full_scan = True
                recommendations.append("EXPLAIN shows full table scan — add index on WHERE/JOIN columns")
            if '"filesort"' in ej.lower() or '"using_filesort": true' in ej:
                recommendations.append("Filesort detected — consider index covering ORDER BY")
            if '"using_temporary_table": true' in ej:
                recommendations.append("Temp table used — optimize GROUP BY/DISTINCT with covering index")

        if ratio > 1000:
            recommendations.append(f"Examining {ratio:,.0f}x more rows than returned — almost certainly missing an index")
        elif ratio > 100:
            recommendations.append(f"High scan ratio ({ratio:,.0f}x) — likely needs better index selectivity")

        if q["full_scans"] and q["full_scans"] > 0 and not has_full_scan:
            recommendations.append(f"Full table scan detected {q['full_scans']:,} times")

        if q["avg_time_sec"] and q["avg_time_sec"] > 1.0:
            recommendations.append(f"Avg {q['avg_time_sec']:.2f}s is slow — consider query rewrite or caching")

        # Check regression
        regression = query_single("""
            WITH recent AS (
                SELECT AVG(avg_time_sec) as recent_avg
                FROM query_digest_snapshots
                WHERE digest = ? AND snapshot_time >= datetime('now', '-1 hour')
            ),
            baseline AS (
                SELECT AVG(avg_time_sec) as baseline_avg
                FROM query_digest_snapshots
                WHERE digest = ? AND snapshot_time BETWEEN datetime('now', '-7 days') AND datetime('now', '-1 hour')
            )
            SELECT recent_avg, baseline_avg,
                   recent_avg / NULLIF(baseline_avg, 0) as factor
            FROM recent, baseline
            WHERE baseline_avg > 0
        """, (q["digest"], q["digest"]))

        if regression and regression.get("factor") and regression["factor"] >= 3.0:
            recommendations.append(
                f"REGRESSION: {regression['factor']:.1f}x slower than 7-day baseline "
                f"({regression['baseline_avg']:.4f}s -> {regression['recent_avg']:.4f}s)"
            )

        # Get frequency stats
        freq = query_single("""
            SELECT
                SUM(CASE WHEN snapshot_time >= datetime('now', '-1 hour') THEN exec_count ELSE 0 END) as last_1h,
                SUM(CASE WHEN snapshot_time >= datetime('now', '-24 hours') THEN exec_count ELSE 0 END) as last_24h,
                SUM(exec_count) as last_7d
            FROM query_digest_snapshots
            WHERE digest = ?
        """, (q["digest"],))

        # Get index suggestion
        table, columns = _extract_table_and_where_columns(q["digest_text"])
        index_sql = ""
        if columns and table:
            schema = q.get("schema_name") or _guess_schema(table)
            full_table = f"`{schema}`.`{table}`" if schema else f"`{table}`"
            idx_name = f"idx_{'_'.join(columns[:3])}"
            col_list = ", ".join(f"`{c}`" for c in columns)
            index_sql = f"ALTER TABLE {full_table} ADD INDEX {idx_name} ({col_list});"

        if recommendations:
            results.append({
                "digest": q["digest"],
                "query_text": q["digest_text"][:200] if q["digest_text"] else "",
                "schema_name": q.get("schema_name", ""),
                "exec_count": q["exec_count"],
                "avg_time": q["avg_time_sec"],
                "total_time": q["total_time_sec"],
                "rows_examined": q["rows_examined"],
                "rows_sent": q["rows_sent"],
                "ratio": ratio,
                "full_scans": q["full_scans"] or 0,
                "recommendations": recommendations,
                "index_sql": index_sql,
                "last_seen": q.get("last_seen", ""),
                "first_seen": q.get("first_seen", ""),
                "freq_1h": freq["last_1h"] if freq else 0,
                "freq_24h": freq["last_24h"] if freq else 0,
                "freq_7d": freq["last_7d"] if freq else 0,
            })

    return results


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _extract_table_and_where_columns(digest_text: str) -> tuple[str, list[str]]:
    """Extract primary table and WHERE clause columns from a SQL digest.

    Works with parameterized queries (? placeholders).
    Returns (table_name, [column_names]).
    """
    if not digest_text:
        return ("", [])

    sql = digest_text.strip().upper()
    table = ""
    columns = []

    # Extract table name
    # FROM `schema`.`table` or FROM table
    from_match = re.search(
        r'FROM\s+`?(\w+)`?\.`?(\w+)`?',
        digest_text, re.IGNORECASE
    )
    if from_match:
        table = from_match.group(2)
    else:
        from_match = re.search(
            r'FROM\s+`?(\w+)`?',
            digest_text, re.IGNORECASE
        )
        if from_match:
            table = from_match.group(1)

    # Skip if it's a system/internal query
    if table.lower() in ('dual', 'information_schema', 'performance_schema', 'sys'):
        return ("", [])

    # Extract WHERE columns
    where_match = re.search(r'WHERE\s+(.+?)(?:ORDER|GROUP|LIMIT|HAVING|UNION|$)',
                            digest_text, re.IGNORECASE | re.DOTALL)
    if where_match:
        where_clause = where_match.group(1)
        # Find column names before = ? or IN (?) or LIKE ? or BETWEEN ? AND ?
        col_matches = re.findall(
            r'`?(\w+)`?\s*(?:=|!=|<>|>=?|<=?|IN|LIKE|BETWEEN|IS)\s',
            where_clause, re.IGNORECASE
        )
        # Filter out SQL keywords and common false positives
        skip = {'AND', 'OR', 'NOT', 'NULL', 'TRUE', 'FALSE', 'IN', 'LIKE',
                'BETWEEN', 'IS', 'EXISTS', 'SELECT', 'FROM', 'WHERE', 'CASE',
                'WHEN', 'THEN', 'ELSE', 'END'}
        columns = []
        seen = set()
        for c in col_matches:
            cu = c.upper()
            if cu not in skip and c not in seen and len(c) > 1:
                columns.append(c)
                seen.add(c)

    return (table, columns[:5])  # limit to 5 columns max


def _guess_schema(table: str) -> str:
    """Try to find schema for a table from schema_snapshots."""
    row = query_single("""
        SELECT table_schema FROM schema_snapshots
        WHERE table_name = ?
        ORDER BY snapshot_time DESC LIMIT 1
    """, (table,))
    return row["table_schema"] if row else ""


def _parse_explain_for_index(explain: dict | None) -> str:
    """Parse EXPLAIN and return a short description of the issue."""
    if not explain or not explain.get("explain_json"):
        return ""
    try:
        data = json.loads(explain["explain_json"])
    except (json.JSONDecodeError, TypeError):
        return ""

    issues = []
    rows = []
    if isinstance(data, dict):
        qb = data.get("query_block", data)
        table = qb.get("table")
        if table:
            rows = [table]
        ordering = qb.get("ordering_operation", qb)
        for nl in ordering.get("nested_loop", []):
            if isinstance(nl, dict) and "table" in nl:
                rows.append(nl["table"])

    for row in rows:
        at = row.get("access_type", "")
        tn = row.get("table_name", "")
        rps = row.get("rows_examined_per_scan", 0)
        if at == "ALL":
            issues.append(f"EXPLAIN: full scan on {tn} (~{rps:,} rows)")
        if row.get("using_filesort"):
            issues.append("filesort")
        if row.get("using_temporary_table"):
            issues.append("temp table")

    return "; ".join(issues)
