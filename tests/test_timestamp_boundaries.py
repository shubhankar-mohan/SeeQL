"""Guard tests for the T-format vs space-format timestamp comparison bug (P1c-1, P1-7).

Rows store naive-UTC ISO-8601 timestamps WITH a 'T' separator
(e.g. '2026-07-17T00:00:01'). SQLite's datetime('now', ...) produces a
SPACE-separated string (e.g. '2026-07-17 12:00:00'). A raw string
comparison between a stored column and datetime('now', ...) is wrong
because 'T' > ' ' lexicographically, so an old row can satisfy a
"recent window" lower bound it should not match. The fix is to wrap the
stored column in datetime(REPLACE(col, 'T', ' ')) before comparing it to
datetime('now', ...).
"""

import sqlite3


def test_t_separator_window_comparison():
    """A 'T'-format row from BEFORE the window must not match a datetime('now') lower bound."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (snapshot_time TEXT)")
    conn.execute("INSERT INTO t VALUES ('2026-07-17T00:00:01')")  # midnight today
    # naive compare: WRONG (matches because 'T' > ' ')
    naive = conn.execute(
        "SELECT COUNT(*) FROM t WHERE snapshot_time >= datetime('2026-07-17 12:00:00','-5 minutes')"
    ).fetchone()[0]
    wrapped = conn.execute(
        "SELECT COUNT(*) FROM t WHERE datetime(REPLACE(snapshot_time,'T',' ')) >= datetime('2026-07-17 12:00:00','-5 minutes')"
    ).fetchone()[0]
    assert naive == 1 and wrapped == 0   # documents the bug and the fix


def test_no_unwrapped_now_comparisons():
    """Repo guard: no query may compare a T-format column to datetime('now') unwrapped.

    Scans every production *.py file (excluding tests/ and venv/) under the
    packages listed in PROD_DIRS below, not just a fixed 5-file spot check,
    so a new collector/route/tool can't reintroduce the bug unnoticed.

    tests/test_seed_demo.py and tests/test_timestamp_comparison.py contain
    intentional unwrapped demonstrations of the bug and are out of scope
    (they live under tests/, which this guard does not walk).
    """
    import re
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    prod_dirs = [
        "agent", "alerting", "api", "collectors", "parsers",
        "storage", "scheduler", "config", "mcp_server", "seeql",
    ]
    cols = r"(snapshot_time|analyzed_at|detected_at|captured_at)"
    comparison_re = re.compile(cols + r"\s*(>=|<=|<|>)\s*datetime\('now'")
    between_re = re.compile(cols + r"\s+BETWEEN\s+datetime\('now'")

    bad = []
    for d in prod_dirs:
        base = repo_root / d
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            parts = path.relative_to(repo_root).parts
            if "tests" in parts or "venv" in parts:
                continue
            src = path.read_text()
            rel = str(path.relative_to(repo_root))
            for m in comparison_re.finditer(src):
                bad.append((rel, m.group(0)))
            for m in between_re.finditer(src):
                bad.append((rel, m.group(0)))
    assert not bad, f"unwrapped T-format comparisons: {bad}"
