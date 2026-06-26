# MCP Server

SeeQL ships a [Model Context Protocol](https://modelcontextprotocol.io/)
server so any MCP-speaking client — Claude Desktop, Claude Code, a custom
HTTP client — can use SeeQL as a full Root-Cause-Analysis surface for
MySQL.

## What it exposes

- **28 tools** spanning investigations, incidents, state reports,
  per-query history, cached EXPLAINs, missing-index correlation, live
  MySQL reads (processlist, locks, transactions, InnoDB status, index
  usage, table status), and — behind config gates — arbitrary EXPLAIN
  and the ability to trigger / abort investigations.
- **7 resources** under `seeql://` (servers, recent investigations /
  incidents, replay markdown, current state markdown).
- **5 prompts** (`seeql/rca`, `seeql/review_investigation`,
  `seeql/explain_digest`, `seeql/schema_audit`,
  `seeql/investigate_window`) that walk the LLM through common
  workflows.

Safety rails are on by default: live-MySQL tools are budgeted per
session, arbitrary `EXPLAIN` and investigation triggering are off until
you opt in, and an optional server allowlist narrows what the client
can see.

## Install

```bash
pip install 'seeql[mcp]'
# or, from the repo:
pip install 'mcp>=1.2'
```

## Run: stdio (Claude Desktop, Claude Code, subprocess clients)

```bash
seeql mcp
```

No arguments = stdio. The server reads JSON-RPC from stdin, writes to
stdout, logs to stderr.

### Claude Desktop config

Add an entry to
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or the equivalent on Windows / Linux:

```json
{
  "mcpServers": {
    "seeql": {
      "command": "/path/to/SeeQL/venv/bin/python",
      "args": ["main.py", "mcp"],
      "cwd": "/path/to/SeeQL"
    }
  }
}
```

Restart Claude Desktop. SeeQL's tools appear under the hammer icon in
the input box. Ask "Do an RCA on my MySQL" — Claude picks the right
tools on its own.

### Claude Code

In a project directory, create `.mcp.json`:

```json
{
  "mcpServers": {
    "seeql": {
      "command": "/path/to/SeeQL/venv/bin/python",
      "args": ["main.py", "mcp"],
      "cwd": "/path/to/SeeQL"
    }
  }
}
```

## Run: HTTP / SSE (remote clients)

```bash
seeql mcp --http
# or
SEEQL_MCP_TOKEN=your-secret seeql mcp --http --port 8765 --bind 127.0.0.1
```

Default bind is `127.0.0.1:8765` with bearer auth. To hit the server:

```bash
curl -i -X POST http://127.0.0.1:8765/mcp \
  -H "Authorization: Bearer $SEEQL_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

If you bind to a non-loopback interface with `auth: none`, the server
logs a loud warning.

## Configuration

All tuneables live under `mcp:` in `config/settings.yaml`:

```yaml
mcp:
  enabled: true
  server_name: "seeql"
  allowed_servers: []                 # Empty = any monitored server is visible
  action_tools_enabled: false         # Master gate for trigger / abort / explain_query
  allow_trigger: false                # Fine-grain: seeql_trigger_investigation
  allow_abort: false                  # Fine-grain: seeql_abort_investigation
  allow_explain_query: false          # Fine-grain: seeql_explain_query
  budget:
    live_calls_per_session: 30        # How many live-MySQL tool calls the client may make
    explain_calls_per_session: 5      # Hard cap on arbitrary EXPLAIN
    tools_per_minute: 60              # Per-tool-name token bucket
  http:
    bind: "127.0.0.1"
    port: 8765
    auth: "bearer"                    # bearer | none
    auth_token: "${SEEQL_MCP_TOKEN}"  # env-var-substituted
```

## Tool surface

### Read-only (always available, zero MySQL cost)

| Tool | What it does |
|------|--------------|
| `seeql_list_servers` | All monitored MySQL servers. |
| `seeql_get_state_report` | Current structured state report (rich). |
| `seeql_list_investigations` | Recent investigations, filterable. |
| `seeql_get_investigation` | Full detail: row + findings + samples. |
| `seeql_list_incidents` | Gap-clustered anomaly windows. |
| `seeql_get_incident` | Window + constituent anomaly events. |
| `seeql_replay_incident` | Chronological timeline + optional LLM RCA. |
| `seeql_replay_window` | Same, for arbitrary time ranges. |
| `seeql_top_queries` | Top-N digests by total_time / avg / ratio / rows. |
| `seeql_get_query_history` | Per-digest trend + latest cached EXPLAIN. |
| `seeql_run_explain` | Cached EXPLAIN for a digest. |
| `seeql_search_slow_log` | Keyword search in slow query log. |
| `seeql_find_missing_index_candidates` | Missing-index correlator. |
| `seeql_get_table_schema` | Cached DDL with live fallback. |
| `seeql_list_unused_indexes` | `sys.schema_unused_indexes` snapshot. |
| `seeql_list_redundant_indexes` | `sys.schema_redundant_indexes` snapshot. |
| `seeql_get_recent_ddl_changes` | DDL history for correlation. |
| `seeql_get_lock_graph` | Recent lock + txn snapshot. |
| `seeql_get_recent_analyses` | Agent's prior findings (check this first). |

### Live (budgeted)

| Tool | Cost |
|------|------|
| `seeql_get_live_processlist` | 1 MySQL query |
| `seeql_get_live_locks` | 1 |
| `seeql_get_live_transactions` | 1 |
| `seeql_get_live_innodb_status` | 1 |
| `seeql_get_index_stats` | 2 |
| `seeql_get_table_status` | 1 |

Each call consumes one slot from `budget.live_calls_per_session`.

### Action (gated)

| Tool | Required flags |
|------|----------------|
| `seeql_trigger_investigation` | `action_tools_enabled=true`, `allow_trigger=true` |
| `seeql_abort_investigation` | `action_tools_enabled=true`, `allow_abort=true` |
| `seeql_explain_query` | `action_tools_enabled=true`, `allow_explain_query=true`; counts against `budget.explain_calls_per_session` |

When a gate is off, the tool call returns a structured error (`rejected_by:
mcp_safety`) so the LLM naturally backs off.

## Resources

Resources are read-without-a-tool-call. The client can pull these
URIs directly:

- `seeql://servers` (JSON)
- `seeql://investigations/recent` (JSON)
- `seeql://investigations/{id}` (JSON)
- `seeql://incidents/recent` (JSON)
- `seeql://incidents/{id}` (JSON)
- `seeql://incidents/{id}/replay.md` (markdown)
- `seeql://state/{server_id}.md` (markdown)

## Prompts

Clients surface prompts as pre-packaged flows the user can invoke:

- `seeql/rca [server]` — discover → state report → check prior work → correlate → drill in → propose.
- `seeql/review_investigation investigation_id` — read + critique an existing investigation.
- `seeql/explain_digest digest [days]` — deep-dive one query.
- `seeql/schema_audit [server] [table]` — unused + redundant + missing indexes.
- `seeql/investigate_window from_ts to_ts [server]` — free-form time-range RCA.

## Troubleshooting

**"MCP server is disabled"** → set `mcp.enabled: true` in
`settings.yaml` (it's true by default, so this only fires if someone
explicitly flipped it off).

**`seeql_explain_query` returns "disabled"** → that tool needs both
`mcp.action_tools_enabled: true` and `mcp.allow_explain_query: true`.
Prefer `seeql_run_explain` (cached) whenever possible.

**Stdio client disconnects immediately** → check stderr from the child
process. Common causes: missing `mcp` package (`pip install mcp`),
malformed `settings.local.yaml`, monitoring SQLite path not writable.

**HTTP 401** → the bearer token in your `Authorization` header doesn't
match `SEEQL_MCP_TOKEN` (or `mcp.http.auth_token`).

## See also

- [Agent docs](./agent.md) — the internal LLM agent the MCP server's
  tools wrap.
- [Alerting docs](./alerting.md) — the webhook investigator the
  `seeql_trigger_investigation` tool feeds.
- [Architecture](./architecture.md) — where the MCP server sits in
  the overall data flow.
