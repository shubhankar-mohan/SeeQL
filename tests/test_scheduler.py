"""Tests for scheduler/runner.py — per-server concurrency + circuit breaker.

P1b-4: `_run_fast` / `_run_medium` / `_run_slow` used to iterate servers
SEQUENTIALLY. A single unreachable server can burn most of a collection
interval retrying (MAX_RETRIES retries x connect_timeout, per collector),
so under the old code it could starve every other server in the fleet
within one cycle. Fixed with:

  1. A ThreadPoolExecutor so each server's collectors run in their own
     worker thread — one stuck server can't delay the others.
  2. A per-server circuit breaker that stops even trying a server once it
     fails EVERY collector for 3 consecutive cycles, until
     `circuit_reset_cycles` more cycles have passed.
"""

import threading
import time

import pytest

import scheduler.runner as runner


class _FakeServer:
    """Stand-in for a ServerContext. _run_loop_over_servers only ever reads
    .server_id off whatever _get_server_contexts() returns."""

    def __init__(self, server_id: str):
        self.server_id = server_id


FAIL_SID = "server-a-unreachable"
OK_SID = "server-b-healthy"


@pytest.fixture(autouse=True)
def reset_fleet_state():
    """_server_failures / _server_skip_until / _cycle_count are module-level
    singletons (they have to be — they must survive across separate
    APScheduler job invocations). Isolate every test from the others."""
    runner._server_failures.clear()
    runner._server_skip_until.clear()
    runner._cycle_count = 0
    yield
    runner._server_failures.clear()
    runner._server_skip_until.clear()
    runner._cycle_count = 0


def _wire_two_servers(monkeypatch, fake_run_fast_loop):
    """Point the fast loop at two fake servers and stub out the
    alerting/Prometheus tail calls, which need their own config/DB and are
    unrelated to fleet-dispatch behavior."""
    contexts = [_FakeServer(FAIL_SID), _FakeServer(OK_SID)]
    monkeypatch.setattr(runner, "_get_server_contexts", lambda: contexts)
    monkeypatch.setattr(runner, "run_fast_loop", fake_run_fast_loop)
    monkeypatch.setattr(runner, "_run_alerts", lambda *a, **kw: None)
    monkeypatch.setattr(runner, "_update_prom_metrics", lambda: None)


class TestFastLoopFleetResilience:
    """Two servers, A always fails (ConnectionError, every collector),
    B always succeeds."""

    def test_one_unreachable_server_does_not_starve_the_other(self, monkeypatch):
        calls: list[str] = []
        # Both fake collectors rendezvous here. Under sequential dispatch, B
        # is never invoked while A is still blocked waiting for it, so the
        # barrier only clears via its 2s timeout — that's the RED signal.
        # Under concurrent dispatch both threads arrive together and it
        # clears almost instantly.
        barrier = threading.Barrier(2, timeout=2.0)

        def fake_run_fast_loop(ctx):
            calls.append(ctx.server_id)
            barrier.wait()
            if ctx.server_id == FAIL_SID:
                raise ConnectionError(f"could not connect to {ctx.server_id}")
            return {"processlist": True}

        _wire_two_servers(monkeypatch, fake_run_fast_loop)

        start = time.monotonic()
        runner._run_fast()
        elapsed = time.monotonic() - start

        assert OK_SID in calls
        assert FAIL_SID in calls
        assert elapsed < 1.0, (
            f"_run_fast() took {elapsed:.2f}s — server B must run concurrently "
            f"with server A, not be starved behind it"
        )

    def test_circuit_opens_after_three_consecutive_all_fail_cycles(self, monkeypatch):
        calls: list[str] = []

        def fake_run_fast_loop(ctx):
            calls.append(ctx.server_id)
            if ctx.server_id == FAIL_SID:
                raise ConnectionError("simulated: server unreachable")
            return {"processlist": True}

        _wire_two_servers(monkeypatch, fake_run_fast_loop)
        monkeypatch.setattr(runner, "_get_circuit_reset_cycles", lambda: 10)

        for _ in range(3):
            runner._run_fast()

        assert calls.count(FAIL_SID) == 3, "all 3 cycles must have actually attempted A"
        assert runner._is_circuit_open(FAIL_SID, runner._cycle_count)
        assert not runner._is_circuit_open(OK_SID, runner._cycle_count)

        calls.clear()
        runner._run_fast()  # 4th cycle: A's circuit must now be open.

        assert FAIL_SID not in calls, "circuit-open server's collectors must not be invoked"
        assert OK_SID in calls, "B must be unaffected by A's open circuit"

    def test_circuit_stays_open_until_reset_cycles_pass(self, monkeypatch):
        calls: list[str] = []

        def fake_run_fast_loop(ctx):
            calls.append(ctx.server_id)
            if ctx.server_id == FAIL_SID:
                raise ConnectionError("simulated: server unreachable")
            return {"processlist": True}

        _wire_two_servers(monkeypatch, fake_run_fast_loop)
        monkeypatch.setattr(runner, "_get_circuit_reset_cycles", lambda: 3)

        for _ in range(3):
            runner._run_fast()  # cycles 1-3: trips at cycle 3, skip_until = 6

        for _ in range(2):
            calls.clear()
            runner._run_fast()  # cycles 4-5: inside the skip window
            assert FAIL_SID not in calls

        calls.clear()
        runner._run_fast()  # cycle 6: skip_until reached -> retried
        assert FAIL_SID in calls, "A must be retried once circuit_reset_cycles have passed"

    def test_success_resets_failure_streak(self, monkeypatch):
        """A success anywhere in the streak resets the counter — the
        breaker only trips on *consecutive* all-fail cycles."""
        outcomes = iter([True, True, False, False])  # succeed, succeed, fail, fail

        def fake_run_fast_loop(ctx):
            if ctx.server_id == FAIL_SID:
                if next(outcomes):
                    return {"processlist": True}
                raise ConnectionError("simulated: server unreachable")
            return {"processlist": True}

        _wire_two_servers(monkeypatch, fake_run_fast_loop)
        monkeypatch.setattr(runner, "_get_circuit_reset_cycles", lambda: 10)

        for _ in range(4):
            runner._run_fast()

        # Only 2 consecutive failures (cycles 3-4) — never reached 3, so the
        # circuit must still be closed.
        assert not runner._is_circuit_open(FAIL_SID, runner._cycle_count)
