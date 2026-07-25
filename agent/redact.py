"""
SQL-literal redaction (P0-9, P2-7).

Several paths put raw statement text in front of the LLM: `trx_query` in the
structured state report, the live `get_live_processlist`/`get_live_locks`/
`get_live_transactions` tools, `search_slow_log` (real SQL from the slow
query log), the replay timeline's lock-wait entries, and the EXPLAIN tools'
echoed query text. All of that can carry literal values from the workload —
emails, phone numbers, internal IDs — which is customer data, not schema
metadata.

`redact_sql` masks literal values (quoted strings, numbers, hex blobs) while
leaving keywords, identifiers, and structure intact, so the model still sees
enough to reason about the query shape. `maybe_redact` is the call site
helper: it reads `agent.redact_sql_literals` from config on every call (so
config changes/test monkeypatches take effect immediately) and applies
`redact_sql` only when the flag is on. The flag defaults to True — redaction
is privacy-first and opt-out, not opt-in.

Redaction is a presentation-layer concern: it must never be applied to SQL
text before it is executed against MySQL (e.g. `EXPLAIN FORMAT=JSON ...`).
Only the text that is returned to the model/prompt goes through
`maybe_redact`.
"""

import re

from config import get_config

# Match both single- and double-quoted string literals. In default MySQL
# sql_mode a double-quoted `"a@b.com"` is a *string literal* (same as single
# quotes), so it carries workload data and must be masked. (Under the rare
# ANSI_QUOTES sql_mode `"..."` is instead an identifier — SeeQL targets the
# default mode, so masking it there is an acceptable, safe over-redaction.)
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'" + r'|"(?:[^"\\]|\\.)*"')
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_HEXBLOB = re.compile(r"\b0x[0-9a-fA-F]+\b")


def redact_sql(sql: str | None) -> str | None:
    """Mask literal values in SQL text (strings, numbers, hex blobs) while
    keeping structure readable. Used before statement text reaches an LLM
    prompt when agent.redact_sql_literals is on."""
    if not sql:
        return sql
    out = _STRING.sub("'?'", sql)
    out = _HEXBLOB.sub("?", out)
    return _NUMBER.sub("?", out)


def maybe_redact(sql: str | None) -> str | None:
    """Return redact_sql(sql) when agent.redact_sql_literals is enabled
    (default: True), else return sql unchanged. Reads config fresh on every
    call — this is called per-row/per-field on small strings, so the cost
    of not caching is negligible and it keeps behavior correct if config
    is reloaded (or monkeypatched in tests) between calls."""
    enabled = get_config().get("agent", {}).get("redact_sql_literals", True)
    return redact_sql(sql) if enabled else sql
