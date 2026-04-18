"""Tests for seeql/doctor.py (DevEx review — Plan 3)."""

from unittest.mock import patch, MagicMock

import pytest

from seeql import doctor
from seeql.doctor import CheckResult


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


class TestIndividualChecks:
    def test_check_gcp_creds_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        r = doctor.check_gcp_creds()
        assert r.passed is False
        assert r.error_code == "E003"

    def test_check_gcp_creds_unresolved_placeholder(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "${MY_VAR}")
        r = doctor.check_gcp_creds()
        assert r.passed is False
        assert "placeholder" in r.detail

    def test_check_gcp_creds_file_not_found(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/nonexistent/creds.json")
        r = doctor.check_gcp_creds()
        assert r.passed is False
        assert "not found" in r.detail

    def test_check_gcp_creds_ok(self, monkeypatch, tmp_path):
        fake = tmp_path / "creds.json"
        fake.write_text('{"type": "service_account"}')
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(fake))
        r = doctor.check_gcp_creds()
        assert r.passed is True
        assert "creds.json" in r.detail


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
        assert "0/3 checks passed" in captured.out
        assert "[FAIL]" in captured.out

    def test_run_returns_zero_on_all_pass(self, monkeypatch, capsys):
        def passing_check():
            return CheckResult(name="forced pass", passed=True, detail="ok")
        monkeypatch.setattr(doctor, "CHECKS", [passing_check] * 3)

        failures = doctor.run()
        assert failures == 0
        captured = capsys.readouterr()
        assert "3/3 checks passed" in captured.out
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
