"""
Tests for mcp_server/safety.py — MCPSafety guards and the wrap_tool helper.
"""

import time

import pytest

from mcp_server.safety import (
    MCPSafety,
    ToolRejected,
    wrap_tool,
    LIVE_TOOLS,
    EXPENSIVE_TOOL,
    ACTION_TOOLS,
)


class TestServerAllowlist:
    def test_empty_allowlist_allows_all(self):
        s = MCPSafety(allowed_servers=[])
        s.check_server("anything")  # no raise
        s.check_server(None)

    def test_denied_server_rejected(self):
        s = MCPSafety(allowed_servers=["a", "b"])
        with pytest.raises(ToolRejected) as exc:
            s.check_server("c")
        assert "not in mcp.allowed_servers" in str(exc.value)

    def test_allowed_server_passes(self):
        s = MCPSafety(allowed_servers=["a"])
        s.check_server("a")

    def test_none_server_passes(self):
        # None means "use registry default"; safety doesn't know the default.
        s = MCPSafety(allowed_servers=["a"])
        s.check_server(None)


class TestBudget:
    def test_snapshot_tools_never_rejected(self):
        s = MCPSafety(live_calls_per_session=0, explain_calls_per_session=0)
        for t in ("seeql_list_servers", "seeql_run_explain", "seeql_get_query_history"):
            s.check_budget(t)  # no raise

    def test_live_tool_cap_enforced(self):
        s = MCPSafety(live_calls_per_session=2)
        s.record_call("seeql_get_live_processlist")
        s.record_call("seeql_get_live_locks")
        with pytest.raises(ToolRejected) as exc:
            s.check_budget("seeql_get_live_transactions")
        assert "live-MySQL tools" in str(exc.value)

    def test_explain_cap_separate_from_live(self):
        s = MCPSafety(live_calls_per_session=10, explain_calls_per_session=1)
        s.record_call(EXPENSIVE_TOOL)
        with pytest.raises(ToolRejected):
            s.check_budget(EXPENSIVE_TOOL)
        # Live tools still work
        s.check_budget("seeql_get_live_locks")


class TestActionGate:
    def test_trigger_rejected_by_default(self):
        s = MCPSafety()  # action_tools_enabled=False
        with pytest.raises(ToolRejected) as exc:
            s.check_action("seeql_trigger_investigation")
        assert "action_tools_enabled" in str(exc.value).lower() or \
               "mcp.action_tools_enabled" in str(exc.value)

    def test_trigger_allowed_when_both_flags_set(self):
        s = MCPSafety(action_tools_enabled=True, allow_trigger=True)
        s.check_action("seeql_trigger_investigation")

    def test_trigger_rejected_when_master_on_but_sub_off(self):
        s = MCPSafety(action_tools_enabled=True, allow_trigger=False)
        with pytest.raises(ToolRejected) as exc:
            s.check_action("seeql_trigger_investigation")
        assert "allow_trigger" in str(exc.value)

    def test_explain_query_double_gated(self):
        # Master only → still rejected
        s = MCPSafety(action_tools_enabled=True, allow_explain_query=False)
        with pytest.raises(ToolRejected):
            s.check_action(EXPENSIVE_TOOL)
        # Both on
        s = MCPSafety(action_tools_enabled=True, allow_explain_query=True)
        s.check_action(EXPENSIVE_TOOL)

    def test_read_only_tools_untouched(self):
        s = MCPSafety()
        # Read-only tools aren't in ACTION_TOOLS; check_action is a no-op.
        s.check_action("seeql_list_servers")
        s.check_action("seeql_get_state_report")


class TestRateLimiter:
    def test_under_capacity_allowed(self):
        s = MCPSafety(tools_per_minute=5)
        for _ in range(5):
            s.check_rate("t")

    def test_over_capacity_rejected(self):
        s = MCPSafety(tools_per_minute=2)
        s.check_rate("t")
        s.check_rate("t")
        with pytest.raises(ToolRejected):
            s.check_rate("t")

    def test_per_tool_independent(self):
        s = MCPSafety(tools_per_minute=1)
        s.check_rate("tool_a")
        # tool_b has its own bucket
        s.check_rate("tool_b")

    def test_zero_disables_limiter(self):
        s = MCPSafety(tools_per_minute=0)
        for _ in range(100):
            s.check_rate("t")

    def test_bucket_refills_over_time(self, monkeypatch):
        # Capacity 60, refill 1 token/sec. Drain the bucket, then verify
        # that after a short advance the bucket allows proportional calls
        # but blocks beyond.
        s = MCPSafety(tools_per_minute=60)
        base = [100.0]
        monkeypatch.setattr("mcp_server.safety.time.monotonic", lambda: base[0])

        # Drain all 60 tokens.
        for _ in range(60):
            s.check_rate("t")
        with pytest.raises(ToolRejected):
            s.check_rate("t")

        # Advance 2.0 seconds → 2 tokens refilled.
        base[0] += 2.0
        s.check_rate("t")
        s.check_rate("t")
        with pytest.raises(ToolRejected):
            s.check_rate("t")


class TestWrapTool:
    def test_snapshot_tool_passes_through(self):
        s = MCPSafety()
        called = []
        def impl():
            called.append(1)
            return {"ok": True}
        wrapped = wrap_tool(s, "seeql_list_servers", impl)
        assert wrapped() == {"ok": True}
        assert called == [1]

    def test_rejection_returns_structured_error(self):
        s = MCPSafety(action_tools_enabled=False)
        wrapped = wrap_tool(s, "seeql_trigger_investigation", lambda: {"ran": True})
        result = wrapped()
        assert result["rejected_by"] == "mcp_safety"
        assert "action_tools_enabled" in result["error"].lower() or \
               "action_tools_enabled" in result["error"]

    def test_successful_live_call_records(self):
        s = MCPSafety(action_tools_enabled=True, allow_explain_query=True)
        wrapped = wrap_tool(s, "seeql_get_live_processlist", lambda: [{"pid": 1}])
        out = wrapped()
        assert out == [{"pid": 1}]
        snap = s.snapshot()
        assert snap["live_used"] == 1

    def test_exception_captured_as_tool_error(self):
        s = MCPSafety()
        def blow_up():
            raise RuntimeError("kapow")
        wrapped = wrap_tool(s, "seeql_list_servers", blow_up)
        out = wrapped()
        assert out["rejected_by"] == "tool_exception"
        assert "kapow" in out["error"]
