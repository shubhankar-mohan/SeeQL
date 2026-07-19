"""Tests for alerting/engine.py.

P1c-6: the cooldown must only be set once an alert actually delivers.
Before this fix, `_cooldowns[scoped_key] = alert.fired_at` ran regardless of
`delivered`, so a Slack outage (delivered=False) silenced the alert for the
whole cooldown window with no retry. The fix also always sends to the log
channel as a fallback when the configured channels fail, so the failure is
at least visible, without counting that fallback towards `delivered`.
"""

import sqlite3
from pathlib import Path

import config as config_module
from config import server_registry as server_registry_module

SCHEMA_SQL_PATH = Path(__file__).parent.parent / "storage" / "schema.sql"


def _seed_lock_cascade(db_path):
    """Seed enough lock_wait_snapshots rows to deterministically fire
    evaluate_lock_cascade (default rule config: min_count=3, min_wait_seconds=10)."""
    conn = sqlite3.connect(str(db_path))
    for _ in range(3):
        conn.execute(
            "INSERT INTO lock_wait_snapshots (snapshot_time, server_id, wait_seconds) "
            "VALUES (datetime('now'), 'default', 15)"
        )
    conn.commit()
    conn.close()


class _EngineTestBase:
    """Shared setup: real temp monitoring DB, alerting enabled with only
    lock_cascade active (deterministic, cheap to seed), rule channels
    deliberately excluding "log" so `delivered` reflects only the real
    (mocked) channel outcome."""

    def _setup(self, tmp_path, test_config, rule_channels):
        db_path = tmp_path / "engine_test.db"
        test_config["monitoring_db"]["path"] = str(db_path)
        test_config["alerting"] = {
            "enabled": True,
            "default_cooldown_minutes": 15,
            "channels": {
                "slack": {"enabled": True, "webhook_url": "https://hooks.slack.test/webhook"},
                "log": {"enabled": True},
            },
            "rules": {
                "lock_cascade": {
                    "enabled": True,
                    "min_count": 3,
                    "min_wait_seconds": 10,
                    "cooldown_minutes": 15,
                    "channels": rule_channels,
                },
                "threads_running_spike": {"enabled": False},
                "query_regression": {"enabled": False},
                "ddl_change": {"enabled": False},
                "high_cpu": {"enabled": False},
                "high_memory": {"enabled": False},
                "deadlock_detected": {"enabled": False},
                "anomaly_detection": {"enabled": False},
            },
        }
        config_module._config = test_config

        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA_SQL_PATH.read_text())
        conn.commit()
        conn.close()
        _seed_lock_cascade(db_path)

        from storage.connection import reset_connections
        reset_connections()

        # ServerRegistry is a module-level singleton; force a fresh load
        # from our test config (no `servers:` key -> single "default" server).
        self._prev_registry = server_registry_module._registry
        server_registry_module._registry = None

        import alerting.engine as engine
        engine._cooldowns.clear()
        engine._initialized = False
        self.engine = engine

    def _teardown(self):
        server_registry_module._registry = self._prev_registry
        from storage.connection import reset_connections
        reset_connections()


class TestCooldownNotSetOnDeliveryFailure(_EngineTestBase):
    def test_no_cooldown_and_log_fallback_when_all_channels_fail(self, tmp_path, test_config, monkeypatch):
        # Rule is configured with "slack" only -- deliberately excludes "log"
        # so a failing Slack channel leaves `delivered` False.
        self._setup(tmp_path, test_config, rule_channels=["slack"])
        try:
            monkeypatch.setattr("alerting.channels.SlackChannel.send", lambda self, alert: False)

            log_calls = []
            monkeypatch.setattr(
                "alerting.channels.LogChannel.send",
                lambda self, alert: log_calls.append(alert) or True,
            )

            fired = self.engine.evaluate()
            assert len(fired) == 1
            assert fired[0].delivered is False
            assert "lock_cascade:default" not in self.engine._cooldowns
            # Fallback: log channel must still have been invoked even though
            # the rule's configured channels didn't include "log".
            assert len(log_calls) == 1

            # Second cycle must re-attempt immediately -- no cooldown means
            # the rule is evaluated again instead of being suppressed.
            fired2 = self.engine.evaluate()
            assert len(fired2) == 1
            assert fired2[0].delivered is False
        finally:
            self._teardown()

    def test_alert_row_still_stored_with_delivered_zero(self, tmp_path, test_config, monkeypatch):
        self._setup(tmp_path, test_config, rule_channels=["slack"])
        try:
            monkeypatch.setattr("alerting.channels.SlackChannel.send", lambda self, alert: False)

            self.engine.evaluate()

            from storage.connection import get_mon_reader
            with get_mon_reader() as conn:
                row = conn.execute(
                    "SELECT delivered FROM alert_history WHERE rule_name = ?",
                    ("lock_cascade:default",),
                ).fetchone()
            assert row is not None
            assert row["delivered"] == 0
        finally:
            self._teardown()


class TestShippedSlackLogConfigStillRetries(_EngineTestBase):
    """Regression guard for the CRITICAL review finding: the shipped config
    uses `channels: [slack, log]` for every rule. Because LogChannel.send()
    always returns True and was walked in the same loop as Slack, `delivered`
    became True even when Slack failed -> cooldown set -> retries suppressed,
    silently defeating P1c-6. `delivered` must reflect ONLY real (non-log)
    channels."""

    def test_slack_fails_with_log_configured_still_retries(self, tmp_path, test_config, monkeypatch):
        # EXACTLY the shipped channel list: [slack, log].
        self._setup(tmp_path, test_config, rule_channels=["slack", "log"])
        try:
            monkeypatch.setattr("alerting.channels.SlackChannel.send", lambda self, alert: False)

            fired = self.engine.evaluate()
            assert len(fired) == 1
            # log succeeded, but log must NOT count as delivery.
            assert fired[0].delivered is False
            # No cooldown -> the outage doesn't silence the rule.
            assert "lock_cascade:default" not in self.engine._cooldowns

            # Alert row stored with delivered=0 despite log succeeding.
            from storage.connection import get_mon_reader
            with get_mon_reader() as conn:
                row = conn.execute(
                    "SELECT delivered FROM alert_history WHERE rule_name = ?",
                    ("lock_cascade:default",),
                ).fetchone()
            assert row is not None and row["delivered"] == 0

            # The whole point: the very next cycle re-fires instead of being
            # suppressed by a wrongly-set cooldown.
            fired2 = self.engine.evaluate()
            assert len(fired2) == 1
            assert fired2[0].delivered is False
        finally:
            self._teardown()

    def test_slack_ok_with_log_configured_sets_cooldown(self, tmp_path, test_config, monkeypatch):
        # [slack, log] with Slack healthy: delivered True, cooldown set,
        # next cycle suppressed -- the fix must not break the happy path.
        self._setup(tmp_path, test_config, rule_channels=["slack", "log"])
        try:
            monkeypatch.setattr("alerting.channels.SlackChannel.send", lambda self, alert: True)

            fired = self.engine.evaluate()
            assert len(fired) == 1
            assert fired[0].delivered is True
            assert "lock_cascade:default" in self.engine._cooldowns

            fired2 = self.engine.evaluate()
            assert fired2 == []
        finally:
            self._teardown()


class TestCooldownSetOnDeliverySuccess(_EngineTestBase):
    def test_cooldown_set_and_second_cycle_suppressed(self, tmp_path, test_config, monkeypatch):
        # Slack succeeds this time -- delivered should be True and the
        # cooldown should suppress the very next cycle.
        self._setup(tmp_path, test_config, rule_channels=["slack"])
        try:
            monkeypatch.setattr("alerting.channels.SlackChannel.send", lambda self, alert: True)

            fired = self.engine.evaluate()
            assert len(fired) == 1
            assert fired[0].delivered is True
            assert "lock_cascade:default" in self.engine._cooldowns

            # Cooldown is 15 minutes -- immediate re-evaluation must be suppressed.
            fired2 = self.engine.evaluate()
            assert fired2 == []
        finally:
            self._teardown()
