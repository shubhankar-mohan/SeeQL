"""Tests for seeql/errors.py (Phase 2.5)."""

import pytest

from seeql.errors import CATALOG, SeeQLError, get


class TestErrorCatalog:
    def test_all_ten_codes_present(self):
        for i in range(1, 11):
            code = f"E{i:03d}"
            assert code in CATALOG, f"{code} missing from catalog"

    def test_every_error_is_complete(self):
        """Every catalog entry must have problem + cause + fix."""
        for code, err in CATALOG.items():
            assert err.problem, f"{code} missing problem"
            assert err.cause, f"{code} missing cause"
            assert err.fix, f"{code} missing fix"
            assert err.code == code, f"{code} has mismatched code field"

    def test_format_includes_all_fields(self):
        err = CATALOG["E001"]
        formatted = err.format()
        assert "error[E001]" in formatted
        assert "cause:" in formatted
        assert "fix:" in formatted
        assert err.problem in formatted

    def test_get_returns_new_instance_with_details(self):
        err = get("E006", details="Connection refused (127.0.0.1:3306)")
        assert err.code == "E006"
        assert "Connection refused" in err.format()

    def test_get_unknown_code_returns_bug_report_error(self):
        err = get("E999")
        assert err.code == "E999"
        assert "Unknown error code" in err.problem
        assert "issues" in err.fix

    def test_seeqlerror_is_raiseable(self):
        with pytest.raises(SeeQLError) as exc:
            raise get("E001", details="test")
        assert exc.value.code == "E001"


class TestErrorsWiredIntoCLI:
    """End-to-end wiring: SeeQLError raised inside command functions is
    caught by main() and formatted before exit (Phase 2.5 Finding — the
    catalog shipped without any code actually raising it)."""

    def test_replay_no_args_raises_E010(self, monkeypatch):
        """cmd_replay with no args raises E010 via the main() wrapper."""
        import sys as sys_mod
        import main as main_mod
        from seeql.errors import SeeQLError

        monkeypatch.setattr(sys_mod, "argv", ["seeql", "replay"])
        # main() catches SeeQLError and calls sys.exit — capture the exit
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        # Exit code should be derived from E010 → 10
        assert exc_info.value.code == 10

    def test_replay_invalid_iso_raises_E010(self, monkeypatch, capsys):
        import sys as sys_mod
        import main as main_mod

        monkeypatch.setattr(
            sys_mod, "argv",
            ["seeql", "replay", "--from", "not-a-timestamp", "--to", "also-not"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 10
        captured = capsys.readouterr()
        # Canonical Rust-style block should be in stderr
        assert "error[E010]" in captured.err
        assert "ISO 8601" in captured.err or "not valid" in captured.err

    def test_replay_inverted_window_raises_E010(self, monkeypatch, capsys):
        import sys as sys_mod
        import main as main_mod

        monkeypatch.setattr(
            sys_mod, "argv",
            ["seeql", "replay",
             "--from", "2026-04-10T05:00:00",
             "--to", "2026-04-10T03:00:00"],
        )
        with pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 10
        captured = capsys.readouterr()
        assert "error[E010]" in captured.err
        assert "strictly before" in captured.err
