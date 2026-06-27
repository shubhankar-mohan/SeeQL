"""
Tests for alerting/correlators/missing_index.py.

Seeded SQLite fixtures — no MySQL involved. The correlator is a pure
read-side function.
"""

import json
from datetime import datetime, timezone, timedelta

import pytest

import config as config_module
from storage.connection import reset_connections
from alerting.correlators.missing_index import (
    correlate_missing_index,
    MissingIndexCorrelation,
    MissingIndexEvidence,
)


@pytest.fixture
def mon_db_ctx(mon_db):
    _, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
    }
    reset_connections()
    yield mon_db
    config_module._config = prev
    reset_connections()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hours_ago(h: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat()


def _seed_digest(conn, digest, server_id, table, ratio_multiplier=1000, exec_count=500):
    """Insert a query_digest_snapshots row with the given ratio characteristic."""
    rows_sent = 10
    rows_examined = rows_sent * ratio_multiplier
    conn.execute(
        """INSERT INTO query_digest_snapshots
           (server_id, snapshot_time, digest, digest_text, schema_name,
            exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
            rows_examined, rows_sent, rows_affected,
            full_scans, no_index_used)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            server_id, _now_iso(), digest,
            f"SELECT * FROM {table} WHERE foo = ?",
            "testdb",
            exec_count, 10.0, 10.0 / max(exec_count, 1), 2.0, 0.01,
            rows_examined, rows_sent, 0,
            5, 5,
        ),
    )


def _seed_zero_rows_digest(
    conn, digest, server_id, table, rows_examined=1_000_000, exec_count=500
):
    """A full scan that examines many rows but returns *zero* — the canonical
    missing-index symptom (WHERE on an unindexed column with no matches)."""
    conn.execute(
        """INSERT INTO query_digest_snapshots
           (server_id, snapshot_time, digest, digest_text, schema_name,
            exec_count, total_time_sec, avg_time_sec, max_time_sec, min_time_sec,
            rows_examined, rows_sent, rows_affected,
            full_scans, no_index_used)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            server_id, _now_iso(), digest,
            f"SELECT * FROM {table} WHERE foo = ?",
            "testdb",
            exec_count, 10.0, 10.0 / max(exec_count, 1), 2.0, 0.01,
            rows_examined, 0, 0,
            5, 5,
        ),
    )


def _seed_explain(conn, digest, server_id, table, access_type="ALL"):
    explain_json = json.dumps({
        "query_block": {
            "select_id": 1,
            "table": {
                "table_name": table,
                "access_type": access_type,
                "key": None if access_type == "ALL" else "PRIMARY",
                "rows_examined_per_scan": 1_000_000,
            },
        }
    })
    conn.execute(
        """INSERT INTO explain_captures
           (captured_at, server_id, digest, digest_text, schema_name, explain_json,
            total_time_sec, avg_time_sec, exec_count)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (_now_iso(), server_id, digest, "SELECT ... ", "testdb", explain_json, 10.0, 0.5, 100),
    )


def _seed_ddl_with_dropped_index(conn, server_id, table, index_name="idx_foo"):
    old_ddl = (
        f"CREATE TABLE `{table}` (\n"
        "  `id` bigint,\n"
        "  `foo` varchar(255),\n"
        f"  KEY `{index_name}` (`foo`)\n"
        ")"
    )
    new_ddl = (
        f"CREATE TABLE `{table}` (\n"
        "  `id` bigint,\n"
        "  `foo` varchar(255)\n"
        ")"
    )
    conn.execute(
        """INSERT INTO ddl_changes
           (detected_at, server_id, table_schema, table_name, change_type,
            old_schema_hash, new_schema_hash, old_index_hash, new_index_hash,
            old_ddl, new_ddl)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            _hours_ago(2), server_id, "testdb", table, "index",
            "oldhash", "newhash", "oih", "nih",
            old_ddl, new_ddl,
        ),
    )


class TestCorrelator:
    def test_no_data_returns_empty_correlation(self, mon_db_ctx):
        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        assert isinstance(c, MissingIndexCorrelation)
        assert c.evidence == []
        assert c.has_findings is False

    def test_auto_discovers_suspect_digests(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        # 3 digests, only 2 exceed default threshold (100x).
        _seed_digest(conn, "0xHIGH", "srv1", "members", ratio_multiplier=500)
        _seed_digest(conn, "0xMID", "srv1", "orders", ratio_multiplier=200)
        _seed_digest(conn, "0xLOW", "srv1", "tiny", ratio_multiplier=5)
        conn.commit()

        c = correlate_missing_index(
            "srv1", _hours_ago(1), _now_iso()
        )
        digests = {e.digest for e in c.evidence}
        assert "0xHIGH" in digests
        assert "0xMID" in digests
        assert "0xLOW" not in digests
        assert c.has_findings is True

    def test_zero_rows_sent_full_scan_is_discovered(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        # Examines a million rows, returns none — must be flagged even though
        # rows_sent == 0 (the bug previously excluded it via `rows_sent > 0`).
        _seed_zero_rows_digest(conn, "0xZERO", "srv1", "members", rows_examined=1_000_000)
        # A tiny zero-row scan should NOT be flagged (below threshold).
        _seed_zero_rows_digest(conn, "0xTINY", "srv1", "scratch", rows_examined=5)
        conn.commit()

        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        digests = {e.digest for e in c.evidence}
        assert "0xZERO" in digests
        assert "0xTINY" not in digests
        assert c.has_findings is True

        e = next(ev for ev in c.evidence if ev.digest == "0xZERO")
        assert e.rows_sent == 0
        assert e.rows_examined == 1_000_000
        # Ratio falls back to rows_examined when rows_sent == 0 — no div-by-zero.
        assert e.ratio == 1_000_000.0

    def test_suspect_digests_override_discovery(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xHIGH", "srv1", "members", ratio_multiplier=500)
        _seed_digest(conn, "0xLOW", "srv1", "tiny", ratio_multiplier=5)
        conn.commit()

        # Caller explicitly forces the low-ratio digest in. Correlator should
        # include it even though it's below the auto-discovery threshold.
        c = correlate_missing_index(
            "srv1", _hours_ago(1), _now_iso(),
            suspect_digests=["0xLOW"],
        )
        digests = [e.digest for e in c.evidence]
        assert digests == ["0xLOW"]
        # But has_findings stays False because ratio < threshold.
        assert c.has_findings is False

    def test_explain_merged_into_evidence(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xX", "srv1", "members", ratio_multiplier=500)
        _seed_explain(conn, "0xX", "srv1", "members", access_type="ALL")
        conn.commit()

        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        e = c.top_evidence
        assert e is not None
        assert e.table_name == "members"
        assert "type=ALL" in (e.explain_summary or "")
        assert "key=NULL" in (e.explain_summary or "")

    def test_dropped_index_detected_from_ddl_diff(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xY", "srv1", "members", ratio_multiplier=500)
        _seed_explain(conn, "0xY", "srv1", "members", access_type="ALL")
        _seed_ddl_with_dropped_index(conn, "srv1", "members", index_name="idx_members_foo")
        conn.commit()

        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        e = c.top_evidence
        assert e.dropped_index_hint == "idx_members_foo"
        assert len(e.recent_ddl) >= 1
        # Dropped-index evidence bumps confidence high.
        assert e.confidence >= 0.9

    def test_redundant_index_becomes_recommendation(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xZ", "srv1", "members", ratio_multiplier=500)
        _seed_explain(conn, "0xZ", "srv1", "members")
        conn.execute(
            """INSERT INTO redundant_index_snapshots
               (server_id, snapshot_time, table_schema, table_name,
                redundant_index_name, redundant_index_columns,
                dominant_index_name, dominant_index_columns,
                subpart_exists, sql_drop_index)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "srv1", _now_iso(), "testdb", "members",
                "idx_dup", "foo", "idx_primary", "foo,bar",
                0, "ALTER TABLE `testdb`.`members` DROP INDEX `idx_dup`",
            ),
        )
        conn.commit()

        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        e = c.top_evidence
        assert e.recommended_index is not None
        assert e.recommended_index.startswith("ALTER TABLE")
        assert "DROP INDEX" in e.recommended_index

    def test_unused_index_surfaced_to_avoid_duplicate_recommendation(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xA", "srv1", "members", ratio_multiplier=500)
        _seed_explain(conn, "0xA", "srv1", "members")
        conn.execute(
            """INSERT INTO unused_index_snapshots
               (server_id, snapshot_time, object_schema, table_name, index_name)
               VALUES (?,?,?,?,?)""",
            ("srv1", _now_iso(), "testdb", "members", "idx_stale"),
        )
        conn.commit()

        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        e = c.top_evidence
        idx_names = {u.get("index_name") for u in e.unused_indexes}
        assert "idx_stale" in idx_names

    def test_to_markdown_renders(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xB", "srv1", "members", ratio_multiplier=500)
        _seed_explain(conn, "0xB", "srv1", "members")
        conn.commit()

        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        md = c.to_markdown()
        assert "0xB" in md
        assert "Missing-index evidence" in md

    def test_to_markdown_empty(self, mon_db_ctx):
        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        md = c.to_markdown()
        assert "No missing-index signals" in md

    def test_server_isolation(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xC", "srv1", "members", ratio_multiplier=500)
        _seed_digest(conn, "0xD", "srv2", "orders", ratio_multiplier=500)
        conn.commit()

        c1 = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        c2 = correlate_missing_index("srv2", _hours_ago(1), _now_iso())

        assert {e.digest for e in c1.evidence} == {"0xC"}
        assert {e.digest for e in c2.evidence} == {"0xD"}

    def test_table_guessed_from_digest_when_no_explain(self, mon_db_ctx):
        conn, _ = mon_db_ctx
        _seed_digest(conn, "0xE", "srv1", "loyalty_members", ratio_multiplier=500)
        conn.commit()
        c = correlate_missing_index("srv1", _hours_ago(1), _now_iso())
        e = c.top_evidence
        # No explain, but digest_text has "FROM loyalty_members" — should catch.
        assert e.table_name == "loyalty_members"
