"""
SeeQL doctor — diagnostic command that makes environment state legible.

Runs 7 checks against the local environment and reports pass/fail with
actionable fix suggestions drawn from the E001–E010 error catalog.

Exit codes:
    0 — all checks passed
    N>0 — N checks failed (exit code = failure count, capped at 99)

Each check is independent: one failure doesn't skip the rest. The output
is a sketch-aesthetic-adjacent plain text report:

    SeeQL doctor
    ============
    [PASS] MySQL reachable            prod.example.com:3306
    [PASS] performance_schema enabled  ON
    [FAIL] dba_agent has PROCESS grant missing
           → Run: GRANT PROCESS ON *.* TO 'dba_agent'@'...';
    ...
    6/7 checks passed.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
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

    def format(self, width: int) -> str:
        status = "[PASS]" if self.passed else "[FAIL]"
        line = f"{status} {self.name:<{width}} {self.detail}"
        if not self.passed and self.error_code:
            err = errors.CATALOG.get(self.error_code)
            if err:
                line += f"\n       → {err.fix}"
                line += f"\n       → see {err.docs_url}"
        return line


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_config_loads() -> CheckResult:
    """E004 — Can we even parse the config?"""
    try:
        from config import get_config
        cfg = get_config()
        host = cfg.get("production_db", {}).get("host", "?")
        return CheckResult(
            name="Config loads",
            passed=True,
            detail=f"production_db.host = {host}",
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
        from config import get_config
        cfg = get_config().get("production_db", {})
        host = cfg.get("host", "?")
        port = cfg.get("port", 3306)
        ok = check_prod_connection()
        if ok:
            return CheckResult(
                name="Production MySQL reachable",
                passed=True,
                detail=f"{host}:{port} as {cfg.get('user', '?')}",
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


def check_gcp_creds() -> CheckResult:
    """E003 — Is GCP ADC configured for Cloud Monitoring + Vertex AI?"""
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
    """E009 — Is any LLM backend configured?"""
    try:
        from agent.llm_agent import _detect_backend
        from config import get_config
        backend = _detect_backend(get_config().get("agent", {}))
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
    failures = sum(1 for r in results if not r.passed)
    passed = len(results) - failures
    print(f"{passed}/{len(results)} checks passed.")

    if failures == 0:
        print("\n✓ SeeQL is healthy and ready to run.")
    else:
        print(f"\n✗ {failures} check(s) failed — see fix suggestions above.")
        print("  Once fixed, re-run: seeql doctor")

    return min(failures, 99)
