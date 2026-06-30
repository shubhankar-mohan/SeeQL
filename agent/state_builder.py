"""
Structured State Builder — bridge between raw metrics and LLM reasoning.

Queries the SQLite monitoring DB and produces a structured report with:
    1. Current State (last 5 minutes)
    2. Changes Since Last Analysis
    3. Historical Context (7-day comparison)

The output is both a structured dict (for programmatic use) and a
Markdown string (for the LLM prompt).
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent import queries as Q
from config import get_config
from storage.connection import get_mon_reader

logger = logging.getLogger(__name__)


@dataclass
class StateReport:
    """Structured state report for the LLM agent."""
    generated_at: str = ""
    current_state: dict = field(default_factory=dict)
    changes: dict = field(default_factory=dict)
    historical: dict = field(default_factory=dict)
    incidents: list = field(default_factory=list)

    def to_markdown(self) -> str:
        return _render_markdown(self)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "current_state": self.current_state,
            "changes": self.changes,
            "historical": self.historical,
            "incidents": self.incidents,
        }


def build_state_report(since: str | None = None, server_id: str | None = None) -> StateReport:
    """
    Build a structured state report from the monitoring database.

    Args:
        since: ISO timestamp for "changes since". Defaults to last analysis time or 1h ago.
        server_id: Filter data to this server. None = default server.
    """
    if server_id is None:
        from config.server_registry import get_server_registry
        server_id = get_server_registry().get_default_server_id()
    config = get_config().get("agent", {}).get("state_builder", {})
    regression_threshold = config.get("regression_threshold", 3.0)
    long_txn_sec = config.get("long_transaction_sec", 30)

    report = StateReport(generated_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat())

    with get_mon_reader() as conn:
        # ---------------------------------------------------------------
        # 1. Current State
        # ---------------------------------------------------------------
        report.current_state = _build_current_state(conn, long_txn_sec, server_id)

        # ---------------------------------------------------------------
        # 2. Changes Since Last Analysis
        # ---------------------------------------------------------------
        if since is None:
            since = _get_last_analysis_time(conn, server_id)
        report.changes = _build_changes(conn, since, regression_threshold, server_id)

        # ---------------------------------------------------------------
        # 3. Historical Context
        # ---------------------------------------------------------------
        report.historical = _build_historical(conn, report.changes.get("regressions", []), server_id)

        # ---------------------------------------------------------------
        # 4. Recent Incidents (Phase 1.12) — unresolved incidents in the
        # last 24h. Gives the LLM a pointer to go deeper via replay tools.
        # ---------------------------------------------------------------
        report.incidents = _build_incidents(conn, server_id)

    # Anomaly detection (runs outside the conn context)
    try:
        from alerting.anomaly import detect_anomalies, METRIC_CONFIGS
        anomalies = detect_anomalies(server_id=server_id)
        report.current_state["anomalies"] = [
            {
                "metric": a.metric,
                "description": METRIC_CONFIGS.get(a.metric, {}).get("description", a.metric),
                "current": a.current,
                "baseline_mean": a.baseline_mean,
                "z_score": a.z_score,
                "pct_change": a.pct_change,
                "direction": a.direction,
                "severity": a.severity,
            }
            for a in anomalies
        ]
    except Exception as e:
        logger.debug(f"Anomaly detection in state builder failed: {e}")

    return report


def _build_current_state(conn, long_txn_sec: int, server_id: str) -> dict:
    state = {}
    sid = server_id

    # Top queries by total time
    rows = conn.execute(Q.TOP_QUERIES_BY_TIME, (sid, sid, 10)).fetchall()
    state["top_queries"] = [dict(r) for r in rows]

    # Top queries by examined/sent ratio (missing index signals)
    rows = conn.execute(Q.TOP_QUERIES_BY_RATIO, (sid, sid, 5)).fetchall()
    state["missing_index_candidates"] = [dict(r) for r in rows]

    # Lock waits
    row = conn.execute(Q.CURRENT_LOCK_WAITS, ('-5 minutes', sid)).fetchone()
    state["lock_waits"] = dict(row) if row else {"lock_count": 0, "max_wait_sec": 0}

    # Buffer pool — hit_ratio is computed from cumulative global_status_snapshots
    # counters, not from buffer_pool_snapshots.hit_ratio which is an unreliable
    # instantaneous sample. See api.query_helpers.latest_hit_ratio_pct.
    row = conn.execute(Q.CURRENT_BUFFER_POOL, (sid, sid)).fetchone()
    bp = dict(row) if row else {}
    from api.query_helpers import latest_hit_ratio_pct
    hit_pct = latest_hit_ratio_pct(server_id=sid, conn=conn)
    # Store as a fraction in [0.0, 1.0] so state report renders "hit_ratio=0.9920"
    bp["hit_ratio"] = (hit_pct / 100.0) if hit_pct is not None else None
    state["buffer_pool"] = bp

    # Threads
    rows = conn.execute(Q.CURRENT_THREADS, (sid, sid)).fetchall()
    state["threads"] = {r["variable_name"]: r["raw_value"] for r in rows}

    # QPS
    row = conn.execute(Q.CURRENT_QPS, (sid,)).fetchone()
    state["qps"] = row["per_second"] if row and row["per_second"] else 0

    # Long transactions
    rows = conn.execute(Q.LONG_TRANSACTIONS, (sid, sid, long_txn_sec)).fetchall()
    state["long_transactions"] = [dict(r) for r in rows]

    # GCP metrics
    rows = conn.execute(Q.CURRENT_GCP_METRICS, (sid, sid)).fetchall()
    state["gcp_metrics"] = {r["metric_name"]: r["value"] for r in rows}

    return state


def _build_changes(conn, since: str, regression_threshold: float, server_id: str) -> dict:
    changes = {}
    sid = server_id

    # DDL changes
    rows = conn.execute(Q.RECENT_DDL_CHANGES, (since, sid)).fetchall()
    changes["ddl_changes"] = [dict(r) for r in rows]

    # New query fingerprints
    rows = conn.execute(Q.NEW_QUERY_FINGERPRINTS, (sid, sid)).fetchall()
    changes["new_queries"] = [dict(r) for r in rows]

    # Query regressions
    rows = conn.execute(Q.QUERY_REGRESSIONS, (sid, sid, regression_threshold)).fetchall()
    changes["regressions"] = [dict(r) for r in rows]

    # Recent deadlocks
    rows = conn.execute(Q.RECENT_DEADLOCKS, (since, sid)).fetchall()
    deadlocks = []
    for r in rows:
        dl = {"snapshot_time": r["snapshot_time"]}
        if r["parsed_json"]:
            try:
                dl["details"] = json.loads(r["parsed_json"])
            except json.JSONDecodeError:
                pass
        deadlocks.append(dl)
    changes["deadlocks"] = deadlocks

    return changes


def _build_historical(conn, regressions: list, server_id: str) -> dict:
    hist = {}
    sid = server_id

    # Baseline Threads_running (same hour, 7 days ago)
    row = conn.execute(Q.BASELINE_THREADS_RUNNING, (sid,)).fetchone()
    hist["baseline_threads_running"] = row["avg_value"] if row and row["avg_value"] else None

    # Baseline QPS
    row = conn.execute(Q.BASELINE_QPS, (sid,)).fetchone()
    hist["baseline_qps"] = row["avg_qps"] if row and row["avg_qps"] else None

    # Peak threads in last 24h
    row = conn.execute(Q.PEAK_THREADS_24H, (sid,)).fetchone()
    hist["peak_threads_24h"] = row["peak_threads"] if row and row["peak_threads"] else None

    # Longest lock wait in last 24h
    row = conn.execute(Q.LONGEST_LOCK_24H, (sid,)).fetchone()
    if row and row["longest_wait_sec"]:
        hist["lock_24h"] = {
            "longest_wait_sec": row["longest_wait_sec"],
            "total_events": row["total_lock_events"],
        }

    # Previous recommendations (last 24h) — prevents duplicate recommendations
    rows = conn.execute(Q.PREVIOUS_RECOMMENDATIONS, (sid,)).fetchall()
    prev_recs = []
    for r in rows:
        recs_raw = r["recommendations"]
        if recs_raw:
            try:
                import json
                recs_text = json.loads(recs_raw)
            except (json.JSONDecodeError, TypeError):
                recs_text = recs_raw
            prev_recs.append({
                "analyzed_at": r["analyzed_at"],
                "severity": r["severity"],
                "recommendations": recs_text[:500] if isinstance(recs_text, str) else str(recs_text)[:500],
            })
    hist["previous_recommendations"] = prev_recs

    # 30-day trends for regressed queries
    regression_trends = []
    for reg in regressions[:5]:  # limit to top 5
        digest = reg.get("digest")
        if not digest:
            continue
        rows = conn.execute(Q.QUERY_30D_TREND, (digest, sid)).fetchall()
        if rows:
            regression_trends.append({
                "digest": digest,
                "digest_text": reg.get("digest_text", ""),
                "daily_avgs": [{"day": r["day"], "avg": r["daily_avg"]} for r in rows],
            })
    hist["regression_trends"] = regression_trends

    return hist


def _get_last_analysis_time(conn, server_id: str = "default") -> str:
    """Get timestamp of last agent analysis, or 1 hour ago."""
    row = conn.execute(Q.LAST_ANALYSIS_TIME, (server_id,)).fetchone()
    if row and row["last_at"]:
        return row["last_at"]
    from datetime import timedelta
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()


def _build_incidents(conn, server_id: str) -> list[dict]:
    """Query unresolved incident windows from the last 24h for this server.

    Returns a list of dicts (with `involved_metrics` already JSON-decoded)
    ordered oldest-first so the LLM sees the natural chronology.
    """
    try:
        rows = conn.execute(
            """
            SELECT id, start_time, end_time, severity, involved_metrics,
                   event_count, status
            FROM incident_windows
            WHERE server_id = ?
              AND status != 'resolved'
              AND datetime(start_time) >= datetime('now', '-24 hours')
            ORDER BY start_time ASC
            """,
            (server_id,),
        ).fetchall()
    except Exception as e:
        logger.debug(f"Incident lookup failed: {e}")
        return []

    out = []
    for r in rows:
        try:
            metrics = json.loads(r["involved_metrics"])
        except (json.JSONDecodeError, TypeError):
            metrics = []
        out.append({
            "id": r["id"],
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "severity": r["severity"],
            "involved_metrics": metrics,
            "event_count": r["event_count"],
            "status": r["status"],
        })
    return out


def _render_markdown(report: StateReport) -> str:
    """Render the state report as Markdown for the LLM prompt."""
    lines = []
    cs = report.current_state
    ch = report.changes
    hist = report.historical
    incidents = report.incidents or []

    lines.append("## Current State")
    lines.append("")

    # Top queries
    if cs.get("top_queries"):
        lines.append("### Top Queries by Total Time")
        for i, q in enumerate(cs["top_queries"][:10], 1):
            lines.append(
                f"{i}. digest=`{q.get('digest', '?')}` schema=`{q.get('schema_name', '?')}` "
                f"`{(q.get('digest_text') or '?')[:80]}` — "
                f"total={q.get('total_time_sec', 0):.2f}s, "
                f"avg={q.get('avg_time_sec', 0):.4f}s, "
                f"execs={q.get('exec_count', 0)}, "
                f"rows_examined={q.get('rows_examined', 0)}"
            )
        lines.append("")

    # Missing index candidates
    if cs.get("missing_index_candidates"):
        lines.append("### Missing Index Candidates (high rows_examined/rows_sent)")
        for q in cs["missing_index_candidates"]:
            lines.append(
                f"- digest=`{q.get('digest', '?')}` schema=`{q.get('schema_name', '?')}` "
                f"`{(q.get('digest_text') or '?')[:80]}` — "
                f"ratio={q.get('ratio', 0):.0f}x, "
                f"examined={q.get('rows_examined', 0)}, sent={q.get('rows_sent', 0)}"
            )
        lines.append("")

    # Lock waits
    lw = cs.get("lock_waits", {})
    lock_count = lw.get("lock_count", 0)
    if lock_count > 0:
        lines.append(f"### Lock Waits: {lock_count} active, max wait {lw.get('max_wait_sec', 0)}s")
    else:
        lines.append("### Lock Waits: none")
    lines.append("")

    # Buffer pool
    bp = cs.get("buffer_pool", {})
    if bp:
        hit = bp.get("hit_ratio")
        hit_str = f"{hit:.4f}" if hit else "N/A"
        lines.append(f"### Buffer Pool: hit_ratio={hit_str}, dirty_pages={bp.get('dirty_pages', 0)}")
    lines.append("")

    # Threads
    threads = cs.get("threads", {})
    running = threads.get("Threads_running", "?")
    connected = threads.get("Threads_connected", "?")
    qps = cs.get("qps", 0)
    lines.append(f"### Server: Threads_running={running}, Threads_connected={connected}, QPS={qps:.1f}")
    lines.append("")

    # GCP metrics
    gcp = cs.get("gcp_metrics", {})
    if gcp:
        cpu = gcp.get("cpu_utilization")
        mem = gcp.get("memory_utilization")
        disk = gcp.get("disk_utilization")
        lines.append(
            f"### Infrastructure: CPU={_pct(cpu)}, Memory={_pct(mem)}, Disk={_pct(disk)}"
        )
        lines.append("")

    # Long transactions
    long_txns = cs.get("long_transactions", [])
    if long_txns:
        lines.append(f"### Long Transactions: {len(long_txns)} active")
        for t in long_txns[:5]:
            lines.append(
                f"- trx={t.get('trx_id')}, pid={t.get('pid', '?')}, age={t.get('age_sec')}s, "
                f"rows_locked={t.get('rows_locked', 0)}, rows_modified={t.get('rows_modified', 0)}, "
                f"query=`{(t.get('trx_query') or '?')[:60]}`"
            )
        lines.append("")

    # Anomalies
    anomalies = cs.get("anomalies", [])
    if anomalies:
        lines.append(f"### Statistical Anomalies: {len(anomalies)} detected")
        for a in anomalies:
            dir_word = "above" if a["direction"] == "high" else "below"
            lines.append(
                f"- **{a['description']}**: {a['current']:.4f} "
                f"({a['pct_change']:+.0f}% {dir_word} baseline mean={a['baseline_mean']:.4f}, "
                f"z={a['z_score']:.1f}) [{a['severity']}]"
            )
        lines.append("")

    # Recent Incidents (Phase 1.12)
    if incidents:
        lines.append(f"### Recent Incidents (last 24h, unresolved): {len(incidents)}")
        for inc in incidents:
            metrics_str = ", ".join(inc["involved_metrics"]) or "—"
            lines.append(
                f"- #{inc['id']} [{inc['severity']}] "
                f"{inc['start_time']} → {inc['end_time']} "
                f"({inc['event_count']} events, metrics: {metrics_str}) "
                f"[{inc['status']}]"
            )
        lines.append(
            "> Use `python main.py replay --incident <id>` for a full postmortem."
        )
        lines.append("")
    else:
        lines.append("### Recent Incidents: none unresolved in the last 24h")
        lines.append("")

    # --- Changes ---
    lines.append("## Changes Since Last Analysis")
    lines.append("")

    ddl = ch.get("ddl_changes", [])
    if ddl:
        lines.append(f"### DDL Changes: {len(ddl)}")
        for d in ddl:
            lines.append(f"- `{d['table_schema']}`.`{d['table_name']}` — {d['change_type']} change at {d['detected_at']}")
        lines.append("")

    new_q = ch.get("new_queries", [])
    if new_q:
        lines.append(f"### New Query Fingerprints: {len(new_q)}")
        for q in new_q[:10]:
            lines.append(
                f"- digest=`{q.get('digest', '?')}` schema=`{q.get('schema_name', '?')}` "
                f"`{(q.get('digest_text') or '?')[:80]}`"
            )
        lines.append("")

    regs = ch.get("regressions", [])
    if regs:
        lines.append(f"### Query Regressions: {len(regs)}")
        for r in regs:
            lines.append(
                f"- digest=`{r.get('digest', '?')}` schema=`{r.get('schema_name', '?')}` "
                f"`{(r.get('digest_text') or '?')[:60]}` — "
                f"was {r.get('baseline_avg', 0):.4f}s, now {r.get('recent_avg', 0):.4f}s "
                f"({r.get('regression_factor', 0):.1f}x slower)"
            )
        lines.append("")

    deadlocks = ch.get("deadlocks", [])
    if deadlocks:
        lines.append(f"### Deadlocks Detected: {len(deadlocks)}")
        for dl in deadlocks:
            details = dl.get("details", {})
            tables = details.get("tables_involved", [])
            lines.append(f"- at {dl['snapshot_time']}, tables: {', '.join(tables) if tables else 'unknown'}")
        lines.append("")

    if not ddl and not new_q and not regs and not deadlocks:
        lines.append("No significant changes detected.")
        lines.append("")

    # --- Historical ---
    lines.append("## Historical Context")
    lines.append("")

    baseline_tr = hist.get("baseline_threads_running")
    if baseline_tr is not None:
        current_tr = threads.get("Threads_running", "?")
        lines.append(f"- Threads_running now: {current_tr}, same hour last week avg: {baseline_tr:.1f}")

    baseline_qps = hist.get("baseline_qps")
    if baseline_qps is not None:
        lines.append(f"- QPS now: {qps:.1f}, same hour last week avg: {baseline_qps:.1f}")

    peak_24h = hist.get("peak_threads_24h")
    if peak_24h is not None:
        lines.append(f"- Peak Threads_running (24h): {peak_24h:.0f}")

    lock_24h = hist.get("lock_24h")
    if lock_24h:
        lines.append(
            f"- Lock waits (24h): {lock_24h['total_events']} events, "
            f"longest wait {lock_24h['longest_wait_sec']:.0f}s"
        )

    lines.append("")

    # Previous recommendations — prevents duplicate suggestions
    prev_recs = hist.get("previous_recommendations", [])
    if prev_recs:
        lines.append("### Previous Recommendations (last 24h)")
        lines.append("Do NOT repeat these unless the issue persists and was not acted on.")
        for pr in prev_recs:
            lines.append(
                f"- [{pr['severity']}] at {pr['analyzed_at']}: "
                f"{pr['recommendations'][:200]}"
            )
        lines.append("")

    trends = hist.get("regression_trends", [])
    if trends:
        lines.append("### Regression 30-Day Trends")
        for t in trends:
            avgs = t.get("daily_avgs", [])
            if len(avgs) >= 2:
                first = avgs[0]["avg"]
                last = avgs[-1]["avg"]
                lines.append(
                    f"- digest=`{t.get('digest', '?')}` "
                    f"`{(t.get('digest_text') or '?')[:60]}`: "
                    f"30d ago={first:.4f}s → today={last:.4f}s"
                )
        lines.append("")

    # --- Tool Reference Table ---
    # Structured digest/schema/table lookup so the LLM doesn't have to parse inline
    all_queries = cs.get("top_queries", []) + cs.get("missing_index_candidates", [])
    regs = ch.get("regressions", [])
    seen_digests = set()
    ref_rows = []
    for q in all_queries + regs:
        d = q.get("digest", "")
        if d and d not in seen_digests:
            seen_digests.add(d)
            # Extract table name from digest_text
            dt = q.get("digest_text", "")
            table = _extract_table_name(dt)
            issue = ""
            if q in regs:
                issue = f"regression {q.get('regression_factor', 0):.1f}x"
            elif q in cs.get("missing_index_candidates", []):
                issue = f"missing index (ratio {q.get('ratio', 0):.0f}x)"
            else:
                issue = f"slow (total {q.get('total_time_sec', 0):.2f}s)"
            ref_rows.append((d, q.get("schema_name", "?"), table, issue))

    if ref_rows:
        lines.append("## Tool Reference")
        lines.append("| digest | schema | table | issue |")
        lines.append("|--------|--------|-------|-------|")
        for digest, schema, table, issue in ref_rows[:15]:
            lines.append(f"| `{digest[:16]}...` | {schema} | {table} | {issue} |")
        lines.append("")

    lines.append("")
    return "\n".join(lines)


def _pct(val) -> str:
    if val is None:
        return "N/A"
    return f"{val * 100:.1f}%"


import re as _re
_TABLE_RE = _re.compile(r'(?:FROM|JOIN|UPDATE|INTO)\s+`?(\w+)`?', _re.IGNORECASE)

def _extract_table_name(sql: str) -> str:
    """Best-effort extraction of the main table name from SQL text."""
    m = _TABLE_RE.search(sql or "")
    return m.group(1) if m else "?"
