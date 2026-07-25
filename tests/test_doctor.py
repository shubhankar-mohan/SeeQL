"""Tests for seeql/doctor.py (DevEx review — Plan 3)."""

from unittest.mock import patch, MagicMock

import pytest

from seeql import doctor
from seeql.doctor import CheckResult

# A minimal, non-placeholder gcp config so tests that exercise check_gcp_creds'
# ADC/env-var logic aren't accidentally short-circuited into SKIP by the new
# "gcp not configured" gate — and aren't accidentally coupled to whatever a
# developer's untracked settings.local.yaml happens to contain either.
_GCP_CONFIGURED = {"gcp": {"project_id": "test-project"}}


class TestCheckResult:
    def test_passed_format(self):
        r = CheckResult(name="Test check", passed=True, detail="all good")
        s = r.format(width=20)
        assert "[PASS]" in s
        assert "all good" in s

    def test_failed_with_error_code_includes_fix(self):
        r = CheckResult(
            name="Test check",
            passed=False,
            detail="broken",
            error_code="E001",
        )
        s = r.format(width=20)
        assert "[FAIL]" in s
        assert "→" in s  # fix arrow
        assert "docs" in s.lower() or "github" in s.lower()

    def test_skipped_format_shows_skip_not_fail(self):
        """A skipped check is neither [PASS] nor [FAIL] — it's [SKIP]."""
        r = CheckResult(
            name="Test check",
            passed=False,
            skipped=True,
            detail="not applicable",
        )
        s = r.format(width=20)
        assert "[SKIP]" in s
        assert "[FAIL]" not in s
        assert "[PASS]" not in s

    def test_skipped_format_does_not_print_error_fix(self):
        """Even if a skipped result somehow carries an error_code, SKIP must not
        print the catalog fix/docs lines — skips are informational, not actionable."""
        r = CheckResult(
            name="Test check",
            passed=False,
            skipped=True,
            detail="not applicable",
            error_code="E001",
        )
        s = r.format(width=20)
        assert "[SKIP]" in s
        assert "→" not in s


class TestIndividualChecks:
    def test_check_gcp_creds_missing(self, monkeypatch):
        monkeypatch.setattr("config.get_config", lambda: _GCP_CONFIGURED)
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        r = doctor.check_gcp_creds()
        assert r.passed is False
        assert r.skipped is False
        assert r.error_code == "E003"

    def test_check_gcp_creds_unresolved_placeholder(self, monkeypatch):
        monkeypatch.setattr("config.get_config", lambda: _GCP_CONFIGURED)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "${MY_VAR}")
        r = doctor.check_gcp_creds()
        assert r.passed is False
        assert "placeholder" in r.detail

    def test_check_gcp_creds_file_not_found(self, monkeypatch):
        monkeypatch.setattr("config.get_config", lambda: _GCP_CONFIGURED)
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/creds.json")
        r = doctor.check_gcp_creds()
        assert r.passed is False
        assert "not found" in r.detail

    def test_check_gcp_creds_ok(self, monkeypatch, tmp_path):
        monkeypatch.setattr("config.get_config", lambda: _GCP_CONFIGURED)
        fake = tmp_path / "creds.json"
        fake.write_text('{"type": "service_account"}')
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake))
        r = doctor.check_gcp_creds()
        assert r.passed is True
        assert "creds.json" in r.detail


class TestGcpCredsSkip:
    """A non-GCP install shouldn't be FAILed for not having GCP configured."""

    @pytest.mark.parametrize(
        "gcp_config",
        [
            pytest.param({}, id="no-gcp-section"),
            pytest.param({"gcp": {}}, id="empty-gcp-section"),
            pytest.param({"gcp": {"project_id": ""}}, id="blank-project-id"),
            pytest.param({"gcp": {"project_id": "your-gcp-project-id"}}, id="stock-placeholder"),
            pytest.param({"gcp": {"project_id": "your-project-here"}}, id="your-prefixed-placeholder"),
        ],
    )
    def test_skips_when_gcp_not_configured(self, monkeypatch, gcp_config):
        monkeypatch.setattr("config.get_config", lambda: gcp_config)
        # Even if ADC happens to be set in the environment, an unconfigured
        # gcp.project_id means the check doesn't apply to this install.
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/some/path.json")

        r = doctor.check_gcp_creds()

        assert r.skipped is True
        assert r.passed is False
        assert "not configured" in r.detail

    def test_does_not_skip_with_real_project_id(self, monkeypatch):
        monkeypatch.setattr("config.get_config", lambda: _GCP_CONFIGURED)
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

        r = doctor.check_gcp_creds()

        assert r.skipped is False


class TestLlmBackendSkip:
    """An install running with the LLM agent disabled shouldn't FAIL for it."""

    def test_skips_when_agent_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "config.get_config", lambda: {"agent": {"enabled": False}}
        )

        r = doctor.check_llm_backend()

        assert r.skipped is True
        assert r.passed is False
        assert "disabled" in r.detail

    def test_skips_when_agent_section_missing(self, monkeypatch):
        monkeypatch.setattr("config.get_config", lambda: {})

        r = doctor.check_llm_backend()

        assert r.skipped is True

    def test_does_not_skip_when_agent_enabled(self, monkeypatch):
        monkeypatch.setattr(
            "config.get_config", lambda: {"agent": {"enabled": True}}
        )
        # No real Gemini/Claude credentials in the test env -> backend is None
        # -> this should FAIL (not skip). We only care that it isn't skipped;
        # the pass/fail verdict itself is check_llm_backend's pre-existing job.
        r = doctor.check_llm_backend()

        assert r.skipped is False


class TestPerfSchemaConsumers:
    """`performance_schema enabled` alone doesn't prove the consumers SeeQL
    needs (query digests, statement history, stage/wait events) are ON."""

    @patch("storage.connection.get_prod_connection")
    def test_fails_when_a_consumer_is_disabled(self, mock_get_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("events_statements_current", "YES"),
            ("events_statements_history", "YES"),
            ("global_instrumentation", "YES"),
            ("thread_instrumentation", "YES"),
            ("statements_digest", "NO"),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        cm = MagicMock()
        cm.__enter__.return_value = mock_conn
        cm.__exit__.return_value = False
        mock_get_conn.return_value = cm

        r = doctor.check_perf_schema_consumers()

        assert r.passed is False
        assert r.skipped is False
        assert "statements_digest" in r.detail
        # Remediation must be actionable and flag that it needs an admin user.
        assert "UPDATE performance_schema.setup_consumers" in r.detail
        assert "ENABLED='YES'" in r.detail
        assert "admin" in r.detail.lower()

    @patch("storage.connection.get_prod_connection")
    def test_fails_when_multiple_consumers_disabled(self, mock_get_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("events_statements_current", "NO"),
            ("events_statements_history", "YES"),
            ("global_instrumentation", "YES"),
            ("thread_instrumentation", "YES"),
            ("statements_digest", "NO"),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        cm = MagicMock()
        cm.__enter__.return_value = mock_conn
        cm.__exit__.return_value = False
        mock_get_conn.return_value = cm

        r = doctor.check_perf_schema_consumers()

        assert r.passed is False
        assert "events_statements_current" in r.detail
        assert "statements_digest" in r.detail

    @patch("storage.connection.get_prod_connection")
    def test_passes_when_all_consumers_enabled(self, mock_get_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("events_statements_current", "YES"),
            ("events_statements_history", "YES"),
            ("global_instrumentation", "YES"),
            ("thread_instrumentation", "YES"),
            ("statements_digest", "YES"),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        cm = MagicMock()
        cm.__enter__.return_value = mock_conn
        cm.__exit__.return_value = False
        mock_get_conn.return_value = cm

        r = doctor.check_perf_schema_consumers()

        assert r.passed is True

    @patch("storage.connection.get_prod_connection")
    def test_fails_gracefully_on_connection_error(self, mock_get_conn):
        """Mirrors check_performance_schema's pattern: a connection failure is
        reported as a normal FAIL, not an unhandled crash."""
        mock_get_conn.side_effect = RuntimeError("no route to host")

        r = doctor.check_perf_schema_consumers()

        assert r.passed is False
        assert r.skipped is False


class TestStageInstruments:
    """Informational only — must never fail doctor, even on a query error."""

    @patch("storage.connection.get_prod_connection")
    def test_passes_even_when_all_disabled(self, mock_get_conn):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("stage/sql/init", "NO"),
            ("stage/sql/statistics", "NO"),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        cm = MagicMock()
        cm.__enter__.return_value = mock_conn
        cm.__exit__.return_value = False
        mock_get_conn.return_value = cm

        r = doctor.check_stage_instruments()

        assert r.passed is True
        assert r.skipped is False
        assert "0/2" in r.detail

    @patch("storage.connection.get_prod_connection")
    def test_passes_even_on_connection_error(self, mock_get_conn):
        mock_get_conn.side_effect = RuntimeError("boom")

        r = doctor.check_stage_instruments()

        assert r.passed is True


class TestRun:
    def test_run_returns_failure_count(self, monkeypatch, capsys):
        """When all checks fail, run() returns the count."""
        # Make every check fail
        def failing_check():
            return CheckResult(name="forced fail", passed=False, detail="test")
        monkeypatch.setattr(doctor, "CHECKS", [failing_check] * 3)

        failures = doctor.run()
        assert failures == 3
        captured = capsys.readouterr()
        assert "0 passed, 0 skipped, 3 failed" in captured.out
        assert "[FAIL]" in captured.out

    def test_run_returns_zero_on_all_pass(self, monkeypatch, capsys):
        def passing_check():
            return CheckResult(name="forced pass", passed=True, detail="ok")
        monkeypatch.setattr(doctor, "CHECKS", [passing_check] * 3)

        failures = doctor.run()
        assert failures == 0
        captured = capsys.readouterr()
        assert "3 passed, 0 skipped, 0 failed" in captured.out
        assert "healthy" in captured.out

    def test_run_survives_crashing_check(self, monkeypatch, capsys):
        """A check that raises an exception is recorded as a failure."""
        def exploding_check():
            raise RuntimeError("boom")
        monkeypatch.setattr(doctor, "CHECKS", [exploding_check])

        failures = doctor.run()
        assert failures == 1
        captured = capsys.readouterr()
        assert "check crashed" in captured.out

    def test_run_treats_skips_as_exit_zero(self, monkeypatch, capsys):
        """Mix of PASS + SKIP must exit 0 — skipped checks are not failures."""
        def passing_check():
            return CheckResult(name="stub pass", passed=True, detail="ok")

        def skipping_check():
            return CheckResult(
                name="stub skip", passed=False, skipped=True, detail="n/a"
            )
        monkeypatch.setattr(doctor, "CHECKS", [passing_check, skipping_check])

        failures = doctor.run()
        assert failures == 0
        captured = capsys.readouterr()
        assert "1 passed, 1 skipped, 0 failed" in captured.out
        assert "[SKIP]" in captured.out
        assert "[FAIL]" not in captured.out

    def test_run_gcp_and_llm_checks_skip_when_unconfigured_and_exits_zero(
        self, monkeypatch, capsys
    ):
        """End-to-end: a healthy non-GCP, agent-disabled install must not be
        FAILed for lacking things it never claimed to need — the real bug
        this task fixes. `check_prod_reachable` / `check_performance_schema` /
        `check_perf_schema_consumers` all hit production MySQL, which isn't
        available in a test run, so we swap those out for a stub pass —
        the assertion here is about the GCP/LLM checks and the exit code,
        not about unrelated collector connectivity.
        """
        fake_config = {"gcp": {}, "agent": {"enabled": False}}
        monkeypatch.setattr("config.get_config", lambda: fake_config)

        def stub_pass():
            return CheckResult(name="stub prod check", passed=True, detail="ok")

        monkeypatch.setattr(
            doctor,
            "CHECKS",
            [
                stub_pass,
                stub_pass,
                doctor.check_gcp_creds,
                doctor.check_llm_backend,
            ],
        )

        failures = doctor.run()

        captured = capsys.readouterr()
        assert failures == 0
        assert "[FAIL]" not in captured.out
        assert captured.out.count("[SKIP]") == 2
        assert "gcp not configured" in captured.out
        assert "agent disabled" in captured.out
        assert "2 passed, 2 skipped, 0 failed" in captured.out
