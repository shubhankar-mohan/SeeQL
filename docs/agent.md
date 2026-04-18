# LLM agent

SeeQL's agent layer receives a pre-processed state report, reasons
about it using tool calls, and writes findings + recommendations back
to the `agent_analyses` table.

## Backends

The backend is selected automatically from `agent.model` + available
credentials:

| `agent.model` | GCP creds? | Backend |
|---------------|------------|---------|
| `claude-*` | yes | Vertex AI (`AnthropicVertex`) |
| `claude-*` | no | Anthropic direct (`ANTHROPIC_API_KEY`) |
| `gemini-*` | yes | Vertex AI (`google-genai`) |
| `gemini-*` | no | **fails** — Gemini requires GCP |

Set in `settings.local.yaml`:

```yaml
agent:
  enabled: true
  model: "claude-sonnet-4-6"          # or "gemini-2.0-flash", etc.
  anthropic_api_key: "${ANTHROPIC_API_KEY}"
```

## Prompts

Two user-prompt templates live in
[`agent/prompts.py`](../agent/prompts.py):

- `ROUTINE_ANALYSIS_PROMPT` — scheduled 15-minute check. The agent
  first calls `get_recent_analyses(24, 3)` to avoid re-investigating
  the same persistent issue.
- `INCIDENT_ANALYSIS_PROMPT` — triggered by an alert; prefixed with
  trigger-specific immediate steps (e.g. `lock_cascade` demands
  `get_live_locks()` + `get_live_transactions()` first).

The system prompt (also in `prompts.py`) carries the tool catalog,
severity rubric, output format (`### Severity:` header), and operating
rules.

## Tools

Eight tools defined in [`agent/tools.py`](../agent/tools.py):

| Tool | Reads from | Purpose |
|------|-----------|---------|
| `run_explain(digest)` | local EXPLAIN | EXPLAIN an observed query (uses real parameter values from `query_sample_text` when available) |
| `get_table_schema(schema, table)` | local snapshots | CREATE TABLE DDL + index definitions |
| `get_query_history(digest, days)` | local snapshots | Daily avg-time trend over N days |
| `get_lock_graph()` | local snapshots | Most recent lock-wait snapshot |
| `search_slow_log(keyword, limit)` | local snapshots | Search the Cloud Logging slow query log captures |
| `get_live_processlist()` | live MySQL | Real-time active threads |
| `get_live_locks()` | live MySQL | Real-time InnoDB lock waits |
| `get_live_innodb_status()` | live MySQL | `SHOW ENGINE INNODB STATUS` |
| `get_live_transactions()` | live MySQL | Active InnoDB transactions |
| `get_index_stats(schema, table)` | live MySQL | Index cardinality + usage |
| `get_table_status(schema, table)` | live MySQL | Table size + fragmentation |
| `explain_query(query, schema)` | live MySQL | EXPLAIN arbitrary SQL (for testing rewrites) |

Snapshot tools are fast and always available. Live tools may fail
under load (pool exhaustion, connection loss) — by convention, the
agent treats those failures as infrastructure noise, not production
issues.

## State report

Built by [`agent/state_builder.py`](../agent/state_builder.py), the
state report is a Markdown document with three sections:

1. **Current state** — top queries, lock waits, buffer pool, threads,
   GCP metrics
2. **Changes since last analysis** — new query fingerprints, DDL
   changes, regressions, deadlocks
3. **Historical context** — 7-day baselines, daily avg trends

Plus a "Recent incidents" block (introduced with incident replay) so
routine analyses see unresolved incidents from the last 24 h.

## Analysis lifecycle

```
┌─────────────┐   ┌──────────────┐   ┌─────────────────┐   ┌──────────┐
│  Scheduler  │──►│ build_state_ │──►│ Tool-use loop   │──►│ _parse_  │
│  (15 min)   │   │ report()     │   │ (up to N rounds)│   │ and_store│
└─────────────┘   └──────────────┘   └─────────────────┘   └─────┬────┘
                                                                 │
                         ┌───────────────────────────────────────┘
                         ▼
                  agent_analyses  ──► dashboard + API + Slack (if alert)
```

`agent.skip_quiet: true` (default) short-circuits the scheduler when
the state report has nothing interesting — saves tokens during
overnight quiet periods.

## Output format

The agent MUST open its response with `### Severity:` followed by one
of `critical` / `warning` / `info`. `_parse_and_store` is strict about
this — malformed output is still stored but flagged for the dashboard.

Structure:

```markdown
### Severity: warning
### Findings
- **Category**: missing_index
- **Description**: ...
- **Evidence**: EXPLAIN shows type=ALL on a 6M-row table
### Recommendations
- **Action**: CREATE INDEX idx_phone ON members(phone_number)
- **Expected Impact**: ...
- **Risk**: low
- **Priority**: immediate
```

## Tuning

| Setting | Default | Effect |
|---------|---------|--------|
| `agent.max_tokens` | 8192 | Response length cap |
| `agent.max_tool_rounds` | 10 | Max tool-call rounds per analysis |
| `agent.schedule_seconds` | 900 | 15 min — routine cadence |
| `agent.skip_quiet` | true | Skip cycles with no interesting signal |
| `state_builder.trend_threshold` | 0.2 | 20 % change = up/down vs stable |
| `state_builder.regression_threshold` | 3.0 | 3× slower = "regression" |
| `state_builder.long_transaction_sec` | 30 | "long" TX threshold |

## Running without an LLM

Set `agent.enabled: false`. SeeQL still collects everything, runs
anomaly detection + incidents, serves the dashboard, and evaluates
alerts. You lose root-cause narration and `seeql replay` analysis
(replay still shows the timeline).
