import sqlite3
import pathlib
import importlib.util


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("seed_demo", "scripts/seed_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build(tmp_path):
    seed = _load_seed_module()
    db = str(tmp_path / "grandline_demo.db")
    seed.build(db)
    return sqlite3.connect(db)


def test_current_state_panels_populated(tmp_path):
    conn = _build(tmp_path)
    conn.row_factory = sqlite3.Row

    # one server, resolvable as grandline-prod
    sid = conn.execute("SELECT server_id FROM servers WHERE role='primary'").fetchone()[0]
    assert sid == "grandline-prod"

    # processlist thread count at latest snapshot
    n = conn.execute(
        "SELECT COUNT(*) FROM processlist_snapshots "
        "WHERE snapshot_time=(SELECT MAX(snapshot_time) FROM processlist_snapshots)"
    ).fetchone()[0]
    assert n > 0

    # a red lock wait (>10s) at latest snapshot
    locks = conn.execute(
        "SELECT wait_seconds FROM lock_wait_snapshots "
        "WHERE snapshot_time=(SELECT MAX(snapshot_time) FROM lock_wait_snapshots)"
    ).fetchall()
    assert any(r[0] > 10 for r in locks)

    # a long transaction (>60s) at latest snapshot
    txns = conn.execute(
        "SELECT age_sec FROM transaction_snapshots "
        "WHERE snapshot_time=(SELECT MAX(snapshot_time) FROM transaction_snapshots)"
    ).fetchall()
    assert any(r[0] > 60 for r in txns)

    # Threads_running present at latest global_status snapshot
    tr = conn.execute(
        "SELECT raw_value FROM global_status_snapshots "
        "WHERE variable_name='Threads_running' "
        "AND snapshot_time=(SELECT MAX(snapshot_time) FROM global_status_snapshots)"
    ).fetchone()
    assert tr is not None


def test_queries_and_schema_populated(tmp_path):
    conn = _build(tmp_path)
    conn.row_factory = sqlite3.Row

    # digests exist in the last 24h window (query page uses BETWEEN)
    n_digests = conn.execute(
        "SELECT COUNT(DISTINCT digest) FROM query_digest_snapshots"
    ).fetchone()[0]
    assert n_digests >= 8

    # at least one digest carries a runnable sample + high full_scans
    scan = conn.execute(
        "SELECT digest FROM query_digest_snapshots "
        "WHERE query_sample_text IS NOT NULL AND full_scans > 0 LIMIT 1"
    ).fetchone()
    assert scan is not None

    # regression pair: some digest is >=3x slower in last hour vs 1h-7d baseline
    rows = conn.execute(
        """
        WITH recent AS (
          SELECT digest, AVG(avg_time_sec) a FROM query_digest_snapshots
          WHERE snapshot_time >= datetime('now','-1 hour') GROUP BY digest),
        baseline AS (
          SELECT digest, AVG(avg_time_sec) a FROM query_digest_snapshots
          WHERE snapshot_time BETWEEN datetime('now','-7 days') AND datetime('now','-1 hour')
          GROUP BY digest)
        SELECT r.digest FROM recent r JOIN baseline b ON r.digest=b.digest
        WHERE b.a > 0 AND r.a / b.a >= 3.0
        """
    ).fetchall()
    assert len(rows) >= 1

    # explain capture present for the scan digest
    assert conn.execute("SELECT COUNT(*) FROM explain_captures").fetchone()[0] >= 1

    # schema + index + ddl + todo-page tables non-empty
    for tbl in ("schema_snapshots", "unused_index_snapshots",
                "redundant_index_snapshots", "ddl_changes",
                "table_io_snapshots", "slow_query_log",
                "global_variable_snapshots", "innodb_status_snapshots"):
        assert conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] > 0, tbl

    # DDL change is within the 24h window the overview/active-alerts panel uses
    recent_ddl = conn.execute(
        "SELECT COUNT(*) FROM ddl_changes WHERE detected_at >= datetime('now','-24 hours')"
    ).fetchone()[0]
    assert recent_ddl >= 1

    # max_connections present for the todo page
    assert conn.execute(
        "SELECT variable_value FROM global_variable_snapshots WHERE variable_name='max_connections'"
    ).fetchone() is not None
