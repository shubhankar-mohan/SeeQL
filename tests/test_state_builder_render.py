"""
Golden test for agent/state_builder.py's Markdown renderer (P4-16 debt).

`_render_markdown` produces the ENTIRE input the LLM agent reasons over —
there is no other view of the monitoring data the agent sees. This test
seeds one deterministic row per section of the state report and pins the
exact rendered text, so a future change to the renderer (a tweaked label, a
dropped format spec, a silently-changed number) fails a test instead of
shipping unnoticed.

Also covers P1c-11 directly: a buffer-pool hit_ratio of exactly 0.0 (every
read a physical miss — the worst possible signal) must render "0.0000", not
the old falsy-check's "N/A".
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import config as config_module
from agent.state_builder import StateReport, _render_markdown, build_state_report
from alerting.anomaly import AnomalyResult
from storage.connection import reset_connections


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def state_builder_db(mon_db):
    """Wire config at the mon_db fixture's temp path.

    mon_db itself restores config to its pre-fixture value once schema +
    migrations are applied (see conftest.py), so callers that need the DB
    wired up for the duration of the test re-point config here — same
    pattern as test_missing_index_correlation.py's `mon_db_ctx`.
    """
    conn, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
    }
    reset_connections()
    yield conn, db_path
    config_module._config = prev
    reset_connections()


class TestHitRatioZeroRendersHonestly:
    """P1c-11: `f"{hit:.4f}" if hit else "N/A"` treats a hit_ratio of exactly
    0.0 as falsy, so a catastrophic all-physical-reads buffer pool rendered
    as "N/A" — indistinguishable from "we have no data at all". Only an
    actually-missing (None) hit_ratio should render N/A.

    These construct a StateReport directly (no DB) since `_render_markdown`
    is a pure function of the report's plain dicts — this keeps the edge
    case fast and independent of the anomaly engine's wall-clock-dependent
    baseline logic, which is exercised by tests/test_anomaly.py instead.
    """

    def test_hit_ratio_zero_renders_as_number_not_na(self):
        report = StateReport(current_state={"buffer_pool": {"hit_ratio": 0.0, "dirty_pages": 5}})
        md = _render_markdown(report)
        assert "hit_ratio=0.0000" in md
        assert "hit_ratio=N/A" not in md

    def test_hit_ratio_missing_still_renders_na(self):
        """None (no data at all) is the only case that should render N/A."""
        report = StateReport(current_state={"buffer_pool": {"hit_ratio": None, "dirty_pages": 0}})
        md = _render_markdown(report)
        assert "hit_ratio=N/A" in md


class TestStateBuilderGoldenRender:
    """One known row per section; assert the exact rendered numbers.

    Anomaly detection is mocked (`alerting.anomaly.detect_anomalies`) rather
    than seeded from 28 days of same-hour/day-of-week history: that
    algorithm is already covered by tests/test_anomaly.py, and coupling this
    renderer-pinning test to wall-clock hour/weekday baselines would make it
    flaky for a reason unrelated to what it's guarding. Everything else here
    goes through the real queries in agent/queries.py against a seeded
    SQLite fixture.
    """

    def test_golden_render_all_sections(self, state_builder_db):
        conn, db_path = state_builder_db
        now = datetime.now(timezone.utc)
        now_iso = _iso(now)
        raw = sqlite3.connect(str(db_path))

        # --- 1. Top query / missing-index candidate (rows_examined/rows_sent
        #        = 40000/400 = 100x). This is the single latest
        #        query_digest_snapshots row for 'default', so it alone
        #        determines the "Top Queries" / "Missing Index" snapshot.
        raw.execute(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec,
                rows_examined, rows_sent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (now_iso, "default", "0xTOPQUERY0001",
             "SELECT * FROM orders WHERE user_id = ?", "shop",
             200, 10.0, 0.05, 0.3, 40000, 400),
        )

        # --- 2. Buffer pool hit ratio: cumulative counters, not the stale
        #        buffer_pool_snapshots.hit_ratio column. 1 - 800/100000 =
        #        0.9920 — a "normal" positive value; the 0.0 edge case is
        #        covered directly in TestHitRatioZeroRendersHonestly above.
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value) "
            "VALUES (?, 'default', 'Innodb_buffer_pool_reads', 800)",
            (now_iso,),
        )
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value) "
            "VALUES (?, 'default', 'Innodb_buffer_pool_read_requests', 100000)",
            (now_iso,),
        )

        # --- Current Threads_running/connected + QPS, so the "Server" line
        #     and the historical "now" values are real numbers rather than
        #     placeholders. Same snapshot_time as the buffer-pool rows above
        #     so they share the single latest global_status_snapshots time.
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value) "
            "VALUES (?, 'default', 'Threads_running', 12)",
            (now_iso,),
        )
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value) "
            "VALUES (?, 'default', 'Threads_connected', 40)",
            (now_iso,),
        )
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value, per_second) "
            "VALUES (?, 'default', 'Queries', 0, 30.0)",
            (now_iso,),
        )

        # --- Baseline samples for the "28-day same-hour avg" historical
        #     lines (P1c-11 relabel). Exactly 7 days ago always has the same
        #     UTC hour AND the same day-of-week as `now` (no DST in UTC), so
        #     this deterministically satisfies BASELINE_THREADS_RUNNING /
        #     BASELINE_QPS's same-hour-same-DOW filter regardless of when
        #     the suite runs.
        week_ago_iso = _iso(now - timedelta(days=7))
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value) "
            "VALUES (?, 'default', 'Threads_running', 8)",
            (week_ago_iso,),
        )
        raw.execute(
            "INSERT INTO global_status_snapshots "
            "(snapshot_time, server_id, variable_name, raw_value, per_second) "
            "VALUES (?, 'default', 'Queries', 0, 25.0)",
            (week_ago_iso,),
        )

        # --- 3. Lock wait (active in the last 5 minutes).
        raw.execute(
            """INSERT INTO lock_wait_snapshots
               (snapshot_time, server_id, waiting_trx_id, waiting_pid, waiting_query,
                wait_seconds, blocking_trx_id, blocking_pid, blocking_query,
                blocking_trx_age_sec, blocking_rows_locked)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_iso(now - timedelta(minutes=1)), "default", "T1", 101,
             "UPDATE inventory SET qty=qty-1 WHERE sku=?", 12,
             "T2", 202, "SELECT * FROM inventory FOR UPDATE", 45, 10),
        )

        # --- 4. Long transaction (age_sec=90 > default long_transaction_sec=30).
        raw.execute(
            """INSERT INTO transaction_snapshots
               (snapshot_time, server_id, trx_id, trx_state, age_sec, pid,
                trx_query, rows_locked, rows_modified)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (_iso(now - timedelta(minutes=1)), "default", "421", "RUNNING", 90, 555,
             "UPDATE orders SET status='shipped' WHERE id=99", 120, 45),
        )

        # --- 5. DDL change, within the explicit `since` window below.
        ddl_time = _iso(now - timedelta(minutes=10))
        raw.execute(
            """INSERT INTO ddl_changes
               (detected_at, server_id, table_schema, table_name, change_type, new_ddl)
               VALUES (?,?,?,?,?,?)""",
            (ddl_time, "default", "shop", "orders", "index", "CREATE INDEX ..."),
        )

        # --- 6. Query regression: baseline 3 days ago (0.02s) -> recent
        #        5 min ago (0.60s) = 30.0x slower. These two rows also feed
        #        the (bonus, asserted below) "30-Day Trends" line since both
        #        fall in the last 30 days on different calendar dates.
        raw.execute(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec,
                rows_examined, rows_sent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_iso(now - timedelta(days=3)), "default", "0xREGRESSED0001",
             "SELECT * FROM sessions WHERE token = ?", "shop",
             50, 1.0, 0.02, 0.05, 500, 50),
        )
        raw.execute(
            """INSERT INTO query_digest_snapshots
               (snapshot_time, server_id, digest, digest_text, schema_name,
                exec_count, total_time_sec, avg_time_sec, max_time_sec,
                rows_examined, rows_sent)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (_iso(now - timedelta(minutes=5)), "default", "0xREGRESSED0001",
             "SELECT * FROM sessions WHERE token = ?", "shop",
             80, 48.0, 0.60, 0.9, 800, 80),
        )

        # --- 7. Incident window (unresolved, within the last 24h).
        cur = raw.execute(
            """INSERT INTO incident_windows
               (server_id, start_time, end_time, severity, involved_metrics,
                event_count, status)
               VALUES ('default', ?, ?, 'warning', '["threads_running"]', 2, 'detected')""",
            (_iso(now - timedelta(hours=1)), _iso(now - timedelta(minutes=50))),
        )
        incident_id = cur.lastrowid

        raw.commit()
        raw.close()
        reset_connections()

        anomaly = AnomalyResult(
            metric="threads_running",
            current=50.0,
            baseline_mean=10.0,
            baseline_stddev=2.0,
            z_score=20.0,
            pct_change=400.0,
            direction="high",
            severity="critical",
            server_id="default",
        )

        since = _iso(now - timedelta(hours=2))
        with patch("alerting.anomaly.detect_anomalies", return_value=[anomaly]):
            report = build_state_report(since=since, server_id="default")
        md = report.to_markdown()

        # --- Section headers present ---
        for header in (
            "## Current State",
            "### Top Queries by Total Time",
            "### Missing Index Candidates",
            "### Lock Waits:",
            "### Buffer Pool:",
            "### Server:",
            "### Long Transactions:",
            "### Statistical Anomalies:",
            "### Recent Incidents",
            "## Changes Since Last Analysis",
            "### DDL Changes:",
            "### Query Regressions:",
            "## Historical Context",
        ):
            assert header in md, f"missing section header: {header!r}\n---\n{md}"

        # --- Exact numbers, per section ---
        assert (
            "1. digest=`0xTOPQUERY0001` schema=`shop` "
            "`SELECT * FROM orders WHERE user_id = ?` — "
            "total=10.00s, avg=0.0500s, execs=200, rows_examined=40000"
        ) in md

        assert (
            "- digest=`0xTOPQUERY0001` schema=`shop` "
            "`SELECT * FROM orders WHERE user_id = ?` — "
            "ratio=100x, examined=40000, sent=400"
        ) in md

        assert "### Lock Waits: 1 active, max wait 12s" in md

        assert "### Buffer Pool: hit_ratio=0.9920, dirty_pages=0" in md

        assert "### Server: Threads_running=12, Threads_connected=40, QPS=30.0" in md

        assert "### Long Transactions: 1 active" in md
        assert (
            "- trx=421, pid=555, age=90s, rows_locked=120, rows_modified=45, "
            "query=`UPDATE orders SET status='shipped' WHERE id=99`"
        ) in md

        assert "### Statistical Anomalies: 1 detected" in md
        assert (
            "- **Active threads**: 50.0000 (+400% above baseline mean=10.0000, "
            "z=20.0) [critical]"
        ) in md

        assert "### Recent Incidents (last 24h, unresolved): 1" in md
        assert (
            f"- #{incident_id} [warning] "
            f"{_iso(now - timedelta(hours=1))} → {_iso(now - timedelta(minutes=50))} "
            "(2 events, metrics: threads_running) [detected]"
        ) in md

        assert "### DDL Changes: 1" in md
        assert f"- `shop`.`orders` — index change at {ddl_time}" in md

        assert "### Query Regressions: 1" in md
        assert (
            "- digest=`0xREGRESSED0001` schema=`shop` "
            "`SELECT * FROM sessions WHERE token = ?` — "
            "was 0.0200s, now 0.6000s (30.0x slower)"
        ) in md

        # P1c-11: the 28-day same-hour label, not the misleading old
        # "same hour last week" text. BASELINE_THREADS_RUNNING/BASELINE_QPS
        # (agent/queries.py) filter on same-hour-same-DOW within the last 28
        # days but — unlike the anomaly engine's baseline — do NOT exclude
        # the current sample, so the "now" row (which trivially shares its
        # own hour/DOW) is averaged in alongside the week-ago row:
        # AVG(12, 8) = 10.0, AVG(30.0, 25.0) = 27.5.
        assert "- Threads_running now: 12, 28-day same-hour avg: 10.0" in md
        assert "- QPS now: 30.0, 28-day same-hour avg: 27.5" in md
        assert "same hour last week" not in md

        # Bonus (free from the rows above, still deterministic): peak/lock
        # 24h summaries and the 30-day regression trend line.
        assert "- Peak Threads_running (24h): 12" in md
        assert "- Lock waits (24h): 1 events, longest wait 12s" in md
        assert (
            "- digest=`0xREGRESSED0001` `SELECT * FROM sessions WHERE token = ?`: "
            "30d ago=0.0200s → today=0.6000s"
        ) in md
