import agent.tools as tools


def _opened_conn_mock():
    """Return (sentinel, mock). The sentinel flips to True the instant
    get_prod_connection is called — BEFORE the mock raises. Because the
    flip happens outside of _tool_explain_query's try/except, it is
    observable even though the mock's exception itself gets swallowed by
    that except Exception block. This is what makes the rejection tests
    non-hollow: removing the guard clause causes the mock to be invoked,
    flipping the sentinel, and the assertion below catches it regardless
    of what error dict the function ends up returning.
    """
    opened = {"v": False}

    def _mock(*a, **k):
        opened["v"] = True
        raise AssertionError("must NOT open a prod connection for a non-runnable query")

    return opened, _mock


def test_rejects_parameterized_query(monkeypatch):
    opened, mock = _opened_conn_mock()
    monkeypatch.setattr(tools, "get_prod_connection", mock)
    r = tools._tool_explain_query({"query": "SELECT x FROM t WHERE id = ?"})
    assert "error" in r and ("?" in r["error"] or "placeholder" in r["error"].lower())
    assert opened["v"] is False, "get_prod_connection was called for a non-runnable query"


def test_rejects_truncated_ellipsis(monkeypatch):
    opened, mock = _opened_conn_mock()
    monkeypatch.setattr(tools, "get_prod_connection", mock)
    r = tools._tool_explain_query({"query": "SELECT a, b, c FROM orders WHERE created_at > ..."})
    assert "error" in r
    assert opened["v"] is False, "get_prod_connection was called for a non-runnable query"


def test_rejects_unicode_ellipsis(monkeypatch):
    opened, mock = _opened_conn_mock()
    monkeypatch.setattr(tools, "get_prod_connection", mock)
    r = tools._tool_explain_query({"query": "SELECT a FROM t WHERE k IN (…)"})
    assert "error" in r
    assert opened["v"] is False, "get_prod_connection was called for a non-runnable query"


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
