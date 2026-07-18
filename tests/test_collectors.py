"""Tests for collector modules.

Collectors take a ``ServerContext`` and obtain connections via
``with ctx.get_connection() as conn:``. These tests build a mock context whose
``get_connection()`` yields a mock MySQL connection, then exercise the
``collect(now, ctx) -> store(data)`` workflow.
"""

from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from collectors.fast_loop import (
    ProcesslistCollector,
    LockWaitCollector,
    TransactionCollector,
    MetadataLockCollector,
    run_fast_loop,
)
from collectors.medium_loop import (
    QueryDigestCollector,
    WaitEventCollector,
    TableIOCollector,
    InnoDBMetricCollector,
    BufferPoolCollector,
    GlobalStatusCollector,
    run_medium_loop,
)
from collectors.slow_loop import SchemaSnapshotCollector, run_slow_loop
from tests.fixtures.mysql_mock_data import (
    MOCK_PROCESSLIST,
    MOCK_LOCK_WAITS,
    MOCK_TRANSACTIONS,
    MOCK_METADATA_LOCKS,
    MOCK_QUERY_DIGESTS,
    MOCK_WAIT_EVENTS,
    MOCK_TABLE_IO,
    MOCK_INNODB_METRICS,
    MOCK_BUFFER_POOL,
    MOCK_GLOBAL_STATUS,
    MOCK_SCHEMA_FINGERPRINT,
    MOCK_INDEX_FINGERPRINT,
    MOCK_TABLE_SIZES,
)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _mock_cursor_with_data(data):
    """Create a mock connection whose cursor.fetchall() returns ``data``."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = [row.copy() for row in data]
    mock_conn.cursor.return_value = mock_cursor
    return mock_conn


def _mock_ctx_with_data(data, server_id="test-server"):
    """Build a mock ServerContext whose get_connection() yields a mock conn.

    ``ctx.get_connection()`` returns a context manager whose ``__enter__``
    yields a mock MySQL connection backed by ``data``.
    """
    mock_conn = _mock_cursor_with_data(data)
    ctx = MagicMock()
    ctx.server_id = server_id
    ctx.get_connection.return_value.__enter__.return_value = mock_conn
    ctx.get_connection.return_value.__exit__.return_value = False
    return ctx


class TestProcesslistCollector:
    @patch("collectors.fast_loop.writer")
    def test_collect_and_store(self, mock_writer):
        ctx = _mock_ctx_with_data(MOCK_PROCESSLIST)

        collector = ProcesslistCollector()
        now = _utcnow()
        data = collector.collect(now, ctx)

        assert "processlist" in data
        assert len(data["processlist"]) == 2
        assert data["processlist"][0]["snapshot_time"] == now
        assert data["processlist"][0]["server_id"] == "test-server"

        collector.store(data)
        mock_writer.write_processlist.assert_called_once()


class TestLockWaitCollector:
    @patch("collectors.fast_loop.writer")
    def test_collect_and_store(self, mock_writer):
        ctx = _mock_ctx_with_data(MOCK_LOCK_WAITS)

        collector = LockWaitCollector()
        now = _utcnow()
        data = collector.collect(now, ctx)

        assert "lock_waits" in data
        assert len(data["lock_waits"]) == 1
        assert data["lock_waits"][0]["server_id"] == "test-server"

        collector.store(data)
        mock_writer.write_lock_waits.assert_called_once()


class TestTransactionCollector:
    @patch("collectors.fast_loop.writer")
    def test_collect_and_store(self, mock_writer):
        ctx = _mock_ctx_with_data(MOCK_TRANSACTIONS)

        collector = TransactionCollector()
        now = _utcnow()
        data = collector.collect(now, ctx)

        assert "transactions" in data
        assert len(data["transactions"]) == 1
        assert data["transactions"][0]["server_id"] == "test-server"

        collector.store(data)
        mock_writer.write_transactions.assert_called_once()


class TestMetadataLockCollector:
    @patch("collectors.fast_loop.writer")
    def test_collect_and_store(self, mock_writer):
        ctx = _mock_ctx_with_data(MOCK_METADATA_LOCKS)

        collector = MetadataLockCollector()
        now = _utcnow()
        data = collector.collect(now, ctx)

        assert "metadata_locks" in data
        assert len(data["metadata_locks"]) == 1
        assert data["metadata_locks"][0]["server_id"] == "test-server"

        collector.store(data)
        mock_writer.write_metadata_locks.assert_called_once()


class TestQueryDigestCollector:
    @patch("collectors.medium_loop.writer")
    def test_collect_and_store(self, mock_writer):
        ctx = _mock_ctx_with_data(MOCK_QUERY_DIGESTS)

        collector = QueryDigestCollector()
        now = _utcnow()
        data = collector.collect(now, ctx)

        assert "digests" in data
        assert len(data["digests"]) == 1
        assert data["digests"][0]["server_id"] == "test-server"

        collector.store(data)
        mock_writer.write_query_digests.assert_called_once()


class TestGlobalStatusCollector:
    @patch("collectors.medium_loop.writer")
    def test_first_run_no_delta(self, mock_writer):
        ctx = _mock_ctx_with_data(MOCK_GLOBAL_STATUS)

        # Fresh collector → fresh in-memory delta calculator → first run.
        collector = GlobalStatusCollector()
        now = _utcnow()
        data = collector.collect(now, ctx)

        assert "global_status" in data
        assert len(data["global_status"]) > 0
        for row in data["global_status"]:
            assert row["delta_value"] is None
            assert row["server_id"] == "test-server"


class TestSchemaSnapshotCollectorGroupConcat:
    """P1b-2: wide-table DDL fingerprints must not silently truncate.

    MySQL's default group_concat_max_len (1024 bytes) truncates the
    GROUP_CONCAT() output SCHEMA_FINGERPRINT/INDEX_FINGERPRINT build their
    MD5 hash from. Past ~60-70 columns, the concatenated string is cut at a
    fixed byte boundary, so a column added beyond that boundary never
    changes the hash and the DDL change goes undetected. The collector must
    raise the session limit before running the fingerprint queries.
    """

    def test_group_concat_max_len_set_before_schema_fingerprint(self):
        # Minimal, self-contained recording cursor/connection (does not
        # reuse `_mock_cursor_with_data`, which only stubs `fetchall()` and
        # can't assert call *order*). Mirrors the record-execute-calls
        # pattern in tests/test_identifier_validation.py.
        class _RecordingCursor:
            def __init__(self):
                self.executed: list[str] = []

            def execute(self, sql, *args, **kwargs):
                self.executed.append(sql)

            def fetchall(self):
                # Empty result set for every query keeps this test focused
                # purely on statement ordering: no rows means the collector
                # never reaches its DDL-diff branch or calls conn.cursor()
                # a second time for SHOW CREATE TABLE.
                return []

            def fetchone(self):
                return None

        class _RecordingConnection:
            def __init__(self, cursor):
                self._cursor = cursor

            def cursor(self, *args, **kwargs):
                return self._cursor

        recording_cursor = _RecordingCursor()
        mock_conn = _RecordingConnection(recording_cursor)

        ctx = MagicMock()
        ctx.server_id = "test-server"
        ctx.get_connection.return_value.__enter__.return_value = mock_conn
        ctx.get_connection.return_value.__exit__.return_value = False

        collector = SchemaSnapshotCollector()
        # Pre-seed so collect() skips _load_previous_hashes(), which reads
        # the real monitoring SQLite DB via get_mon_reader() — unrelated to
        # what this test verifies (production-cursor statement ordering).
        collector._initialized.add(ctx.server_id)

        collector.collect(_utcnow(), ctx)

        executed = recording_cursor.executed
        assert "SET SESSION group_concat_max_len = 1048576" in executed, (
            f"group_concat_max_len was never raised; statements executed: {executed}"
        )
        set_idx = executed.index("SET SESSION group_concat_max_len = 1048576")

        fingerprint_idx = next(
            (i for i, sql in enumerate(executed) if "information_schema.COLUMNS" in sql),
            None,
        )
        assert fingerprint_idx is not None, "SCHEMA_FINGERPRINT was never executed"

        assert set_idx < fingerprint_idx, (
            "group_concat_max_len must be raised BEFORE SCHEMA_FINGERPRINT runs, "
            "otherwise wide-table fingerprints silently truncate"
        )


class TestSchemaSnapshotCollectorCrashSafety:
    """P1b-6: the in-memory hash cache must only advance after store()
    durably writes the new snapshot + DDL change rows.

    Bug: the old collect() assigned self._previous_hashes[sid] to the NEW
    hashes as its very last step, before store() ever ran. If store() then
    raised (SQLite disk full, a lock timeout, a crash mid-write), the cache
    was already advanced — the next cycle compares against a hash that was
    never durably recorded, so the DDL change is lost forever. Fix:
    collect() returns the candidate new hashes without touching
    self._previous_hashes; store() only assigns them after a successful
    write.
    """

    @staticmethod
    def _multi_fetchall_conn(data_sequence):
        """Mock connection: cursor.fetchall() returns a different list per
        call (fingerprints, indexes, table sizes); cursor.fetchone() backs
        the SHOW CREATE TABLE lookup issued when a change is detected."""
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[row.copy() for row in d] for d in data_sequence]
        cursor.fetchone.return_value = (
            "loyalty_members", "CREATE TABLE `loyalty_members` (id BIGINT)",
        )
        conn.cursor.return_value = cursor
        return conn

    def _ctx(self, server_id):
        conn = self._multi_fetchall_conn(
            [MOCK_SCHEMA_FINGERPRINT, MOCK_INDEX_FINGERPRINT, MOCK_TABLE_SIZES]
        )
        ctx = MagicMock()
        ctx.server_id = server_id
        ctx.get_connection.return_value.__enter__.return_value = conn
        ctx.get_connection.return_value.__exit__.return_value = False
        return ctx

    def test_failed_store_does_not_advance_cache_change_is_redetected(self, monkeypatch):
        sid = "test-server"
        collector = SchemaSnapshotCollector()
        collector._initialized.add(sid)  # skip _load_previous_hashes (real SQLite read)

        # Seed a "previous" snapshot where loyalty_members' schema_hash
        # differs from MOCK_SCHEMA_FINGERPRINT's "abc123hash" — collect()
        # will detect this as a DDL change. `users` matches exactly (no
        # change), isolating a single detected change for the test.
        pre_store_cache = {
            ("mydb", "loyalty_members"): {
                "schema_hash": "OLD_HASH_BEFORE_DDL_CHANGE",
                "index_hash": "idx_abc123",
                "create_stmt": "CREATE TABLE loyalty_members (old)",
            },
            ("mydb", "users"): {
                "schema_hash": "def456hash",
                "index_hash": "idx_def456",
                "create_stmt": "CREATE TABLE users (...)",
            },
        }
        collector._previous_hashes[sid] = pre_store_cache

        data = collector.collect(_utcnow(), self._ctx(sid))

        # Sanity: the change was actually detected.
        assert len(data["changes"]) == 1
        assert data["changes"][0]["table_name"] == "loyalty_members"

        # THE BUG, pinned: collect() alone must not mutate the cache — it
        # must still be the exact pre-collect() object. Pre-fix, collect()
        # had already rebound self._previous_hashes[sid] to the new hashes
        # by this point, so this assertion is what fails RED.
        assert collector._previous_hashes[sid] is pre_store_cache

        assert data["sid"] == sid
        assert data["new_hashes"][("mydb", "loyalty_members")]["schema_hash"] == "abc123hash"

        # store() raises — simulate a crash / disk-full mid-write.
        monkeypatch.setattr(
            "storage.writer.write_schema_and_changes",
            MagicMock(side_effect=RuntimeError("simulated store failure")),
        )
        with pytest.raises(RuntimeError, match="simulated store failure"):
            collector.store(data)

        # Cache must remain untouched after the failed store — not
        # partially advanced.
        assert collector._previous_hashes[sid] is pre_store_cache
        assert (
            collector._previous_hashes[sid][("mydb", "loyalty_members")]["schema_hash"]
            == "OLD_HASH_BEFORE_DDL_CHANGE"
        )

        # Next cycle: since the cache never advanced, the SAME change is
        # re-detected — nothing was silently lost.
        data2 = collector.collect(_utcnow(), self._ctx(sid))
        assert len(data2["changes"]) == 1
        assert data2["changes"][0]["table_name"] == "loyalty_members"

        # This time store() succeeds — the cache should advance now, and
        # only now.
        monkeypatch.setattr(
            "storage.writer.write_schema_and_changes",
            MagicMock(return_value=len(data2["snapshots"]) + len(data2["changes"])),
        )
        collector.store(data2)

        assert (
            collector._previous_hashes[sid][("mydb", "loyalty_members")]["schema_hash"]
            == "abc123hash"
        )


class TestMonitoringCredentialsSelfHeal:
    """Finding 5: a failed/transient credential resolution must NOT be cached.

    Only a successful (non-None) resolution latches; a transient failure
    (ADC / GCE metadata endpoint not ready) leaves the cache unresolved so
    the next collection cycle retries and the GCP collectors self-heal.
    """

    def test_failed_resolution_is_not_latched(self, monkeypatch):
        import collectors as collectors_pkg

        if not collectors_pkg._google_sdk_available():
            pytest.skip("google-auth not installed")

        # Clean, unresolved cache state (auto-restored by monkeypatch).
        monkeypatch.setattr(collectors_pkg, "_credentials_resolved", False)
        monkeypatch.setattr(collectors_pkg, "_monitoring_credentials", None)
        monkeypatch.setattr(collectors_pkg, "_credentials_failed_at", 0.0)

        # Point the env var at a missing file so the service-account branch
        # is skipped and resolution deterministically reaches the ADC path.
        monkeypatch.setenv(
            "MONITORING_APPLICATION_CREDENTIALS", "/nonexistent/creds.json"
        )

        # Cycle 1: ADC fails transiently → None, and must stay unresolved.
        with patch("google.auth.default", side_effect=Exception("metadata not ready")):
            assert collectors_pkg.get_monitoring_credentials() is None
        assert collectors_pkg._credentials_resolved is False
        assert collectors_pkg._monitoring_credentials is None

        # The transient failure backs off rather than latching permanently;
        # simulate the backoff window elapsing so the next cycle retries.
        monkeypatch.setattr(collectors_pkg, "_credentials_failed_at", 0.0)

        # Cycle 2: ADC now succeeds → resolves and caches (self-heal).
        fake_creds = object()
        with patch("google.auth.default", return_value=(fake_creds, "proj")):
            assert collectors_pkg.get_monitoring_credentials() is fake_creds
        assert collectors_pkg._credentials_resolved is True
        assert collectors_pkg._monitoring_credentials is fake_creds


class TestMediumLoopGcpRegistration:
    """Finding 6: GCP collectors register per-run, not frozen at import.

    The collector list is rebuilt on each ``run_medium_loop`` call, so GCP
    collectors appear as soon as ``gcp.project_id`` is configured (config may
    be loaded / overridden / env-substituted after this module imports).
    """

    def test_registration_reflects_config_at_call_time(self, monkeypatch):
        import collectors.medium_loop as ml

        if not ml._GCP_COLLECTORS_AVAILABLE:
            pytest.skip("gcp extra not installed")

        # Stub the GCP collector singletons so no real cloud calls happen.
        gcp_metric = MagicMock()
        gcp_metric.name = "gcp_metrics"
        gcp_metric.run.return_value = True
        gcp_slow = MagicMock()
        gcp_slow.name = "gcp_slow_log"
        gcp_slow.run.return_value = True
        monkeypatch.setattr(ml, "_gcp_metric_collector", gcp_metric)
        monkeypatch.setattr(ml, "_gcp_slow_log_collector", gcp_slow)

        ctx = _mock_ctx_with_data([])

        # Placeholder project_id → GCP collectors must NOT register.
        monkeypatch.setattr(
            "config.get_config",
            lambda: {"gcp": {"project_id": "your-gcp-project-id"}},
        )
        results = run_medium_loop(ctx)
        assert "gcp_metrics" not in results
        assert "gcp_slow_log" not in results
        gcp_metric.run.assert_not_called()

        # Real project_id supplied later → same call path now registers them.
        monkeypatch.setattr(
            "config.get_config",
            lambda: {"gcp": {"project_id": "kc-prod-123"}},
        )
        results = run_medium_loop(ctx)
        assert results.get("gcp_metrics") is True
        assert results.get("gcp_slow_log") is True
        gcp_metric.run.assert_called_once()
        gcp_slow.run.assert_called_once()


class TestRunFastLoop:
    @patch("collectors.fast_loop.writer")
    def test_returns_results_dict(self, mock_writer):
        # One context manager per fast collector (collector order):
        # processlist, lock_waits, transactions, metadata_locks.
        ctx = MagicMock()
        ctx.server_id = "test-server"

        def _empty_cm():
            cm = MagicMock()
            cm.__enter__.return_value = _mock_cursor_with_data([])
            cm.__exit__.return_value = False
            return cm

        ctx.get_connection.side_effect = [_empty_cm() for _ in range(4)]

        results = run_fast_loop(ctx)
        assert isinstance(results, dict)
        assert results == {
            "processlist": True,
            "lock_waits": True,
            "transactions": True,
            "metadata_locks": True,
        }
