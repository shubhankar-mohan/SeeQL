import agent.tools as tools


def _no_conn(*a, **k):
    raise AssertionError("must NOT open a prod connection for a non-runnable query")


def test_rejects_parameterized_query(monkeypatch):
    monkeypatch.setattr(tools, "get_prod_connection", _no_conn)
    r = tools._tool_explain_query({"query": "SELECT x FROM t WHERE id = ?"})
    assert "error" in r and "?" in r["error"] or "placeholder" in r["error"].lower()


def test_rejects_truncated_ellipsis(monkeypatch):
    monkeypatch.setattr(tools, "get_prod_connection", _no_conn)
    r = tools._tool_explain_query({"query": "SELECT a, b, c FROM orders WHERE created_at > ..."})
    assert "error" in r


def test_rejects_unicode_ellipsis(monkeypatch):
    monkeypatch.setattr(tools, "get_prod_connection", _no_conn)
    r = tools._tool_explain_query({"query": "SELECT a FROM t WHERE k IN (…)"})
    assert "error" in r


def test_allows_clean_select(monkeypatch):
    called = {}
    class FakeCur:
        def execute(self, *a, **k): called["ran"] = True
        def fetchone(self): return {"EXPLAIN": '{"query_block":{"select_id":1}}'}
    class FakeConn:
        def cursor(self, **k): return FakeCur()
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(tools, "get_prod_connection", lambda *_a, **_k: FakeConn())
    r = tools._tool_explain_query({"query": "SELECT id FROM users WHERE email = 'a@b.com'"})
    assert r.get("source") == "live" and called.get("ran")
