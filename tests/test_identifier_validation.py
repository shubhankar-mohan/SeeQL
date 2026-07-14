import contextlib

import agent.tools as tools


def test_valid_identifier_regex():
    assert tools._valid_identifier("order_reconciliation")
    assert tools._valid_identifier("Orders123")
    assert not tools._valid_identifier("orders; DROP TABLE x")
    assert not tools._valid_identifier("bad`name")
    assert not tools._valid_identifier("a b")
    assert not tools._valid_identifier("")


def test_get_index_stats_rejects_bad_name(monkeypatch):
    # Sentinel-based mock: prove prod was never queried, rather than merely
    # checking for an "error" key (which a broad except-Exception could also
    # produce even if the guard were missing/removed).
    called = {"flag": False}

    def _sentinel_run_live_query(*a, **k):
        called["flag"] = True
        return []

    monkeypatch.setattr(tools, "_run_live_query", _sentinel_run_live_query)

    r = tools._tool_get_index_stats({"schema_name": "db", "table_name": "x; DROP"})

    assert "error" in r
    assert called["flag"] is False


def test_get_table_schema_rejects_bad_name(monkeypatch):
    # Sentinel-based mock: prove get_prod_connection was never opened for a
    # mangled/injection identifier. _tool_get_table_schema wraps the prod
    # call in `except Exception`, so a raised AssertionError there would be
    # swallowed and turned into a (still-truthy) "error" result even if the
    # validation guard were missing -- so we assert on the sentinel flag
    # instead of relying solely on "error" in the response.
    called = {"flag": False}

    def _sentinel_get_prod_connection(*a, **k):
        called["flag"] = True
        raise AssertionError("must NOT hit prod for an invalid identifier")

    class FakeCursor:
        def fetchone(self):
            return None  # monitoring DB miss

    class FakeMonConn:
        def execute(self, *a, **k):
            return FakeCursor()

    @contextlib.contextmanager
    def fake_reader():
        yield FakeMonConn()

    monkeypatch.setattr(tools, "get_mon_reader", fake_reader)
    monkeypatch.setattr(tools, "get_prod_connection", _sentinel_get_prod_connection)

    r = tools._tool_get_table_schema({"schema_name": "db", "table_name": "t`; DROP"})

    assert "error" in r
    assert called["flag"] is False
