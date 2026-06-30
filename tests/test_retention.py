"""Tests for storage/retention.py — focused on override robustness.

A malformed `retention.overrides` value used to abort the entire daily cleanup
(the int() coercion ran outside the per-table try/except), letting the DB grow
unbounded. These guard that.
"""

import config as config_module
from storage.retention import _retention_for, run_retention_cleanup


class TestRetentionOverrideResilience:
    def test_bad_override_falls_back_to_default(self):
        config_module._config = {"retention": {"overrides": {"alert_history": "ninety"}}}
        assert _retention_for("alert_history", 90) == 90

    def test_none_override_falls_back_to_default(self):
        config_module._config = {"retention": {"overrides": {"alert_history": None}}}
        assert _retention_for("alert_history", 42) == 42

    def test_valid_override_is_used(self):
        config_module._config = {"retention": {"overrides": {"alert_history": 5}}}
        assert _retention_for("alert_history", 90) == 5

    def test_run_does_not_abort_on_one_bad_override(self, mon_db):
        """A single malformed override must not stop the whole cleanup run."""
        conn, db_path = mon_db
        config_module._config = {
            "monitoring_db": {"path": str(db_path), "wal_mode": False,
                              "busy_timeout_ms": 5000},
            "retention": {"days": 90, "overrides": {"alert_history": "ninety"}},
        }
        from storage.connection import reset_connections
        reset_connections()
        # Must not raise — returns the per-table deletion summary.
        result = run_retention_cleanup()
        assert isinstance(result, dict)
        reset_connections()
