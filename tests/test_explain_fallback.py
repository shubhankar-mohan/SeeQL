"""
Tests for the missing-index correlator's guarded run_explain fallback (P0.9).

When a suspect digest has no cached `explain_captures` row AND the caller
has opted in via `allow_live_explain=True`, the correlator makes a
best-effort attempt to obtain a live plan via `agent.tools._tool_run_explain`.
On success it populates `explain_summary` (via the existing
`_summarize_explain`); on ANY failure — including no prod access, which is
the normal case in CI — it degrades to (None, None) and never raises.

`allow_live_explain` defaults to False, so a cache miss with the default
arguments must NOT attempt the fallback at all (zero MySQL cost).
"""

from alerting.correlators import missing_index as mi


def test_explain_for_digest_uses_cache_when_present(monkeypatch):
    """Cache hit: fallback must not even be attempted."""
    monkeypatch.setattr(mi, "_fetch_latest_explain", lambda *a, **k: {"explain_json": "{}"})
    monkeypatch.setattr(mi, "_summarize_explain", lambda explain: ("type=ref, key=PRIMARY, rows=1", "members"))

    def _boom(*a, **k):
        raise AssertionError("fallback should not run on a cache hit")

    monkeypatch.setattr(mi, "_run_explain_fallback", _boom)

    summary, table = mi._explain_for_digest(conn=None, server_id="s", digest="0xCACHED")
    assert summary == "type=ref, key=PRIMARY, rows=1"
    assert table == "members"


def test_explain_for_digest_falls_back_to_run_explain(monkeypatch):
    """No cached capture, opted in via allow_live_explain=True: fallback returns a live plan."""
    monkeypatch.setattr(mi, "_fetch_latest_explain", lambda *a, **k: None)
    monkeypatch.setattr(
        mi,
        "_run_explain_fallback",
        lambda server_id, digest: ("type=ALL, key=NULL, rows=999", "pirates"),
    )

    summary, table = mi._explain_for_digest(
        conn=None, server_id="s", digest="7107e33a", allow_live_explain=True
    )
    assert summary is not None and "ALL" in summary
    assert table == "pirates"


def test_explain_for_digest_default_does_not_call_fallback(monkeypatch):
    """Default (allow_live_explain omitted / False): cache miss must NOT call the
    fallback at all — no live MySQL call, preserving the zero-cost invariant."""
    monkeypatch.setattr(mi, "_fetch_latest_explain", lambda *a, **k: None)

    called = {"flag": False}

    def _sentinel(server_id, digest):
        called["flag"] = True
        return ("type=ALL, key=NULL, rows=999", "pirates")

    monkeypatch.setattr(mi, "_run_explain_fallback", _sentinel)

    summary, table = mi._explain_for_digest(conn=None, server_id="s", digest="7107e33a")

    assert called["flag"] is False
    assert (summary, table) == (None, None)


def test_run_explain_fallback_calls_tool_and_summarizes(monkeypatch):
    """Exercise the real _run_explain_fallback body against a mocked tool call."""
    fake_result = {
        "source": "live",
        "explain": {
            "query_block": {
                "table": {
                    "table_name": "pirates",
                    "access_type": "ALL",
                    "rows_examined_per_scan": 999,
                }
            }
        },
    }
    called = {}

    def fake_tool_run_explain(input_data):
        called["digest"] = input_data["digest"]
        return fake_result

    def fake_set_current_server(server_id):
        called["server_id"] = server_id

    import agent.tools as agent_tools
    monkeypatch.setattr(agent_tools, "_tool_run_explain", fake_tool_run_explain)
    monkeypatch.setattr(agent_tools, "set_current_server", fake_set_current_server)

    summary, table = mi._run_explain_fallback("srv1", "7107e33a")

    assert called == {"digest": "7107e33a", "server_id": "srv1"}
    assert summary is not None and "ALL" in summary
    assert table == "pirates"


def test_run_explain_fallback_degrades_on_any_failure(monkeypatch):
    """No prod access (the normal case in CI) — must degrade to (None, None), never raise."""
    import agent.tools as agent_tools

    def _raise(input_data):
        raise RuntimeError("no route to production MySQL")

    monkeypatch.setattr(agent_tools, "_tool_run_explain", _raise)

    summary, table = mi._run_explain_fallback("srv1", "0xNOPROD")
    assert (summary, table) == (None, None)


def test_run_explain_fallback_degrades_on_tool_error_payload(monkeypatch):
    """The tool itself can return {"error": ...} instead of raising."""
    import agent.tools as agent_tools

    monkeypatch.setattr(
        agent_tools, "_tool_run_explain", lambda input_data: {"error": "Cannot EXPLAIN non-SELECT query"}
    )

    summary, table = mi._run_explain_fallback("srv1", "0xWRITE")
    assert (summary, table) == (None, None)


def test_explain_for_digest_no_cache_no_prod_access_degrades(monkeypatch):
    """End-to-end guard: cache miss + real (failing) fallback path never raises."""
    monkeypatch.setattr(mi, "_fetch_latest_explain", lambda *a, **k: None)

    import agent.tools as agent_tools

    def _raise(input_data):
        raise RuntimeError("no route to production MySQL")

    monkeypatch.setattr(agent_tools, "_tool_run_explain", _raise)

    summary, table = mi._explain_for_digest(conn=None, server_id="s", digest="0xNOPROD")
    assert (summary, table) == (None, None)
