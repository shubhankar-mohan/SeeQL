"""
Webhook-triggered root-cause investigator.

Entry: `run_investigation(investigation_id)` — invoked by APScheduler as an
ad-hoc one-shot job the webhook router enqueues. The orchestrator walks:

    queued → phase1 → phase2 → phase3 → completed
                                         aborted
                                         load_guard_paused

Phase 1 (≤10s, zero new MySQL queries):
    state report + mini-timeline + missing-index correlator
    → hypothesis finding, decide whether to proceed to Phase 2

Phase 2 (≤120s, budgeted):
    run_llm_analysis with WEBHOOK_INVESTIGATION_PROMPT + tool_budget
    → root-cause finding

Phase 3 (scheduled separately via scheduler.add_job + DateTrigger):
    implemented in CP5.

The module is import-light by default; Phase 2 imports the LLM agent only
on use so tests that don't exercise the LLM path don't drag in the SDK.
"""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from storage import writer
from storage.connection import get_mon_reader
from alerting.inbound.models import InboundAlert
from alerting.budget import Budget
from alerting.correlators.missing_index import (
    correlate_missing_index,
    MissingIndexCorrelation,
)

logger = logging.getLogger(__name__)


# Short window we pre-build for Phase 1 so the LLM doesn't have to scan
# the whole day. 12 minutes is enough to cover the sampling cadence + any
# reasonable fire-to-receive latency from the provider.
TIMELINE_WINDOW_MINUTES = 12


# ---------------------------------------------------------------------------
# Investigator config — read lazily from global settings
# ---------------------------------------------------------------------------

def _inv_config() -> dict:
    try:
        from config import get_config
        return dict(get_config().get("investigator") or {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def run_investigation(investigation_id: int) -> dict:
    """
    Top-level orchestrator. Scheduler calls this with the new investigation's
    row id. Returns a dict summary — primarily for tests; the real output
    is the mutated rows in `investigations` / `investigation_findings`.
    """
    inv = _load_investigation(investigation_id)
    if inv is None:
        logger.warning(f"run_investigation({investigation_id}): not found")
        return {"status": "missing"}

    alert = _load_alert_for(inv)
    if alert is None:
        logger.warning(f"run_investigation({investigation_id}): inbound_alert missing")
        _terminal(investigation_id, status="aborted", abort_reason="missing_alert")
        return {"status": "aborted", "reason": "missing_alert"}

    # Phase 1 — triage
    _transition(investigation_id, status="phase1")
    try:
        triage = _phase1_triage(inv, alert)
    except Exception as e:
        logger.exception(f"Phase 1 failed for investigation {investigation_id}: {e}")
        _terminal(investigation_id, status="aborted", abort_reason=f"phase1_error: {e}")
        return {"status": "aborted", "reason": "phase1_error"}

    if not triage.get("proceed", True):
        # Clean, low-severity exit — still record what we know and dispatch.
        _terminal(
            investigation_id,
            status="completed",
            root_cause_summary=triage.get("hypothesis") or "No issue detected",
            confidence=0.4,
        )
        _dispatch_findings(investigation_id, alert, triage_only=True, triage=triage)
        return {"status": "completed", "phase": 1}

    # Phase 2 — LLM
    _transition(investigation_id, status="phase2")
    phase2 = _phase2_investigate(inv, alert, triage)

    # Decide whether to kick off Phase 3. If confidence is already very high
    # AND the suspect digests' clearance condition is known, we might skip P3.
    config = _inv_config()
    confidence_threshold = float(config.get("confidence_completion_threshold", 0.8) or 0.8)
    if phase2.get("confidence", 0.0) >= confidence_threshold:
        _terminal(
            investigation_id,
            status="completed",
            root_cause_summary=phase2.get("root_cause") or triage.get("hypothesis"),
            confidence=phase2.get("confidence", 0.0),
            analysis_id=phase2.get("analysis_id"),
        )
        _dispatch_findings(investigation_id, alert, phase2=phase2, triage=triage)
        return {"status": "completed", "phase": 2}

    # Otherwise schedule Phase 3. The sampler (CP5) is responsible for
    # load-guard, budget, clearance, and terminal transitions.
    _schedule_phase3(investigation_id)
    _transition(
        investigation_id,
        status="phase3",
        analysis_id=phase2.get("analysis_id"),
        root_cause_summary=phase2.get("root_cause") or triage.get("hypothesis"),
        confidence=phase2.get("confidence"),
    )
    return {"status": "phase3_scheduled", "phase": 2}


# ---------------------------------------------------------------------------
# Phase 1 — triage (zero new MySQL queries)
# ---------------------------------------------------------------------------

def _phase1_triage(inv: dict, alert: InboundAlert) -> dict:
    """
    Build initial hypothesis from SQLite-only data:
      - Current state report
      - Mini chronological timeline around alert.fired_at
      - Missing-index correlation
    """
    server_id = inv["server_id"]
    fired_at = _parse_dt(alert.fired_at) or datetime.now(timezone.utc)
    window_start = (fired_at - timedelta(minutes=TIMELINE_WINDOW_MINUTES)).isoformat()
    window_end = (fired_at + timedelta(minutes=2)).isoformat()

    # State report — resilient: failure => empty dict / stub markdown.
    state_md = _safe_state_report_markdown(server_id)

    # Timeline — reuse replay's internal builder (single source of truth).
    timeline_md, events_by_category = _safe_build_timeline(
        server_id, window_start, window_end
    )

    # Missing-index correlation
    correlation = correlate_missing_index(
        server_id=server_id,
        window_start=window_start,
        window_end=window_end,
    )

    # Link to an open incident_window if one overlaps.
    incident_window_id = _find_overlapping_incident_window(
        server_id, window_start, window_end
    )
    if incident_window_id:
        writer.update_investigation(
            inv["id"], incident_window_id=incident_window_id
        )

    # Decide whether to proceed. "Transient" = all signals quiet AND alert is
    # not critical. Otherwise always proceed.
    proceed = _should_proceed(alert, events_by_category, correlation)

    hypothesis = _phase1_hypothesis(alert, correlation, events_by_category)

    content = {
        "hypothesis": hypothesis,
        "timeline_window": {"start": window_start, "end": window_end},
        "events_by_category": events_by_category,
        "missing_index_correlation": correlation.to_dict(),
        "proceed_to_phase2": proceed,
    }
    writer.write_investigation_findings([{
        "investigation_id": inv["id"],
        "created_at": _now_iso(),
        "phase": 1,
        "kind": "hypothesis",
        "severity": alert.severity,
        "content": json.dumps(content, default=str),
    }])

    # If the correlator found strong evidence, write a separate correlation
    # finding so Phase 2 + the dashboard can surface it quickly.
    if correlation.has_findings:
        writer.write_investigation_findings([{
            "investigation_id": inv["id"],
            "created_at": _now_iso(),
            "phase": 1,
            "kind": "correlation",
            "severity": alert.severity,
            "content": json.dumps(correlation.to_dict(), default=str),
        }])

    return {
        "hypothesis": hypothesis,
        "proceed": proceed,
        "state_md": state_md,
        "timeline_md": timeline_md,
        "events_by_category": events_by_category,
        "correlation": correlation,
        "window_start": window_start,
        "window_end": window_end,
    }


def _should_proceed(
    alert: InboundAlert,
    events_by_category: dict[str, int],
    correlation: MissingIndexCorrelation,
) -> bool:
    """
    Proceed to Phase 2 unless the timeline and correlator are BOTH quiet
    AND the alert is not critical. Critical alerts always proceed.
    """
    if alert.severity == "critical":
        return True
    if correlation.has_findings:
        return True
    total_signals = sum(v for v in events_by_category.values() if isinstance(v, int))
    if total_signals >= 1:
        return True
    return False


def _phase1_hypothesis(
    alert: InboundAlert,
    correlation: MissingIndexCorrelation,
    events_by_category: dict[str, int],
) -> str:
    top = correlation.top_evidence
    if top and correlation.has_findings:
        table = top.table_name or "an unknown table"
        dropped = f" (recently dropped index `{top.dropped_index_hint}`)" if top.dropped_index_hint else ""
        return (
            f"Likely missing-index regression on `{table}` — digest `{top.digest}` "
            f"scans {top.rows_examined:,} rows to return {top.rows_sent:,}{dropped}."
        )
    if events_by_category.get("lock_waits", 0) > 0:
        return "Lock contention observed near the alert window — investigating cascade."
    if events_by_category.get("deadlocks", 0) > 0:
        return "Deadlock(s) recorded in the alert window — investigating contended tables."
    if events_by_category.get("ddl_changes", 0) > 0:
        return "Recent DDL change overlaps the alert window — investigating regression."
    return f"No standout SQLite signals; proceeding to live Phase 2 for `{alert.alert_type}`."


# ---------------------------------------------------------------------------
# Phase 2 — LLM with tool budget
# ---------------------------------------------------------------------------

def _phase2_investigate(inv: dict, alert: InboundAlert, triage: dict) -> dict:
    """
    Call run_llm_analysis with a bounded tool budget. On missing LLM backend
    we write a best-effort finding from the Phase 1 data and return without
    error — resilience is more important than coverage in a live incident.
    """
    config = _inv_config()
    budget = Budget(
        investigation_id=inv["id"],
        live_tool_cap=int(config.get("phase2_live_tool_cap", 10) or 10),
        explain_cap=int(config.get("phase2_explain_cap", 2) or 2),
        queries_per_minute=int(config.get("query_budget_per_minute", 20) or 20),
    )

    prompt = _build_phase2_prompt(alert, triage, budget)

    try:
        from agent.llm_agent import run_llm_analysis
        result = run_llm_analysis(
            prompt,
            analysis_type="investigation",
            server_id=inv["server_id"],
            tool_budget=budget,
            max_tool_rounds_override=int(config.get("phase2_max_tool_rounds", 8) or 8),
        )
    except RuntimeError as e:
        logger.info(f"Phase 2 LLM unavailable: {e}. Falling back to triage.")
        return _phase2_fallback(inv, alert, triage, reason=str(e))
    except Exception as e:
        logger.exception(f"Phase 2 LLM errored: {e}")
        return _phase2_fallback(inv, alert, triage, reason=f"llm_error: {e}")

    text = result.get("text") or ""
    severity = result.get("severity") or alert.severity
    analysis_id = result.get("analysis_id")

    root_cause = _extract_root_cause(text)
    confidence = _extract_confidence(text)

    # Persist the findings
    writer.write_investigation_findings([{
        "investigation_id": inv["id"],
        "created_at": _now_iso(),
        "phase": 2,
        "kind": "root_cause" if root_cause else "evidence",
        "severity": severity,
        "content": json.dumps({
            "llm_text": text,
            "analysis_id": analysis_id,
            "budget": budget.snapshot(),
        }, default=str),
    }])

    return {
        "root_cause": root_cause or triage.get("hypothesis"),
        "confidence": confidence,
        "severity": severity,
        "analysis_id": analysis_id,
        "llm_text": text,
    }


def _phase2_fallback(
    inv: dict, alert: InboundAlert, triage: dict, reason: str
) -> dict:
    writer.write_investigation_findings([{
        "investigation_id": inv["id"],
        "created_at": _now_iso(),
        "phase": 2,
        "kind": "evidence",
        "severity": alert.severity,
        "content": json.dumps({
            "llm_unavailable": True,
            "reason": reason,
            "fallback_hypothesis": triage.get("hypothesis"),
        }),
    }])
    return {
        "root_cause": triage.get("hypothesis"),
        "confidence": 0.4,
        "severity": alert.severity,
        "analysis_id": None,
        "llm_text": None,
    }


def _build_phase2_prompt(
    alert: InboundAlert, triage: dict, budget: Budget
) -> str:
    from agent.prompts import WEBHOOK_INVESTIGATION_PROMPT, INCIDENT_TRIGGERS

    trigger_instructions = INCIDENT_TRIGGERS.get(
        alert.alert_type, INCIDENT_TRIGGERS.get("webhook_generic") or INCIDENT_TRIGGERS["default"]
    )

    correlation: MissingIndexCorrelation = triage["correlation"]
    correlation_md = correlation.to_markdown()

    return WEBHOOK_INVESTIGATION_PROMPT.format(
        provider=alert.provider,
        alert_type=alert.alert_type,
        severity=alert.severity,
        fired_at=alert.fired_at,
        server_id=alert.server_id,
        alert_summary=alert.summary,
        trigger_instructions=trigger_instructions,
        missing_index_evidence=correlation_md,
        timeline=triage.get("timeline_md") or "_(no timeline events)_",
        state_report=triage.get("state_md") or "_(state report unavailable)_",
        live_tool_cap=budget.live_tool_cap,
        explain_cap=budget.explain_cap,
        timeline_window_minutes=TIMELINE_WINDOW_MINUTES,
    )


_ROOT_CAUSE_RE = re.compile(
    r"\*\*Root cause\*\*\s*[:\-]\s*(.+?)(?=\n[\-\*]|\n###|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_CONFIDENCE_RE = re.compile(
    r"\*\*Confidence\*\*\s*[:\-]\s*([0-9]*\.?[0-9]+)",
    re.IGNORECASE,
)


def _extract_root_cause(text: str) -> str | None:
    if not text:
        return None
    m = _ROOT_CAUSE_RE.search(text)
    if not m:
        return None
    return m.group(1).strip().split("\n")[0][:400]


def _extract_confidence(text: str) -> float:
    if not text:
        return 0.0
    m = _CONFIDENCE_RE.search(text)
    if not m:
        return 0.0
    try:
        val = float(m.group(1))
    except Exception:
        return 0.0
    # Some LLMs emit "80%" — coerce to 0-1 scale if > 1.
    if val > 1.0:
        val = val / 100.0
    return max(0.0, min(val, 1.0))


# ---------------------------------------------------------------------------
# State transitions / persistence helpers
# ---------------------------------------------------------------------------

def _transition(investigation_id: int, **fields) -> None:
    try:
        writer.update_investigation(investigation_id, **fields)
    except Exception as e:
        logger.warning(f"investigation transition failed ({investigation_id}, {fields}): {e}")


def _terminal(investigation_id: int, status: str, **fields) -> None:
    fields.setdefault("status", status)
    fields.setdefault("ended_at", _now_iso())
    _transition(investigation_id, **fields)


def _load_investigation(investigation_id: int) -> dict | None:
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                "SELECT * FROM investigations WHERE id = ?", (investigation_id,)
            ).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.warning(f"_load_investigation({investigation_id}): {e}")
        return None


def _load_alert_for(inv: dict) -> InboundAlert | None:
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                "SELECT * FROM inbound_alerts WHERE id = ?",
                (inv["inbound_alert_id"],),
            ).fetchone()
            if row is None:
                return None
            raw = {}
            try:
                raw = json.loads(row["payload"] or "{}")
            except Exception:
                raw = {}
            return InboundAlert(
                provider=row["provider"],
                external_id=row["external_id"] or f"{row['provider']}:{row['id']}",
                alert_type=row["alert_type"],
                severity=row["severity"],
                summary=row["summary"] or "",
                fired_at=row["received_at"],
                server_id=row["server_id"],
                callback_url=row["callback_url"],
                context={},
                raw_payload=raw,
                signature_verified=bool(row["signature_verified"]),
            )
    except Exception as e:
        logger.warning(f"_load_alert_for: {e}")
        return None


def _find_overlapping_incident_window(
    server_id: str, window_start: str, window_end: str
) -> int | None:
    try:
        with get_mon_reader() as conn:
            row = conn.execute(
                """
                SELECT id FROM incident_windows
                WHERE server_id = ?
                  AND end_time >= ?
                  AND start_time <= ?
                ORDER BY start_time DESC
                LIMIT 1
                """,
                (server_id, window_start, window_end),
            ).fetchone()
            return int(row["id"]) if row else None
    except Exception as e:
        logger.debug(f"_find_overlapping_incident_window: {e}")
        return None


def _safe_state_report_markdown(server_id: str) -> str:
    try:
        from agent.state_builder import build_state_report
        report = build_state_report(server_id=server_id)
        if hasattr(report, "to_markdown"):
            return report.to_markdown()
    except Exception as e:
        logger.debug(f"state report unavailable: {e}")
    return "_(state report unavailable)_"


def _safe_build_timeline(
    server_id: str, window_start: str, window_end: str
) -> tuple[str, dict[str, int]]:
    try:
        from agent.replay import _build_timeline
        md, events = _build_timeline(server_id, window_start, window_end)
        return md, dict(events)
    except Exception as e:
        logger.debug(f"timeline unavailable: {e}")
        return "_(no timeline events)_", {}


# ---------------------------------------------------------------------------
# Phase 3 scheduler hook (thin stub — CP5 provides the sampler body)
# ---------------------------------------------------------------------------

def _schedule_phase3(investigation_id: int) -> None:
    """
    Register the Phase 3 sampler on the existing APScheduler. CP5 implements
    the sampler; here we just schedule the first tick.
    """
    try:
        from scheduler.runner import _scheduler_instance
    except Exception:
        _scheduler_instance = None  # type: ignore[assignment]

    config = _inv_config()
    interval = int(config.get("phase3_sampling_interval_seconds", 20) or 20)
    run_at = datetime.now(timezone.utc) + timedelta(seconds=interval)

    writer.update_investigation(
        investigation_id, phase3_next_run_at=run_at.isoformat()
    )

    if _scheduler_instance is None:
        logger.info(
            f"Phase 3 scheduled inline-only (no running scheduler) for inv {investigation_id}"
        )
        return

    try:
        from apscheduler.triggers.date import DateTrigger
        from alerting.phase3 import phase3_sample  # lazy; module added in CP5
    except Exception as e:
        logger.info(f"Phase 3 sampler not available yet: {e}")
        return

    try:
        _scheduler_instance.add_job(
            phase3_sample,
            trigger=DateTrigger(run_date=run_at),
            args=[investigation_id],
            id=f"investigation:{investigation_id}:phase3",
            max_instances=1,
            misfire_grace_time=60,
            replace_existing=True,
        )
    except Exception as e:
        logger.warning(f"Failed to schedule Phase 3 for inv {investigation_id}: {e}")


# ---------------------------------------------------------------------------
# Finding dispatch — reuse existing alerting channel infrastructure
# ---------------------------------------------------------------------------

def _dispatch_findings(
    investigation_id: int,
    alert: InboundAlert,
    triage: dict | None = None,
    phase2: dict | None = None,
    triage_only: bool = False,
) -> None:
    """
    Package findings as an `Alert` and send via the configured channels.
    Best-effort: channel failures must never prevent terminal transition.
    """
    try:
        from alerting.models import Alert, Severity
        from alerting.engine import _build_channels
        from config import get_config
    except Exception as e:
        logger.debug(f"dispatch: cannot import channel stack: {e}")
        return

    root = (phase2 or {}).get("root_cause") or (triage or {}).get("hypothesis") or alert.summary
    message = (
        f"Investigation #{investigation_id} for {alert.alert_type} alert on "
        f"server `{alert.server_id}`: {root}"
    )
    context = {
        "investigation_id": investigation_id,
        "alert_type": alert.alert_type,
        "provider": alert.provider,
        "server_id": alert.server_id,
        "triage_only": triage_only,
    }
    if phase2:
        context["confidence"] = phase2.get("confidence")
        context["analysis_id"] = phase2.get("analysis_id")

    sev = alert.severity
    try:
        a = Alert(
            rule_name=f"investigation:{alert.alert_type}",
            severity=Severity(sev) if sev in ("critical", "warning", "info") else Severity.WARNING,
            message=message,
            context=context,
        )
    except Exception as e:
        logger.debug(f"dispatch: Alert() construction failed: {e}")
        return

    try:
        channels = _build_channels(get_config().get("alerting", {}))
    except Exception as e:
        logger.debug(f"dispatch: _build_channels failed: {e}")
        return

    for name, ch in channels.items():
        try:
            ch.send(a)
        except Exception as e:
            logger.debug(f"dispatch: channel {name} send failed: {e}")


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
