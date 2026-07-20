"""
MCP prompts — templated prompts the client surfaces to the user.

These don't call tools themselves; they produce a message string that
includes instructions + seed context the LLM then works from (often
by calling the tools registered in mcp_server/tools/).
"""

import logging
import re

logger = logging.getLogger(__name__)

# P2-8: prompt args (digest/table/server) are interpolated directly into the
# returned prompt text, which the calling LLM then reads as instructions —
# an unvalidated value could break out of its "just a name" context and
# inject new instructions. Validate before interpolation, not after.

# Same bare-identifier contract as agent.tools._IDENT_RE — table names have
# no legitimate use for anything outside this character set, and a value
# that fails this is already guaranteed to be rejected by the downstream
# seeql_get_table_schema/seeql_get_index_stats tool calls anyway.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
# server_id is a SeeQL config key (operator-chosen), not a MySQL identifier —
# this codebase's own server ids use hyphens (e.g. "prod-primary"), so this
# is intentionally more permissive than _IDENT_RE while still rejecting
# whitespace/quotes/backticks/braces/semicolons that could break out of the
# surrounding prompt text.
_SERVER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
# MySQL performance_schema digests are SHA-256-family hex strings.
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
# ISO-8601-ish timestamp: accepts both the `T` and space separators the
# codebase actually produces (naive `YYYY-MM-DDTHH:MM:SS`, space-separated,
# optional fractional seconds, optional `Z`/`+HH:MM` offset). Lenient enough
# not to reject the app's own timestamps, strict enough that a value can't
# carry quotes/newlines/instructions into the interpolated prompt text.
_ISO_TS_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:?\d{2}|Z)?"
)


def _require_digest(digest: str) -> None:
    if not digest or not _DIGEST_RE.fullmatch(digest):
        raise ValueError(
            f"Invalid digest {digest!r} — expected an 8-64 character hex string "
            "(a performance_schema query digest)."
        )


def _require_table(table: str) -> None:
    if not table or not _IDENT_RE.fullmatch(table):
        raise ValueError(
            f"Invalid table name {table!r} — expected a bare identifier: "
            "letters, digits, underscore, no quotes/spaces/semicolons."
        )


def _require_server(server: str) -> None:
    if not server or not _SERVER_ID_RE.fullmatch(server):
        raise ValueError(
            f"Invalid server id {server!r} — expected letters, digits, "
            "underscore, dot, or hyphen only."
        )


def _require_timestamp(ts: str) -> None:
    if not ts or not _ISO_TS_RE.fullmatch(ts):
        raise ValueError(
            f"Invalid timestamp {ts!r} — expected an ISO-8601 datetime like "
            "2026-04-23T12:34:56 (the `T` or a space separator, optional "
            "fractional seconds, optional Z/+HH:MM offset)."
        )


def register(mcp) -> None:
    @mcp.prompt(
        name="seeql/rca",
        description=(
            "Walk the LLM through a full RCA of the current state: discover "
            "servers, pull state reports, check for ongoing investigations, "
            "correlate missing-index signals, and propose a fix. Use when "
            "the user says 'investigate' or 'what's wrong with my DB'."
        ),
    )
    def rca_prompt(server: str | None = None) -> str:
        if server is not None:
            _require_server(server)
        server_hint = (
            f" Target server: `{server}`. "
            if server
            else " Use seeql_list_servers first to discover available servers. "
        )
        return (
            "You are doing RCA on a MySQL database monitored by SeeQL.\n\n"
            "Protocol (do these steps in order, stopping as soon as you "
            "have a confident root cause):\n\n"
            f"1. **Discover**.{server_hint}"
            "Then call seeql_get_state_report to see the current picture.\n"
            "2. **Check prior work**. Call seeql_get_recent_analyses(24) "
            "and seeql_list_investigations(limit=5) so you don't re-do "
            "analysis SeeQL has already done.\n"
            "3. **Correlate**. If the state report shows missing-index "
            "candidates or the top queries look suspicious, call "
            "seeql_find_missing_index_candidates to join cached EXPLAINs + "
            "recent DDL + unused indexes into structured evidence.\n"
            "4. **Drill in**. For any suspect digest, use seeql_run_explain "
            "(cached) first. Only fall back to live tools "
            "(seeql_get_live_processlist, seeql_get_live_locks, etc.) if "
            "the cached data can't answer the question.\n"
            "5. **Propose**. End with a single concrete remediation: an "
            "exact CREATE INDEX / DROP INDEX / query rewrite / KILL pid.\n\n"
            "Be concise. Cite the tool call that gave you each piece of "
            "evidence. Confidence on a 0.0-1.0 scale."
        )

    @mcp.prompt(
        name="seeql/review_investigation",
        description=(
            "Read an existing SeeQL investigation end-to-end and critique "
            "it: was the hypothesis right, is the evidence convincing, is "
            "the recommendation specific enough?"
        ),
    )
    def review_investigation_prompt(investigation_id: int) -> str:
        return (
            f"Review SeeQL investigation #{investigation_id}.\n\n"
            f"1. Call seeql_get_investigation({{'id': {investigation_id}}}) "
            "to pull the row, every finding (phase 1/2/3), and the sample "
            "rollup.\n"
            "2. Summarize: what did SeeQL think the root cause was? What "
            "evidence backed it?\n"
            "3. Critique: is the hypothesis consistent with the evidence? "
            "Is the recommendation specific and safe?\n"
            "4. If the investigation is still running (status in phase1..3), "
            "call seeql_get_state_report to check whether the issue has "
            "since cleared.\n"
            "5. Output a structured review: Root cause / Evidence strength "
            "(0-1) / Recommendation specificity (0-1) / Your verdict."
        )

    @mcp.prompt(
        name="seeql/explain_digest",
        description=(
            "Deep-dive one query digest: what it does, when it got slow, "
            "why, and what to do about it."
        ),
    )
    def explain_digest_prompt(digest: str, days: int = 7) -> str:
        _require_digest(digest)
        return (
            f"Deep-dive the query digest `{digest}`.\n\n"
            f"1. seeql_get_query_history(digest='{digest}', days={days}) — "
            "how has performance changed?\n"
            f"2. seeql_run_explain(digest='{digest}') — what does the plan "
            "look like?\n"
            "3. If the latest snapshot shows high rows_examined/rows_sent, "
            "call seeql_find_missing_index_candidates(suspect_digests="
            f"['{digest}']) for correlator evidence.\n"
            "4. seeql_get_recent_ddl_changes(hours=72) — was schema touched "
            "when the regression appeared?\n"
            "5. Output: what this query does, when it got slow, why, and "
            "exact remediation."
        )

    @mcp.prompt(
        name="seeql/schema_audit",
        description=(
            "Audit a server's (or table's) indexes for missing, unused, "
            "and redundant indexes."
        ),
    )
    def schema_audit_prompt(
        server: str | None = None, table: str | None = None,
    ) -> str:
        if server is not None:
            _require_server(server)
        if table is not None:
            _require_table(table)
        target = f"for table `{table}`" if table else "for the whole server"
        server_note = f" (server `{server}`)" if server else ""
        return (
            f"Audit indexes {target}{server_note}.\n\n"
            "1. seeql_list_unused_indexes — candidates to DROP (save disk, "
            "save write-time cost).\n"
            "2. seeql_list_redundant_indexes — each row ships a ready-made "
            "DROP INDEX statement.\n"
            "3. seeql_find_missing_index_candidates — digests missing "
            "coverage.\n"
            "4. For any strong missing-index candidate, call "
            "seeql_get_table_schema to see the current DDL + "
            "seeql_get_index_stats (live — one budgeted call) to confirm "
            "no existing index already covers the predicate.\n"
            "5. Output a prioritized plan: drop these, add these, with "
            "exact SQL."
        )

    @mcp.prompt(
        name="seeql/investigate_window",
        description=(
            "RCA for a specific time window that may or may not have "
            "clustered into a formal incident."
        ),
    )
    def investigate_window_prompt(
        from_ts: str, to_ts: str, server: str | None = None,
    ) -> str:
        _require_timestamp(from_ts)
        _require_timestamp(to_ts)
        if server is not None:
            _require_server(server)
        server_arg = f", server='{server}'" if server else ""
        return (
            f"Reconstruct what happened between {from_ts} and {to_ts}.\n\n"
            f"1. seeql_replay_window(from_ts='{from_ts}', to_ts='{to_ts}'"
            f"{server_arg}) — the timeline + LLM narration if an LLM backend "
            "is configured.\n"
            "2. seeql_get_recent_ddl_changes around the window edges.\n"
            "3. For the top suspect digest(s) identified by the replay, "
            "run seeql_get_query_history to see the trend.\n"
            "4. Output: trigger → cascade → root cause → recommendation."
        )
