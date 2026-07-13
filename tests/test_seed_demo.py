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
