"""Tests for parsers module."""

from datetime import datetime, timedelta

from parsers.global_status import GlobalStatusDeltaCalculator, TRACKED_VARIABLES
from parsers.innodb_status import _parse_deadlock
from tests.fixtures.mysql_mock_data import MOCK_GLOBAL_STATUS, MOCK_GLOBAL_STATUS_SECOND


class TestGlobalStatusDeltaCalculator:
    def test_first_call_no_delta(self):
        calc = GlobalStatusDeltaCalculator()
        now = datetime(2025, 1, 1, 10, 0, 0)
        result = calc.process(MOCK_GLOBAL_STATUS, now)

        assert len(result) > 0
        for row in result:
            assert row["delta_value"] is None
            assert row["per_second"] is None
            assert row["snapshot_time"] == now

    def test_second_call_computes_delta(self):
        calc = GlobalStatusDeltaCalculator()
        t1 = datetime(2025, 1, 1, 10, 0, 0)
        t2 = t1 + timedelta(seconds=300)

        calc.process(MOCK_GLOBAL_STATUS, t1)
        result = calc.process(MOCK_GLOBAL_STATUS_SECOND, t2)

        questions = next(r for r in result if r["variable_name"] == "Questions")
        assert questions["delta_value"] == 1500  # 2500 - 1000
        assert questions["per_second"] == 5.0  # 1500 / 300

        com_select = next(r for r in result if r["variable_name"] == "Com_select")
        assert com_select["delta_value"] == 1200  # 2000 - 800
        assert com_select["per_second"] == 4.0

    def test_counter_decrease_skips_delta(self):
        calc = GlobalStatusDeltaCalculator()
        t1 = datetime(2025, 1, 1, 10, 0, 0)
        t2 = t1 + timedelta(seconds=300)

        higher = [{"Variable_name": "Questions", "Value": "5000"}]
        lower = [{"Variable_name": "Questions", "Value": "1000"}]

        calc.process(higher, t1)
        result = calc.process(lower, t2)

        questions = next(r for r in result if r["variable_name"] == "Questions")
        assert questions["delta_value"] is None
        assert questions["per_second"] is None

    def test_non_tracked_vars_filtered(self):
        calc = GlobalStatusDeltaCalculator()
        now = datetime(2025, 1, 1, 10, 0, 0)
        result = calc.process(MOCK_GLOBAL_STATUS, now)

        var_names = {r["variable_name"] for r in result}
        assert "Some_untracked_var" not in var_names
        assert all(v in TRACKED_VARIABLES for v in var_names)

    def test_non_integer_values_skipped(self):
        calc = GlobalStatusDeltaCalculator()
        now = datetime(2025, 1, 1, 10, 0, 0)
        data = [
            {"Variable_name": "Questions", "Value": "not_a_number"},
            {"Variable_name": "Queries", "Value": "100"},
        ]
        result = calc.process(data, now)

        var_names = {r["variable_name"] for r in result}
        assert "Questions" not in var_names
        assert "Queries" in var_names

    def test_zero_elapsed_time_no_crash(self):
        calc = GlobalStatusDeltaCalculator()
        t = datetime(2025, 1, 1, 10, 0, 0)

        calc.process(MOCK_GLOBAL_STATUS, t)
        result = calc.process(MOCK_GLOBAL_STATUS_SECOND, t)  # same time

        for row in result:
            assert row["per_second"] is None


def test_deadlock_timestamp_parsed():
    """P1b-3: InnoDB reprints the same LATEST DETECTED DEADLOCK section on
    every SHOW ENGINE INNODB STATUS call until the server restarts, so the
    section's own header timestamp — not snapshot freshness — is the only
    reliable signal for "is this a NEW deadlock"."""
    text = "2026-07-17 01:42:33 0x16f887000\n*** (1) TRANSACTION:\nTRANSACTION 48119 ..."
    parsed = _parse_deadlock(text)
    assert parsed["deadlock_at"] == "2026-07-17 01:42:33"


def test_deadlock_timestamp_missing_is_none():
    """No leading timestamp line (e.g. malformed/empty section) => None, not
    a crash — evaluate_deadlock treats a missing deadlock_at as un-fireable."""
    parsed = _parse_deadlock("*** (1) TRANSACTION:\nTRANSACTION 48119 ...")
    assert parsed["deadlock_at"] is None
