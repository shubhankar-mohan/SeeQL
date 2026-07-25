"""
Minimal test for `seeql --version`.

Calls main() directly with a patched sys.argv (same style as
test_cli_investigations.py) instead of spawning a subprocess, so the test
stays fast.
"""

import sys

import pytest

from main import main


def test_version_flag_exits_zero_and_prints_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["seeql", "--version"])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("seeql ")
