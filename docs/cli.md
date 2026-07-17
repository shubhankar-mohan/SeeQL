# CLI reference

SeeQL ships a single `seeql` command with nine subcommands. Install
via `pip install -e ".[api]"` or the Docker image — both expose the
`seeql` console script.

## Overview

```
seeql <command> [options]

Commands:
  check           Run health checks and exit
  doctor          Diagnose the local environment (7 checks)
  init-db         Initialize the monitoring SQLite schema
  run             Continuous collector loop (add --once for a single cycle)
  serve           API + dashboard + scheduler
  replay          Reconstruct + narrate a past incident
  incidents       Browse detected incidents
  investigations  Manage webhook-triggered investigations
  mcp             Run the SeeQL MCP server (stdio by default, HTTP with --http)
```

Exit codes: `0` on success. On a `SeeQLError`, the exit code is the
numeric suffix of the error code (`E007` → exit 7). On an unexpected
exception, Python prints the full traceback and exits non-zero.

---

## `seeql check`

Fast preflight. Exits 0 if SeeQL can:

1. Load and validate `settings.yaml`
2. Connect to the production MySQL as `dba_agent`
3. Open / create the monitoring SQLite DB

**Exits:**

- `0` healthy
- `E001` / `E006` / `E007` MySQL issues
- `E004` config invalid
- `E008` SQLite issues

**Use when:** verifying a deploy before starting `seeql serve` / `run`.

---

## `seeql doctor`

Deeper diagnostic — runs 7 checks and prints a coloured pass/fail
table:

1. Python version (≥3.12)
2. Config file loadable
3. Production MySQL reachable + credentials valid
4. `performance_schema` enabled on the target
5. Required grants present (`SELECT`, `PROCESS`)
6. Monitoring SQLite writable + schema migrated
7. LLM backend reachable (if `agent.enabled: true`)

Pretty-prints the remediation for any failure with a link to the
relevant [error page](errors/).

**Use when:** "it used to work" — doctor narrows down which layer
broke.

---

## `seeql init-db`

Creates `data/mysql_monitor.db` and runs
[`storage/schema.sql`](../storage/schema.sql) to create all 30 tables.
Idempotent — safe to re-run on an existing DB.

**Use when:** first-time setup, or after wiping `data/` for a clean
start.

---

## `seeql run`

Starts the continuous collector with three scheduled loops:

- Fast loop — every 30 s (processlist, lock waits, transactions,
  metadata locks)
- Medium loop — every 5 min (query digests, wait events, table IO,
  InnoDB metrics, buffer pool, global status, etc.)
- Slow loop — every 30 min (schema snapshots, index analysis, global
  variables)

Plus a daily retention job.

### `seeql run --once`

Run exactly one cycle of all loops and exit. Useful for cron-driven
deployments, test environments, or troubleshooting ("does anything
collect?").

---

## `seeql serve`

Starts uvicorn at `0.0.0.0:8080` serving the FastAPI app + dashboard +
Prometheus `/metrics` endpoint. By default also starts the scheduler
in-process, so one container can do everything.

### `seeql serve --no-scheduler`

API + dashboard only, no collectors. Use when you have a separate
collector pod (`seeql run`) writing to a shared SQLite volume, and you
just want a read-only API replica.

---

## `seeql replay`

Reconstructs a chronological timeline of anomalies, locks, DDL changes,
and metric snapshots inside a window, then optionally asks the LLM to
narrate the root cause.

```bash
# By window
seeql replay --from 2026-04-10T03:00:00 --to 2026-04-10T04:00:00

# By incident id (see `seeql incidents list`)
seeql replay --incident 42

# Most recent detected incident
seeql replay --latest

# Multi-server deployments: pin a server
seeql replay --latest --server db-prod-west
```

The LLM analysis falls back to timeline-only if no backend is
configured — `--latest` still works offline. Analysis writes to
`agent_analyses` and prints a Markdown summary to stdout.

**See:** [Incidents & replay](incidents.md), [E010 — invalid time
range](errors/E010.md).

---

## `seeql incidents list`

Browses the `incident_windows` table. Useful to find the incident id
you want to replay, or to sanity-check that incident detection is
firing.

```bash
seeql incidents list                      # 20 most recent
seeql incidents list --limit 100
seeql incidents list --status detected    # still open
seeql incidents list --status resolved
seeql incidents list --server db-prod-west
```

Output columns: id, start, end, duration, severity, involved metrics,
status.

---

## `seeql investigations list|show|trigger|abort`

Manages webhook-triggered root-cause investigations (see
[docs/api.md](api.md) and the `webhooks:` / `investigator:` sections in
`settings.yaml`). An investigation is created when `POST
/webhooks/{provider}` receives an alert, and runs a 3-phase pipeline:
zero-query triage, budgeted LLM tool calls, then continuous sampling
with a load guard.

```bash
# List recent investigations (20 most recent by default)
seeql investigations list
seeql investigations list --status phase2 --limit 50
seeql investigations list --server db-prod-west

# Show one investigation in full — findings + sample counts
seeql investigations show 17

# Manually trigger one — testing / incident drills
seeql investigations trigger --type lock_cascade --severity critical \
    --server db-prod-west --summary "drill: simulated lock cascade"

# Abort a running investigation
seeql investigations abort 17 --reason "false alarm"
```

`list --status` filters on `queued`, `phase1`, `phase2`, `phase3`,
`completed`, `aborted`, or `load_guard_paused`. `trigger --severity`
accepts `critical`, `warning`, or `info` (default `warning`); `--type`
defaults to `default` and `--server` defaults to the primary server.
`abort --reason` defaults to `cli_abort`.

---

## `seeql mcp`

Runs the SeeQL [Model Context Protocol](https://modelcontextprotocol.io/)
server so external MCP clients (Claude Desktop, Claude Code, custom HTTP
clients) can use SeeQL as a root-cause-analysis surface. Requires the
`mcp` extra: `pip install 'seeql[mcp]'` (or `pip install 'mcp>=1.2'`).

```bash
seeql mcp                          # stdio transport (default)
seeql mcp --http                   # streamable HTTP/SSE transport
seeql mcp --http --port 9000 --bind 0.0.0.0
```

- `--http` — use streamable HTTP/SSE instead of stdio
- `--port` — HTTP port (default: `mcp.http.port` in `settings.yaml`, or `8765`)
- `--bind` — HTTP bind address (default: `mcp.http.bind`, or `127.0.0.1`)

**See:** [docs/mcp.md](mcp.md) for the full tool/resource/prompt catalog,
safety-rail config, and the Claude Desktop config snippet.

---

## Legacy flags

Pre-subparser invocations still work for one release with a deprecation
warning:

| Legacy | Equivalent |
|--------|-----------|
| `python main.py --check` | `seeql check` |
| `python main.py --init-db` | `seeql init-db` |
| `python main.py --once` | `seeql run --once` |
| `python main.py --api` | `seeql serve` |
| `python main.py --api-only` | `seeql serve --no-scheduler` |

The `--check/--once/--api/--api-only` flags are removed in v0.2.0.
