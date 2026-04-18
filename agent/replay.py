"""
Incident replay (Phase 1.6).

Rebuilds a chronological timeline from the persisted monitoring data for a
time window, then optionally passes it to the LLM for root-cause analysis.
If no LLM backend is configured, the timeline alone is still valuable — the
output is a complete postmortem primer a DevOps engineer can read in under
two minutes.

Entry points:
    run_replay(from_ts, to_ts, server_id=None, incident_id=None) -> ReplayResult
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from storage.connection import get_mon_reader
from agent.prompts import INCIDENT_INVESTIGATOR_PROMPT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------
@dataclass
class ReplayResult:
    from_ts: str
    to_ts: str
    server_id: str
    incident_id: int | None
    timeline_md: str
    analysis_md: str | None = None
    analysis_id: int | None = None
    severity: str | None = None
    events_by_category: dict[str, int] = field(default_factory=dict)

    def to_markdown(self) -> str:
        hdr = (
            f"# Incident Replay — {self.from_ts} → {self.to_ts}"
            f" ({self.server_id})"
        )
        if self.incident_id is not None:
            hdr += f"  — incident #{self.incident_id}"

        counts = ", ".join(
            f"{k}={v}" for k, v in self.events_by_category.items() if v > 0
        ) or "no events in window"

        body = [
            hdr,
            "",
            f"_Events: {counts}_",
            "",
            "## Timeline",
            "",
            self.timeline_md,
            "",
        ]

        if self.analysis_md:
            body += [
                "## Root Cause Analysis",
                "",
                self.analysis_md,
                "",
            ]
        else:
            body += [
                "## Root Cause Analysis",
                "",
                "_LLM analysis unavailable. Configure GCP credentials "
                "(GOOGLE_APPLICATION_CREDENTIALS + project_id) or "
                "ANTHROPIC_API_KEY for automated root cause narration. "
                "The timeline above is still a complete postmortem primer._",
                "",
            ]
        return "\n".join(body)


# ---------------------------------------------------------------------------
# Timeline builder
# ---------------------------------------------------------------------------
_TIMELINE_ANOMALIES = """
SELECT detected_at AS ts,
       metric_name, current_value, baseline_mean, z_score, severity, direction
FROM anomaly_events
WHERE server_id = ? AND detected_at BETWEEN ? AND ?
ORDER BY detected_at ASC
"""

_TIMELINE_LOCK_WAITS = """
SELECT snapshot_time AS ts,
       waiting_pid, blocking_pid, wait_seconds, waiting_query, blocking_query
FROM lock_wait_snapshots
WHERE server_id = ? AND snapshot_time BETWEEN ? AND ?
ORDER BY snapshot_time ASC
"""

_TIMELINE_DDL = """
SELECT detected_at AS ts,
       table_schema, table_name, change_type
FROM ddl_changes
WHERE server_id = ? AND detected_at BETWEEN ? AND ?
ORDER BY detected_at ASC
"""

_TIMELINE_DEADLOCKS = """
SELECT snapshot_time AS ts, section_name
FROM innodb_status_snapshots
WHERE server_id = ?
  AND section_name = 'LATEST DETECTED DEADLOCK'
  AND snapshot_time BETWEEN ? AND ?
ORDER BY snapshot_time ASC
"""

_TIMELINE_THREADS = """
SELECT snapshot_time AS ts, raw_value
FROM global_status_snapshots
WHERE server_id = ?
  AND variable_name = 'Threads_running'
  AND snapshot_time BETWEEN ? AND ?
ORDER BY snapshot_time ASC
"""


def _build_timeline(server_id: str, from_ts: str, to_ts: str) -> tuple[str, dict]:
    """Run all timeline queries, interleave by timestamp, return markdown + counts."""
    events: list[tuple[str, str]] = []
    counts = {
        "anomalies": 0,
        "lock_waits": 0,
        "ddl_changes": 0,
        "deadlocks": 0,
        "threads_samples": 0,
    }

    with get_mon_reader() as conn:
        for row in conn.execute(_TIMELINE_ANOMALIES, (server_id, from_ts, to_ts)):
            events.append((
                row["ts"],
                f"**ANOMALY** [{row['severity']}] `{row['metric_name']}` "
                f"= {row['current_value']:.2f} "
                f"(baseline {row['baseline_mean']:.2f}, z={row['z_score']:.1f}, "
                f"{row['direction']})",
            ))
            counts["anomalies"] += 1

        for row in conn.execute(_TIMELINE_LOCK_WAITS, (server_id, from_ts, to_ts)):
            wq = (row["waiting_query"] or "")[:80]
            events.append((
                row["ts"],
                f"**LOCK** pid={row['waiting_pid']} waiting {row['wait_seconds']}s "
                f"for pid={row['blocking_pid']} — `{wq}`",
            ))
            counts["lock_waits"] += 1

        for row in conn.execute(_TIMELINE_DDL, (server_id, from_ts, to_ts)):
            events.append((
                row["ts"],
                f"**DDL** {row['change_type']} on `{row['table_schema']}.{row['table_name']}`",
            ))
            counts["ddl_changes"] += 1

        for row in conn.execute(_TIMELINE_DEADLOCKS, (server_id, from_ts, to_ts)):
            events.append((
                row["ts"],
                "**DEADLOCK** — see innodb_status_snapshots for full graph",
            ))
            counts["deadlocks"] += 1

        # Threads: only emit every Nth sample to avoid drowning the LLM
        threads_rows = list(
            conn.execute(_TIMELINE_THREADS, (server_id, from_ts, to_ts))
        )
        counts["threads_samples"] = len(threads_rows)
        step = max(1, len(threads_rows) // 10)
        for i, row in enumerate(threads_rows):
            if i % step == 0:
                events.append((
                    row["ts"],
                    f"_threads_running_ = {row['raw_value']}",
                ))

    events.sort(key=lambda e: e[0])

    if not events:
        return ("_No events recorded in this window._", counts)

    lines = [f"- `{ts}` — {msg}" for ts, msg in events]
    return ("\n".join(lines), counts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def run_replay(
    from_ts: str,
    to_ts: str,
    server_id: str | None = None,
    incident_id: int | None = None,
) -> ReplayResult:
    """
    Run a full replay: timeline + LLM analysis + incident linking.

    The replay is resilient to partial failures:
      - If timeline construction fails, we still return a (probably empty)
        ReplayResult — the caller can distinguish this from "no events".
      - If the LLM call fails or no backend is configured, we return the
        timeline-only form with analysis_md=None.
      - If the incident linking update fails, we log and continue — the
        replay output is still useful.
    """
    if server_id is None:
        try:
            from config.server_registry import get_server_registry
            server_id = get_server_registry().get_default_server_id()
        except Exception:
            server_id = "default"

    try:
        timeline_md, counts = _build_timeline(server_id, from_ts, to_ts)
    except Exception as e:
        logger.warning(f"Timeline build failed: {e}")
        timeline_md = f"_Timeline unavailable: {e}_"
        counts = {}

    result = ReplayResult(
        from_ts=from_ts,
        to_ts=to_ts,
        server_id=server_id,
        incident_id=incident_id,
        timeline_md=timeline_md,
        events_by_category=counts,
    )

    # LLM analysis — best-effort, never blocks returning the timeline
    try:
        from agent.llm_agent import run_llm_analysis

        incident_line = (
            f"- Incident ID: #{incident_id}" if incident_id is not None else ""
        )
        prompt = INCIDENT_INVESTIGATOR_PROMPT.format(
            from_ts=from_ts,
            to_ts=to_ts,
            server_id=server_id,
            incident_line=incident_line,
            timeline=timeline_md,
        )
        llm = run_llm_analysis(
            prompt=prompt,
            analysis_type="replay",
            server_id=server_id,
        )
        result.analysis_md = llm.get("text")
        result.analysis_id = llm.get("analysis_id")
        result.severity = llm.get("severity")

        # Link the incident window to the analysis
        if incident_id is not None and result.analysis_id is not None:
            try:
                from storage.connection import get_mon_connection
                with get_mon_connection() as conn:
                    conn.execute(
                        """UPDATE incident_windows
                           SET analysis_id = ?, status = 'analyzed'
                           WHERE id = ?""",
                        (result.analysis_id, incident_id),
                    )
            except Exception as e:
                logger.warning(f"Failed to link incident {incident_id} to analysis: {e}")
    except RuntimeError as e:
        # _detect_backend returned None — no LLM configured
        logger.info(f"Replay LLM unavailable: {e} — timeline-only replay")
    except Exception as e:
        logger.warning(f"Replay LLM analysis failed: {e}")

    return result
