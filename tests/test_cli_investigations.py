"""
Light integration tests for the `seeql investigations` CLI subcommands.

We call the handler directly (via argparse Namespace) instead of spawning
a subprocess so tests stay fast and reuse the pytest mon_db fixture.
"""

import json
import argparse
from datetime import datetime, timezone

import pytest

import config as config_module
from storage.connection import reset_connections
from storage import writer

from main import cmd_investigations


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def mon_db_ctx(mon_db):
    _, db_path = mon_db
    prev = config_module._config
    config_module._config = {
        "monitoring_db": {"path": str(db_path), "wal_mode": False, "busy_timeout_ms": 5000},
        "investigator": {},
    }
    reset_connections()
    yield mon_db
    config_module._config = prev
    reset_connections()


def _seed_investigation(alert_type="missing_index", status="completed", server_id="prod"):
    alert_id = writer.write_inbound_alert({
        "provider": "generic",
        "received_at": _iso(),
        "server_id": server_id,
        "external_id": f"ext-{alert_type}-{status}",
        "alert_type": alert_type,
        "severity": "warning",
        "summary": f"test {alert_type}",
        "payload": "{}",
        "signature_verified": 1,
    })
    return alert_id, writer.write_investigation({
        "inbound_alert_id": alert_id,
        "server_id": server_id,
        "started_at": _iso(),
        "ended_at": _iso() if status in ("completed", "aborted") else None,
        "status": status,
        "confidence": 0.72,
        "root_cause_summary": "Missing idx_foo on members",
    })


class TestInvestigationsList:
    def test_empty_list(self, mon_db_ctx, capsys):
        cmd_investigations(argparse.Namespace(
            inv_cmd="list", status=None, server=None, limit=20,
        ))
        out = capsys.readouterr().out
        assert "No investigations" in out

    def test_list_shows_rows(self, mon_db_ctx, capsys):
        _seed_investigation(alert_type="missing_index", status="completed")
        _seed_investigation(alert_type="lock_cascade", status="phase3")
        cmd_investigations(argparse.Namespace(
            inv_cmd="list", status=None, server=None, limit=20,
        ))
        out = capsys.readouterr().out
        assert "missing_index" in out
        assert "lock_cascade" in out
        assert "completed" in out
        assert "phase3" in out

    def test_list_filter_by_status(self, mon_db_ctx, capsys):
        _seed_investigation(alert_type="missing_index", status="completed")
        _seed_investigation(alert_type="lock_cascade", status="phase3")
        cmd_investigations(argparse.Namespace(
            inv_cmd="list", status="completed", server=None, limit=20,
        ))
        out = capsys.readouterr().out
        assert "completed" in out
        assert "phase3" not in out


class TestInvestigationsShow:
    def test_show_missing_exits(self, mon_db_ctx):
        with pytest.raises(SystemExit):
            cmd_investigations(argparse.Namespace(inv_cmd="show", id=99999))

    def test_show_renders(self, mon_db_ctx, capsys):
        alert_id, inv_id = _seed_investigation()
        writer.write_investigation_findings([{
            "investigation_id": inv_id,
            "created_at": _iso(),
            "phase": 1,
            "kind": "hypothesis",
            "severity": "warning",
            "content": json.dumps({"hypothesis": "test hypothesis text"}),
        }])
        writer.write_investigation_samples([{
            "investigation_id": inv_id,
            "sampled_at": _iso(),
            "sample_type": "processlist",
            "query_count": 1,
            "data": "[]",
        }])

        cmd_investigations(argparse.Namespace(inv_cmd="show", id=inv_id))
        out = capsys.readouterr().out
        assert f"Investigation #{inv_id}" in out
        assert "missing_index" in out
        assert "processlist" in out
        assert "hypothesis" in out
        assert "Missing idx_foo on members" in out


class TestInvestigationsAbort:
    def test_abort_sets_status(self, mon_db_ctx, capsys):
        _, inv_id = _seed_investigation(status="phase3")
        cmd_investigations(argparse.Namespace(
            inv_cmd="abort", id=inv_id, reason="cli_test",
        ))
        out = capsys.readouterr().out
        assert "Aborted investigation" in out

        conn, _ = mon_db_ctx
        row = conn.execute(
            "SELECT status, abort_reason, ended_at FROM investigations WHERE id = ?",
            (inv_id,),
        ).fetchone()
        assert row["status"] == "aborted"
        assert row["abort_reason"] == "cli_test"
        assert row["ended_at"] is not None

    def test_abort_missing_exits(self, mon_db_ctx):
        with pytest.raises(SystemExit):
            cmd_investigations(argparse.Namespace(
                inv_cmd="abort", id=99999, reason="cli_test",
            ))


class TestInvestigationsTrigger:
    def test_trigger_creates_and_runs_inline(self, mon_db_ctx, capsys, monkeypatch):
        # Stub run_investigation so we don't hit the LLM / MySQL.
        from alerting import investigator as INV
        monkeypatch.setattr(
            INV, "run_investigation",
            lambda inv_id: {"status": "stubbed", "id": inv_id},
        )
        # Stub server registry default
        from config import server_registry
        monkeypatch.setattr(
            server_registry.get_server_registry().__class__,
            "get_default_server_id",
            lambda self: "prod",
        )

        cmd_investigations(argparse.Namespace(
            inv_cmd="trigger",
            type="missing_index",
            severity="warning",
            server="prod",
            summary="cli-trigger-test",
        ))
        out = capsys.readouterr().out
        assert "Triggered investigation" in out
        assert "stubbed" in out

        conn, _ = mon_db_ctx
        row = conn.execute(
            "SELECT alert_type, provider FROM inbound_alerts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["alert_type"] == "missing_index"
        assert row["provider"] == "cli"
