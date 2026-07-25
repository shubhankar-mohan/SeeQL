"""
SeeQL doctor — diagnostic command that makes environment state legible.

Runs 9 checks against the local environment and reports pass/fail/skip with
actionable fix suggestions drawn from the E001–E010 error catalog. Checks
that don't apply to this install (no GCP configured, LLM agent disabled)
report SKIP rather than FAIL — a healthy install that never opted into those
features should still be able to exit 0.

Exit codes:
    0 — all applicable checks passed (skipped checks don't count either way)
    N>0 — N checks failed (exit code = failure count, capped at 99)

Each check is independent: one failure doesn't skip the rest. The output
is a sketch-aesthetic-adjacent plain text report:

    SeeQL doctor
    ============
    [PASS] MySQL reachable            prod.example.com:3306
    [PASS] performance_schema enabled  ON
    [FAIL] dba_agent has PROCESS grant missing
           → Run: GRANT PROCESS ON *.* TO 'dba_agent'@'...';
    [SKIP] GCP credentials (ADC)       gcp not configured
    ...
    6 passed, 1 skipped, 0 failed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from seeql import errors


# ---------------------------------------------------------------------------
# Check result type
# ---------------------------------------------------------------------------
@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    error_code: str | None = None  # For failed checks, points at E0XX in the catalog
    skipped: bool = False  # Check doesn't apply to this install — not a failure

    def format(self, width: int) -> str:
        if self.skipped:
            status = "[SKIP]"
        elif self.passed:
            status = "[PASS]"
        else:
            status = "[FAIL]"
        line = f"{status} {self.name:<{width}} {self.detail}"
        if not self.passed and not self.skipped and self.error_code:
            err = errors.CATALOG.get(self.error_code)
            if err:
                line += f"\n       → {err.fix}"
                line += f"\n       → see {err.docs_url}"
        return line


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def _default_server_target() -> tuple[str, int, str]:
    """(host, port, user) for the default server in the registry.

    This is the server `check_prod_connection()` actually tests. Reading it from
    the registry (rather than the legacy `production_db:` config section) keeps
    doctor's output honest for installs configured the documented way with a
    `servers:` block — the registry handles both shapes.
    """
    from config.server_registry import get_server_registry
    reg = get_server_registry()
    srv = reg.get_server(reg.get_default_server_id())
    db = (srv.db_config if srv else {}) or {}
    return db.get("host", "?"), db.get("port", 3306), db.get("user", "?")


def check_config_loads() -> CheckResult:
    """E004 — Can we even parse the config?"""
    try:
        from config import get_config
        get_config()
        host, port, _ = _default_server_target()
        return CheckResult(
            name="Config loads",
            passed=True,
            detail=f"default server -> {host}:{port}",
        )
    except Exception as e:
        return CheckResult(
            name="Config loads",
            passed=False,
            detail=str(e)[:60],
            error_code="E004",
        )


def check_mon_db_writable() -> CheckResult:
    """E008 — Is the SQLite monitoring DB writable?"""
    try:
        from config import get_config
        path = Path(get_config().get("monitoring_db", {}).get("path", "data/mysql_monitor.db"))
        parent = path.parent
        if not parent.exists():
            return CheckResult(
                name="Monitoring DB writable",
                passed=False,
                detail=f"parent dir {parent} missing",
                error_code="E008",
            )
        # Check free space
        stat = shutil.disk_usage(parent)
        free_mb = stat.free // (1024 * 1024)
        if free_mb < 100:
            return CheckResult(
                name="Monitoring DB writable",
                passed=False,
                detail=f"only {free_mb} MB free on {parent}",
                error_code="E008",
            )
        # Try to touch a test file
        test_file = parent / ".doctor_test"
        try:
            test_file.write_text("ok")
            test_file.unlink()
        except OSError as e:
            return CheckResult(
                name="Monitoring DB writable",
                passed=False,
                detail=f"write failed: {e}",
                error_code="E008",
            )
        return CheckResult(
            name="Monitoring DB writable",
            passed=True,
            detail=f"{path} ({free_mb} MB free)",
        )
    except Exception as e:
        return CheckResult(
            name="Monitoring DB writable",
            passed=False,
            detail=str(e)[:60],
            error_code="E008",
        )


def check_mon_schema_current() -> CheckResult:
    """Schema initialized with the Phase 1 tables?"""
    try:
        from storage.connection import get_mon_reader
        with get_mon_reader() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('anomaly_events', 'incident_windows')"
            ).fetchall()
        count = len(rows)
        if count == 2:
            return CheckResult(
                name="Schema has incident tables",
                passed=True,
                detail="anomaly_events + incident_windows present",
            )
        return CheckResult(
            name="Schema has incident tables",
            passed=False,
            detail=f"only {count}/2 Phase 1 tables — run `seeql init-db`",
            error_code="E008",
        )
    except Exception as e:
        return CheckResult(
            name="Schema has incident tables",
            passed=False,
            detail=str(e)[:60],
            error_code="E008",
        )


def check_prod_reachable() -> CheckResult:
    """E001 / E006 — Can we log in to the production MySQL?"""
    try:
        from storage.connection import check_prod_connection
        host, port, user = _default_server_target()
        ok = check_prod_connection()
        if ok:
            return CheckResult(
                name="Production MySQL reachable",
                passed=True,
                detail=f"{host}:{port} as {user}",
            )
        return CheckResult(
            name="Production MySQL reachable",
            passed=False,
            detail=f"{host}:{port} — check_prod_connection returned False",
            error_code="E006",
        )
    except Exception as e:
        msg = str(e).lower()
        if "access denied" in msg or "1045" in msg:
            code = "E001"
        elif "timed out" in msg or "2003" in msg or "can't connect" in msg:
            code = "E006"
        else:
            code = "E006"
        return CheckResult(
            name="Production MySQL reachable",
            passed=False,
            detail=str(e)[:60],
            error_code=code,
        )


def check_performance_schema() -> CheckResult:
    """E002 — Is performance_schema enabled on the target?"""
    try:
        from storage.connection import get_prod_connection
        with get_prod_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SHOW VARIABLES LIKE 'performance_schema'")
            row = cursor.fetchone()
        if row and str(row[1]).upper() == "ON":
            return CheckResult(
                name="performance_schema enabled",
                passed=True,
                detail="ON",
            )
        return CheckResult(
            name="performance_schema enabled",
            passed=False,
            detail=f"current value: {row[1] if row else 'unknown'}",
            error_code="E002",
        )
    except Exception as e:
        return CheckResult(
            name="performance_schema enabled",
            passed=False,
            detail=str(e)[:60],
            error_code="E002",
        )


def check_perf_schema_consumers() -> CheckResult:
    """Are the performance_schema *consumers* SeeQL's collectors need turned on?

    `performance_schema enabled` (above) only proves the feature flag is on.
    Cloud SQL and some hardened installs ship with performance_schema ON but
    individual consumers OFF — which silently starves query_digests,
    wait_events, and execution_stages of data while every other doctor check
    stays green. This is exactly the "green doctor sitting on empty digest
    data" failure mode this check exists to catch.
    """
    try:
        from storage.connection import get_prod_connection
        from collectors.queries import DOCTOR_CONSUMERS
        with get_prod_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(DOCTOR_CONSUMERS)
            rows = cursor.fetchall()
        if not rows:
            return CheckResult(
                name="performance_schema consumers enabled",
                passed=False,
                detail="setup_consumers query returned no rows — unexpected",
            )
        disabled = [str(name) for name, enabled in rows if str(enabled).upper() != "YES"]
        if disabled:
            fix = "; ".join(
                "UPDATE performance_schema.setup_consumers SET ENABLED='YES' "
                f"WHERE NAME='{name}'"
                for name in disabled
            )
            return CheckResult(
                name="performance_schema consumers enabled",
                passed=False,
                detail=(
                    f"disabled: {', '.join(disabled)}\n"
                    f"       → Run as an admin user (dba_agent typically can't): {fix};"
                ),
            )
        return CheckResult(
            name="performance_schema consumers enabled",
            passed=True,
            detail=f"all {len(rows)} required consumers ON",
        )
    except Exception as e:
        # Mirrors check_performance_schema's pattern: a connection failure
        # (or a query failure that stems from performance_schema itself
        # being unreachable/disabled) is reported under the same E002
        # catalog entry, so the "-> Run: ..." remediation isn't dropped.
        return CheckResult(
            name="performance_schema consumers enabled",
            passed=False,
            detail=str(e)[:60],
            error_code="E002",
        )


def check_stage_instruments() -> CheckResult:
    """INFO — execution_stages collector needs setup_instruments 'stage/%' ON.

    Non-failing by design: stage instrumentation is optional and adds
    overhead, and SeeQL degrades gracefully without it (the execution_stages
    collector just returns no rows). This surfaces the tradeoff instead of
    leaving an operator to discover a silently-empty
    execution_stage_snapshots table on their own.
    """
    try:
        from storage.connection import get_prod_connection
        from collectors.queries import DOCTOR_STAGE_INSTRUMENTS
        with get_prod_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(DOCTOR_STAGE_INSTRUMENTS)
            rows = cursor.fetchall()
        total = len(rows)
        enabled = sum(1 for _, e in rows if str(e).upper() == "YES")
        if total == 0:
            detail = "no stage/% instruments found on this server"
        elif enabled == 0:
            detail = (
                f"0/{total} stage/% instruments enabled — execution_stages "
                "collector will see no data (informational only, not required)"
            )
        else:
            detail = f"{enabled}/{total} stage/% instruments enabled"
        return CheckResult(
            name="Execution-stage instruments (info)",
            passed=True,
            detail=detail,
        )
    except Exception as e:
        # Informational only — a query/connection failure here must never
        # contribute to doctor's failure count.
        return CheckResult(
            name="Execution-stage instruments (info)",
            passed=True,
            detail=f"could not check (informational only): {str(e)[:60]}",
        )


def check_gcp_creds() -> CheckResult:
    """E003 — Is GCP ADC configured for Cloud Monitoring + Vertex AI?

    SKIPs (rather than FAILs) when this install never opted into GCP —
    i.e. `gcp.project_id` is unset or still the stock `your-...` placeholder
    shipped in settings.yaml. A non-GCP install has no way to satisfy this
    check and shouldn't be penalized for it.
    """
    from config import get_config
    project_id = get_config().get("gcp", {}).get("project_id")
    if not project_id or project_id.startswith("your-"):
        return CheckResult(
            name="GCP credentials (ADC)",
            passed=False,
            skipped=True,
            detail="gcp not configured",
        )
    adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not adc:
        return CheckResult(
            name="GCP credentials (ADC)",
            passed=False,
            detail="GOOGLE_APPLICATION_CREDENTIALS not set",
            error_code="E003",
        )
    if adc.startswith("${"):
        return CheckResult(
            name="GCP credentials (ADC)",
            passed=False,
            detail=f"unresolved placeholder: {adc}",
            error_code="E003",
        )
    p = Path(adc)
    if not p.exists():
        return CheckResult(
            name="GCP credentials (ADC)",
            passed=False,
            detail=f"file not found: {adc}",
            error_code="E003",
        )
    return CheckResult(
        name="GCP credentials (ADC)",
        passed=True,
        detail=f"{p.name} ({p.stat().st_size} bytes)",
    )


def check_llm_backend() -> CheckResult:
    """E009 — Is any LLM backend configured?

    SKIPs (rather than FAILs) when `agent.enabled` is false — an install
    that intentionally runs SeeQL without the LLM layer (collection +
    alerting only) shouldn't be penalized for not having Gemini/Claude/OpenAI
    credentials it never asked for.
    """
    try:
        from agent.llm_agent import _detect_backend
        from config import get_config
        agent_config = get_config().get("agent", {})
        if not agent_config.get("enabled"):
            return CheckResult(
                name="LLM backend configured",
                passed=False,
                skipped=True,
                detail="agent disabled",
            )
        backend = _detect_backend(agent_config)
        if backend is None:
            return CheckResult(
                name="LLM backend configured",
                passed=False,
                detail="no Gemini or Claude credentials found",
                error_code="E009",
            )
        return CheckResult(
            name="LLM backend configured",
            passed=True,
            detail=f"{backend['type']} / {backend['model']}",
        )
    except Exception as e:
        return CheckResult(
            name="LLM backend configured",
            passed=False,
            detail=str(e)[:60],
            error_code="E009",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
CHECKS = [
    check_config_loads,
    check_mon_db_writable,
    check_mon_schema_current,
    check_prod_reachable,
    check_performance_schema,
    check_perf_schema_consumers,
    check_stage_instruments,
    check_gcp_creds,
    check_llm_backend,
]


def run() -> int:
    """Run all checks and print a report. Returns the number of failures."""
    print("SeeQL doctor")
    print("=" * 60)

    results: list[CheckResult] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as e:
            # A check itself blew up — treat as a failure but don't crash
            results.append(CheckResult(
                name=check.__name__,
                passed=False,
                detail=f"check crashed: {e}"[:60],
            ))

    name_width = max(len(r.name) for r in results) + 2
    for r in results:
        print(r.format(name_width))

    print("=" * 60)
    failures = sum(1 for r in results if not r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    passed = len(results) - failures - skipped
    print(f"{passed} passed, {skipped} skipped, {failures} failed.")

    if failures == 0:
        print("\n✓ SeeQL is healthy and ready to run.")
    else:
        print(f"\n✗ {failures} check(s) failed — see fix suggestions above.")
        print("  Once fixed, re-run: seeql doctor")

    return min(failures, 99)
