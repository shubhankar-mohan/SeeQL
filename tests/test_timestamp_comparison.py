import sqlite3
from datetime import datetime, timedelta, timezone


def _iso(dt):  # ISO-'T', matching the collectors
    return dt.replace(tzinfo=None).isoformat()


def _make_db(tmp_path):
    db = str(tmp_path / "mon.db")
    conn = sqlite3.connect(db)
    with open("storage/schema.sql") as fh:
        conn.executescript(fh.read())
    return conn, db


def test_same_date_boundary_row_is_included(tmp_path):
    """A row 30 min ago (ISO-'T') must satisfy `>= datetime('now','-1 hour')`.

    The normalized/fixed comparison correctly includes it.
    """
    conn, _ = _make_db(tmp_path)
    now = datetime.now(timezone.utc)
    t30 = _iso(now - timedelta(minutes=30))
    conn.execute(
        "INSERT INTO query_digest_snapshots (snapshot_time, server_id, digest, avg_time_sec) "
        "VALUES (?, 'default', 'abc', 0.5)", (t30,))
    conn.commit()

    # The FIXED comparison pattern:
    n_fixed = conn.execute(
        "SELECT COUNT(*) FROM query_digest_snapshots "
        "WHERE datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now','-1 hour')"
    ).fetchone()[0]
    assert n_fixed == 1


def test_raw_compare_wrongly_includes_stale_same_day_row(tmp_path):
    """Demonstrates the actual direction of the ISO-'T' vs space bug.

    SQLite's default BINARY collation compares byte-by-byte. 'T' is 0x54,
    ' ' (space, what `datetime(...)` returns) is 0x20. So for a
    same-calendar-date row, the ISO-'T' column value ALWAYS sorts above a
    space-separated bound at the date/time separator position, regardless of
    the actual clock time encoded in the rest of the string. That means a
    raw `col >= datetime('now', ...)` comparison wrongly *includes* stale
    same-day rows that are chronologically outside the window (the opposite
    problem from "excluding boundary rows", but the same root cause: byte
    comparison never actually parses the datetime). The normalized
    `datetime(REPLACE(col,'T',' '))` pattern fixes this by making both sides
    space-separated before SQLite's datetime() functions compare them.
    """
    conn, _ = _make_db(tmp_path)
    now = datetime.now(timezone.utc)
    if now.hour < 3:
        import pytest
        pytest.skip("too close to UTC midnight for a deterministic same-day comparison")

    # 2 hours ago, same calendar date, clearly OUTSIDE a "last 1 hour" window.
    stale = _iso(now - timedelta(hours=2))
    conn.execute(
        "INSERT INTO query_digest_snapshots (snapshot_time, server_id, digest, avg_time_sec) "
        "VALUES (?, 'default', 'abc', 0.5)", (stale,))
    conn.commit()

    n_fixed = conn.execute(
        "SELECT COUNT(*) FROM query_digest_snapshots "
        "WHERE datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('now','-1 hour')"
    ).fetchone()[0]
    assert n_fixed == 0  # correctly excluded: 2h ago is not within the last hour

    n_raw = conn.execute(
        "SELECT COUNT(*) FROM query_digest_snapshots "
        "WHERE snapshot_time >= datetime('now','-1 hour')"
    ).fetchone()[0]
    assert n_raw == 1  # raw byte compare wrongly includes it (the bug)


def test_rules_query_regression_uses_normalized_compare(tmp_path, monkeypatch):
    """evaluate_query_regression must detect a same-day regression via the fix.

    Also plants a stale-but-same-calendar-date row (2 hours ago, fast) that
    chronologically belongs in the *baseline* bucket (older than the -1 hour
    "recent" cutoff, within the -7 day baseline window). With the raw
    `snapshot_time >= datetime('now', '-1 hour')` compare, SQLite's BINARY
    collation makes any same-date ISO-'T' row satisfy that bound regardless
    of actual time (see test_raw_compare_wrongly_includes_stale_same_day_row),
    so the buggy query wrongly folds this fast row into "recent" instead of
    "baseline" (and, symmetrically, the buggy `BETWEEN ... AND datetime('now',
    '-1 hour')` upper bound wrongly excludes it from baseline), diluting
    recent_avg and softening the computed regression factor to ~7.6x instead
    of the correct ~11.1x. The normalized comparison buckets it correctly.
    """
    import alerting.rules as rules
    conn, _ = _make_db(tmp_path)
    now = datetime.now(timezone.utc)
    if now.hour < 3:
        import pytest
        pytest.skip("too close to UTC midnight for a deterministic same-day comparison")

    # recent slow (last hour), same calendar date
    for m in (5, 20, 45):
        conn.execute("INSERT INTO query_digest_snapshots (snapshot_time,server_id,digest,digest_text,avg_time_sec) "
                     "VALUES (?, 'default','d1','SELECT 1',0.20)", (_iso(now - timedelta(minutes=m)),))
    # stale, same calendar date, OUTSIDE the -1 hour window -> must NOT be
    # bucketed as "recent". Only a buggy raw compare pulls it in.
    conn.execute("INSERT INTO query_digest_snapshots (snapshot_time,server_id,digest,digest_text,avg_time_sec) "
                 "VALUES (?, 'default','d1','SELECT 1',0.01)", (_iso(now - timedelta(hours=2)),))
    # baseline fast, prior days
    for d in (2, 3, 4, 5):
        conn.execute("INSERT INTO query_digest_snapshots (snapshot_time,server_id,digest,digest_text,avg_time_sec) "
                     "VALUES (?, 'default','d1','SELECT 1',0.02)", (_iso(now - timedelta(days=d)),))
    conn.commit()

    import contextlib
    @contextlib.contextmanager
    def fake_reader():
        conn.row_factory = sqlite3.Row
        yield conn
    monkeypatch.setattr(rules, "get_mon_reader", fake_reader)

    alert = rules.evaluate_query_regression({"threshold": 5.0}, "default")
    assert alert is not None
    # Correct bucketing: recent_avg=0.20 (3 rows), baseline_avg=(4*0.02+0.01)/5=0.018
    # -> factor ~= 11.11. The buggy raw compare instead mis-buckets the stale
    # row into "recent" (and excludes it from baseline), giving ~7.6x.
    assert abs(alert.context["factor"] - 11.11) < 0.5
