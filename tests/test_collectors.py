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


class TestMonitoringCredentialsSelfHeal:
    """Finding 5: a failed/transient credential resolution must NOT be cached.

    Only a successful (non-None) resolution latches; a transient failure
    (ADC / GCE metadata endpoint not ready) leaves the cache unresolved so
    the next collection cycle retries and the GCP collectors self-heal.
    """

    def test_failed_resolution_is_not_latched(self, monkeypatch):
        import collectors as collectors_pkg

        if not collectors_pkg._GOOGLE_AVAILABLE:
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
