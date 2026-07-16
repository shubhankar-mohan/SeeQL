import sqlite3, contextlib
from datetime import datetime, timezone
import alerting.rules as rules


def _reader(conn):
    @contextlib.contextmanager
    def r():
        conn.row_factory = sqlite3.Row; yield conn
    return r


def _db(tmp_path, mem):
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    with open("storage/schema.sql") as fh: conn.executescript(fh.read())
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    conn.execute("INSERT INTO gcp_metric_snapshots (snapshot_time,server_id,metric_name,metric_type,value) "
                 "VALUES (?, 'default','memory_utilization','gauge',?)", (now, mem))
    conn.commit()
    return conn


def test_high_memory_fires_at_90pct(tmp_path, monkeypatch):
    conn = _db(tmp_path, 0.90)
    monkeypatch.setattr(rules, "get_mon_reader", _reader(conn))
    alert = rules.evaluate_high_memory({"threshold": 0.85, "severity": "warning"}, "default")
    assert alert is not None and "memory" in alert.message.lower()


def test_high_memory_quiet_at_60pct(tmp_path, monkeypatch):
    conn = _db(tmp_path, 0.60)
    monkeypatch.setattr(rules, "get_mon_reader", _reader(conn))
    assert rules.evaluate_high_memory({"threshold": 0.85}, "default") is None


def test_high_memory_registered():
    assert "high_memory" in rules.RULE_EVALUATORS


def test_cpu_absolute_floor_suppresses_low_cpu():
    # The anomaly engine must not mark a sub-floor CPU value critical.
    from alerting.anomaly import _apply_cpu_floor  # helper added by this task
    # z says "critical" but absolute CPU 6.6% is below the 50% floor
    assert _apply_cpu_floor(severity="critical", current_value=0.066, floor=0.50) != "critical"
    assert _apply_cpu_floor(severity="critical", current_value=0.92, floor=0.50) == "critical"
