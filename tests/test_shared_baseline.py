import sqlite3
from datetime import datetime, timedelta, timezone
import agent.queries as Q


def _iso(dt): return dt.replace(tzinfo=None).isoformat()


def _db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    with open("storage/schema.sql") as fh: conn.executescript(fh.read())
    conn.row_factory = sqlite3.Row
    return conn


def test_threads_baseline_excludes_incident_window(tmp_path):
    conn = _db(tmp_path)
    now = datetime.now(timezone.utc)
    # normal samples same hour/DOW over prior weeks
    for w in (1, 2, 3):
        conn.execute("INSERT INTO global_status_snapshots (snapshot_time,server_id,variable_name,raw_value) "
                     "VALUES (?, 'default','Threads_running',10)", (_iso(now - timedelta(weeks=w)),))
    # a contaminated spike inside an incident window, same hour/DOW last week
    spike_t = now - timedelta(weeks=1, minutes=1)
    conn.execute("INSERT INTO global_status_snapshots (snapshot_time,server_id,variable_name,raw_value) "
                 "VALUES (?, 'default','Threads_running',500)", (_iso(spike_t),))
    conn.execute("INSERT INTO incident_windows (server_id,start_time,end_time,severity,involved_metrics,status) "
                 "VALUES ('default',?,?,'critical','[\"threads_running\"]','detected')",
                 (_iso(spike_t - timedelta(minutes=5)), _iso(spike_t + timedelta(minutes=5))))
    conn.commit()
    avg = conn.execute(Q.BASELINE_THREADS_RUNNING, ("default",)).fetchone()["avg_value"]
    assert avg is not None and avg < 50  # the 500 spike must be excluded
