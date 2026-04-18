"""
SeeQL error catalog (Phase 2.5).

Structured error class + 10 well-known error codes covering the most common
startup / runtime failures. Each error carries a Rust-style (Tier 2)
message: problem + cause + fix + docs URL. Callers raise `SeeQLError` by
code and the CLI formats it before exiting non-zero.

The catalog is intentionally small (E001..E010) — the goal is "every new
user hits a clear message for the top 10 things that can go wrong," not
"exhaustively enumerate every failure mode."
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SeeQLError(Exception):
    """A user-facing error with structured fields for CLI formatting."""

    code: str            # e.g. "E001"
    problem: str         # one-line summary of what went wrong
    cause: str           # why it happened
    fix: str             # what the user should do
    docs_url: str = ""   # optional deep link
    details: str = ""    # optional runtime context (appended verbatim)

    def __post_init__(self):
        super().__init__(self.problem)

    def format(self) -> str:
        """Render the error in the canonical Rust-style block."""
        lines = [
            "",
            f"error[{self.code}]: {self.problem}",
            f"  = cause: {self.cause}",
            f"  = fix:   {self.fix}",
        ]
        if self.docs_url:
            lines.append(f"  = docs:  {self.docs_url}")
        if self.details:
            lines.append(f"  = details: {self.details}")
        lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Canonical catalog
# ---------------------------------------------------------------------------
_DOCS = "https://github.com/shubhankar-mohan/SeeQL/blob/main/docs/errors/{code}.md"


def _url(code: str) -> str:
    return _DOCS.format(code=code)


CATALOG: dict[str, SeeQLError] = {
    "E001": SeeQLError(
        code="E001",
        problem="MySQL authentication failed",
        cause="The dba_agent user cannot log in — wrong password, wrong host "
              "restriction, or the user doesn't exist yet.",
        fix="Verify PROD_DB_PASSWORD is set correctly, and that "
            "`CREATE USER 'dba_agent'@'<host>' IDENTIFIED BY '...'` has been "
            "run with `GRANT SELECT, PROCESS ON *.* TO 'dba_agent'@'<host>'`.",
        docs_url=_url("E001"),
    ),
    "E002": SeeQLError(
        code="E002",
        problem="performance_schema is disabled on the target MySQL instance",
        cause="Most of SeeQL's collectors need performance_schema (query "
              "digests, lock waits, wait events, execution stages, table IO).",
        fix="Enable the Cloud SQL flag `performance_schema=on` and restart "
            "the instance. On self-managed MySQL, add "
            "`performance_schema=ON` to my.cnf and restart. SeeQL can run "
            "in limited mode without it, but most collectors will be disabled.",
        docs_url=_url("E002"),
    ),
    "E003": SeeQLError(
        code="E003",
        problem="GCP Application Default Credentials not configured",
        cause="SeeQL uses ADC for Cloud Monitoring (CPU/memory/disk) and "
              "Cloud Logging (slow query logs). Without ADC, those collectors "
              "and the Gemini/Vertex-Claude LLM backends cannot authenticate.",
        fix="Run `gcloud auth application-default login` on your dev box, or "
            "attach a service account to the GCE VM with roles "
            "`monitoring.viewer` + `logging.viewer` + `aiplatform.user`.",
        docs_url=_url("E003"),
    ),
    "E004": SeeQLError(
        code="E004",
        problem="Invalid config file",
        cause="`config/settings.yaml` or `settings.local.yaml` has a syntax "
              "error, a missing required key, or an env var that didn't "
              "substitute.",
        fix="Validate YAML with `python -c 'import yaml; "
            "yaml.safe_load(open(\"settings.local.yaml\"))'`. Compare your "
            "file against the stock `config/settings.yaml` for missing keys. "
            "Unsubstituted `${VAR_NAME}` placeholders usually mean the env "
            "var isn't exported.",
        docs_url=_url("E004"),
    ),
    "E005": SeeQLError(
        code="E005",
        problem="Required Cloud SQL flag is missing or wrong",
        cause="Cloud SQL flags `performance_schema`, `slow_query_log`, "
              "`innodb_monitor_enable`, or `long_query_time` are not set "
              "correctly — which breaks the corresponding collector.",
        fix="In Cloud SQL console or gcloud, set: "
            "`performance_schema=on`, `slow_query_log=on`, "
            "`long_query_time=1`, `innodb_monitor_enable=all`. Cloud SQL will "
            "restart the instance.",
        docs_url=_url("E005"),
    ),
    "E006": SeeQLError(
        code="E006",
        problem="MySQL connection timeout",
        cause="SeeQL couldn't reach the production MySQL instance within the "
              "configured `connect_timeout`. Usually a firewall or VPC "
              "misconfiguration.",
        fix="Verify the host/port in `production_db` is reachable "
            "(`nc -vz <host> 3306`). For Cloud SQL private IP, confirm the "
            "VM is in the same VPC and peering is established.",
        docs_url=_url("E006"),
    ),
    "E007": SeeQLError(
        code="E007",
        problem="Permission denied — dba_agent is missing required grants",
        cause="The monitoring user can connect but can't read the tables "
              "SeeQL needs. Typically missing SELECT on "
              "`performance_schema.*` or PROCESS.",
        fix="Run `GRANT SELECT, PROCESS ON *.* TO 'dba_agent'@'<host>';` "
            "and re-test with `seeql doctor`.",
        docs_url=_url("E007"),
    ),
    "E008": SeeQLError(
        code="E008",
        problem="SQLite monitoring database is full or not writable",
        cause="The configured `monitoring_db.path` is on a disk that's out "
              "of space, is read-only, or the process doesn't have write "
              "access.",
        fix="Check `df -h $(dirname <path>)` for space, `ls -la <path>` for "
            "permissions. Increase `monitoring_db.max_size_mb` if the "
            "auto-shrink retention is too aggressive for your workload.",
        docs_url=_url("E008"),
    ),
    "E009": SeeQLError(
        code="E009",
        problem="LLM API credentials invalid",
        cause="The configured Claude or Gemini backend rejected the request. "
              "Either the `ANTHROPIC_API_KEY` is wrong, the Vertex AI service "
              "account lacks `aiplatform.user`, or the configured model name "
              "doesn't exist in the chosen region.",
        fix="For Anthropic API: regenerate the key at console.anthropic.com. "
            "For Vertex AI: confirm the service account has `aiplatform.user` "
            "and that the model is available in `gcp.vertex_region`. You can "
            "set `agent.enabled: false` to run SeeQL without the LLM layer.",
        docs_url=_url("E009"),
    ),
    "E010": SeeQLError(
        code="E010",
        problem="Invalid time range for replay",
        cause="`seeql replay --from X --to Y` received timestamps that are "
              "not valid ISO 8601, or `to` is earlier than `from`, or the "
              "range is empty (zero-width window).",
        fix="Use ISO 8601 timestamps like `2026-04-10T03:00:00` or "
            "`2026-04-10T03:00:00+00:00`. Make sure `--to` is after `--from`. "
            "For an auto-detected window, use `seeql replay --incident <id>` "
            "or `seeql replay --latest` instead.",
        docs_url=_url("E010"),
    ),
}


def get(code: str, details: str = "") -> SeeQLError:
    """
    Look up an error by code, optionally attaching runtime details.

    Usage:
        raise errors.get("E001", details=str(original_exception))
    """
    base = CATALOG.get(code)
    if base is None:
        return SeeQLError(
            code=code,
            problem=f"Unknown error code {code}",
            cause="This is a bug in SeeQL — the code wasn't in the catalog.",
            fix="File an issue at https://github.com/shubhankar-mohan/SeeQL/issues",
            docs_url="",
            details=details,
        )
    # Dataclass-copy with runtime details
    return SeeQLError(
        code=base.code,
        problem=base.problem,
        cause=base.cause,
        fix=base.fix,
        docs_url=base.docs_url,
        details=details,
    )


__all__ = ["SeeQLError", "CATALOG", "get"]
