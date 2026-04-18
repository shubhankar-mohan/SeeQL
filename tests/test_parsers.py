"""Tests for parsers module."""

from datetime import datetime, timedelta

import pytest

from parsers.global_status import GlobalStatusDeltaCalculator, TRACKED_VARIABLES
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
