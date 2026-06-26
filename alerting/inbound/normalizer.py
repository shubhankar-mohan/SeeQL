"""
Shared normalization helpers used by every adapter.

`map_alert_type` collapses provider-specific alert names into SeeQL's
canonical trigger vocabulary. `resolve_server_id` walks the usual locations
where a provider may have stuck the target server identifier, falling back
to a config-level default per provider and finally to 'default'.
"""

from typing import Any, Mapping

from alerting.inbound.models import CANONICAL_ALERT_TYPES


# Heuristic mapping from provider labels / policy names to canonical alert
# types. The first matching key (case-insensitive, substring match) wins.
# Order matters — more specific matches first.
_HEURISTIC_MAPPINGS: list[tuple[str, str]] = [
    ("lock_cascade", "lock_cascade"),
    ("lock-cascade", "lock_cascade"),
    ("lock wait", "lock_cascade"),
    ("lock_wait", "lock_cascade"),
    ("deadlock", "deadlock_detected"),
    ("missing_index", "missing_index"),
    ("missing index", "missing_index"),
    ("full_table_scan", "missing_index"),
    ("full table scan", "missing_index"),
    ("no_index_used", "missing_index"),
    ("slow_query", "missing_index"),
    ("slow query", "missing_index"),
    ("query_regression", "query_regression"),
    ("query regression", "query_regression"),
    ("threads_running", "threads_running_spike"),
    ("threads running", "threads_running_spike"),
    ("high_cpu", "high_cpu"),
    ("high cpu", "high_cpu"),
    ("cpu_util", "high_cpu"),
    ("cpu utilization", "high_cpu"),
    ("ddl_change", "ddl_change"),
    ("ddl change", "ddl_change"),
    ("schema_change", "ddl_change"),
]


def map_alert_type(raw: str | None, hints: Mapping[str, Any] | None = None) -> str:
    """
    Map a raw provider alert identifier (e.g. GCP policy name, PagerDuty
    title, Grafana label) to the canonical SeeQL trigger vocabulary.

    If `hints` contains an explicit 'alert_type' field that's already a valid
    canonical type, it wins immediately — this lets adapters pass through
    well-formed webhooks without re-running the heuristic search.

    Falls back to 'default' when nothing matches.
    """
    hints = hints or {}
    hint_type = hints.get("alert_type")
    if isinstance(hint_type, str) and hint_type in CANONICAL_ALERT_TYPES:
        return hint_type

    if not raw:
        return "default"

    # Normalize separators so "high-cpu-policy" and "high_cpu_rule" both
    # match our "high cpu" needle. Collapse any run of whitespace to one
    # space so needles like "high cpu" match "high    cpu" too.
    text = str(raw).lower().replace("-", " ").replace("_", " ")
    text = " ".join(text.split())
    for needle, canonical in _HEURISTIC_MAPPINGS:
        normalized_needle = needle.replace("-", " ").replace("_", " ")
        normalized_needle = " ".join(normalized_needle.split())
        if normalized_needle in text:
            return canonical
    return "default"


def resolve_server_id(
    payload: Mapping[str, Any],
    provider: str,
    provider_default: str | None = None,
) -> str:
    """
    Resolve the target server_id from a webhook payload.

    Search order:
      1. Provider-specific label paths (labels.server_id, labels.instance, etc.)
      2. Top-level payload keys: server_id, instance, host
      3. Provider-level config default (settings.yaml)
      4. Hardcoded 'default'
    """
    # 1. Labels (common to GCP / Grafana)
    labels = payload.get("labels") or {}
    if isinstance(labels, dict):
        for key in ("server_id", "instance", "host", "database_id"):
            val = labels.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()

    # 2. Top-level keys
    for key in ("server_id", "instance", "host"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    # 3. Provider-level config default
    if provider_default and str(provider_default).strip():
        return str(provider_default).strip()

    # 4. Final fallback
    return "default"


def coerce_severity(raw: str | None) -> str:
    """Normalize provider severity labels to SeeQL's three levels."""
    if not raw:
        return "warning"
    s = str(raw).lower().strip()
    if s in ("critical", "crit", "fatal", "emergency", "error", "p1", "sev1"):
        return "critical"
    if s in ("warning", "warn", "major", "p2", "sev2", "high"):
        return "warning"
    if s in ("info", "informational", "notice", "low", "p3", "p4", "sev3"):
        return "info"
    return "warning"
